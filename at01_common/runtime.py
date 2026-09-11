"""运行时入口(runtime): 构造系统并编排 initialize/start/stop 生命周期。

`run(system_cls)` 是原 `run.py main()` 的通用化: 接受系统类, 信号驱动优雅停机。
与 wiring 一样属编排粘合(信号/事件循环/生命周期), 不做交易决策, 不改交易语义。

V11.6 P2 从 run.py main() 抽出。
"""

from __future__ import annotations

import asyncio
import signal as signal_mod
from pathlib import Path

# 进程级停机请求通道: 由 `run()` 注册, 供 Web 层(管理页面)请求优雅停机。
# 不直接自送 SIGTERM —— Windows 上 `os.kill(pid, SIGTERM)` 会直接终止进程, 不跑处理器,
# 优雅停机(禁开仓 → 落库 → 关连接)会被跳过。
_stop_event: asyncio.Event | None = None
_stop_loop: asyncio.AbstractEventLoop | None = None


def request_shutdown() -> bool:
    """请求优雅停机(线程/协程安全)。返回是否成功投递。

    与 Ctrl+C / `docker stop`(SIGTERM)走**同一条**路径: 置位 stop_event → `run()` 退出
    asyncio.wait → `system.stop()` 优雅收尾。返回 False 表示当前进程没有在跑 `run()`
    (例如测试环境), 调用方据此如实回报而不是假装成功。
    """
    if _stop_event is None or _stop_loop is None:
        return False
    try:
        _stop_loop.call_soon_threadsafe(_stop_event.set)
    except RuntimeError:  # 事件循环已关闭
        return False
    return True


def shutdown_requested() -> bool:
    """停机是否已被请求(供轮询/展示)。"""
    return _stop_event is not None and _stop_event.is_set()


def in_container() -> bool:
    """当前进程是否运行在容器内(决定退出后能否被自动拉起)。"""
    return Path("/.dockerenv").exists()


async def run(system_cls) -> None:
    """构造 `system_cls()` 并编排 initialize/start/stop(信号驱动优雅停机)。"""
    global _stop_event, _stop_loop

    system = system_cls()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    _stop_event = stop_event
    _stop_loop = loop
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows
            signal_mod.signal(sig, lambda *_: stop_event.set())

    try:
        await system.initialize()
        server_task = asyncio.create_task(system.start())
        stop_task = asyncio.create_task(stop_event.wait())

        done, _ = await asyncio.wait(
            {server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except KeyboardInterrupt:
        pass
    finally:
        await system.stop()
        _stop_event = None
        _stop_loop = None


__all__ = ["run", "request_shutdown", "shutdown_requested", "in_container"]
