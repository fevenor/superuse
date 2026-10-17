"""Broker assembly: devices, control core, HTTP/WS service, runtime file.

Startup keeps the documented ordering (docs/development/windows-operations.md §1):
devices open and neutralize first, the manager recovers to idle only after a
fresh frame, then the loopback HTTP server binds and the runtime file is
written. Shutdown mirrors it in reverse, always bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web

from superuse.config import Config
from superuse.core.clock import MonotonicClock
from superuse.core.control import ControlManager
from superuse.core.errors import ControlError, ErrorCode
from superuse.core.frames import BasisRegistry, FrameHub
from superuse.core.models import DeviceStatus
from superuse.core.scheduler import InputScheduler
from superuse.device.capture import CaptureWorker
from superuse.device.discovery import (
    list_serial_ports,
    list_video_devices,
    match_serial_port,
    match_video_device,
)
from superuse.device.serial_actor import SerialActor
from superuse.device.simulated import SimulatedCapture, SimulatedSerial
from superuse.device.transport import RealSerialTransport
from superuse.server.http import Api
from superuse.server.sessions import Credentials, SessionRegistry
from superuse.server.websocket import install_ws

logger = logging.getLogger(__name__)

RUNTIME_FILENAME = "broker.json"


class FileLatchStore:
    """Crash-safe human-latch flag (docs/development/control-state-machine.md §6)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._memory: bool | None = None

    def save(self, latched: bool) -> None:
        self._memory = latched
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"human_latched": latched}), encoding="utf-8")
            # The latch flag carries no secrets, but state files share the
            # runtime directory's user-only ACL policy (windows-operations §2).
            from superuse.windows.security import apply_user_only_acl

            apply_user_only_acl(tmp)
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("latch persist failed; in-memory value kept")

    def load(self) -> bool | None:
        if self._memory is not None:
            return self._memory
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return bool(data["human_latched"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError):
            # Present-but-corrupt state recovers conservatively as latched.
            return True


class Broker:
    def __init__(
        self,
        config: Config,
        *,
        runtime_dir: Path,
        web_dir: Path | None = None,
        credentials: Credentials | None = None,
        supervisor_tick_s: float = 0.5,
    ) -> None:
        self.config = config
        self.runtime_dir = runtime_dir
        self.web_dir = web_dir
        self.clock = MonotonicClock()
        self.credentials = credentials or Credentials()
        self.latch_store = FileLatchStore(runtime_dir.parent / "state" / "control.json")
        self._supervisor_tick_s = supervisor_tick_s

        self.actor: SerialActor | None = None
        self.capture: SimulatedCapture | CaptureWorker | None = None
        self._capture_generation = 0
        self._video_status_generation = 0
        self._serial_transport = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._supervisor: DeviceSupervisor | None = None
        self._supervisor_task: asyncio.Task | None = None

        # The actor is created in start() (needs the running loop); the
        # manager needs it at construction, so a placeholder is swapped out
        # before any barrier can run.
        self.manager = ControlManager(
            clock=self.clock,
            actor=_DeferredActor(),
            latch_store=self.latch_store,
            source="simulated" if config.simulated else "real",
            lease_ttl_s=config.lease_ttl_s,
            fence_timeout_s=config.fence_timeout_s,
            freshness_s=config.freshness_s,
            stall_timeout_s=config.stall_timeout_s,
        )
        self.bases = BasisRegistry(self.manager.instance_id)
        self.hub = FrameHub(
            instance_id=self.manager.instance_id,
            bases=self.bases,
            source="simulated" if config.simulated else "real",
            snapshot_max_width=config.section("video")["snapshot_max_width"],
            jpeg_quality=config.section("video")["jpeg_quality"],
        )
        self.scheduler = InputScheduler(
            clock=self.clock,
            actor=self.manager._actor,
            manager=self.manager,
            bases=self.bases,
            action_timeout_s=config.action_timeout_s,
            queue_limit=config.section("control")["input_queue_limit"],
        )
        self.manager.set_owner_cancel_hook(self.scheduler.cancel_owner_jobs)
        self.sessions = SessionRegistry(
            credentials=self.credentials, instance_id=self.manager.instance_id
        )
        self.api = Api(
            config=config,
            manager=self.manager,
            scheduler=self.scheduler,
            sessions=self.sessions,
            hub=self.hub,
            web_dir=web_dir,
        )
        self.ws = None
        self.runner: web.AppRunner | None = None
        self.port = 0
        self._periodic: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    def _build_serial(self) -> tuple[object, SerialActor]:
        loop = asyncio.get_running_loop()
        control = self.config.section("control")
        if self.config.simulated:
            transport = SimulatedSerial()
            factory = lambda: transport  # noqa: E731
        else:
            device_cfg = self.config.section("device")
            identity = device_cfg["serial"]
            port = match_serial_port(
                list_serial_ports(),
                vid=identity["vid"],
                pid=identity["pid"],
                serial_number=identity["serial_number"],
            )
            transport = RealSerialTransport(
                port.device,
                baud=identity["baud"],
                read_timeout_s=identity["read_timeout_ms"] / 1000,
            )
            factory = lambda t=transport: t  # noqa: E731
        actor = SerialActor(
            factory,
            loop=loop,
            write_timeout_s=device_cfg_serial_write_timeout(self.config),
            input_queue_limit=control["input_queue_limit"],
            on_status=self._on_serial_status,
        )
        return transport, actor

    def _build_capture(self):
        """One frame source for both modes, publishing onto the broker loop.

        Real and simulated sources share the same thread-safe channel: their
        worker threads call ``publish``/``geometry``, which hop onto the loop
        where FrameHub and the control manager live. No source-type special
        casing anywhere downstream (review R04).
        """
        loop = self._loop
        assert loop is not None
        generation = self.hub.capture_generation

        def publish(frame) -> None:
            loop.call_soon_threadsafe(self._on_capture_frame, generation, frame)

        def geometry(width: int, height: int) -> None:
            loop.call_soon_threadsafe(self._on_capture_geometry, generation, width, height)

        if self.config.simulated:
            video = self.config.section("device")["video"]
            return SimulatedCapture(
                fps=self.config.section("video")["publish_fps"] * 2,
                width=video["width"],
                height=video["height"],
                on_frame=publish,
            )
        video = self.config.section("device")["video"]
        camera = match_video_device(
            list_video_devices(video["backend"]),
            backend=video["backend"],
            name=video["name"],
            device_path=video["device_path"],
        )
        return CaptureWorker(
            backend=video["backend"],
            index=camera.index,
            width=video["width"],
            height=video["height"],
            fps=video["requested_fps"],
            on_geometry=geometry,
            on_frame=publish,
        )

    def _on_serial_status(self, status: str) -> None:
        mapping = {
            "ready": DeviceStatus.READY,
            "starting": DeviceStatus.STARTING,
            "fault": DeviceStatus.FAULT,
            "disconnected": DeviceStatus.DISCONNECTED,
        }
        self.manager.set_serial_status(mapping.get(status, DeviceStatus.FAULT))

    def _on_frame(self, frame) -> None:
        changed = self.hub.publish(frame)
        generation = self.hub.capture_generation
        if self._video_status_generation != generation:
            self._video_status_generation = generation
            self.manager.set_video_status(DeviceStatus.READY)
        self.manager.frame_received()
        if changed:
            self.manager.geometry_changed()

    def _on_capture_frame(self, generation: int, frame) -> None:
        if generation == self.hub.capture_generation:
            self._on_frame(frame)

    def _on_capture_geometry(self, generation: int, width: int, height: int) -> None:
        if generation == self.hub.capture_generation:
            self._on_geometry(width, height)

    def _on_geometry(self, width: int, height: int) -> None:
        logger.info("capture geometry changed to %sx%s", width, height)
        self.manager.geometry_changed()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        transport, actor = self._build_serial()
        self._serial_transport = transport
        self.actor = actor
        self.manager._actor = actor  # swap the deferred placeholder
        self.scheduler._actor = actor
        actor.start(query_device_info=True)

        self.hub.begin_capture_generation()
        capture = self._build_capture()
        self.capture = capture
        capture.start()

        app = self.api.build_app()
        self.ws = install_ws(self.api, app)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.5)
        await self.runner.setup()
        # Bounded graceful-shutdown window: cleanup must never park on live
        # sockets after the release barrier already finished (review R02).
        site = web.TCPSite(self.runner, self.config.section("server")["host"], 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        self.api.port = self.port
        await self.ws.start()
        self._write_runtime_file()
        self._periodic = asyncio.ensure_future(self._periodic_task())
        self._supervisor = DeviceSupervisor(self, tick_s=self._supervisor_tick_s)
        self._supervisor_task = asyncio.ensure_future(self._supervisor.run())
        logger.info(
            "broker listening on 127.0.0.1:%s (instance %s)", self.port, self.manager.instance_id
        )

    async def _periodic_task(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.1)
                now = time.monotonic()
                await self.manager.tick()
                for session in self.sessions.sweep(now):
                    await self.manager.session_closed(session)
                self.bases.sweep(now)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("periodic task crashed")

    async def stop(self) -> None:
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor_task
            self._supervisor_task = None
            self._supervisor = None
        if self._periodic is not None:
            self._periodic.cancel()
            self._periodic = None
        # Documented order (windows-operations §1): mark stopping, refuse new
        # input, stop jobs/leases and run the bounded release barrier FIRST —
        # runner.cleanup() waits for live requests/WS, so it must come after
        # the device is released, not before (otherwise a held events socket
        # delays the fence past the shutdown budget).
        await self.manager.stop()
        await self.scheduler.close()
        if self.ws is not None:
            await self.ws.stop()
        if self.runner is not None:
            await self.runner.cleanup()  # stop accepting; close connections
        if self.capture is not None:
            if getattr(self.capture, "fault", None) or getattr(self.capture, "fault_reason", None):
                self.manager.set_video_status(DeviceStatus.FAULT)
            self.capture.stop()
            self.capture = None
        if self.actor is not None:
            self.actor.stop()
            self.actor = None
        self._remove_runtime_file()

    # ------------------------------------------------------------------
    # Runtime file
    # ------------------------------------------------------------------

    def _runtime_path(self) -> Path:
        return self.runtime_dir / RUNTIME_FILENAME

    def _write_runtime_file(self) -> None:
        import platform

        path = self._runtime_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": 1,
            "instance_id": self.manager.instance_id,
            "pid": os.getpid(),
            "port": self.port,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "python": platform.python_version(),
            "credentials": {
                "agent_bootstrap": self.credentials.agent_bootstrap,
                "launcher": self.credentials.launcher,
            },
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _remove_runtime_file(self) -> None:
        # Only remove a file that still belongs to this instance.
        path = self._runtime_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("instance_id") == self.manager.instance_id:
                path.unlink()
        except (OSError, ValueError):
            pass

    async def wait_healthy(self, timeout_s: float = 15.0) -> None:
        """Drive recovery once a fresh frame exists (used at startup/tests)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.manager.mode.value == "idle":
                return
            with contextlib.suppress(ControlError):
                await self.manager.recover()
            await asyncio.sleep(0.1)
        raise ControlError(ErrorCode.DEVICE_UNAVAILABLE, "Device did not become healthy")


def default_web_dir() -> Path | None:
    """Locate the human UI assets: packaged bundle first, then repo checkout.

    PyInstaller exposes bundled data under sys._MEIPASS; an explicit
    SUPERUSE_WEB_DIR wins over both (tests use it to point at temp copies).
    """
    import os
    import sys

    override = os.environ.get("SUPERUSE_WEB_DIR")
    if override:
        candidate = Path(override)
        if (candidate / "index.html").is_file():
            return candidate
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / "web"
        if (candidate / "index.html").is_file():
            return candidate
    candidate = Path(__file__).resolve().parent / "web"  # installed wheel resources
    if (candidate / "index.html").is_file():
        return candidate
    candidate = Path(__file__).resolve().parents[2] / "web"  # source checkout
    return candidate if (candidate / "index.html").is_file() else None


def device_cfg_serial_write_timeout(config: Config) -> float:
    return config.section("device")["serial"]["write_timeout_ms"] / 1000


class DeviceSupervisor:
    """Recovery/reconnection loop (review R09, docs/development/device-video.md §5).

    Responsibilities:
    - drive ``manager.recover()`` once serial+video are healthy again and a
      fresh frame exists (latch/fence ordering stays inside the manager);
    - re-open the serial device with the *same configured identity* after a
      fault/disconnect, using the 1/2/4/8/10s backoff ladder; the identity
      match runs again on every attempt, so a different device is never
      silently adopted;
    - restart a faulted capture source when its old handle closed cleanly
      (a hung close keeps the thread slot occupied and is never replaced).

    Old jobs are never replayed: revocation already cancelled them and the
    manager only re-grants through the normal acquire path.
    """

    BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 10.0)

    def __init__(self, broker: Broker, *, tick_s: float = 0.5) -> None:
        self._broker = broker
        self._tick_s = tick_s
        self._serial_attempts = 0
        self._capture_attempts = 0
        self._capture_reconnect_pending = False
        self._capture_retry_at = 0.0
        self._reconnecting = False

    async def run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._tick_s)
                broker = self._broker
                if broker.manager.mode.value in ("stopping",):
                    return
                await self._maybe_reconnect_serial()
                await self._maybe_restart_capture()
                await self._maybe_recover()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("device supervisor crashed")

    async def _maybe_recover(self) -> None:
        broker = self._broker
        manager = broker.manager
        from superuse.core.models import Mode

        if manager.mode is not Mode.RECOVERY:
            return
        if manager.get_state().serial_status is not DeviceStatus.READY:
            return
        frame = broker.hub.latest
        if frame is None or (broker.clock.now - frame.monotonic) > broker.config.freshness_s:
            return
        try:
            await manager.recover()
            self._serial_attempts = 0
            self._capture_attempts = 0
        except ControlError:
            pass  # not healthy enough yet; keep waiting

    async def _maybe_reconnect_serial(self) -> None:
        broker = self._broker
        if self._reconnecting:
            return
        actor = broker.actor
        if actor is None or actor.status not in ("fault", "disconnected"):
            return
        self._reconnecting = True
        try:
            backoff = self.BACKOFF_S[min(self._serial_attempts, len(self.BACKOFF_S) - 1)]
            await asyncio.sleep(backoff)
            if broker.manager.mode.value == "stopping" or broker.actor is not actor:
                return
            # Identity is re-verified by _build_serial via discovery; a changed
            # or missing device raises instead of adopting a substitute.
            old = broker.actor
            transport, replacement = broker._build_serial()
            old.stop()
            broker._serial_transport = transport
            broker.actor = replacement
            broker.manager._actor = replacement
            broker.scheduler._actor = replacement
            replacement.start(query_device_info=True)
            self._serial_attempts += 1
            logger.info("serial reconnect attempt %d (identity re-matched)", self._serial_attempts)
        except ControlError as exc:
            self._serial_attempts += 1
            logger.warning("serial reconnect deferred: %s", exc)
        except Exception:
            self._serial_attempts += 1
            logger.exception("serial reconnect failed")
        finally:
            self._reconnecting = False

    async def _maybe_restart_capture(self) -> None:
        broker = self._broker
        capture = broker.capture
        status = getattr(capture, "status", None)
        fault_reason = getattr(capture, "fault_reason", None) or getattr(capture, "fault", None)
        if fault_reason in ("capture_close_hung", "close_hung"):
            return  # thread slot still occupied; never leak a second thread
        if not self._capture_reconnect_pending:
            if status != "fault" or not fault_reason:
                return
            broker.manager.set_video_status(DeviceStatus.FAULT)
            try:
                capture.stop()
            except Exception:
                logger.exception("capture close failed; replacement refused")
                return
            # stop() itself may discover a stuck reader. Never replace that
            # source, even if the original fault was only read_failed.
            reason = getattr(capture, "fault_reason", None) or getattr(capture, "fault", None)
            if reason in ("capture_close_hung", "close_hung"):
                return
            broker.hub.begin_capture_generation()
            self._capture_reconnect_pending = True
            self._capture_retry_at = (
                broker.clock.now
                + self.BACKOFF_S[min(self._capture_attempts, len(self.BACKOFF_S) - 1)]
            )
        if broker.clock.now < self._capture_retry_at:
            return
        self._capture_attempts += 1
        replacement = None
        try:
            replacement = broker._build_capture()
            broker.capture = replacement
            replacement.start()
            self._capture_reconnect_pending = False
            logger.info("capture restarted after fault (attempt %d)", self._capture_attempts)
        except Exception:
            if replacement is not None:
                replacement.stop()
            self._capture_retry_at = (
                broker.clock.now
                + self.BACKOFF_S[min(self._capture_attempts, len(self.BACKOFF_S) - 1)]
            )
            logger.exception("capture restart failed")


class _DeferredActor:
    """Placeholder actor used before start(); refuses every operation."""

    def submit(self, report):  # pragma: no cover - replaced in start()
        from superuse.core.ports import WriteOutcome

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        future.set_result(WriteOutcome(0, "CLOSED"))
        return future

    async def fence(self, ticket, timeout_s):  # pragma: no cover
        from superuse.core.ports import FenceResult

        return FenceResult(ok=False, error="WRITE_FAILED")
