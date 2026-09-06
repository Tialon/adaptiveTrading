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
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW", comment="NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/EXPIRED")
    strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    signal_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="纸面交易")
    error_msg: Mapped[str] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_order_symbol_time", "symbol", "created_at"),
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
    """

    __tablename__ = "position_bucket"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    bucket_type: Mapped[str] = mapped_column(String(10), nullable=False, comment="core/trade")
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_bucket_symbol_type", "symbol", "bucket_type", unique=True),
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
