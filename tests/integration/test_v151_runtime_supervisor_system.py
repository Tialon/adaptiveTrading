"""V11.5 P0-2 RuntimeSupervisor 与 run.py 集成测试。

覆盖:
1. critical 任务异常 → 进入安全状态(急停 arm + 生命周期 SAFE_MODE + 持久化调度);
2. `_on_signal` 停机后拒绝处理(含 BUY), 运行中正常经过交易闸门;
3. `_apply_core_action` 停机后拒绝核心仓 ADD(BUY);
4. `stop()` 置 `_shutting_down` 标志 + 幂等 + 关闭各资源(market/strategy/risk flush/close_db);
5. `stop()` 取消监督器中的后台任务。

不改变交易策略语义: 仅在停机窗口内冻结开仓, 不触碰既有闸门/风控/记账逻辑。
"""

import asyncio

import run
from at01_common.runtime_supervisor import RuntimeSupervisor


# ---------------------------------------------------------------------------
# 轻量 fakes(避免初始化整套引擎/DB)
# ---------------------------------------------------------------------------

class _FakeLogger:
    def __init__(self):
        self.errors = []

    def error(self, *a, **kw):
        self.errors.append((a, kw))

    def exception(self, *a, **kw):
        pass

    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass


class _FakeKillSwitch:
    def __init__(self):
        self.armed = False
        self.reason = ""
        self.persist_called = False

    def arm(self, reason):
        self.armed = True
        self.reason = reason

    async def persist(self):
        self.persist_called = True
        return True


class _FakeRiskManager:
    def __init__(self):
        self.kill_switch = _FakeKillSwitch()


class _FakeRiskManagerFlush:
    def __init__(self):
        self.kill_switch = _FakeKillSwitch()
        self.flushed = False

    async def flush_events(self):
        self.flushed = True


class _FakeLifecycle:
    def __init__(self):
        self.state = "INIT"
        self.entered_safe = None

    def enter_safe_mode(self, reason):
        self.state = "SAFE_MODE"
        self.entered_safe = reason
        return True


class _FakeGate:
    def __init__(self):
        self.open_calls = 0
        self.reduce_calls = 0

    def can_open_position(self):
        self.open_calls += 1
        return (False, "测试拦截")

    def can_reduce_position(self):
        self.reduce_calls += 1
        return (False, "测试拦截")


class _FakeSide:
    value = "BUY"


class _FakeSignal:
    side = _FakeSide()
    symbol = "SOLUSDT"


class _FakeMarketEngine:
    def __init__(self):
        self.stopped = False
        self.state = {}

    async def stop(self):
        self.stopped = True


class _FakeStrategyEngine:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def _coro_forever():
    while True:
        await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# 1. critical 任务异常 → 安全状态
# ---------------------------------------------------------------------------

class TestCriticalTaskFailure:
    async def test_critical_failure_arms_kill_switch_and_safe_mode(self):
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.logger = _FakeLogger()
        sys.risk_manager = _FakeRiskManager()
        sys.lifecycle = _FakeLifecycle()
        sys._pending_tasks = set()

        sys._handle_critical_task_failure("risk-loop", RuntimeError("boom"))

        # 立即(同步): 急停 arm + 生命周期 SAFE_MODE
        assert sys.risk_manager.kill_switch.armed is True
        assert "risk-loop" in sys.risk_manager.kill_switch.reason
        assert sys.lifecycle.state == "SAFE_MODE"
        assert sys.lifecycle.entered_safe == "critical 任务 risk-loop 异常退出"
        # fire-and-forget 持久化任务被追踪
        assert len(sys._pending_tasks) == 1

        # 让持久化任务完成, 避免 pending 警告
        task = next(iter(sys._pending_tasks))
        await task
        assert sys.risk_manager.kill_switch.persist_called is True

    async def test_critical_failure_guards_none_components(self):
        """组件未初始化时调用不抛(防御性)。"""
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.logger = _FakeLogger()
        sys.risk_manager = None
        sys.lifecycle = None
        sys._pending_tasks = set()
        sys._handle_critical_task_failure("risk-loop", RuntimeError("boom"))
        assert len(sys._pending_tasks) == 0


# ---------------------------------------------------------------------------
# 2/3. 停机后禁止 BUY(信号 + 核心仓加仓)
# ---------------------------------------------------------------------------

