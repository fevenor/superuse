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
    runtime_dir = tmp_path / "rt"
    config_file = tmp_path / "simulated.toml"
    config_file.write_text('schema_version=1\n[device]\nmode="simulated"\n', encoding="utf-8")
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


async def test_packaged_mcp_exe_stdio(tmp_path):
    """Packaged SuperuseMCP.exe answers a real MCP handshake."""
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Start a packaged headless broker for the bridge to attach to.
    runtime_dir = tmp_path / "rt"
    config_file = tmp_path / "simulated.toml"
    config_file.write_text('schema_version=1\n[device]\nmode="simulated"\n', encoding="utf-8")
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
                assert payload["state"]["source"] == "simulated"
    finally:
        broker_proc.terminate()
        try:
            await asyncio.wait_for(broker_proc.wait(), timeout=15)
        except TimeoutError:
            broker_proc.kill()
