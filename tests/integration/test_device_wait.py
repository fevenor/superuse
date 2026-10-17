"""No-device startup, late attach and manual rescan (task 2026-10-17 T2).

Real-mode configs with no matching hardware must leave the broker serving
status (tray/UI/stdio stay alive); the supervisor attaches devices when they
appear and the same ladder covers first plug-in and reconnects.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("aiohttp")

import superuse.app as app_module  # noqa: E402
from superuse.app import Broker  # noqa: E402
from superuse.config import load_config  # noqa: E402
from superuse.core.errors import ControlError, ErrorCode  # noqa: E402
from superuse.core.models import DeviceStatus  # noqa: E402
from superuse.device.discovery import SerialPortInfo, VideoDeviceInfo  # noqa: E402


class FakeSerialTransport:
    def __init__(self, device: str, *, baud: int = 0, read_timeout_s: float = 0.0) -> None:
        self.device = device

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def write_report(self, data: bytes, timeout_s: float) -> int:
        return len(data)

    def read(self, timeout_s: float) -> bytes:
        return b""


class FakeCapture:
    """Frame-producing stand-in with CaptureWorker's observable surface."""

    def __init__(self, **kwargs):
        self.on_frame = kwargs.get("on_frame")
        self.status = "starting"
        self.fault_reason = None
        import threading

        self._stop = threading.Event()
        self._thread = None

    def start(self):
        import threading

        def _produce():
            from superuse.device.capture import Frame

            self.status = "ready"
            n = 0
            while not self._stop.wait(0.05):
                n += 1
                frame = Frame(
                    data=bytes([n % 256]) * (32 * 24 * 3),
                    fmt="bgr",
                    width=32,
                    height=24,
                    sequence=n,
                    monotonic=time.monotonic(),
                    captured_at=Frame.now_iso(),
                )
                if self.on_frame:
                    self.on_frame(frame)

        self._thread = threading.Thread(target=_produce, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.status = "disconnected"


def real_config():
    return load_config(overrides={"device": {"mode": "real"}})


def make_port():
    return SerialPortInfo(
        device="COMFAKE", vid=0x1209, pid=0x0001, serial_number="FAKE-SN", description="fake"
    )


def make_camera():
    return VideoDeviceInfo(
        name="SK USB3 Video",
        device_path="fake-path",
        vid=0x345F,
        pid=0x2131,
        backend="dshow",
        index=7,
    )


async def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.fixture
async def waiting_broker(tmp_path, monkeypatch):
    """Real-mode broker started with no hardware present."""
    holder = {"ports": [], "cameras": []}
    monkeypatch.setattr(app_module, "list_serial_ports", lambda: list(holder["ports"]))
    monkeypatch.setattr(app_module, "list_video_devices", lambda backend: list(holder["cameras"]))
    monkeypatch.setattr(app_module, "RealSerialTransport", FakeSerialTransport)
    monkeypatch.setattr(app_module, "CaptureWorker", FakeCapture)
    broker = Broker(real_config(), runtime_dir=tmp_path / "runtime", web_dir=None)
    broker._supervisor_tick_holder = holder  # tests flip holder contents
    await broker.start()
    try:
        yield broker, holder
    finally:
        await broker.stop()


async def test_no_device_startup_keeps_services_alive(waiting_broker):
    broker, _ = waiting_broker
    assert not broker.initial_device_ok
    assert broker.actor is None
    assert broker.capture is None

    state = broker.manager.get_state()
    assert state.serial_status is DeviceStatus.DISCONNECTED
    assert state.video_status is DeviceStatus.DISCONNECTED
    wire = state.to_wire()
    assert "DEVICE_UNAVAILABLE" in wire["serial_detail"]
    assert "DEVICE_UNAVAILABLE" in wire["video_detail"]

    # Services are observable and answers are structured, never fake success.
    import aiohttp

    async with aiohttp.ClientSession() as http:
        async with http.get(f"http://127.0.0.1:{broker.port}/healthz") as response:
            assert response.status == 200
    with pytest.raises(ControlError) as raised:
        await broker.hub.snapshot(
            now=time.monotonic,
            freshness_s=broker.config.freshness_s,
            max_width=640,
            after_frame_id=None,
            wait_ms=0,
        )
    assert raised.value.code in (
        ErrorCode.VIDEO_UNAVAILABLE,
        ErrorCode.VIDEO_STALE,
        ErrorCode.FRAME_TIMEOUT,
    )


async def test_late_attach_recovers_control_ready(waiting_broker, monkeypatch):
    broker, holder = waiting_broker
    monkeypatch.setattr(broker._supervisor, "BACKOFF_S", (0,))
    # Devices appear after startup.
    holder["ports"].append(make_port())
    holder["cameras"].append(make_camera())
    assert await until(
        lambda: broker.manager.get_state().serial_status is DeviceStatus.READY, timeout=10
    )
    assert await until(lambda: broker.hub.latest is not None, timeout=10)
    assert await until(lambda: broker.manager.mode.value in ("idle", "human_hold"), timeout=10)
    # Details cleared once healthy.
    state = broker.manager.get_state()
    assert state.serial_detail is None
    assert state.video_detail is None
    assert broker.initial_device_ok is False  # startup fact stays recorded


async def test_partial_devices_are_distinguishable(waiting_broker, monkeypatch):
    broker, holder = waiting_broker
    monkeypatch.setattr(broker._supervisor, "BACKOFF_S", (0,))
    holder["ports"].append(make_port())  # serial only
    assert await until(
        lambda: broker.manager.get_state().serial_status is DeviceStatus.READY, timeout=10
    )
    state = broker.manager.get_state()
    assert state.video_status is DeviceStatus.DISCONNECTED
    assert "DEVICE_UNAVAILABLE" in state.to_wire()["video_detail"]


async def test_ambiguous_identity_is_reported_not_guessed(waiting_broker, monkeypatch):
    broker, holder = waiting_broker
    monkeypatch.setattr(broker._supervisor, "BACKOFF_S", (0,))
    holder["ports"].extend([make_port(), make_port()])
    holder["cameras"].append(make_camera())
    assert await until(lambda: broker.hub.latest is not None, timeout=10)
    state = broker.manager.get_state()
    assert state.serial_status is DeviceStatus.DISCONNECTED
    assert "DEVICE_AMBIGUOUS" in state.to_wire()["serial_detail"]
    # Never adopted one of the candidates.
    assert broker.actor is None


async def test_open_failure_lands_in_fault_with_detail(waiting_broker, monkeypatch):
    broker, holder = waiting_broker

    class BusyTransport(FakeSerialTransport):
        def open(self):
            raise PermissionError(13, "Access denied")

    monkeypatch.setattr(app_module, "RealSerialTransport", BusyTransport)
    monkeypatch.setattr(broker._supervisor, "BACKOFF_S", (0,))
    holder["ports"].append(make_port())
    assert await until(
        lambda: broker.manager.get_state().serial_status is DeviceStatus.FAULT, timeout=10
    )
    assert "open failed" in broker.manager.get_state().to_wire()["serial_detail"]


async def test_rescan_resets_backoff_and_retries_immediately(waiting_broker, monkeypatch):
    broker, holder = waiting_broker
    supervisor = broker._supervisor
    supervisor.BACKOFF_S = (30.0,)  # would stall any automatic retry
    # The supervisor's first cycle enters its 30s backoff with no device.
    assert await until(lambda: supervisor._reconnecting, timeout=5)
    # Manual rescan wakes the backoff sleep and resets the ladder; the
    # attempt still fails (no device) but completes promptly.
    broker.rescan_devices()
    assert await until(lambda: supervisor._serial_attempts == 1, timeout=5)
    # Device appears; without another rescan the 30s ladder keeps waiting.
    holder["ports"].append(make_port())
    await asyncio.sleep(0.2)
    assert broker.actor is None
    # Manual rescan zeroes the ladder and wakes the loop again.
    broker.rescan_devices()
    assert await until(lambda: broker.actor is not None, timeout=5)
    assert supervisor._serial_attempts == 0


async def test_wait_healthy_skipped_when_device_missing(tmp_path, monkeypatch):
    """Startup does not burn the health-check timeout without hardware."""
    monkeypatch.setattr(app_module, "list_serial_ports", lambda: [])
    monkeypatch.setattr(app_module, "list_video_devices", lambda backend: [])
    broker = Broker(real_config(), runtime_dir=tmp_path / "runtime", web_dir=None)
    started = time.monotonic()
    await broker.start()
    assert not broker.initial_device_ok
    # No wait_healthy delay is observable here (serve decides), but start()
    # itself must not block on missing hardware.
    assert time.monotonic() - started < 5
    await broker.stop()


async def test_stop_with_never_attached_devices_is_clean(waiting_broker):
    broker, _ = waiting_broker
    # Stop while no device ever attached: bounded shutdown, runtime removed.
    runtime_file = broker.runtime_dir / "broker.json"
    assert runtime_file.is_file()
    await broker.stop()
    assert not runtime_file.exists()
    assert broker.actor is None and broker.capture is None
