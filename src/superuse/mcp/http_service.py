"""Streamable HTTP MCP service (task 2026-10-17 T4).

Serves the locked SDK's Streamable HTTP transport under uvicorn on the
broker's asyncio loop (one loop for aiohttp + uvicorn, no cross-thread
calls). Identity is the standard MCP transport session — the SDK-assigned
``Mcp-Session-Id`` — and every session owns an independent BrokerClient
(its own broker agent session, lease, renewal and idle policy). The shared
network key only admits requests; it is never an identity.

Binding model (§3.3): local mode listens on 127.0.0.1:mcp_port when a key is
configured; enabling LAN rebinds to lan_host:mcp_port. A rebind tears the
service down (closing every session's client) and starts a fresh one — a
brief reconnect window is expected and documented.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable

from superuse.mcp.broker_client import BrokerClient
from superuse.mcp.server import ClientResolver, StateProvider, build_server

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
SESSION_HEADER = "mcp-session-id"
MAX_SESSIONS = 32  # bounded: a handful of agents is the intended shape
SESSION_IDLE_TIMEOUT_S = 30 * 60


def _session_id_from_context(ctx) -> str | None:
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        return None
    try:
        return request.headers.get(SESSION_HEADER)
    except Exception:  # pragma: no cover - defensive against SDK changes
        return None


class HttpMcpClientPool:
    """One BrokerClient per MCP transport session, created lazily."""

    def __init__(self, runtime_dir, *, label_prefix: str = "http-agent") -> None:
        self._runtime_dir = runtime_dir
        self._label_prefix = label_prefix
        self._clients: dict[str, BrokerClient] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def client_for(self, session_id: str, ctx=None) -> BrokerClient:
        async with self._lock:
            if self._closed:
                from mcp.server.mcpserver.exceptions import ToolError

                raise ToolError("BROKER_UNAVAILABLE: MCP client pool is closed")
            client = self._clients.get(session_id)
            if client is None:
                client = BrokerClient(
                    runtime_dir=self._runtime_dir,
                    label=self._label_for(ctx),
                )
                # start() connects to the loopback broker and begins the
                # heartbeat/renewal loops owned by this session alone.
                await client.start()
                self._clients[session_id] = client
            return client

    def _label_for(self, ctx) -> str:
        """clientInfo name is a display hint only — never an identity."""
        try:
            params = ctx.request_context.session.client_params  # type: ignore[union-attr]
            name = params.client_info.name if params and params.client_info else ""
            name = "".join(ch for ch in (name or "") if ch.isprintable())[:40]
            if name:
                return f"{self._label_prefix}:{name}"
        except Exception:  # pragma: no cover - SDK shape changes
            pass
        return self._label_prefix

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            client = self._clients.pop(session_id, None)
        if client is not None:
            await client.close()  # releases the lease; cancels renewal loops

    async def close_all(self) -> None:
        # Admission must close before yielding to a client that is still
        # initializing, or a queued resolver can refill a drained pool.
        self._closed = True
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()

    @property
    def session_ids(self) -> list[str]:
        return list(self._clients)

    def resolver(self, sealed: Callable[[], bool] | None = None) -> ClientResolver:
        async def _resolve(ctx) -> BrokerClient:
            from mcp.server.mcpserver.exceptions import ToolError

            if sealed is not None and sealed():
                # Tool-level rejection keeps an in-flight call answering with
                # an errored CallToolResult instead of a transport failure
                # (review 2026-10-17 R3).
                raise ToolError("BROKER_UNAVAILABLE: LAN access is being disabled")
            session_id = _session_id_from_context(ctx)
            if session_id is None:
                # Requests without a transport session cannot be isolated.
                raise ToolError("AUTH_FAILED: no MCP session; initialize first")
            client = await self.client_for(session_id, ctx)
            # Client startup performs HTTP I/O. The listener may have been
            # sealed while it was in progress, before this tool acquired its
            # client. Never let that late resolution start a new input call.
            if sealed is not None and sealed():
                raise ToolError("BROKER_UNAVAILABLE: LAN access is being disabled")
            return client

        return _resolve


class _BearerKeyMiddleware:
    """ASGI middleware admitting requests that carry the exact network key.

    Non-ASCII keys ride the header as their UTF-8 bytes (latin-1 passthrough),
    which is the documented encoding rule (§3.2). Rejection is a plain 401
    JSON-RPC error body, independent of the SDK's OAuth machinery.

    While the owning service is sealed for shutdown (review R3), requests
    that would open a NEW transport session are refused outright; requests
    on existing sessions pass so their tools can return structured errors.
    """

    def __init__(
        self,
        app,
        check: Callable[[bytes | None], bool],
        sealed: Callable[[], bool] | None = None,
    ) -> None:
        self.app = app
        self.check = check
        self.sealed = sealed

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        authorization: bytes | None = None
        has_session = False
        for name, value in headers:
            if name == b"authorization":
                authorization = value
            elif name == SESSION_HEADER.encode("ascii"):
                has_session = True
        status, message = None, None
        ok = False
        if authorization is not None:
            scheme, _, token = authorization.partition(b" ")
            if scheme.lower() == b"bearer":
                ok = self.check(token)
        if not ok:
            status, message = 401, "Unauthorized: valid key required"
        elif self.sealed is not None and self.sealed() and not has_session:
            status, message = 503, "Service unavailable: shutting down"
        if status is not None:
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001 if status == 401 else -32002, "message": message},
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class HttpMcpService:
    """Lifecycle owner of the /mcp listener: build, serve, rebind, stop."""

    def __init__(
        self,
        *,
        runtime_dir,
        state_provider: StateProvider | None = None,
        check_key: Callable[[bytes | None], bool],
    ) -> None:
        self._runtime_dir = runtime_dir
        self._state_provider = state_provider
        self._check_key = check_key
        self._server = None
        self._serve_task: asyncio.Task | None = None
        self._manager = None
        self._pool: HttpMcpClientPool | None = None
        self.host: str | None = None
        self.port: int | None = None
        self._sealed = False

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.started

    @property
    def sealed(self) -> bool:
        """True once the owning entrance began shutting down (review R3)."""
        return self._sealed

    def _build_starlette_app(self, host: str):
        from mcp.server.streamable_http_manager import (
            StreamableHTTPASGIApp,
            StreamableHTTPSessionManager,
        )
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.applications import Starlette
        from starlette.routing import Route

        pool = HttpMcpClientPool(self._runtime_dir)
        server = build_server(
            pool.resolver(lambda: self._sealed), state_provider=self._state_provider
        )

        authority = self

        class _HookedManager(StreamableHTTPSessionManager):
            """Adds client-pool cleanup to the SDK's session lifecycle."""

            async def _discard_session(self, session_id, transport) -> None:
                await super()._discard_session(session_id, transport)
                # Whatever the session owned (lease, renewals, jobs) is
                # released exactly here — never by a late cleanup task.
                await authority._discard_pool_session(session_id)

        # DNS-rebinding protection for the addresses clients actually use.
        # A wildcard bind is reached through a real NIC address, so the Host
        # (and browser Origin) allowlists must enumerate those, not just the
        # bind address (review 2026-10-17 R2). Agent requests carry no Origin
        # and stay allowed (§3.4).
        if host in ("127.0.0.1", "localhost", "::1"):
            security = None  # SDK default covers loopback hosts
        else:
            from superuse.server.network import local_ipv4_addresses

            reachable = {host, "127.0.0.1", "localhost", *local_ipv4_addresses()}
            security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[f"{h}:*" for h in sorted(reachable)],
                allowed_origins=[f"http://{h}:*" for h in sorted(reachable)],
            )

        manager = _HookedManager(
            app=server._lowlevel_server,
            security_settings=security,
            max_sessions=MAX_SESSIONS,
            session_idle_timeout=SESSION_IDLE_TIMEOUT_S,
        )
        mcp_endpoint = StreamableHTTPASGIApp(manager)

        async def _lifespan(app):
            async with manager.run():
                yield

        starlette = Starlette(
            routes=[Route(MCP_PATH, endpoint=mcp_endpoint)],
            lifespan=_lifespan,
        )
        # Key gate wraps everything: no request reaches the transport without
        # the exact configured key (§3.4); while sealed it also refuses new
        # transport sessions (review R3).
        return (
            _BearerKeyMiddleware(starlette, self._check_key, lambda: self._sealed),
            manager,
            pool,
        )

    def seal(self) -> None:
        """Close the admission window: new tool calls and sessions fail now.

        In-flight session requests keep answering — their tools return
        structured errors via the sealed resolver instead of a transport
        failure. The local (loopback) service instance is never sealed by
        LAN shutdowns: sealing is per service, not global.
        """
        self._sealed = True

    async def drain(self) -> None:
        """Revoke every session's broker client BEFORE the listener waits.

        Each client close releases its lease through the control manager's
        barrier (cancelling its running jobs) while the internal API is
        still up — the revocation point the shutdown ordering requires.
        """
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close_all()

    async def _discard_pool_session(self, session_id: str) -> None:
        if self._pool is not None:
            await self._pool.close_session(session_id)

    async def start(self, *, host: str, port: int) -> None:
        """Bind and serve; raises with the concrete host:port on failure.

        The socket is bound HERE and handed to uvicorn: a conflict surfaces
        as this method's OSError instead of being swallowed by uvicorn's
        internal SystemExit/cancel-scope dance.
        """
        if self.running:
            raise RuntimeError("HTTP MCP service already running")
        import socket as socket_module

        import uvicorn

        app, manager, pool = self._build_starlette_app(host)
        bound = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
        try:
            bound.bind((host, port))
            bound.listen(128)
        except OSError as exc:
            bound.close()
            raise OSError(f"HTTP MCP cannot listen on {host}:{port}: {exc}") from exc
        bound.setblocking(False)

        config = uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            lifespan="on",
            # Never let uvicorn install its default logging config: the
            # DefaultFormatter probes sys.stdout.isatty(), which crashes with
            # stdout=None in windowed release EXEs (review 2026-10-17 R1).
            # With log_config=None uvicorn only sets logger levels.
            log_config=None,
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[bound]), name="http-mcp-serve")
        try:
            deadline = asyncio.get_running_loop().time() + 15.0
            while not server.started:
                if task.done():
                    raise OSError(f"HTTP MCP server failed to start on {host}:{port}")
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError(f"HTTP MCP server startup timed out on {host}:{port}")
                await asyncio.sleep(0.05)
        except BaseException:
            server.should_exit = True
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
        self._server = server
        self._serve_task = task
        self._manager = manager
        self._pool = pool
        self.host = host
        self.port = port
        logger.info("HTTP MCP listening on %s:%s%s", host, port, MCP_PATH)

    async def stop(self) -> None:
        """Tear the listener down and close every session's client."""
        server, task, pool = self._server, self._serve_task, self._pool
        self._server = self._serve_task = self._manager = None
        self._pool = None
        self.host = self.port = None
        if server is not None and task is not None:
            server.should_exit = True
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, timeout=10.0)
            if not task.done():  # pragma: no cover - stubborn shutdown
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        if pool is not None:
            await pool.close_all()

    async def clients_snapshot(self) -> list[str]:
        return list(self._pool.session_ids) if self._pool is not None else []


def build_http_mcp_service(
    broker, *, state_provider: StateProvider | None = None
) -> HttpMcpService:
    """Convenience wiring from a Broker instance (loopback clients)."""

    def _check(raw: bytes | None) -> bool:
        return broker.network.check_key_bytes(raw)

    return HttpMcpService(
        runtime_dir=broker.runtime_dir,
        state_provider=state_provider,
        check_key=_check,
    )
