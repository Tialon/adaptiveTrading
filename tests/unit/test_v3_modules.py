"""V3.0 新模块单元测试: Decision / Portfolio / Alpha / 状态机 / Exit标签"""

import pytest

from at20_analytics.alpha import AlphaEngine
from at20_analytics.engine import MarketAnalytics
from at30_strategy.strategy_base import Signal, SignalSide
from at30_strategy.strategy_decision import DecisionEngine
from at50_risk.risk_portfolio import PortfolioEngine
from at50_risk.risk_position import PositionManager


def make_analytics(**kw):
    defaults = dict(
        symbol="SOLUSDT", price=100.0, vwap=101.0, vwap_deviation=-0.01,
        cvd_rising=True, cvd_slope=0.3, delta_ratio=0.15,
        trend="neutral", ema_fast=100.0, ema_slow=100.0,
        recent_high=102.0, recent_low=99.0, volume_ratio=1.2,
        regime="SIDEWAY", change_pct_24h=1.5,
    )
    defaults.update(kw)
    return MarketAnalytics(**defaults)


def make_signal(strategy="grid", side=SignalSide.BUY, score=80.0, price=100.0, qty=1.0):
    return Signal(
        symbol="SOLUSDT", strategy=strategy, side=side, price=price,
        quantity=qty, reason=["测试"], score=score,
    )


class TestDecisionEngine:
    def test_buy_consensus(self):
        """多策略一致看多 -> BUY"""
        de = DecisionEngine()
        signals = [
            make_signal("entry", SignalSide.BUY, 85.0),
            make_signal("grid", SignalSide.BUY, 60.0),
        ]
        d = de.decide(signals, make_analytics())
        assert d.action == "BUY"
        assert d.confidence > 50
        assert d.net_score > 0

    def test_sell_consensus(self):
        de = DecisionEngine()
        signals = [
            make_signal("exit", SignalSide.SELL, 90.0),
            make_signal("trend", SignalSide.SELL, 75.0),
        ]
        d = de.decide(signals, make_analytics())
        assert d.action == "SELL"

    def test_conflict_low_net_holds(self):
        """买卖对冲 -> 净分不足 -> HOLD"""
        de = DecisionEngine()
        signals = [
            make_signal("entry", SignalSide.BUY, 70.0),
            make_signal("trend", SignalSide.SELL, 70.0),
        ]
        d = de.decide(signals, make_analytics())
        assert d.action == "HOLD"
        assert not d.actionable

    def test_conflict_sell_wins_with_higher_score(self):
        """冲突中卖方分数更高 -> SELL"""
        de = DecisionEngine()
        signals = [
            make_signal("entry", SignalSide.BUY, 65.0),
            make_signal("exit", SignalSide.SELL, 90.0),
        ]
        d = de.decide(signals, make_analytics())
        assert d.action == "SELL"

    def test_panic_blocks_buy(self):
        """PANIC 环境买票归零 -> 不买"""
        de = DecisionEngine()
        signals = [make_signal("entry", SignalSide.BUY, 90.0)]
        d = de.decide(signals, make_analytics(regime="PANIC"))
        assert d.action != "BUY"

    def test_bear_reduces_buy(self):
        """BEAR 环境买入门槛变相提高"""
        de = DecisionEngine()
        signals = [make_signal("entry", SignalSide.BUY, 80.0)]
        d_bear = de.decide(signals, make_analytics(regime="BEAR"))
        d_bull = de.decide(signals, make_analytics(regime="BULL"))
        assert d_bear.net_score < d_bull.net_score

    def test_high_exit_score_forces_sell(self):
        """exit 清仓高分优先(即使买票也不冲突)"""
        de = DecisionEngine()
        signals = [
            make_signal("entry", SignalSide.BUY, 85.0),
            make_signal("exit", SignalSide.SELL, 90.0),
        ]
        d = de.decide(signals, make_analytics())
        assert d.action == "SELL"
        assert any("退出" in r for r in d.reason)

    def test_observe_signals_not_voted(self):
        """观察档信号不计票"""
        de = DecisionEngine()
        sig = make_signal("entry", SignalSide.BUY, 65.0)
        sig.reason.insert(0, "观察档(65分,未达80)")
        d = de.decide([sig], make_analytics())
        assert d.action == "HOLD"
        assert any(v.get("note") == "observe" for v in d.votes)

    def test_empty_signals_hold(self):
        de = DecisionEngine()
        d = de.decide([], make_analytics())
        assert d.action == "HOLD"

    def test_weight_update_by_performance(self):
        """绩效动态调权"""
        de = DecisionEngine()
        de.update_weights({"grid": {"win_rate": 0.9, "profit": 100.0}})
        assert de.weights["grid"] > de.DEFAULT_WEIGHTS["grid"]
        de.update_weights({"trend": {"win_rate": 0.1, "profit": -50.0}})
        assert de.weights["trend"] < de.DEFAULT_WEIGHTS["trend"]


