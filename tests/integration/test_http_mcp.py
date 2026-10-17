"""Streamable HTTP MCP against the simulated broker (task 2026-10-17 T4).

Official SDK client, real uvicorn on the broker loop, simulated devices.
Covers: key admission, per-session client isolation (same-label agents),
control contention, cancellation, session teardown releasing the lease, and
rebind semantics.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from contextlib import closing
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("aiohttp")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from superuse.app import Broker, default_web_dir  # noqa: E402
from superuse.config import load_config  # noqa: E402
from superuse.mcp.http_service import build_http_mcp_service  # noqa: E402


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def key_broker(tmp_path: Path):
    config = load_config(overrides={"device": {"mode": "simulated"}, "network": {"key": "a"}})
    broker = Broker(config, runtime_dir=tmp_path / "runtime", web_dir=default_web_dir())
    await broker.start()
    await broker.wait_healthy(timeout_s=10)
    try:
        yield broker
    finally:
        await broker.stop()


def state_provider(broker: Broker):
    def _state() -> dict:
        return {"state": broker.manager.get_state().to_wire(), "capabilities": ["get_status"]}

    return _state


@pytest.fixture
async def service(key_broker):
    service = build_http_mcp_service(key_broker, state_provider=state_provider(key_broker))
    port = free_port()
    await service.start(host="127.0.0.1", port=port)
    try:
        yield service, f"http://127.0.0.1:{port}/mcp"
    finally:
        await service.stop()


async def connect(url: str, key: str = "a"):
    """Open an authenticated official client session."""
    import httpx2

    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=10.0)
    transport = streamable_http_client(url, http_client=http)
    cm = await transport.__aenter__()
    session = ClientSession(cm[0], cm[1])
    await session.__aenter__()
    await session.initialize()
    return transport, session


async def close(transport, session) -> None:
    with_context = []
    for obj in (session, transport):
        try:
            await obj.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001
            with_context.append(exc)
    if with_context:
        raise with_context[0]


def text_of(result) -> str:
    return "".join(item.text for item in result.content if hasattr(item, "text"))


# ----------------------------------------------------------------------
# Admission
# ----------------------------------------------------------------------


async def test_wrong_or_missing_key_is_rejected_before_initialize(service):
    _, url = service
    import httpx2

    async with httpx2.AsyncClient(timeout=5.0) as http:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        }
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic a"}):
            response = await http.post(url, json=body, headers=headers)
            assert response.status_code == 401, (headers, response.status_code)
            assert b"key required" in response.content

    # The official client cannot get past initialize with a wrong key (the
    # 401 surfaces as any transport-level failure; the raw check above pins
    # the actual status).
    with pytest.raises((Exception, BaseExceptionGroup)):  # noqa: BLE001
        transport, session = await connect(url, key="wrong")
        await close(transport, session)


async def test_weak_key_admits_full_tool_flow(service, key_broker):
    _, url = service
    transport, session = await connect(url, key="a")
    try:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {"get_status", "acquire_control", "capture_screen"} <= names

        result = await session.call_tool("get_status", {})
        assert not result.is_error
        status = json.loads(text_of(result))
        assert status["state"]["source"] == "simulated"

        resources = await session.list_resources()
        assert any(r.uri == "superuse://state" for r in resources.resources)
        state = await session.read_resource("superuse://state")
        assert json.loads(state.contents[0].text)["state"]["instance_id"]

        shot = await session.call_tool("capture_screen", {"max_width": 320, "wait_ms": 1000})
        assert not shot.is_error
    finally:
        await close(transport, session)


# ----------------------------------------------------------------------
# Session isolation and control contention
# ----------------------------------------------------------------------


async def test_same_label_agents_get_isolated_sessions_and_one_owner(service, key_broker):
    _, url = service
    t1, s1 = await connect(url)
    t2, s2 = await connect(url)
    try:
        grant = await s1.call_tool("acquire_control", {})
        assert not grant.is_error
        json.loads(text_of(grant))["lease"]

        busy = await s2.call_tool("acquire_control", {})
        assert busy.is_error
        assert "CONTROL_BUSY" in text_of(busy)

        # The second agent sees the first one as owner (distinct sessions).
        status2 = json.loads(text_of(await s2.call_tool("get_status", {})))
        owner = status2["state"]["owner"]
        assert owner is not None and owner["kind"] == "agent"
    finally:
        await close(t2, s2)
        await close(t1, s1)
    # After both clients disconnect, sessions close and the lease is released.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = key_broker.manager.get_state()
        if state.owner is None:
            break
        await asyncio.sleep(0.1)
    assert key_broker.manager.get_state().owner is None


async def test_second_agent_takes_over_after_release(service):
    _, url = service
    t1, s1 = await connect(url)
    t2, s2 = await connect(url)
    try:
        await s1.call_tool("acquire_control", {})
        await s1.call_tool("release_control", {})
        grant2 = await s2.call_tool("acquire_control", {})
        assert not grant2.is_error
        await s2.call_tool("release_control", {})
    finally:
        await close(t2, s2)
        await close(t1, s1)


async def test_cancellation_propagates_and_releases(service, key_broker):
    _, url = service
    transport, session = await connect(url)
    try:
        await session.call_tool("acquire_control", {})
        task = asyncio.ensure_future(
            session.call_tool("type_text", {"text": "x" * 200, "char_interval_ms": 50})
        )
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(BaseException) as raised:
            await task
        assert _contains_cancelled(raised.value)

        # notifications/cancelled reaches the dispatcher: the broker-side job
        # ends in "cancelled" (same semantics as the stdio bridge; the lease
        # itself follows the bounded agent-idle release policy).
        deadline = time.monotonic() + 10
        cancelled = False
        while time.monotonic() < deadline:
            records = [
                record.job
                for record in key_broker.scheduler._jobs.values()
                if record.job.request_id
            ]
            if any(job.status.value == "cancelled" for job in records):
                cancelled = True
                break
            await asyncio.sleep(0.1)
        assert cancelled
    finally:
        await close(transport, session)
    # Closing the session tears its client down and releases the lease.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and key_broker.manager.get_state().owner is not None:
        await asyncio.sleep(0.1)
    assert key_broker.manager.get_state().owner is None


def _contains_cancelled(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):  # noqa: UP038
        return any(_contains_cancelled(sub) for sub in exc.exceptions)
    return False


# ----------------------------------------------------------------------
# Lifecycle: rebind and shutdown
# ----------------------------------------------------------------------


async def test_rebind_kills_old_sessions_and_starts_clean(key_broker):
    service = build_http_mcp_service(key_broker, state_provider=state_provider(key_broker))
    port = free_port()
    await service.start(host="127.0.0.1", port=port)
    url = f"http://127.0.0.1:{port}/mcp"
    t1, s1 = await connect(url)
    await s1.call_tool("acquire_control", {})
    # Rebind (stop + start) must invalidate the old session and its lease.
    await service.stop()
    assert not service.running
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and key_broker.manager.get_state().owner is not None:
        await asyncio.sleep(0.1)
    assert key_broker.manager.get_state().owner is None

    await service.start(host="127.0.0.1", port=port)
    try:
        # A request on the stale session is rejected as unknown.
        with pytest.raises(BaseException, match="Session not found"):
            await s1.call_tool("get_status", {})
        # Fresh sessions work on the new listener.
        t2, s2 = await connect(url)
        try:
            status = json.loads(text_of(await s2.call_tool("get_status", {})))
            assert status["state"]["instance_id"]
        finally:
            await close(t2, s2)
    finally:
        await close(t1, s1)
        await service.stop()


async def test_port_conflict_raises_with_address(key_broker):
    service = build_http_mcp_service(key_broker, state_provider=state_provider(key_broker))
    blocker = free_port()
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", blocker))
        sock.listen(1)
        with pytest.raises(OSError, match=str(blocker)):
            await service.start(host="127.0.0.1", port=blocker)
    assert not service.running


async def test_service_without_configured_key_rejects_everything(key_broker):
    key_broker.network = type(key_broker.network)(key="")
    service = build_http_mcp_service(key_broker, state_provider=state_provider(key_broker))
    port = free_port()
    await service.start(host="127.0.0.1", port=port)
    try:
        with pytest.raises(Exception, match=r".*[Uu]nauthorized.*"):
            t, s = await connect(f"http://127.0.0.1:{port}/mcp", key="anything")
            await close(t, s)
    finally:
        await service.stop()


async def test_wildcard_bind_allows_no_origin_agents_and_rejects_bad_origins(key_broker):
    """The LAN-shape bind (0.0.0.0 + explicit transport security).

    Agents carry no Origin header and must pass; browser-style cross-origin
    requests must be refused (acceptance B8).
    """
    service = build_http_mcp_service(key_broker, state_provider=state_provider(key_broker))
    port = free_port()
    await service.start(host="0.0.0.0", port=port)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        transport, session = await connect(url)
        try:
            result = await session.call_tool("get_status", {})
            assert not result.is_error
        finally:
            await close(transport, session)
        import httpx2

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        }
        async with httpx2.AsyncClient(timeout=5.0) as http:
            response = await http.post(
                url,
                json=body,
                headers={"Authorization": "Bearer a", "Origin": "http://evil.example"},
            )
            assert response.status_code == 403
    finally:
        await service.stop()
