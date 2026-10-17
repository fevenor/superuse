"""Regressions for the 2026-10-17 implementation review (R1-R4).

R5 (migration TOML variants) lives in tests/unit/test_release_boundary.py.
All devices here are simulated or unopened.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from superuse.app import Broker, default_web_dir
from superuse.config import load_config
from superuse.mcp.http_service import HttpMcpService
from superuse.server.network import local_ipv4_addresses


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ----------------------------------------------------------------------
# R1: the HTTP MCP service must start in a windowed process (no std streams)
# ----------------------------------------------------------------------


async def test_http_mcp_starts_without_standard_streams(tmp_path, monkeypatch):
    """uvicorn's default logging probes sys.stdout.isatty(); windowed EXEs
    have stdout=None and the broker used to die on it."""
    service = HttpMcpService(runtime_dir=tmp_path, check_key=lambda raw: raw == b"a")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdout", None)
            patch.setattr(sys, "stderr", None)
            await service.start(host="127.0.0.1", port=free_port())
        assert service.running
    finally:
        await service.stop()


async def test_broker_survives_http_mcp_configuration_failure(tmp_path, monkeypatch):
    """Any HTTP MCP start failure (not just OSError) degrades the network
    surface without taking the broker down."""
    from superuse.mcp import http_service as service_module

    config = load_config(overrides={"device": {"mode": "simulated"}})
    broker = Broker(config, runtime_dir=tmp_path / "runtime", web_dir=default_web_dir())

    async def exploding_start(self, *, host, port):
        raise ValueError("Unable to configure formatter 'default'")

    monkeypatch.setattr(service_module.HttpMcpService, "start", exploding_start)
    await broker.start()  # must not raise
    try:
        assert broker.network.key_configured is False  # no key: not even attempted
        broker.network._key = "a"  # simulate a configured key mid-run
        await broker._start_local_http_mcp()
        assert broker.http_mcp is None
        assert broker.http_mcp_error
        assert broker.port  # services stayed up
    finally:
        await broker.stop()


# ----------------------------------------------------------------------
# R2: wildcard bind must accept the Host of the NIC it is reached through
# ----------------------------------------------------------------------


async def test_wildcard_mcp_accepts_real_lan_host_header(tmp_path):
    lan_addresses = [ip for ip in local_ipv4_addresses() if not ip.startswith("127.")]
    if not lan_addresses:
        pytest.skip("no non-loopback IPv4 address on this machine")
    service = HttpMcpService(runtime_dir=tmp_path, check_key=lambda raw: raw == b"a")
    port = free_port()
    await service.start(host="0.0.0.0", port=port)
    try:
        async with aiohttp.ClientSession() as http:
            for lan_ip in lan_addresses[:2]:
                async with http.post(
                    f"http://127.0.0.1:{port}/mcp",
                    headers={
                        "Authorization": "Bearer a",
                        "Host": f"{lan_ip}:{port}",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "review", "version": "1"},
                        },
                    },
                ) as response:
                    body = await response.text()
                    assert response.status == 200, (lan_ip, response.status, body)
    finally:
        await service.stop()


# ----------------------------------------------------------------------
# Shared broker fixture for R3/R4
# ----------------------------------------------------------------------


@pytest.fixture
async def key_broker(tmp_path: Path):
    config = load_config(
        overrides={
            "device": {"mode": "simulated"},
            "network": {
                "key": "a",
                "web_port": free_port(),
                "mcp_port": free_port(),
                "lan_host": "127.0.0.1",
            },
        }
    )
    broker = Broker(config, runtime_dir=tmp_path / "runtime", web_dir=default_web_dir())
    await broker.start()
    await broker.wait_healthy(timeout_s=10)
    try:
        yield broker
    finally:
        await broker.stop()


# ----------------------------------------------------------------------
# R3: LAN disable revokes HTTP MCP control BEFORE listener cleanup waits
# ----------------------------------------------------------------------


async def test_disable_lan_revokes_mcp_before_listener_cleanup(key_broker, monkeypatch):
    broker = key_broker
    await broker.enable_lan()
    cleanup_entered, resume_cleanup = asyncio.Event(), asyncio.Event()
    original_stop = broker.lan_web.stop

    async def blocked_cleanup():
        cleanup_entered.set()
        await resume_cleanup.wait()
        await original_stop()

    monkeypatch.setattr(broker.lan_web, "stop", blocked_cleanup)
    url = f"http://127.0.0.1:{broker.config.network['mcp_port']}/mcp"
    result = None
    task = None
    try:
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer a"}, timeout=10) as http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    grant = await session.call_tool("acquire_control", {})
                    assert not grant.is_error
                    task = asyncio.create_task(broker.disable_lan())
                    await asyncio.wait_for(cleanup_entered.wait(), 5)
                    assert not broker.network.enabled
                    # While the web listener is still tearing down, the old
                    # lease must already be unusable.
                    result = await session.call_tool("press_key", {"code": "KeyA"})
    finally:
        resume_cleanup.set()
        if task:
            await task
    assert result.is_error, "MCP input still succeeded after LAN generation was invalidated"
    # After the full disable, the local binding is back on the same port and
    # accepts fresh sessions again (by design; lan_host here is loopback).
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer a"}, timeout=5) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                fresh = await session.call_tool("get_status", {})
                assert not fresh.is_error


# ----------------------------------------------------------------------
# R4: LAN logout revokes page sessions, leases and live sockets
# ----------------------------------------------------------------------


async def test_lan_logout_revokes_existing_input_socket(key_broker):
    broker = key_broker
    await broker.enable_lan()
    base = f"http://127.0.0.1:{broker.config.network['web_port']}"
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{base}/lan/login", json={"key": "a"}) as response:
            csrf = (await response.json())["csrf_token"]
            cookie = response.cookies["superuse_lan"].value
        headers = {"Cookie": f"superuse_lan={cookie}", "Origin": base, "X-CSRF-Token": csrf}
        async with http.post(f"{base}/v1/sessions/page", json={}, headers=headers) as response:
            assert response.status == 200, await response.text()
            page = await response.json()
        headers["X-Superuse-Page"] = page["page_token"]
        async with http.post(
            f"{base}/v1/control/takeover",
            json={"request_id": str(uuid.uuid4())},
            headers=headers,
        ) as response:
            grant = await response.json()
        assert grant["lease"]

        # A second logged-in browser keeps its own state across the logout.
        async with http.post(f"{base}/lan/login", json={"key": "a"}) as response:
            other_csrf = (await response.json())["csrf_token"]
            other_cookie = response.cookies["superuse_lan"].value

        async with http.ws_connect(f"{base}/v1/input", headers=headers) as ws:
            await ws.send_json(
                {
                    "type": "authenticate",
                    "page_token": page["page_token"],
                    "csrf": csrf,
                }
            )
            # Establish a live stream before logout.
            await ws.send_json(
                {
                    "api_version": 1,
                    "sequence": 1,
                    "lease": grant["lease"],
                    "action": {"kind": "keyboard_state", "keys": [], "modifiers": []},
                }
            )
            for _ in range(4):
                ack = await asyncio.wait_for(ws.receive_json(), 3)
                if ack.get("status") == "written":
                    break
            assert ack.get("status") == "written", ack

            async with http.post(f"{base}/lan/logout", json={}, headers=headers) as response:
                assert response.status == 200
            async with http.get(f"{base}/lan/context", headers=headers) as response:
                assert response.status == 401

            # The logout closed this page's sockets server-side: the next
            # send either hits a closed socket or gets a rejection — never a
            # written key on the revoked lease.
            written_after_logout = False
            try:
                await ws.send_json(
                    {
                        "api_version": 1,
                        "sequence": 2,
                        "lease": grant["lease"],
                        "action": {"kind": "keyboard_state", "keys": ["KeyA"], "modifiers": []},
                    }
                )
                for _ in range(3):
                    message = await asyncio.wait_for(ws.receive(), 3)
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        break
                    payload = json.loads(message.data)
                    if payload.get("status") == "written":
                        written_after_logout = True
                        break
                    if payload.get("status") == "rejected":
                        break
            except (TimeoutError, ConnectionResetError, aiohttp.ClientError):
                pass  # server-side close racing the send
            assert not written_after_logout, "input socket still writes keys after logout"

        # The human latch semantics survive the logout revocation.
        state = broker.manager.get_state()
        assert state.owner is None
        assert state.mode.value in ("human_hold", "idle")

        # The OTHER browser can still use its own identity.
        async with http.get(
            f"{base}/lan/context",
            headers={"Cookie": f"superuse_lan={other_cookie}", "Origin": base},
        ) as response:
            assert response.status == 200
        assert other_csrf


async def test_logout_revokes_control_before_waiting_for_a_slow_socket(key_broker):
    broker = key_broker
    await broker.enable_lan()
    base = f"http://127.0.0.1:{broker.config.network['web_port']}"
    closing, resume = asyncio.Event(), asyncio.Event()

    class SlowSocket:
        async def close(self, **kwargs):
            closing.set()
            await resume.wait()

    async with aiohttp.ClientSession() as http:
        async with http.post(f"{base}/lan/login", json={"key": "a"}) as response:
            csrf = (await response.json())["csrf_token"]
            cookie = response.cookies["superuse_lan"].value
        headers = {"Cookie": f"superuse_lan={cookie}", "Origin": base, "X-CSRF-Token": csrf}
        async with http.post(f"{base}/v1/sessions/page", json={}, headers=headers) as response:
            page = await response.json()
        headers["X-Superuse-Page"] = page["page_token"]
        async with http.post(
            f"{base}/v1/control/takeover",
            json={"request_id": str(uuid.uuid4())},
            headers=headers,
        ) as response:
            assert response.status == 200
        record = broker.sessions.page_record_by_token(page["page_token"])
        record.websockets.add(SlowSocket())

        async def logout():
            async with http.post(f"{base}/lan/logout", json={}, headers=headers) as response:
                assert response.status == 200

        task = asyncio.create_task(logout())
        try:
            await asyncio.wait_for(closing.wait(), 3)
            state = broker.manager.get_state()
            revoked_before_close = (
                state.owner is None
                and state.mode.value == "human_hold"
                and broker.sessions.page_record_by_token(page["page_token"]) is None
            )
        finally:
            resume.set()
            await task
    assert revoked_before_close, "slow socket cleanup delayed session and lease revocation"


async def test_mcp_client_starting_during_shutdown_cannot_resolve(tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from superuse.mcp import http_service

    starting, resume = asyncio.Event(), asyncio.Event()
    closed = []

    class SlowClient:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            starting.set()
            await resume.wait()

        async def close(self):
            closed.append(self)

    monkeypatch.setattr(http_service, "BrokerClient", SlowClient)
    pool = http_service.HttpMcpClientPool(tmp_path)
    sealed = False
    resolve = pool.resolver(lambda: sealed)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(request=SimpleNamespace(headers={"mcp-session-id": "s"}))
    )
    task = asyncio.create_task(resolve(ctx))
    await asyncio.wait_for(starting.wait(), 3)
    sealed = True
    draining = asyncio.create_task(pool.close_all())
    resume.set()
    try:
        with pytest.raises(ToolError, match="being disabled"):
            await task
    finally:
        await draining
    assert len(closed) == 1
    assert not pool.session_ids
    with pytest.raises(ToolError, match="pool is closed"):
        await pool.client_for("late-session")
