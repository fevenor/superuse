"""LAN browser entry against the simulated broker (task 2026-10-17 T5).

Covers the key login chain (cookie -> page session -> WS), the absent
management endpoints, origin/host checks and generation invalidation.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import closing
from pathlib import Path

import aiohttp
import pytest

from superuse.app import Broker, default_web_dir
from superuse.config import load_config
from superuse.server.lan import LAN_BROWSER_COOKIE, LanApi, LanWebServer


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def lan_stack(tmp_path: Path):
    config = load_config(overrides={"device": {"mode": "simulated"}, "network": {"key": "a"}})
    broker = Broker(config, runtime_dir=tmp_path / "runtime", web_dir=default_web_dir())
    await broker.start()
    await broker.wait_healthy(timeout_s=10)
    broker.network.set_enabled(True)
    server = LanWebServer(
        LanApi(
            broker.api,
            lan_identities=broker.lan_identities,
            authority=broker.network,
            web_port=0,
        )
    )
    port = free_port()
    server.api.web_port = port
    server.api.port = port
    await server.start(host="127.0.0.1", port=port)
    base = f"http://127.0.0.1:{port}"
    try:
        yield broker, server, base
    finally:
        await server.stop()
        await broker.stop()


async def login(http: aiohttp.ClientSession, base: str, key: str = "a") -> dict:
    """POST /lan/login and read everything inside the response lifetime."""
    async with http.post(
        f"{base}/lan/login",
        json={"key": key},
        headers={"Origin": base, "Host": base.split("//", 1)[1]},
    ) as response:
        result = {"status": response.status, "cookie": None, "cookie_properties": {}}
        if LAN_BROWSER_COOKIE in response.cookies:
            morsel = response.cookies[LAN_BROWSER_COOKIE]
            result["cookie_properties"] = {
                key: morsel[key]
                for key in ("httponly", "samesite", "secure", "path")
                if morsel[key]
            }
        if response.status == 200:
            result["body"] = await response.json()
            result["cookie"] = response.cookies[LAN_BROWSER_COOKIE].value
        return result


async def page_session(http: aiohttp.ClientSession, base: str, cookie: str, csrf: str) -> dict:
    async with http.post(
        f"{base}/v1/sessions/page",
        json={"label": "lan-test"},
        headers={
            "Cookie": f"{LAN_BROWSER_COOKIE}={cookie}",
            "X-CSRF-Token": csrf,
            "Origin": base,
        },
    ) as response:
        assert response.status == 200, await response.text()
        return await response.json()


# ----------------------------------------------------------------------
# Unauthenticated surface
# ----------------------------------------------------------------------


async def test_unauthenticated_requests_are_rejected_and_management_absent(lan_stack):
    _, _, base = lan_stack
    async with aiohttp.ClientSession() as http:
        for method, path in (
            ("GET", "/v1/state"),
            ("GET", "/v1/status"),
            ("POST", "/v1/control/takeover"),
            ("GET", "/v1/frames/latest"),
        ):
            async with http.request(method, f"{base}{path}") as response:
                assert response.status == 401, (path, response.status)
                body = await response.json()
                assert body["error"]["code"] == "AUTH_FAILED"
        # Local-management endpoints do not exist on the LAN listener.
        absent = (
            "/v1/ui/launch",
            "/v1/sessions/agent",
            "/v1/sessions/browser",
            "/v1/browser/context",
        )
        for path in absent:
            async with http.post(f"{base}{path}", json={}) as response:
                assert response.status == 404, path
        # Static login page is served without authentication.
        async with http.get(f"{base}/") as response:
            assert response.status == 200
            html = await response.text()
            assert "loginForm" in html


async def test_bad_origin_and_host_are_rejected(lan_stack):
    _, _, base = lan_stack
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{base}/lan/login", json={"key": "a"}, headers={"Origin": "http://evil.example"}
        ) as response:
            assert response.status == 403
        async with http.get(
            f"{base}/lan/context", headers={"Host": "evil.example:1234"}
        ) as response:
            assert response.status == 403


async def test_agent_bearers_are_not_accepted_on_the_web_entry(lan_stack):
    broker, _, base = lan_stack
    # Even a genuine broker agent token must not work here.
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{base}/v1/sessions/agent",
            json={"label": "x"},
            headers={
                "Authorization": f"Bearer {broker.credentials.agent_bootstrap}",
            },
        ) as response:
            assert response.status == 404  # route absent by design


# ----------------------------------------------------------------------
# Login chain
# ----------------------------------------------------------------------


async def test_login_chain_takeover_and_handback(lan_stack):
    broker, server, base = lan_stack
    async with aiohttp.ClientSession() as http:
        wrong = await login(http, base, key="wrong")
        assert wrong["status"] == 401
        logged_in = await login(http, base)
        assert logged_in["status"] == 200
        context = logged_in["body"]
        cookie = logged_in["cookie"]
        assert cookie
        properties = logged_in["cookie_properties"]
        assert properties.get("httponly")
        assert properties.get("samesite")
        assert "secure" not in properties  # HTTP cookie must stay sendable
        assert context["csrf_token"]

        grant = await page_session(http, base, cookie, context["csrf_token"])
        page_token = grant["page_token"]
        headers = {
            "Cookie": f"{LAN_BROWSER_COOKIE}={cookie}",
            "X-Superuse-Page": page_token,
            "X-CSRF-Token": context["csrf_token"],
            "Origin": base,
        }

        # State, status and a frame work through the LAN entrance.
        async with http.get(f"{base}/v1/state", headers=headers) as response:
            assert response.status == 200
            state = await response.json()
            assert state["source"] == "simulated"
        async with http.get(f"{base}/v1/frames/latest", headers=headers) as response:
            assert response.status == 200

        # CSRF is enforced for state-changing routes.
        async with http.post(
            f"{base}/v1/control/takeover",
            json={"request_id": "5" * 36},
            headers={k: v for k, v in headers.items() if k != "X-CSRF-Token"},
        ) as response:
            assert response.status == 403

        async with http.post(
            f"{base}/v1/control/takeover", json={"request_id": "5" * 36}, headers=headers
        ) as response:
            assert response.status == 200, await response.text()
            takeover = await response.json()
            assert takeover["lease"]
        assert broker.manager.get_state().mode.value == "human"

        async with http.post(
            f"{base}/v1/control/handback", json={"request_id": "6" * 36}, headers=headers
        ) as response:
            assert response.status == 200
        assert broker.manager.get_state().mode.value == "idle"

        # WebSockets authenticate through the same identity.
        ws_headers = {"Origin": base, "Cookie": headers["Cookie"]}
        async with http.ws_connect(f"{base}/v1/events", headers=ws_headers) as ws:
            await ws.send_json(
                {"type": "authenticate", "page_token": page_token, "csrf": context["csrf_token"]}
            )
            message = await asyncio.wait_for(ws.receive(), timeout=5)
            event = json.loads(message.data)
            assert event["state"]["instance_id"]
        async with http.ws_connect(f"{base}/v1/video", headers=ws_headers) as ws:
            await ws.send_json(
                {"type": "authenticate", "page_token": page_token, "csrf": context["csrf_token"]}
            )
            message = await asyncio.wait_for(ws.receive(), timeout=5)
            assert message.type is aiohttp.WSMsgType.BINARY


async def test_logout_and_generation_invalidation(lan_stack):
    broker, server, base = lan_stack
    async with aiohttp.ClientSession() as http:
        logged_in = await login(http, base)
        assert logged_in["status"] == 200
        context = logged_in["body"]
        cookie = logged_in["cookie"]
        grant = await page_session(http, base, cookie, context["csrf_token"])
        headers = {
            "Cookie": f"{LAN_BROWSER_COOKIE}={cookie}",
            "X-Superuse-Page": grant["page_token"],
            "X-CSRF-Token": context["csrf_token"],
            "Origin": base,
        }
        # A browser always sends its cookie; aiohttp's jar skips IP hosts, so
        # the request carries it explicitly here too.
        async with http.post(
            f"{base}/lan/logout",
            json={},
            headers={"Origin": base, "Cookie": headers["Cookie"]},
        ) as response:
            assert response.status == 200
        async with http.get(f"{base}/lan/context", headers=headers) as response:
            assert response.status == 401  # identity gone

    # Generation flip (LAN toggle) kills identities and page tokens for good.
    broker.network.set_enabled(False)
    broker.lan_identities.invalidate_all()
    for session in broker.sessions.close_entrance("lan"):
        await broker.manager.session_closed(session)
    broker.network.set_enabled(True)
    async with aiohttp.ClientSession() as http:
        fresh = await login(http, base)  # a fresh login works again
        assert fresh["status"] == 200


async def test_local_entry_still_works_alongside(lan_stack):
    broker, _, base = lan_stack
    # The loopback entrance with its own cookie namespace stays usable.
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"http://127.0.0.1:{broker.port}/v1/ui/launch",
            json={"request_id": "7" * 36},
            headers={"Authorization": f"Bearer {broker.credentials.launcher}"},
        ) as response:
            assert response.status == 200
            url = (await response.json())["url"]
        bootstrap = url.split("#bootstrap=")[1]
        async with http.post(
            f"http://127.0.0.1:{broker.port}/v1/sessions/browser",
            json={"bootstrap": bootstrap},
        ) as response:
            assert response.status == 200
            local_cookie = response.cookies["superuse_browser"].value
            context = await response.json()
        async with http.post(
            f"http://127.0.0.1:{broker.port}/v1/sessions/page",
            json={"label": "local-page"},
            headers={
                "Cookie": f"superuse_browser={local_cookie}",
                "X-CSRF-Token": context["csrf_token"],
            },
        ) as response:
            assert response.status == 200
