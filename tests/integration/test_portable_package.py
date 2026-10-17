"""ZIP extraction, cwd-independent MCP and moved-directory restart (simulated)."""

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


async def start_portable(app, cwd, profile):
    env = dict(os.environ, LOCALAPPDATA=str(profile))
    proc = await asyncio.create_subprocess_exec(
        str(app / "Superuse.exe"),
        "serve",
        "--headless",
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
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
        proc.terminate()  # simulated device only; leave stale runtime to test restart
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
    elsewhere = tmp_path / "other cwd"
    elsewhere.mkdir()
    profile = tmp_path / "profile-must-stay-empty"
    proc, first = await start_portable(app, elsewhere, profile)
    try:
        first_status = await mcp_status(app, elsewhere, profile)
        assert first_status["state"]["source"] == "simulated"
        assert (app / "data/config.toml").is_file()
        assert (app / "data/logs/superuse.log").is_file()
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{first['port']}/app.js") as response:
                assert response.status == 200
    finally:
        await stop_process(proc)
    config = app / "data/config.toml"
    customized = config.read_text(encoding="utf-8") + "\n[video]\npublish_fps = 7\n"
    config.write_text(customized, encoding="utf-8")
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
        assert status["state"]["source"] == "simulated"
        assert status["state"]["human_latched"] is True
        assert status["state"]["mode"] == "human_hold"
        assert status["state"]["owner"] is None
        assert (moved / "data/config.toml").read_text(encoding="utf-8") == customized
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
