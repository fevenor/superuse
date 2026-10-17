"""V02: encode-cache reuse and latest-only viewer queues (simulated frames)."""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("cv2")
pytest.importorskip("numpy")

from superuse.core.frames import BasisRegistry, FrameHub  # noqa: E402
from superuse.device.capture import Frame  # noqa: E402


def make_hub(*, cache_limit: int = 3) -> FrameHub:
    return FrameHub(
        instance_id="11111111-1111-1111-1111-111111111111",
        bases=BasisRegistry("11111111-1111-1111-1111-111111111111"),
        source="simulated",
        snapshot_max_width=1280,
        jpeg_quality=80,
        encode_cache_limit=cache_limit,
    )


def bgr_frame(hub, width: int, height: int, shade: int) -> Frame:
    import numpy as np

    data = bytes(bytearray(np.full((height, width, 3), shade, dtype=np.uint8).tobytes()))
    hub.begin_capture_generation()
    return Frame(
        data=data,
        fmt="bgr",
        width=width,
        height=height,
        sequence=1,
        monotonic=time.monotonic(),
        captured_at=Frame.now_iso(),
    )


def test_v02_same_frame_encoding_reused_across_consumers():
    hub = make_hub()
    hub.publish(bgr_frame(hub, 64, 48, 40))
    now = time.monotonic
    first = hub.publish_snapshot(now=now(), freshness_s=1.0, max_width=32)
    second = hub.publish_snapshot(now=now(), freshness_s=1.0, max_width=32)
    assert first is not None and second is not None
    # Same (frame, width, quality): the encoded bytes object is reused, so
    # multiple screenshot/video consumers share one encoding.
    assert first[1] is second[1]
    assert first[0]["basis_id"] == second[0]["basis_id"]

    wider = hub.publish_snapshot(now=now(), freshness_s=1.0, max_width=64)
    assert wider is not None and wider[1] is not first[1]  # distinct profile


def test_v02_encode_cache_is_bounded():
    hub = make_hub(cache_limit=2)
    for shade in range(5):
        frame = bgr_frame(hub, 64, 48, 10 + shade)
        hub.publish(frame)
        hub.publish_snapshot(now=time.monotonic(), freshness_s=1.0, max_width=32)
    assert len(hub._encode_cache) <= 2


def test_browser_preview_keeps_native_detail_and_separate_encoding_profile():
    import cv2
    import numpy as np

    hub = make_hub()
    hub.publish(bgr_frame(hub, 1920, 1080, 40))
    preview = hub.publish_snapshot(
        now=time.monotonic(), freshness_s=1.0, max_width=1920, jpeg_quality=90
    )
    snapshot = hub.publish_snapshot(now=time.monotonic(), freshness_s=1.0)
    assert preview is not None and snapshot is not None
    assert (preview[0]["image_width"], preview[0]["image_height"]) == (1920, 1080)
    assert cv2.imdecode(np.frombuffer(preview[1], dtype=np.uint8), cv2.IMREAD_COLOR).shape == (
        1080,
        1920,
        3,
    )
    assert snapshot[0]["image_width"] == 1280  # MCP default stays independent
    assert (hub.latest.frame_id, 1920, 90) in hub._encode_cache
    assert (hub.latest.frame_id, 1280, 80) in hub._encode_cache


async def test_v02_latest_only_viewer_queue_drops_older_frame():
    from superuse.server.websocket import VideoViewer

    viewer = VideoViewer(ws=None)
    payload_a, payload_b = b"frame-a", b"frame-b"
    viewer.offer(payload_a)
    viewer.offer(payload_b)  # a has not been consumed: replaced by b
    assert viewer.outbox.qsize() == 1
    assert await asyncio.wait_for(viewer.outbox.get(), timeout=1.0) == payload_b
    viewer.close()
    assert viewer.task is None
