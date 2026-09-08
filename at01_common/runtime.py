"""运行时入口(runtime): 构造系统并编排 initialize/start/stop 生命周期。

`run(system_cls)` 是原 `run.py main()` 的通用化: 接受系统类, 信号驱动优雅停机。
与 wiring 一样属编排粘合(信号/事件循环/生命周期), 不做交易决策, 不改交易语义。

V11.6 P2 从 run.py main() 抽出。
"""

from __future__ import annotations

import asyncio
import signal as signal_mod


async def run(system_cls) -> None:
    """构造 `system_cls()` 并编排 initialize/start/stop(信号驱动优雅停机)。"""
    system = system_cls()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
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
