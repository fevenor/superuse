"""D02 identity matching across enumeration snapshots (no devices opened)."""

from __future__ import annotations

import pytest

from superuse.core.errors import ControlError, ErrorCode
from superuse.device.discovery import (
    SerialPortInfo,
    VideoDeviceInfo,
    match_serial_port,
    match_video_device,
)


def ports(*items):
    return [SerialPortInfo(device=d, vid=v, pid=p, serial_number=s) for d, v, p, s in items]


def test_unique_match_and_index_remap():
    snapshot_1 = ports(("COM3", 0x1209, 0x0001, "A1"), ("COM5", 0x1A86, 0x7523, "CH"))
    found = match_serial_port(snapshot_1, vid=0x1209, pid=0x0001, serial_number="A1")
    assert found.device == "COM3"

    # USB replug moved the same identity to a different COM letter.
    snapshot_2 = ports(("COM7", 0x1209, 0x0001, "A1"), ("COM5", 0x1A86, 0x7523, "CH"))
    found_2 = match_serial_port(snapshot_2, vid=0x1209, pid=0x0001, serial_number="A1")
    assert found_2.device == "COM7"

    # Without a serial number, a unique VID/PID match is fine...
    only = ports(("COM3", 0x1209, 0x0001, "A1"))
    assert match_serial_port(only, vid=0x1209, pid=0x0001).device == "COM3"


def test_ambiguous_and_missing_devices_never_fall_back():
    two = ports(("COM3", 0x1209, 0x0001, "A1"), ("COM4", 0x1209, 0x0001, "B2"))
    with pytest.raises(ControlError) as ei:
        match_serial_port(two, vid=0x1209, pid=0x0001)
    assert ei.value.code is ErrorCode.DEVICE_AMBIGUOUS

    with pytest.raises(ControlError) as none_:
        match_serial_port(two, vid=0x1209, pid=0x0001, serial_number="ZZZ")
    assert none_.value.code is ErrorCode.DEVICE_UNAVAILABLE

    # Unrelated vendors are never considered, even when nothing matches.
    others = ports(("COM9", 0x1A86, 0x7523, "CH340"))
    with pytest.raises(ControlError):
        match_serial_port(others, vid=0x1209, pid=0x0001)


def cams(backend: str, entries):
    return [
        VideoDeviceInfo(name=n, device_path=p, vid=0x345F, pid=0x2131, backend=backend, index=i)
        for i, (n, p) in enumerate(entries)
    ]


def test_video_backend_spaces_are_separate():
    dshow = cams(
        "dshow",
        [
            ("SK USB3 Video", "@device_pnp_\\\\?\\usb#vid_345f&pid_2131#SK"),
            ("Integrated Webcam", "builtin0"),
        ],
    )
    msmf = cams("msmf", [("SK USB3 Video", "msmf_path_1")])

    found = match_video_device(dshow, backend="dshow", name="SK USB3 Video")
    assert found.index == 0

    # The same name exists under msmf with its own index space; choosing a
    # backend never reuses the other backend's index.
    found_msmf = match_video_device(msmf, backend="msmf", name="SK USB3 Video")
    assert found_msmf.backend == "msmf" and found_msmf.device_path == "msmf_path_1"

    # Device path disambiguates duplicates within one backend.
    dup = cams("dshow", [("SK USB3 Video", "path_A"), ("SK USB3 Video", "path_B")])
    with pytest.raises(ControlError) as ei:
        match_video_device(dup, backend="dshow", name="SK USB3 Video")
    assert ei.value.code is ErrorCode.DEVICE_AMBIGUOUS
    assert (
        match_video_device(
            dup, backend="dshow", name="SK USB3 Video", device_path="path_B"
        ).device_path
        == "path_B"
    )

    # No builtin-camera fallback: a wrong name is an error, not a substitute.
    with pytest.raises(ControlError):
        match_video_device(
            dshow, backend="dshow", name="Integrated Webcam", device_path="builtin0"
        ) if False else match_video_device(dshow, backend="dshow", name="No Such Cam")
