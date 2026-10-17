"""serve/open/status/diagnose command implementations.

``serve`` owns the single-instance mutex, runs the broker's asyncio core on a
daemon thread and (unless --headless) the tray loop on the main thread.
A second serve forwards an "open UI" request to the healthy primary and
exits. Diagnostics are read-only; no camera is opened for enumeration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

from superuse.app import Broker, default_web_dir
from superuse.buildenv import is_frozen_build
from superuse.config import ConfigError, load_config, migrate_legacy_simulated_config
from superuse.core.models import Session, SessionKind
from superuse.paths import resolve_paths
from superuse.windows import instance, security
from superuse.windows.tray import TrayApp, open_browser, state_label

logger = logging.getLogger(__name__)


def setup_logging(config, *, runtime_dir: Path, console: bool) -> None:
    """Rotating sanitized logs (docs/development/windows-operations.md §5).

    UTC timestamps, one 5MiB file by default with a bounded number of
    backups. Never log tokens, input text or screenshots — the format only
    carries module, level and the (sanitized) message.
    """
    import logging.handlers

    log_cfg = config.section("logging")
    root = logging.getLogger("superuse")
    root.setLevel(log_cfg["level"])
    formatter = logging.Formatter(
        "%(asctime)s %(relativeCreated)8dms %(name)s %(levelname)s %(message)s"
    )
    formatter.converter = __import__("time").gmtime  # UTC in logs
    logs_dir = runtime_dir.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / "superuse.log",
        maxBytes=log_cfg["max_bytes"],
        backupCount=log_cfg["backup_count"],
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)


def default_runtime_dir() -> Path:
    return resolve_paths().runtime_dir


def _load(path: str | Path | None, *, want_simulated: bool = False, force_real: bool = False):
    overrides = None
    if want_simulated:
        from superuse.buildenv import simulated_devices_allowed

        if not simulated_devices_allowed():
            raise SystemExit("simulated devices are not available in release builds")
        overrides = {"device": {"mode": "simulated"}}
    elif force_real:
        overrides = {"device": {"mode": "real"}}
    try:
        return load_config(user_file=Path(path) if path else None, overrides=overrides)
    except (ConfigError, OSError, ValueError) as exc:
        raise SystemExit(f"configuration error: {exc}") from exc


class ServeApp:
    def __init__(self, args) -> None:
        self.args = args
        self.paths = resolve_paths(runtime_dir=args.runtime_dir, config_file=args.config)
        self.runtime_dir = self.paths.runtime_dir
        config = self.paths.config_file
        # A missing implicit portable config is initialized after the mutex;
        # a missing explicitly selected config remains a configuration error.
        pending = self.paths.portable and not args.config and config and not config.exists()
        self._want_simulated = bool(getattr(args, "simulated", False))
        if is_frozen_build():
            # Release builds load exactly once, after the legacy simulated ->
            # real migration ran (run()); an early load would reject the very
            # config that migration is supposed to repair.
            self.config = None  # type: ignore[assignment]
        else:
            self.config = _load(None if pending else config, want_simulated=self._want_simulated)
        self.mutex = instance.BrokerMutex()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.broker: Broker | None = None
        self._thread: threading.Thread | None = None
        self._startup_error: Exception | None = None
        self._stopped = threading.Event()
        self._tray_session: Session | None = None
        self._tray_app: TrayApp | None = None
        self._migration_notice: str | None = None
        self._migration_failed = False
        self._migration_error: str | None = None

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def run(self) -> int:
        if not self.mutex.acquire():
            return self._forward_to_primary()
        try:
            try:
                self.paths.initialize()
                self._migrate_legacy_config()
                self.config = _load(
                    self.paths.config_file,
                    want_simulated=self._want_simulated,
                    force_real=self._migration_failed,
                )
                setup_logging(self.config, runtime_dir=self.runtime_dir, console=self.args.headless)
                if self._migration_notice:
                    logger.warning("config migration: %s", self._migration_notice)
                if self._migration_failed:
                    logger.error(
                        "config migration to real device mode failed (%s); the file still"
                        " says simulated and was NOT saved — running with real devices"
                        " for this session only",
                        self._migration_error,
                    )
            except OSError as exc:
                raise SystemExit(f"Cannot write application data: {exc}") from exc
            self._start_broker_thread()
            if self.args.headless or sys.platform != "win32" or self._no_tray_available():
                self._stopped.wait()
                return 0
            self._run_tray_blocking()
            return 0
        finally:
            self._shutdown()

    def _migrate_legacy_config(self) -> None:
        """One-time simulated→real rewrite, release builds only (task T1).

        Development checkouts never rewrite a config the developer explicitly
        set to simulated. A failed write is reported after logging is up and
        the session continues in real mode; it never falls back to simulation.
        """
        if not is_frozen_build() or self._want_simulated:
            return
        config_file = self.paths.config_file
        if config_file is None or not config_file.exists():
            return
        try:
            self._migration_notice = migrate_legacy_simulated_config(config_file)
        except OSError as exc:
            self._migration_failed = True
            self._migration_error = str(exc)

    @staticmethod
    def _no_tray_available() -> bool:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401

            return False
        except ImportError:
            logger.warning("tray dependencies missing; running headless")
            return True

    def _forward_to_primary(self) -> int:
        # Second instance: ask the healthy primary to open its UI, then exit.
        record = asyncio.run(instance.find_healthy_broker(self.runtime_dir))
        if record is None:
            print("another broker owns the mutex but is not healthy", file=sys.stderr)
            return 1
        url = _launch_url(record)
        if url:
            open_browser(url)
            return 0
        return 1

    # ------------------------------------------------------------------
    # Broker thread
    # ------------------------------------------------------------------

    def _start_broker_thread(self) -> None:
        started = threading.Event()

        def _run() -> None:
            asyncio.run(self._broker_main(started))

        self._thread = threading.Thread(target=_run, name="broker-loop", daemon=True)
        self._thread.start()
        if not started.wait(timeout=30):
            raise SystemExit("broker failed to start")
        if self._startup_error is not None:
            raise SystemExit(
                "broker failed to start; see the application log"
            ) from self._startup_error

    async def _broker_main(self, started: threading.Event) -> None:
        self.loop = asyncio.get_running_loop()
        try:
            try:
                self.broker = Broker(
                    self.config,
                    runtime_dir=self.runtime_dir,
                    web_dir=default_web_dir(),
                )
                await self.broker.start()
                if self.broker.initial_device_ok:
                    try:
                        await self.broker.wait_healthy(timeout_s=15)
                    except Exception:
                        logger.warning("device not healthy yet; staying in recovery")
                else:
                    # Tray/UI/stdio are already serving; the supervisor keeps
                    # retrying the device on its backoff ladder (task T2).
                    logger.info("device not attached; waiting for the SuperKVM device")
                security.apply_user_only_acl(instance.runtime_path(self.runtime_dir))
            except Exception as exc:
                self._startup_error = exc
                logger.exception("broker start failed: %s", exc)
                self._stopped.set()
                return
            finally:
                started.set()
            await self._stopped_async()
        finally:
            if self.broker is not None:
                await self.broker.stop()

    async def _stopped_async(self) -> None:
        while not self._stopped.is_set():
            await asyncio.sleep(0.2)

    def _shutdown(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
        self.mutex.release()

    # ------------------------------------------------------------------
    # Tray actions (dispatched onto the broker loop)
    # ------------------------------------------------------------------

    def _launcher_headers(self, record: dict | None = None) -> dict | None:
        record = record or instance.read_runtime_file(self.runtime_dir)
        if record is None:
            return None
        token = record.get("credentials", {}).get("launcher")
        return {"Authorization": f"Bearer {token}"} if token else None

    async def open_ui(self) -> None:
        """Open the UI URL; runs on the broker loop as an async tray action.

        The one-time-URL fetch is awaited (no asyncio.run on a live loop) and
        the browser opens on its own thread so the loop keeps serving
        heartbeats/input while it runs (review R07).
        """
        record = instance.read_runtime_file(self.runtime_dir)
        url = await fetch_launch_url(record)
        if url:
            open_browser(url)
        else:
            logger.warning("cannot open UI: broker not healthy")

    async def takeover_and_open(self) -> None:
        """Tray takeover: local human session walks the barrier first.

        Runs as a task on the broker loop; nothing blocks the loop, and the
        browser page later takes over from the tray session through the
        normal barrier (the tray lease never leaks into the URL).
        """
        broker = self.broker
        assert broker is not None
        session = Session(
            session_id=f"tray-{instance.new_instance_marker()[:8]}",
            kind=SessionKind.HUMAN,
            label="tray",
        )
        try:
            await broker.manager.takeover(session)
            self._tray_session = session
        except Exception as exc:
            logger.warning("tray takeover pending: %s", exc)
        await self.open_ui()

    async def handback(self) -> None:
        broker = self.broker
        assert broker is not None
        try:
            session = self._tray_session or Session(
                session_id=f"tray-{instance.new_instance_marker()[:8]}",
                kind=SessionKind.HUMAN,
                label="tray",
            )
            await broker.manager.handback(session)
        except Exception as exc:
            logger.info("tray handback not applicable: %s", exc)

    def copy_diagnostics(self) -> str:
        return diagnostics_text(self.config, self.broker)

    async def rescan_devices(self) -> None:
        if self.broker is not None:
            self.broker.rescan_devices()

    async def set_lan(self, enabled: bool) -> None:
        """Tray LAN toggle; the broker serializes the transaction itself."""
        broker = self.broker
        if broker is None:
            return
        result = await (broker.enable_lan() if enabled else broker.disable_lan())
        if not result.get("ok"):
            logger.warning("LAN %s failed: %s", "enable" if enabled else "disable", result)

    def lan_enabled(self) -> bool:
        return self.broker is not None and self.broker.network.enabled

    def lan_toggle_available(self) -> bool:
        return self.broker is not None and self.broker.network.key_configured

    def lan_address_text(self) -> str:
        broker = self.broker
        if broker is None or broker.lan_web is None:
            return "局域网访问未开启"
        network = self.config.network
        addresses = broker.lan_addresses() or ["127.0.0.1"]
        lines = [f"浏览器: http://{address}:{network['web_port']}" for address in addresses]
        lines.extend(f"MCP: http://{address}:{network['mcp_port']}/mcp" for address in addresses)
        return "\n".join(lines)

    def quit(self) -> None:
        self._stopped.set()
        # The tray loop owns the main thread; stopping the broker alone would
        # leave the icon running forever (process hang on quit).
        if self._tray_app is not None:
            self._tray_app.stop()

    def state_summary(self) -> str:
        if self.broker is None:
            return "启动中"
        state = self.broker.manager.get_state()
        label = state_label(state.mode, state.source == "simulated")
        network = self.broker.network
        if network.last_bind_error:
            lan = "局域网端口错误"
        elif not network.key_configured:
            lan = "局域网未配置密钥"
        elif network.enabled:
            lan = "局域网已开启"
        else:
            lan = "局域网已关闭"
        label = f"{label}；{lan}"
        if self._migration_notice or self._migration_failed:
            return f"已升级为真实设备模式，请连接设备；{label}"
        return label

    def _run_tray_blocking(self) -> None:
        web_dir = default_web_dir() or Path()
        # Pillow cannot decode SVG: the tray needs a raster icon (review R06).
        icon_path = web_dir / "icon.png"
        app = TrayApp(
            loop=self.loop,
            on_open_ui=self.open_ui,
            on_takeover_and_open=self.takeover_and_open,
            on_handback=self.handback,
            on_copy_diagnostics=self.copy_diagnostics,
            on_quit=self.quit,
            get_state_summary=self.state_summary,
            icon_path=str(icon_path) if icon_path.exists() else None,
            on_rescan_devices=self.rescan_devices,
            on_set_lan=self.set_lan,
            lan_enabled=self.lan_enabled,
            lan_toggle_available=self.lan_toggle_available,
            lan_address_text=self.lan_address_text,
        )
        self._tray_app = app
        app.run()


async def fetch_launch_url(record: dict | None) -> str | None:
    """One-time UI URL from a healthy broker; None on any failure."""
    if record is None:
        return None
    import aiohttp

    token = record.get("credentials", {}).get("launcher")
    if not token:
        return None
    try:
        async with aiohttp.ClientSession(
            base_url=f"http://127.0.0.1:{record['port']}",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as http:
            async with http.post(
                "/v1/ui/launch",
                json={"request_id": "00000000-0000-0000-0000-000000000000"},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                body = await response.json()
                return body.get("url") if response.status == 200 else None
    except (aiohttp.ClientError, TimeoutError):
        return None


def _launch_url(record: dict | None) -> str | None:
    return asyncio.run(fetch_launch_url(record))


# ----------------------------------------------------------------------
# open / status / diagnose
# ----------------------------------------------------------------------


def run_open(args) -> int:
    runtime_dir = resolve_paths(runtime_dir=args.runtime_dir).runtime_dir
    record = asyncio.run(instance.find_healthy_broker(runtime_dir))
    if record is None:
        print("no healthy broker; start Superuse from its tray app first", file=sys.stderr)
        return 1
    url = _launch_url(record)
    if not url:
        print("broker refused to issue a UI URL", file=sys.stderr)
        return 1
    open_browser(url)
    return 0


def run_status(args) -> int:
    runtime_dir = resolve_paths(runtime_dir=args.runtime_dir).runtime_dir
    record = instance.read_runtime_file(runtime_dir)
    if record is None:
        print(json.dumps({"broker": False}))
        return 1
    print(json.dumps({"broker": True, "runtime": security.sanitize_runtime_record(record)}))
    return 0


def diagnostics_text(config, broker: Broker | None) -> str:
    lines = [
        f"source={config.section('device')['mode']}",
        "serial_identity=0x1209:0x0001",
        f"video_backend={config.section('device')['video']['backend']}",
    ]
    if broker is not None:
        state = broker.manager.get_state()
        lines.append(
            f"state={state_label(state.mode, state.source == 'simulated')} "
            f"serial={state.serial_status.value} video={state.video_status.value} "
            f"frame_age_ms={state.frame_age_ms}"
        )
        serial = getattr(broker, "_serial_transport", None)
        writes = getattr(serial, "writes", None)
        if writes is not None:
            lines.append(f"simulated_hid_writes={len(writes)}")
    lines.append(f"serial_ports={json.dumps(list_serial_port_names())}")
    lines.append(f"pnp_cameras={json.dumps(list_pnp_camera_names())}")
    return "\n".join(lines)


def list_serial_port_names() -> list[str]:
    try:
        from superuse.device.discovery import list_serial_ports

        return [
            f"{p.device} vid={p.vid:04x} pid={p.pid:04x}"
            + (f" sn={security.sanitize_text(p.serial_number)}" if p.serial_number else "")
            for p in list_serial_ports()
        ]
    except Exception:
        return []


def list_pnp_camera_names() -> list[str]:
    """Read-only PnP query; never opens camera handles."""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_PnPEntity"
                " | Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' }"
                " | Select-Object -ExpandProperty Name",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [name.strip() for name in result.stdout.splitlines() if name.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def run_diagnose(args) -> int:
    paths = resolve_paths(config_file=args.config)
    config_file = paths.config_file
    pending = paths.portable and not args.config and config_file and not config_file.exists()
    config = _load(None if pending else config_file)
    text = diagnostics_text(config, None)
    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "diagnostics.txt").write_text(text, encoding="utf-8")
        print(f"written {out_dir / 'diagnostics.txt'}")
    else:
        print(text)
    return 0


def build_argparsers(sub) -> None:
    serve = sub.add_parser(
        "serve",
        help="tray + broker; waits for the real device by default (simulated is dev-only)",
    )
    serve.add_argument("--config", default=None)
    serve.add_argument(
        "--real", action="store_true", help="compatibility no-op: real devices are the default"
    )
    serve.add_argument(
        "--simulated",
        action="store_true",
        help="development only: run simulated devices (rejected by release builds)",
    )
    serve.add_argument("--headless", action="store_true", help="no tray; run until Ctrl+C (CI)")
    serve.add_argument("--runtime-dir", default=None)
    serve.set_defaults(which="serve")

    for name, help_text in (("open", "open the UI of the running broker"),):
        opener = sub.add_parser(name, help=help_text)
        opener.add_argument("--runtime-dir", default=None)
        opener.set_defaults(which=name)

    status = sub.add_parser("status", help="sanitized broker status JSON")
    status.add_argument("--runtime-dir", default=None)
    status.set_defaults(which="status")

    diagnose = sub.add_parser("diagnose", help="read-only enumeration and diagnostics")
    diagnose.add_argument("--config", default=None)
    diagnose.add_argument("--output", default=None)
    diagnose.set_defaults(which="diagnose")


def dispatch(args, parser) -> int:
    which = getattr(args, "which", None)
    if which == "serve":
        return ServeApp(args).run()
    if which == "open":
        return run_open(args)
    if which == "status":
        return run_status(args)
    if which == "diagnose":
        return run_diagnose(args)
    parser.print_help()
    return 0
