"""V11.6 P0-3 证明: 实盘(live)财务真相审计 —— AccountLedger 仅纸面落库, 实盘锚定交易所对账。

审计结论(财务真相链, 逐表核对):

    | 表/机制                          | 纸面(is_paper=True) | 实盘(is_paper=False) |
    |----------------------------------|---------------------|----------------------|
    | Order                            | 落库                | 落库(带 is_paper 标记) |
    | OrderFill                        | 落库                | 落库                 |
    | Position                         | 落库(镜像)          | 落库(镜像)           |
    | PositionLot(FIFO 买入批次)       | 落库                | 落库                 |
    | SellAllocation(FIFO 卖出分配)    | 落库                | 落库                 |
    | AccountLedger(审计账本)          | 落库(USDT+SOL 两行) | **不落库**           |
    | ExchangeTruth + ReconciliationMatrix | ——              | 落库/对账(财务真相锚) |

关键事实: `AccountLedgerWriter.record()` 的调用被 `cash_before is not None and
cash_after is not None` 闸门包裹(execution_executor.py::_apply_fill_accounting)。
实盘现金余额来自交易所、不在成交路径同步查询, 故 `cash_before/cash_after` 恒为
`None`(见 `execute()` 的 `self.paper.cash if self.is_paper else None` 推导), 于是实盘
**不写 AccountLedger**。

因此: 实盘财务真相 = 交易所对账链(ExchangeTruthReconciler 逐笔 myTrades 核对 +
ReconciliationMatrix 统一处置), 而非本地审计账本。记:

    LIVE_ACCOUNT_LEDGER_MODE = EXCHANGE_TRUTH_RECONCILIATION

本片三件事:
1. 实盘买入: Position + PositionLot 落库, AccountLedger 不落库;
2. 实盘卖出: SellAllocation 落库, AccountLedger 仍不落库;
3. 实盘财务真相锚 = 交易所对账: 跨源资金级差异(equity_drift/orphan_trade/
   fill_truth_mismatch/missing)在 ReconciliationMatrix 单源即 KILLED(不靠本地账本)。
"""

from sqlalchemy import func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import AccountLedger, Position, PositionLot, SellAllocation
from at50_execution.execution_executor import ExecutionEngine
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_manager import RiskManager


async def _count(model) -> int:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


def _buy() -> Signal:
    return Signal(symbol="SOLUSDT", strategy="trend", side=SignalSide.BUY, price=100.0, quantity=1.0)


def _sell() -> Signal:
    return Signal(symbol="SOLUSDT", strategy="trend", side=SignalSide.SELL, price=110.0, quantity=1.0)


def _live_engine() -> ExecutionEngine:
    engine = ExecutionEngine(risk_manager=RiskManager())
    engine.is_paper = False  # 实盘: 现金余额来自交易所, 本地不追踪
    return engine


async def test_live_buy_writes_position_and_lot_but_not_ledger(db_tables):
    """实盘买入: Position + PositionLot 落库, AccountLedger 不落库。"""
    engine = _live_engine()
    await engine._apply_fill_accounting(
        signal=_buy(), client_order_id="live-buy-1", exchange_order_id="ex-1",
        fill_qty=1.0, fill_price=100.0, fee=0.0,
        pos_before=0.0, cash_before=None,  # 实盘 cash_before 恒 None
    )

    assert await _count(Position) == 1
    assert await _count(PositionLot) == 1
    assert await _count(AccountLedger) == 0  # 实盘不落审计账本


async def test_live_sell_writes_allocation_but_not_ledger(db_tables):
    """实盘卖出平仓: SellAllocation 落库, AccountLedger 仍不落库。"""
    engine = _live_engine()
    await engine._apply_fill_accounting(
        signal=_buy(), client_order_id="live-buy-1", exchange_order_id="ex-1",
        fill_qty=1.0, fill_price=100.0, fee=0.0, pos_before=0.0, cash_before=None,
    )
    await engine._apply_fill_accounting(
        signal=_sell(), client_order_id="live-sell-1", exchange_order_id="ex-2",
        fill_qty=1.0, fill_price=110.0, fee=0.0, pos_before=1.0, cash_before=None,
    )

    assert await _count(PositionLot) == 1
    assert await _count(SellAllocation) == 1  # FIFO 卖出分配(实盘也落库)
    assert await _count(AccountLedger) == 0  # 全程不落审计账本


def test_exchange_truth_is_live_financial_anchor():
    """实盘财务真相锚 = 交易所对账: 跨源资金级差异单源即 KILLED(不靠本地账本)。"""
    from at50_execution.reconciliation_matrix import Severity, classify_severity

    for t in ("equity_drift", "orphan_trade", "fill_truth_mismatch", "fill_truth_missing", "exchange_only"):
        assert classify_severity(t) is Severity.KILLED, f"[{t}] 应单源即 KILLED(实盘真相锚)"
