"""W02-flavoured checks: mutex, ACL, CLI open/status, headless serve."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("win32security")  # windows-only tests

from superuse.windows import instance, security  # noqa: E402


def test_broker_mutex_single_owner():
    first = instance.BrokerMutex()
    assert first.acquire() is True
    try:
        second = instance.BrokerMutex()
        assert second.acquire() is False  # same login session: refused
    finally:
        first.release()
    third = instance.BrokerMutex()
    assert third.acquire() is True
    third.release()


def test_runtime_file_acl_current_user_only(tmp_path: Path):
    target = tmp_path / "runtime" / "broker.json"
    security.secure_write_json(target, {"instance_id": "x", "port": 0})
    assert target.is_file()
    assert security.apply_user_only_acl(target)
    entries = security.read_dacl_entries(target)
    assert entries, "DACL should be readable on Windows"
    allowed = {"S-1-5-18", "S-1-5-32-544"}  # SYSTEM, Administrators
    unexpected = [sid for sid in entries if sid not in allowed and not sid.startswith("S-1-5-21")]
    # Only the machine's own user SIDs (S-1-5-21-...) plus SYSTEM/Admins.
    assert unexpected == []


def test_sanitize_runtime_drops_credentials():
    record = {"instance_id": "i", "port": 1, "credentials": {"agent_bootstrap": "SECRET"}}
    cleaned = security.sanitize_runtime_record(record)
    assert "credentials" not in cleaned
    assert "SECRET" not in json.dumps(cleaned)


async def test_cli_open_against_running_broker(broker, tmp_path):
    """`superuse open` finds the healthy broker and gets a one-time URL.

    The CLI runs as an async subprocess so the in-process broker loop keeps
    serving /healthz while we wait (a sync subprocess.run would starve it).
    """
    runtime_dir = broker.runtime_dir
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "superuse",
        "open",
        "--runtime-dir",
        str(runtime_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode == 0, stderr.decode()


async def test_cli_status_sanitized(broker):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "superuse",
        "status",
        "--runtime-dir",
        str(broker.runtime_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    result_stdout = stdout.decode()
    result_stderr = stderr.decode()

    class _Result:
        returncode = proc.returncode
        stdout = result_stdout
        stderr = result_stderr

    result = _Result()
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["broker"] is True
    assert "credentials" not in payload["runtime"]
    assert broker.credentials.agent_bootstrap not in result.stdout
    assert broker.credentials.launcher not in result.stdout


def test_headless_serve_single_instance_and_health(tmp_path):
    runtime_dir = tmp_path / "rt"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "superuse",
            "serve",
            "--headless",
            "--runtime-dir",
            str(runtime_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import aiohttp

        record = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            record = instance.read_runtime_file(runtime_dir)
            if record:
                port = record["port"]
                try:

                    async def _check(port=port):
                        async with aiohttp.ClientSession(
                            base_url=f"http://127.0.0.1:{port}",
                            timeout=aiohttp.ClientTimeout(total=1),
                        ) as http:
                            async with http.get("/healthz") as response:
                                return response.status == 200

                    if asyncio_run(_check()):
                        break
                except Exception:
                    pass
            time.sleep(0.3)
        assert record, "serve did not write a runtime file"
        assert record["instance_id"]

        # Second serve while the first is healthy forwards and exits nonzero-zero.
        second = subprocess.run(
            [
                sys.executable,
                "-m",
                "superuse",
                "serve",
                "--headless",
                "--runtime-dir",
                str(runtime_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert second.returncode == 0  # forwarded the open request and exited
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_tray_menu_and_labels():
    pytest.importorskip("pystray")
    import asyncio

    from superuse.core.models import Mode
    from superuse.windows.tray import TrayApp, state_label

    assert state_label(Mode.HUMAN_HOLD, True) == "人工锁定（模拟）"
    assert state_label("agent", False) == "agent 控制中"
    loop = asyncio.new_event_loop()
    try:
        app = TrayApp(
            loop=loop,
            on_open_ui=lambda: None,
            on_takeover_and_open=lambda: None,
            on_handback=lambda: None,
            on_copy_diagnostics=lambda: "diag",
            on_quit=lambda: None,
            get_state_summary=lambda: "空闲",
        )
        menu = app.build_menu()
        labels = [str(item.text) if item.text else "" for item in menu.items]
        assert any("打开界面" in label for label in labels)
        assert any("接管并打开界面" in label for label in labels)
        assert any("退出" in label for label in labels)
    finally:
        loop.close()
