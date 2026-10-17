"""Integration regressions for the 2026-10-17 review (R02/R04-R10)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import pytest

from tests.integration.conftest import acquire, agent_session, browser_page

pytest.importorskip("aiohttp")

RID = lambda: f"55555555-5555-5555-5555-55555555{int(time.time() * 1000) % 10000:04d}"  # noqa: E731


async def test_r02_stop_releases_before_ws_cleanup(broker, base_url):
    """Active events socket must not delay the release barrier on shutdown."""
    agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
    _, grant = await acquire(base_url, agent, RID())
    assert broker.manager.mode.value == "agent"

    session = aiohttp.ClientSession()
    ws = await session.ws_connect(
        f"{base_url}/v1/events",
        headers={"Authorization": f"Bearer {agent['session_token']}"},
    )
    await ws.receive_json(timeout=3)  # full state event

    neutral_before = len(
        [w for w in broker._serial_transport.writes if len(w) >= 6 and w[3] in (2, 4, 5)]
    )
    started = time.monotonic()
    await asyncio.wait_for(broker.stop(), timeout=8.0)
    elapsed = time.monotonic() - started

    assert broker.manager.mode.value == "stopping"
    assert broker.manager.current_lease is None
    hid_after = [w for w in broker._serial_transport.writes if len(w) >= 6 and w[3] in (2, 4, 5)]
    assert len(hid_after) > neutral_before  # barrier neutralization was written
    assert elapsed < 8.0
    await ws.close()
    await session.close()


async def test_r05_video_ws_streams_and_cleans_up(broker, base_url):
    """Full auth -> >=2 binary frames -> disconnect leaves no viewer/task."""
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{base_url}/v1/ui/launch",
            json={"request_id": "5" * 36},
            headers={"Authorization": f"Bearer {broker.credentials.launcher}"},
        ) as response:
            url = (await response.json())["url"]
    bootstrap = url.split("#bootstrap=")[1]

    page = await browser_page(base_url, bootstrap)
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(
        f"{base_url}/v1/video",
        origin=base_url,
        headers={"Cookie": f"superuse_browser={page['cookie']}"},
    )
    await ws.send_json(
        {"type": "authenticate", "page_token": page["page_token"], "csrf": page["csrf"]}
    )
    frames = 0
    deadline = time.monotonic() + 10
    while frames < 2 and time.monotonic() < deadline:
        message = await asyncio.wait_for(ws.receive(), timeout=5)
        if message.type is aiohttp.WSMsgType.BINARY:
            view = int.from_bytes(message.data[:4], "big")
            header = json.loads(message.data[4 : 4 + view])
            assert header["source"] == "simulated"
            frames += 1
    assert frames >= 2, "video frames must flow after full authentication"

    await ws.close()
    await session.close()
    for _ in range(50):
        if not broker.ws._video_viewers:
            break
        await asyncio.sleep(0.1)
    assert not broker.ws._video_viewers
    for viewer in getattr(broker.ws, "_video_viewers", set()):
        assert viewer.task is None  # pragma: no cover - defensive


async def test_r04_non_simulated_capture_feeds_hub(tmp_path, monkeypatch):
    """A non-SimulatedCapture source (real-path shape) reaches FrameHub."""
    import superuse.app as app_module
    from superuse.app import Broker
    from superuse.config import load_config
    from superuse.device.capture import Frame

    class FakeRealCapture:
        """Same interface shape as CaptureWorker (thread + on_frame)."""

        status = "starting"
        fault_reason = None

        def __init__(self, *, on_frame=None, on_geometry=None, **kwargs):
            import threading

            self._on_frame = on_frame
            self._count = 0
            self._stop = threading.Event()
            self._thread = None

        def start(self):
            import threading

            def _produce():
                if self._stop.wait(0.2):
                    return
                for i in range(20):
                    if self._stop.is_set():
                        return
                    self._count += 1
                    frame = Frame(
                        data=bytes([i % 256]) * (32 * 24 * 3),
                        fmt="bgr",
                        width=32,
                        height=24,
                        sequence=self._count,
                        monotonic=time.monotonic(),
                        captured_at=Frame.now_iso(),
                    )
                    if self._on_frame:
                        self._on_frame(frame)
                    self.status = "ready"
                    if self._stop.wait(0.05):
                        return

            self._thread = threading.Thread(target=_produce, daemon=True)
            self._thread.start()

        def stop(self):
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=2)
                assert not self._thread.is_alive()
            self.status = "disconnected"

    monkeypatch.setattr(app_module, "CaptureWorker", FakeRealCapture)
    broker = None
    try:
        config = load_config(overrides={"device": {"mode": "real"}})
        broker = Broker(config, runtime_dir=tmp_path / "runtime", web_dir=None)
        # Real serial mode would need hardware; swap in the simulated serial
        # transport builder so only the CAPTURE path is fake-real here.
        broker._build_serial = lambda: (_FakeSerialTransport(), _FakeActor(broker))
        await broker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and broker.hub.latest is None:
            await asyncio.sleep(0.1)
        assert broker.hub.latest is not None, "real-path frames must reach the hub"
        assert broker.hub.latest.width == 32
    finally:
        if broker is not None:
            await broker.stop()


class _FakeSerialTransport:
    def open(self):
        pass

    def close(self):
        pass

    def write_report(self, data, timeout_s):
        return len(data)

    def read(self, timeout_s):
        return b""


class _FakeActor:
    """Minimal actor for the R04 assembly test (no hardware)."""

    def __init__(self, broker):
        self.status = "ready"

    def start(self, query_device_info=True):

        self._status_cb = None

    def submit(self, report):
        import asyncio

        from superuse.core.ports import WriteOutcome

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        future.set_result(WriteOutcome(len(b"X" * 10), None))
        return future

    async def fence(self, ticket, timeout_s):
        from superuse.core.ports import FenceResult

        return FenceResult(ok=True)

    def stop(self):
        pass


async def test_r06_tray_icon_is_loadable():
    from PIL import Image

    web_dir = Path(__file__).resolve().parents[2] / "web"
    icon = Image.open(web_dir / "icon.png")
    icon.load()
    assert icon.format == "PNG"


async def test_r07_tray_actions_do_not_block_the_loop(broker, base_url, monkeypatch):
    """Tray open/takeover/handback run as loop tasks; nothing deadlocks."""
    import argparse

    from superuse.serve import ServeApp
    from superuse.windows.tray import TrayApp

    args = argparse.Namespace(
        config=None, real=False, headless=True, runtime_dir=str(broker.runtime_dir)
    )
    app = ServeApp(args)
    app.broker = broker
    app.loop = asyncio.get_running_loop()

    tray = TrayApp(
        loop=app.loop,
        on_open_ui=app.open_ui,
        on_takeover_and_open=app.takeover_and_open,
        on_handback=app.handback,
        on_copy_diagnostics=app.copy_diagnostics,
        on_quit=app.quit,
        get_state_summary=app.state_summary,
    )

    # A concurrent loop task must keep progressing while tray actions run.
    ticks = []

    async def heartbeat_like():
        while len(ticks) < 20:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    watcher = asyncio.ensure_future(heartbeat_like())

    # takeover (browser open stubbed where serve.py references it)
    import superuse.serve as serve_module

    opened = []
    monkeypatch.setattr(serve_module, "open_browser", lambda url: opened.append(url))
    tray.takeover_clicked(None, None)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and broker.manager.mode.value != "human":
        await asyncio.sleep(0.05)
    assert broker.manager.mode.value == "human"

    tray.handback_clicked(None, None)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and broker.manager.mode.value != "idle":
        await asyncio.sleep(0.05)
    assert broker.manager.mode.value == "idle"

    tray.open_ui_clicked(None, None)
    await asyncio.sleep(0.5)
    assert opened, "open_ui should have fetched a URL (browser stubbed)"

    await watcher
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert max(gaps) < 1.0, f"loop stalled during tray actions: max gap {max(gaps):.2f}s"


async def test_r08_bridge_cancel_propagates_to_broker_job(broker, base_url):
    """Cancelling the bridge's wait stops the broker-side job (bounded)."""
    from superuse.mcp.broker_client import BrokerClient

    client = BrokerClient(runtime_dir=broker.runtime_dir, label="cancel-test")
    await client.start()
    try:
        await client.acquire(ttl_ms=15000)
        typing = asyncio.ensure_future(
            client.command({"kind": "type_text", "text": "x" * 300, "char_interval_ms": 50})
        )
        await asyncio.sleep(1.0)
        running = [r.job for r in broker.scheduler._jobs.values() if not r.job.terminal]
        assert running, "job should be running"
        job_id = running[0].job_id
        assert running[0].reports_written >= 1  # typing really started

        typing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await typing

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = await client.request("GET", f"/v1/jobs/{job_id}")
            if job.get("terminal"):
                break
            await asyncio.sleep(0.05)
        assert job["status"] == "cancelled", "broker job must stop, not run to completion"
    finally:
        await client.close()


