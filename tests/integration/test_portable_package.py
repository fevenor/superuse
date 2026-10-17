"""ZIP extraction, cwd-independent MCP and moved-directory restart (real mode).

The release EXE runs real-device mode with identities that cannot match any
hardware ("SUPERUSE-TEST-NONE"), so these tests exercise the deterministic
waiting-for-device path without opening whatever real SuperKVM happens to
be attached to the test machine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import zipfile
from pathlib import Path

import aiohttp
import pytest

from superuse import __version__

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "dist" / f"Superuse-{__version__}-windows-x64-portable.zip"
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not ARCHIVE.is_file(), reason="build portable ZIP on Windows first"
)


# Real-mode identities that cannot match any hardware (AGENTS.md: default
# tests open no real devices, and the release EXE has no simulated mode).
NO_HARDWARE_CONFIG = """schema_version = 1

[device]
mode = "real"

[device.serial]
serial_number = "SUPERUSE-TEST-NONE"

[device.video]
device_path = "superuse-test-none"
"""


async def start_portable(app, cwd, profile, *, detached=False):
    env = dict(os.environ, LOCALAPPDATA=str(profile))
    stdio = (
        {"creationflags": 0x00000008}  # DETACHED_PROCESS: windowed, no inherited streams
        if detached
        else {"stdout": asyncio.subprocess.DEVNULL, "stderr": asyncio.subprocess.DEVNULL}
    )
    proc = await asyncio.create_subprocess_exec(
        str(app / "Superuse.exe"),
        "serve",
        "--headless",
        cwd=cwd,
        env=env,
        **stdio,
    )
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            assert proc.returncode is None, "portable process exited before becoming healthy"
            try:
                record = json.loads((app / "data/runtime/broker.json").read_text(encoding="utf-8"))
                if record.get("pid") == proc.pid:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=2)
                    ) as http:
                        async with http.get(
                            f"http://127.0.0.1:{record['port']}/healthz"
                        ) as response:
                            if response.status == 200 and (await response.json()).get("alive"):
                                return proc, record
            except (OSError, ValueError, aiohttp.ClientError, TimeoutError):
                pass
            await asyncio.sleep(0.2)
        raise AssertionError("portable broker startup timed out")
    except BaseException:
        await stop_process(proc)
        raise


async def stop_process(proc):
    if proc.returncode is None:
        proc.terminate()  # no simulated device exists; leave stale runtime to test restart
        try:
            await asyncio.wait_for(proc.wait(), 15)
        except TimeoutError:
            proc.kill()
            await asyncio.wait_for(proc.wait(), 5)


async def mcp_status(app, cwd, profile):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(app / "SuperuseMCP.exe"),
        args=["--client-label", "portable-test"],
        cwd=str(cwd),
        env={"LOCALAPPDATA": str(profile)},
    )
    async with asyncio.timeout(20):
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool("get_status", {})
                assert not result.is_error
                return json.loads(
                    "".join(item.text for item in result.content if hasattr(item, "text"))
                )


async def test_packaged_mcp_examples_connect_without_a_console(tmp_path):
    import socket

    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    with zipfile.ZipFile(ARCHIVE) as archive:
        archive.extractall(tmp_path)
    app = tmp_path / "Superuse"
    guide = (app / "MCP.md").read_text(encoding="utf-8")
    assert "SuperuseMCP.exe" in guide and "Streamable HTTP" in guide
    assert "acquire_control" in guide and "release_control" in guide
    stdio_example = json.loads((app / "mcp-client.example.json").read_text(encoding="utf-8"))
    assert stdio_example["mcpServers"]["superuse"]["command"].endswith("SuperuseMCP.exe")
    http_example = json.loads((app / "mcp-client-http.example.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["superuse-http"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        mcp_port = sock.getsockname()[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        web_port = sock.getsockname()[1]
    assert mcp_port != web_port
    (app / "data/config.toml").write_text(
        NO_HARDWARE_CONFIG + f'\n[network]\nkey="a"\nmcp_port={mcp_port}\nweb_port={web_port}\n',
        encoding="utf-8",
    )
    proc, _ = await start_portable(app, tmp_path, tmp_path / "profile", detached=True)
    try:
        # The internal broker becomes observable before its optional HTTP
        # listener. Wait for that listener without sending any input.
        async with asyncio.timeout(20):
            while True:
                assert proc.returncode is None, "windowed HTTP MCP startup exited early"
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", mcp_port)
                except OSError:
                    await asyncio.sleep(0.1)
                else:
                    writer.close()
                    await writer.wait_closed()
                    break
        url = http_example["url"].replace(":18766/", f":{mcp_port}/")
        headers = {
            name: value.replace("REPLACE_WITH_NETWORK_KEY", "a")
            for name, value in http_example["headers"].items()
        }
        async with asyncio.timeout(20):
            async with httpx2.AsyncClient(headers=headers, timeout=10) as http:
                async with streamable_http_client(url, http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        result = await session.call_tool("get_status", {})
                        assert not result.is_error
                        status = json.loads(
                            "".join(c.text for c in result.content if hasattr(c, "text"))
                        )
                        assert status["state"]["source"] == "real"
                        assert status["state"]["serial_status"] == "disconnected"
        # The same package's stdio entry remains usable with no key setting
        # in the host configuration, from a different working directory.
        assert (await mcp_status(app, tmp_path, tmp_path / "profile"))["state"]["source"] == "real"
    finally:
        await stop_process(proc)


async def test_portable_zip_migration_and_credential_rotation(tmp_path):
    expected_hash = ARCHIVE.with_suffix(".zip.sha256").read_text().split()[0]
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == expected_hash
    origin = tmp_path / "portable A"
    origin.mkdir()
    with zipfile.ZipFile(ARCHIVE) as archive:
        assert "Superuse/data/" in archive.namelist()
        assert not any(
            name.startswith("Superuse/data/") and name != "Superuse/data/"
            for name in archive.namelist()
        )
        archive.extractall(origin)
    app = origin / "Superuse"
    # Pre-seed the config so the very first start never opens real hardware.
    (app / "data/config.toml").write_text(NO_HARDWARE_CONFIG, encoding="utf-8")
    elsewhere = tmp_path / "other cwd"
    elsewhere.mkdir()
    profile = tmp_path / "profile-must-stay-empty"
    proc, first = await start_portable(app, elsewhere, profile)
    try:
        first_status = await mcp_status(app, elsewhere, profile)
        # Release default: real mode, waiting for the device (never simulated).
        assert first_status["state"]["source"] == "real"
        assert first_status["state"]["serial_status"] == "disconnected"
        assert first_status["state"]["video_status"] == "disconnected"
        assert (app / "data/config.toml").is_file()
        assert 'mode = "real"' in (app / "data/config.toml").read_text(encoding="utf-8")
        assert (app / "data/logs/superuse.log").is_file()
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{first['port']}/app.js") as response:
                assert response.status == 200
    finally:
        await stop_process(proc)
    # A legacy simulated config is migrated to real on the next start; other
    # settings and the human latch survive untouched (acceptance A3).
    config = app / "data/config.toml"
    customized = config.read_text(encoding="utf-8") + "\n[video]\npublish_fps = 7\n"
    config.write_text(customized.replace('mode = "real"', 'mode = "simulated"'), encoding="utf-8")
    # A retained human hold must survive a move. No session/lease may survive.
    state = app / "data/state/control.json"
    state.parent.mkdir(exist_ok=True)
    state.write_text('{"human_latched": true}', encoding="utf-8")
    destination = tmp_path / "迁移后 B"
    destination.mkdir()
    moved = destination / "Superuse"
    assert app.resolve().is_relative_to(tmp_path.resolve())
    assert moved.resolve().is_relative_to(tmp_path.resolve())
    app.rename(moved)
    proc, second = await start_portable(moved, elsewhere, profile)
    try:
        assert first["instance_id"] != second["instance_id"]
        assert first["credentials"] != second["credentials"]
        status = await mcp_status(moved, elsewhere, profile)
        assert status["state"]["source"] == "real"
        assert status["state"]["human_latched"] is True
        # No device attached: recovery waits for hardware while the latch
        # keeps control away from agents (never handed back automatically).
        assert status["state"]["mode"] == "recovery"
        assert status["state"]["owner"] is None
        migrated_config = (moved / "data/config.toml").read_text(encoding="utf-8")
        assert 'mode = "real"' in migrated_config
        assert "simulated" not in migrated_config
        assert "publish_fps = 7" in migrated_config
        assert not (profile / "Superuse").exists()
    finally:
        await stop_process(proc)


@pytest.mark.parametrize("failure", ["config", "runtime"])
async def test_packaged_headless_startup_error_exits_without_dialog(tmp_path, failure):
    with zipfile.ZipFile(ARCHIVE) as archive:
        archive.extractall(tmp_path)
    app = tmp_path / "Superuse"
    if failure == "config":
        (app / "data/config.toml").write_text("invalid TOML [", encoding="utf-8")
    else:
        (app / "data/runtime").write_text("not a runtime directory", encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        str(app / "Superuse.exe"),
        "serve",
        "--headless",
        cwd=tmp_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert await asyncio.wait_for(proc.wait(), 15) == 1
        assert not (app / "data/runtime").is_dir()
    finally:
        await stop_process(proc)
