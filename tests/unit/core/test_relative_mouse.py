"""Relative counts, wire bounds, cancellation and human stream invariants."""

from __future__ import annotations

import uuid

import pytest

from superuse.core.errors import ControlError
from superuse.core.models import (
    CommandEnvelope,
    HumanInputEnvelope,
    Mode,
    MouseClickRelative,
    MouseDragRelative,
    MouseMoveRelative,
    MouseRelativeState,
    MouseRelReport,
    MouseScrollRelative,
    ReleaseInputs,
    WireError,
    action_from_wire,
)
from superuse.device.protocol import encode_hid_report
from tests.unit.conftest import settle


async def submit(h, agent, lease, action):
    return await h.scheduler.submit_command(
        agent, CommandEnvelope(str(uuid.uuid4()), lease, action)
    )


async def finish(h, job):
    for _ in range(150):
        await h.advance(0.05)
        h.manager.frame_received()
        if job.terminal:
            break
    assert job.status.value == "written", job.error


@pytest.mark.parametrize("dx,dy", [(4096, -4096), (-128, 257), (0, 0)])
async def test_relative_counts_are_preserved_and_packets_bounded(harness, dx, dy):
    h = harness
    agent, lease = await h.acquire_agent()
    job = await submit(h, agent, lease.lease, MouseMoveRelative(dx, dy))
    await finish(h, job)
    reports = [w["report"] for w in h.input_writes(agent.session_id)]
    assert reports and all(isinstance(r, MouseRelReport) for r in reports)
    assert (sum(r.dx for r in reports), sum(r.dy for r in reports)) == (dx, dy)
    for report in reports:
        assert -127 <= report.dx <= 127 and -127 <= report.dy <= 127
        wire = encode_hid_report(report)
        assert wire[3:6] == bytes((5, 5, 1))
        assert wire[-1] == sum(wire[:-1]) & 255


async def test_relative_click_drag_scroll_and_release_never_send_absolute(harness):
    h = harness
    agent, lease = await h.acquire_agent()
    await finish(h, await submit(h, agent, lease.lease, MouseClickRelative("right", 2)))
    reports = [w["report"] for w in h.input_writes(agent.session_id)]
    assert [r.buttons for r in reports] == [2, 0, 2, 0]
    before = len(reports)
    await finish(h, await submit(h, agent, lease.lease, MouseDragRelative(4096, -500, "left", 50)))
    drag = [w["report"] for w in h.input_writes(agent.session_id)][before:]
    assert drag[0].buttons == 1 and drag[-1].buttons == 0
    assert sum(r.dx for r in drag) == 4096 and sum(r.dy for r in drag) == -500
    assert all(-127 <= r.dx <= 127 and -127 <= r.dy <= 127 for r in drag)
    await finish(h, await submit(h, agent, lease.lease, MouseScrollRelative(-3)))
    await finish(h, await submit(h, agent, lease.lease, ReleaseInputs()))
    mouse = [
        w["report"] for w in h.input_writes(agent.session_id) if hasattr(w["report"], "buttons")
    ]
    assert all(isinstance(r, MouseRelReport) for r in mouse)
    assert mouse[-2].wheel == -3 and mouse[-1] == MouseRelReport(0, 0, 0, 0)
    assert h.scheduler.owner_state(agent.session_id).buttons == 0


async def test_cancel_relative_drag_releases_without_pointer_warp(harness):
    h = harness
    agent, lease = await h.acquire_agent()
    job = await submit(h, agent, lease.lease, MouseDragRelative(1000, 1000, duration_ms=5000))
    await h.advance(0.05)
    assert h.scheduler.owner_state(agent.session_id).buttons == 1
    await h.scheduler.cancel_job(agent, job.job_id)
    await settle(25)
    assert job.status.value == "cancelled"
    mouse = [
        w["report"] for w in h.input_writes(agent.session_id) if hasattr(w["report"], "buttons")
    ]
    assert all(isinstance(r, MouseRelReport) for r in mouse)
    assert mouse[-1] == MouseRelReport(0, 0, 0, 0)
    assert h.scheduler.owner_state(agent.session_id).buttons == 0


async def test_takeover_cancels_relative_drag_and_no_late_old_reports(harness):
    h = harness
    agent, lease = await h.acquire_agent()
    job = await submit(h, agent, lease.lease, MouseDragRelative(1000, 0, duration_ms=5000))
    await h.advance(0.05)
    human = h.human()
    record = await h.manager.takeover(human)
    before = len(h.actor.writes)
    await h.advance(0.5)
    await settle(20)
    assert job.status.value == "cancelled"
    assert h.manager.current_lease == record and h.manager.mode is Mode.HUMAN
    assert not any(w["session_id"] == agent.session_id for w in h.actor.writes[before:])


async def test_human_relative_burst_preserves_deltas_and_duplicate_is_not_replayed(harness):
    h = harness
    human = h.human()
    lease = await h.manager.takeover(human)
    acks = []
    stream = h.scheduler.attach_stream(human, acks.append)
    for seq in range(1, 6):
        stream.handle(HumanInputEnvelope(seq, lease.lease, MouseRelativeState(10, -5, 0)))
    await settle(40)
    reports = [w["report"] for w in h.input_writes(human.session_id)]
    assert (
        len(reports) == 5 and sum(r.dx for r in reports) == 50 and sum(r.dy for r in reports) == -25
    )
    stream.handle(HumanInputEnvelope(5, lease.lease, MouseRelativeState(100, 100, 1)))
    await settle(10)
    assert len(h.input_writes(human.session_id)) == 5
    assert all(a.status == "written" for a in acks)
    stream.discard_pending()


async def test_agent_cannot_send_persistent_relative_button_state(harness):
    h = harness
    agent, lease = await h.acquire_agent()
    with pytest.raises(ControlError) as denied:
        await submit(h, agent, lease.lease, MouseRelativeState(0, 0, 1))
    assert denied.value.code.value == "FORBIDDEN"
    assert not h.input_writes(agent.session_id)


async def test_relative_queue_overflow_fences_even_pending_press(harness):
    h = harness
    human = h.human()
    lease = await h.manager.takeover(human)
    acks = []
    stream = h.scheduler.attach_stream(human, acks.append)
    h.actor.set_gate(False)
    stream.handle(HumanInputEnvelope(1, lease.lease, MouseRelativeState(0, 0, 1)))
    await settle()
    for seq in range(2, 129):
        stream.handle(HumanInputEnvelope(seq, lease.lease, MouseRelativeState(1, 0, 1)))
    assert len(stream._pending) == 128
    stream.handle(HumanInputEnvelope(129, lease.lease, MouseRelativeState(0, 0, 0)))
    assert acks[-1].error["code"] == "INPUT_BACKPRESSURE"
    h.actor.set_gate(True)
    await h.advance(0.1)
    await settle(20)
    assert h.manager.mode is Mode.HUMAN_HOLD


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "mouse_move_relative", "dx": True, "dy": 0},
        {"kind": "mouse_move_relative", "dx": 0.5, "dy": 0},
        {"kind": "mouse_move_relative", "dx": 4097, "dy": 0},
        {"kind": "mouse_scroll_relative", "steps": 0},
        {"kind": "mouse_relative_state", "dx": 128, "dy": 0, "buttons": 0},
        {"kind": "mouse_click_relative", "x": 100, "y": 100},
    ],
)
def test_relative_wire_rejects_invalid_counts_and_absolute_fields(action):
    with pytest.raises(WireError):
        action_from_wire(action)
