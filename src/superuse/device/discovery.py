"""Device discovery and stable-identity matching.

Selection stores *stable identities* (VID/PID + serial number / device path),
never raw COM letters or camera indexes. Opening re-maps the identity to the
current index; enumeration itself never opens any device — especially not the
built-in camera (docs/development/device-video.md §1). Real enumeration helpers lazily
import their libraries so base-environment imports stay hardware-free.
"""

from __future__ import annotations

from dataclasses import dataclass

from superuse.core.errors import ControlError, ErrorCode

SERIAL_VID = 0x1209
SERIAL_PID = 0x0001
VIDEO_VID = 0x345F
VIDEO_PID = 0x2131
VIDEO_NAME = "SK USB3 Video"


@dataclass(frozen=True)
class SerialPortInfo:
    device: str  # e.g. COM3 — a snapshot, never persisted as identity
    vid: int
    pid: int
    serial_number: str
    description: str = ""


@dataclass(frozen=True)
class VideoDeviceInfo:
    name: str
    device_path: str
    vid: int | None
    pid: int | None
    backend: str  # "dshow" | "msmf" — index spaces are NOT interchangeable
    index: int  # backend-local snapshot index


def match_serial_port(
    ports: list[SerialPortInfo], *, vid: int, pid: int, serial_number: str = ""
) -> SerialPortInfo:
    """Find the unique port matching the configured identity.

    Empty ``serial_number`` matches any serial of the same VID/PID, but then
    requires exactly one candidate; multiple candidates raise
    DEVICE_AMBIGUOUS instead of guessing. No fallback to other devices.
    """
    candidates = [p for p in ports if p.vid == vid and p.pid == pid]
    if serial_number:
        candidates = [p for p in candidates if p.serial_number == serial_number]
    if not candidates:
        raise ControlError(
            ErrorCode.DEVICE_UNAVAILABLE,
            f"No serial device matching {vid:04x}:{pid:04x}"
            + (f" sn={serial_number}" if serial_number else ""),
        )
    if len(candidates) > 1:
        listing = ", ".join(f"{p.device}(sn={p.serial_number or '?'})" for p in candidates)
        raise ControlError(
            ErrorCode.DEVICE_AMBIGUOUS,
            f"Multiple serial devices match; configure serial_number ({listing})",
        )
    return candidates[0]


def match_video_device(
    cameras: list[VideoDeviceInfo], *, backend: str, name: str, device_path: str = ""
) -> VideoDeviceInfo:
    """Find the unique capture device for the requested backend.

    Backend-specific indexes never leak across: matching is by name/path
    inside the backend's own enumeration. No builtin-camera fallback.
    """
    candidates = [c for c in cameras if c.backend == backend and c.name == name]
    if device_path:
        candidates = [c for c in candidates if c.device_path == device_path]
    if not candidates:
        raise ControlError(
            ErrorCode.DEVICE_UNAVAILABLE,
            f"No {backend} camera named {name!r}"
            + (f" at path {device_path!r}" if device_path else ""),
        )
    if len(candidates) > 1:
        listing = ", ".join(c.device_path or "?" for c in candidates)
        raise ControlError(
            ErrorCode.DEVICE_AMBIGUOUS,
            f"Multiple {backend} cameras named {name!r}; configure device_path ({listing})",
        )
    return candidates[0]


def list_serial_ports() -> list[SerialPortInfo]:
    """Enumerate CDC candidates via pyserial (app extra); opens nothing."""
    import serial.tools.list_ports

    return [
        SerialPortInfo(
            device=p.device,
            vid=p.vid or 0,
            pid=p.pid or 0,
            serial_number=p.serial_number or "",
            description=p.description or "",
        )
        for p in serial.tools.list_ports.comports()
    ]


def list_video_devices(backend: str) -> list[VideoDeviceInfo]:
    """Enumerate cameras for one backend via cv2-enumerate-cameras (app extra).

    Enumeration only — this must not open camera handles; the helper library
    itself performs a read-only device query per backend.
    """
    import cv2
    from cv2_enumerate_cameras import enumerate_cameras  # type: ignore[import-untyped]

    capture_backends = {
        "dshow": cv2.CAP_DSHOW,
        "msmf": cv2.CAP_MSMF,
    }
    if backend not in capture_backends:
        raise ControlError(ErrorCode.DEVICE_UNAVAILABLE, f"Unknown capture backend {backend!r}")
    devices = []
    for camera in enumerate_cameras(capture_backends[backend]):
        devices.append(
            VideoDeviceInfo(
                name=camera.name,
                device_path=getattr(camera, "path", "") or "",
                vid=getattr(camera, "vid", None),
                pid=getattr(camera, "pid", None),
                backend=backend,
                index=camera.index,
            )
        )
    return devices
