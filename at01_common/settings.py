"""
主配置
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    symbols: str = "BTCUSDT"  # 逗号分隔多标的
    paper_trading: bool = True  # 纸面交易(模拟成交),false 时走实盘
    paper_initial_cash: float = 100000.0  # 纸面交易初始资金 USDT
    paper_fee_rate: float = 0.001  # 纸面交易手续费率

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

    # AI Advisor(默认 Anthropic 兼容协议,可切 openai)
    ai_enabled: bool = False
    ai_provider: str = "anthropic"  # anthropic / openai
    ai_base_url: str = "https://virex.virexstar.com"
    ai_api_key: str = ""
    ai_model: str = "glm-5.3"
    ai_interval_seconds: int = 300  # 分析间隔

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


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()
