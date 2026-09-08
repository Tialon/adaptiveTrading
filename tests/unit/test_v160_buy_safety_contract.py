"""V11.6 P0-2 证明: BUY 安全契约 —— 开新仓(BUY)的 12 项必要条件逐条收口到统一闸门。

「BUY_ALLOWED」的充要条件(全部同时满足才放行):

    1.  KillSwitch          == OFF
    2.  RiskState           == NORMAL(非 RECOVERY_CHECK / PAUSED / REDUCE_ONLY / KILLED)
    3.  Lifecycle           == TRADING(非 DEGRADED / SAFE_MODE / STOPPED / 预热中)
    4.  FundBreaker         == NORMAL(非 REDUCE_ONLY / PAUSE / KILL)
    5.  Reconciliation      == PASS
    6.  Market              == HEALTHY
    7.  Exchange            == HEALTHY
    8.  CriticalTask        == RUNNING(关键后台任务运行中)
    9.  Shutdown            == false(未停机)
   10.  supervisor          == AVAILABLE(监督器在位, 关键任务受监控)

其中 8~10 是 V11.6 P0-2 新增、此前散落在 run.py 调用方/间接路径, 现收口进 `TradingGate`
两维: `critical_tasks_healthy`(覆盖 8 + 10, 即「关键后台任务未运行」)与 `shutting_down`(覆盖 9)。

本片与 test_v142(异常态绝不 BUY)互补: v142 用真实组件穷举「异常触发」, 本片把 BUY_ALLOWED
**契约本身** 显式枚举为一条条可审计的必要条件, 并验证 run.py 接线会把「关键任务崩溃」与「停机」
两个信号写回闸门(单一权威, 无 bypass)。
"""


import pytest

import run
from at60_risk.fund_circuit_breaker import BreakerAction, FundCircuitBreaker
from at60_risk.risk_manager import RiskManager
from at60_risk.system_lifecycle import SystemLifecycle
from at60_risk.trading_gate import TradingGate


# ---------------------------------------------------------------------------
# 健康基线: 一个「理应放行 BUY」的 TRADING 闸门
# ---------------------------------------------------------------------------

def _healthy_gate() -> tuple[TradingGate, RiskManager, SystemLifecycle, FundCircuitBreaker]:
    rm = RiskManager()
    lc = SystemLifecycle()
    assert lc.warm_up() and lc.sync() and lc.self_check() and lc.ready() and lc.start_trading()
    breaker = FundCircuitBreaker()
    return TradingGate(rm, lc, breaker), rm, lc, breaker


# ---------------------------------------------------------------------------
# BUY 安全契约: 12 项必要条件, 每项单独破坏 → 必须拒绝 BUY
# ---------------------------------------------------------------------------

def _cond(fn):
    """把「破坏某一必要条件」的闭包包装为返回闸门的工厂(健康基线 + 一次变异)。"""
    def make():
        gate, rm, lc, br = _healthy_gate()
        fn(gate, rm, lc, br)
        return gate
    return make


CONDITIONS = [
    ("1 kill_switch ON", _cond(lambda g, rm, *_a: rm.kill_switch.arm("x"))),
    ("2 risk RECOVERY_CHECK", _cond(lambda g, rm, *_a: (rm.kill_switch.arm("x"), rm.state_machine.kill("x"), rm.state_machine.reset()))),
    ("3 risk PAUSED", _cond(lambda g, rm, *_a: rm.pause("x"))),
    ("4 risk REDUCE_ONLY", _cond(lambda g, rm, *_a: rm.reduce_only("x"))),
    ("5 lifecycle DEGRADED", _cond(lambda g, rm, lc, _b: lc.degrade("x"))),
    ("6 reconciliation FAIL", _cond(lambda g, *_a: setattr(g, "reconciled", False))),
    ("7 market anomaly", _cond(lambda g, *_a: setattr(g, "market_data_healthy", False))),
    ("8 exchange unavailable", _cond(lambda g, *_a: setattr(g, "exchange_healthy", False))),
    ("9 fund_breaker REDUCE_ONLY", _cond(lambda g, *_a: setattr(g, "last_breaker_action", BreakerAction.REDUCE_ONLY))),
    ("10 critical task failure", _cond(lambda g, *_a: setattr(g, "critical_tasks_healthy", False))),
    ("11 shutdown", _cond(lambda g, *_a: setattr(g, "shutting_down", True))),
    ("12 supervisor unavailable", _cond(lambda g, *_a: setattr(g, "critical_tasks_healthy", False))),
]
_IDS = [n for n, _ in CONDITIONS]


