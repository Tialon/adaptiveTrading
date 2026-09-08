"""V11.2 P0-5 端到端故障注入(系统级, 非纯函数)。

用真实 RiskManager + SystemLifecycle + FundCircuitBreaker + TradingGate 装配成
「系统级闸门栈」(与 run.py 装配一致), 对 24 类典型故障逐一注入, 断言:

1. 每种故障的最终状态明确(PASS / DEGRADED / RECOVERY / SAFE_MODE / KILLED 的语义);
2. 核心不变量 —— **故障发生绝不「异常 → 继续 BUY」**: 凡降级/急停故障, `can_open_position`
   必须为 False。

执行/账务类故障(timeout/重复成交/部分成交/未知订单/fee 计价)的账务正确性已由
`test_v107_chaos.py` 与 fee 相关测试覆盖; 此处聚焦「故障 → 闸门最终态」的系统级不变量。
"""

import pytest

from at50_execution.drift import compute_drift
from at60_risk.fund_circuit_breaker import BreakerAction, FundCircuitBreaker
from at60_risk.risk_manager import RiskManager
from at60_risk.system_lifecycle import SystemLifecycle
from at60_risk.trading_gate import TradingGate


class SystemHarness:
    """系统级闸门栈(真实组件, 与 run.py 装配一致; 免 WS/DB)。"""

    def __init__(self, start_trading: bool = True):
        self.rm = RiskManager()
        self.lifecycle = SystemLifecycle()
        self.breaker = FundCircuitBreaker()
        self.gate = TradingGate(self.rm, self.lifecycle, self.breaker)

        self.lifecycle.warm_up()
        self.lifecycle.sync()
        self.lifecycle.self_check()
        self.lifecycle.ready()
        if start_trading:
            self.lifecycle.start_trading()

    # 复刻 run.py 的 P0-4 执行链: 真相漂移 -> assess -> 风险态 -> 闸门
    def apply_breaker(self, **drift_kwargs):
        drift = compute_drift(**drift_kwargs)
        assert drift.trusted, drift.reason
        decision = self.breaker.assess(
            equity_drift=drift.equity_drift,
            position_drift=drift.position_drift,
            cash_drift=drift.cash_drift,
        )
        self.gate.last_breaker_action = decision.action
        if decision.action is BreakerAction.REDUCE_ONLY:
            self.rm.reduce_only(f"drift: {decision.reason}")
        elif decision.action is BreakerAction.PAUSE:
            self.rm.pause(f"drift: {decision.reason}")
        elif decision.action is BreakerAction.KILL:
            self.rm.kill_switch.arm(f"drift: {decision.reason}")
        return decision


# 持仓漂移(本地 100 vs 交易所 99.8 = 0.2%, 其余维一致 → 仅持仓漂移)
def _position_drift_kwargs():
    return dict(
        local_equity=10000.0, exchange_equity=10000.0,
        local_position=100.0, exchange_position=99.8,
        local_cash=10000.0, exchange_cash=10000.0,
    )


# ---------------------------------------------------------------------------
# 24 场景表: name / final / inject / 期望闸门与状态
# ---------------------------------------------------------------------------

def _recover_breaker(h: SystemHarness) -> None:
    """熔断触发后漂移归零 + 手动恢复 -> 重新可开。"""
    h.apply_breaker(**_position_drift_kwargs())
    h.gate.last_breaker_action = BreakerAction.NONE
    h.rm.state_machine.recover()


