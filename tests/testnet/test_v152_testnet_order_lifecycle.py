r"""V11.5 P0-3 真实币安测试网订单生命周期验证(真实下单, opt-in)。

目标: 从「只读冒烟」迈向「真实下单闭环」。在真实测试网(testnet.binance.vision)上
验证一条完整链路: connect → exchangeInfo → account → 下单 → 查询 → 撤单 →
myTrades → 本地记账(Position / PositionLot / SellAllocation / AccountLedger / Order /
OrderFill) → 交叉对账 → 交易所真相。

两条路径:
1. 无成交 REST 生命周期(确定性, 不依赖余额): 挂一张远低于市价的限价买单(必不成交),
   走 create_order → get_order(NEW) → cancel_order → get_order(CANCELED) →
   get_my_trades(空) → 交易所 SOL 余额不变(交易所真相)。
2. 真实成交记账闭环(需测试网余额): 经 ExecutionEngine 实盘路径(MARKET 买 → 卖),
   校验 Position / PositionLot / SellAllocation / Order / OrderFill 全链路落库,
   并对交易所 SOL 余额做交叉对账(买入净增 ≈ fill_qty, 卖出回到基线)。

安全 / 隔离(绝不触碰主网):
- 仅测试网: 需显式 `RUN_TESTNET_TRADING=1` 才运行(默认跳过); 且断言配置为
  `BINANCE_TESTNET=true`、客户端 `testnet=True`、base_url 含 "testnet",
  三重守卫「绝不主网」。
- CI 永不执行: `pytestmark = pytest.mark.testnet`, CI 运行 `-m "not testnet"`
  (见 .github/workflows/ci.yml) 自动排除; 本文件默认无环境变量也 skip。
- 不打印 / 记录 API key/secret; 账户余额仅用于「跳过」与「对账」判定, 不外泄。
- 测试网不可达、无测试网 key、余额不足 → 跳过(不 fail), 属环境前置, 非代码缺陷。

运行(本地, 需能访问 testnet.binance.vision 且 .env 配测试网 key):
    $env:RUN_TESTNET_TRADING="1"; .venv\Scripts\python -m pytest tests/testnet/test_v152_testnet_order_lifecycle.py -v -s

已知限制(诚实披露, 与 production-readiness.md §8 一致): 实盘路径 cash_before=None
(实盘不逐笔跟踪现金), 故 AccountLedger 在实盘不落库(由权益对账兜底)。本测试断言该
行为, 而不是假装实盘也写账本。
"""

import os
import time
from decimal import Decimal

import pytest

from at01_common.settings import get_settings
from at10_market.market_rest_client import BinanceRestClient
from at60_execution.exchange_filters import SymbolFilters

SYMBOL = "SOLUSDT"

pytestmark = [pytest.mark.testnet]


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _balances(account: dict) -> dict[str, float]:
    """账户 balances -> {asset: free + locked}(float)。"""
    out: dict[str, float] = {}
    for b in account.get("balances", []):
        asset = b.get("asset", "")
        free = float(b.get("free", 0) or 0)
        locked = float(b.get("locked", 0) or 0)
        out[asset] = free + locked
    return out


async def _ensure_reachable(client) -> None:
    """测试网不可达则跳过(不 fail)。"""
    try:
        ok = await client.ping()
    except Exception:
        ok = False
    if not ok:
        pytest.skip("测试网不可达(无网络/被墙), 跳过真实下单验证")


# ---------------------------------------------------------------------------
# 客户端 fixture(仅测试网 + opt-in + 三重主网守卫)
# ---------------------------------------------------------------------------


@pytest.fixture
async def client():
    """真实测试网 REST 客户端; 未 opt-in / 非测试网 / 无 key 则跳过整个模块。"""
    val = os.environ.get("RUN_TESTNET_TRADING", "").strip().lower()
    if val not in ("1", "true"):
        pytest.skip("需显式 RUN_TESTNET_TRADING=1 才运行真实测试网下单(CI 默认跳过)")

    s = get_settings()
    # 守卫 1: 配置必须是测试网(绝不主网)。
    if s.binance_testnet is not True:
        pytest.skip("BINANCE_TESTNET != true, 拒绝运行真实下单(绝不主网)")

    # 守卫 2: 无测试网 key 则跳过(无法签名下单)。
    if not s.binance_testnet_api_key or not s.binance_testnet_api_secret:
        pytest.skip("未配置测试网 BINANCE_TESTNET_API_KEY/SECRET, 跳过真实下单")

    c = BinanceRestClient(testnet=True)
    # 守卫 3: 客户端必须锁定测试网(base_url 含 testnet)。
    assert c.testnet is True
    assert "testnet" in (c.base_url or "").lower(), "非测试网 base_url, 拒绝下单"

    await c.connect()
    try:
        yield c
    finally:
        await c.disconnect()