class TestPortfolioEngine:
    def test_buy_then_cost(self):
        pm = PositionManager()
        pe = PortfolioEngine(pm)
        pe.on_buy_fill("SOLUSDT", 10.0, 100.0)
        v = pe.view("SOLUSDT", 100.0)
        assert v.quantity == 10.0
        assert v.avg_cost == 100.0

    def test_buy_fee_amortized_into_avg_cost(self):
        # 回归: 买入费须摊入均价成本(与 FIFO lot 单位成本口径一致)
        pm = PositionManager()
        pe = PortfolioEngine(pm)
        pe.on_buy_fill("SOLUSDT", 1.0, 100.0, fee=1.0)
        v = pe.view("SOLUSDT", 100.0)
        assert v.avg_cost == pytest.approx(101.0)

    def test_profitable_sell_reduces_cost(self):
        """盈利卖出 -> 剩余成本下降(核心: 卖出目的是降本)"""
        pm = PositionManager()
        pe = PortfolioEngine(pm)
        pe.on_buy_fill("SOLUSDT", 10.0, 100.0)  # 成本 100
        realized, new_cost = pe.on_sell_fill("SOLUSDT", 2.0, 150.0)  # 卖 2 个赚 100
        assert realized == pytest.approx(100.0)
        # 剩余 8 个, 已实现 100 -> 均价仍 100, 但保本价下降
        v = pe.view("SOLUSDT", 100.0)
        assert v.quantity == 8.0
        assert v.realized_pnl == pytest.approx(100.0)
        assert v.breakeven_price == pytest.approx(100.0 - 100.0 / 8.0)  # 87.5

    def test_cost_curve_recorded(self):
        pm = PositionManager()
        pe = PortfolioEngine(pm)
        pe.on_buy_fill("SOLUSDT", 1.0, 100.0)
        pe.on_buy_fill("SOLUSDT", 1.0, 110.0)
        pe.on_sell_fill("SOLUSDT", 0.5, 120.0)
        v = pe.view("SOLUSDT", 120.0)
        assert len(v.cost_curve) == 3
        events = pe.cost_events["SOLUSDT"]
        assert events[0].kind == "buy"
        assert events[-1].kind == "sell"

    def test_target_position_by_regime(self):
        pm = PositionManager()
        pe = PortfolioEngine(pm)
        pe.on_buy_fill("SOLUSDT", 1.0, 100.0)
        t_bull = pe.target_position("SOLUSDT", 10000.0, 0.4, "BULL")
        t_bear = pe.target_position("SOLUSDT", 10000.0, 0.4, "BEAR")
        t_panic = pe.target_position("SOLUSDT", 10000.0, 0.4, "PANIC")
        assert t_bull > t_bear
        assert t_panic == 0.0

    def test_rebalance_suggestion(self):
        pm = PositionManager()
        pe = PortfolioEngine(pm)
        pe.on_buy_fill("SOLUSDT", 1.0, 100.0)
        s = pe.rebalance_suggestion("SOLUSDT", 100.0, 5.0)
        assert s["action"] == "BUY"
        s2 = pe.rebalance_suggestion("SOLUSDT", 100.0, 0.5)
        assert s2["action"] == "SELL"

    def test_total_pnl(self):
        pm = PositionManager()
        pe = PortfolioEngine(pm)
        pe.on_buy_fill("SOLUSDT", 2.0, 100.0)
        pe.on_sell_fill("SOLUSDT", 1.0, 120.0)
        v = pe.view("SOLUSDT", 110.0)
        # realized 20 + unrealized (110-100)*1 = 10
        assert v.total_pnl == pytest.approx(30.0)


