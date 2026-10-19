"""Real-device verification of the mouse coordinate-mode work (2026-10-19).

Final protocol (hm11 matrix, live-screen evidence): the coordinate mode
gates report types directionally (native passes, cross-mode dropped; a zero
0x05 cannot release a button in absolute mode); switching is a RUNTIME
transaction - kind-5 write followed immediately by a mode-matched zero
report - and omitting that zero report wedges the mouse pipeline until a
device restart (hm09 covers the kept restart capability).

Host-side facts are asserted; visual conclusions are recorded as evidence
images under artifacts/hardware/mouse-mode/ for separate review (testing.md
§8: serial writes never substitute for visual confirmation).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

from superuse.app import Broker  # noqa: E402
from superuse.config import load_config  # noqa: E402
from superuse.device.discovery import list_serial_ports, list_video_devices  # noqa: E402

pytestmark = pytest.mark.hardware
EVIDENCE = Path(__file__).resolve().parents[2] / "artifacts" / "hardware" / "mouse-mode"


def require_desktop_confirmation():
    if os.environ.get("SUPERUSE_HARDWARE_DESKTOP_CONFIRMED") != "1":
        pytest.skip("inspect the preview first, then explicitly confirm a safe desktop")


async def wait_until(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.fixture
async def real_broker(tmp_path):
    if os.environ.get("SUPERUSE_ALLOW_HARDWARE_TESTS") != "1":
        pytest.skip("requires SUPERUSE_ALLOW_HARDWARE_TESTS=1 and session authorization")
    serial = [p for p in list_serial_ports() if (p.vid, p.pid) == (0x1209, 0x0001)]
    video = [v for v in list_video_devices("dshow") if (v.vid, v.pid) == (0x345F, 0x2131)]
    assert len(serial) == len(video) == 1, "requires uniquely selected SuperKVM serial/video"
    config = load_config(
        overrides={
            "device": {
                "mode": "real",
                "serial": {"serial_number": serial[0].serial_number},
                "video": {"device_path": video[0].device_path, "backend": "dshow"},
            }
        }
    )
    assert not config.simulated
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    broker = Broker(config, runtime_dir=tmp_path / "runtime", supervisor_tick_s=0.1)
    try:
        await broker.start()
        await broker.wait_healthy(timeout_s=20)
        yield broker
    finally:
        await broker.stop()


def _points() -> tuple[float, float, float, float]:
    """Image-relative fractions (x1,y1,x2,y2); override for the live desktop."""
    raw = os.environ.get("SUPERUSE_HW_POINTS", "0.55,0.45,0.78,0.62")
    values = [float(v) for v in raw.split(",")]
    assert len(values) == 4
    return tuple(values)  # type: ignore[return-value]


async def snapshot(broker, name: str) -> dict:
    import time

    metadata, jpeg = await broker.hub.snapshot(
        now=time.monotonic,
        freshness_s=broker.config.freshness_s,
        max_width=1280,
        wait_ms=2000,
    )
    (EVIDENCE / name).write_bytes(jpeg)
    return metadata


async def set_mode(broker, mode: int) -> dict:
    state = await broker.set_device_mouse_mode(mode)
    await wait_until(
        lambda: broker.manager.get_state().mouse_mode == ("absolute" if mode == 2 else "relative")
    )
    return state


async def test_hm00_wake_and_preview(real_broker):
    """Wake a sleeping target display with bounded relative jiggles, then
    capture a preview for desktop inspection (no clicks/keys)."""
    import numpy as np

    def frame_std(jpeg: bytes) -> float:
        import cv2

        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        return float(image.std()) if image is not None else 0.0

    broker = real_broker
    from superuse.core.models import MouseRelReport, Session, SessionKind
    from superuse.core.ports import AuthorizedReport

    agent = Session(session_id=str(uuid.uuid4()), kind=SessionKind.AGENT, label="hm00-wake")
    record = await broker.manager.acquire(agent)
    result = {"wake_jiggles": 0, "frame_std": None}
    try:
        import time

        for attempt in range(12):
            metadata, jpeg = await broker.hub.snapshot(
                now=time.monotonic,
                freshness_s=broker.config.freshness_s,
                max_width=1280,
                wait_ms=2000,
            )
            (EVIDENCE / f"hm00-wake-{attempt:02d}.jpg").write_bytes(jpeg)
            std = frame_std(jpeg)
            result["frame_std"] = std
            if std > 6.0:  # not a uniformly black frame
                result["woke"] = True
                break
            broker.actor._mouse_mode_raw = None  # mode may be either; jiggle is 0x05
            outcome = await broker.actor.submit(
                AuthorizedReport(
                    session_id=agent.session_id,
                    lease_id=record.lease.lease_id,
                    generation=record.lease.generation,
                    report=MouseRelReport(0, 40, 0, 0),
                )
            )
            assert outcome.ok
            result["wake_jiggles"] += 1
            await asyncio.sleep(2.5)
        else:
            result["woke"] = False
        (EVIDENCE / "hm00-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        await snapshot(broker, "hm00-preview.jpg")
    finally:
        await broker.manager.release_control(agent, record.lease)
    assert result.get("woke"), "display stayed black after bounded jiggles"


async def test_hm01_attach_reports_mode_and_diagnostics(real_broker):
    """Read-only: mode surfaces in State and /v1/status after attach."""
    broker = real_broker
    await wait_until(lambda: broker.manager.get_state().mouse_mode is not None, timeout=15)
    mode = broker.manager.get_state().mouse_mode
    device = broker.api.device_info_provider() or {}
    assert device.get("mouse_mode") == mode
    assert device.get("firmware")
    (EVIDENCE / "hm01-attach.json").write_text(
        json.dumps(
            {
                "state_device_mouse_mode": mode,
                "device_info": broker.actor.device_info,
                "status_device_block": device,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    await snapshot(broker, "hm01-preview.jpg")


async def test_hm03_mode_rediscovered_after_reconnect(real_broker):
    """A replaced actor re-queries; the mode must not stay stale or missing."""
    require_desktop_confirmation()
    from superuse.core.models import DeviceStatus, Mode

    broker = real_broker
    await wait_until(lambda: broker.manager.get_state().mouse_mode is not None, timeout=15)
    if broker.manager.get_state().mouse_mode != "absolute":
        await set_mode(broker, 2)
    broker._supervisor_task.cancel()
    await broker._supervisor_task
    supervisor = broker._supervisor
    old_actor = broker.actor
    old_actor._transport.close()
    await wait_until(lambda: broker.manager.mode is Mode.RECOVERY)
    await supervisor._maybe_reconnect_serial()
    await wait_until(
        lambda: broker.manager.get_state().serial_status is DeviceStatus.READY, timeout=15
    )
    await supervisor._maybe_recover()
    await wait_until(lambda: broker.manager.get_state().mouse_mode is not None, timeout=15)
    assert broker.manager.get_state().mouse_mode == "absolute"
    (EVIDENCE / "hm03-reconnect.json").write_text(
        json.dumps(
            {
                "mode_after_reconnect": broker.manager.get_state().mouse_mode,
                "actor_replaced": broker.actor is not old_actor,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


async def wake_display(broker, label: str, result: dict, attempts: int = 15) -> bool:
    """Wake a display that slept (device reboot cuts HDMI): alternate a
    relative jiggle, an ESC tap and an absolute reposition, then check for a
    live picture (black and the capture card's static bright pattern are
    both rejected)."""
    import time

    import cv2
    import numpy as np

    from superuse.core.models import (
        KeyboardReport,
        MouseAbsReport,
        MouseRelReport,
        Session,
        SessionKind,
    )
    from superuse.core.ports import AuthorizedReport

    agent = Session(session_id=str(uuid.uuid4()), kind=SessionKind.AGENT, label=f"{label}-wake")
    record = None

    async def send(report):
        nonlocal record
        if broker.manager.current_lease is None:
            record = await broker.manager.acquire(agent)
        broker.actor._mouse_mode_raw = None
        outcome = await broker.actor.submit(
            AuthorizedReport(
                session_id=agent.session_id,
                lease_id=record.lease.lease_id,
                generation=record.lease.generation,
                report=report,
            )
        )
        assert outcome.ok, outcome.error

    def live(jpeg: bytes) -> bool:
        gray = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
        return gray is not None and 15 < gray.mean() < 205 and gray.std() > 15

    for attempt in range(attempts):
        _, jpeg = await broker.hub.snapshot(
            now=time.monotonic,
            freshness_s=broker.config.freshness_s,
            max_width=1280,
            wait_ms=2000,
        )
        (EVIDENCE / f"{label}-wake-{attempt:02d}.jpg").write_bytes(jpeg)
        if live(jpeg):
            result[f"{label}_wake_attempts"] = attempt
            if record is not None:
                with contextlib.suppress(Exception):
                    await broker.manager.release_control(agent, record.lease)
            return True
        if attempt % 3 == 0:
            await send(MouseRelReport(0, 60, 0, 0))
        elif attempt % 3 == 1:
            await send(KeyboardReport(0, (0x29,)))
            await asyncio.sleep(0.15)
            await send(KeyboardReport(0, ()))
        else:
            await send(MouseAbsReport(0, 0x0600, 0x0600, 0))
        await asyncio.sleep(3.0)
    # Give the last input its own wake latency before concluding failure.
    await asyncio.sleep(3.0)
    _, jpeg = await broker.hub.snapshot(
        now=time.monotonic,
        freshness_s=broker.config.freshness_s,
        max_width=1280,
        wait_ms=2000,
    )
    if live(jpeg):
        result[f"{label}_wake_attempts"] = attempts
        if record is not None:
            with contextlib.suppress(Exception):
                await broker.manager.release_control(agent, record.lease)
        return True
    result[f"{label}_wake_attempts"] = attempts
    if record is not None:
        with contextlib.suppress(Exception):
            await broker.manager.release_control(agent, record.lease)
    return False


async def test_hm09_restart_capability(real_broker):
    """The replicated restart capability (kept for remote recovery):

    restart write -> our handle closes -> supervisor re-attaches the same
    identity -> the new actor's device-info reply lands (mode re-verified).
    Mouse liveness around the restart is recorded as evidence; the visual
    matrix itself lives in hm11 (routine switching needs no restart).
    """
    require_desktop_confirmation()
    import time

    import cv2
    import numpy as np

    from superuse.core.models import MouseRelReport, Session, SessionKind
    from superuse.core.ports import AuthorizedReport

    broker = real_broker
    await wait_until(lambda: broker.manager.get_state().mouse_mode is not None, timeout=15)
    agent = Session(session_id=str(uuid.uuid4()), kind=SessionKind.AGENT, label="hm09")

    async def grab(name: str) -> bytes:
        _, jpeg = await broker.hub.snapshot(
            now=time.monotonic,
            freshness_s=broker.config.freshness_s,
            max_width=1280,
            wait_ms=2000,
        )
        (EVIDENCE / name).write_bytes(jpeg)
        return jpeg

    def diff(a: bytes, b: bytes) -> float:
        ga = cv2.imdecode(np.frombuffer(a, np.uint8), cv2.IMREAD_GRAYSCALE)
        gb = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_GRAYSCALE)
        return float(np.abs(ga.astype(int) - gb.astype(int)).mean())

    async def mouse_drag_diff() -> float:
        """Relative press-drag on the desktop: a live mouse shows a large diff."""
        record = await broker.manager.acquire(agent)
        try:
            before = await grab("hm09-probe-before.jpg")
            broker.actor._mouse_mode_raw = None
            for report in (
                MouseRelReport(1, -300, -200, 0),
                MouseRelReport(1, 120, 90, 0),
                MouseRelReport(0, 0, 0, 0),
            ):
                outcome = await broker.actor.submit(
                    AuthorizedReport(
                        session_id=agent.session_id,
                        lease_id=record.lease.lease_id,
                        generation=record.lease.generation,
                        report=report,
                    )
                )
                assert outcome.ok, outcome.error
                await asyncio.sleep(0.06)
            await asyncio.sleep(0.4)
            after = await grab("hm09-probe-after.jpg")
            return round(diff(before, after), 3)
        finally:
            with contextlib.suppress(Exception):
                await broker.manager.release_control(agent, record.lease)

    result: dict = {}
    try:
        await wake_display(broker, "hm09-pre", result)
        result["mouse_diff_before_restart"] = await mouse_drag_diff()
        old_actor = broker.actor
        state = await broker.restart_device()
        result["restart_returned_ready"] = state["serial_status"] == "ready"
        result["actor_replaced"] = broker.actor is not old_actor
        await wait_until(lambda: broker.manager.get_state().mouse_mode is not None, timeout=30)
        result["mode_after_restart"] = broker.manager.get_state().mouse_mode
        # The reboot cuts HDMI and the display usually sleeps; wake before the
        # informational liveness probe (the probe depends on target power
        # settings, so it is recorded rather than asserted).
        result["woke_after_restart"] = await wake_display(broker, "hm09", result)
        result["mouse_diff_after_restart"] = await mouse_drag_diff()
        (EVIDENCE / "hm09-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        assert result["restart_returned_ready"], "restart did not return to ready"
        assert result["actor_replaced"], "restart did not replace the actor"
        assert result["mode_after_restart"] == "absolute", "mode not re-reported after restart"
    finally:
        with contextlib.suppress(Exception):
            if broker.manager.get_state().mouse_mode != "absolute":
                await set_mode(broker, 2)


async def test_hm11_webapp_sequence_runtime_switch(real_broker):
    """The definitive hardware matrix through the PRODUCT mode transaction
    (kind-5 write -> immediate mode-matched zero report -> re-query verify;
    the actor's 5s heartbeat runs throughout). Native drags must work in
    both modes; cross-mode probes and the zero-0x05 semantics are recorded.
    """
    require_desktop_confirmation()
    import time

    import cv2
    import numpy as np

    from superuse.core.models import (
        MouseAbsReport,
        MouseRelReport,
        Session,
        SessionKind,
    )
    from superuse.core.ports import AuthorizedReport
    from superuse.device.protocol import (
        build_device_info_query,
    )

    broker = real_broker
    await wait_until(lambda: broker.manager.get_state().mouse_mode is not None, timeout=30)
    agent = Session(session_id=str(uuid.uuid4()), kind=SessionKind.AGENT, label="hm11")
    result: dict = {"mode_at_start": broker.manager.get_state().mouse_mode}

    async def grab(name: str) -> bytes:
        _, jpeg = await broker.hub.snapshot(
            now=time.monotonic,
            freshness_s=broker.config.freshness_s,
            max_width=1280,
            wait_ms=2000,
        )
        (EVIDENCE / name).write_bytes(jpeg)
        return jpeg

    def diff(a: bytes, b: bytes) -> float:
        ga = cv2.imdecode(np.frombuffer(a, np.uint8), cv2.IMREAD_GRAYSCALE)
        gb = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_GRAYSCALE)
        return float(np.abs(ga.astype(int) - gb.astype(int)).mean())

    # 5s device-info heartbeat, exactly like the webapp (ext query as
    # keepalive on the same serial actor control path).
    stop_heartbeat = asyncio.Event()
    actor_refs = {"actor": broker.actor}

    async def heartbeat():
        while not stop_heartbeat.is_set():
            actor = actor_refs["actor"]
            if actor is not None and actor.status == "ready":
                actor._enqueue_control(("raw", build_device_info_query(), lambda outcome: None))
            try:
                await asyncio.wait_for(stop_heartbeat.wait(), timeout=5.0)
            except TimeoutError:
                pass

    async def send_raw(*reports):
        record = await broker.manager.acquire(agent)
        try:
            for report in reports:
                broker.actor._mouse_mode_raw = None  # probe bypasses our gate
                outcome = await broker.actor.submit(
                    AuthorizedReport(
                        session_id=agent.session_id,
                        lease_id=record.lease.lease_id,
                        generation=record.lease.generation,
                        report=report,
                    )
                )
                assert outcome.ok, outcome.error
                await asyncio.sleep(0.02)
        finally:
            with contextlib.suppress(Exception):
                await broker.manager.release_control(agent, record.lease)

    async def webapp_switch(mode: int) -> None:
        """The PRODUCT transaction - which is now the webapp sequence:
        kind-5 write -> immediate mode-matched zero report -> re-query verify."""
        await broker.set_device_mouse_mode(mode)

    async def drag_diff(tag: str, absolute: bool) -> float:
        before = await grab(f"hm11-{tag}-before.jpg")
        if absolute:
            await send_raw(
                MouseAbsReport(1, 0x04CC, 0x0399, 0),
                MouseAbsReport(1, 0x0A66, 0x08CC, 0),
                MouseAbsReport(0, 0x0A66, 0x08CC, 0),
            )
        else:
            await send_raw(
                MouseRelReport(1, -300, -200, 0),
                MouseRelReport(1, 120, 90, 0),
                MouseRelReport(0, 0, 0, 0),
            )
        await asyncio.sleep(0.4)
        return round(diff(before, await grab(f"hm11-{tag}-after.jpg")), 3)

    hb = asyncio.ensure_future(heartbeat())
    try:
        await wake_display(broker, "hm11-start", result)

        # --- Start from absolute (webapp sequence if we are in relative). ---
        if result["mode_at_start"] != "absolute":
            await webapp_switch(2)
            result["switch_to_abs_s"] = True
        result["abs_drag_mode2"] = await drag_diff("m2", absolute=True)

        # --- Runtime switch to relative, webapp sequence, NO restart. ------
        t0 = time.monotonic()
        await webapp_switch(1)
        result["switch_to_rel_s"] = round(time.monotonic() - t0, 1)
        result["rel_drag_mode1"] = await drag_diff("m1", absolute=False)
        before = await grab("hm11-m1-04-before.jpg")
        await send_raw(MouseAbsReport(0, 0x0200, 0x0200, 0))
        await asyncio.sleep(0.4)
        result["raw_0x04_mode1"] = round(diff(before, await grab("hm11-m1-04-after.jpg")), 3)

        # --- Runtime switch back to absolute. ------------------------------
        t0 = time.monotonic()
        await webapp_switch(2)
        result["switch_to_abs2_s"] = round(time.monotonic() - t0, 1)
        result["abs_drag_mode2_final"] = await drag_diff("m2f", absolute=True)
        before = await grab("hm11-m2-05-before.jpg")
        await send_raw(MouseRelReport(0, 120, 90, 0))
        await asyncio.sleep(0.4)
        result["raw_0x05_mode2"] = round(diff(before, await grab("hm11-m2-05-after.jpg")), 3)
        before = await grab("hm11-m2-zero-before.jpg")
        await send_raw(MouseRelReport(0, 0, 0, 0))
        await asyncio.sleep(0.4)
        result["zero_0x05_mode2"] = round(diff(before, await grab("hm11-m2-zero-after.jpg")), 3)
        # Zero-0x05 button lift while absolute.
        before = await grab("hm11-m2-lift-before.jpg")
        await send_raw(
            MouseAbsReport(1, 0x04CC, 0x0399, 0),
            MouseAbsReport(1, 0x0899, 0x0666, 0),
        )
        await asyncio.sleep(0.4)
        held = await grab("hm11-m2-lift-held.jpg")
        result["abs_press_drag"] = round(diff(before, held), 3)
        await send_raw(MouseRelReport(0, 0, 0, 0))
        await asyncio.sleep(0.4)
        result["zero_0x05_lift"] = round(diff(held, await grab("hm11-m2-lift-after.jpg")), 3)
        result["mode_left"] = broker.manager.get_state().mouse_mode
        (EVIDENCE / "hm11-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    finally:
        stop_heartbeat.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(hb, timeout=2)
        with contextlib.suppress(Exception):
            if broker.manager.get_state().mouse_mode != "absolute":
                await webapp_switch(2)
