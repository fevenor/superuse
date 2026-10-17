"""D04 simulated capture geometry, stall and close behaviour."""

from __future__ import annotations

import threading
import time

from superuse.device.simulated import SimulatedCapture


def count_named_threads(name: str) -> int:
    return sum(1 for t in threading.enumerate() if t.name == name)


def wait_for(condition, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_frames_geometry_change_and_stall():
    cap = SimulatedCapture(fps=50.0, width=64, height=48)
    cap.start()
    try:
        assert wait_for(lambda: cap.latest is not None)
        assert (cap.latest.width, cap.latest.height) == (64, 48)

        cap.set_size(96, 54)
        assert wait_for(lambda: cap.latest is not None and cap.latest.width == 96)
        assert cap.latest.height == 54
        assert cap.latest.fmt in ("bgr", "jpeg")

        # Wait until production has certainly passed the stall marker, then
        # assert nothing newer appears (avoids racing the producer thread).
        marker = cap.latest.sequence
        cap.stall_after = marker
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and cap.latest.sequence == marker:
            time.sleep(0.02)
        stale = cap.latest
        time.sleep(0.3)
        assert cap.latest is stale  # stalled: no newer frames, no fake image
    finally:
        cap.stop()


def test_close_is_bounded_and_no_thread_leak():
    for _ in range(3):
        cap = SimulatedCapture(fps=100.0, width=32, height=32)
        cap.start()
        assert wait_for(lambda c=cap: c.latest is not None)
        cap.stop()
    assert count_named_threads("simulated-capture") == 0


def test_hung_close_marks_fault_without_second_thread():
    cap = SimulatedCapture(fps=200.0, width=16, height=16, close_mode="hang")
    cap.start()
    assert wait_for(lambda: cap.latest is not None)
    original = cap._thread
    cap.stop()
    assert cap.fault == "close_hung"
    # The stuck thread stays (daemon); the source refuses to spawn another.
    assert original is not None and original.is_alive()
    before = count_named_threads("simulated-capture")
    cap.start()  # no-op: thread slot occupied
    assert count_named_threads("simulated-capture") == before
