"""
主配置
"""

from functools import lru_cache

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
    )

    # 应用配置
    app_name: str = "adaptiveTrading"
    app_version: str = "0.1.0"
    debug: bool = False

    # 交易标的与运行模式
    symbols: str = "SOLUSDT"  # 冻结单币 SOLUSDT(非 SOLUSDT 由 validate() fail-fast)
    paper_trading: bool = True  # 纸面交易(模拟成交),false 时走实盘
    paper_initial_cash: float = 100000.0  # 纸面交易初始资金 USDT
    paper_fee_rate: float = 0.001  # 纸面交易手续费率
    live_trading_confirm: str = ""  # 主网实盘安全守卫: 显式设 "true" 才允许主网启动

    # 数据库配置(开发默认 SQLite,生产切 MySQL)
    database_url: str = "sqlite+aiosqlite:///./adaptive.db"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    # Redis 配置(可选,不可用时降级为内存缓存)
    redis_url: str = "redis://localhost:6379/0"
    redis_enabled: bool = False

    # 日志配置
    log_level: str = "INFO"
    log_file: str = "logs/adaptive.log"

    # Web 配置
    api_host: str = "0.0.0.0"
    api_port: int = 8800

    # 币安 API
    binance_testnet: bool = True

    # 测试网凭证
    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""
    binance_testnet_base_url: str = "https://testnet.binance.vision"
    binance_testnet_ws_url: str = "wss://stream.testnet.binance.vision/ws"

    # 主网凭证
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_base_url: str = "https://api.binance.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"

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
    buy_dip_pct: float = 0.005  # 相对 VWAP 折价买入阈值
    sell_profit_pct: float = 0.01  # 止盈比例
    sell_trailing_drawdown: float = 0.05  # 移动止盈回撤比例(V2.0: 5%)
    # V9.0: 分批止盈阶梯(盈利%:卖出持仓%, 逗号分隔, 如 "5:20,10:30,20:50")
    sell_take_profit_ladder: str = "5:20,10:30,20:50"
    entry_buy_threshold: float = 80.0  # V2.0: 买入评分阈值
    entry_observe_threshold: float = 60.0  # V2.0: 观察阈值
    regime_enabled: bool = True  # V2.0: Market Regime 开关
    regime_watch_interval: float = 30.0  # V2.0: 环境评估间隔(秒)

    # 风控引擎(V2.0: 百分比化)
    risk_max_position_pct: float = 0.40  # 最大持仓占总权益 40%
    risk_max_single_order_pct: float = 0.05  # 单笔最大 5%
    risk_max_daily_loss: float = 0.05  # 日内最大亏损 5%
    risk_max_drawdown: float = 0.15  # 最大回撤 15% 熔断
    risk_cooldown_seconds: int = 300  # 熔断冷却时间(秒)
    risk_initial_equity: float = 100000.0  # 初始权益(用于回撤计算)
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
    regime_hmm_model_path: str = "models/regime_hmm.json"

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
    portfolio_profit_sweep_enabled: bool = False  # C+B: 交易已实现盈利按比例扫入核心仓(默认关)

    # V9.0: Core Position Manager
    core_manager_min_interval_seconds: int = 3600  # 核心仓决策最小间隔(低频)
    core_manager_btc_fail_pct: float = 2.0  # BTC 24h 跌幅超过该阈值视为锚失败(%)

    # V9.0: 每日自动复盘
    daily_report_enabled: bool = True
    daily_report_dir: str = "reports"

    # AI Advisor(供应商可切换: openai/qwen/deepseek)
    # 各供应商 key / base_url 默认值见 at50_strategy/llm_config.py(从 .env 读)
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
            ("risk_max_daily_loss", "日内最大亏损"),
            ("risk_max_drawdown", "最大回撤"),
        ):
            v = getattr(self, field)
            if not (0.0 < v <= 1.0):
                problems.append(f"{label}({field}) 需在 (0, 1] 区间, 当前 {v}")

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

        return problems


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()
