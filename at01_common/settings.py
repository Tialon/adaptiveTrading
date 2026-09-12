"""
主配置
"""

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# V11.3 P0-2: 冻结产品定义 —— 单币 SOLUSDT(现货)。非 SOLUSDT 由 validate() fail-fast,
# 不静默支持多币种生产模式。
SUPPORTED_SYMBOLS: tuple[str, ...] = ("SOLUSDT",)


def _parse_take_profit_ladder(raw: str) -> list[tuple[float, float]]:
    """解析分批止盈阶梯 "5:20,10:30,20:50" -> [(0.05, 0.20), ...] (仅用于配置审计)。"""
    ladder: list[tuple[float, float]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        left, right = part.split(":", 1)
        try:
            pct = float(left) / 100.0
            ratio = float(right) / 100.0
        except ValueError:
            continue
        if pct > 0 and 0 < ratio <= 1.0:
            ladder.append((pct, ratio))
    return sorted(ladder, key=lambda x: x[0])


class Settings(BaseSettings):
    """系统配置(支持 .env 与环境变量覆盖)"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # 应用配置
    app_name: str = "adaptiveTrading"
    app_version: str = "0.1.0"
    debug: bool = False

    # 版本追踪(V11.8 §7): 容器内无 .git, 运行时 git rev-parse 会空; 由镜像构建 ARG GIT_SHA
    # 打进 ENV, 与 compose 注入的 IMAGE_TAG 一起, 使「Pi → 容器 → 镜像 → git SHA」可完整追踪。
    git_sha: str = ""
    image_tag: str = ""

    # 交易标的与运行模式
    symbols: str = "SOLUSDT"  # 冻结单币 SOLUSDT(非 SOLUSDT 由 validate() fail-fast)
    paper_trading: bool = True  # 纸面交易(模拟成交),false 时走实盘
    paper_initial_cash: float = 100000.0  # 纸面交易初始资金 USDT
    paper_fee_rate: float = 0.001  # 纸面交易手续费率
    live_trading_confirm: str = ""  # 主网实盘安全守卫: 显式设 "true" 才允许主网启动

    # V12.7: 运行模式(操作者唯一需要理解的模式开关)。
    #   "paper" / "testnet" / "live" 三选一; **留空 = 按旧配置推导**(兼容老 .env)。
    # 设置后即为权威, 解析结果驱动 paper_trading / binance_testnet / run_testnet_trading,
    # 于是既有守卫读到的仍是自洽的值(不改 TradingGate / RiskManager / ExecutionEngine 逻辑)。
    # 与显式写下的旧字段明显冲突 → fail-closed, 不静默选择其一。见 at01_common/trading_mode.py。
    trading_mode: str = ""

    # 数据库配置(开发默认 SQLite,生产切 MySQL)
    database_url: str = "sqlite+aiosqlite:///./adaptive.db"
    database_echo: bool = False

    # Redis 配置(可选,不可用时降级为内存缓存)
    redis_url: str = "redis://localhost:6379/0"
    redis_enabled: bool = False

    # 日志配置
    log_level: str = "INFO"
    log_file: str = "logs/adaptive.log"

    # Web 配置
    # V11.5 P0-1: 默认绑定回环地址, 不默认暴露到 0.0.0.0。局域网/公网需显式改 API_HOST 并配 WEB_ADMIN_TOKEN。
    api_host: str = "127.0.0.1"
    api_port: int = 8800
    # V11.5 P0-1: Web 写接口共享令牌(空 = 写接口锁定 fail-closed)。设 WEB_ADMIN_TOKEN 开启。
    web_admin_token: str = ""
    # V12.6: 写接口鉴权开关。**默认 "off"**(操作者 2026-09-12 明确要求「默认关闭鉴权、
    # 局域网可操作、方便优先」); 设为 on 才要求令牌。
    #
    # 关闭后: 页面无需填令牌; `validate()` 不再因「非回环 + 空令牌」拒绝启动。
    # 代价(如实列出, 不复述成"没问题"):
    #   - 局域网内任何设备无需凭据即可**改配置**(含关闭启动对账等安全网)、
    #     **恢复急停**(即便系统是因为真实的资金差异被冻结的)、**停机**。
    #   - 缓解: 启动日志每次醒目告警 + 页面常驻横幅 + `/ops` 报 WARN —— 让"关着"这件事
    #     不可能被遗忘成默认状态。
    # 要恢复 fail-closed: 设 `WEB_ADMIN_AUTH=on` 且配非空 `WEB_ADMIN_TOKEN`。
    web_admin_auth: str = "off"

    @property
    def admin_auth_disabled(self) -> bool:
        """写接口鉴权是否被**显式**关闭(默认 False = 鉴权生效)。"""
        return self.web_admin_auth.strip().lower() in (
            "off", "false", "0", "no", "disabled", "none",
        )

    # 币安 API
    binance_testnet: bool = True

    # 测试网凭证
    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""
    binance_testnet_base_url: str = "https://testnet.binance.vision"
    binance_testnet_ws_url: str = "wss://stream.testnet.binance.vision/ws"
    # V11.8: 测试网真实执行 opt-in 开关(与 testnet_gate 配合); 也支持 .env 配置
    run_testnet_trading: str = ""

    # 主网凭证
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_base_url: str = "https://api.binance.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"
    # 主网 API key 权限已人工确认(仅 Spot 交易、关闭提现/资金转移)。Binance 无法通过 API 自证
    # 权限, 故须操作者核对后显式置 true; 默认 false → 主网就绪自检 BLOCKED。
    # 同时兼容早期文档/部署文件的 MAINNET_API_SCOPE_CONFIRM；规范名称带 confirmed 语义。
    # 若未设置则默认 false，主网自检仍 fail-closed。
    mainnet_api_scope_confirmed: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "MAINNET_API_SCOPE_CONFIRMED", "MAINNET_API_SCOPE_CONFIRM"
        ),
    )
    # V12 §10-11: 主网首次只读接管(账户快照 + 对账 + HODL 基线)。只读安全, 默认开。
    mainnet_takeover_enabled: bool = True

    # V12.6 P2: 启动守卫显式解锁(降摩擦通道)。
    # 格式 `GUARD_OVERRIDE=<ISO8601 到期时间>:<确认短语>`, 例:
    #   GUARD_OVERRIDE=2026-09-13T00:00:00Z:I-KNOW-THIS-IS-MAINNET
    # 默认空 = 行为与以往逐字一致。只解锁**启动前**守卫(主网拦截/就绪自检/测试网闸门);
    # **不影响运行时交易闸门 TradingGate** —— 买卖许可仍逐笔判定。解析见 guard_override.py。
    guard_override: str = ""

    # 行情引擎
    market_trade_window: int = 500  # 逐笔成交滚动窗口大小
    market_kline_interval: str = "1m"
    market_depth_level: int = 20

    # 分析引擎
    analytics_vwap_window: int = 300  # VWAP 滚动窗口(笔数)
    analytics_whale_quantile: float = 0.99  # 大单分位数阈值
    analytics_whale_min_quote: float = 50000.0  # 大单最小金额(USDT)
    analytics_accumulation_window: int = 60  # 吸筹检测窗口(秒)

    # 策略引擎
    strategy_enabled: str = "buy,sell,grid,trend"  # 启用的策略
    grid_upper_pct: float = 0.02  # 网格上边界相对当前价百分比
    grid_lower_pct: float = 0.02  # 网格下边界
    grid_count: int = 10  # 网格数量
    trend_fast_period: int = 12  # 快均线周期(K线数)
    trend_slow_period: int = 26  # 慢均线周期
    sell_trailing_drawdown: float = 0.05  # 移动止盈回撤比例(V2.0: 5%)
    # V9.0: 分批止盈阶梯(盈利%:卖出持仓%, 逗号分隔, 如 "5:20,10:30,20:50")
    sell_take_profit_ladder: str = "5:20,10:30,20:50"
    entry_buy_threshold: float = 80.0  # V2.0: 买入评分阈值
    entry_observe_threshold: float = 60.0  # V2.0: 观察阈值
    regime_enabled: bool = True  # V2.0: Market Regime 开关
    regime_watch_interval: float = 30.0  # V2.0: 环境评估间隔(秒)

    # 风控引擎(V2.0: 百分比化)
    risk_max_position_pct: float = 0.40  # 最大持仓占总权益 40%(策略信号子仓上限)
    risk_max_single_order_pct: float = 0.05  # 单笔最大 5%(V12 §17)
    risk_max_sol_exposure: float = 0.70  # V12 §16: SOL 市值/权益硬上限 70%(超限禁买、放行卖)
    risk_max_daily_loss: float = 0.03  # V12 §18: 日内最大亏损 3%(超限 REDUCE_ONLY, 非清仓)
    risk_max_drawdown: float = 0.15  # V12 §19: 最大回撤 15% 急停(KILL, 需人工检查)
    # V12 §19: 分级回撤档位(5% 观察 / 8% 降险 / 12% 仅减仓 / 15% 急停)
    risk_drawdown_observe_pct: float = 0.05
    risk_drawdown_reduce_pct: float = 0.08
    risk_drawdown_pause_pct: float = 0.12
    risk_cooldown_seconds: int = 300  # 熔断冷却时间(秒)
    risk_initial_equity: float = 100000.0  # 初始权益(回撤基准; V12 主网接管时由真实账户权益覆盖)
    # V1 兼容(绝对金额,若>0 则优先于百分比)
    risk_max_position_quote: float = 0.0
    risk_max_single_order_quote: float = 0.0
    # V2.0: 异常保护
    risk_price_spike_pct: float = 0.03  # 单笔价格瞬间波动 3% 视为异常
    risk_anomaly_pause_seconds: int = 60  # 异常后暂停交易秒数
    risk_max_ws_silence_seconds: int = 30  # 行情静默告警阈值

    # 执行引擎
    execution_order_type: str = "LIMIT"  # 默认限价单
    execution_price_slip_bps: float = 5.0  # 限价单滑点(基点)
    execution_fill_poll_seconds: float = 1.0  # 成交确认轮询间隔
    execution_max_retry: int = 3

    # V9.0 M3.2: regime 条件滑点(逗号分隔 "REGIME:bps", 命中放大, 缺失回退平铺)
    slippage_regime_bps: str = "PANIC:50,VOLATILE:20,BEAR:15"

    # V9.0 M3.3: HMM Regime(可选模块, 默认关闭; 不接入实盘 regime 判定)
    regime_hmm_enabled: bool = False

    # V9.0 M3.4: Funding + OI 情绪因子(可选, 默认关闭; 不碰现货主链路)
    sentiment_enabled: bool = False
    sentiment_poll_interval_seconds: int = 300  # 低频轮询周期
    sentiment_funding_threshold: float = 0.0005  # 拥挤多头 funding 阈值(0.05%)
    binance_futures_base_url: str = "https://fapi.binance.com"

    # 对账(V8)
    reconcile_interval_seconds: int = 300  # 本地 vs 交易所持仓对账间隔(秒)

    # V10: 生产安全三件套
    startup_reconcile_enabled: bool = True  # 启动对账(仅实盘; 未解决差异 -> 急停冻结)
    equity_reconcile_tolerance_pct: float = 0.02  # 权益对账容差(本地 vs 交易所, 2%)

    # V9.0: 组合三桶比例(核心/交易/现金, 按总权益口径; 三者和应为 1.0)
    portfolio_core_ratio: float = 0.40
    portfolio_trading_ratio: float = 0.30
    portfolio_cash_ratio: float = 0.30
    portfolio_rebalance_interval_seconds: int = 300  # 组合再平衡(核心仓决策)周期

    # V9.0: Core Position Manager
    core_manager_min_interval_seconds: int = 3600  # 核心仓决策最小间隔(低频)
    core_manager_btc_fail_pct: float = 2.0  # BTC 24h 跌幅超过该阈值视为锚失败(%)

    # V9.0: 每日自动复盘
    daily_report_enabled: bool = True
    daily_report_dir: str = "reports"
    # V13: AI Review Package 输出目录(每日一份 review/YYYY-MM-DD/)。
    # 与 daily_report_dir 分开是因为产物形态不同: 日报是给人读的 Markdown,
    # 复盘包是给 AI 读的 JSON 集合 —— 混在一起会让两边都难找。
    ai_review_dir: str = "review"

    # AI Advisor(供应商可切换: openai/qwen/deepseek)
    # 各供应商 key / base_url 默认值见 at30_strategy/llm_config.py(从 .env 读)
    ai_enabled: bool = False
    ai_provider: str = "deepseek"  # 供应商选择参数
    ai_base_url: str = ""  # 通用覆盖(空则用供应商默认 base_url)
    ai_api_key: str = ""   # 通用覆盖(空则用供应商 .env key)
    ai_model: str = "deepseek-chat"
    ai_interval_seconds: int = 86400  # V4: 每日一次(策略参数优化节奏)

    @property
    def symbol_list(self) -> list[str]:
        """解析标的列表"""
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]

    @property
    def enabled_strategies(self) -> list[str]:
        """解析启用策略列表"""
        return [s.strip().lower() for s in self.strategy_enabled.split(",") if s.strip()]

    @property
    def binance_rest_url(self) -> str:
        if self.binance_testnet:
            return self.binance_testnet_base_url
        return self.binance_base_url

    @property
    def binance_stream_url(self) -> str:
        if self.binance_testnet:
            return self.binance_testnet_ws_url
        return self.binance_ws_url

    @property
    def slippage_regime_bps_map(self) -> dict[str, float]:
        """解析 "REGIME:bps,..." 为 {REGIME: bps}; 非法项静默忽略"""
        out: dict[str, float] = {}
        for item in self.slippage_regime_bps.split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            key, _, val = item.partition(":")
            try:
                out[key.strip().upper()] = float(val.strip())
            except ValueError:
                continue
        return out

    def validate(self) -> list[str]:
        """生产配置审计: 返回问题清单(空 = 通过)。

        覆盖 V11.2 P1-4 + V11.3 P0-3 审计项(约 17 个字段 fail-fast):
        - 运行模式: 实盘(PAPER_TRADING=false)必须配置对应环境(testnet/主网) API key/secret;
        - 标的: 非空 + 冻结单币 SOLUSDT;
        - 组合三桶比例(核心/交易/现金)和 == 1.0;
        - 风控百分比阈值在 (0, 1]; 初始权益/手续费率/网格/止盈阶梯/重试/周期数值合法;
        - 均线周期 fast < slow, 观察阈值 < 买入阈值, API 端口合法, 策略开关非空, DB 地址非空。

        注: 主网「显式确认 LIVE_TRADING_CONFIRM=true」由 run.py 启动守卫单独强制,
        此处不重复(避免改变纸面模式下的既有语义)。
        """
        problems: list[str] = []

        # ---- 运行模式与凭证 ----
        if not self.paper_trading:
            if self.binance_testnet:
                if not self.binance_testnet_api_key or not self.binance_testnet_api_secret:
                    problems.append("PAPER_TRADING=false 但未配置测试网 BINANCE_TESTNET_API_KEY/SECRET")
            else:
                if not self.binance_api_key or not self.binance_api_secret:
                    problems.append("主网实盘(PAPER_TRADING=false, BINANCE_TESTNET=false)但未配置主网 BINANCE_API_KEY/SECRET")

        # ---- 标的(冻结单币) ----
        if not self.symbol_list:
            problems.append("SYMBOLS 为空")
        else:
            unsupported = [s for s in self.symbol_list if s not in SUPPORTED_SYMBOLS]
            if unsupported:
                problems.append(
                    f"冻结单币 SOLUSDT, 不支持标的 {', '.join(unsupported)}(仅支持 {', '.join(SUPPORTED_SYMBOLS)})"
                )

        # ---- 组合三桶比例 ----
        bucket_total = self.portfolio_core_ratio + self.portfolio_trading_ratio + self.portfolio_cash_ratio
        if abs(bucket_total - 1.0) > 1e-6:
            problems.append(f"组合三桶比例和 {bucket_total:.4f} != 1.0")

        # ---- 风控百分比阈值(0, 1] ----
        for field, label in (
            ("risk_max_position_pct", "最大持仓占比"),
            ("risk_max_single_order_pct", "单笔占比"),
            ("risk_max_sol_exposure", "SOL 敞口硬上限"),
            ("risk_max_daily_loss", "日内最大亏损"),
            ("risk_max_drawdown", "最大回撤"),
            ("risk_drawdown_observe_pct", "回撤观察档"),
            ("risk_drawdown_reduce_pct", "回撤降险档"),
            ("risk_drawdown_pause_pct", "回撤仅减仓档"),
        ):
            v = getattr(self, field)
            if not (0.0 < v <= 1.0):
                problems.append(f"{label}({field}) 需在 (0, 1] 区间, 当前 {v}")

        # ---- V12 §19: 分级回撤档位严格递增 ----
        if not (
            0.0
            < self.risk_drawdown_observe_pct
            < self.risk_drawdown_reduce_pct
            < self.risk_drawdown_pause_pct
            < self.risk_max_drawdown
        ):
            problems.append(
                "回撤档位需满足 0 < observe < reduce < pause < max_drawdown, 当前 "
                f"{self.risk_drawdown_observe_pct}/{self.risk_drawdown_reduce_pct}/"
                f"{self.risk_drawdown_pause_pct}/{self.risk_max_drawdown}"
            )

        # ---- 初始权益 / 手续费率 ----
        if self.risk_initial_equity <= 0:
            problems.append(f"初始权益 risk_initial_equity 必须 > 0, 当前 {self.risk_initial_equity}")
        if not (0.0 <= self.paper_fee_rate < 1.0):
            problems.append(f"纸面手续费率 paper_fee_rate 需在 [0, 1), 当前 {self.paper_fee_rate}")

        # ---- 网格 ----
        if self.grid_count < 2:
            problems.append(f"网格数 grid_count 需 >= 2, 当前 {self.grid_count}")
        if self.grid_upper_pct <= 0 or self.grid_lower_pct <= 0:
            problems.append(
                f"网格上下边界比例需 > 0, 当前 upper={self.grid_upper_pct} lower={self.grid_lower_pct}"
            )

        # ---- 分批止盈阶梯 ----
        if not _parse_take_profit_ladder(self.sell_take_profit_ladder):
            problems.append(
                f"分批止盈阶梯 sell_take_profit_ladder 无有效档位, 当前 {self.sell_take_profit_ladder!r}"
            )

        # ---- 执行 / 对账 / 组合周期 ----
        if self.execution_max_retry < 0:
            problems.append(f"重试次数 execution_max_retry 需 >= 0, 当前 {self.execution_max_retry}")
        if self.reconcile_interval_seconds <= 0:
            problems.append(f"对账间隔 reconcile_interval_seconds 需 > 0, 当前 {self.reconcile_interval_seconds}")
        if self.portfolio_rebalance_interval_seconds <= 0:
            problems.append(
                f"组合再平衡周期 portfolio_rebalance_interval_seconds 需 > 0, 当前 {self.portfolio_rebalance_interval_seconds}"
            )

        # ---- 均线周期 / 买入阈值 ----
        if not (0 < self.trend_fast_period < self.trend_slow_period):
            problems.append(
                f"均线周期需满足 0 < fast < slow, 当前 fast={self.trend_fast_period} slow={self.trend_slow_period}"
            )
        if not (0.0 <= self.entry_observe_threshold < self.entry_buy_threshold <= 100.0):
            problems.append(
                f"买入阈值需满足 0 <= observe < buy <= 100, 当前 observe={self.entry_observe_threshold} buy={self.entry_buy_threshold}"
            )

        # ---- 端口 / 策略开关 / DB 地址 ----
        if not (1 <= self.api_port <= 65535):
            problems.append(f"API 端口 api_port 需在 [1, 65535], 当前 {self.api_port}")
        if not self.enabled_strategies:
            problems.append("strategy_enabled 未启用任何策略")
        if not self.database_url:
            problems.append("database_url 为空")

        # ---- Web 安全(V11.5 P0-1): 非回环绑定必须配写接口令牌 ----
        # V12.4: 仅当操作者**显式**设 WEB_ADMIN_AUTH=off 时才放行这一项; 默认仍然 fail-fast。
        if self.api_host not in ("127.0.0.1", "localhost", "::1"):
            if not self.web_admin_token and not self.admin_auth_disabled:
                problems.append(
                    "API_HOST 非回环地址(将暴露到网络)但未配置 WEB_ADMIN_TOKEN; "
                    "请设 WEB_ADMIN_TOKEN 保护写接口, 或改回 API_HOST=127.0.0.1, "
                    "或(个人局域网)显式设 WEB_ADMIN_AUTH=off 关闭写接口鉴权"
                )

        return problems

    def mainnet_blocked_reason(self) -> str | None:
        """主网启动守卫: **真钱交易主网**必须显式确认。

        V12.6: 判定条件由「是否连主网」改为「**是否可能用真钱下单**」。

        原实现只要 `BINANCE_TESTNET=false` 就要求 `LIVE_TRADING_CONFIRM=true`, 即便
        `PAPER_TRADING=true`。那拦的是「主网纸面观察」—— 一个**没有任何真钱能力**的状态
        (纸面模式下 `ExecutionEngine.is_paper=True`, 下单走 PaperBroker; 三个用 REST 的
        对账器全是 `rest_client=None`; `validate()` 也不要求主网凭证)。

        而真正要防的「意外拿真钱交易主网」并不靠这一道: 从主网观察改成真钱交易需要
        `PAPER_TRADING=false`, 那会再次进入本函数且此时**仍会拦**(除非显式确认)。
        所以旧条件是一道挂错位置的重复守卫。

        保持不变: `PAPER_TRADING=false` + `BINANCE_TESTNET=false`(主网真实)
        → 仍必须 `LIVE_TRADING_CONFIRM=true`, 且过九项就绪自检。纯函数, 可独立测试。
        """
        if self.binance_testnet:
            return None
        if self.paper_trading:
            return None  # 主网观察: 只读主网行情 + 本地模拟成交, 无下单能力
        if self.live_trading_confirm.strip().lower() == "true":
            return None
        return "主网实盘需显式确认 LIVE_TRADING_CONFIRM=true 后启动"

    @property
    def observing_mainnet(self) -> bool:
        """主网观察模式: 主网真实行情 + 纸面成交(不下真单)。

        V12.6: 这是个**合法且有用**的状态(主网流动性/微观结构远比测试网真实),
        但仍值得让操作者知道自己在看主网 —— 启动时会打醒目提示。
        """
        return (not self.binance_testnet) and self.paper_trading


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()
