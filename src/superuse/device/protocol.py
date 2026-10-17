"""SuperKVM CDC wire protocol: HID input frames and read-only extension frames.

HID framing (``57 AB ADDR CMD LEN PAYLOAD SUM8``) is the CH9329-compatible
input path described in docs/development/device-video.md §2; the extension framing and the
weighted checksum come from the vendor webapp as verified in the parent
research (probe.py / device-info.json). The legacy ``GET_INFO`` command 0x01
is intentionally absent: the device rejects it.
"""

from __future__ import annotations

import struct

from superuse.core.models import KeyboardReport, MouseAbsReport, MouseRelReport

HID_HEADER = bytes((0x57, 0xAB))
HID_ADDR = 0x00
CMD_KEYBOARD = 0x02
CMD_MOUSE_ABS = 0x04
CMD_MOUSE_REL = 0x05

EXT_PREFIX = bytes((0x57, 0xAB))
# 57 AB + ch:u8 + sn:u16 + dst:u16 + total:u32 + kind:u16 + size:u32 = 17 bytes
EXT_HEADER = struct.Struct("<BHHIHI")
EXT_HEADER_SIZE = 2 + EXT_HEADER.size  # 17
EXT_CHECKSUM = struct.Struct("<I")
EXT_QUERY_CHANNEL = 1
EXT_QUERY_KIND = 1
EXT_INFO_KIND = 0x81
EXT_PAYLOAD_LIMIT = 256 * 1024
EXT_BUFFER_LIMIT = 512 * 1024


def hid_checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_hid_packet(cmd: int, payload: bytes) -> bytes:
    if len(payload) > 255:
        raise ValueError("HID payload too long")
    packet = HID_HEADER + bytes((HID_ADDR, cmd, len(payload))) + payload
    return packet + bytes((hid_checksum(packet),))


def encode_hid_report(report: KeyboardReport | MouseAbsReport | MouseRelReport) -> bytes:
    if isinstance(report, KeyboardReport):
        keys = sorted({k for k in report.keys if k})
        if len(keys) > 6:
            raise ValueError("At most 6 keys per keyboard report")
        keys = keys + [0] * (6 - len(keys))
        payload = bytes((report.modifier & 0xFF, 0x00, *keys))
        return build_hid_packet(CMD_KEYBOARD, payload)
    if isinstance(report, MouseAbsReport):
        if not (0 <= report.x <= 4095 and 0 <= report.y <= 4095):
            raise ValueError("Absolute coordinates must be 0..4095")
        payload = bytes(
            (
                0x02,
                report.buttons & 0x07,
                report.x & 0xFF,
                (report.x >> 8) & 0x0F,
                report.y & 0xFF,
                (report.y >> 8) & 0x0F,
                report.wheel & 0xFF,
            )
        )
        return build_hid_packet(CMD_MOUSE_ABS, payload)
    if isinstance(report, MouseRelReport):
        payload = bytes(
            (
                0x01,
                report.buttons & 0x07,
                report.dx & 0xFF,
                report.dy & 0xFF,
                report.wheel & 0xFF,
            )
        )
        return build_hid_packet(CMD_MOUSE_REL, payload)
    raise TypeError(f"Unsupported report type {type(report).__name__}")


# ---------------------------------------------------------------------------
# Extension protocol (read-only device management frames)
# ---------------------------------------------------------------------------


def ext_checksum(data: bytes) -> int:
    """Vendor weighted sum (webapp getMcAddCrcFallback); not CRC32."""
    return sum(b + (b & 7) * (i & 31) for i, b in enumerate(data)) & 0xFFFFFFFF


def build_ext_frame(
    channel: int, kind: int, payload: bytes = b"", sn: int = 0, dst: int = 0
) -> bytes:
    if len(payload) > EXT_PAYLOAD_LIMIT:
        raise ValueError("Extension payload too long")
    body = EXT_HEADER.pack(channel, sn, dst, len(payload) + 6, kind, len(payload))
    frame = EXT_PREFIX + body + payload
    return frame + EXT_CHECKSUM.pack(ext_checksum(frame))


