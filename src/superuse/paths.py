"""Shared, stdlib-only storage discovery for the tray, CLI and MCP bridge.

An existing data directory beside the executable selects portable mode.
Never use cwd for implicit discovery or fall back after a portable write error.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PORTABLE_CONFIG = """# Superuse portable configuration. The app waits for the real SuperKVM
# device; simulation exists only in development source runs.
# See config.example.toml for all supported fields.
schema_version = 1

[device]
mode = "real"
"""


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file():
        return checkout
    return Path(sys.executable).resolve().parent


@dataclass(frozen=True)
class AppPaths:
    app_dir: Path
    portable: bool
    data_dir: Path
    runtime_dir: Path
    config_file: Path | None

    def initialize(self) -> None:
        """Called only by the owning broker, never by read-only discovery."""
        if not self.portable:
            return
        # Probe writability even when a custom runtime location was supplied;
        # portable storage must not silently switch to the user profile.
        self.data_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryFile(dir=self.data_dir):
            pass
        default_config = self.data_dir / "config.toml"
        if not default_config.exists():
            # Publish only complete content. Windows rename refuses to replace
            # an existing file, including one added while we wrote the temp.
            # link provides the same no-replace behavior on POSIX dev hosts.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="\n", dir=self.data_dir, delete=False
                ) as output:
                    temporary = Path(output.name)
                    output.write(PORTABLE_CONFIG)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    if os.name == "nt":
                        os.rename(temporary, default_config)
                    else:
                        os.link(temporary, default_config)
                except FileExistsError:
                    pass
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


def resolve_paths(
    *,
    runtime_dir: str | Path | None = None,
    config_file: str | Path | None = None,
    app_dir: Path | None = None,
) -> AppPaths:
    base = (app_dir if app_dir is not None else application_dir()).resolve()
    portable_data = base / "data"
    try:
        portable_data.lstat()
    except FileNotFoundError:
        present = False
    else:
        present = True
    if present and not portable_data.is_dir():
        raise ValueError("The application data path exists but is not a directory")
    portable = present
    if portable:
        data = portable_data
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        data = (Path(local) if local else Path.home() / "AppData" / "Local") / "Superuse"
    else:
        data = Path.home() / ".superuse"
    config = Path(config_file).resolve() if config_file is not None else None
    if config is None and portable:
        config = data / "config.toml"
    return AppPaths(
        app_dir=base,
        portable=portable,
        data_dir=data,
        runtime_dir=Path(runtime_dir).resolve() if runtime_dir is not None else data / "runtime",
        config_file=config,
    )
