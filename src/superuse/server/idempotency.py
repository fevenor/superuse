from __future__ import annotations

import asyncio
import functools
import json
import time
import uuid
from dataclasses import dataclass

from superuse.core.errors import ControlError, ErrorCode


@dataclass
class Entry:
    fingerprint: str
    task: asyncio.Task
    finished_at: float | None = None


class RequestCache:
    def __init__(self, capacity: int = 1024, ttl_s: float = 120.0):
        self.entries: dict[tuple[str, str], Entry] = {}
        self.capacity = capacity
        self.ttl_s = ttl_s

    async def run(self, session_id, request_id, fingerprint, operation):
        try:
            valid = isinstance(request_id, str) and str(uuid.UUID(request_id)) == request_id.lower()
        except ValueError:
            valid = False
        if not valid:
            raise ControlError(ErrorCode.INVALID_REQUEST, "request_id must be a UUID")
        now = time.monotonic()
        self.entries = {
            key: entry
            for key, entry in self.entries.items()
            if entry.finished_at is None or now - entry.finished_at < self.ttl_s
        }
        key = (session_id, request_id.lower())
        entry = self.entries.get(key)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                raise ControlError(
                    ErrorCode.REQUEST_ID_CONFLICT, "request_id already used for another request"
                )
        else:
            if len(self.entries) >= self.capacity:
                raise ControlError(ErrorCode.INPUT_BACKPRESSURE, "Control request cache is full")
            task = asyncio.create_task(operation())
            entry = Entry(fingerprint, task)
            self.entries[key] = entry

            def finished(task):
                entry.finished_at = time.monotonic()
                if not task.cancelled():
                    task.exception()

            task.add_done_callback(finished)
        return await asyncio.shield(entry.task)


def idempotent_control(handler):
    @functools.wraps(handler)
    async def wrapped(self, request):
        from aiohttp import web

        from superuse.server.http import error_response

        session = self.auth_any(request, require_csrf=True)
        body = await self.parse_json_object(request)

        async def operation():
            try:
                response = await handler(self, request)
            except ControlError as exc:
                response = error_response(exc, body.get("request_id"))
            return response.status, response.body

        status, payload = await self.control_requests.run(
            session.session_id,
            body.get("request_id"),
            json.dumps([request.path, body], sort_keys=True, separators=(",", ":")),
            operation,
        )
        return web.Response(body=payload, status=status, content_type="application/json")

    return wrapped
