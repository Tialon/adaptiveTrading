"""V11.3 P0-9 手续费会计最终审计(跨路径一致性 + 费用恰好一次 + base 资产折算)。

既有覆盖: test_v112(FeeCalculator 折算/降级)、test_v103(live lot 会计费用)、
test_v113(重建路径费用)。本片补「最终审计」级不变量, 钉死:

1. **跨路径一致性**: 同一成交序列, live `LotTracker`(add_buy/allocate_sell)与重建
   `build_plan`(`_replay_fifo`)产出的已实现盈亏/持仓/现金**逐位一致** —— 这是「重启后
   对账不因费用口径漂移」的关键不变量;
2. **费用恰好一次**: 买入费摊入 lot 成本、卖出费一次性从已实现盈亏扣除; 完整往返净盈亏
   恒等于 `价格差 - 买入费 - 卖出费`(不重复扣、不漏扣);
3. **现金守恒**: cash_after = cash_before - (买入 quote + 买入费) + (卖出 quote - 卖出费);
4. **base 资产(SOL)手续费折算**: `_compute_fill_metrics` 对 SOL 计价手续费正确折算 quote,
   并贯穿到已实现盈亏。

冻结不变: 纯审计 + 测试钉住, 不改记账公式。
"""

import pytest

from at50_execution.execution_executor import _compute_fill_metrics
from at50_execution.ledger_reconstruction import build_plan
from at60_risk.risk_lot import LotTracker

SYMBOL = "SOLUSDT"


def _t(tid, oid, side, price, qty, quote=None, commission="0", asset="USDT", time=None):
    """构造一枚 myTrades 成交(side: BUY/SELL -> isBuyer)。"""
    return {
        "id": tid,
        "orderId": oid,
        "isBuyer": side == "BUY",
        "price": str(price),
        "qty": str(qty),
        "quoteQty": str(quote if quote is not None else price * qty),
        "commission": commission,
        "commissionAsset": asset,
        "time": time if time is not None else tid * 1000,
    }


# ---------------------------------------------------------------------------
# 1. 跨路径一致性(live LotTracker vs 重建 build_plan)
# ---------------------------------------------------------------------------

async def test_cross_path_realized_pnl_identical(db_tables):
    """同一序列(含买卖费), live 与重建产出的已实现盈亏逐位一致。"""
    # BUY 1@100(费0.1) + BUY 1@120(费0.12) + SELL 1.5@130(费0.15)
    lt = LotTracker()
    await lt.add_buy(SYMBOL, 1.0, 100.0, fee_quote=0.1)
    await lt.add_buy(SYMBOL, 1.0, 120.0, fee_quote=0.12)
    live_realized, live_matched, _ = await lt.allocate_sell(SYMBOL, 1.5, 130.0, fee_quote=0.15)

    plan = build_plan(
        [
            _t(1, "B1", "BUY", 100.0, 1.0, commission="0.1"),
            _t(2, "B2", "BUY", 120.0, 1.0, commission="0.12"),
            _t(3, "S1", "SELL", 130.0, 1.5, commission="0.15"),
        ],
        SYMBOL, cash_before=10_000.0,
    )

    assert plan.safe_mode is False
    assert plan.position["realized_pnl"] == pytest.approx(live_realized)
    assert plan.position["quantity"] == pytest.approx(0.5)
    assert plan.position["avg_price"] == pytest.approx(120.12)
    # live 匹配成本(Σ lot 成本) == 重建剩余 cost basis 反推口径一致
    assert live_matched == pytest.approx(100.1 * 1.0 + 120.12 * 0.5)


async def test_cross_path_partial_sell_multi_lot(db_tables):
    """跨多 lot 部分卖出 + 卖出费, 两路径分配数量/成本逐位一致。"""
    lt = LotTracker()
    await lt.add_buy(SYMBOL, 2.0, 100.0, fee_quote=0.2)
    await lt.add_buy(SYMBOL, 3.0, 150.0, fee_quote=0.45)
    live_realized, _, allocs = await lt.allocate_sell(SYMBOL, 3.0, 160.0, fee_quote=0.3)

    plan = build_plan(
        [
            _t(1, "B1", "BUY", 100.0, 2.0, commission="0.2"),
            _t(2, "B2", "BUY", 150.0, 3.0, commission="0.45"),
            _t(3, "S1", "SELL", 160.0, 3.0, commission="0.3"),
        ],
        SYMBOL, cash_before=10_000.0,
    )

    assert plan.safe_mode is False
    assert plan.position["realized_pnl"] == pytest.approx(live_realized)
    # 分配明细逐位一致(数量 / lot 成本)
    assert len(allocs) == len(plan.sell_allocations) == 2
    for a, b in zip(allocs, plan.sell_allocations):
        assert a["quantity"] == pytest.approx(b["quantity"])
        assert a["lot_price"] == pytest.approx(b["lot_price"])


