"""V11.3 P0-7 Memory/Task Leak 审计(asyncio.create_task 生命周期)。

钉住风险事件 fire-and-forget 任务的生命周期:
1. `_record_event_now` 创建的任务被 `_pending_tasks` 追踪(不再裸丢弃);
2. 任务完成后经 done 回调自动移出集合(无泄漏, 不随事件数线性增长);
3. `flush_events()` 在停机前等待在途事件落库;
4. `_record_event` 内部吞掉 DB 异常, 不产生「Task exception was never retrieved」。
"""

import asyncio


from at50_risk.risk_manager import RiskManager


def test_record_event_now_tracks_task():
    """在事件循环内调用 _record_event_now -> 任务进入 _pending_tasks(被追踪)。"""
    async def _run():
        rm = RiskManager()
        rm._record_event_now("risk_state", "测试事件")
        assert len(rm._pending_tasks) == 1
        task = next(iter(rm._pending_tasks))
        assert isinstance(task, asyncio.Task)
        await task
        # 完成后 done 回调自动移除 -> 不泄漏
        assert len(rm._pending_tasks) == 0

    asyncio.run(_run())


def test_record_event_now_no_running_loop_is_noop():
    """无运行事件循环(同步上下文)调用 -> 不抛、不泄漏。"""
    rm = RiskManager()
    # 无运行 loop 时 RuntimeError 被吞, 不产生任务
    rm._record_event_now("risk_state", "无循环")
    assert len(rm._pending_tasks) == 0


async def test_flush_events_awaits_pending(db_tables):
    """flush_events 等待在途事件落库, 返回后集合清空。"""
    rm = RiskManager()
    for i in range(20):
        rm._record_event_now("risk_state", f"事件{i}")
    assert len(rm._pending_tasks) == 20
    await rm.flush_events()
    assert len(rm._pending_tasks) == 0


async def test_many_events_no_leak(db_tables):
    """连续大量事件不泄漏: 集合大小始终有界(峰值后归零)。"""
    rm = RiskManager()
    peak = 0
    for i in range(200):
        rm._record_event_now("risk_state", f"事件{i}")
        peak = max(peak, len(rm._pending_tasks))
        if i % 50 == 0:
            await rm.flush_events()
    await rm.flush_events()
    assert len(rm._pending_tasks) == 0
    # 峰值远小于总量(事件被及时消费, 而非累积到 200)
    assert peak < 200


async def test_record_event_swallows_db_error(db_tables):
    """DB 异常被 _record_event 内部吞掉, 不抛到任务外(无 never-retrieved 警告)。"""
    from at01_common.database import reset_engine

    rm = RiskManager()
    # 制造落库失败: 指向一个必挂的 session(关闭引擎后建会话将失败)
    reset_engine()
    rm._record_event_now("risk_state", "会失败的事件")
    await rm.flush_events()  # 不应抛异常
    assert len(rm._pending_tasks) == 0
