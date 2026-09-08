"""V11.5 P1-2 运行时故障注入(增量)—— task crash / shutdown-during-order / WS reconnect。

在已有 `test_v124_fault_injection.py`(24 系统级场景)与 `test_v142_abnormal_never_buy.py`
(三层异常不 BUY)基础上, 补齐三处**运行时**故障的端到端不变量:

1. **critical 任务崩溃**: 真实 `AdaptiveTradingSystem.__init__` 装配的 `RuntimeSupervisor`
   (on_critical_failure 回调) → critical 任务异常退出 → `_handle_critical_task_failure`
   → arm 急停 + 进入 SAFE_MODE → `TradingGate.can_open_position()` 拒绝 BUY;
   且崩溃被 runtime health 快照的 `last_error` 捕获(V11.5 P1-1 接线)。
2. **下单中停机**: `_on_signal` / `_apply_core_action` 在 `_shutting_down` 后丢弃在途信号,
   不触达 `execution_engine.execute`(graceful shutdown 禁一切新仓)。
3. **WS 重连**: 连接断开(connection_ok=False)禁开仓 → 重连(connection_ok=True)恢复,
   收敛可交易(断连期间绝不 BUY)。

核心不变量(与全项目一致): 故障 → 绝不「异常 → 继续 BUY」→ 安全态 → 收敛 → 不变量。
"""

import asyncio

import pytest

import run
from at10_web import system_state as global_system_state
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.fund_circuit_breaker import FundCircuitBreaker
from at60_risk.risk_manager import RiskManager
from at60_risk.system_lifecycle import SystemLifecycle
from at60_risk.trading_gate import TradingGate


class _Log:
    """最小日志桩: 任意方法调用均 no-op(适配 structlog 的 .info/.warning/.error/.exception)。"""

    def __getattr__(self, name):
        return lambda *a, **k: None


def _trading_stack():
    """真实 RiskManager + SystemLifecycle + TradingGate, 推进到 TRADING。"""
    rm = RiskManager()
    lc = SystemLifecycle()
    lc.warm_up()
    lc.sync()
    lc.self_check()
    lc.ready()
    lc.start_trading()
    gate = TradingGate(rm, lc, FundCircuitBreaker())
    return rm, lc, gate


class _Exec:
    """记录 execute 调用次数的执行引擎桩。"""

    def __init__(self):
        self.executed = []

    async def execute(self, sig):
        self.executed.append(sig)
        return {"status": "FILLED", "fill_qty": 0.0}


# ---------------------------------------------------------------------------
# 1. critical 任务崩溃 -> SAFE_MODE -> 不 BUY
# ---------------------------------------------------------------------------


class TestCriticalTaskCrash:
    async def test_crash_enters_safe_mode_and_blocks_buy(self, monkeypatch):
        """真实 supervisor 接线: critical 任务异常退出 -> 急停 + SAFE_MODE -> 闸门禁 BUY。"""
        # AdaptiveTradingSystem.__init__ 不触碰 DB/WS(只装配 supervisor + settings)。
        sys = run.AdaptiveTradingSystem()
        rm, lc, gate = _trading_stack()
        sys.risk_manager = rm
        sys.lifecycle = lc
        sys.trading_gate = gate

        # 去掉持久化落库(测试无需 DB), 只保留内存态副作用。
        async def _noop_persist(self):
            return None

        monkeypatch.setattr(
            run.AdaptiveTradingSystem, "_persist_critical_kill_switch", _noop_persist
        )

        assert gate.can_open_position()[0]  # 崩溃前可开仓

        async def _boom():
            raise RuntimeError("boom")

        t = sys.supervisor.spawn(_boom(), name="risk-loop", critical=True)
        with pytest.raises(RuntimeError):
            await t
        await asyncio.sleep(0)  # 排空 done 回调(同步触发 _handle_critical_task_failure)

        # 安全态: 急停 + SAFE_MODE
        assert rm.kill_switch.is_armed
        assert lc.is_safe_mode
        assert sys.supervisor.failure_count == 1
        # 核心不变量: 崩溃后绝不 BUY
        assert not gate.can_open_position()[0]
        # V11.5 P1-1 接线: 崩溃被 runtime health 的 last_error 捕获
        assert global_system_state.last_error is not None
        assert "risk-loop" in global_system_state.last_error["source"]


# ---------------------------------------------------------------------------
# 2. 下单中停机: 在途信号被丢弃, 不触达 execute
# ---------------------------------------------------------------------------


class TestShutdownDuringOrder:
    async def test_signal_dropped_after_shutdown(self):
        """停机后, 新 BUY 信号在 _on_signal 首行即被丢弃, 不触达执行引擎。"""
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.logger = _Log()
        sys._shutting_down = True
        exec_ = _Exec()
        sys.execution_engine = exec_

        sig = Signal(
            symbol="SOLUSDT",
            strategy="trend",
            side=SignalSide.BUY,
            price=100.0,
            quantity=1.0,
        )
        await sys._on_signal(sig)
        assert exec_.executed == []

    async def test_core_add_dropped_after_shutdown(self):
        """停机后, 核心仓 ADD(加仓=BUY)同样被丢弃。"""
        from at55_portfolio.core_manager import CoreAction

        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.logger = _Log()
        sys._shutting_down = True
        exec_ = _Exec()
        sys.execution_engine = exec_

        decision = {"action": CoreAction.ADD, "reason": "t", "add_qty": 1.0}
        await sys._apply_core_action("SOLUSDT", 100.0, decision)
        assert exec_.executed == []


# ---------------------------------------------------------------------------
# 3. WS 重连: 断连禁开仓 -> 重连恢复(收敛)
# ---------------------------------------------------------------------------


class TestWsReconnect:
    def test_reconnect_restores_open_after_disconnect(self):
        rm, lc, gate = _trading_stack()
        assert gate.can_open_position()[0]  # 健康: 可开仓

        gate.connection_ok = False  # WS 断开
        assert not gate.can_open_position()[0], "断连期间仍可开仓, 违反不变量"

        gate.connection_ok = True  # WS 重连
        assert gate.can_open_position()[0], "重连后未恢复可交易"
