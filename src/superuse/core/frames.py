"""Coordinate basis registry.

A basis binds one (instance, capture generation, geometry, encoded size)
triple so clients can turn coordinates in *the image they see* into HID
absolute coordinates (docs/development/api-contract.md section 5). Basis identity is
reused while the key stays valid; entries are LRU-bounded and expire after
``unused_ttl_s`` without a touch.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from superuse.core.errors import ControlError, ErrorCode

HID_MAX = 4095


@dataclass(frozen=True)
class Basis:
    basis_id: str
    instance_id: str
    capture_generation: int
    geometry_id: str
    image_width: int
    image_height: int
    native_width: int
    native_height: int


class BasisRegistry:
    def __init__(self, instance_id: str, *, capacity: int = 64, unused_ttl_s: float = 60.0) -> None:
        self._instance_id = instance_id
        self._capacity = capacity
        self._unused_ttl_s = unused_ttl_s
        self._by_id: OrderedDict[str, tuple[Basis, float]] = OrderedDict()
        self._key_to_id: dict[tuple, str] = {}

    def key(self, capture_generation: int, geometry_id: str, width: int, height: int) -> tuple:
        return (self._instance_id, capture_generation, geometry_id, width, height)

    def register(
        self,
        capture_generation: int,
        geometry_id: str,
        image_width: int,
        image_height: int,
        native_width: int,
        native_height: int,
        now: float,
    ) -> Basis:
        """Return the basis for this key, creating or refreshing it."""
        key = self.key(capture_generation, geometry_id, image_width, image_height)
        basis_id = self._key_to_id.get(key)
        if basis_id is not None and basis_id in self._by_id:
            basis, _ = self._by_id[basis_id]
            self._by_id.move_to_end(basis_id)
            self._by_id[basis_id] = (basis, now)
            return basis
        basis = Basis(
            basis_id=str(uuid.uuid4()),
            instance_id=self._instance_id,
            capture_generation=capture_generation,
            geometry_id=geometry_id,
            image_width=image_width,
            image_height=image_height,
            native_width=native_width,
            native_height=native_height,
        )
        self._insert(basis, now)
        return basis

    def _insert(self, basis: Basis, now: float) -> None:
        if len(self._by_id) >= self._capacity:
            self._evict_one()
        self._by_id[basis.basis_id] = (basis, now)
        key = self.key(
            basis.capture_generation, basis.geometry_id, basis.image_width, basis.image_height
        )
        self._key_to_id[key] = basis.basis_id

    def _evict_one(self) -> None:
        # LRU: drop least-recently used entries first; all entries share the
        # same capacity so eviction may remove a still-fresh basis — callers
        # then get STALE_BASIS and re-screenshot, which is the documented
        # client action.
        victim_id, _ = next(iter(self._by_id.items()))
        self._remove(victim_id)

    def _remove(self, basis_id: str) -> None:
        entry = self._by_id.pop(basis_id, None)
        if entry is None:
            return
        basis, _ = entry
        key = self.key(
            basis.capture_generation, basis.geometry_id, basis.image_width, basis.image_height
        )
        if self._key_to_id.get(key) == basis_id:
            del self._key_to_id[key]

    def resolve(self, basis_id: str, now: float | None = None) -> Basis:
        entry = self._by_id.get(basis_id)
        if entry is None:
            raise ControlError(ErrorCode.STALE_BASIS, "Unknown or recycled basis; capture again")
        basis, last_used = entry
        if now is not None and last_used is not None and (now - last_used) > self._unused_ttl_s:
            self._remove(basis_id)
            raise ControlError(ErrorCode.STALE_BASIS, "Basis expired; capture again")
        self._by_id.move_to_end(basis_id)
        self._by_id[basis_id] = (basis, now if now is not None else last_used)
        return basis

    def touch(self, basis_id: str, now: float) -> None:
        if basis_id in self._by_id:
            basis, _ = self._by_id[basis_id]
            self._by_id.move_to_end(basis_id)
            self._by_id[basis_id] = (basis, now)

    def invalidate_generation(self, capture_generation: int) -> None:
        """Drop every basis of a capture generation after geometry changes."""
        for basis_id in [
            basis_id
            for basis_id, (basis, _) in self._by_id.items()
            if basis.capture_generation == capture_generation
        ]:
            self._remove(basis_id)

    def sweep(self, now: float) -> int:
        """Remove entries unused past the TTL; returns how many were dropped."""
        expired = [
            basis_id
            for basis_id, (_, used) in self._by_id.items()
            if (now - used) > self._unused_ttl_s
        ]
        for basis_id in expired:
            self._remove(basis_id)
        return len(expired)

    def __len__(self) -> int:
        return len(self._by_id)


def image_to_hid(value: float, image_size: int, what: str) -> int:
    """Map a pixel coordinate in the seen image to the 0..4095 HID range.

    ``floor(pixel * 4096 / image_size)`` with the last pixel clamped to 4095;
    out-of-range or non-finite values raise COORDINATE_OUT_OF_BOUNDS instead
    of silently clamping a wrong point.
    """
    if value != value or value in (float("inf"), float("-inf")):
        raise ControlError(ErrorCode.COORDINATE_OUT_OF_BOUNDS, f"{what}: non-finite coordinate")
    if value < 0 or value >= image_size:
        raise ControlError(
            ErrorCode.COORDINATE_OUT_OF_BOUNDS, f"{what}: {value} outside 0..{image_size - 1}"
        )
    hid = int(value * 4096 / image_size)
    return min(hid, HID_MAX)


def map_point(basis: Basis, x: float, y: float) -> tuple[int, int]:
    """Image-pixel point -> HID absolute coordinates for this basis."""
    return (
        image_to_hid(x, basis.image_width, "x"),
        image_to_hid(y, basis.image_height, "y"),
    )


class FrameTimeout(ControlError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.FRAME_TIMEOUT, message)


@dataclass(frozen=True)
class PublishedFrame:
    frame_id: int
    capture_generation: int
    geometry_id: str
    data: bytes
    fmt: str
    width: int
    height: int
    sequence: int
    monotonic: float
    captured_at: str


class FrameHub:
    """Latest-frame store with shared encode cache and basis registration.

    One capture generation feeds every consumer: the human video stream and
    agent snapshots share the same encoding of the same frame
    (docs/development/device-video.md §4). All methods run on the event loop thread; the
    capture thread bridges in via ``call_soon_threadsafe``.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        bases: BasisRegistry,
        source: str,
        snapshot_max_width: int = 1280,
        jpeg_quality: int = 80,
        encode_cache_limit: int = 3,
    ) -> None:
        self._instance_id = instance_id
        self._bases = bases
        self._source = source
        self._snapshot_max_width = snapshot_max_width
        self._jpeg_quality = jpeg_quality
        self._cache_limit = encode_cache_limit
        self._frame: PublishedFrame | None = None
        self._frame_id = 0
        self._capture_generation = 0
        self._geometry_id: str | None = None
        self._new_frame_event: Any = None
        self._encode_cache: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
        self._cv2 = None

    # -- publish --------------------------------------------------------------

    @property
    def latest(self) -> PublishedFrame | None:
        return self._frame

    @property
    def capture_generation(self) -> int:
        return self._capture_generation

    def begin_capture_generation(self) -> int:
        """Start (or restart) the capture source; bumps the generation."""
        self._bases.invalidate_generation(self._capture_generation)
        self._capture_generation += 1
        self._geometry_id = None
        self._frame = None
        return self._capture_generation

    def publish(self, frame) -> bool:
        """Accept a new frame; True when the geometry changed."""
        import uuid as _uuid

        previous = self._frame
        changed = previous is None or (previous.width, previous.height) != (
            frame.width,
            frame.height,
        )
        if changed:
            self._geometry_id = str(_uuid.uuid4())
            if previous is not None:
                self._bases.invalidate_generation(self._capture_generation)
        self._frame_id += 1
        self._frame = PublishedFrame(
            frame_id=self._frame_id,
            capture_generation=self._capture_generation,
            geometry_id=self._geometry_id or "",
            data=frame.data,
            fmt=frame.fmt,
            width=frame.width,
            height=frame.height,
            sequence=frame.sequence,
            monotonic=frame.monotonic,
            captured_at=frame.captured_at,
        )
        if self._new_frame_event is not None:
            self._new_frame_event.set()
        return changed

    def frame_age(self, now: float) -> float | None:
        if self._frame is None:
            return None
        return now - self._frame.monotonic

    def _ensure_event(self) -> Any:
        import asyncio

        if self._new_frame_event is None:
            self._new_frame_event = asyncio.Event()
        return self._new_frame_event

    # -- snapshots ------------------------------------------------------------

    async def snapshot(
        self,
        *,
        now: Callable[[], float],
        freshness_s: float,
        max_width: int | None = None,
        after_frame_id: int | None = None,
        wait_ms: int = 2000,
    ) -> tuple[dict, bytes]:
        """Encoded snapshot plus FrameMetadata; raises FRAME_TIMEOUT.

        A qualifying frame must be newer than ``after_frame_id`` and within
        the freshness budget; ``wait_ms=0`` never waits. No stale fallback.
        """
        import asyncio

        loop = asyncio.get_running_loop()
        width_limit = max_width or self._snapshot_max_width
        event = self._ensure_event()
        deadline = loop.time() + max(0, wait_ms) / 1000
        while True:
            frame = self._frame
            if frame is not None:
                fresh = (now() - frame.monotonic) <= freshness_s
                newer = after_frame_id is None or frame.frame_id > after_frame_id
                if fresh and newer:
                    return self._build_snapshot(frame, width_limit, now())
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise FrameTimeout("No fresh frame within the wait budget")
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                raise FrameTimeout("No fresh frame within the wait budget") from None

    def publish_snapshot(
        self,
        *,
        now: float,
        freshness_s: float,
        max_width: int | None = None,
        jpeg_quality: int | None = None,
    ) -> tuple[dict, bytes] | None:
        """Synchronous latest-frame encoding for the video stream (no wait)."""
        frame = self._frame
        if frame is None or (now - frame.monotonic) > freshness_s:
            return None
        return self._build_snapshot(
            frame, max_width or self._snapshot_max_width, now, jpeg_quality=jpeg_quality
        )

    def _build_snapshot(
        self,
        frame: PublishedFrame,
        width_limit: int,
        now: float,
        *,
        jpeg_quality: int | None = None,
    ) -> tuple[dict, bytes]:
        import time as _time

        jpeg = self._encode(frame, width_limit, jpeg_quality=jpeg_quality)
        image_width = min(frame.width, width_limit)
        image_height = max(1, round(frame.height * image_width / frame.width))
        basis = self._bases.register(
            capture_generation=frame.capture_generation,
            geometry_id=frame.geometry_id,
            image_width=image_width,
            image_height=image_height,
            native_width=frame.width,
            native_height=frame.height,
            now=now,
        )
        metadata = {
            "api_version": 1,
            "instance_id": self._instance_id,
            "frame_id": frame.frame_id,
            "capture_generation": frame.capture_generation,
            "geometry_id": frame.geometry_id,
            "basis_id": basis.basis_id,
            "source": self._source,
            "image_width": image_width,
            "image_height": image_height,
            "native_width": frame.width,
            "native_height": frame.height,
            "captured_at": frame.captured_at,
            "age_ms": max(0.0, (_time.monotonic() - frame.monotonic) * 1000),
            "mime_type": "image/jpeg",
        }
        return metadata, jpeg

    # -- encoding ---------------------------------------------------------------

    def _load_cv2(self):
        if self._cv2 is None:
            import cv2

            self._cv2 = cv2
        return self._cv2

    def _encode(
        self, frame: PublishedFrame, width_limit: int, *, jpeg_quality: int | None = None
    ) -> bytes:
        quality = self._jpeg_quality if jpeg_quality is None else jpeg_quality
        target_width = min(frame.width, width_limit)
        if target_width == frame.width and frame.fmt == "jpeg":
            return frame.data
        key = (frame.frame_id, target_width, quality)
        cached = self._encode_cache.get(key)
        if cached is not None:
            self._encode_cache.move_to_end(key)
            return cached
        cv2 = self._load_cv2()
        import numpy as np

        if frame.fmt == "jpeg":
            image = cv2.imdecode(np.frombuffer(frame.data, dtype=np.uint8), cv2.IMREAD_COLOR)
        else:
            image = np.frombuffer(frame.data, dtype=np.uint8).reshape(
                (frame.height, frame.width, 3)
            )
        if image is None or image.size == 0:
            raise ControlError(ErrorCode.VIDEO_UNAVAILABLE, "Frame could not be decoded")
        if target_width != frame.width:
            target_height = max(1, round(frame.height * target_width / frame.width))
            image = cv2.resize(image, (target_width, target_height), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise ControlError(ErrorCode.VIDEO_UNAVAILABLE, "Frame encoding failed")
        data = encoded.tobytes()
        self._encode_cache[key] = data
        while len(self._encode_cache) > self._cache_limit:
            self._encode_cache.popitem(last=False)
        return data
