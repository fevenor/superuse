"""Simulated devices: fault-injectable serial transport and capture source.

The simulator proves logic ordering only — never hardware HID behaviour or
real framerates (docs/development/device-video.md §6). Every UI/state surface using it
must label the source ``simulated``; missing real devices never fall back to
these objects silently.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from superuse.device.capture import Frame
from superuse.device.transport import TransportClosed


class SimulatedSerial:
    """CDC transport double recording writes with injectable failures.

    All methods are called from the actor's writer/reader threads, so the
    knobs are plain thread synchronization primitives.
    """

    def __init__(self, *, serial_number: str = "SIM-0001") -> None:
        self.serial_number = serial_number
        self.writes: list[bytes] = []
        self.is_open = False
        # Failure knobs: None | "short" | "timeout" | "fail"
        self.write_mode: str | None = None
        self.closed_mode = False
        self._block = threading.Event()
        self._block.set()
        self._responses: deque[bytes] = deque()
        self._rx_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        if self.closed_mode:
            raise TransportClosed("simulated port removed")
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    # -- transport ----------------------------------------------------------

    def write_report(self, data: bytes, timeout_s: float) -> int:
        if not self.is_open or self.closed_mode:
            raise TransportClosed("simulated port closed")
        if self.write_mode == "timeout":
            if not self._block.wait(timeout_s):
                raise TimeoutError("simulated write timeout")
        elif self.write_mode == "fail":
            raise OSError("simulated write failure")
        with self._rx_lock:
            self.writes.append(bytes(data))
        if self.write_mode == "short":
            return max(1, len(data) - 1)
        return len(data)

    def read(self, timeout_s: float) -> bytes:
        if not self.is_open or self.closed_mode:
            raise TransportClosed("simulated port closed")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._rx_lock:
                if self._responses:
                    return self._responses.popleft()
            time.sleep(0.002)
        return b""

    # -- test knobs ----------------------------------------------------------

    def block_writes(self) -> None:
        self._block.clear()

    def unblock_writes(self) -> None:
        self._block.set()

    def queue_response(self, data: bytes) -> None:
        with self._rx_lock:
            self._responses.append(bytes(data))


@dataclass
class SimulatedCapture:
    """Frame source emitting a deterministic pattern at a set FPS.

    Renders a timestamp pattern via Pillow when available (app extra); the
    dependency-free fallback emits raw BGR rows so core logic stays testable
    in the base environment. Supports size changes and stall injection.
    """

    fps: float = 10.0
    width: int = 1280
    height: int = 720
    start_delay_s: float = 0.0
    stall_after: int | None = None  # stop producing frames after N frames
    close_mode: str = "clean"  # "clean" | "hang" (close never finishes)
    on_frame: Callable[[Frame], None] | None = None

    latest: Frame | None = field(default=None, init=False)
    frames: list[Frame] = field(default_factory=list, init=False)
    fault: str | None = field(default=None, init=False)

    _seq: int = field(default=0, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="simulated-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                self.fault = "close_hung"
                return  # keep the slot occupied: never spawn a second thread
            self._thread = None

    def set_size(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    def _render(self) -> bytes:
        try:
            import io

            from PIL import Image, ImageDraw

            image = Image.new("RGB", (self.width, self.height), (18, 18, 30))
            draw = ImageDraw.Draw(image)
            draw.text((16, 16), f"SUPERUSE SIMULATED #{self._seq}", fill=(220, 220, 220))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=80)
            return buffer.getvalue()
        except ImportError:
            # Dependency-free fallback: raw BGR rows with a moving stripe.
            row = bytearray()
            for x in range(self.width):
                row += bytes(((x + self._seq) % 256, 60, 90))
            return (bytes(row) * self.height)[: self.width * self.height * 3]

    def _fmt(self) -> str:
        try:
            import PIL  # noqa: F401

            return "jpeg"
        except ImportError:
            return "bgr"

    def _run(self) -> None:
        if self.start_delay_s:
            time.sleep(self.start_delay_s)
        interval = 1.0 / max(self.fps, 0.1)
        # "hang" mode ignores the stop event, simulating a stuck native read
        # that Python cannot force-kill (DESIGN.md §3).
        while not (self._stop.is_set() and self.close_mode != "hang"):
            if self.stall_after is not None and self._seq >= self.stall_after:
                time.sleep(0.05)  # stall: keep the thread, stop producing frames
                continue
            self._seq += 1
            frame = Frame(
                data=self._render(),
                fmt=self._fmt(),
                width=self.width,
                height=self.height,
                sequence=self._seq,
                monotonic=time.monotonic(),
                captured_at=Frame.now_iso(),
            )
            self.latest = frame
            self.frames.append(frame)
            if self.on_frame is not None:
                try:
                    self.on_frame(frame)
                except Exception:  # callback isolation
                    pass
            time.sleep(interval)
