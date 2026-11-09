from __future__ import annotations

import asyncio

from superuse.core.models import WireError, action_from_wire
from superuse.device import keymap
from superuse.mcp.broker_client import BridgeError

CHUNK_SIZE = 64
TOTAL_TIMEOUT_S = 120


async def type_text(client, text: str, interval: int, progress) -> dict:
    try:
        action_from_wire({"kind": "type_text", "text": text, "char_interval_ms": interval})
    except WireError as exc:
        raise BridgeError("INVALID_REQUEST", str(exc)) from exc
    normalized = keymap.normalize_text(text)
    if not normalized:
        raise BridgeError("INPUT_LIMIT", "Text must not be empty")
    if any(keymap.char_to_hid(ch) is None for ch in normalized):
        raise BridgeError("UNSUPPORTED_TEXT", "Only US ASCII, LF and TAB are supported")
    lease = client.require_lease()
    reports = 0
    characters = 0
    job_ids = []
    completed_chunks = 0
    in_flight = False
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            for offset in range(0, len(normalized), CHUNK_SIZE):
                if client.lease is not lease:
                    raise BridgeError("CONTROL_REVOKED", "Typing lease changed between chunks")
                chunk = normalized[offset : offset + CHUNK_SIZE]
                in_flight = True
                job = await client.command(
                    {"kind": "type_text", "text": chunk, "char_interval_ms": interval}
                )
                in_flight = False
                reports += job["reports_written"]
                characters += min(len(chunk), job["reports_written"] // 2)
                job_ids.append(job["job_id"])
                if job["status"] != "written":
                    error = job.get("error") or {}
                    raise BridgeError(
                        error.get("code", "RESULT_UNKNOWN"),
                        f"Typing stopped; last_job_id={job['job_id']}; "
                        f"last_job_reports_written={job['reports_written']}",
                    )
                completed_chunks += 1
                await progress(characters, len(normalized))
    except (BridgeError, TimeoutError) as exc:
        code = exc.code if isinstance(exc, BridgeError) else "ACTION_TIMEOUT"
        raise BridgeError(
            code,
            f"Typing stopped: {exc}; confirmed_characters_written={characters}; "
            f"reports_written={reports}; chunks_completed={completed_chunks}; "
            f"current_chunk_result_unknown={in_flight}. "
            "Observe the target before continuing; do not resend the whole text. "
            "A failed chunk may have sent an additional key-down without a confirmed key-up.",
        ) from exc
    return {
        "completion": "written",
        "reports_written": reports,
        "steps_completed": reports,
        "job_id": job_ids[-1],
        "job_ids": job_ids,
        "characters_written": characters,
        "chunks_completed": len(job_ids),
    }