async def test_r09_supervisor_recovers_and_reconnects(tmp_path):
    """Geometry fault -> recovery; supervisor auto-recovers. Serial fault ->
    identity-preserving reconnect with backoff."""
    from superuse.app import Broker
    from superuse.config import load_config

    broker = Broker(
        load_config(),
        runtime_dir=tmp_path / "runtime",
        web_dir=None,
        supervisor_tick_s=0.1,
    )
    await broker.start()
    try:
        await broker.wait_healthy(timeout_s=10)
        assert broker.manager.mode.value == "idle"

        # Geometry change parks control in recovery; the supervisor's
        # auto-recover returns it to idle once frames are fresh again.
        broker.manager.geometry_changed()
        for _ in range(100):
            await asyncio.sleep(0.1)
            if broker.manager.mode.value == "idle":
                break
        assert broker.manager.mode.value == "idle"

        # Serial transport dies (hot-unplug); the supervisor rebuilds the
        # actor from the same configured identity and recovery completes.
        old_actor = broker.actor
        old_actor._transport.closed_mode = True
        old_actor._transport.close()
        old_actor._set_status("disconnected")
        deadline = time.monotonic() + 20
        replaced = False
        while time.monotonic() < deadline:
            if broker.actor is not old_actor and broker.actor.status == "ready":
                replaced = True
                break
            await asyncio.sleep(0.1)
        assert replaced, "supervisor must rebuild the serial actor"
        await broker.wait_healthy(timeout_s=10)
        assert broker.manager.mode.value == "idle"
    finally:
        await broker.stop()


def test_r10_version_entry_needs_only_stdlib():
    """--version works with site-packages excluded (base dev environment)."""
    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from superuse.__main__ import main; sys.exit(main(['--version']))" % (root / "src")
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "superuse" in result.stdout
