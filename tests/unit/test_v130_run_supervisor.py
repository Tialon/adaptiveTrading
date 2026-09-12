"""V11.4 P0-8 run.py 运行时监督审计 —— 局部导入作用域回归。

审计发现: `AdaptiveTradingSystem.initialize()` 内**局部导入**的若干观测函数与枚举
(`evaluate_alerts` / `record_execution` / `record_reconcile_verdict` / `record_breaker_action`
/ `CoreAction`)在其它方法里被引用, 但那些方法没有自己的局部导入、也不在模块级导入, 导致
`NameError`, 且被各自方法的 `except Exception` 静默吞掉(只留 "XX循环异常" 日志):

- `_apply_breaker_decision`  —— 资金熔断执行链(reduce_only / pause / kill)整体失效;
- `_apply_verdict`           —— 对账 KILLED/RECOVERY/DEGRADED 的 kill_switch / pause 处置失效
  (仅 lifecycle 迁移在 `record_reconcile_verdict` 之前执行, 持久急停/暂停不落);
- `_on_signal`               —— 成交后双仓记账(bucket on_buy/on_sell_fill + persist)失效;
- `_risk_loop`               —— 阈值告警(evaluate_alerts)永不评估;
- `_apply_core_action`       —— 核心仓 ADD/REDUCE 执行失效。

本测试用最小假系统直接调用上述方法, 断言「不 NameError 且处置副作用真实发生」,
锁定修复, 防止这些监督/执行路径因作用域问题再次静默失效。
"""


import run
from at60_execution.observability import MetricsStore
from at40_portfolio.core_manager import CoreAction
from at50_risk.fund_circuit_breaker import BreakerAction, BreakerDecision
from at60_execution.reconciliation_matrix import Severity, Verdict
from at50_risk.system_lifecycle import SystemLifecycle


class _Log:
    """最小日志桩: 任意方法调用均 no-op(适配 structlog 的 .info/.warning/.error/.exception)。"""

    def __getattr__(self, name):
        return lambda *a, **k: None


class _KillSwitch:
    def __init__(self):
        self.armed_reason = None
        self.origin = "MANUAL"

    def arm(self, reason, origin="MANUAL"):  # V13: 真实接口带 origin
        self.armed_reason = reason
        self.origin = origin

    async def persist(self):
        pass


class _RiskManager:
    def __init__(self):
        self.kill_switch = _KillSwitch()
        self.reduced = None
        self.paused = None

    def reduce_only(self, reason):
        self.reduced = reason

    def pause(self, reason):
        self.paused = reason


class _Gate:
    def __init__(self):
        self.last_breaker_action = None
        self.reconciled = None
        self.exchange_healthy = None

    def can_open_position(self):
        return (True, "")

    def can_reduce_position(self):
        return (True, "")


class _ExecutionEngine:
    def __init__(self, result):
        self._result = result
        self.executed = []

    async def execute(self, sig):
        self.executed.append(sig)
        return self._result


class _BucketManager:
    def __init__(self):
        self.bought = []
        self.sold = []
        self.persisted = 0

    def on_buy_fill(self, symbol, qty, price, bucket):
        self.bought.append((symbol, qty, price, bucket))

    def on_sell_fill(self, symbol, qty, price, bucket):
        self.sold.append((symbol, qty, price, bucket))

    async def persist(self, symbol):
        self.persisted += 1


def _bare_system():
    """绕过 __init__(避免加载 settings/.env), 只装配监督路径所需的最小依赖。"""
    sys = object.__new__(run.AdaptiveTradingSystem)
    sys.logger = _Log()
    sys.risk_manager = _RiskManager()
    sys.trading_gate = _Gate()
    sys.metrics = MetricsStore()
    sys.lifecycle = SystemLifecycle()
    sys.current_equity = 1000.0
    return sys


class TestApplyBreakerDecision:
    async def test_reduce_only_enforced(self, monkeypatch):
        sys = _bare_system()
        monkeypatch.setattr(
            run.AdaptiveTradingSystem, "_record_breaker_decision",
            lambda self, d, s, dr: _noop(),
        )
        d = BreakerDecision(action=BreakerAction.REDUCE_ONLY, reason="equity=0.3%")
        await sys._apply_breaker_decision(d, "SOLUSDT", None)
        assert sys.risk_manager.reduced is not None, "REDUCE_ONLY 应触发 reduce_only"
        assert sys.metrics.get_counter("breaker_reduce_only") == 1

    async def test_kill_enforced(self, monkeypatch):
        sys = _bare_system()
        monkeypatch.setattr(
            run.AdaptiveTradingSystem, "_record_breaker_decision",
            lambda self, d, s, dr: _noop(),
        )
        d = BreakerDecision(action=BreakerAction.KILL, reason="equity=0.6%")
        await sys._apply_breaker_decision(d, "SOLUSDT", None)
        assert sys.risk_manager.kill_switch.armed_reason is not None, "KILL 应 arm 急停"
        assert sys.lifecycle.is_safe_mode, "KILL 应进入 SAFE_MODE"
        assert sys.metrics.get_counter("breaker_kill") == 1


class TestApplyVerdict:
    async def test_killed_arms_kill_switch(self):
        sys = _bare_system()
        await sys._apply_verdict(Verdict(Severity.KILLED, [], ["equity drift"]))
        assert sys.risk_manager.kill_switch.armed_reason is not None, "KILLED 应持久 arm 急停"
        assert sys.metrics.get_counter("reconcile_killed") == 1

    async def test_degraded_pauses(self):
        sys = _bare_system()
        await sys._apply_verdict(Verdict(Severity.DEGRADED, [], ["data incomplete"]))
        assert sys.risk_manager.paused is not None, "DEGRADED 应触发 pause"
        assert sys.metrics.get_counter("reconcile_degraded") == 1


class TestApplyCoreAction:
    async def test_add_executes_core_buy(self):
        sys = _bare_system()
        sys.execution_engine = _ExecutionEngine(
            {"status": "FILLED", "fill_qty": 1.0, "fill_price": 100.0}
        )
        sys.bucket_manager = _BucketManager()
        decision = {"action": CoreAction.ADD, "reason": "test", "add_qty": 1.0}
        await sys._apply_core_action("SOLUSDT", 100.0, decision)
        assert len(sys.execution_engine.executed) == 1, "ADD 应下单"
        assert len(sys.bucket_manager.bought) == 1, "ADD 成交应入核心仓"
        assert sys.bucket_manager.persisted == 1

    async def test_reduce_executes_core_sell(self):
        sys = _bare_system()
        sys.execution_engine = _ExecutionEngine(
            {"status": "FILLED", "fill_qty": 0.5, "fill_price": 100.0}
        )
        sys.bucket_manager = _BucketManager()
        decision = {"action": CoreAction.REDUCE, "reason": "test", "reduce_qty": 0.5}
        await sys._apply_core_action("SOLUSDT", 100.0, decision)
        assert len(sys.execution_engine.executed) == 1, "REDUCE 应下单"
        assert len(sys.bucket_manager.sold) == 1, "REDUCE 成交应减核心仓"
        assert sys.bucket_manager.persisted == 1


async def _noop():
    """monkeypatch 替换 _record_breaker_decision 用的空协程(避免 DB 落库)。"""
    pass
