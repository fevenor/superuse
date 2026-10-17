"""LAN enable/disable transactions (task 2026-10-17 T6).

Covers: transactional enable (web+MCP both bind or nothing changes), port
conflict rollback restoring the local MCP, disable ordering (generation,
remote owner release, listener teardown, local MCP restored), rapid toggle
serialization and stdio/loopback unaffected throughout.
"""

from __future__ import annotations

import asyncio
import socket
import time
from contextlib import closing
from pathlib import Path

import aiohttp
import pytest

pytest.importorskip("mcp")

import httpx2  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from superuse.app import Broker, default_web_dir  # noqa: E402
from superuse.config import load_config  # noqa: E402
from superuse.server.lan import LAN_BROWSER_COOKIE  # noqa: E402


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Ports:
    def __init__(self) -> None:
        self.web = free_port()
        self.mcp = free_port()


def make_config(key: str, ports: Ports):
    return load_config(
        overrides={
            "device": {"mode": "simulated"},
            "network": {
                "key": key,
                "web_port": ports.web,
                "mcp_port": ports.mcp,
                "lan_host": "127.0.0.1",  # stands in for the LAN in tests
            },
        }
    )


@pytest.fixture
async def lan_broker(tmp_path: Path):
    ports = Ports()
    broker = Broker(
        make_config("a", ports), runtime_dir=tmp_path / "runtime", web_dir=default_web_dir()
    )
    await broker.start()
    await broker.wait_healthy(timeout_s=10)
    broker.test_ports = ports  # type: ignore[attr-defined]
    try:
        yield broker
    finally:
        await broker.stop()


def port_open(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


async def mcp_call(url: str, tool: str, arguments: dict | None = None, key: str = "a"):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=10.0)
    transport = streamable_http_client(url, http_client=http)
    cm = await transport.__aenter__()
    session = ClientSession(cm[0], cm[1])
    await session.__aenter__()
    try:
        await session.initialize()
        result = await session.call_tool(tool, arguments or {})
        return result
    finally:
        await session.__aexit__(None, None, None)
        await transport.__aexit__(None, None, None)


async def lan_page_takeover(broker: Broker, ports: Ports) -> dict:
    base = f"http://127.0.0.1:{ports.web}"
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{base}/lan/login", json={"key": "a"}) as response:
            assert response.status == 200
            context = await response.json()
            cookie = response.cookies[LAN_BROWSER_COOKIE].value
        headers = {
            "Cookie": f"{LAN_BROWSER_COOKIE}={cookie}",
            "X-CSRF-Token": context["csrf_token"],
            "Origin": base,
        }
        async with http.post(
            f"{base}/v1/sessions/page", json={"label": "remote"}, headers=headers
        ) as response:
            grant = await response.json()
        headers["X-Superuse-Page"] = grant["page_token"]
        async with http.post(
            f"{base}/v1/control/takeover", json={"request_id": "1" * 36}, headers=headers
        ) as response:
            assert response.status == 200
            return await response.json()


# ----------------------------------------------------------------------


async def test_enable_without_key_fails_closed(tmp_path: Path):
    ports = Ports()
    broker = Broker(
        make_config("", ports), runtime_dir=tmp_path / "runtime", web_dir=default_web_dir()
    )
    await broker.start()
    await broker.wait_healthy(timeout_s=10)
    try:
        result = await broker.enable_lan()
        assert result == {"ok": False, "error": "key_missing"}
        assert broker.lan_web is None
        assert not broker.network.enabled
        assert not port_open(ports.web)
    finally:
        await broker.stop()


async def test_enable_binds_both_entries_and_disable_tears_down(lan_broker):
    broker = lan_broker
    ports = broker.test_ports

    result = await broker.enable_lan()
    assert result["ok"], result
    assert broker.network.enabled
    assert port_open(ports.web)
    assert broker.http_mcp is not None
    assert broker.http_mcp.host == "127.0.0.1"

    # Both entrances work: remote page takes control; MCP answers.
    takeover = await lan_page_takeover(broker, ports)
    assert takeover["lease"]
    assert broker.manager.get_state().owner is not None
    status = await mcp_call(f"http://127.0.0.1:{ports.mcp}/mcp", "get_status")
    assert not status.is_error

    # Disable: remote owner releases, listeners close, local MCP restored.
    result = await broker.disable_lan()
    assert result["ok"]
    assert not broker.network.enabled
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and broker.manager.get_state().owner is not None:
        await asyncio.sleep(0.1)
    assert broker.manager.get_state().owner is None
    for _ in range(50):
        if not port_open(ports.web):
            break
        await asyncio.sleep(0.1)
    assert not port_open(ports.web)
    assert broker.http_mcp is not None  # local MCP came back
    status = await mcp_call(f"http://127.0.0.1:{ports.mcp}/mcp", "get_status")
    assert not status.is_error