def test_healthy_gate_allows_buy():
    """基线: 12 项全满足时放行 BUY。"""
    gate, *_ = _healthy_gate()
    assert gate.can_open_position()[0] is True


@pytest.mark.parametrize("name,factory", CONDITIONS, ids=_IDS)
def test_each_condition_blocks_buy(name: str, factory):
    """契约: 任一必要条件被破坏, `can_open_position()` 必须拒绝且给出可审计原因。"""
    ok, reason = factory().can_open_position()
    assert ok is False, f"[{name}] 违反后仍放行 BUY"
    assert reason, f"[{name}] 拒绝但无原因(审计不可用)"


def test_shutdown_also_blocks_reduce():
    """停机窗口既禁 BUY 也禁减仓(与 _on_signal 早退语义一致, 防在途信号偷卖)。"""
    gate, *_ = _healthy_gate()
    gate.shutting_down = True
    assert not gate.can_open_position()[0]
    assert not gate.can_reduce_position()[0]


def test_snapshot_exposes_new_gate_dimensions():
    """闸门快照把新增两维暴露给审计/面板/可观测。"""
    gate, *_ = _healthy_gate()
    snap = gate.snapshot()
    assert snap["shutting_down"] is False
    assert snap["critical_tasks_healthy"] is True
    assert snap["can_open_position"] is True


# ---------------------------------------------------------------------------
# run.py 接线: 关键任务崩溃 / 停机 必须写回闸门(单一权威, 无 bypass)
# ---------------------------------------------------------------------------

class _GateProbe:
    """最小闸门探针: 只记录 run.py 是否把两维信号写回。"""
    shutting_down = False
    critical_tasks_healthy = True


class _FakeLogger:
    def error(self, *a, **kw): ...
    def exception(self, *a, **kw): ...
    def info(self, *a, **kw): ...


class _FakeKillSwitch:
    def arm(self, reason): ...
    async def persist(self): return True


class _FakeRiskManager:
    def __init__(self):
        self.kill_switch = _FakeKillSwitch()


class _FakeLifecycle:
    def enter_safe_mode(self, reason): return True


async def test_critical_failure_flips_gate_critical_tasks_signal():
    """关键后台任务崩溃 → run.py 把 critical_tasks_healthy 写回闸门 → 禁 BUY。"""
    sys = object.__new__(run.AdaptiveTradingSystem)
    sys.logger = _FakeLogger()
    sys.risk_manager = _FakeRiskManager()
    sys.lifecycle = _FakeLifecycle()
    sys._pending_tasks = set()
    sys.trading_gate = _GateProbe()

    sys._handle_critical_task_failure("risk-loop", RuntimeError("boom"))

    assert sys.trading_gate.critical_tasks_healthy is False

    # 回收 fire-and-forget 持久化任务, 避免 never-awaited 警告
    for task in list(sys._pending_tasks):
        await task


async def test_shutdown_flips_gate_shutting_down_signal(monkeypatch):
    """stop() → run.py 把 shutting_down 写回闸门 → 禁 BUY/减仓。"""
    sys = run.AdaptiveTradingSystem()
    sys._running = True
    sys.trading_gate = _GateProbe()

    # 最小化 stop() 触及的引擎/资源(不初始化整套 DB/交易所)
    class _E:
        async def stop(self): ...
    class _S:
        async def close(self): ...
    class _R:
        async def flush_events(self): ...
        kill_switch = _FakeKillSwitch()
    sys.market_engine = _E()
    sys.strategy_engine = _S()
    sys.sentiment_analyzer = None
    sys.risk_manager = _R()
    sys.lifecycle = _FakeLifecycle()

    async def _close_db(): ...
    monkeypatch.setattr(run, "close_db", _close_db)

    await sys.stop()

    assert sys.trading_gate.shutting_down is True
