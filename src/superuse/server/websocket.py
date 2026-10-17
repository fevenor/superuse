"""WebSocket endpoints: events, video, human input (docs/development/api-contract.md §4).

Human sockets authenticate with cookie+Origin, then a 3-second
authenticate message carrying the page token and CSRF. Agent event sockets
use the Authorization bearer at handshake. Video sends binary
len(header)+header+jpeg messages, latest-only with slow-client drops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

from superuse.core.errors import ControlError, ErrorCode
from superuse.core.models import HumanInputEnvelope, WireError
from superuse.server.http import Api
from superuse.server.sessions import parse_cookie

logger = logging.getLogger(__name__)

AUTH_WINDOW_S = 3.0
VIDEO_HEADER_LIMIT = 4096
VIDEO_FRAME_LIMIT = 16 * 1024 * 1024
VIDEO_SEND_TIMEOUT_S = 2.0
PREVIEW_MAX_WIDTH = 1920
PREVIEW_JPEG_QUALITY = 90
MAX_WS_MESSAGE = 256 * 1024


@dataclass(eq=False)  # identity hash: instances live in a set
class VideoViewer:
    """One video connection: latest-only outbox plus its sender task.

    The sender task is cancelled on disconnect — a dropped viewer must not
    leave a coroutine parked on an outbox that will never fill again.
    """

    ws: web.WebSocketResponse
    outbox: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    task: asyncio.Task | None = None

    def offer(self, payload: bytes) -> None:
        """Queue the newest frame; an older pending frame is dropped."""
        try:
            self.outbox.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                self.outbox.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - raced consumer
                pass
            try:
                self.outbox.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - raced consumer
                pass

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None


def _origin_ok(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return False
    host = request.headers.get("Host", "")
    return origin in (f"http://{host}",)


class WsServer:
    def __init__(self, api: Api) -> None:
        self.api = api
        self.manager = api.manager
        self.scheduler = api.scheduler
        self.sessions = api.sessions
        self.hub = api.hub
        self._video_viewers: set[VideoViewer] = set()
        self._broadcaster: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Authentication handshake
    # ------------------------------------------------------------------

    async def _authenticate_human(self, request: web.Request, ws: web.WebSocketResponse):
        if not _origin_ok(request):
            raise ControlError(ErrorCode.FORBIDDEN, "Bad Origin")
        cookies = parse_cookie(request.headers.get("Cookie"))
        self.api.ws_browser_identity(cookies.get(self.api.browser_cookie))
        try:
            message = await asyncio.wait_for(ws.receive(), timeout=AUTH_WINDOW_S)
        except TimeoutError as exc:
            raise ControlError(ErrorCode.AUTH_FAILED, "Authentication timeout") from exc
        if message.type is not WSMsgType.TEXT:
            raise ControlError(ErrorCode.AUTH_FAILED, "Authenticate with a JSON text message")
        try:
            data = json.loads(message.data)
        except json.JSONDecodeError as exc:
            raise ControlError(ErrorCode.AUTH_FAILED, "Malformed authenticate") from exc
        if not isinstance(data, dict) or set(data) != {"type", "page_token", "csrf"}:
            raise ControlError(ErrorCode.AUTH_FAILED, "Expected authenticate{page_token,csrf}")
        if data["type"] != "authenticate":
            raise ControlError(ErrorCode.AUTH_FAILED, "First message must authenticate")
        session = self.sessions.authenticate_page(data["page_token"])
        record = self.sessions.page_record_by_token(data["page_token"])
        if record is None or record.csrf != data["csrf"]:
            raise ControlError(ErrorCode.FORBIDDEN, "CSRF mismatch")
        return session, record

    # ------------------------------------------------------------------
    # /v1/events
    # ------------------------------------------------------------------

    async def events(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=MAX_WS_MESSAGE)
        await ws.prepare(request)
        record = None
        try:
            if request.headers.get("Authorization", "").startswith("Bearer "):
                session = self.api.auth_agent(request)
                agent_record = self.sessions.agent_record(session.session_id)
                if agent_record is None or agent_record.event_connections >= 2:
                    await ws.close(code=4003, message=b"event connection limit")
                    return ws
                agent_record.event_connections += 1
            else:
                session, record = await self._authenticate_human(request, ws)
                if record.event_connections >= 2:
                    await ws.close(code=4003, message=b"event connection limit")
                    return ws
                record.event_connections += 1
        except ControlError as exc:
            await ws.send_json({"error": exc.to_wire(), "request_id": "0" * 36})
            await ws.close(code=4001, message=b"authentication failed")
            return ws

        queue = self.manager.subscribe()
        try:
            if record is not None:
                record.websockets.add(ws)
            # Full state immediately after authentication.
            latest = self.manager.get_state()
            await ws.send_json(
                {
                    "event_id": 0,
                    "instance_id": self.manager.instance_id,
                    "state_revision": latest.state_revision,
                    "reason": "subscribed",
                    "state": latest.to_wire(),
                }
            )
            sender = asyncio.ensure_future(self._forward_events(ws, queue))
            async for _message in ws:
                pass  # clients never send on events; ignore anything
            sender.cancel()
        finally:
            self.manager.unsubscribe(queue)
            if record is not None:
                record.websockets.discard(ws)
                record.event_connections = max(0, record.event_connections - 1)
            else:
                agent_record = self.sessions.agent_record(session.session_id)
                if agent_record is not None:
                    agent_record.event_connections = max(0, agent_record.event_connections - 1)
        return ws

    async def _forward_events(self, ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
        try:
            while True:
                event = await queue.get()
                await ws.send_json(event.to_wire())
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass

    # ------------------------------------------------------------------
    # /v1/video
    # ------------------------------------------------------------------

    async def video(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=MAX_WS_MESSAGE)
        await ws.prepare(request)
        try:
            _session, record = await self._authenticate_human(request, ws)
        except ControlError as exc:
            await ws.send_json({"error": exc.to_wire(), "request_id": "0" * 36})
            await ws.close(code=4001, message=b"authentication failed")
            return ws
        viewer = VideoViewer(ws=ws)
        viewer.task = asyncio.ensure_future(self._send_video(viewer))
        try:
            self._video_viewers.add(viewer)
        except TypeError:  # registration failed: never leak the sender task
            viewer.close()
            raise
        try:
            record.websockets.add(ws)
            while True:
                message = await ws.receive()
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
                # The video socket carries no client payloads.
        finally:
            record.websockets.discard(ws)
            self._video_viewers.discard(viewer)
            viewer.close()
        return ws

    async def _send_video(self, viewer: VideoViewer) -> None:
        try:
            while True:
                payload = await viewer.outbox.get()
                await asyncio.wait_for(viewer.ws.send_bytes(payload), timeout=VIDEO_SEND_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass  # slow viewer closed; capture and other viewers continue

    async def broadcast_loop(self) -> None:
        """Publish the latest encoded frame to every viewer at publish_fps."""
        import time

        interval = 1.0 / self.api.config.section("video")["publish_fps"]
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._video_viewers:
                    continue
                snapshot = self.hub.publish_snapshot(
                    now=time.monotonic(),
                    freshness_s=self.api.config.stall_timeout_s,
                    max_width=PREVIEW_MAX_WIDTH,
                    jpeg_quality=PREVIEW_JPEG_QUALITY,
                )
                if snapshot is None:
                    continue
                metadata, jpeg = snapshot
                header = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
                if len(header) > VIDEO_HEADER_LIMIT:
                    continue
                payload = struct.pack(">I", len(header)) + header + jpeg
                for viewer in list(self._video_viewers):
                    viewer.offer(payload)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # /v1/input
    # ------------------------------------------------------------------

    async def input(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=MAX_WS_MESSAGE)
        await ws.prepare(request)
        try:
            session, record = await self._authenticate_human(request, ws)
        except ControlError as exc:
            await ws.send_json({"error": exc.to_wire(), "request_id": "0" * 36})
            await ws.close(code=4001, message=b"authentication failed")
            return ws
        if record.input_connection:
            await ws.close(code=4003, message=b"input connection already open")
            return ws
        record.input_connection = True

        def send_ack(ack) -> None:
            asyncio.ensure_future(self._safe_send_json(ws, ack.to_wire()))

        stream = self.scheduler.attach_stream(session, send_ack)
        owner_session = session
        record_at_open = self.manager.current_lease
        connection_lease = (
            record_at_open.lease
            if record_at_open and record_at_open.session.session_id == session.session_id
            else None
        )
        try:
            record.websockets.add(ws)
            async for message in ws:
                if message.type is WSMsgType.BINARY:
                    await ws.close(code=4000, message=b"binary not allowed on input")
                    break
                if message.type is not WSMsgType.TEXT:
                    continue
                try:
                    envelope = HumanInputEnvelope.from_wire(json.loads(message.data))
                except (json.JSONDecodeError, WireError):
                    await ws.close(code=4000, message=b"malformed input envelope")
                    break
                if connection_lease is None:
                    # A viewer may open the stream before taking over. Bind at
                    # its first authorized input, never to a later replacement.
                    try:
                        self.manager.authorize_input(session, envelope.lease)
                    except ControlError:
                        pass  # stream emits the normal per-input rejection
                    else:
                        connection_lease = envelope.lease
                if connection_lease is not None and envelope.lease != connection_lease:
                    await ws.close(code=4000, message=b"input lease changed; open a new stream")
                    break
                stream.handle(envelope)
        finally:
            record.websockets.discard(ws)
            record.input_connection = False
            self.scheduler.detach_stream(session.session_id)
            # Only the lease owner's input-connection loss releases control.
            if connection_lease is not None:
                asyncio.ensure_future(
                    self.manager.human_suspend(
                        owner_session, "human_input_closed", expected_lease=connection_lease
                    )
                )

        return ws

    async def _safe_send_json(self, ws: web.WebSocketResponse, data: dict) -> None:
        try:
            await ws.send_json(data)
        except (ConnectionResetError, RuntimeError):
            pass

    # ------------------------------------------------------------------

    def register(self, app: web.Application) -> None:
        app.router.add_get("/v1/events", self.events)
        app.router.add_get("/v1/video", self.video)
        app.router.add_get("/v1/input", self.input)

    async def start(self) -> None:
        self._broadcaster = asyncio.ensure_future(self.broadcast_loop())

    async def stop(self) -> None:
        if self._broadcaster is not None:
            self._broadcaster.cancel()
            self._broadcaster = None
        for viewer in list(self._video_viewers):
            viewer.close()
        self._video_viewers.clear()


def install_ws(api: Api, app: web.Application) -> WsServer:
    server = WsServer(api)
    server.register(app)
    return server
