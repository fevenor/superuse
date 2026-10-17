"""Portable discovery must not depend on cwd or silently use profile storage."""

import argparse
import importlib.util
import os
import runpy
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from superuse import paths


def test_executable_data_overrides_profile_and_frozen_resource_dir(tmp_path, monkeypatch):
    base = tmp_path / "portable app"
    (base / "data").mkdir(parents=True)
    elsewhere = tmp_path / "different cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(base / "SuperuseMCP.exe"))
    monkeypatch.setattr(paths.sys, "_MEIPASS", str(tmp_path / "resources"), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    resolved = paths.resolve_paths()
    assert resolved.portable
    assert resolved.runtime_dir == base / "data/runtime"
    assert resolved.config_file == base / "data/config.toml"
    assert not (tmp_path / "profile").exists()
    assert not list((base / "data").iterdir())  # discovery is read-only


def test_cwd_data_does_not_enable_portable_and_normal_config_stays_explicit(tmp_path, monkeypatch):
    base = tmp_path / "app"
    base.mkdir()
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    resolved = paths.resolve_paths(app_dir=base)
    assert not resolved.portable
    assert resolved.runtime_dir == tmp_path / "profile/Superuse/runtime"
    assert resolved.config_file is None


def test_initialize_keeps_existing_config_and_overrides_are_independent(tmp_path):
    (tmp_path / "data").mkdir()
    resolved = paths.resolve_paths(app_dir=tmp_path)
    resolved.initialize()
    config = tmp_path / "data/config.toml"
    assert 'mode = "simulated"' in config.read_text()
    custom = "schema_version = 1\n[video]\npublish_fps = 7\n"
    config.write_text(custom)
    resolved.initialize()
    assert config.read_text() == custom
    explicit = paths.resolve_paths(
        app_dir=tmp_path,
        config_file=tmp_path / "selected.toml",
        runtime_dir=tmp_path / "selected-rt",
    )
    assert explicit.config_file == tmp_path / "selected.toml"
    assert explicit.runtime_dir == tmp_path / "selected-rt"
    assert explicit.portable


def test_unwritable_portable_never_falls_back(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))

    def denied(*args, **kwargs):
        raise PermissionError("test read-only directory")

    monkeypatch.setattr(tempfile, "TemporaryFile", denied)
    with pytest.raises(PermissionError):
        paths.resolve_paths(app_dir=tmp_path).initialize()
    assert not (tmp_path / "profile").exists()
    assert not (tmp_path / "data/config.toml").exists()


def test_data_file_is_an_explicit_error(tmp_path):
    (tmp_path / "data").write_text("not a directory")
    with pytest.raises(ValueError, match="not a directory"):
        paths.resolve_paths(app_dir=tmp_path)


def test_broken_data_link_does_not_fall_back_to_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    try:
        (tmp_path / "data").symlink_to(tmp_path / "missing", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires Windows developer mode or privilege")
    with pytest.raises(ValueError, match="not a directory"):
        paths.resolve_paths(app_dir=tmp_path)
    assert not (tmp_path / "profile").exists()


@pytest.mark.parametrize("failure", ["write", "publish", "existing"])
def test_config_initialization_never_leaves_partial_or_replaces_existing(
    tmp_path, monkeypatch, failure
):
    (tmp_path / "data").mkdir()
    config = tmp_path / "data/config.toml"
    resolved = paths.resolve_paths(app_dir=tmp_path)

    def fail_write(*args):
        assert not config.exists()
        raise OSError("interrupted config write")

    def publish(source, destination):
        assert Path(source).read_text() == paths.PORTABLE_CONFIG
        if failure == "existing":
            config.write_text("user configuration")
            raise FileExistsError
        raise PermissionError("cannot publish config")

    if failure == "write":
        monkeypatch.setattr(paths.os, "fsync", fail_write)
    else:
        monkeypatch.setattr(paths.os, "rename" if os.name == "nt" else "link", publish)
    if failure == "existing":
        resolved.initialize()
        assert config.read_text() == "user configuration"
    else:
        with pytest.raises(OSError):
            resolved.initialize()
        assert not config.exists()
    assert {p.name for p in resolved.data_dir.iterdir()} == (
        {"config.toml"} if failure == "existing" else set()
    )


@pytest.mark.parametrize(
    ("arguments", "dialogs"),
    [([], 1), (["serve", "--headless"], 0), (["diagnose"], 0)],
)
def test_windowed_errors_only_show_a_dialog_for_interactive_serve(monkeypatch, arguments, dialogs):
    import ctypes

    from superuse import __main__

    def fail(argv):
        raise SystemExit("test startup failure")

    messages = []
    monkeypatch.setattr(__main__, "main", fail)
    monkeypatch.setattr(paths.sys, "argv", ["Superuse.exe", *arguments])
    monkeypatch.setattr(paths.sys, "stderr", None)
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(user32=SimpleNamespace(MessageBoxW=lambda *args: messages.append(args))),
        raising=False,
    )
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(Path(__file__).resolve().parents[2] / "packaging/entry_tray.py"))
    assert raised.value.code == 1
    assert len(messages) == dialogs