def build_device_info_query() -> bytes:
    """Read-only requestDeviceInfo packet (channel=1, kind=1, size=0)."""
    return build_ext_frame(EXT_QUERY_CHANNEL, EXT_QUERY_KIND)


class ExtFrame:
    __slots__ = ("channel", "sn", "dst", "total", "kind", "size", "payload", "checksum_ok")

    def __init__(
        self,
        channel: int,
        sn: int,
        dst: int,
        total: int,
        kind: int,
        size: int,
        payload: bytes,
        checksum_ok: bool,
    ) -> None:
        self.channel = channel
        self.sn = sn
        self.dst = dst
        self.total = total
        self.kind = kind
        self.size = size
        self.payload = payload
        self.checksum_ok = checksum_ok


class ExtFrameParser:
    """Incremental receiver with bounded buffers and sliding resync.

    Feed arbitrary chunked/noisy bytes; complete valid frames pop out. On a
    bad header, oversized declared payload or failed checksum the scan slides
    one byte forward (resync). The buffer never exceeds EXT_BUFFER_LIMIT.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[ExtFrame]:
        self._buffer.extend(data)
        frames: list[ExtFrame] = []
        position = 0
        while True:
            start = self._buffer.find(EXT_PREFIX, position)
            if start < 0:
                position = max(position, len(self._buffer) - 1)
                break
            if len(self._buffer) - start < EXT_HEADER_SIZE + EXT_CHECKSUM.size:
                position = start
                break
            channel, sn, dst, total, kind, size = EXT_HEADER.unpack_from(self._buffer, start + 2)
            if size > EXT_PAYLOAD_LIMIT or total != size + 6:
                position = start + 1  # implausible header: resync
                continue
            end = start + EXT_HEADER_SIZE + size
            if len(self._buffer) < end + EXT_CHECKSUM.size:
                position = start
                break
            (received,) = EXT_CHECKSUM.unpack_from(self._buffer, end)
            body = bytes(self._buffer[start:end])
            payload = bytes(self._buffer[start + EXT_HEADER_SIZE : end])
            frames.append(
                ExtFrame(
                    channel, sn, dst, total, kind, size, payload, received == ext_checksum(body)
                )
            )
            position = end + EXT_CHECKSUM.size
        if position > 0:
            del self._buffer[:position]
        if len(self._buffer) > EXT_BUFFER_LIMIT:
            # Never allocate unbounded memory for a noisy line; drop the head.
            del self._buffer[: len(self._buffer) - EXT_BUFFER_LIMIT]
        return frames


def parse_device_info(payload: bytes) -> dict:
    """Parse the read-only device-info response payload (kind 0x81).

    Field offsets verified against a real 135-byte reply in the research
    evidence; tolerate shorter payloads.
    """

    def u16(offset: int) -> int:
        return struct.unpack_from("<H", payload, offset)[0]

    def version(offset: int) -> str:
        value = u16(offset)
        return f"{value // 1000}.{value % 1000 // 100}.{value % 100}"

    info: dict = {}
    if len(payload) >= 24:
        info.update(
            {
                "firmware": version(16),
                "hardware": version(18),
                "bootloader": version(20),
                "temperature_c": u16(22) / 100,
            }
        )
    if len(payload) >= 36:
        info.update(
            {
                "mouse_coordinate_mode_raw": payload[25],
                "usb_baud": struct.unpack_from("<I", payload, 28)[0],
                "usb_vid": hex(u16(32)),
                "usb_pid": hex(u16(34)),
            }
        )
    if len(payload) >= 69:
        info["run_state_raw"] = payload[68]
    if len(payload) >= 109:
        info["build_time"] = payload[77:109].decode("utf-8", errors="replace").rstrip("\x00")
    if len(payload) >= 135:
        info["video_low_power_enable_raw"] = payload[134]
    return info
