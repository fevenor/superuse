"""Release boundary: real-device default, frozen simulation guard, migration.

Task 2026-10-17 T1. Real hardware is the default everywhere; simulated mode
loads only in development processes; release builds rewrite legacy
simulated configs to real without touching anything else.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from superuse import buildenv
from superuse import config as config_module
from superuse.config import (
    MIGRATION_NOTICE,
    ConfigError,
    load_config,
    migrate_legacy_simulated_config,
)


def test_default_mode_is_real():
    config = load_config()
    assert not config.simulated
    assert config.raw["device"]["mode"] == "real"


def test_explicit_simulated_still_loads_in_development():
    config = load_config(overrides={"device": {"mode": "simulated"}})
    assert config.simulated


def test_frozen_build_rejects_simulated_config(monkeypatch):
    monkeypatch.setattr(config_module, "simulated_devices_allowed", lambda: False)
    with pytest.raises(ConfigError, match="not available in release builds"):
        load_config(overrides={"device": {"mode": "simulated"}})
    # Real mode keeps loading inside a frozen build.
    assert load_config().raw["device"]["mode"] == "real"


def test_broker_refuses_simulated_hardware_in_frozen_build(tmp_path, monkeypatch):
    monkeypatch.setattr(buildenv, "simulated_devices_allowed", lambda: False)
    from superuse.app import Broker

    with pytest.raises(RuntimeError, match="not available in release builds"):
        Broker(
            load_config(overrides={"device": {"mode": "simulated"}}),
            runtime_dir=tmp_path / "runtime",
        )


def test_serve_simulated_flag_is_rejected_in_frozen_build(monkeypatch):
    monkeypatch.setattr(buildenv, "simulated_devices_allowed", lambda: False)
    from superuse.serve import _load

    with pytest.raises(SystemExit, match="not available in release builds"):
        _load(None, want_simulated=True)


# ----------------------------------------------------------------------
# Legacy simulated -> real migration
# ----------------------------------------------------------------------

LEGACY_CONFIG = """# user comment stays
schema_version = 1

[device]
mode = "simulated"  # chosen long ago

[device.serial]
serial_number = "ABC123"