async def test_remote_lease_and_cookie_die_with_disable(lan_broker):
    broker = lan_broker
    ports = broker.test_ports
    await broker.enable_lan()
    base = f"http://127.0.0.1:{ports.web}"
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{base}/lan/login", json={"key": "a"}) as response:
            context = await response.json()
            cookie = response.cookies[LAN_BROWSER_COOKIE].value
        headers = {
            "Cookie": f"{LAN_BROWSER_COOKIE}={cookie}",
            "X-CSRF-Token": context["csrf_token"],
            "Origin": base,
        }
        async with http.post(
            f"{base}/v1/sessions/page", json={"label": "remote"}, headers=headers
        ) as response:
            grant = await response.json()
        headers["X-Superuse-Page"] = grant["page_token"]
        await lan_page_takeover(broker, ports)
        await broker.disable_lan()
        # Re-enabling must not resurrect the old cookie or page token.
        await broker.enable_lan()
        async with http.get(f"{base}/lan/context", headers=headers) as response:
            assert response.status == 401
        async with http.post(
            f"{base}/v1/control/takeover",
            json={"request_id": "2" * 36},
            headers=headers,
        ) as response:
            assert response.status == 401


async def test_web_port_conflict_rolls_back_and_keeps_local_mcp(lan_broker, monkeypatch):
    broker = lan_broker
    ports = broker.test_ports
    local_mcp_before = broker.http_mcp

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", ports.web))
    blocker.listen(1)
    try:
        result = await broker.enable_lan()
        assert not result["ok"]
        assert result["error"] == "bind_error"
        assert broker.lan_web is None
        assert not broker.network.enabled
        # The untouched local MCP keeps serving (never rebound on a web failure).
        assert broker.http_mcp is local_mcp_before
        status = await mcp_call(f"http://127.0.0.1:{ports.mcp}/mcp", "get_status")
        assert not status.is_error
    finally:
        blocker.close()


async def test_mcp_port_conflict_rolls_back_web_too(lan_broker):
    broker = lan_broker
    ports = broker.test_ports

    # Stop the local MCP first so the port is free for the blocker; the LAN
    # rebind must then fail and roll the whole transaction back.
    local = broker.http_mcp
    assert local is not None
    await local.stop()
    broker.http_mcp = None

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", ports.mcp))
    blocker.listen(1)
    try:
        result = await broker.enable_lan()
        assert not result["ok"]
        assert result["error"] == "bind_error"
        assert broker.lan_web is None  # web listener rolled back
        assert not broker.network.enabled
        for _ in range(50):
            if not port_open(ports.web):
                break
            await asyncio.sleep(0.1)
        assert not port_open(ports.web)
        # The rollback could not restore the local MCP (the blocker holds the
        # port) and said so instead of faking success.
        assert broker.http_mcp is None
        assert broker.network.last_bind_error
    finally:
        blocker.close()
        # Once the port is free again the local binding comes back.
        await broker._restore_local_mcp()
        assert broker.http_mcp is not None
        status = await mcp_call(f"http://127.0.0.1:{ports.mcp}/mcp", "get_status")
        assert not status.is_error


async def test_rapid_toggles_serialize(lan_broker):
    broker = lan_broker
    toggles = [broker.enable_lan() for _ in range(3)] + [broker.disable_lan() for _ in range(3)]
    results = await asyncio.gather(*toggles)
    assert all(r.get("ok") for r in results)
    # Whatever the interleaving, the final state is consistent.
    await broker.enable_lan()
    assert broker.network.enabled and broker.lan_web is not None
    await broker.disable_lan()
    assert not broker.network.enabled and broker.lan_web is None


async def test_local_stdio_bridge_still_works_across_toggle(lan_broker):
    """The loopback stdio path keeps its single-session semantics (I03)."""
    broker = lan_broker
    from superuse.mcp.broker_client import BrokerClient

    client = BrokerClient(runtime_dir=broker.runtime_dir, label="stdio-toggle-test")
    await client.start()
    try:
        grant = await client.acquire()
        assert grant["lease"]
        await broker.enable_lan()
        await broker.disable_lan()
        # The stdio client's lease survives LAN toggles (it is a local owner).
        status = await client.get_status()
        assert status["state"]["owner"]["label"] == "stdio-toggle-test"
        await client.release()
    finally:
        await client.close()
