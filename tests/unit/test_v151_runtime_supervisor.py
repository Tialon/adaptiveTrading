"""V11.5 P0-2 RuntimeSupervisor 单元测试(纯 asyncio, 无交易语义)。

覆盖:
1. spawn 创建命名任务并登记状态;
2. 任务正常退出 → 无异常、failure_count 不变;
3. 任务异常退出(非 critical)→ 捕获异常、failure_count+1、不触发安全回调;
4. critical 任务异常退出 → 触发安全回调;
5. graceful shutdown: 取消所有任务并回收;
6. shutdown 幂等(重复调用 no-op);
7. shutdown 后拒绝 spawn;
8. 手动 cancel 任务 → cancelled 状态;
9. 任务名重复 → 报错。
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


class TestSpawn:
    async def test_spawn_named_task_and_status(self):
        sup = RuntimeSupervisor()
        t = sup.spawn(_coro_return(), name="demo")
        assert isinstance(t, asyncio.Task)
        assert sup.task_count() == 1
        st = sup.status()[0]
        assert st["name"] == "demo"
        assert st["critical"] is False
        assert st["running"] is True
        await t
        await _yield_once()
        assert sup.failure_count == 0

    async def test_duplicate_name_rejected(self):
        sup = RuntimeSupervisor()
        sup.spawn(_coro_forever(), name="dup")
        coro = _coro_forever()
        with pytest.raises(ValueError):
            sup.spawn(coro, name="dup")
        coro.close()  # 未 consume 的协程, 显式关闭避免 never-awaited 警告
        await sup.shutdown()


class TestNormalExit:
    async def test_normal_exit_no_exception(self):
        sup = RuntimeSupervisor()
        t = sup.spawn(_coro_return(), name="ok")
        await t
        await _yield_once()
        st = sup.status()[0]
        assert st["done"] is True
        assert st["cancelled"] is False
        assert st["exception"] is None
        assert sup.failure_count == 0


class TestException:
    async def test_noncritical_exception_captured_no_callback(self):
        events = []
        sup = RuntimeSupervisor(on_critical_failure=lambda n, e: events.append(n))
        t = sup.spawn(_coro_raise(), name="boom")
        with pytest.raises(RuntimeError):
            await t
        await _yield_once()
        st = sup.status()[0]
        assert st["done"] is True
        assert st["exception"] is not None
        assert "boom" in st["exception"]
        assert sup.failure_count == 1
        assert events == []  # 非 critical 不触发安全回调

    async def test_critical_exception_triggers_callback(self):
        events = []
        sup = RuntimeSupervisor(
            on_critical_failure=lambda n, e: events.append((n, repr(e)))
        )
        t = sup.spawn(_coro_raise(), name="risk-loop", critical=True)
        with pytest.raises(RuntimeError):
            await t
        await _yield_once()
        assert len(events) == 1
        assert events[0][0] == "risk-loop"
        assert "boom" in events[0][1]
        assert sup.failure_count == 1


class TestShutdown:
    async def test_shutdown_cancels_all(self):
        sup = RuntimeSupervisor()
        t1 = sup.spawn(_coro_forever(), name="a")
        t2 = sup.spawn(_coro_forever(), name="b")
        await _yield_once()
        await sup.shutdown()
        assert t1.cancelled()
        assert t2.cancelled()
        assert sup.shutting_down is True
        assert sup.task_count() == 0  # 清空注册表

    async def test_shutdown_idempotent(self):
        sup = RuntimeSupervisor()
        t = sup.spawn(_coro_forever(), name="a")
        await _yield_once()
        await sup.shutdown()
        await sup.shutdown()  # 第二次 no-op, 不抛
        assert t.cancelled()

    async def test_spawn_rejected_after_shutdown(self):
        sup = RuntimeSupervisor()
        await sup.shutdown()
        coro = _coro_return()
        with pytest.raises(RuntimeError):
            sup.spawn(coro, name="late")
        coro.close()  # 未 consume 的协程, 显式关闭避免 never-awaited 警告


class TestCancellation:
    async def test_manual_cancel_marks_cancelled_not_failure(self):
        sup = RuntimeSupervisor()
        t = sup.spawn(_coro_forever(), name="a")
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
