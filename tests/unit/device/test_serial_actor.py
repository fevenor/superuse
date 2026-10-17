"""SerialActor behaviour on the simulated transport (D01/D03, threaded)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from superuse.core.models import KeyboardReport, MouseAbsReport
from superuse.core.ports import AuthorizedReport, FenceTicket, WriteOutcome
from superuse.device.protocol import build_device_info_query, build_ext_frame
from superuse.device.serial_actor import SerialActor
from superuse.device.simulated import SimulatedSerial

ROOT = Path(__file__).resolve().parents[3]
VECTORS = {
    v["name"]: bytes.fromhex(v["hex"])
    for v in json.loads((ROOT / "examples/protocol-vectors.json").read_text(encoding="utf-8"))
}


async def test_fence_after_failed_open_does_not_leave_executor_waiter():
    serial = SimulatedSerial()
    serial.closed_mode = True
    actor = SerialActor(lambda: serial, loop=asyncio.get_running_loop())
    with pytest.raises(Exception, match="removed"):
        actor.start()
    result = await asyncio.wait_for(actor.fence(FenceTicket(1, "failed-open"), 0.1), 0.5)
    assert not result.ok
    assert actor._queue.empty()
    actor.stop()


async def test_fence_timeout_does_not_leave_executor_waiter():
    import threading

    actor = SerialActor(SimulatedSerial, loop=asyncio.get_running_loop())
    stop = threading.Event()
    actor._writer = threading.Thread(target=lambda: stop.wait(2), daemon=True)
    actor._writer.start()  # alive, but deliberately never services the queue
    try:
        result = await actor.fence(FenceTicket(1, "blocked-writer"), 0.05)
        assert not result.ok and result.error == "TIMEOUT"
        # Executor shutdown must complete even when no fence response arrives.
        await asyncio.wait_for(asyncio.get_running_loop().shutdown_default_executor(), 0.5)
    finally:
        stop.set()
        actor._writer.join(timeout=1)


async def test_actor_writes_and_revokes_by_generation():
    serial = SimulatedSerial()
    loop = asyncio.get_running_loop()
    actor = SerialActor(lambda: serial, loop=loop)
    actor.start(query_device_info=False)
    try:
        future = actor.submit(
            AuthorizedReport(
                session_id="s1",
                lease_id="l1",
                generation=0,
                report=KeyboardReport(0x00, (0x04,)),
            )
        )
        outcome: WriteOutcome = await asyncio.wait_for(future, 2.0)
        assert outcome.ok and outcome.written == len(VECTORS["key_a_down"])
        assert serial.writes[-1] == VECTORS["key_a_down"]

        # A stale-generation report queued before the fence is revoked at
        # dequeue time and never reaches the wire.
        stale = actor.submit(
            AuthorizedReport(
                session_id="s1",
                lease_id="l1",
                generation=0,
                report=KeyboardReport(0x00, (0x05,)),
            )
        )
        fence = await actor.fence(FenceTicket(generation=1, transition_id="t1"), 2.0)
        assert fence.ok
        stale_outcome = await asyncio.wait_for(stale, 2.0)
        assert stale_outcome.error == "REVOKED"
        # The fence's two neutralization reports hit the wire in order.
        assert serial.writes[-2:] == [
            VECTORS["keyboard_release"],
            VECTORS["mouse_relative_zero_release"],
        ]
        assert all(w != VECTORS["key_a_down"].replace(b"\x04", b"\x05", 1) for w in serial.writes)
    finally:
        actor.stop()


async def test_actor_short_write_and_timeout_are_uncertain_faults():
    serial = SimulatedSerial()
    loop = asyncio.get_running_loop()
    actor = SerialActor(lambda: serial, loop=loop, write_timeout_s=0.2)
    actor.start(query_device_info=False)
    try:
        serial.write_mode = "short"
        outcome = await asyncio.wait_for(
            actor.submit(
                AuthorizedReport(
                    session_id="s",
                    lease_id="l",
                    generation=0,
                    report=MouseAbsReport(0, 100, 100, 0),
                )
            ),
            2.0,
        )
        assert outcome.error == "SHORT_WRITE"
        assert actor.status == "fault"
    finally:
        actor.stop()

    serial2 = SimulatedSerial()
    actor2 = SerialActor(lambda: serial2, loop=loop, write_timeout_s=0.15)
    actor2.start(query_device_info=False)
    try:
        serial2.write_mode = "timeout"
        serial2.block_writes()
        outcome2 = await asyncio.wait_for(
            actor2.submit(
                AuthorizedReport(
                    session_id="s",
                    lease_id="l",
                    generation=0,
                    report=KeyboardReport(0, (0x04,)),
                )
            ),
            2.0,
        )
        assert outcome2.error == "TIMEOUT"
        assert actor2.status == "fault"
        serial2.unblock_writes()
    finally:
        actor2.stop()


async def test_actor_fence_timeout_when_transport_blocks():
    serial = SimulatedSerial()
    loop = asyncio.get_running_loop()
    actor = SerialActor(lambda: serial, loop=loop, write_timeout_s=5.0)
    actor.start(query_device_info=False)
    serial.write_mode = "timeout"
    serial.block_writes()
    try:
        result = await actor.fence(FenceTicket(generation=3, transition_id="t"), 0.3)
        assert not result.ok and result.error == "TIMEOUT"
    finally:
        serial.unblock_writes()
        actor.stop()


async def test_actor_reports_closed_after_disconnect():
    serial = SimulatedSerial()
    loop = asyncio.get_running_loop()
    actor = SerialActor(lambda: serial, loop=loop)
    actor.start(query_device_info=False)
    serial.closed_mode = True
    serial.close()
    try:
        outcome = await asyncio.wait_for(
            actor.submit(
                AuthorizedReport(
                    session_id="s", lease_id="l", generation=0, report=KeyboardReport(0, ())
                )
            ),
            2.0,
        )
        assert outcome.error in ("CLOSED", "FAILED")
        assert actor.status in ("disconnected", "fault")
    finally:
        actor.stop()


async def test_actor_device_info_query_and_response():
    serial = SimulatedSerial()
    loop = asyncio.get_running_loop()
    actor = SerialActor(lambda: serial, loop=loop)
    actor.start(query_device_info=True)
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not serial.writes:
            await asyncio.sleep(0.01)
        assert serial.writes and serial.writes[0] == build_device_info_query()

        payload = bytearray(135)
        payload[16:18] = (0x94, 0x03)  # u16 916 -> firmware 0.9.16 (vendor scheme)
        payload[18:20] = (0x4C, 0x04)  # u16 1100 -> hardware 1.1.0
        serial.queue_response(build_ext_frame(1, 0x81, bytes(payload)))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and actor.device_info is None:
            await asyncio.sleep(0.01)
        assert actor.device_info is not None
        assert actor.device_info["firmware"] == "0.9.16"
        assert actor.device_info["hardware"] == "1.1.0"
    finally:
        actor.stop()


async def test_actor_input_queue_backpressure():
    serial = SimulatedSerial()
    loop = asyncio.get_running_loop()
    actor = SerialActor(lambda: serial, loop=loop, input_queue_limit=2, write_timeout_s=5.0)
    actor.start(query_device_info=False)
    serial.write_mode = "timeout"
    serial.block_writes()
    try:
        futures = [
            actor.submit(
                AuthorizedReport(
                    session_id="s", lease_id="l", generation=0, report=KeyboardReport(0, ())
                )
            )
            for _ in range(2)
        ]
        third = actor.submit(
            AuthorizedReport(
                session_id="s", lease_id="l", generation=0, report=KeyboardReport(0, ())
            )
        )
        assert (await asyncio.wait_for(third, 1.0)).error == "BACKPRESSURE"
        for f in futures:
            f.cancel()
    finally:
        serial.unblock_writes()
        actor.stop()
