"""I03–I06/I08: the MCP stdio bridge against the simulated broker.

Each bridge runs as its own subprocess (``python -m superuse mcp``) driven by
the official SDK client, exactly as an agent host would.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent, TextContent

pytest.importorskip("mcp")

from tests.integration.conftest import browser_page  # noqa: E402


def bridge_params(broker, label: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "superuse",
            "mcp",
            "--client-label",
            label,
            "--runtime-dir",
            str(broker.runtime_dir),
        ],
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )


async def open_bridge(broker, label: str):
    return stdio_client(bridge_params(broker, label))


async def call_tool(session: ClientSession, name: str, arguments: dict | None = None):
    result = await session.call_tool(name, arguments or {})
    return result


def text_of(result) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


async def test_i03_all_tools_over_official_client(broker):
    async with await open_bridge(broker, "agent-a") as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == {
                "get_status",
                "acquire_control",
                "release_control",
                "capture_screen",
                "press_key",
                "type_text",
                "mouse_move",
                "mouse_click",
                "mouse_drag",
                "mouse_scroll",
                "mouse_move_relative",
                "mouse_click_relative",
                "mouse_drag_relative",
                "mouse_scroll_relative",
                "release_inputs",
                "cancel_action",
            }

            status = await call_tool(session, "get_status")
            assert not status.is_error
            payload = json.loads(text_of(status))
            assert payload["state"]["source"] == "simulated"
            assert "acquire_control" in payload["capabilities"]

            snapshot = await call_tool(session, "capture_screen")
            assert not snapshot.is_error
            image = next(c for c in snapshot.content if isinstance(c, ImageContent))
            assert image.mime_type == "image/jpeg"
            meta = json.loads(text_of(snapshot))
            basis_id = meta["basis_id"]
            assert meta["source"] == "simulated"

            # Input without a lease fails with a machine-readable code.
            denied = await call_tool(session, "press_key", {"code": "KeyA"})
            assert denied.is_error
            assert "CONTROL_REQUIRED" in text_of(denied)

            grant = await call_tool(session, "acquire_control", {"ttl_ms": 15000})
            assert not grant.is_error

            press = await call_tool(session, "press_key", {"code": "KeyA", "hold_ms": 20})
            assert not press.is_error, text_of(press)
            assert json.loads(text_of(press))["completion"] == "written"

            typed = await call_tool(session, "type_text", {"text": "hi\n"})
            assert not typed.is_error

            moved = await call_tool(session, "mouse_move", {"basis_id": basis_id, "x": 5, "y": 5})
            assert not moved.is_error, text_of(moved)

            scrolled = await call_tool(
                session, "mouse_scroll", {"basis_id": basis_id, "x": 5, "y": 5, "steps": -3}
            )
            assert not scrolled.is_error, text_of(scrolled)

            for name, args in (
                ("mouse_move_relative", {"dx": 257, "dy": -128}),
                ("mouse_click_relative", {"button": "right", "count": 2}),
                ("mouse_drag_relative", {"dx": -40, "dy": 20, "duration_ms": 50}),
                ("mouse_scroll_relative", {"steps": -1}),
            ):
                relative = await call_tool(session, name, args)
                assert not relative.is_error, text_of(relative)
                assert json.loads(text_of(relative))["completion"] == "written"

            released = await call_tool(session, "release_control")
            assert not released.is_error

            resources = await session.list_resources()
            assert any(str(r.uri) == "superuse://state" for r in resources.resources)


async def test_i04_two_bridges_share_one_broker(broker):
    async with (
        await open_bridge(broker, "agent-a") as streams_a,
        await open_bridge(broker, "agent-b") as streams_b,
    ):
        async with (
            ClientSession(streams_a[0], streams_a[1]) as session_a,
            ClientSession(streams_b[0], streams_b[1]) as session_b,
        ):
            await session_a.initialize()
            await session_b.initialize()

            grant = await call_tool(session_a, "acquire_control")
            assert not grant.is_error
            busy = await call_tool(session_b, "acquire_control")
            assert busy.is_error and "CONTROL_BUSY" in text_of(busy)

            status_b = await call_tool(session_b, "get_status")
            payload_b = json.loads(text_of(status_b))
            assert payload_b["state"]["mode"] == "agent"
            assert payload_b["state"]["owner"]["label"] == "agent-a"

            # One serial transport only: the broker's single actor serves both.
            assert broker.actor is not None
            await call_tool(session_a, "release_control")


async def test_i05_cancel_and_bridge_exit(broker):
    async with await open_bridge(broker, "agent-a") as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            await call_tool(session, "acquire_control")

            # Long bounded typing job, cancelled mid-flight.
            typing = asyncio.ensure_future(
                call_tool(session, "type_text", {"text": "x" * 400, "char_interval_ms": 50})
            )
            await asyncio.sleep(1.0)
            running = [
                record.job for record in broker.scheduler._jobs.values() if not record.job.terminal
            ]
            assert running, "expected a running job"
            cancelled_job = await call_tool(session, "cancel_action", {"job_id": running[0].job_id})
            assert not cancelled_job.is_error
            result = await asyncio.wait_for(typing, timeout=10)
            assert result.is_error
            assert "ACTION_CANCELLED" in text_of(result)
            job = json.loads(text_of(cancelled_job)) if not cancelled_job.is_error else {}
            assert job.get("status") in ("cancelled", "written")

    # Bridge exit (stdio closed) releases the lease; control returns to idle.
    for _ in range(100):
        if broker.manager.mode.value == "idle":
            break
        await asyncio.sleep(0.1)
    assert broker.manager.mode.value == "idle"


async def test_i08_revocation_visible_without_notifications(broker):
    import time as _time

    async with await open_bridge(broker, "agent-a") as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            await call_tool(session, "acquire_control")

            # Human takes over over HTTP while the agent is idle: no
            # notification is needed — the next input call fails immediately.
            page = await browser_page(
                f"http://127.0.0.1:{broker.port}",
                broker.sessions.issue_bootstrap(_time.monotonic()),
            )
            import aiohttp

            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"http://127.0.0.1:{broker.port}/v1/control/takeover",
                    json={"request_id": "33333333-3333-3333-3333-333333333333"},
                    headers={
                        "Cookie": f"superuse_browser={page['cookie']}",
                        "X-Superuse-Page": page["page_token"],
                        "X-CSRF-Token": page["csrf"],
                        "Origin": f"http://127.0.0.1:{broker.port}",
                    },
                ) as response:
                    assert response.status == 200

            denied = await call_tool(session, "press_key", {"code": "KeyA"})
            assert denied.is_error
            assert "CONTROL_REVOKED" in text_of(denied)


async def test_i06_broker_restart_invalidates_lease(broker, tmp_path):
    async with await open_bridge(broker, "agent-a") as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            grant = await call_tool(session, "acquire_control")
            assert not grant.is_error
            old_instance = json.loads(text_of(grant))["lease"]["instance_id"]

            # A broker restart writes a new instance to the same runtime file.
            from superuse.app import Broker
            from superuse.config import load_config

            replacement = Broker(
                load_config(overrides={"device": {"mode": "simulated"}}),
                runtime_dir=broker.runtime_dir,
                web_dir=None,
            )
            await replacement.start()
            await replacement.wait_healthy(timeout_s=10)

            denied = await call_tool(session, "press_key", {"code": "KeyA"})
            assert denied.is_error
            assert "INSTANCE_CHANGED" in text_of(denied) or "CONTROL_REVOKED" in text_of(denied)

            # The bridge recovers on the next call against the new instance.
            status = await call_tool(session, "get_status")
            payload = json.loads(text_of(status))
            assert payload["state"]["instance_id"] != old_instance
            again = await call_tool(session, "acquire_control")
            assert not again.is_error
            await call_tool(session, "release_control")
            await replacement.stop()