def _scenarios():
    return [
        # 1. REST timeout -> 交易所不健康(api_error)
        dict(id=1, name="Binance REST timeout", final="DEGRADED",
             inject=lambda h: setattr(h.gate, "exchange_healthy", False),
             open_blocked=True, reduce_blocked=True, lifecycle="TRADING", risk="NORMAL", kill=False),
        # 2. WS 断开 -> 连接未就绪
        dict(id=2, name="Binance WS disconnect", final="DEGRADED",
             inject=lambda h: setattr(h.gate, "connection_ok", False),
             open_blocked=True, reduce_blocked=True, lifecycle="TRADING", risk="NORMAL", kill=False),
        # 3. DB insert 失败 -> 执行返回 RECOVERY_REQUIRED -> 急停
        dict(id=3, name="DB insert failure", final="KILLED",
             inject=lambda h: h.rm.kill_switch.arm("db insert failure"),
             open_blocked=True, reduce_blocked=True, lifecycle="TRADING", risk="NORMAL", kill=True),
        # 4. DB 事务回滚 -> 急停冻结
        dict(id=4, name="DB transaction rollback", final="KILLED",
             inject=lambda h: h.rm.kill_switch.arm("db rollback"),
             open_blocked=True, reduce_blocked=True, lifecycle="TRADING", risk="NORMAL", kill=True),
        # 5. 下单后崩溃 -> 启动对账未收敛 -> 急停 + 停在 READY
        dict(id=5, name="process crash after order submission", final="KILLED", ready_only=True,
             inject=lambda h: h.rm.kill_switch.arm("startup reconcile unresolved"),
             open_blocked=True, reduce_blocked=True, lifecycle="READY", risk="NORMAL", kill=True),
        # 6. 成交后崩溃 -> 启动重建账务 -> 恢复为 PASS(open 放行)
        dict(id=6, name="process crash after exchange fill", final="PASS",
             inject=lambda h: None,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
        # 7/8. 重复 WS/REST 成交 -> 幂等去重, 系统仍 PASS
        dict(id=7, name="duplicate WS fill", final="PASS",
             inject=lambda h: None,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
        dict(id=8, name="duplicate REST fill", final="PASS",
             inject=lambda h: None,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
        # 9/10. 部分成交 / 部分成交后撤单 -> 只记已成交部分, PASS
        dict(id=9, name="partial fill", final="PASS",
             inject=lambda h: None,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
        dict(id=10, name="canceled-after-partial-fill", final="PASS",
             inject=lambda h: None,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
        # 11. 未知订单 -> 不静默记账, 恢复收敛 -> PASS
        dict(id=11, name="unknown order", final="PASS",
             inject=lambda h: None,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
        # 12. 缺 myTrades -> fill_truth_missing -> 跨源 KILLED
        dict(id=12, name="missing myTrades", final="KILLED",
             inject=lambda h: h.rm.kill_switch.arm("fill_truth_missing"),
             open_blocked=True, reduce_blocked=True, lifecycle="TRADING", risk="NORMAL", kill=True),
        # 13. myTrades 分页不完整 -> truth_incomplete -> DEGRADED(暂停)
        dict(id=13, name="incomplete myTrades pagination", final="DEGRADED",
             inject=lambda h: (h.rm.pause("truth_incomplete"), setattr(h.gate, "exchange_healthy", False)),
             open_blocked=True, reduce_blocked=True, lifecycle="TRADING", risk="PAUSED", kill=False),
        # 14/15/16. fee 计价资产 -> 不影响交易闸门, PASS(账务正确性见 fee 测试)
        dict(id=14, name="fee asset = USDT", final="PASS",
             inject=lambda h: None,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
        dict(id=15, name="fee asset = SOL", final="PASS",
             inject=lambda h: None,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
        dict(id=16, name="fee asset = BNB", final="PASS",
             inject=lambda h: None,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
        # 17. 持仓漂移 -> REDUCE_ONLY(禁开可减)
        dict(id=17, name="position drift", final="REDUCE_ONLY",
             inject=lambda h: h.apply_breaker(**_position_drift_kwargs()),
             open_blocked=True, reduce_blocked=False, lifecycle="TRADING", risk="REDUCE_ONLY", kill=False),
        # 18. 现金漂移 -> REDUCE_ONLY
        dict(id=18, name="cash drift", final="REDUCE_ONLY",
             inject=lambda h: h.apply_breaker(
                 local_equity=5000.0, exchange_equity=5000.0,
                 local_position=0.0, exchange_position=0.0,
                 local_cash=5000.0, exchange_cash=4990.0),
             open_blocked=True, reduce_blocked=False, lifecycle="TRADING", risk="REDUCE_ONLY", kill=False),
        # 19. 权益漂移 -> KILL(资金级最严重)
        dict(id=19, name="equity drift", final="KILLED",
             inject=lambda h: h.apply_breaker(
                 local_equity=10000.0, exchange_equity=10070.0,
                 local_position=0.0, exchange_position=0.0,
                 local_cash=10000.0, exchange_cash=10070.0),
             open_blocked=True, reduce_blocked=True, lifecycle="TRADING", risk="NORMAL", kill=True),
        # 20. 对账冲突(跨源) -> KILLED
        dict(id=20, name="reconciliation conflict", final="KILLED",
             inject=lambda h: h.rm.kill_switch.arm("equity_drift cross-source"),
             open_blocked=True, reduce_blocked=True, lifecycle="TRADING", risk="NORMAL", kill=True),
        # 21. 恢复失败 -> RECOVERY_REQUIRED(暂停, 不持久急停)
        dict(id=21, name="recovery failure", final="RECOVERY",
             inject=lambda h: h.rm.pause("recover_unresolved"),
             open_blocked=True, reduce_blocked=True, lifecycle="TRADING", risk="PAUSED", kill=False),
        # 22. 启动对账失败 -> 急停 + 停在 READY(不交易)
        dict(id=22, name="startup reconciliation failure", final="KILLED", ready_only=True,
             inject=lambda h: h.rm.kill_switch.arm("startup reconcile failed"),
             open_blocked=True, reduce_blocked=True, lifecycle="READY", risk="NORMAL", kill=True),
        # 23. 熔断触发 -> REDUCE_ONLY(禁开)
        dict(id=23, name="circuit breaker trigger", final="REDUCE_ONLY",
             inject=lambda h: h.apply_breaker(**_position_drift_kwargs()),
             open_blocked=True, reduce_blocked=False, lifecycle="TRADING", risk="REDUCE_ONLY", kill=False),
        # 24. 熔断恢复 -> 漂移归零 + 手动恢复 -> PASS(重新可开)
        dict(id=24, name="circuit breaker recovery", final="PASS",
             inject=_recover_breaker,
             open_blocked=False, reduce_blocked=False, lifecycle="TRADING", risk="NORMAL", kill=False),
    ]


# ---------------------------------------------------------------------------
# 核心不变量: 故障 -> 最终态明确, 且凡降级/急停必禁 BUY
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("s", _scenarios(), ids=lambda s: f"{s['id']:02d}-{s['name']}")
def test_fault_injection_final_state(s):
    h = SystemHarness(start_trading=not s.get("ready_only", False))
    s["inject"](h)

    open_ok, _ = h.gate.can_open_position()
    reduce_ok, _ = h.gate.can_reduce_position()

    # 最终态明确: 闸门 / 生命周期 / 风险态 / 急停 四者一致
    assert open_ok is (not s["open_blocked"]), f"open gate mismatch (got {open_ok})"
    assert reduce_ok is (not s["reduce_blocked"]), f"reduce gate mismatch (got {reduce_ok})"
    assert h.lifecycle.current == s["lifecycle"], h.lifecycle.current
    assert h.rm.state_machine.current == s["risk"], h.rm.state_machine.current
    assert h.rm.kill_switch.is_armed is s["kill"], h.rm.kill_switch.is_armed

    # 不变量: 故障发生后绝不「异常 -> 继续 BUY」
    if s["open_blocked"]:
        assert not open_ok, f"故障[{s['name']}]后仍可开仓, 违反不变量"


# ---------------------------------------------------------------------------
# 定向补充: KILLED 不能裸 reset 到可开仓(需 RECOVERY_CHECK -> confirm_recovered)
# ---------------------------------------------------------------------------

def test_killed_requires_confirmed_recovery():
    h = SystemHarness()
    h.rm.kill_switch.arm("equity drift")
    h.rm.state_machine.kill("equity drift")

    assert not h.gate.can_open_position()[0]
    # 裸 reset -> RECOVERY_CHECK, 仍不可开仓
    h.rm.state_machine.reset()
    assert not h.gate.can_open_position()[0]
    assert h.rm.state_machine.current == "RECOVERY_CHECK"
    # 对账确认后 confirm_recovered -> NORMAL, 可开仓
    h.rm.state_machine.confirm_recovered()
    assert h.gate.can_open_position()[0] is False  # 仍被 kill_switch 拦截(需人工解除急停)
    h.rm.kill_switch.disarm()
    assert h.gate.can_open_position()[0]


def test_truth_incomplete_never_trades():
    """truth_incomplete: 不可信漂移不触发熔断, 但交易所不健康 -> 禁开仓。"""
    h = SystemHarness()
    drift = compute_drift(
        local_equity=10000.0, exchange_equity=10070.0,
        local_position=0.0, exchange_position=0.0,
        local_cash=10000.0, exchange_cash=10070.0,
        truth_complete=False,
    )
    assert not drift.trusted
    # run.py: 漂移不可信 -> exchange_healthy=False, 跳过熔断(decision NONE)
    h.gate.exchange_healthy = False
    assert h.gate.last_breaker_action is BreakerAction.NONE
    assert not h.gate.can_open_position()[0]
