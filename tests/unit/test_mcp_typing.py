from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from superuse.mcp.broker_client import BridgeError, BrokerClient


def client(tmp_path):
    result = BrokerClient(runtime_dir=tmp_path, label="typing-test")
    result._lease = {"generation": 1}
    return result


def completed(action):
    reports = len(action["text"]) * 2
    return {"status": "written", "reports_written": reports, "job_id": "test-job"}


async def test_validates_entire_text_before_first_chunk(tmp_path):
    bridge = client(tmp_path)
    bridge.command = AsyncMock(side_effect=completed)
    with pytest.raises(BridgeError, match="UNSUPPORTED_TEXT"):
        await bridge.type_text("a" * 128 + "\u4e2d", 15, AsyncMock())
    bridge.command.assert_not_called()


async def test_partial_failure_stops_without_replay(tmp_path):
    bridge = client(tmp_path)
    bridge.command = AsyncMock(
        side_effect=[
            {"status": "written", "reports_written": 128, "job_id": "one"},
            {
                "status": "failed",
                "reports_written": 7,
                "job_id": "two",
                "error": {"code": "ACTION_TIMEOUT"},
            },
        ]
    )
    with pytest.raises(BridgeError, match="confirmed_characters_written=67") as exc:
        await bridge.type_text("a" * 200, 15, AsyncMock())
    assert "chunks_completed=1" in str(exc.value)
    assert "do not resend the whole text" in str(exc.value)
    assert bridge.command.call_count == 2
    assert bridge._typing_task is None


async def test_reacquired_lease_never_continues_old_text(tmp_path):
    bridge = client(tmp_path)
    bridge.command = AsyncMock(side_effect=completed)

    async def progress(*_):
        bridge._lease = {"generation": 2}

    with pytest.raises(BridgeError, match="CONTROL_REVOKED"):
        await bridge.type_text("a" * 129, 15, progress)
    assert bridge.command.call_count == 1


async def test_other_input_cannot_interleave_between_chunks(tmp_path):
    bridge = client(tmp_path)
    bridge.request = AsyncMock()
    arrived = asyncio.Event()
    resume = asyncio.Event()

    async def response(method, path, **kwargs):
        if method == "POST":
            return {"job_id": "one"}
        return {**completed({"text": "a" * 64}), "terminal": True}

    bridge.request.side_effect = response

    async def progress(*_):
        arrived.set()
        await resume.wait()

    task = asyncio.create_task(bridge.type_text("a" * 128, 15, progress))
    await arrived.wait()
    try:
        with pytest.raises(BridgeError, match="CONTROL_BUSY"):
            await bridge.command({"kind": "press_key", "code": "KeyB"})
        with pytest.raises(BridgeError, match="CONTROL_BUSY"):
            await bridge.type_text("other", 15, AsyncMock())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert bridge._typing_task is None
    assert bridge.request.call_count == 2


async def test_total_deadline_cancels_current_chunk_without_retry(tmp_path, monkeypatch):
    from superuse.mcp import typing

    monkeypatch.setattr(typing, "TOTAL_TIMEOUT_S", 0.01)
    bridge = client(tmp_path)
    cancelled = asyncio.Event()

    async def stuck(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bridge.command = AsyncMock(side_effect=stuck)
    with pytest.raises(BridgeError, match="current_chunk_result_unknown=True"):
        await bridge.type_text("a" * 128, 15, AsyncMock())
    assert cancelled.is_set()
    assert bridge.command.call_count == 1
    assert bridge._typing_task is None