class TestAlphaEngine:
    def test_high_alpha_on_dip_with_flow(self):
        """折价+资金流入+温和上涨 -> 高分"""
        ae = AlphaEngine()
        a = make_analytics(
            price=99.0, vwap=101.0, vwap_deviation=-0.02,
            cvd_rising=True, cvd_slope=0.5, delta_ratio=0.25,
            recent_high=103.0, recent_low=99.0,
            volume_ratio=1.5, change_pct_24h=1.0, trend="neutral",
        )
        s = ae.score(a)
        assert s.score > 70
        assert s.price_factor > 0.8
        assert s.flow_factor > 0.8
        d = s.to_dict()
        assert "factors" in d and len(d["details"]) == 5

    def test_low_alpha_on_top_with_outflow(self):
        """区间高位+资金流出 -> 低分"""
        ae = AlphaEngine()
        a = make_analytics(
            price=103.0, vwap=101.0, vwap_deviation=0.02,
            cvd_rising=False, cvd_slope=-0.3, delta_ratio=-0.2,
            recent_high=103.0, recent_low=99.0, change_pct_24h=9.0,
        )
        s = ae.score(a)
        assert s.score < 40

    def test_relative_strength_vs_btc(self):
        """跑赢 BTC 加分"""
        ae = AlphaEngine()
        a = make_analytics(change_pct_24h=5.0, trend="up")
        strong = ae.score(a, btc_change_24h=0.0)
        weak = ae.score(a, btc_change_24h=8.0)
        assert strong.trend_factor > weak.trend_factor

    def test_volatility_sweet_spot(self):
        """适中波动(0.3~1.5%)得满分"""
        ae = AlphaEngine()
        mid = ae.score(make_analytics(recent_high=100.5, recent_low=99.5))  # ~1%
        low = ae.score(make_analytics(recent_high=100.02, recent_low=99.98))  # ~0.04%
        high = ae.score(make_analytics(recent_high=105.0, recent_low=95.0))  # ~10%
        assert mid.volatility_factor == 1.0
        assert low.volatility_factor < mid.volatility_factor
        assert high.volatility_factor < mid.volatility_factor


class TestStateMachines:
    def test_order_state_transitions(self):
        from at60_execution.execution_state import OrderState

        assert OrderState.CREATE.can_transition(OrderState.SUBMIT)
        assert OrderState.SUBMIT.can_transition(OrderState.OPEN)
        assert OrderState.OPEN.can_transition(OrderState.PARTIAL_FILL)
        assert OrderState.PARTIAL_FILL.can_transition(OrderState.FILLED)
        # 非法
        assert not OrderState.CREATE.can_transition(OrderState.FILLED)
        # 终态
        assert not OrderState.FILLED.can_transition(OrderState.OPEN)
        assert OrderState.FILLED.terminal

    def test_trade_state_transitions(self):
        from at60_execution.execution_state import TradeState

        assert TradeState.IDLE.can_transition(TradeState.ENTRY_PENDING)
        assert TradeState.ENTRY_PENDING.can_transition(TradeState.HOLDING)
        assert TradeState.ENTRY_PENDING.can_transition(TradeState.IDLE)
        assert TradeState.HOLDING.can_transition(TradeState.EXIT_PENDING)
        assert TradeState.EXIT_PENDING.can_transition(TradeState.CLOSED)
        # 非法: IDLE 直接 EXIT
        assert not TradeState.IDLE.can_transition(TradeState.EXIT_PENDING)

    def test_trade_state_machine_flow(self):
        from at60_execution.execution_state import TradeStateMachine, TradeState

        sm = TradeStateMachine()
        assert sm.can_buy("SOLUSDT")
        sm.on_order_submitted("SOLUSDT", "BUY")
        assert sm.get("SOLUSDT") == TradeState.ENTRY_PENDING
        assert not sm.can_buy("SOLUSDT")  # 挂单中不能重复买

        sm.on_order_filled("SOLUSDT", "BUY", remaining_qty=10.0)
        assert sm.get("SOLUSDT") == TradeState.HOLDING
        assert not sm.can_buy("SOLUSDT")

        sm.on_order_submitted("SOLUSDT", "SELL")
        sm.on_order_filled("SOLUSDT", "SELL", remaining_qty=0.0)
        assert sm.get("SOLUSDT") == TradeState.IDLE  # 全平回 IDLE
        assert sm.can_buy("SOLUSDT")

    def test_partial_sell_keeps_holding(self):
        from at60_execution.execution_state import TradeStateMachine, TradeState

        sm = TradeStateMachine()
        sm.on_order_submitted("SOLUSDT", "BUY")
        sm.on_order_filled("SOLUSDT", "BUY", 10.0)
        sm.on_order_submitted("SOLUSDT", "SELL")
        sm.on_order_filled("SOLUSDT", "SELL", remaining_qty=4.0)
        assert sm.get("SOLUSDT") == TradeState.HOLDING

    def test_buy_blocked_while_pending(self):
        from at60_execution.execution_state import TradeStateMachine

        sm = TradeStateMachine()
        sm.on_order_submitted("SOLUSDT", "BUY")
        # ENTRY_PENDING 下重复买被拒
        assert not sm.can_buy("SOLUSDT")