# ---------------------------------------------------------------------------
# 1. 无成交 REST 生命周期(确定性, 不依赖余额)
# ---------------------------------------------------------------------------


class TestRestOrderLifecycleNoFill:
    async def test_limit_buy_submit_query_cancel_trades_empty(self, client):
        """connect → exchangeInfo → account → 挂远低于市价限价买单 → 查询 NEW →
        撤单 CANCELED → myTrades 空 → 交易所 SOL 余额不变(交易所真相)。
        """
        await _ensure_reachable(client)

        # exchangeInfo → SymbolFilters(下单前对齐规则)
        data = await client.get_exchange_info(SYMBOL)
        filters = SymbolFilters.from_exchange_info(SYMBOL, data)
        assert filters.step_size > 0 and filters.tick_size > 0
        assert filters.min_notional > 0

        market = await client.get_price(SYMBOL)
        assert market > 0

        # 远低于市价(50%), 必不成交; 数量保证 notional 达标(min_notional 的 ~1.5 倍)
        raw_price = market * Decimal("0.5")
        raw_qty = Decimal(str(float(filters.min_notional) * 3.0)) / market
        qty, price, violations = filters.adjust(raw_qty, raw_price)
        assert not violations, violations

        # 账户(交易所真相基线)
        acct = await client.get_account()
        assert isinstance(acct, dict) and "balances" in acct
        sol_before = _balances(acct).get("SOL", 0.0)

        # 下单(限价 BUY)
        cid = f"at-testnet-p03-{int(time.time() * 1000)}"
        resp = await client.create_order(
            symbol=SYMBOL,
            side="BUY",
            order_type="LIMIT",
            quantity=qty,
            price=price,
            new_client_order_id=cid,
        )
        eid = str(resp["orderId"])

        # 查询 -> NEW(挂单中, 未成交)
        o = await client.get_order(SYMBOL, order_id=eid)
        assert o["status"] == "NEW"
        assert o["clientOrderId"] == cid
        assert float(o.get("executedQty", 0)) == 0.0

        # 撤单
        await client.cancel_order(SYMBOL, eid)
        o2 = await client.get_order(SYMBOL, order_id=eid)
        assert o2["status"] == "CANCELED"

        # myTrades 该订单无成交
        trades = await client.get_my_trades(SYMBOL, order_id=eid)
        assert trades == []

        # 交易所真相: 无成交, SOL 余额不变
        acct2 = await client.get_account()
        sol_after = _balances(acct2).get("SOL", 0.0)
        assert abs(sol_after - sol_before) < 1e-6


# ---------------------------------------------------------------------------
# 2. 真实成交记账闭环(需测试网余额)
# ---------------------------------------------------------------------------