def test_serve_reads_portable_config_from_base_not_cwd(tmp_path, monkeypatch):
    from superuse.serve import ServeApp

    base = tmp_path / "app"
    (base / "data").mkdir(parents=True)
    (base / "data/config.toml").write_text("schema_version=1\n[video]\npublish_fps=7\n")
    monkeypatch.setattr(paths, "application_dir", lambda: base)
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(config=None, runtime_dir=None, real=False, headless=True)
    app = ServeApp(args)
    assert app.config.section("video")["publish_fps"] == 7
    assert app.runtime_dir == base / "data/runtime"


def test_failed_broker_start_is_reported_and_partial_resources_are_closed(tmp_path, monkeypatch):
    from superuse import serve

    class FailedBroker:
        stopped = False

        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            raise PermissionError("runtime cannot be written")

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(serve, "Broker", FailedBroker)
    app = serve.ServeApp(
        argparse.Namespace(config=None, runtime_dir=str(tmp_path), real=False, headless=True)
    )
    try:
        with pytest.raises(SystemExit, match="broker failed to start"):
            app._start_broker_thread()
    finally:
        app._shutdown()
    assert app.broker.stopped
    assert not app._thread.is_alive()


def build_module():
    spec = importlib.util.spec_from_file_location(
        "build_portable", Path(__file__).resolve().parents[2] / "scripts/build_portable.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_preserves_empty_data_directory_without_user_state(tmp_path):
    build = build_module()
    bundle = tmp_path / "bundle"
    (bundle / "_internal").mkdir(parents=True)
    for name in ("Superuse.exe", "SuperuseMCP.exe"):
        (bundle / name).write_bytes(b"test executable")
    stage = tmp_path / "Superuse"
    build.prepare_release(bundle, stage)
    output = tmp_path / "release.zip"
    build.write_zip(stage, output)
    with zipfile.ZipFile(output) as archive:
        assert "Superuse/data/" in archive.namelist()
        assert not any(
            name.startswith("Superuse/data/") and name != "Superuse/data/"
            for name in archive.namelist()
        )
    assert output.with_suffix(".zip.sha256").is_file()
    (stage / "data/config.toml").write_text("private")
    with pytest.raises(ValueError, match="must be empty"):
        build.write_zip(stage, output)


def test_build_refuses_to_repackage_a_used_installation(tmp_path):
    build = build_module()
    bundle = tmp_path / "bundle"
    (bundle / "data").mkdir(parents=True)
    with pytest.raises(ValueError, match="fresh bundle"):
        build.prepare_release(bundle, tmp_path / "Superuse")


def test_build_requires_executable_files_and_rejects_junction_before_descent(tmp_path, monkeypatch):
    build = build_module()
    bundle = tmp_path / "bundle"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "Superuse.exe").mkdir()
    (bundle / "SuperuseMCP.exe").write_bytes(b"test")
    with pytest.raises(ValueError, match="executables"):
        build.prepare_release(bundle, tmp_path / "Superuse")
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda p: p == bundle or original(p))
    monkeypatch.setattr(Path, "iterdir", lambda p: pytest.fail("descended into a junction"))
    with pytest.raises(ValueError, match="contains a link"):
        build.prepare_release(bundle, tmp_path / "Superuse")
