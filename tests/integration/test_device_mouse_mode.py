"""Device mouse coordinate-mode endpoint, state surfacing and LAN exclusion.

Simulated broker only: the real write path needs hardware (acceptance list A,
task 2026-10-17 doc). What is verifiable here: route presence, auth/CSRF,
body validation, simulated-source rejection and the state field.
"""

from __future__ import annotations

import time

import pytest

from tests.integration.conftest import browser_page

aiohttp = pytest.importorskip("aiohttp")


def _headers(page: dict) -> dict:
    return {
        "Cookie": f"superuse_browser={page['cookie']}",
        "X-Superuse-Page": page["page_token"],
        "X-CSRF-Token": page["csrf"],
    }


async def test_mouse_mode_endpoint_auth_and_validation(broker, base_url):
    page = await browser_page(base_url, broker.sessions.issue_bootstrap(time.monotonic()))
    headers = _headers(page)
    async with aiohttp.ClientSession() as http:
        # Simulated source has no device mouse mode: explicit, not a write.
        async with http.post(
            f"{base_url}/v1/device/mouse-mode", json={"mode": "absolute"}, headers=headers
        ) as response:
            assert response.status == 400
            body = await response.json()
            assert body["error"]["code"] == "INVALID_REQUEST"

        async with http.post(
            f"{base_url}/v1/device/mouse-mode", json={"mode": "pixel"}, headers=headers
        ) as response:
            assert response.status == 400

        no_csrf = {k: v for k, v in headers.items() if k != "X-CSRF-Token"}
        async with http.post(
            f"{base_url}/v1/device/mouse-mode", json={"mode": "absolute"}, headers=no_csrf
        ) as response:
            assert response.status == 403

        no_auth = {}
        async with http.post(
            f"{base_url}/v1/device/mouse-mode", json={"mode": "absolute"}, headers=no_auth
        ) as response:
            assert response.status == 401


async def test_state_surfaces_device_mouse_mode(broker, base_url):
    page = await browser_page(base_url, broker.sessions.issue_bootstrap(time.monotonic()))
    headers = _headers(page)
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base_url}/v1/state", headers=headers) as response:
            assert response.status == 200
            assert "device_mouse_mode" not in await response.json()  # unknown -> omitted

        broker.manager.set_device_mouse_mode("relative")
        async with http.get(f"{base_url}/v1/state", headers=headers) as response:
            state = await response.json()
            assert state["device_mouse_mode"] == "relative"


def test_lan_route_table_excludes_device_mode_write(broker):
    """The endpoint is local-only by construction, not by runtime check."""
    local_paths = {r.canonical for r in broker.api.build_app().router.resources()}
    assert "/v1/device/mouse-mode" in local_paths

    from superuse.server.lan import LanApi

    lan_api = LanApi(
        broker.api,
        lan_identities=broker.lan_identities,
        authority=broker.network,
        web_port=broker.config.network["web_port"],
    )
    lan_paths = {r.canonical for r in lan_api.build_lan_app().router.resources()}
    assert "/v1/device/mouse-mode" not in lan_paths
    assert "/v1/control/takeover" in lan_paths  # the control subset itself is present


async def test_status_device_block(broker, base_url):
    """Sanitized device diagnostics surface through /v1/status."""
    page = await browser_page(base_url, broker.sessions.issue_bootstrap(time.monotonic()))
    headers = _headers(page)
    broker.actor.device_info = {
        "mouse_coordinate_mode": "absolute",
        "mouse_coordinate_mode_raw": 2,
        "firmware": "0.9.16",
        "hardware": "1.1.0",
        "bootloader": "0.9.8",
        "temperature_c": 38.75,
        "run_state_raw": 3,
        "video_low_power_enable_raw": 0,
        "usb_vid": "0x1209",
        "usb_pid": "0x1",
    }
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base_url}/v1/status", headers=headers) as response:
            status = await response.json()
        assert status["device"]["mouse_mode"] == "absolute"
        assert status["device"]["firmware"] == "0.9.16"
        assert status["device"]["video_low_power_enabled"] is False
        assert "serial_number" not in status["device"]


async def test_device_rejection_maps_to_serial_write_failed(broker, monkeypatch):
    """A device-rejected write (rsp_code != 0) maps to SERIAL_WRITE_FAILED."""
    import pytest

    from superuse.core.errors import ControlError
    from superuse.core.ports import WriteOutcome

    monkeypatch.setitem(broker.config.raw["device"], "mode", "real")

    async def rejected(mode, **kwargs):
        return WriteOutcome(0, "REJECTED:5")

    monkeypatch.setattr(broker.actor, "set_mouse_mode", rejected)
    with pytest.raises(ControlError) as exc:
        await broker.set_device_mouse_mode(2)
    assert exc.value.code.value == "SERIAL_WRITE_FAILED"
    assert "rsp_code=5" in exc.value.message


async def test_restart_endpoint_and_lan_exclusion(broker, base_url):
    """Local-only restart endpoint: validation + simulated rejection."""
    import time as _time

    page = await browser_page(base_url, broker.sessions.issue_bootstrap(_time.monotonic()))
    headers = _headers(page)
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{base_url}/v1/device/restart", json={}, headers=headers) as response:
            assert response.status == 400  # simulated source has no device
            body = await response.json()
            assert body["error"]["code"] == "INVALID_REQUEST"

        no_csrf = {k: v for k, v in headers.items() if k != "X-CSRF-Token"}
        async with http.post(f"{base_url}/v1/device/restart", json={}, headers=no_csrf) as response:
            assert response.status == 403

    from superuse.server.lan import LanApi

    local_paths = {r.canonical for r in broker.api.build_app().router.resources()}
    assert "/v1/device/restart" in local_paths
    lan_api = LanApi(
        broker.api,
        lan_identities=broker.lan_identities,
        authority=broker.network,
        web_port=broker.config.network["web_port"],
    )
    lan_paths = {r.canonical for r in lan_api.build_lan_app().router.resources()}
    assert "/v1/device/restart" not in lan_paths
    assert "/v1/device/mouse-mode" not in lan_paths


async def test_broker_restart_flow_on_simulated_transport(broker, monkeypatch):
    """Full restart flow on the simulated transport: write -> handle cycle ->
    supervisor re-attach -> healthy again (mode stays unknown: simulated)."""

    monkeypatch.setitem(broker.config.raw["device"], "mode", "real")
    old_actor = broker.actor
    state = await broker.restart_device()
    assert state["serial_status"] == "ready"
    assert broker.actor is not old_actor
    # The restart frame actually went out on the (old) simulated transport.
    from superuse.device.protocol import build_device_restart_frame

    assert any(w == build_device_restart_frame() for w in old_actor._transport.writes)