class TestRealFillAccountingChain:
    async def test_market_buy_sell_full_chain(self, client, db_tables):
        """市价买 → 卖, 校验 Position / PositionLot / SellAllocation / Order /
        OrderFill 全链路落库, 并对交易所 SOL 余额交叉对账(交易所真相)。
        """
        await _ensure_reachable(client)

        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import (
            AccountLedger,
            Order,
            OrderFill,
            PositionLot,
            SellAllocation,
        )
        from at01_common.settings import Settings
        from at60_execution.execution_executor import ExecutionEngine
        from at30_strategy.strategy_base import Signal, SignalSide
        from at50_risk.risk_manager import RiskManager

        data = await client.get_exchange_info(SYMBOL)
        filters = SymbolFilters.from_exchange_info(SYMBOL, data)
        market = await client.get_price(SYMBOL)
        assert market > 0

        # 最小可交易数量(市价单 notional 用市价, 1.5 倍余量)
        raw_qty = Decimal(str(float(filters.min_notional) * 1.5)) / market
        qty, _, violations = filters.adjust(raw_qty, market)
        assert not violations, violations

        # 余额不足则跳过(不 fail)
        acct0 = await client.get_account()
        bal0 = _balances(acct0)
        usdt = bal0.get("USDT", 0.0)
        if usdt < float(filters.min_notional) * 3.0:
            pytest.skip(f"测试网 USDT 余额不足({usdt:.2f}), 跳过真实成交闭环")
        sol_before = bal0.get("SOL", 0.0)

        # 装配实盘 ExecutionEngine(is_paper=False + MARKET), 绕过共享 settings 单例
        rm = RiskManager()
        await rm.positions.load_from_db()
        engine = ExecutionEngine(risk_manager=rm, rest_client=client)
        engine.is_paper = False
        engine.settings = Settings(
            _env_file=None, paper_trading=False, execution_order_type="MARKET"
        )

        buy_qty = float(qty)
        buy = Signal(
            symbol=SYMBOL,
            strategy="testnet_p03",
            side=SignalSide.BUY,
            price=float(market),
            quantity=buy_qty,
            quote_amount=buy_qty * float(market),
            reason=["testnet lifecycle"],
        )
        res_buy = await engine.execute(buy)
        assert res_buy is not None, "市价买单未成交(60s 超时/被拒)"
        assert res_buy["status"] == "FILLED", res_buy
        assert res_buy["fill_qty"] > 0

        fill_qty = res_buy["fill_qty"]
        pos_after_buy = rm.positions.get(SYMBOL)
        assert pos_after_buy.quantity > 0
        assert pos_after_buy.avg_price > 0

        # --- BUY 本地记账校验(DB) ---
        buy_cid = res_buy["client_order_id"]
        async with AsyncSessionLocal() as session:
            order = (
                await session.execute(select(Order).where(Order.client_order_id == buy_cid))
            ).scalar_one_or_none()
            lots = (
                (
                    await session.execute(
                        select(PositionLot).where(PositionLot.client_order_id == buy_cid)
                    )
                )
                .scalars()
                .all()
            )
            fills = (
                (
                    await session.execute(
                        select(OrderFill).where(OrderFill.client_order_id == buy_cid)
                    )
                )
                .scalars()
                .all()
            )

        assert order is not None
        assert order.status == "FILLED"
        assert order.is_paper is False
        assert order.order_type == "MARKET"
        assert order.filled_quantity > 0
        assert len(lots) == 1, "一笔买入应恰好一个开仓 lot"
        assert lots[0].status == "open"
        assert abs(lots[0].quantity - fill_qty) < 1e-6
        assert len(fills) >= 1, "应有逐笔成交明细(OrderFill)"
        fill_sum = sum(f.quantity for f in fills)
        assert abs(fill_sum - order.filled_quantity) < 1e-6, (
            "OrderFill 求和应等于 Order.filled_quantity"
        )

        # 实盘 AccountLedger 不落库(cash_before=None, 由权益对账兜底) —— 诚实断言已知限制
        async with AsyncSessionLocal() as session:
            ledger_rows = (await session.execute(select(AccountLedger))).scalars().all()
        assert len(ledger_rows) == 0

        # --- 交叉对账: 买入净增 SOL ≈ fill_qty(交易所真相) ---
        acct1 = await client.get_account()
        sol_after_buy = _balances(acct1).get("SOL", 0.0)
        delta_buy = sol_after_buy - sol_before
        assert delta_buy > 0, "买入后交易所 SOL 余额应增加"
        assert delta_buy <= fill_qty + 1e-6, "买入净增 SOL 不应超过成交数量"
        assert delta_buy >= fill_qty * 0.9, "买入净增 SOL 不应显著少于成交数量(手续费余量)"

        # --- 市价卖出平仓 ---
        sell_qty = pos_after_buy.quantity
        sell = Signal(
            symbol=SYMBOL,
            strategy="testnet_p03",
            side=SignalSide.SELL,
            price=float(market),
            quantity=float(sell_qty),
            reason=["testnet lifecycle close"],
        )
        res_sell = await engine.execute(sell)
        assert res_sell is not None, "市价卖单未成交"
        assert res_sell["status"] == "FILLED", res_sell
        assert res_sell["fill_qty"] > 0

        # --- SELL 本地记账校验(DB): SellAllocation + 平仓 ---
        sell_cid = res_sell["client_order_id"]
        async with AsyncSessionLocal() as session:
            sell_order = (
                await session.execute(select(Order).where(Order.client_order_id == sell_cid))
            ).scalar_one_or_none()
            allocs = (
                (
                    await session.execute(
                        select(SellAllocation).where(
                            SellAllocation.sell_client_order_id == sell_cid
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert sell_order is not None and sell_order.status == "FILLED"
        assert sell_order.filled_quantity > 0
        assert len(allocs) >= 1, "一笔卖出应至少一条 FIFO 分配(SellAllocation)"
        alloc_sum = sum(a.quantity for a in allocs)
        assert abs(alloc_sum - sell_order.filled_quantity) < 1e-6

        # 内存持仓已清仓
        pos_final = rm.positions.get(SYMBOL)
        assert pos_final.quantity < 1e-9, "平仓后持仓应为 0"

        # --- 交叉对账: 卖出后 SOL 回到基线(交易所真相) ---
        acct2 = await client.get_account()
        sol_final = _balances(acct2).get("SOL", 0.0)
        assert abs(sol_final - sol_before) <= fill_qty * 0.02, (
            f"平仓后 SOL 应回到基线附近(仅剩往返手续费), "
            f"baseline={sol_before:.8f} final={sol_final:.8f}"
        )