class TestShutdownBlocksBuy:
    async def test_on_signal_proceeds_when_running(self):
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys._shutting_down = False
        sys._running = True
        sys.logger = _FakeLogger()
        sys.trading_gate = _FakeGate()

        await sys._on_signal(_FakeSignal())
        assert sys.trading_gate.open_calls == 1  # 正常经过交易闸门(后被拦截)

    async def test_on_signal_blocked_when_shutting_down(self):
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys._shutting_down = True
        sys._running = False
        sys.logger = _FakeLogger()
        sys.trading_gate = _FakeGate()

        await sys._on_signal(_FakeSignal())
        assert sys.trading_gate.open_calls == 0  # 闸门都未触及

    async def test_core_add_blocked_when_shutting_down(self):
        from at55_portfolio.core_manager import CoreAction

        sys = object.__new__(run.AdaptiveTradingSystem)
        sys._shutting_down = True
        sys._running = False
        sys.logger = _FakeLogger()
        sys.trading_gate = _FakeGate()

        await sys._apply_core_action(
            "SOLUSDT", 100.0, {"action": CoreAction.ADD, "reason": "test"}
        )
        assert sys.trading_gate.open_calls == 0  # ADD(BUY)被停机标志拦截

    async def test_core_add_proceeds_when_running(self):
        from at55_portfolio.core_manager import CoreAction

        sys = object.__new__(run.AdaptiveTradingSystem)
        sys._shutting_down = False
        sys._running = True
        sys.logger = _FakeLogger()
        sys.trading_gate = _FakeGate()

        await sys._apply_core_action(
            "SOLUSDT", 100.0, {"action": CoreAction.ADD, "reason": "test"}
        )
        assert sys.trading_gate.open_calls == 1  # 运行中正常经过闸门


# ---------------------------------------------------------------------------
# 4/5. stop(): 停机标志 + 幂等 + 资源关闭 + 任务回收
# ---------------------------------------------------------------------------

class TestStop:
    async def test_stop_sets_flag_cancels_tasks_closes_resources(self, monkeypatch):
        sys = run.AdaptiveTradingSystem()
        sys._running = True
        sys.market_engine = _FakeMarketEngine()
        sys.strategy_engine = _FakeStrategyEngine()
        sys.sentiment_analyzer = None
        sys.risk_manager = _FakeRiskManagerFlush()
        sys.lifecycle = _FakeLifecycle()

        async def _fake_close_db():
            pass

        monkeypatch.setattr(run, "close_db", _fake_close_db)

        t = sys.supervisor.spawn(_coro_forever(), name="demo")
        await asyncio.sleep(0)

        await sys.stop()

        assert sys._shutting_down is True
        assert sys._running is False
        assert t.cancelled()
        assert sys.market_engine.stopped is True
        assert sys.strategy_engine.closed is True
        assert sys.risk_manager.flushed is True
        assert sys.supervisor.task_count() == 0

    async def test_stop_idempotent(self, monkeypatch):
        sys = run.AdaptiveTradingSystem()
        sys._running = True
        sys.market_engine = _FakeMarketEngine()
        sys.strategy_engine = _FakeStrategyEngine()
        sys.sentiment_analyzer = None
        sys.risk_manager = _FakeRiskManagerFlush()
        sys.lifecycle = _FakeLifecycle()

        async def _fake_close_db():
            pass

        monkeypatch.setattr(run, "close_db", _fake_close_db)

        await sys.stop()
        await sys.stop()  # 第二次 no-op, 不抛
        assert sys._shutting_down is True

    async def test_supervisor_rejects_spawn_after_stop(self, monkeypatch):
        sys = run.AdaptiveTradingSystem()
        sys._running = True
        sys.market_engine = _FakeMarketEngine()
        sys.strategy_engine = _FakeStrategyEngine()
        sys.sentiment_analyzer = None
        sys.risk_manager = _FakeRiskManagerFlush()
        sys.lifecycle = _FakeLifecycle()

        async def _fake_close_db():
            pass

        monkeypatch.setattr(run, "close_db", _fake_close_db)

        await sys.stop()
        import pytest

        coro = _coro_forever()
        with pytest.raises(RuntimeError):
            sys.supervisor.spawn(coro, name="late")
        coro.close()  # 未 consume 的协程, 显式关闭避免 never-awaited 警告
