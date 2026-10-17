"""W03/W04-adjacent smoke checks for the built onedir package.

Skipped unless the package exists (build first, then run pytest):
    uv run --locked --extra app --extra packaging pyinstaller packaging/superuse.spec --noconfirm
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

# Real-mode identities that cannot match any hardware: the packaged EXE
# exercises its deterministic waiting-for-device path without opening real
# devices attached to the test machine (AGENTS.md: default tests open none).
NO_HARDWARE_CONFIG = """schema_version = 1

[device]
mode = "real"

[device.serial]
serial_number = "SUPERUSE-TEST-NONE"

[device.video]
device_path = "superuse-test-none"
"""


PACKAGE_DIR = Path(
    os.environ.get(
        "SUPERUSE_PACKAGE_DIR", Path(__file__).resolve().parents[2] / "dist" / "Superuse"
    )
)
TRAY_EXE = PACKAGE_DIR / "Superuse.exe"
MCP_EXE = PACKAGE_DIR / "SuperuseMCP.exe"

pytestmark = pytest.mark.skipif(
    not (TRAY_EXE.is_file() and MCP_EXE.is_file()), reason="onedir package not built"
)


async def test_packaged_tray_exe_version():
    assert (PACKAGE_DIR / "_internal" / "LICENSE").is_file()
    assert (
        PACKAGE_DIR / "_internal" / "third_party" / "licenses" / "serial-hid-kvm-MIT.txt"
    ).is_file()
    proc = await asyncio.create_subprocess_exec(
        str(TRAY_EXE),
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert b"superuse" in stdout
    assert proc.returncode == 0


async def test_packaged_serve_headless_and_web_assets(tmp_path):
    """Real-mode default: no matching hardware, services still come up.

    The packaged EXE must not fall back to simulated devices (task
    2026-10-17 T1/T2); it waits for the SuperKVM device while serving the
    tray-less status surface.
    """
    runtime_dir = tmp_path / "rt"
    config_file = tmp_path / "no-hardware.toml"
    config_file.write_text(NO_HARDWARE_CONFIG, encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        str(TRAY_EXE),
        "serve",
        "--headless",
        "--config",
        str(config_file),
        "--runtime-dir",
        str(runtime_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        import aiohttp

        from superuse.windows import instance

        record = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            record = instance.read_runtime_file(runtime_dir)
            if record:
                try:
                    async with aiohttp.ClientSession(
                        base_url=f"http://127.0.0.1:{record['port']}",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as http:
                        async with http.get("/healthz") as response:
                            if response.status == 200:
                                break
                except Exception:
                    pass
            await asyncio.sleep(0.4)
        assert record, "packaged serve did not become healthy"

        # The human page is served from the packaged bundle (no network).
        async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{record['port']}") as http:
            async with http.get("/") as response:
                assert response.status == 200
                html = await response.text()
            assert "app.js" in html
            async with http.get("/app.js") as response:
                assert response.status == 200
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except TimeoutError:
            proc.kill()


async def test_packaged_real_mode_waits_for_device_and_rejects_simulated(tmp_path):
    """Status reports the real source waiting for hardware; a simulated
    config cannot load in the release build (no back door for old tests)."""
    import aiohttp

    from superuse.windows import instance

    runtime_dir = tmp_path / "rt2"
    config_file = tmp_path / "no-hardware2.toml"
    config_file.write_text(NO_HARDWARE_CONFIG, encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        str(TRAY_EXE),
        "serve",
        "--headless",
        "--config",
        str(config_file),
        "--runtime-dir",
        str(runtime_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        record = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            record = instance.read_runtime_file(runtime_dir)
            if record:
                try:
                    async with aiohttp.ClientSession(
                        base_url=f"http://127.0.0.1:{record['port']}",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as http:
                        async with http.get("/healthz") as response:
                            if response.status == 200:
                                break
                except Exception:
                    pass
            await asyncio.sleep(0.4)
        assert record
        # stdio get_status shows real mode waiting for the device.
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=str(MCP_EXE),
            args=["--client-label", "real-mode-check", "--runtime-dir", str(runtime_dir)],
        )
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool("get_status", {})
                assert not result.is_error
                payload = json.loads("".join(c.text for c in result.content if hasattr(c, "text")))
                assert payload["state"]["source"] == "real"
                assert payload["state"]["serial_status"] == "disconnected"
                assert payload["state"]["video_status"] == "disconnected"
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except TimeoutError:
            proc.kill()

    # A legacy simulated config is MIGRATED to real (acceptance A3): the
    # release build runs and never starts a simulated device.
    simulated_config = tmp_path / "simulated.toml"
    simulated_config.write_text('schema_version=1\n[device]\nmode="simulated"\n', encoding="utf-8")
    migrated = await asyncio.create_subprocess_exec(
        str(TRAY_EXE),
        "serve",
        "--headless",
        "--config",
        str(simulated_config),
        "--runtime-dir",
        str(tmp_path / "rt3"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # The migrated broker serves status; give it a moment, then check
        # the config rewrite happened before shutting it down.
        await asyncio.sleep(4)
        assert migrated.returncode is None, "migrated serve must keep running"
        migrated_text = simulated_config.read_text(encoding="utf-8")
        assert 'mode = "real"' in migrated_text
        assert "simulated" not in migrated_text
    finally:
        migrated.terminate()
        try:
            await asyncio.wait_for(migrated.wait(), timeout=15)
        except TimeoutError:
            migrated.kill()
            await asyncio.wait_for(migrated.wait(), timeout=5)

    # --simulated is unavailable in the packaged EXE: the flag itself is
    # rejected loudly before any device is touched.
    flagged = await asyncio.create_subprocess_exec(
        str(TRAY_EXE),
        "serve",
        "--headless",
        "--simulated",
        "--runtime-dir",
        str(tmp_path / "rt4"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(flagged.communicate(), timeout=30)
    assert flagged.returncode not in (0, None)
    assert b"simulated" in stdout + stderr


async def test_packaged_mcp_exe_stdio(tmp_path):
    """Packaged SuperuseMCP.exe answers a real MCP handshake (deviceless)."""
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Start a packaged headless broker for the bridge to attach to. Real mode
    # without hardware: status answers with structured "waiting" state.
    runtime_dir = tmp_path / "rt"
    config_file = tmp_path / "no-hardware3.toml"
    config_file.write_text(NO_HARDWARE_CONFIG, encoding="utf-8")
    broker_proc = await asyncio.create_subprocess_exec(
        str(TRAY_EXE),
        "serve",
        "--headless",
        "--config",
        str(config_file),
        "--runtime-dir",
        str(runtime_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        from superuse.windows import instance

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if instance.read_runtime_file(runtime_dir):
                try:
                    import aiohttp

                    record = instance.read_runtime_file(runtime_dir)
                    async with aiohttp.ClientSession(
                        base_url=f"http://127.0.0.1:{record['port']}",
                        timeout=aiohttp.ClientTimeout(total=1),
                    ) as http:
                        async with http.get("/healthz") as response:
                            if response.status == 200:
                                break
                except Exception:
                    pass
            await asyncio.sleep(0.4)

        params = StdioServerParameters(
            command=str(MCP_EXE),
            args=["--client-label", "packaged-test", "--runtime-dir", str(runtime_dir)],
        )
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {
                    "mouse_move_relative",
                    "mouse_click_relative",
                    "mouse_drag_relative",
                    "mouse_scroll_relative",
                } <= {tool.name for tool in tools.tools}
                result = await session.call_tool("get_status", {})
                assert not result.is_error
                payload = json.loads("".join(c.text for c in result.content if hasattr(c, "text")))
                assert payload["state"]["source"] == "real"
                assert payload["state"]["serial_status"] == "disconnected"
    finally:
        broker_proc.terminate()
        try:
            await asyncio.wait_for(broker_proc.wait(), timeout=15)
        except TimeoutError:
            broker_proc.kill()


async def test_packaged_windowed_startup_with_key_and_no_standard_streams(tmp_path):
    """R1 regression: a DETACHED windowed EXE (no inherited std streams) with
    a configured network key must still reach healthy; uvicorn's default
    logging used to crash on stdout=None here and take the broker down.

    Health is observed via the runtime file + loopback HTTP, never via pipes
    (pipes would mask the windowed environment the bug needs).
    """
    import socket as socket_module
    from contextlib import closing

    import aiohttp

    from superuse.windows import instance

    def free_port():
        with closing(socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    runtime_dir = tmp_path / "rt-keyed"
    config_file = tmp_path / "keyed-nohw.toml"
    config_file.write_text(
        NO_HARDWARE_CONFIG
        + f'\n[network]\nkey = "a"\nweb_port = {free_port()}\nmcp_port = {free_port()}\n',
        encoding="utf-8",
    )
    DETACHED_PROCESS = 0x00000008
    proc = await asyncio.create_subprocess_exec(
        str(TRAY_EXE),
        "serve",
        "--headless",
        "--config",
        str(config_file),
        "--runtime-dir",
        str(runtime_dir),
        creationflags=DETACHED_PROCESS,  # no console, no inherited streams
    )
    try:
        record = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert proc.returncode is None, "windowed keyed startup exited early"
            record = instance.read_runtime_file(runtime_dir)
            if record:
                try:
                    async with aiohttp.ClientSession(
                        base_url=f"http://127.0.0.1:{record['port']}",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as http:
                        async with http.get("/healthz") as response:
                            if response.status == 200:
                                break
                except Exception:
                    pass
            await asyncio.sleep(0.4)
        assert record, "detached keyed serve did not become healthy"
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except TimeoutError:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
