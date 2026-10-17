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
from superuse.server.network import LanIdentityRegistry, NetworkAuthority
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
        # Defense in depth behind the config-layer boundary: no code path in
        # a frozen release build may ever construct simulated hardware.
        from superuse.buildenv import simulated_devices_allowed

        if config.simulated and not simulated_devices_allowed():
            raise RuntimeError("simulated devices are not available in release builds")
        self.clock = MonotonicClock()
        self.credentials = credentials or Credentials()
        self.latch_store = FileLatchStore(runtime_dir.parent / "state" / "control.json")
        self._supervisor_tick_s = supervisor_tick_s

        self.actor: SerialActor | None = None
        self.capture: SimulatedCapture | CaptureWorker | None = None
        # True only when both devices opened during start(); a missing device
        # keeps the broker (tray/UI/stdio) alive while the supervisor retries
        # (task 2026-10-17 T2).
        self.initial_device_ok = False
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
        # LAN access policy: shared key, entry tagging, network generation
        # (task 2026-10-17 T3). Config changes need a restart by design.
        self.network = NetworkAuthority(key=config.network["key"])
        self.lan_identities = LanIdentityRegistry(self.network)
        self.api = Api(
            config=config,
            manager=self.manager,
            scheduler=self.scheduler,
            sessions=self.sessions,
            hub=self.hub,
            web_dir=web_dir,
            network=self.network,
        )
        self.ws = None
        self.runner: web.AppRunner | None = None
        self.port = 0
        self._periodic: asyncio.Task | None = None
        # Streamable HTTP MCP (task T4). Local mode binds 127.0.0.1:mcp_port
        # when a key is configured; LAN mode rebinds via the tray (T6).
        self.http_mcp = None
        self.http_mcp_error: str | None = None
        # LAN web entrance (task T6): present only while LAN is enabled.
        self.lan_web = None
        # Serializes enable/disable: rapid tray clicks and programmatic
        # toggles share one transaction at a time.
        self.lan_toggle_lock = asyncio.Lock()

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

    # ------------------------------------------------------------------
    # Device attach (idempotent; used by start() and the supervisor)
    # ------------------------------------------------------------------

    def _open_serial(self) -> bool:
        """Build, start and wire the serial actor; False when it cannot open.

        Match failures (absent/ambiguous identity) land in DISCONNECTED with
        a detail string; open failures (device busy) land in FAULT. Either
        way the broker keeps serving status while the supervisor retries.
        """
        try:
            transport, actor = self._build_serial()
            try:
                actor.start(query_device_info=True)
            except Exception:
                # No writer/reader threads exist yet; close the transport
                # directly — actor.stop() would broadcast a "disconnected"
                # that overwrites the FAULT recorded below.
                with contextlib.suppress(Exception):
                    transport.close()
                raise
        except ControlError as exc:
            self._serial_transport = None
            self.actor = None
            self.manager.set_serial_status(DeviceStatus.DISCONNECTED)
            self.manager.set_serial_detail(f"{exc.code.value}: {exc}")
            return False
        except Exception as exc:
            self._serial_transport = None
            self.actor = None
            self.manager.set_serial_status(DeviceStatus.FAULT)
            self.manager.set_serial_detail(f"open failed: {type(exc).__name__}")
            return False
        self._serial_transport = transport
        self.actor = actor
        self.manager._actor = actor  # swap the deferred placeholder
        self.scheduler._actor = actor
        self.manager.set_serial_detail(None)
        return True

    def _open_capture(self) -> bool:
        """Build and start the capture source; False when it cannot open."""
        try:
            self.hub.begin_capture_generation()
            capture = self._build_capture()
            capture.start()
        except ControlError as exc:
            self.capture = None
            self.manager.set_video_status(DeviceStatus.DISCONNECTED)
            self.manager.set_video_detail(f"{exc.code.value}: {exc}")
            return False
        except Exception as exc:
            self.capture = None
            self.manager.set_video_status(DeviceStatus.FAULT)
            self.manager.set_video_detail(f"open failed: {type(exc).__name__}")
            return False
        self.capture = capture
        self.manager.set_video_detail(None)
        return True

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

        # Devices first (fast path keeps the neutralize-before-serve order),
        # but a missing device is a state, not a startup failure: the tray,
        # UI and stdio status must stay observable while the supervisor
        # retries on its backoff ladder (task 2026-10-17 T2). Serial and
        # video attach independently so "only serial" / "only video" are
        # distinguishable states.
        serial_ok = self._open_serial()
        capture_ok = self._open_capture()
        self.initial_device_ok = serial_ok and capture_ok

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
        await self._start_local_http_mcp()
        if self.initial_device_ok:
            logger.info(
                "broker listening on 127.0.0.1:%s (instance %s)",
                self.port,
                self.manager.instance_id,
            )
        else:
            logger.info(
                "broker listening on 127.0.0.1:%s (instance %s); waiting for the"
                " SuperKVM device (serial=%s video=%s)",
                self.port,
                self.manager.instance_id,
                "attached" if serial_ok else "missing",
                "attached" if capture_ok else "missing",
            )

    def _state_provider(self):
        """Read-only status for the superuse://state resource (in-process)."""
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
        return {"state": self.manager.get_state().to_wire(), "capabilities": capabilities}

    async def _start_local_http_mcp(self) -> None:
        """Bind 127.0.0.1:mcp_port when a key is configured (best effort).

        A port conflict or bind failure is recorded and surfaced via
        /v1/network — it must not take the broker, tray or devices down.
        """
        from superuse.mcp.http_service import build_http_mcp_service

        if not self.network.key_configured:
            logger.info("HTTP MCP not enabled: no network key configured")
            return
        service = build_http_mcp_service(self, state_provider=self._state_provider)
        try:
            await service.start(host="127.0.0.1", port=self.config.network["mcp_port"])
        except Exception as exc:  # noqa: BLE001
            # Any failure here (bind, config, SDK) is a degraded network
            # surface, never a reason to take the broker down (review R1).
            self.http_mcp_error = str(exc)
            self.network.last_bind_error = str(exc)
            logger.warning("local HTTP MCP failed to start: %s", exc)
            return
        self.http_mcp = service
        self.network.last_bind_error = None

    def rescan_devices(self) -> None:
        """User-triggered device re-detection (tray/menu; loop thread)."""
        if self._supervisor is not None:
            self._supervisor.rescan()

    # ------------------------------------------------------------------
    # LAN enable/disable transaction (task 2026-10-17 T6)
    # ------------------------------------------------------------------

    def lan_addresses(self) -> list[str]:
        """Actual local IPv4 addresses for display (shared enumeration)."""
        from superuse.server.network import local_ipv4_addresses

        return local_ipv4_addresses()

    def _new_lan_web(self):
        from superuse.server.lan import LanApi, LanWebServer

        api = LanApi(
            self.api,
            lan_identities=self.lan_identities,
            authority=self.network,
            web_port=self.config.network["web_port"],
        )
        return LanWebServer(api)

    async def enable_lan(self) -> dict:
        """Turn LAN access on; serialized and transactional (see below)."""
        async with self.lan_toggle_lock:
            return await self._enable_lan_locked()

    async def _enable_lan_locked(self) -> dict:
        """Both entries must bind or nothing changes.

        Order: bind the LAN web listener first (a failure leaves the current
        state untouched), then rebind HTTP MCP to the LAN host. If the MCP
        rebind fails, the LAN web listener is rolled back and the local MCP
        binding is restored — never a half-open state or a fake success.
        """
        from superuse.mcp.http_service import build_http_mcp_service

        if not self.network.key_configured:
            return {"ok": False, "error": "key_missing"}
        if self.lan_web is not None:
            return {"ok": True, "addresses": self.lan_addresses()}

        network = self.config.network
        host, web_port, mcp_port = network["lan_host"], network["web_port"], network["mcp_port"]
        web_server = self._new_lan_web()
        try:
            await web_server.start(host=host, port=web_port)
        except OSError as exc:
            self.network.last_bind_error = str(exc)
            logger.warning("LAN web bind failed: %s", exc)
            return {"ok": False, "error": "bind_error", "detail": str(exc)}

        old_mcp, self.http_mcp = self.http_mcp, None
        if old_mcp is not None:
            with contextlib.suppress(Exception):
                await old_mcp.stop()  # documented brief reconnect window
        new_mcp = build_http_mcp_service(self, state_provider=self._state_provider)
        try:
            await new_mcp.start(host=host, port=mcp_port)
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            logger.warning("LAN MCP rebind failed, rolling back: %s", exc)
            with contextlib.suppress(Exception):
                await web_server.stop()
            await self._restore_local_mcp()
            self.network.last_bind_error = detail
            return {"ok": False, "error": "bind_error", "detail": detail}
        self.http_mcp = new_mcp
        self.lan_web = web_server
        self.network.last_bind_error = None
        self.network.set_enabled(True)
        self.network.addresses_provider = self.lan_addresses
        addresses = self.lan_addresses() or ["127.0.0.1"]
        logger.info("LAN access enabled on %s (web :%s, mcp :%s)", host, web_port, mcp_port)
        return {"ok": True, "addresses": addresses}

    async def _restore_local_mcp(self) -> None:
        from superuse.mcp.http_service import build_http_mcp_service

        local = build_http_mcp_service(self, state_provider=self._state_provider)
        try:
            await local.start(host="127.0.0.1", port=self.config.network["mcp_port"])
            self.http_mcp = local
            self.network.last_bind_error = None
        except Exception as exc:  # noqa: BLE001
            self.network.last_bind_error = str(exc)
            logger.warning("local HTTP MCP restore failed: %s", exc)

    async def disable_lan(self) -> dict:
        """Turn LAN access off, serialized (docs §3.5 ordering below)."""
        async with self.lan_toggle_lock:
            return await self._disable_lan_locked()

    async def _disable_lan_locked(self) -> dict:
        """The network generation flips first so remote requests fail closed;
        remote owners then release through the control manager's barrier;
        only afterwards are the listeners and their sessions torn down. The
        local MCP binding comes back so local agents only see the documented
        reconnect window. Old cookies/page tokens/leases never resurrect.
        """
        if self.lan_web is None:
            return {"ok": True}
        # 1. Fail-closed window + identity invalidation.
        self.network.set_enabled(False)
        self.network.addresses_provider = None
        # 2. Remote owners release through the normal barrier; only the LAN
        # entrance's sessions are touched — local pages/agents keep theirs.
        for session in self.sessions.close_entrance("lan"):
            await self.manager.session_closed(session)
        self.lan_identities.invalidate_all()
        # 3. Revocation point for HTTP MCP agents BEFORE any listener waits
        # (review 2026-10-17 R3): seal admission (new sessions 503, in-flight
        # tools answer structured errors), then close every session's client
        # — lease release through the control manager's barrier cancels
        # running jobs while the internal API is still up. Sealing is on this
        # LAN-bound service instance only; the local MCP is unaffected.
        mcp, self.http_mcp = self.http_mcp, None
        if mcp is not None:
            mcp.seal()
            with contextlib.suppress(Exception):
                await mcp.drain()
        # 4. Listeners down: remote web first, then the (LAN-bound) MCP.
        web, self.lan_web = self.lan_web, None
        with contextlib.suppress(Exception):
            await web.stop()
        if mcp is not None:
            with contextlib.suppress(Exception):
                await mcp.stop()
        # 5. Local MCP back on its loopback binding.
        await self._restore_local_mcp()
        logger.info("LAN access disabled")
        return {"ok": True}

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
        # HTTP MCP sessions own broker leases: close their clients (via the
        # service) while the loopback API is still up to serve the releases.
        if self.http_mcp is not None:
            service, self.http_mcp = self.http_mcp, None
            with contextlib.suppress(Exception):
                await service.stop()
        if self.lan_web is not None:
            web, self.lan_web = self.lan_web, None
            self.network.set_enabled(False)
            with contextlib.suppress(Exception):
                await web.stop()
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
            state = self.manager.get_state()
            if (
                state.mode.value in {"idle", "human_hold", "human", "agent"}
                and state.serial_status is DeviceStatus.READY
                and state.video_status is DeviceStatus.READY
                and state.frame_age_ms is not None
                and state.frame_age_ms <= self.config.freshness_s * 1000
            ):
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
    - attach devices that were missing at startup (first plug-in) and reopen
      them after a fault/disconnect — the same backoff ladder and identity
      rules cover both, so a different device is never silently adopted;
    - drive ``manager.recover()`` once serial+video are healthy again and a
      fresh frame exists (latch/fence ordering stays inside the manager);
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
        self._wake = asyncio.Event()

    def rescan(self) -> None:
        """User-triggered re-detection: zero the ladders and wake the loop.

        Only meaningful on the broker loop (the tray dispatches there); a
        rescan never interrupts an in-flight open, it only makes the next
        attempt immediate.
        """
        self._serial_attempts = 0
        self._capture_attempts = 0
        self._capture_retry_at = 0.0
        self._wake.set()

    async def _sleep(self, seconds: float) -> None:
        """Backoff sleep that a rescan latch can shorten to zero.

        The wake event is consumed HERE, not by the tick loop: a rescan that
        fires between cycles must still shorten the next backoff.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        self._wake.clear()

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
        """Attach the serial device: first plug-in and reconnects share this."""
        broker = self._broker
        if self._reconnecting:
            return
        actor = broker.actor
        first_attach = actor is None
        if not first_attach and actor.status not in ("fault", "disconnected"):
            return
        self._reconnecting = True
        try:
            backoff = self.BACKOFF_S[min(self._serial_attempts, len(self.BACKOFF_S) - 1)]
            await self._sleep(backoff)
            if broker.manager.mode.value == "stopping":
                return
            current = broker.actor
            if current is not actor:  # someone else attached/replaced meanwhile
                return
            if first_attach:
                if broker._open_serial():
                    attempts = self._serial_attempts + 1
                    self._serial_attempts = 0
                    logger.info("serial device attached (attempt %d)", attempts)
                else:
                    self._serial_attempts += 1
                return
            # Identity is re-verified by _open_serial via discovery; a changed
            # or missing device raises instead of adopting a substitute. The
            # old actor must stop FIRST: its terminal "disconnected" callback
            # must never land after the replacement's "ready".
            old = broker.actor
            old.stop()
            if broker._open_serial():
                self._serial_attempts += 1
                logger.info(
                    "serial reconnect attempt %d (identity re-matched)", self._serial_attempts
                )
            else:
                self._serial_attempts += 1
        except Exception:
            self._serial_attempts += 1
            logger.exception("serial reconnect failed")
        finally:
            self._reconnecting = False

    async def _maybe_restart_capture(self) -> None:
        broker = self._broker
        if broker.capture is None:
            # First attach: same bounded ladder as the serial side.
            if broker.clock.now < self._capture_retry_at:
                return
            if broker._open_capture():
                self._capture_attempts = 0
                logger.info("capture source attached")
            else:
                self._capture_attempts += 1
                self._capture_retry_at = (
                    broker.clock.now
                    + self.BACKOFF_S[min(self._capture_attempts, len(self.BACKOFF_S) - 1)]
                )
            return
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
