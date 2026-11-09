from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_hardware(request, monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    if (
        request.node.get_closest_marker("hardware")
        and os.getenv("SUPERUSE_ALLOW_HARDWARE_TESTS") == "1"
    ):
        return
    from superuse.device.capture import CaptureWorker
    from superuse.device.transport import RealSerialTransport

    def forbidden(*args, **kwargs):
        pytest.fail("Default tests must not open physical hardware", pytrace=False)

    monkeypatch.setattr(RealSerialTransport, "open", forbidden)
    monkeypatch.setattr(CaptureWorker, "_open", forbidden)
