"""D01 protocol vectors, short writes and extension resync (simulated)."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from superuse.core.models import KeyboardReport, MouseAbsReport, MouseRelReport
from superuse.device.protocol import (
    EXT_HEADER,
    ExtFrameParser,
    build_device_info_query,
    build_ext_frame,
    encode_hid_report,
    ext_checksum,
    parse_device_info,
)

ROOT = Path(__file__).resolve().parents[3]


def vectors() -> list[dict]:
    return json.loads((ROOT / "examples" / "protocol-vectors.json").read_text(encoding="utf-8"))


def test_d01_golden_vectors():
    expected = {v["name"]: bytes.fromhex(v["hex"]) for v in vectors()}
    assert encode_hid_report(KeyboardReport(0, ())) == expected["keyboard_release"]
    assert encode_hid_report(KeyboardReport(0x00, (0x04,))) == expected["key_a_down"]
    assert encode_hid_report(KeyboardReport(0x01, (0x04,))) == expected["ctrl_a_down"]
    assert (
        encode_hid_report(MouseAbsReport(0, 2048, 2048, 0))
        == expected["mouse_absolute_center_release"]
    )
    assert encode_hid_report(MouseRelReport(0, 0, 0, 0)) == expected["mouse_relative_zero_release"]


def test_d01_keyboard_dedupe_and_limits():
    report = encode_hid_report(KeyboardReport(0x02, (0x04, 0x04, 0x05)))
    assert report[7:9] == bytes(
        (0x04, 0x05)
    )  # duplicate key removed; keys start after modifier+reserved
    with pytest.raises(ValueError):
        encode_hid_report(KeyboardReport(0, tuple(range(1, 8))))
    with pytest.raises(ValueError):
        encode_hid_report(MouseAbsReport(0, 4096, 0, 0))


def test_d01_extension_query_roundtrip():
    query = build_device_info_query()
    assert query[:2] == b"\x57\xab"
    channel, sn, dst, total, kind, size = EXT_HEADER.unpack_from(query, 2)
    assert (channel, sn, dst, kind, size) == (1, 0, 0, 1, 0)
    assert total == size + 6
    (checksum,) = struct.unpack_from("<I", query, len(query) - 4)
    assert checksum == ext_checksum(query[:-4])

    parser = ExtFrameParser()
    frames = parser.feed(query)
    assert len(frames) == 1 and frames[0].checksum_ok


def test_d01_extension_chunked_noise_and_resync():
    payload = bytes(range(69))
    frame = build_ext_frame(1, 0x81, payload)
    noise = b"\x00\xffjunk"
    parser = ExtFrameParser()
    got = []
    stream = noise + frame  # noise before the frame; feed in small chunks
    for i in range(0, len(stream), 7):
        got.extend(parser.feed(stream[i : i + 7]))
    valid = [f for f in got if f.checksum_ok and f.kind == 0x81]
    assert len(valid) == 1
    assert valid[0].payload == payload

    # Bytes injected mid-frame corrupt it: emitted but checksum-rejected.
    corrupt = frame[:40] + b"\x57\xab" + frame[40:]
    parser_mid = ExtFrameParser()
    frames = parser_mid.feed(corrupt)
    assert any(not f.checksum_ok for f in frames)
    assert all(not (f.checksum_ok and f.kind == 0x81) for f in frames)

    # Bad checksum followed by a good frame: parser resyncs past the bad one.
    broken = bytearray(build_ext_frame(1, 5, b"abc"))
    broken[-1] ^= 0xFF
    parser2 = ExtFrameParser()
    frames = parser2.feed(bytes(broken) + build_ext_frame(1, 6, b"ok"))
    kinds = [f.kind for f in frames if f.checksum_ok]
    assert kinds == [6]

    # Implausible declared size never allocates huge buffers.
    evil = b"\x57\xab" + struct.pack("<BHHIHI", 1, 0, 0, 999, 1, 300000)
    parser3 = ExtFrameParser()
    assert parser3.feed(evil) == []
    assert len(parser3._buffer) <= 512 * 1024


def test_device_info_parse_tolerates_short_payloads():
    full = parse_device_info(bytes(135))
    assert full["firmware"] == "0.0.0"
    assert "build_time" in full
    short = parse_device_info(bytes(24))
    assert "firmware" in short and "build_time" not in short
    assert parse_device_info(bytes(4)) == {}


def test_mouse_mode_frame_matches_vendor_setmos_emode():
    """The listed extension write must be byte-identical to the vendor frame.

    Expected layout: 57 AB | ch=1 | sn=0 | dst=0 | total=9 | kind=5 | size=3
    | payload [mode,0,1] | weighted CRC32 LE (vendor webapp setMouseMode).
    """
    from superuse.device.protocol import build_mouse_mode_frame

    assert build_mouse_mode_frame(2).hex(" ") == (
        "57 ab 01 00 00 00 00 09 00 00 00 05 00 03 00 00 00 02 00 01 b6 01 00 00"
    )
    assert build_mouse_mode_frame(1).hex(" ") == (
        "57 ab 01 00 00 00 00 09 00 00 00 05 00 03 00 00 00 01 00 01 a4 01 00 00"
    )
    with pytest.raises(ValueError):
        build_mouse_mode_frame(3)
    with pytest.raises(ValueError):
        build_mouse_mode_frame(0)


def test_device_info_mouse_coordinate_mode_names():
    from superuse.device.protocol import MOUSE_MODE_ABSOLUTE, mouse_mode_name

    payload = bytearray(135)
    payload[25] = 2
    info = parse_device_info(bytes(payload))
    assert info["mouse_coordinate_mode_raw"] == 2
    assert info["mouse_coordinate_mode"] == "absolute"
    assert mouse_mode_name(MOUSE_MODE_ABSOLUTE) == "absolute"
    payload[25] = 1
    assert parse_device_info(bytes(payload))["mouse_coordinate_mode"] == "relative"
    payload[25] = 7  # unknown firmware value: raw kept, name omitted
    assert parse_device_info(bytes(payload))["mouse_coordinate_mode"] is None


def test_parse_general_reply():
    from superuse.device.protocol import parse_general_reply

    assert parse_general_reply(bytes((0, 3))) == {"rsp_code": 0, "app_id": 3}
    assert parse_general_reply(bytes((1, 3)))["rsp_code"] == 1
    assert parse_general_reply(b"\x00") == {"rsp_code": None, "app_id": None}


def test_device_restart_frame_matches_vendor():
    """Vendor restartDevice: channel=1, kind=10, payload [1,0,0,0]."""
    from superuse.device.protocol import build_device_restart_frame

    frame = build_device_restart_frame()
    channel, sn, dst, total, kind, size = EXT_HEADER.unpack_from(frame, 2)
    assert (channel, sn, dst, kind, size) == (1, 0, 0, 10, 4)
    assert total == size + 6
    assert frame[17:21] == bytes((1, 0, 0, 0))
    (checksum,) = struct.unpack_from("<I", frame, len(frame) - 4)
    assert checksum == ext_checksum(frame[:-4])
