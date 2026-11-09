from __future__ import annotations

import asyncio
import time

import pytest

from superuse.core.errors import ControlError
from superuse.device.capture import CaptureWorker, Frame
from superuse.device.discovery import VideoDeviceInfo, match_video_device
from superuse.device.protocol import build_video_power_frame, parse_device_log
from superuse.device.serial_actor import SerialActor
from superuse.device.simulated import SimulatedSerial


async def test_ch9329_never_sends_superkvm_extensions():
    serial = SimulatedSerial()
    actor = SerialActor(lambda: serial, loop=asyncio.get_running_loop(), device_type="ch9329")
    actor.start()
    try:
        assert actor._heartbeat is None
        await asyncio.sleep(0.05)
        assert (await actor.set_setting(build_video_power_frame(False))).error == "UNSUPPORTED"
        assert (await actor.query_info()).error == "UNSUPPORTED"
        assert (await actor.set_mouse_mode(2)).error == "UNSUPPORTED"
        assert serial.writes == []
    finally:
        await asyncio.to_thread(actor.stop)


def test_capture_read_exception_releases_and_reports_fault(monkeypatch):
    class BrokenCamera:
        released = False

        def read(self):
            raise RuntimeError("driver failed")

        def release(self):
            self.released = True

    camera = BrokenCamera()
    worker = CaptureWorker(backend="dshow", index=0, width=640, height=480, fps=30)
    monkeypatch.setattr(worker, "_open", lambda: setattr(worker, "_cap", camera))
    worker._run()
    assert worker.status == "fault"
    assert worker.fault_reason == "read_exception"
    assert camera.released


def test_video_identity_requires_matching_usb_ids():
    wrong = VideoDeviceInfo("SK USB3 Video", "fake-path", 1, 2, "dshow", 0)
    with pytest.raises(ControlError):
        match_video_device([wrong], backend="dshow", name="SK USB3 Video", vid=0x345F, pid=0x2131)


def test_dark_frame_calibration_refuses_guessing():
    from superuse.device.calibration import detect_active_rect

    frame = Frame(bytes(320 * 180 * 3), "bgr", 320, 180, 1, time.monotonic(), Frame.now_iso())
    with pytest.raises(ControlError):
        detect_active_rect(frame)


def test_device_log_bounds_and_structure():
    import struct

    text = b"ready"
    payload = struct.pack("<HBB16sH", 101, 3, 1, b"USB", len(text)) + text
    assert parse_device_log(payload)["message"] == "ready"
    assert parse_device_log(payload[:-1]) is None
    assert parse_device_log(b"x" * 10000) is None


async def test_setting_rechecks_firmware_after_waiting_for_reconnect():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from superuse.config import load_config
    from superuse.device.management import DeviceManagement

    old_actor = SimpleNamespace(
        status="ready", device_info={"video_low_power_enable_raw": 0}, set_setting=AsyncMock()
    )
    new_actor = SimpleNamespace(
        status="ready", device_info={"video_low_power_enable_raw": 255}, set_setting=AsyncMock()
    )
    broker = SimpleNamespace(
        config=load_config(),
        actor=old_actor,
        _device_control_lock=asyncio.Lock(),
        _supervisor=None,
        manager=SimpleNamespace(suspend_device=AsyncMock()),
    )
    async with broker._device_control_lock:
        task = asyncio.create_task(DeviceManagement(broker).set_boolean("video_low_power", True))
        await asyncio.sleep(0)
        broker.actor = new_actor
    with pytest.raises(ControlError, match="Setting not reported"):
        await task
    old_actor.set_setting.assert_not_called()
    new_actor.set_setting.assert_not_called()