class TestExitTags:
    """V3.0: Exit Reason 结构化标签"""

    def test_take_profit_tag(self):
        from at30_strategy.strategy_sell import SellStrategy

        s = SellStrategy()
        s.position_provider = lambda sym: (1.0, 100.0, 100.0)
        signals = s.on_market(make_analytics(price=105.5))
        assert len(signals) == 1
        assert any("profit_target" in r for r in signals[0].reason)
        assert "profit_target" in signals[0].indicators["exit_tags"]

    def test_trend_reverse_tag(self):
        from at30_strategy.strategy_sell import SellStrategy

        s = SellStrategy()
        s.position_provider = lambda sym: (1.0, 100.0, 100.0)
        signals = s.on_market(
            make_analytics(price=100.1, trend="down", cvd_falling=True, delta_ratio=-0.1)
        )
        assert len(signals) == 1
        assert any("trend_reverse" in r for r in signals[0].reason)

    def test_overbought_tag(self):
        from at30_strategy.strategy_sell import SellStrategy

        s = SellStrategy()
        s.position_provider = lambda sym: (1.0, 100.0, 110.0)
        signals = s.on_market(make_analytics(price=104.4))
        assert len(signals) == 1
        assert any("overbought" in r for r in signals[0].reason)


class TestSignalResultTracker:
    async def test_register_and_update(self):
        from at30_strategy.strategy_signal_tracker import SignalResultTracker

        tr = SignalResultTracker(window_seconds=3600)
        tr.register(1, "SOLUSDT", "grid", "BUY", 100.0)
        n = await tr.update({"SOLUSDT": 105.0})
        assert n == 1
        rec = tr._tracking[1]
        assert rec["future_profit"] == pytest.approx(0.05)
        assert rec["max_profit"] == pytest.approx(0.05)

        # 反向
        await tr.update({"SOLUSDT": 95.0})
        assert tr._tracking[1]["max_drawdown"] == pytest.approx(-0.05)

    async def test_sell_signal_inverse(self):
        """SELL 信号看反向收益"""
        from at30_strategy.strategy_signal_tracker import SignalResultTracker

        tr = SignalResultTracker()
        tr.register(2, "SOLUSDT", "exit", "SELL", 100.0)
        await tr.update({"SOLUSDT": 95.0})  # 价格跌 -> SELL 正确
        assert tr._tracking[2]["future_profit"] == pytest.approx(0.05)

    async def test_window_expiry(self):
        import time as time_mod

        from at30_strategy.strategy_signal_tracker import SignalResultTracker

        tr = SignalResultTracker(window_seconds=1)
        tr.register(3, "SOLUSDT", "grid", "BUY", 100.0)
        # 快进注册时间
        tr._tracking[3]["registered_at"] = time_mod.time() - 10
        await tr.update({"SOLUSDT": 101.0})
        assert tr._tracking[3]["final"] is True
