"""Integration fixtures: a fully simulated broker behind an aiohttp server.

Runs in the app-extra environment (aiohttp, cv2). No hardware is opened —
the simulated CDC transport and timestamp-pattern capture power everything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

aiohttp = pytest.importorskip("aiohttp")

from superuse.app import Broker, default_web_dir  # noqa: E402
from superuse.config import load_config  # noqa: E402


@pytest.fixture
async def broker(tmp_path: Path):
    # Explicit simulation: the default is real-hardware mode (task T1).
    config = load_config(overrides={"device": {"mode": "simulated"}})
    instance = Broker(config, runtime_dir=tmp_path / "runtime", web_dir=default_web_dir())
    await instance.start()
    await instance.wait_healthy(timeout_s=10)
    yield instance
    await instance.stop()


@pytest.fixture
def base_url(broker) -> str:
    return f"http://127.0.0.1:{broker.port}"


async def agent_session(base_url: str, bootstrap: str, label: str = "test-agent") -> dict:
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{base_url}/v1/sessions/agent",
            json={"label": label},
            headers={"Authorization": f"Bearer {bootstrap}"},
        ) as response:
            assert response.status == 200, await response.text()
            return await response.json()


async def browser_page(
    base_url: str,
    bootstrap: str | None = None,
    *,
    cookie: str | None = None,
    csrf: str | None = None,
) -> dict:
    """Full human bootstrap: exchange fragment -> context -> page session.

    Passing an existing ``cookie``/``csrf`` models a second tab of the same
    browser (the one-time bootstrap was consumed by the first tab).
    """
    async with aiohttp.ClientSession() as http:
        if cookie is None:
            assert bootstrap is not None
            async with http.post(
                f"{base_url}/v1/sessions/browser", json={"bootstrap": bootstrap}
            ) as r:
                assert r.status == 200, await r.text()
                context = await r.json()
                cookie = r.cookies["superuse_browser"].value
        else:
            context = {"csrf_token": csrf}
        async with http.post(
            f"{base_url}/v1/sessions/page",
            json={"label": "test-page"},
            headers={
                "Cookie": f"superuse_browser={cookie}",
                "X-CSRF-Token": context["csrf_token"],
            },
        ) as r:
            assert r.status == 200, await r.text()
            grant = await r.json()
    grant["cookie"] = cookie
    grant["csrf"] = context["csrf_token"]
    return grant


async def acquire(base_url: str, session: dict, request_id: str, ttl_ms: int | None = None):
    import aiohttp

    payload = {"request_id": request_id}
    if ttl_ms is not None:
        payload["ttl_ms"] = ttl_ms
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{base_url}/v1/control/acquire",
            json=payload,
            headers={"Authorization": f"Bearer {session['session_token']}"},
        ) as r:
            return r, await r.json()
