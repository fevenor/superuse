from __future__ import annotations

import asyncio


async def stop_device(device) -> None:
    task = asyncio.create_task(asyncio.to_thread(device.stop))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
