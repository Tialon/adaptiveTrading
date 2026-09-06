"""V2.0 新模块单元测试: Market Regime / 事件总线 / 幂等执行 / 信号格式"""

import json

import pytest

from analytics.regime import MarketRegimeEngine, RegimeAssessment
from strategy.base import Signal, SignalSide


class TestMarketRegime:
    def test_bull_detection(self):
        eng = MarketRegimeEngine()
        r = eng.evaluate(
            symbol="SOLUSDT", symbol_trend="up",
            symbol_ema_fast=100.0, symbol_ema_slow=99.0,
            recent_high=101.0, recent_low=99.5,  # 低波动
            volume_ratio=1.5, delta_ratio=0.1, cvd_rising=True,
            btc_trend="up", btc_change_24h=3.0,
        )
        assert r.regime == "BULL"
        assert r.confidence >= 0.7

    def test_bear_detection(self):
        eng = MarketRegimeEngine()
        r = eng.evaluate(
            symbol="SOLUSDT", symbol_trend="down",
            symbol_ema_fast=99.0, symbol_ema_slow=100.0,
            recent_high=101.0, recent_low=99.5,
            volume_ratio=0.8, delta_ratio=-0.1, cvd_rising=False,
            btc_trend="down", btc_change_24h=-3.0,
        )
        assert r.regime == "BEAR"

    def test_panic_detection(self):
        eng = MarketRegimeEngine()
        r = eng.evaluate(
            symbol="SOLUSDT", symbol_trend="down",
            symbol_ema_fast=95.0, symbol_ema_slow=100.0,
            recent_high=110.0, recent_low=100.0,  # 振幅 ~9.5%
            volume_ratio=3.0, delta_ratio=-0.2, cvd_rising=False,
            btc_trend="down", btc_change_24h=-5.0,
        )
        assert r.regime == "PANIC"

    def test_sideway_detection(self):
        eng = MarketRegimeEngine()
        r = eng.evaluate(
            symbol="SOLUSDT", symbol_trend="neutral",
            symbol_ema_fast=100.0, symbol_ema_slow=100.0,
            recent_high=100.8, recent_low=99.8,
            volume_ratio=1.0, delta_ratio=0.0, cvd_rising=False,
        )
        assert r.regime == "SIDEWAY"

    def test_strategy_adjustment(self):
        adj_bull = MarketRegimeEngine.strategy_adjustment("BULL")
        assert adj_bull["grid_enabled"] is True
        assert adj_bull["add_position_allowed"] is True

        adj_bear = MarketRegimeEngine.strategy_adjustment("BEAR")
        assert adj_bear["grid_enabled"] is False
        assert adj_bear["add_position_allowed"] is False

        adj_panic = MarketRegimeEngine.strategy_adjustment("PANIC")
        assert adj_panic["buy_boost"] == 0.0

        # 未知环境回落到震荡
        adj_unknown = MarketRegimeEngine.strategy_adjustment("???")
        assert adj_unknown == MarketRegimeEngine.strategy_adjustment("SIDEWAY")

    def test_snapshot(self):
        eng = MarketRegimeEngine()
        eng.evaluate(
            "SOLUSDT", "up", 100.0, 99.0, 101.0, 99.5, 1.2, 0.1, True
        )
        snap = eng.snapshot()
        assert "SOLUSDT" in snap
        assert snap["SOLUSDT"]["regime"] in ("BULL", "SIDEWAY", "BEAR", "PANIC")

    def test_assessment_to_dict(self):
        a = RegimeAssessment(regime="BULL")
        d = a.to_dict()
        assert d["regime"] == "BULL"
        assert "reasons" in d and isinstance(d["reasons"], list)


