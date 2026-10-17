"""Strict TOML configuration: defaults < user file < --config file < CLI.

Unknown fields, negative durations, unsupported backends and wrong device
mode spellings fail loudly. Boolean-looking strings are never coerced — TOML
has real booleans (docs/development/windows-operations.md §4).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from superuse.core.errors import ControlError, ErrorCode


class ConfigError(ValueError):
    pass


DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "device": {
        "mode": "simulated",
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

    control = config["control"]
    if control["renew_interval_ms"] * 2 > control["lease_ttl_ms"]:
        raise ConfigError("control.renew_interval_ms must be at most half of lease_ttl_ms")
    video_cfg = config["video"]
    if video_cfg["freshness_ms"] >= video_cfg["stall_timeout_ms"]:
        raise ConfigError("video.freshness_ms must be smaller than stall_timeout_ms")


def config_error_to_control(exc: ConfigError) -> ControlError:
    return ControlError(ErrorCode.INVALID_REQUEST, str(exc))
