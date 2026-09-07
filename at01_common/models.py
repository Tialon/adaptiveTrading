"""
数据模型(SQLAlchemy ORM)
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from at01_common.database import Base

# 自增主键:MySQL 下 BIGINT,SQLite 下 INTEGER(BigInteger 主键在 SQLite 不自增)
ID = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(tz=timezone.utc)


class Kline(Base):
    """K线"""

    __tablename__ = "klines"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    interval: Mapped[str] = mapped_column(String(10), nullable=False)
    open_time: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="开盘时间ms")
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    quote_volume: Mapped[float] = mapped_column(Float, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_kline_symbol_interval_time", "symbol", "interval", "open_time", unique=True),
    )


class TradeRecord(Base):
    """逐笔成交(聚合)"""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    trade_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="交易所成交ID")
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    quote_quantity: Mapped[float] = mapped_column(Float, nullable=False, comment="成交额")
    is_buyer_maker: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="买方为挂单方(卖单主导)")
    trade_time: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="成交时间ms")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_trade_symbol_id", "symbol", "trade_id", unique=True),
    )


class Signal(Base):
    """策略信号"""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False, comment="产生信号的策略")
    side: Mapped[str] = mapped_column(String(8), nullable=False, comment="BUY/SELL")
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=True)
    quote_amount: Mapped[float] = mapped_column(Float, nullable=True, comment="目标金额")
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="信号强度 0~100")
    indicators: Mapped[str] = mapped_column(String(2048), nullable=True, comment="信号时指标快照JSON")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", comment="pending/approved/rejected/executed")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_signal_symbol_time", "symbol", "created_at"),
    )


class Order(Base):
    """订单"""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    exchange_order_id: Mapped[str] = mapped_column(String(64), nullable=True, comment="交易所订单ID")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="LIMIT")
    price: Mapped[float] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    filled_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_fill_price: Mapped[float] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW", comment="NEW/SUBMITTING/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/EXPIRED")
    strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    signal_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="纸面交易")
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="仅减仓(现货卖出闸门标记)")
    # V10.6: 成交后本地记账状态(OK / RECOVERY_REQUIRED)。强一致事务失败时置 RECOVERY_REQUIRED,
    # 供对账收敛优先定位「交易所已成交但本地账本未落」的订单。
    accounting_state: Mapped[str] = mapped_column(String(20), nullable=False, default="OK", comment="成交后本地记账状态: OK / RECOVERY_REQUIRED")
    error_msg: Mapped[str] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_order_symbol_time", "symbol", "created_at"),
    )


class OrderIntent(Base):
    """V10.1: 订单意图幂等表(DB 唯一键防重复下单, 重启不失效)

    替代旧的内存 _recent_executed(重启即失效、键过粗): 键含 quantity+price,
    唯一约束落在数据库, 同一决策周期重复信号即使进程重启也能被拦截。
    """

    __tablename__ = "order_intents"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    signal_id: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="关联 signals.id")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", comment="pending/executed/rejected")
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrderFill(Base):
    """V10.1: 订单成交明细(Order 1 → Fill 1..N)

    一笔订单在交易所可能分多笔成交(不同价格/不同手续费), 本表逐笔落库,
    用于真实均价 / 手续费 / 滑点 / 执行质量审计。内部幂等键 fill_idempotency_key
    (非空唯一) 保证同一笔交易所成交不重复落库(幂等摄入)。
    """

    __tablename__ = "order_fills"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="关联 orders.id")
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    exchange_order_id: Mapped[str] = mapped_column(String(64), nullable=True)
    exchange_trade_id: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="交易所成交ID(tradeId)")
    # V10.6: 内部幂等键(交易所订单ID:成交ID, 成交ID缺失用 'na')。原 (exchange_order_id,
    # exchange_trade_id) 唯一键两列皆可空, NULL 在多数 DB 下不参与唯一判定, 导致「成交ID缺失」
    # 的成交可被重复摄入; 本列非空唯一, 消除该漏洞。
    fill_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, comment="内部幂等键(非空唯一)")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quote_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="成交额")
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    commission_asset: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    trade_time: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment="成交时间ms")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_order_fill_exchange", "exchange_order_id", "exchange_trade_id", unique=True),
        Index("ix_order_fill_order", "order_id"),
    )


class ExecutionAttempt(Base):
    """V10.2: 订单执行尝试审计表(每次下单/重试落一行)

    记录下单请求参数与结果(outcome: success/rejected/ambiguous), 使「重试几次、
    每次结果如何」可审计, 支撑崩溃窗口排查与执行质量统计。请求体不含 API 密钥
    (密钥只在 header), 可安全落库。
    """

    __tablename__ = "execution_attempts"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="关联 orders.id")
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="第几次下单尝试")
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="create")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    request: Mapped[str] = mapped_column(String(2000), nullable=False, default="", comment="下单参数(JSON)")
    response: Mapped[str] = mapped_column(String(2000), nullable=False, default="", comment="响应或错误(JSON)")
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="", comment="success/rejected/ambiguous")
    exchange_order_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_execution_attempt_order", "client_order_id", "attempt_no"),
    )


class PositionLot(Base):
    """V10.3: 开仓批次(逐笔买入 = 一个 lot), FIFO 成本核算的最小单元

    附加审计层: 不动平均成本的 PositionState, 仅用 lot 队列精确计算
    已实现盈亏与剩余成本基础, 供审计/报告与「lot 总和 == 持仓量」对账。
    单位成本 price 已摊入该笔买入费(与 apply_buy 口径一致)。
    """

    __tablename__ = "position_lots"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="剩余未卖数量")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="单位成本(含摊入买入费)")
    fee_quote: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="本 lot 买入费(quote 口径)")
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=True)
    exchange_order_id: Mapped[str] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="open", comment="open/closed")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_position_lot_symbol_status_id", "symbol", "status", "id"),
    )


class SellAllocation(Base):
    """V10.3: 卖出逐笔分配到 lot(FIFO 消费记录)

    一笔卖出可能跨越多个 lot, 每行记录「从哪个 lot 卖出多少、匹配成本、
    该段已实现盈亏」, 使 FIFO 已实现盈亏可逐笔审计(不含卖出手续费,
    手续费在账本 realized_pnl 中一次性扣减)。
    """

    __tablename__ = "sell_allocations"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    sell_client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sell_exchange_order_id: Mapped[str] = mapped_column(String(64), nullable=True)
    lot_id: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="关联 position_lots.id")
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="从该 lot 卖出的数量")
    lot_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="匹配成本(该 lot 单位成本)")
    sell_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="该段已实现盈亏(不含卖出手续费)")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_sell_alloc_lot", "lot_id"),
        Index("ix_sell_alloc_sell", "sell_client_order_id"),
    )


class Position(Base):
    """持仓"""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    peak_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="持仓期间最高价(移动止盈)")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RiskEvent(Base):
    """风控事件"""

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="reject/breaker/drawdown/limit")
    symbol: Mapped[str] = mapped_column(String(20), nullable=True)
    detail: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    equity: Mapped[float] = mapped_column(Float, nullable=True, comment="事件时权益")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AIAdvice(Base):
    """AI 顾问建议"""

    __tablename__ = "ai_advices"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    advice: Mapped[str] = mapped_column(String(16), nullable=False, comment="BUY/SELL/HOLD/WATCH")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    summary: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    raw_response: Mapped[str] = mapped_column(String(4096), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PositionSnapshot(Base):
    """持仓快照(定时采样,收益曲线用)"""

    __tablename__ = "position_snapshot"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    market_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_profit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_profit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    equity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="总权益")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_pos_snap_symbol_time", "symbol", "timestamp"),
    )


class StrategyPerformance(Base):
    """策略绩效(供 AI 优化)"""

    __tablename__ = "strategy_performance"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False, comment="策略名")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    profit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="累计盈亏")
    max_drawdown: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    period_start: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_strategy_perf", "strategy", "symbol", unique=True),
    )


class SignalResult(Base):
    """V3.0: 信号结果跟踪(信号发出后的未来收益,供 AI 学习)"""

    __tablename__ = "signal_result"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="关联 signals.id")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False, comment="信号时价格")
    future_profit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="最新相对盈亏")
    max_profit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600, comment="跟踪窗口(秒)")
    final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="窗口结束")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_signal_result_symbol", "symbol", "strategy"),
    )


class PositionBucket(Base):
    """V4.0: 核心仓/交易仓双仓

    core  = 长期持有(牛市 70%, 卖交易仓不影响核心仓)
    trade = 高抛低吸(网格/评分策略操作的部分)
    V9.0: 追加 target_ratio / target_quantity / current_value(组合目标跟踪)
    """

    __tablename__ = "position_bucket"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    bucket_type: Mapped[str] = mapped_column(String(10), nullable=False, comment="core/trade")
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # V9.0: 组合目标(由 PortfolioManager 写入, 观测用)
    target_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="目标占权益比例")
    target_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="目标数量")
    current_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="当前市值")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_bucket_symbol_type", "symbol", "bucket_type", unique=True),
    )


class ClosedTrade(Base):
    """V9.0: 成交结果日志(一次完整闭环: 开仓->平仓)

    供 AI 复盘: 哪些交易赚了、为何、持仓多久、最大浮盈/回撤、失败原因。
    SELL 成交时记录一次(entry 取持仓期初状态, 无逐笔 FIFO 复杂度)。
    """

    __tablename__ = "trade_records"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    bucket: Mapped[str] = mapped_column(String(10), nullable=False, default="trade", comment="core/trade")
    entry_ts: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="建仓时间(epoch秒)")
    exit_ts: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="平仓时间(epoch秒)")
    entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    holding_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_profit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="持仓期间最大浮盈(quote)")
    max_drawdown: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="持仓期间最大回撤(quote)")
    mistake_reason: Mapped[str] = mapped_column(String(512), nullable=True, comment="失败原因(复盘标注)")
    regime: Mapped[str] = mapped_column(String(16), nullable=False, default="", comment="平仓时市场环境")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_trade_record_symbol_time", "symbol", "created_at"),
    )


class StrategyVersion(Base):
    """V9.0: 策略参数版本快照(不可变, 供回测/实盘对比与 AI 实验)"""

    __tablename__ = "strategy_versions"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    params: Mapped[str] = mapped_column(String(4096), nullable=False, default="{}", comment="参数快照JSON")
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    backtest_result: Mapped[str] = mapped_column(String(2048), nullable=True, comment="回测结果JSON")
    live_result: Mapped[str] = mapped_column(String(2048), nullable=True, comment="实盘结果JSON")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_strategy_version_time", "created_at"),
    )


class DecisionLog(Base):
    """V4.0: 交易日志(AI 复盘核心数据)

    每次决策记录完整上下文: 时间/价格/市场状态/Alpha/仓位/现金/原因
    """

    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False, comment="BUY/SELL/HOLD")
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    regime: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    regime_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    alpha_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    decision_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    core_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="决策时核心仓")
    trade_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="决策时交易仓")
    cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="决策时现金")
    equity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reason: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    context: Mapped[str] = mapped_column(String(2048), nullable=True, comment="指标快照JSON")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_decision_log_time", "symbol", "created_at"),
    )


class AIParameterHistory(Base):
    """V5: AI 参数调整历史(验证 AI 是否有帮助)"""

    __tablename__ = "ai_parameter_history"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    param_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="参数名")
    old_value: Mapped[str] = mapped_column(String(256), nullable=True)
    new_value: Mapped[str] = mapped_column(String(256), nullable=False)
    reason: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    # 效果追踪(后续填)
    pnl_after_24h: Mapped[float] = mapped_column(Float, nullable=True)
    effective: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_ai_param_history", "symbol", "param_name", "created_at"),
    )


class TradeStateRow(Base):
    """V8: 交易周期状态机落库(重启后恢复, 防状态丢失重复建仓)"""

    __tablename__ = "trade_state"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="IDLE")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PaperState(Base):
    """V8: 纸面交易现金持久化(重启后纸面资金不重置)"""

    __tablename__ = "paper_state"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AccountLedger(Base):
    """V9.0 M3: 账户审计账本(逐笔余额变更, 每笔成交落 USDT + SOL 两行)

    与 PortfolioLedger(内存 + 对账)互补: 后者推演不变量, 本表持久化
    现金与持仓的 before/change/after 三列, 供盈亏溯源与多账户审计。
    """

    __tablename__ = "account_ledger"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="成交时间ms")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    bucket: Mapped[str] = mapped_column(String(10), nullable=False, default="trade", comment="core/trade")
    side: Mapped[str] = mapped_column(String(8), nullable=False, comment="BUY/SELL")
    asset: Mapped[str] = mapped_column(String(8), nullable=False, comment="USDT|SOL")
    before_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    change_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    after_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="本笔手续费(quote 口径)")
    commission_asset: Mapped[str] = mapped_column(String(8), nullable=False, default="", comment="手续费计价资产")
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="本笔已实现盈亏(FIFO, 仅 SELL 有值)")
    matched_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="FIFO 匹配成本(Σ lot 成本, 仅 SELL)")
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    related_order_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_account_ledger_symbol_time", "symbol", "ts"),
    )


class KillSwitchState(Base):
    """V10: 急停开关状态(单行 id=1, 持久化, 不自动复位)

    区别于 CircuitBreaker(带 cooldown 会自动复位): 急停冻结需人工
    POST /api/emergency/recover 才解除, 用于启动对账未通过/权益漂移/人工急停。
    """

    __tablename__ = "kill_switch_state"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    armed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ExecutionEvent(Base):
    """V10.7: 订单执行事件日志(append-only 审计)

    记录订单生命周期每个关键事件(创建/提交/成交/撤单/恢复等), 使「为什么
    这个订单最终变成这样」可完整追溯, 支撑崩溃排查、执行质量统计与 AI 复盘。
    只增不改(无 update 路径), event_id 非空唯一保证事件不重。
    """

    __tablename__ = "execution_events"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, comment="事件ID(UUID)")
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="关联 orders.id")
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    exchange_order_id: Mapped[str] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="ORDER_CREATED/SUBMITTING/ACK/PARTIAL_FILL/FILL/CANCELED/UNKNOWN/RECOVERY/RECOVERED")
    event_time: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="事件时间ms")
    payload: Mapped[str] = mapped_column(String(2000), nullable=False, default="{}", comment="事件载荷JSON")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="execution", comment="execution/recovery/reconcile")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="同订单内事件序号")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_exec_event_order", "client_order_id", "sequence"),
    )
