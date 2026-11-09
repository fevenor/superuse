from __future__ import annotations

import asyncio
import contextlib
import copy
import time
import uuid

from superuse.config import ConfigError, load_config
from superuse.core.errors import ControlError, ErrorCode
from superuse.device.discovery import list_serial_ports, list_video_devices
from superuse.device.lifecycle import stop_device
from superuse.device.profiles import BAUD_RATES, capabilities
from superuse.device.protocol import (
    build_mouse_auto_move_frame,
    build_video_power_frame,
)


class DeviceManagement:
    def __init__(self, broker):
        self.broker = broker
        self._choices: dict = {}

    @contextlib.asynccontextmanager
    async def transaction(self):
        broker = self.broker
        async with broker._device_control_lock:
            try:
                await broker.manager.suspend_device()
                yield
            finally:
                if broker._supervisor is not None and broker.manager.mode.value != "stopping":
                    broker._supervisor.rescan()

    def overview(self) -> dict:
        broker = self.broker
        device = broker.config.section("device")
        actor = broker.actor
        info = dict(actor.device_info or {}) if actor else {}
        for key in ("usb_baud", "usb_host_baud"):
            if info.get(key) in (0, 0xFFFFFFFF):
                info[key] = None
        return {
            "capabilities": capabilities(device["type"]),
            "connection": {
                "device_type": device["type"],
                "baud": device["serial"]["baud"],
                "width": device["video"]["width"],
                "height": device["video"]["height"],
                "fps": device["video"]["requested_fps"],
            },
            "baud_rates": list(BAUD_RATES),
            "info": info,
            "power": broker.power_state(),
            "state": broker.manager.get_state().to_wire(),
            "serial": {
                "status": actor.status if actor else "disconnected",
                "tx_bytes": actor.tx_bytes if actor else 0,
                "rx_bytes": actor.rx_bytes if actor else 0,
            },
            "logs_enabled": actor.logs_enabled if actor else False,
            "active_rect": list(broker.hub.active_rect) if broker.hub.active_rect else None,
            "capture": {
                "width": broker.hub.latest.width,
                "height": broker.hub.latest.height,
                "signal": "unknown",
            }
            if broker.hub.latest
            else None,
        }

    async def discover(self) -> dict:
        device = self.broker.config.section("device")
        serial, video = await asyncio.gather(
            asyncio.to_thread(list_serial_ports),
            asyncio.to_thread(list_video_devices, device["video"]["backend"]),
        )
        self._choices = {}
        result = {"serial": [], "video": []}
        for kind, entries in (("serial", serial), ("video", video)):
            for entry in entries:
                if entry.vid is None or entry.pid is None:
                    continue
                if kind == "serial" and not (entry.serial_number or entry.location):
                    continue
                if kind == "video" and not entry.device_path:
                    continue
                key = str(uuid.uuid4())
                self._choices[key] = (kind, entry)
                label = f"{entry.device} · {entry.description}" if kind == "serial" else entry.name
                result[kind].append(
                    {"id": key, "label": f"{label} ({entry.vid:04X}:{entry.pid:04X})"}
                )
        return result

    def _selected(self, key, kind):
        item = self._choices.get(key) if isinstance(key, str) else None
        if item is None or item[0] != kind:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Selection expired; scan devices again")
        return item[1]

    async def configure(self, body: dict) -> dict:
        required = {"device_type", "baud", "width", "height", "fps"}
        if not required <= body.keys() or set(body) - required - {"serial_choice", "video_choice"}:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Invalid connection settings")
        broker = self.broker
        updated = copy.deepcopy(broker.config.raw)
        device = updated["device"]
        device["type"] = body["device_type"]
        device["serial"]["baud"] = body["baud"]
        device["video"].update(
            width=body["width"], height=body["height"], requested_fps=body["fps"]
        )
        if body.get("serial_choice"):
            selected = self._selected(body["serial_choice"], "serial")
            device["serial"].update(
                vid=selected.vid,
                pid=selected.pid,
                serial_number=selected.serial_number,
                location=selected.location,
            )
        if body.get("video_choice"):
            selected = self._selected(body["video_choice"], "video")
            device["video"].update(
                vid=selected.vid,
                pid=selected.pid,
                name=selected.name,
                device_path=selected.device_path,
            )
        if not body.get("serial_choice"):
            device["serial"]["serial_number"] = (
                device["serial"]["serial_number"] or broker._pinned_serial
            )
            device["serial"]["location"] = (
                device["serial"]["location"] or broker._pinned_serial_location
            )
        if not body.get("video_choice"):
            device["video"]["device_path"] = device["video"]["device_path"] or broker._pinned_video
        try:
            load_config(overrides=updated)
        except ConfigError as exc:
            raise ControlError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        async with self.transaction():
            previous = copy.deepcopy(broker.config.raw)
            saved = copy.deepcopy(broker.preferences.values)
            try:
                await self._close_devices()
                values = {**saved, "device": {k: v for k, v in device.items() if k != "mode"}}
                broker.preferences.save(values)
                broker.config.raw["device"] = device
                broker._pinned_serial = broker._pinned_serial_location = broker._pinned_video = ""
                broker.hub.begin_capture_generation()
                broker._supervisor._capture_reconnect_pending = False
                broker._open_serial()
                broker._open_capture()
            except Exception:
                broker.config.raw["device"] = previous["device"]
                broker.preferences.save(saved)
                raise
        return self.overview()

    async def _close_devices(self):
        broker = self.broker
        if broker.capture is not None:
            await stop_device(broker.capture)
            if getattr(broker.capture, "fault_reason", None) == "capture_close_hung":
                raise ControlError(
                    ErrorCode.DEVICE_UNAVAILABLE,
                    "Capture close is blocked; restart the application",
                )
            broker.capture = None
        if broker.actor is not None:
            await stop_device(broker.actor)
            if broker.actor.close_hung:
                raise ControlError(
                    ErrorCode.DEVICE_UNAVAILABLE, "Serial close is blocked; restart the application"
                )
            broker.actor = None

    def _actor(self):
        actor = self.broker.actor
        if self.broker.config.section("device")["type"] != "superkvm":
            raise ControlError(
                ErrorCode.INVALID_REQUEST, "This device does not support SuperKVM settings"
            )
        if actor is None or actor.status != "ready":
            raise ControlError(ErrorCode.DEVICE_UNAVAILABLE, "Serial device not ready")
        return actor

    async def refresh(self) -> dict:
        actor = self._actor()
        revision = actor.info_revision
        result = await actor.query_info()
        if not result.ok:
            raise ControlError(ErrorCode.SERIAL_WRITE_FAILED, "Device query failed")
        deadline = time.monotonic() + 2
        while actor.info_revision <= revision and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if actor.info_revision <= revision:
            raise ControlError(
                ErrorCode.DEVICE_UNAVAILABLE, "Device did not answer the information query"
            )
        return self.overview()

    async def set_boolean(self, name: str, enabled: bool) -> dict:
        if type(enabled) is not bool or name not in ("video_low_power", "mouse_auto_move"):
            raise ControlError(ErrorCode.INVALID_REQUEST, "Invalid device setting")
        field = "video_low_power_enable_raw" if name == "video_low_power" else "mouse_auto_move_raw"
        builder = (
            build_video_power_frame if name == "video_low_power" else build_mouse_auto_move_frame
        )
        async with self.transaction():
            actor = self._actor()
            if (actor.device_info or {}).get(field) not in (0, 1):
                raise ControlError(
                    ErrorCode.INVALID_REQUEST, "Setting not reported by this firmware"
                )
            result = await actor.set_setting(builder(enabled))
            if not result.ok:
                raise ControlError(ErrorCode.SERIAL_WRITE_FAILED, "Device setting write failed")
            await self.refresh()
            if actor.device_info.get(field) != int(enabled):
                raise ControlError(
                    ErrorCode.RESULT_UNKNOWN, "Device did not confirm the requested value"
                )
        return self.overview()

    async def detect_calibration(self) -> dict:
        from superuse.device.calibration import detect_active_rect

        frame = self.broker.hub.latest
        if frame is None or time.monotonic() - frame.monotonic > self.broker.config.freshness_s:
            raise ControlError(ErrorCode.VIDEO_STALE, "No fresh frame to calibrate")
        rect = await asyncio.to_thread(detect_active_rect, frame)
        return {"rect": rect, "width": frame.width, "height": frame.height}

    async def calibrate(self, rect) -> dict:
        async with self.transaction():
            self.broker.hub.set_active_rect(rect)
            self.broker.manager.geometry_changed()
        return self.overview()

    def logs(self, enabled: bool | None = None, clear: bool = False) -> dict:
        actor = self._actor()
        if enabled is not None:
            if type(enabled) is not bool:
                raise ControlError(ErrorCode.INVALID_REQUEST, "enabled must be a boolean")
            actor.logs_enabled = enabled
        rows = actor.read_logs(clear=clear)
        serial = (actor.device_info or {}).get("serial_number", "")
        if serial:
            rows = [{**row, "message": row["message"].replace(serial, "[device]")} for row in rows]
        return {"enabled": actor.logs_enabled, "entries": [] if clear else rows, "capacity": 500}
