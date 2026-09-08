"""V11.6 P1-3 证明: RuntimeSupervisor 第二轮审计 —— 补齐契约边角(回调异常/取消/重启)。

与 test_v151_runtime_supervisor.py(第一轮单元测试)互补, 本轮聚焦三个此前未钉死的边角:

1. **回调异常被吞**: critical 任务崩溃 → `on_critical_failure` 回调自身抛异常时,
   监督器捕获并记日志, 不破坏事件循环、failure_count 仍正确计数;
2. **取消 ≠ 失败**: 手动 cancel / graceful shutdown 取消 critical 任务时, 只标
   cancelled, **不触发** critical 安全回调、不计数 failure(否则停机会被误判为崩溃);
3. **无自动重启**: 任务名唯一且永久占用 —— 任务完成/崩溃后同名 re-spawn 抛
   ValueError; 已完成任务仍登记在注册表(不自动移除), 由 shutdown 统一清空。

审计结论以 `at01_common/runtime_supervisor.py` 模块 docstring 钉死为契约。
"""

import asyncio

import pytest

from at01_common.runtime_supervisor import RuntimeSupervisor


async def _yield_once():
    await asyncio.sleep(0)


async def _coro_return():
    return 42


async def _coro_raise():
    raise RuntimeError("boom")


async def _coro_forever():
    while True:
        await asyncio.sleep(3600)


class TestCallbackException:
    async def test_critical_callback_exception_is_contained(self):
        """critical 崩溃 + 回调自身抛异常: 被吞, failure_count 仍计任务异常。"""
        def _boom_cb(name, exc):
            raise RuntimeError("callback boom")

        sup = RuntimeSupervisor(on_critical_failure=_boom_cb)
        t = sup.spawn(_coro_raise(), name="risk-loop", critical=True)
        with pytest.raises(RuntimeError, match="boom"):
            await t
        await _yield_once()

        st = sup.status()[0]
        assert st["exception"] is not None and "boom" in st["exception"]
        assert sup.failure_count == 1  # 任务异常计数不受回调异常影响
        # 不抛「回调异常」出来 —— 事件循环未被破坏(能继续推进即证明)


class TestCancellation:
    async def test_critical_cancellation_not_treated_as_failure(self):
        """取消 critical 任务(graceful shutdown 路径): 不触发安全回调、不计数失败。"""
        events = []
        sup = RuntimeSupervisor(on_critical_failure=lambda n, e: events.append(n))
        t = sup.spawn(_coro_forever(), name="risk-loop", critical=True)
        await _yield_once()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await _yield_once()

        st = sup.status()[0]
        assert st["cancelled"] is True
        assert st["done"] is True
        assert st["exception"] is None  # 取消不算异常
        assert sup.failure_count == 0
        assert events == []  # 取消不触发 critical 安全回调


class TestNoRestart:
    async def test_task_name_not_reusable_after_normal_completion(self):
        """任务正常完成后: 同名 re-spawn 抛 ValueError(无自动重启, 设计如此)。"""
        sup = RuntimeSupervisor()
        t = sup.spawn(_coro_return(), name="loop")
        await t
        await _yield_once()

        coro = _coro_return()
        with pytest.raises(ValueError):
            sup.spawn(coro, name="loop")
        coro.close()  # 未 consume 的协程显式关闭, 避免 never-awaited 警告
        assert sup.task_count() == 1  # 已完成任务仍登记(不自动移除)

    async def test_task_name_not_reusable_after_crash(self):
        """任务崩溃后: 同名 re-spawn 仍抛 ValueError(崩溃不自动复位/重启)。"""
        sup = RuntimeSupervisor()
        t = sup.spawn(_coro_raise(), name="risk-loop", critical=True)
        with pytest.raises(RuntimeError, match="boom"):
            await t
        await _yield_once()

        coro = _coro_return()
        with pytest.raises(ValueError):
            sup.spawn(coro, name="risk-loop")
        coro.close()
        assert sup.task_count() == 1

    async def test_shutdown_clears_registry_enabling_fresh_spawn(self):
        """shutdown 清空注册表后, 同名可重新 spawn(唯一的重置路径)。"""
        sup = RuntimeSupervisor()
        sup.spawn(_coro_return(), name="loop")
        await sup.shutdown()
        assert sup.task_count() == 0

        sup2 = RuntimeSupervisor()
        t = sup2.spawn(_coro_return(), name="loop")
        await t
        await _yield_once()
        assert sup2.task_count() == 1
