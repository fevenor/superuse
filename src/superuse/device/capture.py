"""Video capture: shared Frame model and the real CaptureWorker.

One worker owns the camera and runs one reader thread (DESIGN.md §3). A hung
native read cannot be force-killed: the worker marks itself fault and refuses
to start a second leaking thread — recovery is an application restart. The
simulated source lives in ``simulated.py``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Frame:
    """One captured frame as bytes plus its true geometry and timing.

    ``fmt`` is ``bgr`` (raw rows, cv2 layout) or ``jpeg`` (already encoded).
    The *actual* width/height are authoritative — requested sizes are never
    trusted as coordinates.
    """

    data: bytes
    fmt: str
    width: int
    height: int
    sequence: int
    monotonic: float
    captured_at: str

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).isoformat()


class CaptureWorker:
    """cv2-backed capture (app extra). No camera is touched until start()."""

    def __init__(
        self,
        *,
        backend: str,
        index: int,
        width: int,
        height: int,
        fps: int,
        on_geometry: Callable[[int, int], None] | None = None,
        on_frame: Callable[[Frame], None] | None = None,
    ) -> None:
        self._backend = backend
        self._index = index
        self._width = width
        self._height = height
        self._fps = fps
        self._on_geometry = on_geometry
        self._on_frame = on_frame

        self.latest: Frame | None = None
        self.status = "starting"
        self.fault_reason: str | None = None
        self._sequence = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap = None
        self._first_frame_budget_s = 10.0

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Capture thread already exists; stop it first")
        self._thread = threading.Thread(target=self._run, name="capture-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                # Stuck native handle: do NOT spawn a replacement thread; the
                # application must surface a restart hint (DESIGN.md §3).
                self.status = "fault"
                self.fault_reason = "capture_close_hung"
                return
        self._release()
        self._thread = None
        self.status = "disconnected"

    def _release(self) -> None:
        cap = self._cap
        self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:  # pragma: no cover - native edge cases
                pass

    def _open(self):
        import cv2

        flags = {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF}.get(self._backend)
        if flags is None:
            raise ValueError(f"Unsupported backend {self._backend!r}")
        cap = cv2.VideoCapture(self._index, flags)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"Failed to open camera index {self._index} via {self._backend}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
        cap.set(cv2.CAP_PROP_FPS, self._fps)
        self._cap = cap

    def _run(self) -> None:
        try:
            self._open()
        except Exception:
            self.status = "fault"
            self.fault_reason = "open_failed"
            return
        deadline = time.monotonic() + self._first_frame_budget_s
        consecutive_failures = 0
        last_size: tuple[int, int] | None = None
        while not self._stop.is_set():
            cap = self._cap
            if cap is None:
                return
            ok, frame = cap.read()
            if not ok or frame is None:
                if self.latest is None and time.monotonic() > deadline:
                    self.status = "fault"
                    self.fault_reason = "first_frame_timeout"
                    return
                consecutive_failures += 1
                if consecutive_failures > 120:  # ~2s of failing reads at 60Hz
                    self.status = "fault"
                    self.fault_reason = "read_failed"
                    return
                time.sleep(0.016)
                continue
            consecutive_failures = 0
            height, width = frame.shape[:2]
            self._sequence += 1
            now = time.monotonic()
            self.latest = Frame(
                data=frame.tobytes(),
                fmt="bgr",
                width=width,
                height=height,
                sequence=self._sequence,
                monotonic=now,
                captured_at=Frame.now_iso(),
            )
            if self._on_frame is not None:
                try:
                    self._on_frame(self.latest)
                except Exception:  # pragma: no cover - callback isolation
                    pass
            if last_size is not None and (width, height) != last_size:
                if self._on_geometry is not None:
                    try:
                        self._on_geometry(width, height)
                    except Exception:  # pragma: no cover - callback isolation
                        pass
            last_size = (width, height)
        self.status = "disconnected"
