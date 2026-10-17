"""MCP server: Superuse tools over stdio and Streamable HTTP (api-contract §2).

Built on the locked official SDK v2 (MCPServer). stdout carries MCP only;
diagnostics go to stderr. Tools never accept session ids, paths or raw
serial; the bridge attaches its own lease. Successful input tools report
``completion="written"`` — no target-UI confirmation is implied.

Client binding (task 2026-10-17 T4): ``build_server`` accepts either one
``BrokerClient`` (the stdio bridge) or an async resolver that maps a request
context to a client (the HTTP service, one independent client per MCP
transport session). Tool bodies always go through the resolver so both
transports share one implementation.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import TextContent

from superuse.core.errors import ErrorCode
from superuse.mcp.broker_client import BridgeError, BrokerClient

logger = logging.getLogger("superuse.mcp")

TOOL_DESCRIPTIONS = {
    "get_status": (
        "Current device/control state, source (real/simulated) and capabilities."
        " Read-only; does not acquire or renew control."
    ),
    "acquire_control": (
        "Request exclusive control for this agent. Fails with HUMAN_CONTROL when"
        " a human holds the latch or CONTROL_BUSY when another agent owns it."
    ),
    "release_control": (
        "Release this agent's control lease and neutralize"
        " inputs. Idempotent no-op without a lease."
    ),
    "capture_screen": (
        "Capture a fresh screenshot of the target. Returns image/jpeg plus frame"
        " metadata (basis_id, sizes, frame_id, age). Does not acquire or renew."
    ),
    "press_key": (
        "Press one physical key (W3C code like KeyA, Enter, F2) with"
        " optional modifiers for a bounded hold. Requires control."
    ),
    "type_text": (
        "Type US ASCII text (LF/TAB allowed; CR normalized). No template tags. Requires control."
    ),
    "mouse_move": (
        "Move the pointer to image-pixel coordinates of the"
        " latest screenshot's basis. Requires control."
    ),
    "mouse_click": "Click (single or double) at image-pixel coordinates; needs control.",
    "mouse_drag": (
        "Drag with a button held between two points over a"
        " bounded duration; cancellable. Requires control."
    ),
    "mouse_scroll": "Scroll positive-up steps at a point. Requires control.",
    "mouse_move_relative": (
        "Move by HID counts, NOT image pixels. Requires control and fresh video. "
        "Re-observe after small moves; pointer acceleration/clipping may apply."
    ),
    "mouse_click_relative": (
        "Click at the CURRENT pointer position. No implicit absolute move. Requires control."
    ),
    "mouse_drag_relative": (
        "Drag from the current pointer by bounded HID counts; releases its button "
        "on completion/cancel. Requires control."
    ),
    "mouse_scroll_relative": (
        "Scroll positive-up steps at the current pointer position. Requires control."
    ),
    "release_inputs": (
        "Neutralize only this agent's held keys/buttons; keeps the lease. Requires control."
    ),
    "cancel_action": (
        "Cancel this agent's running job by id; best effort, does not roll back written input."
    ),
}

_ACQUIRE_HINT = (
    "Acquire control with acquire_control first; observe the state, do not retry in a tight loop."
)

ClientResolver = Callable[[Context], Awaitable[BrokerClient]]
StateProvider = Callable[[], dict]


def _bridge_error(exc: BridgeError) -> ToolError:
    return ToolError(f"{exc.code}: {exc.args[0]}")


def build_server(
    client: BrokerClient | ClientResolver,
    *,
    state_provider: StateProvider | None = None,
) -> MCPServer:
    """Build the Superuse MCPServer.

    ``client`` is either a single bound BrokerClient (stdio bridge) or an
    async ``Context -> BrokerClient`` resolver (HTTP service: one client per
    MCP transport session). ``state_provider`` optionally serves the
    ``superuse://state`` resource without a per-session client (resources
    carry no request context in the locked SDK).
    """
    if isinstance(client, BrokerClient):
        fixed_client: BrokerClient | None = client

        async def _client(_ctx: Context) -> BrokerClient:
            assert fixed_client is not None
            return fixed_client

        resolver = _client
    else:
        fixed_client = None
        resolver = client

    server: MCPServer = MCPServer(
        name="superuse",
        instructions=(
            "Superuse target control. Acquire control before input tools; after "
            "CONTROL_REVOKED/HUMAN_CONTROL, re-observe with capture_screen instead "
            "of retrying. Coordinates are pixels in the image returned by "
            "capture_screen, not the native resolution. Absolute tools may not work on some "
            "targets/lock screens. Use explicit *_relative tools there; their deltas are HID "
            "counts, not pixels. Take small steps and inspect screenshots; never assume an "
            "absolute pointer position from accumulated deltas."
        ),
    )

    async def get_status(ctx: Context) -> str:
        client = await resolver(ctx)
        status = await client.get_status()
        return json.dumps(status, ensure_ascii=False)

    async def acquire_control(ctx: Context, ttl_ms: int = 15000) -> str:
        client = await resolver(ctx)
        ttl = max(5000, min(30000, int(ttl_ms)))
        try:
            grant = await client.acquire(ttl_ms=ttl)
        except BridgeError as exc:
            raise _bridge_error(exc) from exc
        return json.dumps(
            {
                "lease": grant["lease"],
                "expires_in_ms": grant["expires_in_ms"],
                "state": grant["state"],
            },
            ensure_ascii=False,
        )

    async def release_control(ctx: Context) -> str:
        client = await resolver(ctx)
        await client.release()
        return json.dumps({"released": True})

    async def capture_screen(
        ctx: Context,
        max_width: int = 1280,
        after_frame_id: int | None = None,
        wait_ms: int = 2000,
    ) -> list:
        client = await resolver(ctx)
        width = max(320, min(1920, int(max_width)))
        wait = max(0, min(2000, int(wait_ms)))
        try:
            snapshot = await client.snapshot(
                max_width=width, after_frame_id=after_frame_id, wait_ms=wait
            )
        except BridgeError as exc:
            raise _bridge_error(exc) from exc
        jpeg = base64.b64decode(snapshot["data_base64"])
        meta = snapshot["metadata"]
        text = json.dumps(
            {
                "basis_id": meta["basis_id"],
                "image_width": meta["image_width"],
                "image_height": meta["image_height"],
                "native_width": meta["native_width"],
                "native_height": meta["native_height"],
                "frame_id": meta["frame_id"],
                "geometry_id": meta["geometry_id"],
                "captured_at": meta["captured_at"],
                "age_ms": meta["age_ms"],
                "source": meta["source"],
            },
            ensure_ascii=False,
        )
        return [Image(data=jpeg, format="jpeg"), TextContent(type="text", text=text)]

    async def _run_action(ctx: Context, action: dict) -> str:
        client = await resolver(ctx)
        try:
            job = await client.command(action)
        except BridgeError as exc:
            hint = "" if exc.code != ErrorCode.CONTROL_REQUIRED.value else " " + _ACQUIRE_HINT
            raise ToolError(f"{exc.code}: {exc.args[0]}{hint}") from exc
        if job["status"] != "written":
            error = job.get("error") or {}
            raise ToolError(
                f"{error.get('code', ErrorCode.RESULT_UNKNOWN.value)}: "
                f"{error.get('message', 'action failed')} "
                f"(partial={job['partial']}, reports_written={job['reports_written']}, "
                f"steps_completed={job['steps_completed']}, job_id={job['job_id']})"
            )
        return json.dumps(
            {
                "completion": "written",
                "reports_written": job["reports_written"],
                "steps_completed": job["steps_completed"],
                "job_id": job["job_id"],
            }
        )

    async def press_key(
        ctx: Context, code: str, modifiers: list[str] | None = None, hold_ms: int = 50
    ) -> str:
        return await _run_action(
            ctx,
            {
                "kind": "press_key",
                "code": code,
                "modifiers": modifiers or [],
                "hold_ms": max(10, min(1000, int(hold_ms))),
            },
        )

    async def type_text(ctx: Context, text: str, char_interval_ms: int = 15) -> str:
        return await _run_action(
            ctx,
            {
                "kind": "type_text",
                "text": text,
                "char_interval_ms": max(10, min(50, int(char_interval_ms))),
            },
        )

    async def mouse_move(ctx: Context, basis_id: str, x: float, y: float) -> str:
        return await _run_action(ctx, {"kind": "mouse_move", "basis_id": basis_id, "x": x, "y": y})

    async def mouse_click(
        ctx: Context, basis_id: str, x: float, y: float, button: str = "left", count: int = 1
    ) -> str:
        return await _run_action(
            ctx,
            {
                "kind": "mouse_click",
                "basis_id": basis_id,
                "x": x,
                "y": y,
                "button": button,
                "count": count,
            },
        )

    async def mouse_drag(
        ctx: Context,
        basis_id: str,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        button: str = "left",
        duration_ms: int = 500,
    ) -> str:
        return await _run_action(
            ctx,
            {
                "kind": "mouse_drag",
                "basis_id": basis_id,
                "from_x": from_x,
                "from_y": from_y,
                "to_x": to_x,
                "to_y": to_y,
                "button": button,
                "duration_ms": max(50, min(5000, int(duration_ms))),
            },
        )

    async def mouse_scroll(ctx: Context, basis_id: str, x: float, y: float, steps: int) -> str:
        if steps == 0:
            raise ToolError("INPUT_LIMIT: steps must be a non-zero value in -127..127")
        return await _run_action(
            ctx, {"kind": "mouse_scroll", "basis_id": basis_id, "x": x, "y": y, "steps": steps}
        )

    async def release_inputs(ctx: Context) -> str:
        return await _run_action(ctx, {"kind": "release_inputs"})

    async def mouse_move_relative(ctx: Context, dx: int, dy: int) -> str:
        return await _run_action(ctx, {"kind": "mouse_move_relative", "dx": dx, "dy": dy})

    async def mouse_click_relative(ctx: Context, button: str = "left", count: int = 1) -> str:
        return await _run_action(
            ctx, {"kind": "mouse_click_relative", "button": button, "count": count}
        )

    async def mouse_drag_relative(
        ctx: Context, dx: int, dy: int, button: str = "left", duration_ms: int = 500
    ) -> str:
        return await _run_action(
            ctx,
            {
                "kind": "mouse_drag_relative",
                "dx": dx,
                "dy": dy,
                "button": button,
                "duration_ms": duration_ms,
            },
        )

    async def mouse_scroll_relative(ctx: Context, steps: int) -> str:
        return await _run_action(ctx, {"kind": "mouse_scroll_relative", "steps": steps})

    async def cancel_action(ctx: Context, job_id: str) -> str:
        client = await resolver(ctx)
        try:
            job = await client.cancel(job_id)
        except BridgeError as exc:
            raise _bridge_error(exc) from exc
        return json.dumps(job)

    registrations = [
        (get_status, "get_status"),
        (acquire_control, "acquire_control"),
        (release_control, "release_control"),
        (capture_screen, "capture_screen"),
        (press_key, "press_key"),
        (type_text, "type_text"),
        (mouse_move, "mouse_move"),
        (mouse_click, "mouse_click"),
        (mouse_drag, "mouse_drag"),
        (mouse_scroll, "mouse_scroll"),
        (mouse_move_relative, "mouse_move_relative"),
        (mouse_click_relative, "mouse_click_relative"),
        (mouse_drag_relative, "mouse_drag_relative"),
        (mouse_scroll_relative, "mouse_scroll_relative"),
        (release_inputs, "release_inputs"),
        (cancel_action, "cancel_action"),
    ]
    for fn, name in registrations:
        server.add_tool(fn, name=name, description=TOOL_DESCRIPTIONS[name])

    async def read_state() -> str:
        if state_provider is not None:
            return json.dumps(state_provider(), ensure_ascii=False)
        if fixed_client is None:
            # Resolver-mode servers must wire a state provider: the locked
            # SDK reads resources without request context (no session to
            # resolve a client from).
            raise RuntimeError("superuse://state needs a state_provider on HTTP servers")
        return json.dumps(await fixed_client.get_status(), ensure_ascii=False)

    from mcp.server.mcpserver.resources.types import FunctionResource

    server.add_resource(
        FunctionResource(
            uri="superuse://state",
            name="superuse-state",
            description="Live broker state (mode, owner, device health, source).",
            mime_type="application/json",
            fn=read_state,
        )
    )
    return server


async def run_bridge(*, runtime_dir: Path, label: str) -> None:
    client = BrokerClient(runtime_dir=runtime_dir, label=label)
    await client.start()
    server = build_server(client)
    try:
        await server.run_stdio_async()
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="superuse mcp", description="Superuse MCP stdio bridge")
    parser.add_argument("--client-label", default="agent")
    parser.add_argument("--runtime-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    from superuse.paths import resolve_paths

    runtime_dir = resolve_paths(runtime_dir=args.runtime_dir).runtime_dir
    asyncio.run(run_bridge(runtime_dir=runtime_dir, label=args.client_label))
    return 0