[video]
publish_fps = 7
"""


def test_migration_rewrites_only_device_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(LEGACY_CONFIG, encoding="utf-8")
    assert migrate_legacy_simulated_config(path) == MIGRATION_NOTICE
    rewritten = path.read_text(encoding="utf-8")
    assert 'mode = "real"' in rewritten
    assert "simulated" not in rewritten
    assert "# user comment stays" in rewritten
    assert "# chosen long ago" in rewritten
    assert 'serial_number = "ABC123"' in rewritten
    assert "publish_fps = 7" in rewritten
    assert load_config(user_file=path).raw["device"]["mode"] == "real"


def test_migration_is_idempotent_and_leaves_real_files_alone(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(LEGACY_CONFIG, encoding="utf-8")
    assert migrate_legacy_simulated_config(path) is not None
    before = path.read_text(encoding="utf-8")
    assert migrate_legacy_simulated_config(path) is None
    assert path.read_text(encoding="utf-8") == before


def test_migration_skips_missing_and_broken_files(tmp_path):
    assert migrate_legacy_simulated_config(tmp_path / "absent.toml") is None
    broken = tmp_path / "broken.toml"
    broken.write_text("invalid TOML [", encoding="utf-8")
    assert migrate_legacy_simulated_config(broken) is None
    assert broken.read_text(encoding="utf-8") == "invalid TOML ["


def test_migration_does_not_touch_nested_device_tables(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[device]\nmode = "real"\n[device.video]\nname = "SK USB3 Video"\n',
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")
    assert migrate_legacy_simulated_config(path) is None
    assert path.read_text(encoding="utf-8") == before


# ----------------------------------------------------------------------
# Review 2026-10-17 R5: every legal old spelling must migrate
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "contents",
    [
        "[device]\nmode = 'simulated'\n",  # single quotes
        '[device] # my device\nmode = "simulated"\n',  # header comment
        '[device]\nmode\t=\t"simulated"\t# tabbed\n',  # tabs + value comment
        'device.mode = "simulated"\n',  # dotted key
        "device.mode='simulated'\n",
        'device = { mode = "simulated" }\n',  # inline table
        "device = { mode = 'simulated', serial = { serial_number = 'ABC' } }\n",
    ],
)
def test_migration_supports_valid_toml_variants(tmp_path, contents, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(contents, encoding="utf-8")
    assert migrate_legacy_simulated_config(path) == MIGRATION_NOTICE
    rewritten = path.read_text(encoding="utf-8")
    assert "simulated" not in rewritten
    assert load_config(user_file=path).raw["device"]["mode"] == "real"
    # The frozen boundary accepts the migrated file.
    monkeypatch.setattr(config_module, "simulated_devices_allowed", lambda: False)
    assert load_config(user_file=path).raw["device"]["mode"] == "real"


def test_unrewritable_simulated_layout_fails_loudly(tmp_path):
    """A layout parsing as simulated but not editable by the rewriter must
    raise, not silently stay un-migrated (review R5)."""
    # `mode` spelled through a redundant-but-legal construct the rewriter
    # does not match line-wise: continuation via array-free multiline string
    # value spanning lines.
    path = tmp_path / "config.toml"
    path.write_text('[device]\nmode = """simulated"""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot"):
        migrate_legacy_simulated_config(path)
    assert "simulated" in path.read_text(encoding="utf-8")  # untouched on failure


def test_migration_write_failure_raises_and_leaves_file_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(LEGACY_CONFIG, encoding="utf-8")

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(config_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        migrate_legacy_simulated_config(path)
    assert 'mode = "simulated"' in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_development_serve_never_migrates(tmp_path, monkeypatch):
    """A dev checkout running --simulated must not rewrite its own config."""
    from superuse import paths as paths_module
    from superuse import serve

    base = tmp_path / "app"
    (base / "data").mkdir(parents=True)
    (base / "data/config.toml").write_text(LEGACY_CONFIG, encoding="utf-8")
    monkeypatch.setattr(paths_module, "application_dir", lambda: base)
    app = serve.ServeApp(
        argparse.Namespace(config=None, runtime_dir=None, real=False, simulated=True, headless=True)
    )
    app._migrate_legacy_config()
    assert 'mode = "simulated"' in (base / "data/config.toml").read_text(encoding="utf-8")


def test_frozen_serve_migrates_once_and_runs_real(tmp_path, monkeypatch):
    from superuse import paths as paths_module
    from superuse import serve

    monkeypatch.setattr(serve, "is_frozen_build", lambda: True)
    base = tmp_path / "app"
    (base / "data").mkdir(parents=True)
    (base / "data/config.toml").write_text(LEGACY_CONFIG, encoding="utf-8")
    monkeypatch.setattr(paths_module, "application_dir", lambda: base)
    app = serve.ServeApp(
        argparse.Namespace(
            config=None, runtime_dir=None, real=False, simulated=False, headless=True
        )
    )
    app.paths.initialize()
    app._migrate_legacy_config()
    assert app._migration_notice == MIGRATION_NOTICE
    # The rewritten config now loads without any override.
    loaded = serve._load(app.paths.config_file)
    assert loaded.raw["device"]["mode"] == "real"
    assert 'mode = "real"' in (base / "data/config.toml").read_text(encoding="utf-8")


def test_frozen_serve_migration_failure_still_runs_real(tmp_path, monkeypatch):
    from superuse import paths as paths_module
    from superuse import serve

    monkeypatch.setattr(serve, "is_frozen_build", lambda: True)

    def failing_migrate(path: Path):
        raise OSError("locked")

    monkeypatch.setattr(serve, "migrate_legacy_simulated_config", failing_migrate)
    base = tmp_path / "app"
    (base / "data").mkdir(parents=True)
    (base / "data/config.toml").write_text(LEGACY_CONFIG, encoding="utf-8")
    monkeypatch.setattr(paths_module, "application_dir", lambda: base)
    app = serve.ServeApp(
        argparse.Namespace(
            config=None, runtime_dir=None, real=False, simulated=False, headless=True
        )
    )
    app.paths.initialize()
    app._migrate_legacy_config()
    assert app._migration_failed
    # The unwritten file still says simulated; loading must be forced real.
    loaded = serve._load(app.paths.config_file, force_real=True)
    assert loaded.raw["device"]["mode"] == "real"
    assert 'mode = "simulated"' in (base / "data/config.toml").read_text(encoding="utf-8")
