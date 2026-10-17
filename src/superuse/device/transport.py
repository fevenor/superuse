"""Serial transport abstraction.

Only the broker's device module may hold the real serial handle. The real
transport lazily imports pyserial so the base dev environment (and every
default test) can import this module without the app extra or any hardware.
"""

from __future__ import annotations

from typing import Protocol


class TransportClosed(Exception):
    """The serial port is not open (or vanished mid-write)."""


class SerialTransport(Protocol):
    """Byte transport used exclusively by the SerialActor's writer thread."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def write_report(self, data: bytes, timeout_s: float) -> int:
        """Write one report; returns bytes written (may be short) or raises.

        TimeoutError for a bounded hang, TransportClosed when the port is
        gone. A short/failed result is *uncertain* — never resend blindly.
        """
        ...

    def read(self, timeout_s: float) -> bytes:
        """Read available bytes; b"" on timeout. Raises TransportClosed if dead."""
        ...


class RealSerialTransport:
    """pyserial CDC transport; importing this class needs no hardware until open."""

    def __init__(self, port: str, *, baud: int = 2_000_000, read_timeout_s: float = 0.1) -> None:
        self._port = port
        self._baud = baud
        self._read_timeout_s = read_timeout_s
        self._serial = None

    def open(self) -> None:
        import serial  # lazy: app extra only

        if self._serial is not None and self._serial.is_open:
            return
        self._serial = serial.Serial(
            port=self._port,
            baudrate=self._baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self._read_timeout_s,
            write_timeout=None,  # enforced per write via time box below
        )

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def write_report(self, data: bytes, timeout_s: float) -> int:
        import serial  # lazy

        if not self.is_open:
            raise TransportClosed(self._port)
        self._serial.write_timeout = timeout_s
        try:
            return self._serial.write(data)
        except serial.SerialTimeoutException as exc:
            raise TimeoutError(self._port) from exc
        except serial.SerialException as exc:
            self.close()
            raise TransportClosed(self._port) from exc

    def read(self, timeout_s: float) -> bytes:
        import serial  # lazy

        if not self.is_open:
            raise TransportClosed(self._port)
        try:
            self._serial.timeout = timeout_s
            return bytes(self._serial.read(4096))
        except serial.SerialException as exc:
            self.close()
            raise TransportClosed(self._port) from exc
