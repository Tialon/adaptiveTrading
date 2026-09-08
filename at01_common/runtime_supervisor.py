"""
RuntimeSupervisor — 轻量 asyncio 后台任务监督器(V11.5 P0-2)

不微服务化、保持单进程。职责:
- 统一 spawn 后台任务并命名;
- 跟踪任务状态(运行/完成/取消/异常);
- 捕获任务异常(含 done 回调), 记日志;
- critical 任务异常退出 → 触发安全回调(由调用方注入「进入安全状态」处置);
- graceful shutdown: 幂等、取消所有任务、等待回收、清空注册表。

设计: 纯 asyncio, 不依赖交易语义; 便于独立单元测试。

第二轮审计结论(V11.6 P1-3, 钉为契约, 由 test_v163 验证):
- 无自动重启: critical 任务异常退出 → 触发安全回调(冻结, SAFE_MODE + 急停), 不 re-spawn;
  任务名唯一且永久占用 —— 已完成/崩溃任务的名字不可复用(spawn 同名列 ValueError)。
- 取消 ≠ 失败: 手动 cancel 或 shutdown() 取消(含 critical 任务)只标 cancelled, 不触发
  critical 安全回调、不计数 failure(否则 graceful shutdown 会误触发急停)。
- 回调异常被吞: on_critical_failure 自身抛异常仅记日志, 不影响 failure_count 与事件循环。
"""

import asyncio
import time
from dataclasses import dataclass
from collections.abc import Coroutine
from typing import Any, Callable, Optional

from at01_common.logger import get_logger

logger = get_logger("RuntimeSupervisor")


@dataclass
class TaskStatus:
    """单个后台任务的状态快照。"""

    name: str
    task: asyncio.Task
    critical: bool = False
    running: bool = True
    done: bool = False
    cancelled: bool = False
    exception: Optional[str] = None
    started_at: float = 0.0
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "critical": self.critical,
            "running": self.running,
            "done": self.done,
            "cancelled": self.cancelled,
            "exception": self.exception,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class RuntimeSupervisor:
    """后台 asyncio 任务监督器。

    - `spawn(coro, name, critical=False)`: 注册并启动一个命名任务, 返回 asyncio.Task。
    - `on_critical_failure(name, exc)`: critical 任务异常退出时同步回调(由调用方注入,
      例如 arm 急停 + 进入 SAFE_MODE)。
    - `shutdown()`: 幂等优雅停机 —— 取消所有任务、等待回收、清空注册表。
    - `status()`: 返回所有任务的状态快照(供 runtime health / 测试断言)。
    """

    def __init__(
        self,
        on_critical_failure: Optional[Callable[[str, BaseException], None]] = None,
    ) -> None:
        self._tasks: dict[str, TaskStatus] = {}
        self._shutting_down = False
        self._on_critical_failure = on_critical_failure
        self._failure_count = 0

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str, critical: bool = False) -> asyncio.Task:
        """创建并注册一个命名任务。停机后拒绝新任务; 任务名唯一。"""
        if name in self._tasks:
            raise ValueError(f"任务名重复: {name}")
        if self._shutting_down:
            raise RuntimeError("监督器已停机, 拒绝创建新任务")
        task = asyncio.create_task(coro, name=name)
        st = TaskStatus(
            name=name, task=task, critical=critical, started_at=time.time()
        )
        task.add_done_callback(lambda t: self._on_done(name, t))
        self._tasks[name] = st
        return task

    def _on_done(self, name: str, task: asyncio.Task) -> None:
        st = self._tasks.get(name)
        if st is None:
            return
        st.running = False
        st.done = True
        st.finished_at = time.time()
        if task.cancelled():
            st.cancelled = True
            return
        exc = task.exception()
        if exc is None:
            return
        st.exception = repr(exc)
        self._failure_count += 1
        logger.error(
            "后台任务异常退出", name=name, critical=st.critical, error=repr(exc)
        )
        if st.critical and self._on_critical_failure is not None:
            try:
                self._on_critical_failure(name, exc)
            except Exception:
                logger.exception("critical 任务安全处置回调异常", name=name)

    def status(self) -> list[dict]:
        return [st.to_dict() for st in self._tasks.values()]

    def task_count(self) -> int:
        return len(self._tasks)

    async def shutdown(self) -> None:
        """幂等优雅停机: 取消所有任务并等待回收。重复调用为 no-op。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("监督器停机: 取消所有后台任务", count=len(self._tasks))
        for st in self._tasks.values():
            st.task.cancel()
        for st in self._tasks.values():
            try:
                await st.task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
