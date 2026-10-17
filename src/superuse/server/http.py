"""aiohttp routes for the v1 API (docs/development/api-contract.md §3).

Transport-only: parse strictly, authenticate via the session registry, call
the shared control core, and render contract JSON. No control rules live
here — HTTP and MCP share one ControlManager/InputScheduler.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from pathlib import Path

from aiohttp import web

from superuse.config import Config
from superuse.core.control import ControlManager
from superuse.core.errors import ControlError, ErrorCode
from superuse.core.frames import FrameHub
from superuse.core.models import CommandEnvelope, Lease, WireError
from superuse.core.scheduler import InputScheduler
from superuse.server.sessions import (
    BROWSER_COOKIE,
    SESSION_TTL_S,
    SessionRegistry,
    parse_cookie,
)

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
STATUS_BY_CODE: dict[str, int] = {
    ErrorCode.AUTH_FAILED.value: 401,
    ErrorCode.FORBIDDEN.value: 403,
    ErrorCode.INVALID_REQUEST.value: 400,
    ErrorCode.UNSUPPORTED_KEY.value: 400,
    ErrorCode.UNSUPPORTED_TEXT.value: 400,
    ErrorCode.INPUT_LIMIT.value: 400,
    ErrorCode.COORDINATE_OUT_OF_BOUNDS.value: 400,
    ErrorCode.STALE_BASIS.value: 410,
    ErrorCode.RATE_LIMITED.value: 429,
    ErrorCode.INPUT_BACKPRESSURE.value: 429,
    ErrorCode.BROKER_UNAVAILABLE.value: 503,
    ErrorCode.DEVICE_UNAVAILABLE.value: 503,
    ErrorCode.DEVICE_AMBIGUOUS.value: 503,
    ErrorCode.VIDEO_STALE.value: 503,
    ErrorCode.VIDEO_UNAVAILABLE.value: 503,
    ErrorCode.FRAME_TIMEOUT.value: 503,
    ErrorCode.RECOVERY_REQUIRED.value: 503,
    ErrorCode.SERIAL_WRITE_FAILED.value: 503,
    ErrorCode.RESULT_UNKNOWN.value: 503,
    ErrorCode.NOT_FOUND.value: 404,
}


def error_response(exc: ControlError, request_id: str | None) -> web.Response:
    status = STATUS_BY_CODE.get(exc.code.value, 409)
    body = {
        "error": exc.to_wire(),
        "request_id": request_id or str(uuid.uuid4()),  # server-generated for tracking only
    }
    return web.json_response(body, status=status)


def json_response(data, status: int = 200) -> web.Response:
    return web.json_response(
        data, status=status, dumps=lambda d: json.dumps(d, separators=(",", ":"))
    )


class Api:
    """Route table plus auth helpers bound to one broker instance."""

    def __init__(
        self,
        *,
        config: Config,
        manager: ControlManager,
        scheduler: InputScheduler,
        sessions: SessionRegistry,
        hub: FrameHub,
        web_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.manager = manager
        self.scheduler = scheduler
        self.sessions = sessions
        self.hub = hub
        self.web_dir = web_dir
        self.port = 0

    # ------------------------------------------------------------------
    # Middleware: host/origin checks, body limits, auth
    # ------------------------------------------------------------------

    @web.middleware
    async def guard(self, request: web.Request, handler):
        try:
            host = request.headers.get("Host", "")
            if self.port and host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
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
            logger.exception("unhandled route error")
            return error_response(ControlError(ErrorCode.INVALID_REQUEST, "Bad request"), None)

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    async def parse_json_object(request: web.Request) -> dict:
        raw = await request.read() if request.can_read_body else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Malformed JSON") from exc
        if not isinstance(data, dict):
            raise ControlError(ErrorCode.INVALID_REQUEST, "Expected a JSON object")
        return data

    def auth_agent(self, request: web.Request):
        session = self.sessions.authenticate_agent(request.headers.get("Authorization"))
        self.check_instance_header(request)
        return session

    def check_instance_header(self, request: web.Request) -> None:
        known = request.headers.get("X-Superuse-Instance")
        if known and known != self.manager.instance_id:
            raise ControlError(ErrorCode.INSTANCE_CHANGED, "Broker restarted; reconnect")

    def auth_browser_page(self, request: web.Request, *, require_csrf: bool):
        cookies = parse_cookie(request.headers.get("Cookie"))
        self.sessions.browser_identity(cookies.get(BROWSER_COOKIE))
        page_token = request.headers.get("X-Superuse-Page")
        from superuse.core.models import SessionKind

        session = self.sessions.authenticate_page(page_token)
        if session.kind is not SessionKind.HUMAN:
            raise ControlError(ErrorCode.AUTH_FAILED, "Not a page session")
        if require_csrf:
            csrf = request.headers.get("X-CSRF-Token", "")
            record = self.sessions.page_record_by_token(page_token or "")
            if record is None or csrf != record.csrf:
                raise ControlError(ErrorCode.FORBIDDEN, "CSRF mismatch")
        return session

    def auth_any(self, request: web.Request, *, require_csrf: bool = False):
        if request.headers.get("Authorization", "").startswith("Bearer "):
            return self.auth_agent(request)
        return self.auth_browser_page(request, require_csrf=require_csrf)

    @staticmethod
    def require_origin(request: web.Request) -> None:
        origin = request.headers.get("Origin")
        if not origin:
            return  # same-origin fetches may omit Origin
        expected = f"http://127.0.0.1:{request.host.split(':')[-1]}" if request.host else None
        if origin != expected and origin != f"http://localhost:{request.host.split(':')[-1]}":
            raise ControlError(ErrorCode.FORBIDDEN, "Cross-origin request rejected")

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    async def healthz(self, request: web.Request) -> web.Response:
        return json_response({"alive": True, "api_version": 1})

    async def create_agent_session(self, request: web.Request) -> web.Response:
        self.require_origin(request)
        body = await self.parse_json_object(request)
        unknown = set(body) - {"label"}
        if unknown:
            raise ControlError(ErrorCode.INVALID_REQUEST, f"Unknown fields {sorted(unknown)}")
        bearer = request.headers.get("Authorization", "")
        if not bearer.startswith("Bearer "):
            raise ControlError(ErrorCode.AUTH_FAILED, "Bootstrap bearer required")
        import time

        session, token = self.sessions.create_agent_session(
            bootstrap_token=bearer[7:], label=str(body.get("label", ""))[:80], now=time.monotonic()
        )
        return json_response(
            {
                "api_version": 1,
                "instance_id": self.manager.instance_id,
                "session_id": session.session_id,
                "session_token": token,
                "heartbeat_interval_ms": 5000,
                "expires_in_ms": int(SESSION_TTL_S * 1000),
            }
        )

    async def browser_exchange(self, request: web.Request) -> web.Response:
        self.require_origin(request)
        body = await self.parse_json_object(request)
        if set(body) != {"bootstrap"} or not isinstance(body["bootstrap"], str):
            raise ControlError(ErrorCode.INVALID_REQUEST, "Expected {bootstrap}")
        import time

        identity = self.sessions.exchange_bootstrap(body["bootstrap"], time.monotonic())
        response = json_response(
            {
                "api_version": 1,
                "instance_id": self.manager.instance_id,
                "csrf_token": identity.csrf,
            }
        )
        response.set_cookie(
            BROWSER_COOKIE,
            identity.cookie_value,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return response

    async def browser_context(self, request: web.Request) -> web.Response:
        self.require_origin(request)
        cookies = parse_cookie(request.headers.get("Cookie"))
        identity = self.sessions.browser_identity(cookies.get(BROWSER_COOKIE))
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
        cookies = parse_cookie(request.headers.get("Cookie"))
        identity = self.sessions.browser_identity(cookies.get(BROWSER_COOKIE))
        csrf = request.headers.get("X-CSRF-Token", "")
        import time

        session, record = self.sessions.create_page_session(
            identity=identity,
            csrf=csrf,
            label=str(body.get("label", ""))[:80],
            now=time.monotonic(),
        )
        return json_response(
            {
                "api_version": 1,
                "instance_id": self.manager.instance_id,
                "session_id": session.session_id,
                "page_token": record.page_token,
                "heartbeat_interval_ms": 5000,
                "expires_in_ms": int(SESSION_TTL_S * 1000),
            }
        )

    async def ui_launch(self, request: web.Request) -> web.Response:
        body = await self.parse_json_object(request)
        unknown = set(body) - {"request_id"}
        if unknown:
            raise ControlError(ErrorCode.INVALID_REQUEST, f"Unknown fields {sorted(unknown)}")
        bearer = request.headers.get("Authorization", "")
        launcher = self.sessions.credentials.launcher
        if not bearer.startswith("Bearer ") or bearer[7:] != launcher:
            raise ControlError(ErrorCode.FORBIDDEN, "Launcher credential required")
        import time

        bootstrap = self.sessions.issue_bootstrap(time.monotonic())
        url = f"http://127.0.0.1:{self.port}/#bootstrap={bootstrap}"
        return json_response({"url": url, "expires_in_ms": 30000})

    async def session_heartbeat(self, request: web.Request) -> web.Response:
        body = await self.parse_json_object(request)
        if body:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Heartbeat takes no fields")
        session = self.auth_any(request)
        expires_ms = self.sessions.heartbeat(session)
        return json_response({"expires_in_ms": expires_ms})

    async def session_delete(self, request: web.Request) -> web.Response:
        session = self.auth_any(request, require_csrf=True)
        self.sessions.close_session(session.session_id)
        await self.manager.session_closed(session)
        return json_response({"closed": True})

    async def get_state(self, request: web.Request) -> web.Response:
        self.auth_any(request)
        return json_response(self.manager.get_state().to_wire())

    async def get_status(self, request: web.Request) -> web.Response:
        self.auth_any(request)
        capabilities = [
            "get_status",
            "acquire_control",
            "release_control",
            "capture_screen",
            "press_key",
            "type_text",
            "mouse_move",
            "mouse_click",
            "mouse_drag",
            "mouse_scroll",
            "mouse_move_relative",
            "mouse_click_relative",
            "mouse_drag_relative",
            "mouse_scroll_relative",
            "release_inputs",
            "cancel_action",
        ]
        return json_response(
            {"state": self.manager.get_state().to_wire(), "capabilities": capabilities}
        )

    async def control_acquire(self, request: web.Request) -> web.Response:
        session = self.auth_agent(request)
        body = await self.parse_json_object(request)
        unknown = set(body) - {"request_id", "ttl_ms"}
        if unknown or "request_id" not in body:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Expected {request_id[, ttl_ms]}")
        ttl = body.get("ttl_ms", 15000)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not (5000 <= ttl <= 30000):
            raise ControlError(ErrorCode.INVALID_REQUEST, "ttl_ms out of range")
        record = await self.manager.acquire(session, ttl)
        return json_response(
            {
                "lease": {
                    "instance_id": record.lease.instance_id,
                    "lease_id": record.lease.lease_id,
                    "generation": record.lease.generation,
                },
                "expires_in_ms": self._expires_ms(record),
                "state": self.manager.get_state().to_wire(),
            }
        )

    def _expires_ms(self, record) -> int:
        import time

        return max(1, int((record.expires_at - time.monotonic()) * 1000))

    async def control_renew(self, request: web.Request) -> web.Response:
        # Lease owner renew: agents (bearer) and the owning human page
        # (cookie+page+CSRF) both renew the same way.
        session = self.auth_any(request, require_csrf=True)
        body = await self.parse_json_object(request)
        unknown = set(body) - {"request_id", "lease", "ttl_ms"}
        if unknown or "lease" not in body or "request_id" not in body:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Expected {request_id, lease[, ttl_ms]}")
        lease = self._parse_lease(body["lease"])
        ttl = body.get("ttl_ms", 15000)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not (5000 <= ttl <= 30000):
            raise ControlError(ErrorCode.INVALID_REQUEST, "ttl_ms out of range")
        expires_ms = await self.manager.renew(session, lease, ttl)
        return json_response({"expires_in_ms": expires_ms})

    async def control_release(self, request: web.Request) -> web.Response:
        session = self.auth_agent(request)
        body = await self.parse_json_object(request)
        unknown = set(body) - {"request_id", "lease"}
        if unknown or "lease" not in body or "request_id" not in body:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Expected {request_id, lease}")
        lease = self._parse_lease(body["lease"])
        state = await self.manager.release_control(session, lease)
        return json_response(state.to_wire())

    async def control_takeover(self, request: web.Request) -> web.Response:
        session = self.auth_browser_page(request, require_csrf=True)
        body = await self.parse_json_object(request)
        unknown = set(body) - {"request_id"}
        if unknown or "request_id" not in body:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Expected {request_id}")
        record = await self.manager.takeover(session)
        return json_response(
            {
                "lease": {
                    "instance_id": record.lease.instance_id,
                    "lease_id": record.lease.lease_id,
                    "generation": record.lease.generation,
                },
                "expires_in_ms": self._expires_ms(record),
                "state": self.manager.get_state().to_wire(),
            }
        )

    async def control_handback(self, request: web.Request) -> web.Response:
        session = self.auth_browser_page(request, require_csrf=True)
        body = await self.parse_json_object(request)
        unknown = set(body) - {"request_id"}
        if unknown or "request_id" not in body:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Expected {request_id}")
        state = await self.manager.handback(session)
        return json_response(state.to_wire())

    async def post_command(self, request: web.Request) -> web.Response:
        session = self.auth_any(request, require_csrf=True)
        body = await self.parse_json_object(request)
        try:
            envelope = CommandEnvelope.from_wire(body)
        except WireError as exc:
            raise ControlError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        job = await self.scheduler.submit_command(session, envelope)
        return json_response(job.to_wire(), status=202)

    async def get_job(self, request: web.Request) -> web.Response:
        session = self.auth_any(request)
        job = self.scheduler.get_job(session, request.match_info["job_id"])
        return json_response(job.to_wire())

    async def cancel_job(self, request: web.Request) -> web.Response:
        session = self.auth_any(request)
        body = await self.parse_json_object(request)
        unknown = set(body) - {"request_id"}
        if unknown or "request_id" not in body:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Expected {request_id}")
        job = await self.scheduler.cancel_job(session, request.match_info["job_id"])
        return json_response(job.to_wire())

    async def frames_latest(self, request: web.Request) -> web.Response:
        session = self.auth_any(request)
        if session.kind.value == "agent":
            self.check_instance_header(request)
        query = request.query
        try:
            max_width = int(
                query.get("max_width", self.config.section("video")["snapshot_max_width"])
            )
            wait_ms = int(query.get("wait_ms", "2000"))
            after_frame_id = int(query["after_frame_id"]) if "after_frame_id" in query else None
        except ValueError as exc:
            raise ControlError(ErrorCode.INVALID_REQUEST, "Bad query parameter") from exc
        if not (320 <= max_width <= 1920) or not (0 <= wait_ms <= 2000):
            raise ControlError(ErrorCode.INVALID_REQUEST, "Query parameter out of range")
        import time

        metadata, jpeg = await self.hub.snapshot(
            now=time.monotonic,
            freshness_s=self.config.freshness_s,
            max_width=max_width,
            after_frame_id=after_frame_id,
            wait_ms=wait_ms,
        )
        return json_response(
            {"metadata": metadata, "data_base64": base64.b64encode(jpeg).decode("ascii")}
        )

    # -- static web assets (no third-party resources) ---------------------

    _STATIC_TYPES = {
        "index.html": "text/html; charset=utf-8",
        "app.js": "application/javascript; charset=utf-8",
        "app.css": "text/css; charset=utf-8",
        "icon.svg": "image/svg+xml",
    }

    async def index(self, request: web.Request) -> web.Response:
        return await self._static(request, "index.html")

    async def _static(self, request: web.Request, name: str) -> web.Response:
        if self.web_dir is None or name not in self._STATIC_TYPES:
            raise web.HTTPNotFound(text="web asset not installed")
        path = self.web_dir / name
        if not path.is_file():
            raise web.HTTPNotFound(text="web asset not installed")
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_lease(data) -> Lease:
        if not isinstance(data, dict):
            raise ControlError(ErrorCode.INVALID_REQUEST, "lease must be an object")
        try:
            return Lease.from_wire(data)
        except WireError as exc:
            raise ControlError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self.guard], client_max_size=MAX_BODY_BYTES)
        app.router.add_get("/healthz", self.healthz)
        app.router.add_post("/v1/sessions/agent", self.create_agent_session)
        app.router.add_post("/v1/sessions/browser", self.browser_exchange)
        app.router.add_get("/v1/browser/context", self.browser_context)
        app.router.add_post("/v1/sessions/page", self.create_page_session)
        app.router.add_post("/v1/ui/launch", self.ui_launch)
        app.router.add_post("/v1/session/heartbeat", self.session_heartbeat)
        app.router.add_delete("/v1/session", self.session_delete)
        app.router.add_get("/v1/state", self.get_state)
        app.router.add_get("/v1/status", self.get_status)
        app.router.add_post("/v1/control/acquire", self.control_acquire)
        app.router.add_post("/v1/control/renew", self.control_renew)
        app.router.add_post("/v1/control/release", self.control_release)
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