class TestSignalFormat:
    """V2.0 标准信号格式"""

    def test_reason_list_and_str(self):
        sig = Signal(
            symbol="SOLUSDT", strategy="entry", side=SignalSide.BUY,
            price=100.0, quantity=1.0,
            reason=["CVD上升", "VWAP折价1.2%"],
            score=85.0,
            indicators={"cvd_rising": True},
        )
        assert isinstance(sig.reason, list)
        assert sig.reason_str == "CVD上升; VWAP折价1.2%"
        d = sig.to_dict()
        assert d["score"] == 85.0
        assert d["indicators"] == {"cvd_rising": True}
        assert d["reason"] == ["CVD上升", "VWAP折价1.2%"]

    def test_default_fields(self):
        sig = Signal(symbol="X", strategy="s", side=SignalSide.SELL, price=1.0)
        assert sig.reason == []
        assert sig.indicators == {}
        assert sig.score == 0.0
        assert sig.reason_str == ""


class TestEventBus:
    """V2.0 Redis Stream 事件总线(无 Redis 时降级)"""

    def test_disabled_bus_noop(self):
        from analytics.bus import EventBus

        bus = EventBus(redis_client=None)
        assert not bus.available
        # 发布/消费均静默
        import asyncio

        async def go():
            await bus.publish_market({"type": "trade", "price": 100})
            n = await bus.consume("at:market:events", "g", "c", lambda e: None)
            assert n == 0

        asyncio.run(go())
        assert bus.status()["available"] is False

    def test_publish_with_fake_redis(self):
        from analytics.bus import EventBus

        class FakeRedis:
            def __init__(self):
                self.streams = []

            async def xadd(self, stream, fields, maxlen=None, approximate=None):
                self.streams.append((stream, fields))

            async def xlen(self, stream):
                return len(self.streams)

        fake = FakeRedis()
        bus = EventBus(fake)
        assert bus.available

        import asyncio

        async def go():
            await bus.publish_market({"type": "trade", "symbol": "SOLUSDT", "price": 100.0})
            await bus.publish_signal({"type": "signal", "side": "BUY"})

        asyncio.run(go())
        assert len(fake.streams) == 2
        data = json.loads(fake.streams[0][1]["data"])
        assert data["price"] == 100.0


class TestExecutionIdempotency:
    """V2.0: 执行幂等控制"""

    async def test_duplicate_signal_blocked(self):
        from execution.executor import ExecutionEngine
        from risk.manager import RiskManager

        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)

        sig = Signal(
            symbol="BTCUSDT", strategy="grid", side=SignalSide.BUY,
            price=100.0, quantity=1.0,
        )
        r1 = await engine.execute(sig)
        assert r1 is not None and r1["status"] == "FILLED"

        # 10 秒内同 策略:标的:方向 的重复信号被幂等拦截
        sig2 = Signal(
            symbol="BTCUSDT", strategy="grid", side=SignalSide.BUY,
            price=100.5, quantity=1.0,
        )
        r2 = await engine.execute(sig2)
        assert r2 is None
        assert engine.order_count == 1  # 只执行了一次

    async def test_different_strategy_passes(self):
        from execution.executor import ExecutionEngine
        from risk.manager import RiskManager

        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        engine.idempotency_seconds = 0.05

        sig1 = Signal(symbol="BTCUSDT", strategy="grid", side=SignalSide.BUY, price=100.0, quantity=1.0)
        sig2 = Signal(symbol="BTCUSDT", strategy="entry", side=SignalSide.BUY, price=100.0, quantity=1.0)
        assert await engine.execute(sig1) is not None
        assert await engine.execute(sig2) is not None  # 不同策略不受拦截

    async def test_cooldown_expiry(self):
        import time as time_mod

        from execution.executor import ExecutionEngine
        from risk.manager import RiskManager

        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        engine.idempotency_seconds = 0.1

        sig = Signal(symbol="BTCUSDT", strategy="grid", side=SignalSide.BUY, price=100.0, quantity=1.0)
        assert await engine.execute(sig) is not None
        time_mod.sleep(0.15)
        assert await engine.execute(sig) is not None  # 冷却过了再执行
