"""V12 §10-11: 主网只读接管(账户快照 + 对账 + HODL 基线)

- §10: 首次主网启动只读接管 —— 拉取余额/SOL/挂单/成交历史, 产出账户快照/持仓快照/
  交易所真相对账/对账结果。
- §11: 把交易所既有 SOL 视为「初始持仓」, 记基线, 不机械清仓、不强制再平衡。
- 对账未通过(账户快照失败 / 意外挂单 / 本地持仓与交易所 SOL 漂移)→ `allowed=False`。

只用假 REST 客户端(无网络), 覆盖快照与接管两条路径。
"""

import pytest

from at60_execution.mainnet_takeover import MainnetTakeover


class _FakeRest:
    """假 REST 客户端: 返回固定账户/挂单/成交, 可注入失败。"""

    def __init__(
        self,
        usdt_total="5000.0",
        sol_total="50.0",
        price=100.0,
        open_orders=None,
        trades=None,
        account_error=None,
        price_error=None,
    ):
        self.usdt_total = usdt_total
        self.sol_total = sol_total
        self.price = price
        self.open_orders = open_orders if open_orders is not None else []
        self.trades = trades if trades is not None else []
        self.account_error = account_error
        self.price_error = price_error

    async def get_account(self):
        if self.account_error:
            raise RuntimeError(self.account_error)
        return {
            "balances": [
                {"asset": "USDT", "free": self.usdt_total, "locked": "0.0"},
                {"asset": "SOL", "free": self.sol_total, "locked": "0.0"},
            ]
        }

    async def get_price(self, symbol):
        if self.price_error:
            raise RuntimeError(self.price_error)
        return self.price

    async def get_open_orders(self, symbol):
        return list(self.open_orders)

    async def get_my_trades(self, symbol, limit=50):
        return list(self.trades)


class _FakeHodl:
    """假 HODL 基准: 记录 record_baseline 调用, 返回可配置的首次记录结果。"""

    def __init__(self, first_record=True):
        self.calls = []
        self.first_record = first_record

    async def record_baseline(self, equity, sol_qty, sol_price):
        self.calls.append((equity, sol_qty, sol_price))
        return self.first_record


def _order(order_id=1, side="SELL"):
    return {
        "orderId": order_id,
        "side": side,
        "price": "99.0",
        "origQty": "1.0",
        "executedQty": "0.0",
        "status": "NEW",
    }


class TestSnapshot:
    async def test_snapshot_ok(self):
        t = MainnetTakeover(rest_client=_FakeRest(), symbol="SOLUSDT")
        s = await t.snapshot()
        assert s["ok"] is True
        assert s["price"] == pytest.approx(100.0)
        assert s["account"]["usdt_total"] == pytest.approx(5000.0)
        assert s["account"]["sol_total"] == pytest.approx(50.0)
        # 权益 = USDT + SOL*价格
        assert s["equity"] == pytest.approx(5000.0 + 50.0 * 100.0)
        assert s["position"]["sol_qty"] == pytest.approx(50.0)
        assert s["open_order_count"] == 0
        assert s["recent_trade_count"] == 0

    async def test_snapshot_no_rest(self):
        t = MainnetTakeover(rest_client=None, symbol="SOLUSDT")
        s = await t.snapshot()
        assert s["ok"] is False

    async def test_snapshot_account_error(self):
        t = MainnetTakeover(rest_client=_FakeRest(account_error="boom"), symbol="SOLUSDT")
        s = await t.snapshot()
        assert s["ok"] is False
        assert "get_account 失败" in s["error"]

    async def test_snapshot_price_error(self):
        t = MainnetTakeover(rest_client=_FakeRest(price_error="boom"), symbol="SOLUSDT")
        s = await t.snapshot()
        assert s["ok"] is False
        assert "get_price 失败" in s["error"]

    async def test_snapshot_open_orders_and_trades(self):
        rest = _FakeRest(
            open_orders=[_order(1, "SELL")],
            trades=[{"orderId": 9, "isBuyer": True, "price": "98.0", "qty": "1.0"}],
        )
        t = MainnetTakeover(rest_client=rest, symbol="SOLUSDT")
        s = await t.snapshot()
        assert s["ok"] is True
        assert s["open_order_count"] == 1
        assert s["open_orders"][0]["side"] == "SELL"
        assert s["recent_trade_count"] == 1
        assert s["recent_trades"][0]["side"] == "BUY"  # isBuyer -> BUY


class TestTakeover:
    async def test_initial_position_records_baseline(self):
        # 本地空仓 + 交易所有 SOL = 初始持仓(§11), 允许接管并记基线。
        hodl = _FakeHodl()
        t = MainnetTakeover(
            rest_client=_FakeRest(usdt_total="5000.0", sol_total="50.0", price=100.0),
            symbol="SOLUSDT",
            hodl_benchmark=hodl,
        )
        r = await t.takeover(local_sol_qty=0.0)
        assert r["allowed"] is True
        assert r["blocked_reasons"] == []
        assert r["baseline_recorded"] is True
        assert r["reconciliation"]["initial_position_detected"] is True
        # 基线用交易所真实 SOL 数量 + 价格 + 权益
        assert len(hodl.calls) == 1
        equity, sol_qty, sol_price = hodl.calls[0]
        assert sol_qty == pytest.approx(50.0)
        assert sol_price == pytest.approx(100.0)
        assert equity == pytest.approx(10000.0)

    async def test_no_position_no_baseline_not_blocked(self):
        # 本地空仓 + 交易所也空仓 = 全新账户, 允许接管(基线记 0 持仓)。
        hodl = _FakeHodl()
        t = MainnetTakeover(
            rest_client=_FakeRest(usdt_total="10000.0", sol_total="0.0", price=100.0),
            symbol="SOLUSDT",
            hodl_benchmark=hodl,
        )
        r = await t.takeover(local_sol_qty=0.0)
        assert r["allowed"] is True
        assert r["reconciliation"]["initial_position_detected"] is False

    async def test_unexpected_open_order_blocks(self):
        t = MainnetTakeover(
            rest_client=_FakeRest(open_orders=[_order(1, "SELL")]),
            symbol="SOLUSDT",
            hodl_benchmark=_FakeHodl(),
        )
        r = await t.takeover(local_sol_qty=0.0)
        assert r["allowed"] is False
        assert any("意外挂单" in x for x in r["blocked_reasons"])

    async def test_position_drift_blocks(self):
        # 本地持仓 30, 交易所 50 -> 漂移, 拒绝接管。
        t = MainnetTakeover(
            rest_client=_FakeRest(sol_total="50.0"),
            symbol="SOLUSDT",
            hodl_benchmark=_FakeHodl(),
        )
        r = await t.takeover(local_sol_qty=30.0)
        assert r["allowed"] is False
        assert any("漂移" in x for x in r["blocked_reasons"])

    async def test_account_failure_blocks(self):
        t = MainnetTakeover(
            rest_client=_FakeRest(account_error="boom"),
            symbol="SOLUSDT",
            hodl_benchmark=_FakeHodl(),
        )
        r = await t.takeover(local_sol_qty=0.0)
        assert r["allowed"] is False
        assert r["reconciliation"]["api_ok"] is False
        assert r["baseline_recorded"] is False

    async def test_takeover_without_hodl(self):
        # hodl_benchmark 未注入也不应抛错。
        t = MainnetTakeover(rest_client=_FakeRest(), symbol="SOLUSDT")
        r = await t.takeover(local_sol_qty=0.0)
        assert r["allowed"] is True
        assert r["baseline_recorded"] is False
