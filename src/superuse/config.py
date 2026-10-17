"""Strict TOML configuration: defaults < user file < --config file < CLI.

Unknown fields, negative durations, unsupported backends and wrong device
mode spellings fail loudly. Boolean-looking strings are never coerced — TOML
has real booleans (docs/development/windows-operations.md §4).

Real hardware is the default everywhere; simulation is an explicit
development/test choice and cannot load at all inside a frozen release
build (superuse.buildenv).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from superuse.buildenv import simulated_devices_allowed
from superuse.core.errors import ControlError, ErrorCode


class ConfigError(ValueError):
    pass


DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "device": {
        "mode": "real",
        "serial": {
            "vid": 0x1209,
            "pid": 0x0001,
            "serial_number": "",
            "baud": 2_000_000,
            "read_timeout_ms": 100,
            "write_timeout_ms": 250,
        },
        "video": {
            "vid": 0x345F,
            "pid": 0x2131,
            "name": "SK USB3 Video",
            "device_path": "",
            "backend": "dshow",
            "width": 1920,
            "height": 1080,
            "requested_fps": 30,
            "autocrop": False,
        },
    },
    "server": {"host": "127.0.0.1", "port": 0},
    "network": {
        "key": "",  # empty = network access not configured (task 2026-10-17 T3)
        "web_port": 18765,
        "mcp_port": 18766,
        "lan_host": "0.0.0.0",
    },
    "control": {
        "lease_ttl_ms": 15000,
        "renew_interval_ms": 5000,
        "agent_idle_ms": 60000,
        "input_queue_limit": 128,
        "action_timeout_ms": 30000,
        "fence_timeout_ms": 2000,
    },
    "video": {
        "publish_fps": 10,
        "jpeg_quality": 80,
        "snapshot_max_width": 1280,
        "freshness_ms": 1000,
        "stall_timeout_ms": 2000,
    },
    "logging": {
        "level": "INFO",
        "max_bytes": 5_242_880,
        "backup_count": 5,
        "capture_enabled": False,
    },
}


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        return self.raw[name]

    @property
    def simulated(self) -> bool:
        return self.raw["device"]["mode"] == "simulated"

    @property
    def network(self) -> dict[str, Any]:
        return self.raw["network"]

    @property
    def lease_ttl_s(self) -> float:
        return self.raw["control"]["lease_ttl_ms"] / 1000

    @property
    def fence_timeout_s(self) -> float:
        return self.raw["control"]["fence_timeout_ms"] / 1000

    @property
    def action_timeout_s(self) -> float:
        return self.raw["control"]["action_timeout_ms"] / 1000

    @property
    def freshness_s(self) -> float:
        return self.raw["video"]["freshness_ms"] / 1000

    @property
    def stall_timeout_s(self) -> float:
        return self.raw["video"]["stall_timeout_ms"] / 1000


_RANGES: dict[tuple[str, ...], tuple[int | float, int | float]] = {
    ("control", "lease_ttl_ms"): (5000, 30000),
    ("control", "renew_interval_ms"): (1000, 5000),
    ("control", "agent_idle_ms"): (15000, 300000),
    ("control", "input_queue_limit"): (16, 512),
    ("control", "action_timeout_ms"): (1000, 30000),
    ("control", "fence_timeout_ms"): (500, 2000),
    ("video", "publish_fps"): (1, 30),
    ("video", "jpeg_quality"): (40, 95),
    ("video", "snapshot_max_width"): (320, 1920),
    ("video", "freshness_ms"): (100, 1000),
    ("video", "stall_timeout_ms"): (1500, 5000),
    ("logging", "max_bytes"): (1_048_576, 10_485_760),
    ("logging", "backup_count"): (1, 10),
    ("device", "serial", "read_timeout_ms"): (10, 250),
    ("device", "serial", "write_timeout_ms"): (10, 250),
    ("device", "video", "width"): (320, 3840),
    ("device", "video", "height"): (200, 2160),
    ("device", "video", "requested_fps"): (1, 60),
    ("network", "web_port"): (1, 65535),
    ("network", "mcp_port"): (1, 65535),
}

_ENUMS: dict[tuple[str, ...], tuple[Any, ...]] = {
    ("device", "mode"): ("simulated", "real"),
    ("device", "video", "backend"): ("dshow", "msmf"),
    ("logging", "level"): ("DEBUG", "INFO", "WARNING", "ERROR"),
}


def _check_known(path: str, value: Any, template: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected a table")
    unknown = set(value) - set(template)
    if unknown:
        raise ConfigError(f"{path}: unknown field(s) {sorted(unknown)}")


def load_config(
    *, user_file: Path | None = None, overrides: dict[str, Any] | None = None
) -> Config:
    merged = _deep_copy(DEFAULTS)
    for source_name, source in (("user", user_file), ("override", overrides)):
        if source is None:
            continue
        data = (
            tomllib.loads(source.read_text(encoding="utf-8"))
            if isinstance(source, Path)
            else source
        )
        if not isinstance(data, dict):
            raise ConfigError(f"{source_name} config: expected a table")
        merged = _merge(source_name, "", merged, data)
    validate(merged)
    return Config(raw=merged)


def _merge(source: str, path: str, base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    _check_known(path or source, extra, base)
    result = dict(base)
    for key, value in extra.items():
        full = f"{path}.{key}" if path else key
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            result[key] = _merge(source, full, base[key], value)
        elif isinstance(value, dict):
            raise ConfigError(f"{full}: unknown table")
        else:
            result[key] = value
    return result


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    return value


def validate(config: dict[str, Any]) -> None:
    if config.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")
    if config["device"]["mode"] == "simulated" and not simulated_devices_allowed():
        raise ConfigError(
            "device.mode: simulated devices are not available in release builds;"
            " connect a SuperKVM device (the running config was written for an"
            " older development version)"
        )
    for path, (lo, hi) in _RANGES.items():
        node: Any = config
        for part in path:
            node = node[part]
        path[-1]
        if isinstance(node, bool) or not isinstance(node, int) or not (lo <= node <= hi):
            raise ConfigError(f"{'.'.join(path)}: must be an integer in [{lo}, {hi}]")
    for path, allowed in _ENUMS.items():
        node: Any = config
        for part in path:
            node = node[part]
        if node not in allowed:
            raise ConfigError(f"{'.'.join(path)}: must be one of {list(allowed)}")

    serial = config["device"]["serial"]
    if (serial["vid"], serial["pid"]) != (0x1209, 0x0001):
        raise ConfigError(
            "device.serial: only the SuperKVM CDC identity 0x1209:0x0001 is supported"
        )
    if serial["baud"] != 2_000_000:
        raise ConfigError("device.serial.baud: only 2000000 is supported")
    video = config["device"]["video"]
    if (video["vid"], video["pid"]) != (0x345F, 0x2131):
        raise ConfigError(
            "device.video: only the SK USB3 Video identity 0x345f:0x2131 is supported"
        )
    if video["name"] != "SK USB3 Video":
        raise ConfigError("device.video.name: must be 'SK USB3 Video'")
    if video["autocrop"] is not False:
        raise ConfigError("device.video.autocrop: must be false in v1")
    if config["server"]["host"] != "127.0.0.1" or config["server"]["port"] != 0:
        raise ConfigError("server: must bind 127.0.0.1 with an OS-assigned port (0)")
    if config["logging"]["capture_enabled"] is not False:
        raise ConfigError("logging.capture_enabled: must be false in v1")

    network = config["network"]
    if not isinstance(network["key"], str):
        raise ConfigError("network.key: must be a string (no implicit conversion)")
    # The key is compared exactly as written: no trim, case folding or Unicode
    # normalization (task 2026-10-17 §3.2). Empty means "not configured".
    import ipaddress

    try:
        ipaddress.IPv4Address(network["lan_host"])
    except ValueError as exc:
        raise ConfigError(
            "network.lan_host: must be an IPv4 address (e.g. 0.0.0.0 or a LAN address)"
        ) from exc
    if network["web_port"] == network["mcp_port"]:
        raise ConfigError("network: web_port and mcp_port must differ")

    control = config["control"]
    if control["renew_interval_ms"] * 2 > control["lease_ttl_ms"]:
        raise ConfigError("control.renew_interval_ms must be at most half of lease_ttl_ms")
    video_cfg = config["video"]
    if video_cfg["freshness_ms"] >= video_cfg["stall_timeout_ms"]:
        raise ConfigError("video.freshness_ms must be smaller than stall_timeout_ms")


def config_error_to_control(exc: ConfigError) -> ControlError:
    return ControlError(ErrorCode.INVALID_REQUEST, str(exc))


MIGRATION_NOTICE = "已升级为真实设备模式，请连接设备"

_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]")
_TABLE_MODE_RE = re.compile(r"^(\s*)mode\s*=\s*(['\"])(simulated)\2\s*(#.*)?$")
_DOTTED_MODE_RE = re.compile(r"^(\s*)device\s*\.\s*mode\s*=\s*(['\"])(simulated)\2\s*(#.*)?$")
_INLINE_DEVICE_RE = re.compile(r"^\s*device\s*=\s*\{")
_INLINE_MODE_RE = re.compile(r"\bmode\s*=\s*(['\"])(simulated)\1")


def _rewrite_simulated_mode(lines: list[str]) -> bool:
    """Rewrite device.mode simulated->real in place across legal TOML layouts.

    Covers the spellings a previous version could load: plain and
    single-quoted values, comments on the ``[device]`` header or value line,
    dotted ``device.mode`` and the inline table ``device = { mode = ... }``
    (review 2026-10-17 R5). Returns True when exactly one value was rewritten.
    """
    section = ""
    edits = 0
    for index, line in enumerate(lines):
        header = _HEADER_RE.match(line)
        if header is not None:
            section = header.group(1).strip()
            continue
        newline = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
        if section == "device":
            match = _TABLE_MODE_RE.match(line)
            if match is not None:
                indent, comment = match.group(1), (match.group(4) or "").strip()
                lines[index] = _mode_line("mode", indent, comment) + newline
                edits += 1
        elif section == "":
            dotted = _DOTTED_MODE_RE.match(line)
            if dotted is not None:
                indent, comment = dotted.group(1), (dotted.group(4) or "").strip()
                lines[index] = _mode_line("device.mode", indent, comment) + newline
                edits += 1
            elif _INLINE_DEVICE_RE.match(line) and _INLINE_MODE_RE.search(line):
                lines[index] = _INLINE_MODE_RE.sub('mode = "real"', line, count=1)
                edits += 1
    return edits == 1


def _mode_line(key: str, indent: str, comment: str) -> str:
    suffix = f"  {comment}" if comment.startswith("#") else (f"  # {comment}" if comment else "")
    return f'{indent}{key} = "real"{suffix}'


def migrate_legacy_simulated_config(path: Path) -> str | None:
    """Rewrite ``device.mode = "simulated"`` to ``"real"`` in place.

    Release builds call this once at startup so an older portable config does
    not brick the app: everything else in the file (device identity, control,
    logging, the human latch under data/state) stays byte-identical. Returns
    the one-time user notice when a rewrite happened, ``None`` when there was
    nothing to migrate. Raises ``OSError`` when the atomic write fails — the
    caller must report it and keep running in real mode, never assume the
    config was saved.

    Never called for configs explicitly requested as simulated by a
    development process (the caller gates on the frozen boundary).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None  # a genuinely broken file fails loudly in load_config
    if parsed.get("device", {}).get("mode") != "simulated":
        return None

    lines = text.splitlines(keepends=True)
    if _rewrite_simulated_mode(lines):
        rewritten = "".join(lines)
    else:
        # The file legitimately parses as device.mode="simulated" (verified
        # above) but uses a layout the rewriter cannot edit safely; failing
        # loudly beats silently leaving a config the release build would
        # reject (review 2026-10-17 R5).
        raise ConfigError(
            'device.mode is "simulated" in a layout this version cannot '
            'migrate automatically; edit it to mode = "real" and restart'
        )
    # The rewrite must be a pure mode change before it is persisted.
    reparsed = tomllib.loads(rewritten)
    expected = dict(parsed)
    expected.setdefault("device", {})["mode"] = "real"
    if reparsed != expected:
        raise ConfigError(
            "simulated->real migration produced an unexpected config diff; "
            'edit device.mode = "real" manually and restart'
        )

    temporary = path.with_name(path.name + ".migrate.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as output:
        output.write(rewritten)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return MIGRATION_NOTICE
