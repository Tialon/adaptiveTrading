"""V11.4 P0-2 对账长跑仿真(reconciliation mismatch / 漂移分级 / 对账后恢复交易)。

- 对账失配(lot 总和 != 持仓) -> 对账矩阵 RECOVERY_REQUIRED -> 统一闸门禁开;
  重建后恢复 -> 闸门重开, 不变量成立;
- 零漂移对账 -> 资金熔断 NONE, 闸门保持开放(不误杀正常交易);
- 对账后恢复交易 -> 多周期不变量持续成立。
"""


from at50_execution.drift import compute_drift
from at50_execution.reconciliation_matrix import ReconciliationMatrix, Severity
from at60_risk.fund_circuit_breaker import BreakerAction, FundCircuitBreaker
from at60_risk.risk_manager import RiskManager
from at60_risk.system_lifecycle import SystemLifecycle
from at60_risk.trading_gate import TradingGate
from tests.long_running._harness import (
    _paper_engine,
    _signal,
    assert_financial_invariants,
)

SYMBOL = "SOLUSDT"


class _Harness:
    """系统级闸门栈(与 run.py 装配一致; 免 WS/DB)。"""

    def __init__(self):
        self.rm = RiskManager()
        self.lifecycle = SystemLifecycle()
        self.breaker = FundCircuitBreaker()
        self.gate = TradingGate(self.rm, self.lifecycle, self.breaker)
        self.lifecycle.warm_up()
        self.lifecycle.sync()
        self.lifecycle.self_check()
        self.lifecycle.ready()
        self.lifecycle.start_trading()


class TestReconciliationMismatchSoak:
    async def test_mismatch_blocks_open_then_recovers(self, db_tables):
        """lot 总和 != 持仓 -> 对账矩阵 -> 禁开仓; 恢复一致后闸门重开。"""
        h = _Harness()
        engine = _paper_engine(h.rm)
        r = await engine.execute(_signal("BUY", 2.0, 100.0))
        assert r is not None and r["status"] == "FILLED"

        # 篡改内存 lot, 破坏「lot 总和 == 持仓」守恒
        engine.lot_tracker.lots[SYMBOL][0]["quantity"] = 1.0
        diff = engine.lot_tracker.reconcile(SYMBOL, h.rm.positions.get(SYMBOL).quantity)
        assert diff is not None  # 守恒破坏被检出

        # 守恒差异 -> 对账矩阵(单一内部对账器 -> RECOVERY_REQUIRED, 不冒进 kill)
        matrix = ReconciliationMatrix()
        matrix.ingest("lot", [{"type": "lot_sum_mismatch", "symbol": SYMBOL, **diff}])
        verdict = matrix.verdict()
        assert verdict.severity is Severity.RECOVERY_REQUIRED

        # 复用 run.py 语义: RECOVERY_REQUIRED -> 暂停 -> 闸门禁开
        h.rm.pause(verdict.reasons[0])
        ok, reason = h.gate.can_open_position()
        assert not ok, f"对账失配后仍可开仓(原因: {reason})"

        # 恢复一致(纠正内存 lot)+ 恢复风险态 -> 闸门重开
        engine.lot_tracker.lots[SYMBOL][0]["quantity"] = 2.0
        assert engine.lot_tracker.reconcile(SYMBOL, 2.0) is None
        h.rm.state_machine.recover()
        assert h.gate.can_open_position()[0]

    async def test_zero_drift_keeps_gate_open(self, db_tables):
        """零漂移对账 -> 熔断 NONE, 闸门保持开放(不误杀)。"""
        h = _Harness()
        drift = compute_drift(
            local_equity=10000.0, exchange_equity=10000.0,
            local_position=0.0, exchange_position=0.0,
            local_cash=10000.0, exchange_cash=10000.0,
        )
        assert drift.trusted
        decision = h.breaker.assess(
            equity_drift=drift.equity_drift,
            position_drift=drift.position_drift,
            cash_drift=drift.cash_drift,
        )
        assert decision.action is BreakerAction.NONE
        assert h.gate.can_open_position()[0]


class TestReconciliationThenTradeSoak:
    async def test_reconcile_then_trade_invariants_hold(self, db_tables, clock):
        """对账失配冻结 -> 恢复 -> 继续多周期交易, 不变量持续成立。"""
        h = _Harness()
        engine = _paper_engine(h.rm)

        # 先建仓, 再注入对账失配冻结
        await engine.execute(_signal("BUY", 1.0, 100.0))
        clock.advance(100.0)
        h.rm.pause("对账失配(reconciliation mismatch)")
        assert not h.gate.can_open_position()[0]

        # 恢复
        h.rm.state_machine.recover()
        assert h.gate.can_open_position()[0]

        # 平仓 + 后续多周期交易, 不变量成立
        r = await engine.execute(_signal("SELL", 1.0, 102.0))
        assert r is not None and r["status"] == "FILLED"
        clock.advance(100.0)
        for price in (103.0, 104.0, 105.0):
            r = await engine.execute(_signal("BUY", 1.0, price))
            assert r is not None and r["status"] == "FILLED"
            clock.advance(100.0)
            r = await engine.execute(_signal("SELL", 1.0, price * 1.02))
            assert r is not None and r["status"] == "FILLED"
            clock.advance(100.0)
        await assert_financial_invariants(engine, h.rm, 105.0 * 1.02)
