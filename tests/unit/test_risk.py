"""风控单元测试:持仓 / 回撤 / 熔断 / 风控审批"""

import pytest

from risk.breaker import CircuitBreaker
from risk.drawdown import DrawdownController
from risk.manager import RiskManager
from risk.position import PositionManager
from strategy.base import Signal, SignalSide


def make_signal(side=SignalSide.BUY, symbol="BTCUSDT", price=100.0, qty=None, quote=None):
    return Signal(
        symbol=symbol,
        strategy="test",
        side=side,
        price=price,
        quantity=qty,
        quote_amount=quote,
    )


class TestPositionManager:
    def test_buy_updates_avg(self):
        pm = PositionManager()
        pm.apply_buy("BTCUSDT", 1.0, 100.0)
        pm.apply_buy("BTCUSDT", 1.0, 200.0)
        pos = pm.get("BTCUSDT")
        assert pos.quantity == 2.0
        assert pos.avg_price == 150.0
        assert pos.peak_price == 200.0

    def test_sell_realizes_pnl(self):
        pm = PositionManager()
        pm.apply_buy("BTCUSDT", 2.0, 100.0)
        pos, pnl = pm.apply_sell("BTCUSDT", 1.0, 150.0)
        assert pnl == pytest.approx(50.0)
        assert pos.quantity == 1.0
        assert pos.realized_pnl == pytest.approx(50.0)

    def test_sell_cannot_oversell(self):
        pm = PositionManager()
        pm.apply_buy("BTCUSDT", 1.0, 100.0)
        pos, _ = pm.apply_sell("BTCUSDT", 5.0, 120.0)
        assert pos.quantity == 0.0

    def test_empty_position_none(self):
        pm = PositionManager()
        assert pm.get_or_none("BTCUSDT") is None

    def test_peak_price_update(self):
        pm = PositionManager()
        pm.apply_buy("BTCUSDT", 1.0, 100.0)
        pm.update_price("BTCUSDT", 180.0)
        assert pm.get("BTCUSDT").peak_price == 180.0


class TestDrawdown:
    def test_peak_tracking(self):
        dc = DrawdownController(initial_equity=100000.0)
        dd, breach = dc.update(110000.0)
        assert dd == 0.0 and not breach
        dd, breach = dc.update(100000.0)
        assert dd == pytest.approx(110000.0 / 110000.0 * 0 + (110000.0 - 100000.0) / 110000.0) or dd == pytest.approx(1 / 11)

    def test_breach(self):
        dc = DrawdownController(initial_equity=100000.0)
        dc.update(100000.0)
        dd, breach = dc.update(89000.0)  # 11% 回撤
        assert breach


class TestCircuitBreaker:
    def test_trip_on_drawdown(self):
        cb = CircuitBreaker()
        assert not cb.is_open
        cb.check_drawdown(0.15)
        assert cb.is_open
        assert "回撤" in cb.reason

    def test_cooldown_expires(self, monkeypatch):
        import time as time_mod

        cb = CircuitBreaker()
        cb.check_drawdown(0.15)
        assert cb.is_open
        # 模拟冷却期过去
        future = time_mod.time() + cb.cooldown_seconds + 1
        monkeypatch.setattr(time_mod, "time", lambda: future)
        assert not cb.is_open

    def test_daily_loss_trip(self):
        cb = CircuitBreaker()
        cb._day_start_equity = 100000.0
        assert cb.check_daily_loss(94000.0)  # -6% > 5%

    def test_manual_trip_and_reset(self):
        cb = CircuitBreaker()
        cb.manual_trip("测试")
        assert cb.is_open
        cb.reset()
        assert not cb.is_open


class TestRiskManager:
    async def test_buy_approved(self):
        rm = RiskManager()
        sig = make_signal(qty=1.0, price=100.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert d.approved
        assert d.quantity == 1.0

    async def test_buy_position_limit(self):
        rm = RiskManager()
        # 买满限额(risk_max_position_quote=20000)
        rm.positions.apply_buy("BTCUSDT", 199.0, 100.0)  # 19900
        sig = make_signal(qty=10.0, price=100.0)  # 想再买 1000
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert d.approved
        assert d.quantity == pytest.approx(1.0)  # 缩量到剩余 100 USDT

    async def test_buy_rejected_when_full(self):
        rm = RiskManager()
        rm.positions.apply_buy("BTCUSDT", 200.0, 100.0)  # 满 20000
        sig = make_signal(qty=1.0, price=100.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved
        assert "持仓" in d.reason

    async def test_sell_without_position_rejected(self):
        rm = RiskManager()
        sig = make_signal(side=SignalSide.SELL, qty=1.0, price=100.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved

    async def test_sell_capped_to_position(self):
        rm = RiskManager()
        rm.positions.apply_buy("BTCUSDT", 1.0, 100.0)
        sig = make_signal(side=SignalSide.SELL, qty=5.0, price=110.0)
        d = await rm.check(sig, {"BTCUSDT": 110.0})
        assert d.approved
        assert d.quantity == 1.0

    async def test_breaker_blocks_all(self):
        rm = RiskManager()
        rm.breaker.manual_trip("测试熔断")
        sig = make_signal(qty=1.0, price=100.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved
        assert "熔断" in d.reason

    async def test_min_notional_reject(self):
        rm = RiskManager()
        sig = make_signal(qty=0.01, price=100.0)  # 1 USDT
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved

    def test_equity_calculation(self):
        rm = RiskManager()
        rm.positions.apply_buy("BTCUSDT", 1.0, 100.0)
        # 未实现盈亏 +50
        assert rm.equity({"BTCUSDT": 150.0}) == pytest.approx(100050.0)
