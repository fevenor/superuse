"""Broker client for the MCP bridge.

Reads the current user's runtime file (never a token in MCP config), keeps
one agent session alive, manages the control lease with the documented idle
policy (renew only while inputs or a running job are recent; release when
idle), and maps HTTP error envelopes to machine-readable bridge errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from pathlib import Path

import aiohttp

from superuse.core.errors import ErrorCode

logger = logging.getLogger(__name__)  # logs go to stderr only

RUNTIME_FILENAME = "broker.json"


class BridgeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _bridge_error_from_envelope(body: dict) -> BridgeError:
    error = body.get("error") or {}
    return BridgeError(
        error.get("code", ErrorCode.INVALID_REQUEST.value), error.get("message", "broker error")
    )


class BrokerClient:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        label: str,
        lease_ttl_s: float = 15.0,
        renew_interval_s: float = 5.0,
        agent_idle_s: float = 60.0,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._label = label
        self._lease_ttl_s = lease_ttl_s
        # Renew at most TTL/3 (docs/development/control-state-machine.md §3).
        self._renew_interval_s = min(renew_interval_s, lease_ttl_s / 3)
        self._agent_idle_s = agent_idle_s

        self._http: aiohttp.ClientSession | None = None
        self._base_url: str | None = None
        self._bootstrap: str | None = None
        self._instance_id: str | None = None
        self._session_token: str | None = None
        self._lease: dict | None = None
        self._last_input_at: float = 0.0
        self._running_jobs: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._closing = False

    # ------------------------------------------------------------------
    # Session and transport
    # ------------------------------------------------------------------

    def _read_runtime(self) -> dict:
        path = self._runtime_dir / RUNTIME_FILENAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BridgeError(
                ErrorCode.BROKER_UNAVAILABLE.value,
                f"No broker runtime file at {path}; start the Superuse app first",
            ) from exc
        if "port" not in data or "credentials" not in data:
            raise BridgeError(ErrorCode.BROKER_UNAVAILABLE.value, "Malformed runtime file")
        return data

    async def _ensure_http(self) -> None:
        runtime = self._read_runtime()
        self._bootstrap = runtime["credentials"]["agent_bootstrap"]
        self._instance_id = runtime.get("instance_id")
        base = f"http://127.0.0.1:{runtime['port']}"
        if self._http is None or self._base_url != base:
            if self._http is not None:
                await self._http.close()
            self._http = aiohttp.ClientSession(
                base_url=base, timeout=aiohttp.ClientTimeout(total=35)
            )
            self._base_url = base
            self._session_token = None  # new broker: fresh session
            self._lease = None

    async def _ensure_session(self) -> None:
        if self._session_token is not None:
            return
        assert self._bootstrap is not None
        async with self._http.post(
            "/v1/sessions/agent",
            json={"label": self._label},
            headers={"Authorization": f"Bearer {self._bootstrap}"},
        ) as response:
            body = await response.json()
            if response.status != 200:
                raise _bridge_error_from_envelope(body)
            self._session_token = body["session_token"]
            self._instance_id = body["instance_id"]

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params=None,
        _retried_auth: bool = False,
    ) -> dict:
        await self._ensure_http()
        await self._ensure_session()
        headers = {
            "Authorization": f"Bearer {self._session_token}",
            "X-Superuse-Instance": self._instance_id or "",
        }
        try:
            async with self._http.request(
                method, path, json=json_body, params=params, headers=headers
            ) as response:
                body = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            self._session_token = None
            raise BridgeError(
                ErrorCode.RESULT_UNKNOWN.value, f"Broker connection lost: {exc}"
            ) from exc
        if response.status == 401:
            if _retried_auth:
                raise _bridge_error_from_envelope(body if isinstance(body, dict) else {})
            self._session_token = None  # exactly one retry with a fresh session
            await self._ensure_session()
            return await self.request(
                method, path, json_body=json_body, params=params, _retried_auth=True
            )
        if response.status != 200 and response.status != 202:
            error = _bridge_error_from_envelope(body if isinstance(body, dict) else {})
            if error.code in (ErrorCode.INSTANCE_CHANGED.value, ErrorCode.AUTH_FAILED.value):
                self._lease = None
                self._session_token = None
            raise error
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------------
    # Lease management
    # ------------------------------------------------------------------

    @property
    def lease(self) -> dict | None:
        return self._lease

    async def acquire(self, ttl_ms: int = 15000) -> dict:
        body = await self.request(
            "POST",
            "/v1/control/acquire",
            json_body={"request_id": str(uuid.uuid4()), "ttl_ms": ttl_ms},
        )
        self._lease = body["lease"]
        return body

    async def release(self) -> None:
        lease, self._lease = self._lease, None
        if lease is None:
            return  # idempotent no-op
        await self._release_lease(lease)

    async def _release_lease(self, lease: dict) -> None:
        """Release exactly this lease, including after a cancelled submission.

        Never read the current lease here: a delayed cleanup must not release
        a newer acquisition or a human who has taken over.
        """
        try:
            await self.request(
                "POST",
                "/v1/control/release",
                json_body={"request_id": str(uuid.uuid4()), "lease": lease},
            )
        except BridgeError as exc:
            logger.debug("release ignored: %s", exc)

    def require_lease(self) -> dict:
        if self._lease is None:
            raise BridgeError(
                ErrorCode.CONTROL_REQUIRED.value, "Acquire control first (acquire_control)"
            )
        return self._lease

    async def renew_loop(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(self._renew_interval_s)
                now = time.monotonic()
                busy = bool(self._running_jobs) or (now - self._last_input_at) <= self._agent_idle_s
                if self._lease is None:
                    continue
                if busy:
                    try:
                        await self.request(
                            "POST",
                            "/v1/control/renew",
                            json_body={"request_id": str(uuid.uuid4()), "lease": self._lease},
                        )
                    except BridgeError as exc:
                        if exc.code in (
                            ErrorCode.CONTROL_REVOKED.value,
                            ErrorCode.LEASE_EXPIRED.value,
                            ErrorCode.INSTANCE_CHANGED.value,
                        ):
                            self._lease = None
                elif now - self._last_input_at > self._agent_idle_s:
                    await self.release()  # idle: stop occupying the device
        except asyncio.CancelledError:
            return

    async def heartbeat_loop(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(5.0)
                try:
                    await self.request("POST", "/v1/session/heartbeat", json_body={})
                except BridgeError as exc:
                    logger.debug("heartbeat failed: %s", exc)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Operations used by tools
    # ------------------------------------------------------------------

    async def get_status(self) -> dict:
        return await self.request("GET", "/v1/status")

    async def snapshot(
        self, *, max_width: int = 1280, after_frame_id: int | None = None, wait_ms: int = 2000
    ) -> dict:
        params = {"max_width": str(max_width), "wait_ms": str(wait_ms)}
        if after_frame_id is not None:
            params["after_frame_id"] = str(after_frame_id)
        return await self.request("GET", "/v1/frames/latest", params=params)

    async def command(self, action: dict, *, wait_timeout_s: float = 30.0) -> dict:
        """Submit one action job and wait for its terminal result."""
        lease = self.require_lease()
        self._last_input_at = time.monotonic()
        job_id = None
        try:
            submit = await self.request(
                "POST",
                "/v1/commands",
                json_body={
                    "api_version": 1,
                    "request_id": str(uuid.uuid4()),
                    "lease": lease,
                    "action": action,
                },
            )
            job_id = submit["job_id"]
            self._running_jobs.add(job_id)
            deadline = time.monotonic() + wait_timeout_s
            while True:
                job = await self.request("GET", f"/v1/jobs/{job_id}")
                if job.get("terminal"):
                    return job
                if time.monotonic() >= deadline:
                    raise BridgeError(
                        ErrorCode.RESULT_UNKNOWN.value, "Job still running past wait budget"
                    )
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            # Acceptance can precede the submission response. Without a job
            # ID, revoke the captured lease instead of replaying the action.
            # This also fences a submission still in flight at the broker.
            if job_id is None:
                if self._lease is lease:
                    self._lease = None
                cleanup = self._release_lease(lease)
            else:
                cleanup = self.cancel(job_id)
            cleanup_task = asyncio.create_task(cleanup)
            try:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=2.0)
            finally:
                cleanup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await cleanup_task
            raise
        finally:
            if job_id is not None:
                self._running_jobs.discard(job_id)

    async def cancel(self, job_id: str) -> dict:
        return await self.request(
            "POST", f"/v1/jobs/{job_id}/cancel", json_body={"request_id": str(uuid.uuid4())}
        )

    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self._ensure_http()
        await self._ensure_session()
        self._tasks = [
            asyncio.ensure_future(self.heartbeat_loop()),
            asyncio.ensure_future(self.renew_loop()),
        ]

    async def close(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.release()
        if self._http is not None:
            await self._http.close()
            self._http = None