# ---------------------------------------------------------------------------
# 2. 费用恰好一次(往返净盈亏恒等式)
# ---------------------------------------------------------------------------

async def test_roundtrip_fee_exactly_once(db_tables):
    """完整往返: 净盈亏 == 价格差 - 买入费 - 卖出费(不重复/不漏扣)。"""
    lt = LotTracker()
    await lt.add_buy(SYMBOL, 1.0, 100.0, fee_quote=0.1)
    realized, _, _ = await lt.allocate_sell(SYMBOL, 1.0, 110.0, fee_quote=0.11)

    expected = (110.0 - 100.0) - 0.1 - 0.11  # 9.79
    assert realized == pytest.approx(expected)
    assert lt.open_quantity(SYMBOL) == pytest.approx(0.0)


def test_roundtrip_cash_conservation_with_fees():
    """现金守恒: cash_after = cash_before - (买入 quote + 买入费) + (卖出 quote - 卖出费)。"""
    plan = build_plan(
        [
            _t(1, "B1", "BUY", 100.0, 1.0, commission="0.1"),
            _t(2, "S1", "SELL", 110.0, 1.0, commission="0.11"),
        ],
        SYMBOL, cash_before=1000.0,
    )
    assert plan.safe_mode is False
    assert plan.cash_after == pytest.approx(1000.0 - 100.1 + 109.89)
    assert plan.position["realized_pnl"] == pytest.approx(9.79)
    assert plan.conservation["cash_conservation"] is True


async def test_sell_fee_expensed_once_not_per_lot(db_tables):
    """跨多 lot 卖出, 卖出费仅扣一次(不按 lot 数重复扣)。"""
    lt = LotTracker()
    await lt.add_buy(SYMBOL, 1.0, 100.0)
    await lt.add_buy(SYMBOL, 1.0, 200.0)
    realized, _, allocs = await lt.allocate_sell(SYMBOL, 2.0, 150.0, fee_quote=3.0)
    gross = (150.0 - 100.0) * 1.0 + (150.0 - 200.0) * 1.0  # 0.0
    assert len(allocs) == 2
    assert realized == pytest.approx(gross - 3.0)  # 仅扣一次 3.0


# ---------------------------------------------------------------------------
# 3. base 资产(SOL)手续费折算贯穿
# ---------------------------------------------------------------------------

def test_compute_fill_metrics_base_asset_fee_converts():
    """SOL 计价手续费 -> _compute_fill_metrics 折算 quote(commission × price)。"""
    fills = [
        {"qty": "1.0", "quoteQty": "100.0", "price": "100.0",
         "commission": "0.01", "commissionAsset": "SOL"},
    ]
    avg, fee, unpriced = _compute_fill_metrics(SYMBOL, fills)
    assert avg == pytest.approx(100.0)
    assert fee == pytest.approx(0.01 * 100.0)
    assert unpriced is False


def test_build_plan_base_asset_fee_amortizes_and_deducts():
    """SOL 手续费在重建路径: 折算 quote 后摊入买入成本、卖出时扣除。"""
    plan = build_plan(
        [
            # 买入费 0.01 SOL @100 -> fee_quote = 1.0; 卖出费 0.01 SOL @120 -> 1.2
            _t(1, "B1", "BUY", 100.0, 1.0, commission="0.01", asset="SOL"),
            _t(2, "S1", "SELL", 120.0, 1.0, commission="0.01", asset="SOL"),
        ],
        SYMBOL, cash_before=10_000.0,
    )
    assert plan.safe_mode is False
    # lot 单位成本 = (100 + 1.0)/1 = 101; 已实现 = (120-101) - 1.2 = 17.8
    assert plan.position["realized_pnl"] == pytest.approx(17.8)
    # 现金: -101.0(买入 quote+费) + 118.8(卖出 quote-费)
    assert plan.cash_after == pytest.approx(10_000.0 - 101.0 + 118.8)


# ---------------------------------------------------------------------------
# 4. 零费边界
# ---------------------------------------------------------------------------

async def test_zero_fee_roundtrip_unchanged(db_tables):
    """零手续费: 净盈亏 == 纯价格差(费用路径不影响)。"""
    lt = LotTracker()
    await lt.add_buy(SYMBOL, 1.0, 100.0, fee_quote=0.0)
    realized, _, _ = await lt.allocate_sell(SYMBOL, 1.0, 110.0, fee_quote=0.0)
    assert realized == pytest.approx(10.0)
