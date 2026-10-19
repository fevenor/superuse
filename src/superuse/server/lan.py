"""LAN browser entry: shared-key login -> LAN cookie -> page sessions -> WS.

Task 2026-10-17 T5. This is a second *entrance*, not a second control core:
the LanApi reuses the broker's manager/scheduler/sessions/hub and mounts a
route subset of the loopback API. What is deliberately absent here:
bootstrap issuance, agent sessions, /v1/ui/launch, /v1/device/mouse-mode,
/v1/device/restart and
every other local-management surface — a remote browser identity can never be
read as a launcher/agent credential, and device configuration writes stay on
the local entry.

Origin is decided by which listener served the request (this one), never by
client claims or X-Forwarded-*.
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

from superuse.core.errors import ControlError, ErrorCode
from superuse.core.models import SessionKind
from superuse.server.http import MAX_BODY_BYTES, Api, error_response, json_response
from superuse.server.network import LAN_BROWSER_COOKIE, LanIdentityRegistry, NetworkAuthority
from superuse.server.sessions import parse_cookie
from superuse.server.websocket import install_ws

logger = logging.getLogger(__name__)


class LanApi(Api):
    """Route/auth surface for remote browsers (cookie namespace: superuse_lan)."""

    browser_cookie = LAN_BROWSER_COOKIE

    def __init__(
        self,
        base: Api,
        *,
        lan_identities: LanIdentityRegistry,
        authority: NetworkAuthority,
        web_port: int,
    ) -> None:
        super().__init__(
            config=base.config,
            manager=base.manager,
            scheduler=base.scheduler,
            sessions=base.sessions,
            hub=base.hub,
            web_dir=base.web_dir,
            network=base.network,
        )
        self.lan_identities = lan_identities
        self.authority = authority
        self.web_port = web_port
        self.port = web_port  # base-class Host check target

    # ------------------------------------------------------------------
    # Entrance guards
    # ------------------------------------------------------------------

    @web.middleware
    async def guard(self, request: web.Request, handler):
        try:
            host = request.headers.get("Host", "")
            if self.web_port and not host.endswith(f":{self.web_port}"):
                return error_response(
                    ControlError(ErrorCode.FORBIDDEN, "Unexpected Host header"), None
                )
            response = await handler(request)
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' blob: data:; connect-src 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except ControlError as exc:
            return error_response(exc, None)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("unhandled lan route error")
            return error_response(ControlError(ErrorCode.INVALID_REQUEST, "Bad request"), None)

    @staticmethod
    def require_origin(request: web.Request) -> None:
        """Same-origin against whatever address the browser actually used."""
        origin = request.headers.get("Origin")
        if not origin:
            return  # same-origin fetches may omit Origin
        if request.host and origin != f"http://{request.host}":
            raise ControlError(ErrorCode.FORBIDDEN, "Cross-origin request rejected")

    # ------------------------------------------------------------------
    # Auth: LAN cookie -> generation-bound page session
    # ------------------------------------------------------------------

    def _lan_identity(self, request: web.Request):
        cookies = parse_cookie(request.headers.get("Cookie"))
        return self.lan_identities.authenticate(cookies.get(LAN_BROWSER_COOKIE))

    def ws_browser_identity(self, cookie_value: str | None):
        return self.lan_identities.authenticate(cookie_value)

    def auth_browser_page(self, request: web.Request, *, require_csrf: bool):
        identity = self._lan_identity(request)
        page_token = request.headers.get("X-Superuse-Page")
        session = self.sessions.authenticate_page(page_token)
        if session.kind is not SessionKind.HUMAN:
            raise ControlError(ErrorCode.AUTH_FAILED, "Not a page session")
        record = self.sessions.page_record_by_token(page_token or "")
        if record is None or record.entrance != "lan":
            raise ControlError(ErrorCode.AUTH_FAILED, "Not a LAN page session")
        if record.network_generation != self.authority.generation:
            raise ControlError(ErrorCode.AUTH_FAILED, "LAN session invalidated; log in again")
        if require_csrf:
            csrf = request.headers.get("X-CSRF-Token", "")
            if csrf != record.csrf or csrf != identity.csrf:
                raise ControlError(ErrorCode.FORBIDDEN, "CSRF mismatch")
        return session

    def auth_agent(self, request: web.Request):
        raise ControlError(
            ErrorCode.AUTH_FAILED,
            "Agent bearers are not accepted on the web entry; use the HTTP MCP port",
        )

    def auth_any(self, request: web.Request, *, require_csrf: bool = False):
        return self.auth_browser_page(request, require_csrf=require_csrf)

    def check_instance_header(self, request: web.Request) -> None:
        known = request.headers.get("X-Superuse-Instance")
        if known and known != self.manager.instance_id:
            raise ControlError(ErrorCode.INSTANCE_CHANGED, "Broker restarted; reconnect")

    # ------------------------------------------------------------------
    # Login / context / page sessions
    # ------------------------------------------------------------------

    async def lan_login(self, request: web.Request) -> web.Response:
        self.require_origin(request)
        body = await self.parse_json_object(request)
        if set(body) != {"key"} or not isinstance(body["key"], str):
            raise ControlError(ErrorCode.INVALID_REQUEST, "Expected {key}")
        identity = self.lan_identities.login(body["key"])
        response = json_response(
            {
                "api_version": 1,
                "instance_id": self.manager.instance_id,
                "csrf_token": identity.csrf,
            }
        )
        # HTTP (no TLS): a Secure flag would stop the cookie from being sent.
        response.set_cookie(
            LAN_BROWSER_COOKIE,
            identity.cookie_value,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return response

    async def lan_logout(self, request: web.Request) -> web.Response:
        """Log out ONE browser: drop its identity, page sessions, leases and
        live sockets (review 2026-10-17 R4).

        Other logged-in browsers, local pages and any new owner keep their
        state. Human owners suspend into human_hold — the latch is retained,
        never handed to an agent.
        """
        self.require_origin(request)
        cookies = parse_cookie(request.headers.get("Cookie"))
        cookie_value = cookies.get(LAN_BROWSER_COOKIE)
        targets = [
            record
            for record in self.sessions.lan_page_records()
            if record.owner_identity == cookie_value
        ]
        self.lan_identities.logout(cookie_value)
        # Revoke the page tokens and input lease before awaiting socket I/O.
        # A peer that is slow to complete the close handshake must not keep
        # sending keys while logout is waiting for its other connections.
        for record in targets:
            self.sessions.close_session(record.session.session_id)
        for record in targets:
            await self.manager.session_closed(record.session)
        for record in targets:
            for socket in list(record.websockets):
                try:
                    await socket.close(code=4001, message=b"logged out")
                except Exception:  # pragma: no cover - already closing
                    pass
            record.websockets.clear()
        response = json_response({"closed": True, "pages_closed": len(targets)})
        response.del_cookie(LAN_BROWSER_COOKIE, path="/")
        return response

    async def lan_context(self, request: web.Request) -> web.Response:
        self.require_origin(request)
        identity = self._lan_identity(request)
        return json_response(
            {
                "api_version": 1,
                "instance_id": self.manager.instance_id,
                "csrf_token": identity.csrf,
            }
        )

    async def create_page_session(self, request: web.Request) -> web.Response:
        self.require_origin(request)
        body = await self.parse_json_object(request)
        unknown = set(body) - {"label"}
        if unknown:
            raise ControlError(ErrorCode.INVALID_REQUEST, f"Unknown fields {sorted(unknown)}")
        identity = self._lan_identity(request)
        session, record = self.sessions.create_page_session(
            identity=None,
            csrf=identity.csrf,
            label=str(body.get("label", ""))[:80],
            now=time.monotonic(),
            entrance="lan",
            network_generation=identity.generation,
            owner_identity=identity.cookie_value,
        )
        return json_response(
            {
                "api_version": 1,
                "instance_id": self.manager.instance_id,
                "session_id": session.session_id,
                "page_token": record.page_token,
                "heartbeat_interval_ms": 5000,
                "expires_in_ms": int(15.0 * 1000),
            }
        )

    # ------------------------------------------------------------------

    def build_lan_app(self) -> web.Application:
        """Control-relevant subset of the loopback API for remote browsers."""
        app = web.Application(middlewares=[self.guard], client_max_size=MAX_BODY_BYTES)
        app.router.add_post("/lan/login", self.lan_login)
        app.router.add_post("/lan/logout", self.lan_logout)
        app.router.add_get("/lan/context", self.lan_context)
        app.router.add_post("/v1/sessions/page", self.create_page_session)
        app.router.add_post("/v1/session/heartbeat", self.session_heartbeat)
        app.router.add_delete("/v1/session", self.session_delete)
        app.router.add_get("/v1/state", self.get_state)
        app.router.add_get("/v1/status", self.get_status)
        app.router.add_post("/v1/control/renew", self.control_renew)
        app.router.add_post("/v1/control/takeover", self.control_takeover)
        app.router.add_post("/v1/control/handback", self.control_handback)
        app.router.add_post("/v1/commands", self.post_command)
        app.router.add_get("/v1/jobs/{job_id}", self.get_job)
        app.router.add_post("/v1/jobs/{job_id}/cancel", self.cancel_job)
        app.router.add_get("/v1/frames/latest", self.frames_latest)
        app.router.add_get("/", self.index)
        app.router.add_get("/app.js", lambda r: self._static(r, "app.js"))
        app.router.add_get("/app.css", lambda r: self._static(r, "app.css"))
        app.router.add_get("/icon.svg", lambda r: self._static(r, "icon.svg"))
        return app


class LanWebServer:
    """Lifecycle owner of the lan_host:web_port listener."""

    def __init__(self, api: LanApi) -> None:
        self.api = api
        self.runner: web.AppRunner | None = None
        self.ws = None
        self.port: int | None = None
        self.host: str | None = None

    @property
    def running(self) -> bool:
        return self.runner is not None

    async def start(self, *, host: str, port: int) -> None:
        if self.running:
            raise RuntimeError("LAN web server already running")
        app = self.api.build_lan_app()
        self.ws = install_ws(self.api, app)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.5)
        await runner.setup()
        try:
            site = web.TCPSite(runner, host, port)
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            raise OSError(f"LAN web cannot listen on {host}:{port}: {exc}") from exc
        self.runner = runner
        self.host, self.port = host, port
        await self.ws.start()
        logger.info("LAN web listening on %s:%s", host, port)

    async def stop(self) -> None:
        runner, ws = self.runner, self.ws
        self.runner = None
        self.ws = None
        self.host = self.port = None
        if ws is not None:
            await ws.stop()
        if runner is not None:
            await runner.cleanup()
