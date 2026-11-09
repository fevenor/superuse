from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace

import aiohttp
import pytest

from superuse.core.errors import ControlError
from superuse.core.frames import map_point
from superuse.core.models import KeyboardReport
from superuse.core.ports import AuthorizedReport
from tests.integration.conftest import acquire, agent_session, browser_page


async def page_headers(broker, base_url):
    page = await browser_page(base_url, broker.sessions.issue_bootstrap(time.monotonic()))
    return {
        "Cookie": f"superuse_browser={page['cookie']}",
        "X-Superuse-Page": page["page_token"],
        "X-CSRF-Token": page["csrf"],
    }


async def test_control_retry_cannot_revoke_later_owner(broker, base_url):
    headers = await page_headers(broker, base_url)
    request_id = str(uuid.uuid4())
    async with aiohttp.ClientSession(headers=headers) as http:

        async def takeover():
            async with http.post(
                base_url + "/v1/control/takeover", json={"request_id": request_id}
            ) as response:
                assert response.status == 200
                return await response.json()

        first, concurrent = await asyncio.gather(takeover(), takeover())
        assert first == concurrent
        async with http.post(
            base_url + "/v1/control/handback", json={"request_id": str(uuid.uuid4())}
        ) as response:
            assert response.status == 200
        agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
        _, grant = await acquire(base_url, agent, str(uuid.uuid4()))
        retried = await takeover()
        assert retried == first
        assert broker.manager.current_lease.lease.lease_id == grant["lease"]["lease_id"]
        async with http.post(
            base_url + "/v1/control/handback", json={"request_id": request_id}
        ) as response:
            assert response.status == 409
            assert (await response.json())["error"]["code"] == "REQUEST_ID_CONFLICT"
        async with http.post(
            base_url + "/v1/control/takeover", json={"request_id": "invalid"}
        ) as response:
            assert response.status == 400


async def test_late_frames_cannot_refresh_control(broker):
    await asyncio.to_thread(broker.capture.stop)
    await asyncio.sleep(0.05)
    frame = broker.capture.latest
    previous = broker.manager._last_frame
    broker._on_frame(replace(frame, monotonic=time.monotonic() - 3))
    assert broker.manager._last_frame == previous
    broker._on_frame(replace(frame, monotonic=time.monotonic() + 1))
    assert broker.manager._last_frame == previous


async def test_executor_checks_expiry_identity_and_freshness(broker, base_url):
    agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
    _, grant = await acquire(base_url, agent, str(uuid.uuid4()))
    lease = grant["lease"]
    report = AuthorizedReport(
        agent["session_id"],
        lease["lease_id"],
        lease["generation"],
        KeyboardReport(0, ()),
        instance_id=lease["instance_id"],
    )
    count = len(broker.actor._transport.writes)
    assert (
        await broker.actor.submit(replace(report, instance_id=str(uuid.uuid4())))
    ).error == "REVOKED"
    assert (
        await broker.actor.submit(replace(report, deadline=time.monotonic() - 1))
    ).error == "ACTION_TIMEOUT"
    await asyncio.to_thread(broker.capture.stop)
    await asyncio.sleep(0.05)
    broker.manager._last_frame = time.monotonic() - 1.5
    broker.manager._refresh_write_authority()
    assert (await broker.actor.submit(report)).error == "VIDEO_STALE"
    assert len(broker.actor._transport.writes) == count


async def test_native_png_and_shared_calibrated_basis(broker, base_url):
    headers = await page_headers(broker, base_url)
    async with aiohttp.ClientSession(headers=headers) as http:
        async with http.get(base_url + "/v1/frames/screenshot.png") as response:
            assert response.status == 200
            png = await response.read()
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    frame = broker.hub.latest
    assert image.shape[:2] == (frame.height, frame.width)
    before, _ = await broker.hub.snapshot(now=time.monotonic, freshness_s=1)
    await broker.management.calibrate([100, 50, frame.width - 200, frame.height - 100])
    after, _ = await broker.hub.snapshot(now=time.monotonic, freshness_s=1)
    assert before["basis_id"] != after["basis_id"]
    basis = broker.bases.resolve(after["basis_id"])
    x = 100 * basis.image_width / frame.width
    y = 50 * basis.image_height / frame.height
    assert map_point(basis, x, y) == (0, 0)
    with pytest.raises(ControlError):
        map_point(basis, 0, 0)
    with pytest.raises(ControlError):
        broker.bases.resolve(before["basis_id"])


async def test_management_is_local_human_and_persists_power(broker, base_url, monkeypatch):
    from superuse.server.lan import LanApi

    headers = await page_headers(broker, base_url)
    monkeypatch.setattr(
        broker.power,
        "set_enabled",
        lambda value: setattr(broker.power, "_handle", 1 if value else None),
    )
    monkeypatch.setattr(broker.power, "close", lambda: setattr(broker.power, "_handle", None))
    async with aiohttp.ClientSession(headers=headers) as http:
        async with http.post(
            base_url + "/v1/device/action",
            json={"request_id": str(uuid.uuid4()), "operation": "keep_awake", "value": True},
        ) as response:
            assert response.status == 200
            assert (await response.json())["active"] is True
        async with http.get(base_url + "/v1/device/overview") as response:
            assert response.status == 200
            assert (await response.json())["power"]["keep_awake"] is True
    assert broker.preferences.values["power"]["keep_awake"] is True
    assert broker.network.enabled is False
    lan = LanApi(
        broker.api,
        lan_identities=broker.lan_identities,
        authority=broker.network,
        web_port=broker.config.network["web_port"],
    )
    paths = {r.canonical for r in lan.build_lan_app().router.resources()}
    assert "/v1/device/action" not in paths
    assert "/v1/device/overview" not in paths
    assert "/v1/frames/screenshot.png" in paths
    agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
    async with aiohttp.ClientSession() as http:
        async with http.get(
            base_url + "/v1/device/overview",
            headers={"Authorization": f"Bearer {agent['session_token']}"},
        ) as response:
            assert response.status == 401
