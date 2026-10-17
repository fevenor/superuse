"""Explicitly gated SuperKVM checks. No hardware is opened by default pytest."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

from superuse.app import Broker  # noqa: E402
from superuse.config import load_config  # noqa: E402
from superuse.device.discovery import list_serial_ports, list_video_devices  # noqa: E402
from superuse.mcp.broker_client import BrokerClient  # noqa: E402

pytestmark = pytest.mark.hardware
EVIDENCE = Path(__file__).resolve().parents[2] / "artifacts" / "hardware"


@pytest.fixture
async def real_broker(tmp_path):
    if os.environ.get("SUPERUSE_ALLOW_HARDWARE_TESTS") != "1":
        pytest.skip("requires SUPERUSE_ALLOW_HARDWARE_TESTS=1 and session authorization")
    serial = [p for p in list_serial_ports() if (p.vid, p.pid) == (0x1209, 0x0001)]
    video = [v for v in list_video_devices("dshow") if (v.vid, v.pid) == (0x345F, 0x2131)]
    assert len(serial) == len(video) == 1, "requires uniquely selected SuperKVM serial/video"
    assert serial[0].serial_number and video[0].device_path
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
    (EVIDENCE / "devices.json").write_text(
        json.dumps({"serial": asdict(serial[0]), "video": asdict(video[0])}, indent=2),
        encoding="utf-8",
    )
    broker = Broker(config, runtime_dir=tmp_path / "runtime", supervisor_tick_s=0.1)
    try:
        await broker.start()
        await broker.wait_healthy(timeout_s=20)
        yield broker
    finally:
        await broker.stop()


async def test_real_preview(real_broker):
    """Capture and inspect before any target UI input (startup sends neutral reports)."""
    broker = real_broker
    client = BrokerClient(runtime_dir=broker.runtime_dir, label="hardware-preview")
    await client.start()
    try:
        snapshot = await client.snapshot()
        assert snapshot["metadata"]["source"] == "real"
        jpeg = base64.b64decode(snapshot["data_base64"])
        assert jpeg.startswith(b"\xff\xd8")
        (EVIDENCE / "preview.jpg").write_bytes(jpeg)
        first = broker.hub.latest
        started = time.monotonic()
        await asyncio.sleep(3)
        last = broker.hub.latest
        elapsed = time.monotonic() - started
        assert last.frame_id > first.frame_id
        result = {
            "source": "real",
            "native_width": last.width,
            "native_height": last.height,
            "observation_s": elapsed,
            "frames": last.frame_id - first.frame_id,
            "observed_fps_short_sample": (last.frame_id - first.frame_id) / elapsed,
            "frame_age_ms": (time.monotonic() - last.monotonic) * 1000,
            "device_info": broker.actor.device_info,
        }
        (EVIDENCE / "preview-result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
    finally:
        await client.close()


async def test_real_relative_pointer_probe(real_broker):
    """Bounded relative movement only: no clicks, typing, unlock or mode writes."""
    import uuid

    from superuse.core.models import MouseRelReport, Session, SessionKind
    from superuse.core.ports import AuthorizedReport

    broker = real_broker
    client = BrokerClient(runtime_dir=broker.runtime_dir, label="relative-probe-viewer")
    await client.start()
    owner = Session(session_id=str(uuid.uuid4()), kind=SessionKind.AGENT, label="relative-probe")
    record = await broker.manager.acquire(owner)
    try:
        await save_snapshot(client, "relative-before.jpg")
        for label, dx in (("relative-forward.jpg", 5), ("relative-return.jpg", -5)):
            for _ in range(20):
                broker.manager.authorize_input(owner, record.lease)
                outcome = await broker.actor.submit(
                    AuthorizedReport(
                        session_id=owner.session_id,
                        lease_id=record.lease.lease_id,
                        generation=record.lease.generation,
                        report=MouseRelReport(0, dx, 0, 0),
                    )
                )
                assert outcome.ok
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
            await save_snapshot(client, label)
        (EVIDENCE / "relative-probe-result.json").write_text(
            json.dumps(
                {"reports_written": 40, "relative_counts_each_way": 100, "visual_confirmed": False}
            ),
            encoding="utf-8",
        )
    finally:
        await broker.manager.release_control(owner, record.lease)
        await client.close()


async def test_real_relative_mcp_and_human_input(real_broker):
    """Relative tools + authenticated human input WS, on the locked target.

    Only small no-button moves and neutral reports; no clicks or unlock.
    Screenshots require visual review separately from host-side assertions.
    """
    import uuid

    import aiohttp
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    from mcp.types import ImageContent

    from tests.integration.conftest import browser_page
    from tests.integration.test_mcp import bridge_params, text_of

    broker = real_broker
    url = f"http://127.0.0.1:{broker.port}"
    async with stdio_client(bridge_params(broker, "hardware-relative-mcp")) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()

            async def tool(name, arguments=None):
                result = await session.call_tool(name, arguments or {})
                assert not result.is_error, text_of(result)
                return result

            async def capture(name):
                result = await tool("capture_screen")
                image = next(c for c in result.content if isinstance(c, ImageContent))
                (EVIDENCE / name).write_bytes(base64.b64decode(image.data))

            await capture("relative-mcp-before.jpg")
            await tool("acquire_control")
            moved = await tool("mouse_move_relative", {"dx": -80, "dy": 40})
            assert json.loads(text_of(moved))["completion"] == "written"
            await asyncio.sleep(0.4)
            await capture("relative-mcp-after.jpg")
            await tool("release_inputs")
            await tool("release_control")
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{url}/v1/ui/launch",
                    json={"request_id": str(uuid.uuid4())},
                    headers={"Authorization": f"Bearer {broker.credentials.launcher}"},
                ) as response:
                    assert response.status == 200
                    launch = await response.json()
                page = await browser_page(url, launch["url"].split("#bootstrap=")[1])
                headers = {
                    "Cookie": f"superuse_browser={page['cookie']}",
                    "X-CSRF-Token": page["csrf"],
                    "X-Superuse-Page": page["page_token"],
                }
                async with http.post(
                    f"{url}/v1/control/takeover",
                    headers=headers,
                    json={"request_id": str(uuid.uuid4())},
                ) as response:
                    assert response.status == 200
                    grant = await response.json()
                ws = await http.ws_connect(
                    f"{url}/v1/input", origin=url, headers={"Cookie": headers["Cookie"]}
                )
                await ws.send_json(
                    {"type": "authenticate", "page_token": page["page_token"], "csrf": page["csrf"]}
                )
                for seq in range(1, 21):
                    await ws.send_json(
                        {
                            "api_version": 1,
                            "sequence": seq,
                            "lease": grant["lease"],
                            "action": {
                                "kind": "mouse_relative_state",
                                "dx": -2,
                                "dy": 1,
                                "buttons": 0,
                            },
                        }
                    )
                    ack = await ws.receive_json(timeout=3)
                    assert ack["sequence"] == seq and ack["status"] == "written", ack
                await asyncio.sleep(0.3)
                await capture("relative-human-after.jpg")
                await ws.close()
                await wait_until(lambda: broker.manager.mode.value == "human_hold")
                blocked = await session.call_tool("acquire_control", {})
                assert blocked.is_error and "HUMAN_CONTROL" in text_of(blocked)
                async with http.post(
                    f"{url}/v1/control/handback",
                    headers=headers,
                    json={"request_id": str(uuid.uuid4())},
                ) as response:
                    assert response.status == 200
            await tool("acquire_control")
            await tool("mouse_move_relative", {"dx": 120, "dy": -60})
            await tool("release_inputs")
            await tool("release_control")
            await asyncio.sleep(0.3)
            await capture("relative-final.jpg")
    (EVIDENCE / "relative-api-result.json").write_text(
        json.dumps(
            {
                "official_mcp_relative_move": "written",
                "human_ws_reports_written": 20,
                "human_latch_preserved": True,
                "only_moves_and_releases": True,
                "manual_visual_review_required": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def require_desktop_confirmation():
    if os.environ.get("SUPERUSE_HARDWARE_DESKTOP_CONFIRMED") != "1":
        pytest.skip("inspect preview first, then explicitly confirm an empty desktop")


async def wait_until(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def save_snapshot(client, name):
    snapshot = await client.snapshot()
    (EVIDENCE / name).write_bytes(base64.b64decode(snapshot["data_base64"]))
    return snapshot["metadata"]


async def test_real_mcp_pointer_context(real_broker):
    """Official MCP stdio -> desktop right-click -> readable new screenshot."""
    require_desktop_confirmation()
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import ImageContent, TextContent

    broker = real_broker
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "superuse", "mcp", "--runtime-dir", str(broker.runtime_dir)],
        env=dict(os.environ),
    )
    async with stdio_client(params) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            snapshot = await session.call_tool("capture_screen", {"max_width": 1280})
            assert not snapshot.is_error
            meta = json.loads(
                "".join(c.text for c in snapshot.content if isinstance(c, TextContent))
            )
            assert meta["source"] == "real"
            grant = await session.call_tool("acquire_control", {})
            assert not grant.is_error
            click = await session.call_tool(
                "mouse_click", {"basis_id": meta["basis_id"], "x": 500, "y": 280, "button": "right"}
            )
            assert not click.is_error
            await asyncio.sleep(0.4)
            after = await session.call_tool("capture_screen", {"after_frame_id": meta["frame_id"]})
            assert not after.is_error
            image = next(c for c in after.content if isinstance(c, ImageContent))
            (EVIDENCE / "mcp-context-menu.jpg").write_bytes(base64.b64decode(image.data))
            release = await session.call_tool("release_control", {})
            assert not release.is_error
    (EVIDENCE / "mcp-result.json").write_text(
        json.dumps({"official_stdio": True, "real_snapshot": True, "right_click_written": True}),
        encoding="utf-8",
    )


async def test_real_control_cancel_and_reopen(real_broker, monkeypatch):
    """Real handles, HTTP takeover, response cancellation, reopen and shared video.

    Failure injection closes this broker's own handles; it does not emulate a
    physical USB unplug, nor disable the Windows device or modify its firmware.
    """
    require_desktop_confirmation()
    import uuid

    import aiohttp

    from superuse.core.errors import ControlError, ErrorCode
    from superuse.core.models import DeviceStatus, Mode
    from superuse.mcp.broker_client import BridgeError
    from tests.integration.conftest import browser_page

    broker = real_broker
    result = {}
    base_url = f"http://127.0.0.1:{broker.port}"
    broker._supervisor_task.cancel()
    await broker._supervisor_task
    supervisor = broker._supervisor
    client = BrokerClient(runtime_dir=broker.runtime_dir, label="hardware-regression")
    await client.start()
    gate = asyncio.Event()
    http = aiohttp.ClientSession()
    try:
        await client.acquire()
        # Close the context menu left by the preceding visual test.
        assert (await client.command({"kind": "press_key", "code": "Escape"}))[
            "status"
        ] == "written"
        async with http.post(
            f"{base_url}/v1/ui/launch",
            json={"request_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {broker.credentials.launcher}"},
        ) as response:
            launch = await response.json()
            assert response.status == 200
        page = await browser_page(base_url, launch["url"].split("#bootstrap=")[1])
        headers = {
            "Cookie": f"superuse_browser={page['cookie']}",
            "X-CSRF-Token": page["csrf"],
            "X-Superuse-Page": page["page_token"],
        }

        # Keep a human video viewer alive while the agent sends input.
        ws = await http.ws_connect(
            f"{base_url}/v1/video",
            origin=base_url,
            headers={"Cookie": headers["Cookie"]},
        )
        await ws.send_json(
            {"type": "authenticate", "page_token": page["page_token"], "csrf": page["csrf"]}
        )
        for _ in range(2):
            assert (await ws.receive(timeout=3)).type is aiohttp.WSMsgType.BINARY
        result["shared_human_video"] = True

        typing = asyncio.create_task(
            client.command({"kind": "press_key", "code": "Escape", "hold_ms": 1000})
        )
        await wait_until(
            lambda: any(
                not r.job.terminal and r.job.reports_written
                for r in broker.scheduler._jobs.values()
            )
        )
        started = time.monotonic()
        async with http.post(
            f"{base_url}/v1/control/takeover",
            headers=headers,
            json={"request_id": str(uuid.uuid4())},
        ) as response:
            human = await response.json()
            assert response.status == 200, human
        result["takeover_ms"] = (time.monotonic() - started) * 1000
        assert (await typing)["status"] == "cancelled"
        with pytest.raises(BridgeError) as revoked:
            await client.command({"kind": "release_inputs"})
        assert revoked.value.code == "CONTROL_REVOKED"
        result["old_agent_rejected"] = revoked.value.code

        # Left-button down on empty desktop -> 0x05 zero release -> move.
        meta = await save_snapshot(client, "before-release.jpg")
        actions = [
            {"kind": "mouse_state", "basis_id": meta["basis_id"], "x": 350, "y": 250, "buttons": 1},
            {"kind": "release_inputs"},
            {"kind": "mouse_move", "basis_id": meta["basis_id"], "x": 550, "y": 350},
        ]
        for action in actions:
            async with http.post(
                f"{base_url}/v1/commands",
                headers=headers,
                json={
                    "api_version": 1,
                    "request_id": str(uuid.uuid4()),
                    "lease": human["lease"],
                    "action": action,
                },
            ) as response:
                body = await response.json()
                assert response.status == 202, body
            job = broker.scheduler._jobs[body["job_id"]].job
            await wait_until(lambda job=job: job.terminal)
            assert job.status.value == "written"
        await asyncio.sleep(0.3)
        await save_snapshot(client, "after-zero-release-move.jpg")
        result["zero_relative_release_written"] = True
        async with http.post(
            f"{base_url}/v1/control/handback",
            headers=headers,
            json={"request_id": str(uuid.uuid4())},
        ) as response:
            assert response.status == 200
        await ws.close()

        # Accepted job, deliberately delayed response, then host cancellation.
        await client.acquire()
        accepted = asyncio.Event()
        original_submit = broker.scheduler.submit_command

        async def hold_response(*args, **kwargs):
            job = await original_submit(*args, **kwargs)
            accepted.set()
            await gate.wait()
            return job

        monkeypatch.setattr(broker.scheduler, "submit_command", hold_response)
        cancelled = asyncio.create_task(
            client.command({"kind": "press_key", "code": "Escape", "hold_ms": 1000})
        )
        await asyncio.wait_for(accepted.wait(), timeout=3)
        job = next(r.job for r in broker.scheduler._jobs.values() if not r.job.terminal)
        await wait_until(lambda: job.reports_written >= 1)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await wait_until(lambda: job.terminal)
        assert job.status.value == "cancelled"
        assert broker.manager.current_lease is None
        assert client.lease is None
        result["cancel_before_response"] = job.status.value
        gate.set()
        monkeypatch.setattr(broker.scheduler, "submit_command", original_submit)

        # Close and reopen the actual COM handle while an agent owns a lease.
        await client.acquire()
        generation = broker.manager.generation
        old_actor = broker.actor
        old_actor._transport.close()
        await wait_until(lambda: broker.manager.mode is Mode.RECOVERY)
        assert broker.manager.current_lease is None
        await supervisor._maybe_reconnect_serial()
        await wait_until(lambda: broker.manager.get_state().serial_status is DeviceStatus.READY)
        await supervisor._maybe_recover()
        assert broker.manager.mode is Mode.IDLE
        assert broker.manager.generation > generation
        assert broker.actor is not old_actor
        assert broker.actor._generation == broker.manager.generation
        with pytest.raises(BridgeError):
            await client.command({"kind": "release_inputs"})
        result["serial_handle_reopen"] = "recovered_old_lease_rejected"

        # Stop the actual capture; first discovery attempt reports absent,
        # then the normal builder re-enumerates and opens the same identity.
        await client.acquire()
        old_capture = broker.capture
        old_generation = broker.hub.capture_generation
        old_capture.status, old_capture.fault_reason = "fault", "test_reopen"
        original_build = broker._build_capture
        builds = 0

        def temporarily_absent():
            nonlocal builds
            builds += 1
            if builds == 1:
                raise ControlError(ErrorCode.DEVICE_UNAVAILABLE, "injected discovery absence")
            return original_build()

        monkeypatch.setattr(broker, "_build_capture", temporarily_absent)
        await supervisor._maybe_restart_capture()
        assert old_capture.status == "disconnected"
        assert broker.manager.current_lease is None
        await asyncio.sleep(1.05)
        await supervisor._maybe_restart_capture()
        assert builds == 1 and supervisor._capture_reconnect_pending
        await asyncio.sleep(2.05)
        await supervisor._maybe_restart_capture()
        await wait_until(lambda: broker.hub.latest is not None)
        await supervisor._maybe_recover()
        assert builds == 2
        assert broker.capture is not old_capture
        assert broker.hub.capture_generation > old_generation
        assert broker.manager.mode is Mode.IDLE
        result["capture_handle_reopen_after_absence"] = "recovered"
        await save_snapshot(client, "after-reopen.jpg")
        (EVIDENCE / "lifecycle-result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
    finally:
        gate.set()
        await http.close()
        await client.close()


async def test_real_input_diagnostic(real_broker, monkeypatch):
    """Record bounded device diagnostics and screenshots, without claiming UI success."""
    require_desktop_confirmation()
    import re

    from superuse.device.protocol import ExtFrameParser

    broker = real_broker
    chunks = []
    original_read = broker._serial_transport.read

    def read(timeout_s):
        data = original_read(timeout_s)
        if data and len(chunks) < 64:
            chunks.append(data[:8192])
        return data

    monkeypatch.setattr(broker._serial_transport, "read", read)
    client = BrokerClient(runtime_dir=broker.runtime_dir, label="hardware-input-diagnostic")
    await client.start()
    try:
        meta = await save_snapshot(client, "diagnostic-before.jpg")
        await client.acquire()
        await client.command(
            {"kind": "mouse_move", "basis_id": meta["basis_id"], "x": 800, "y": 400}
        )
        await asyncio.sleep(1)
        await save_snapshot(client, "diagnostic-move.jpg")
        await client.command(
            {"kind": "press_key", "code": "F10", "modifiers": ["left_shift"], "hold_ms": 100}
        )
        await asyncio.sleep(1)
        await save_snapshot(client, "diagnostic-menu.jpg")
        await client.command({"kind": "press_key", "code": "Escape"})
        # A Super tap commonly opens a desktop launcher; inspect the saved
        # image rather than assuming Escape/Shift+F10 had a visible effect.
        import uuid

        from superuse.core.models import (
            CommandEnvelope,
            KeyboardState,
            PressKey,
            Session,
            SessionKind,
        )

        await client.release()
        human = Session(
            session_id=str(uuid.uuid4()), kind=SessionKind.HUMAN, label="key-diagnostic"
        )
        lease = await broker.manager.takeover(human)

        async def human_action(action):
            job = await broker.scheduler.submit_command(
                human,
                CommandEnvelope(request_id=str(uuid.uuid4()), lease=lease.lease, action=action),
            )
            await wait_until(lambda: job.terminal)
            assert job.status.value == "written"

        await human_action(KeyboardState(modifiers=("left_gui",)))
        await asyncio.sleep(0.1)
        await human_action(KeyboardState())
        await asyncio.sleep(0.7)
        await save_snapshot(client, "diagnostic-super-menu.jpg")
        await human_action(PressKey(code="Escape"))
        await broker.manager.handback(human)
        parser = ExtFrameParser()
        messages = []
        for chunk in chunks:
            for frame in parser.feed(chunk):
                if frame.channel == 5:
                    messages.extend(
                        s.decode("ascii") for s in re.findall(rb"[\x20-\x7e]{6,}", frame.payload)
                    )
        (EVIDENCE / "input-diagnostic.json").write_text(
            json.dumps({"device_messages": messages, "rx_chunks": len(chunks)}, indent=2),
            encoding="utf-8",
        )
    finally:
        await client.close()
