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
- 取消 ≠ 失败: 手动 cancel 或 shutdown() 取消(含 critical 任务)只标 cancelled, 不触发
  critical 安全回调、不计数 failure(否则 graceful shutdown 会误触发急停)。
- 回调异常被吞: on_critical_failure 自身抛异常仅记日志, 不影响 failure_count 与事件循环。

**V13 变更: critical 任务增加有界自动重启**。此前「critical 任务异常退出 → 直接冻结,
不 re-spawn」是明确契约, 后果是**关键任务崩一次就要有人去重启进程** —— 这是
「必须有人值守」的头号原因。V13 改为:

    异常退出 → 指数退避重启(最多 max_restarts 次) → 仍失败才触发 on_critical_failure

重启**只对 critical 任务**做, 并且:
- 每次重启都记日志与计数(`restart_count`), 用户能在「今天发生了什么」里看到;
- 用尽重启额度后行为与旧版一致(冻结 + SAFE_MODE + 急停), **没有降低最终安全等级**;
- 重启任务名加后缀 `#rN`, 保证唯一名契约不被破坏(spawn 同名列仍 ValueError)。
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
        *,
        restart_factory: Optional[Callable[[str], Optional[Coroutine[Any, Any, Any]]]] = None,
        max_restarts: int = 3,
        restart_backoff: float = 5.0,
        on_critical_down: Optional[Callable[[str], None]] = None,
        on_critical_recovered: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._tasks: dict[str, TaskStatus] = {}
        self._shutting_down = False
        self._on_critical_failure = on_critical_failure
        self._failure_count = 0
        # V13: 有界自动重启。`restart_factory(任务名)` 返回**新的**协程(协程只能用一次,
        # 不能复用死掉的那个); 返回 None 表示这个任务不可重启。
        self._restart_factory = restart_factory
        self._max_restarts = max_restarts
        self._restart_backoff = restart_backoff
        self._restart_counts: dict[str, int] = {}
        self._restart_tasks: set[asyncio.Task] = set()
        # V13: 「关键任务当前是否缺席」。**这是重启带来的新问题**: 旧契约下 critical 任务
        # 一崩就冻结, 所以「冻结」本身就代表了「任务不在」。有了重启, 崩溃与重启成功之间
        # 有一段任务缺席的窗口 —— 那段时间风险监控/对账并没有在跑, 若闸门仍以为一切健康,
        # 就会**在监控缺失的情况下继续交易**。所以缺席状态必须独立跟踪, 与是否冻结无关。
        self._critical_down: set[str] = set()
        self._on_critical_down = on_critical_down
        self._on_critical_recovered = on_critical_recovered

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def restart_count(self) -> int:
        """累计重启次数(供事件流/健康报告展示「自动重启了 N 次」)。"""
        return sum(self._restart_counts.values())

    @property
    def critical_tasks_healthy(self) -> bool:
        """所有关键任务当前都在跑(与是否被冻结无关)。"""
        return not self._critical_down

    @property
    def critical_down(self) -> list[str]:
        return sorted(self._critical_down)

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
        if not st.critical:
            return
        base = self.base_name(name)
        # 先标记缺席: 无论接下来是重启还是冻结, **从这一刻起任务就是不在的**。
        self._mark_critical_down(base)
        if self._try_restart(name, st):
            return
        if self._on_critical_failure is not None:
            try:
                # 回调用**基线名**: 对调用方而言 `risk-loop#r2` 与 `risk-loop` 是同一个
                # 逻辑任务, 把后缀透出去只会让日志与冻结原因变得难读。
                self._on_critical_failure(base, exc)
            except Exception:
                logger.exception("critical 任务安全处置回调异常", name=name)

    def _mark_critical_down(self, base: str) -> None:
        """标记关键任务缺席并通知(仅首次缺席时通知, 避免重复刷)。"""
        first = base not in self._critical_down
        self._critical_down.add(base)
        if first and self._on_critical_down is not None:
            try:
                self._on_critical_down(base)
            except Exception:
                logger.exception("关键任务缺席回调异常", name=base)

    def _mark_critical_up(self, base: str) -> None:
        """标记关键任务已回来 —— 只有在**没有其他关键任务缺席**时才通知恢复。"""
        self._critical_down.discard(base)
        if self._on_critical_recovered is not None:
            try:
                self._on_critical_recovered(base)
            except Exception:
                logger.exception("关键任务恢复回调异常", name=base)

    @staticmethod
    def base_name(name: str) -> str:
        """剥掉重启后缀, 得到任务基线名(`risk-loop#r2` → `risk-loop`)。

        **额度必须按基线名计** —— 否则重启出来的任务名每次都变(`#r1#r1#r1…`),
        计数永远从 0 开始, 额度形同虚设, 崩溃任务会被无限拉起。
        """
        return name.split("#r", 1)[0]

    def _try_restart(self, name: str, st: TaskStatus) -> bool:
        """尝试有界重启 critical 任务。返回 True 表示已安排重启(不走冻结路径)。

        停机中 / 无工厂 / 额度用尽 → 返回 False, 由调用方走既有的冻结路径。
        """
        if self._shutting_down or self._restart_factory is None:
            return False
        base = self.base_name(name)
        used = self._restart_counts.get(base, 0)
        if used >= self._max_restarts:
            logger.error(
                "critical 任务重启额度已用尽, 转入冻结", name=base, restarts=used,
            )
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 没有事件循环(同步测试上下文) → 不重启
            return False

        self._restart_counts[base] = used + 1
        delay = self._restart_backoff * (2 ** used)
        task = loop.create_task(self._restart_later(base, delay, used + 1))
        self._restart_tasks.add(task)
        task.add_done_callback(self._restart_tasks.discard)
        return True

    async def _restart_later(self, base: str, delay: float, attempt: int) -> None:
        """退避后重启。**重启失败不再递归** —— 新任务若又崩, 会再次走 `_on_done`,
        由那里的额度判定收敛(不会无限递归, 因为按基线名计的额度单调递增)。"""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        if self._shutting_down:
            return
        made = self._restart_factory(base) if self._restart_factory else None
        if made is None:
            logger.error("critical 任务无法重启(无工厂产物)", name=base)
            return
        # 任务名唯一契约不变: 重启后的任务用 `#rN` 后缀, 原任务名保持已占用。
        new_name = f"{base}#r{attempt}"
        try:
            self.spawn(made, name=new_name, critical=True)
            # 新任务已就位。它若立刻又崩, `_on_done` 会再次把它标为缺席 ——
            # 中间只有个把事件循环 tick 的窗口, 且额度按基线名收敛, 不会失控。
            self._mark_critical_up(base)
            logger.info("critical 任务已自动重启", name=base, attempt=attempt,
                        delay=round(delay, 1))
        except Exception:
            logger.exception("critical 任务重启失败", name=base)

    def restart_status(self) -> dict[str, int]:
        """每个任务的重启次数(供健康报告)。"""
        return dict(self._restart_counts)

    def sync_gate(self, gate: Any) -> None:
        """把「关键任务是否缺席」同步到统一闸门。

        闸门的 `critical_tasks_healthy` 是 BUY 安全契约的一维, 也是**唯一**决定
        「监控缺失时能不能下单」的地方。让调用方在每轮循环里同步一次, 比在这里
        反向依赖闸门更简单, 也不会形成环。
        """
        if gate is None:
            return
        gate.critical_tasks_healthy = self.critical_tasks_healthy

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
        # V13: 先撤掉待执行的重启, 免得停机之后又被拉起一个任务
        for t in list(self._restart_tasks):
            t.cancel()
        self._restart_tasks.clear()
        for st in self._tasks.values():
            st.task.cancel()
        for st in self._tasks.values():
            try:
                await st.task
            except asyncio.CancelledError:
                pass
            except Exception:
                # V13 修复: 此前只捕获 CancelledError —— 若某个任务**已经因异常结束**
                # (例如 regime-loop 崩过), `await st.task` 会把那个异常**重新抛出**,
                # 于是 shutdown 中途夭折: 后面的任务既没被 await 也没被回收, 优雅停机变成
                # 半途而废。异常在 `_on_done` 里已经记过日志与计数, 这里不该再抛一次。
                logger.warning("停机时忽略已崩溃任务的异常", name=st.name)
        self._tasks.clear()
