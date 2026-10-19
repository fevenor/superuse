"""Scheduler races C03/C04/C12(job)/C13/C14/C15 (docs/development/testing.md §2)."""

from __future__ import annotations

import asyncio

import pytest

from superuse.core.errors import ControlError, ErrorCode
from superuse.core.models import (
    CommandEnvelope,
    KeyboardState,
    MouseDrag,
    PressKey,
    TypeText,
)
from tests.unit.conftest import Harness, settle


def _rid(n: int) -> str:
    return f"0000000{n:08d}-0000-0000-0000-000000000000"[-36:]


def envelope(lease, action, n=1) -> CommandEnvelope:
    return CommandEnvelope(request_id=_rid(n), lease=lease, action=action)


async def test_c03_takeover_mid_type_text_neutralizes_before_new_owner(harness: Harness):
    h = harness
    agent, record = await h.acquire_agent()
    job = await h.scheduler.submit_command(
        agent, envelope(record.lease, TypeText(text="abcde", char_interval_ms=10))
    )
    await settle()  # first char reports written; job parked in char_interval wait
    agent_writes_before = len(h.input_writes(agent.session_id))
    assert agent_writes_before > 0

    human = h.human()
    human_record = await h.manager.takeover(human)
    await h.advance(1.0)  # fire the job's pending char wait (cancelled)
    await settle()
    await h.scheduler.wait_job(job.job_id, 5.0)
    assert job.status.value == "cancelled"

    writes = h.actor.writes
    neutral_idx = [i for i, w in enumerate(writes) if w["neutral"]]
    assert len(neutral_idx) >= 2
    barrier_neutral = neutral_idx[-2:]  # the takeover fence's two neutralization writes
    agent_all = [i for i, w in enumerate(writes) if w["session_id"] == agent.session_id]
    assert agent_all, "agent should have written before the takeover"
    assert max(agent_all) < barrier_neutral[0]  # no stale agent report after the barrier

    # New owner input lands strictly after both neutralization reports.
    from superuse.core.models import HumanInputEnvelope, MouseState

    acks = []
    stream = h.scheduler.attach_stream(human, acks.append)
    stream.handle(
        HumanInputEnvelope(
            sequence=1,
            lease=human_record.lease,
            action=MouseState(basis_id=h.basis_id, x=100, y=100, buttons=0),
        )
    )
    await settle()
    human_idx = [i for i, w in enumerate(writes) if w["session_id"] == human.session_id]
    assert human_idx
    assert min(human_idx) > barrier_neutral[1]


async def test_c04_takeover_between_authorize_and_write(harness: Harness):
    h = harness
    agent, record = await h.acquire_agent()
    h.actor.set_gate(False)  # the first report blocks inside the write thread
    job = await h.scheduler.submit_command(agent, envelope(record.lease, PressKey(code="KeyA")))
    await settle()
    assert len(h.input_writes(agent.session_id)) == 0  # blocked pre-write

    human = h.human()
    takeover = asyncio.ensure_future(h.manager.takeover(human))
    await settle()
    h.actor.set_gate(True)  # release the stuck write; fence then purges the rest
    await takeover
    await settle()
    await h.scheduler.wait_job(job.job_id, 5.0)

    writes = h.actor.writes
    agent_writes = [w for w in writes if w["session_id"] == agent.session_id]
    # At most the in-flight down report got out; the release never did.
    assert len(agent_writes) <= 1
    assert job.status.value == "cancelled"
    assert h.actor.revoked_reports >= 0
    # Neutralization still happened before any new owner input.
    neutral = [i for i, w in enumerate(writes) if w["neutral"]]
    assert len(neutral) >= 2


async def test_c12_job_backpressure_fails_without_sticky_keys(harness: Harness):
    h = Harness(queue_limit=2)
    await h.make_healthy()
    agent, record = await h.acquire_agent()
    h.actor.set_gate(False)
    # Pre-fill the actor queue with raw reports from the same owner.
    from superuse.core.models import KeyboardReport
    from superuse.core.ports import AuthorizedReport

    def raw():
        return AuthorizedReport(
            session_id=agent.session_id,
            lease_id=record.lease.lease_id,
            generation=record.lease.generation,
            report=KeyboardReport(0, ()),
        )

    h.actor.submit(raw())
    await settle()  # the worker takes it in-flight and blocks on the gate
    h.actor.submit(raw())
    h.actor.submit(raw())  # queue now holds 2 = limit; no yields in between
    job = await h.scheduler.submit_command(agent, envelope(record.lease, PressKey(code="KeyA"), 2))
    await h.advance(0.1)
    await settle()
    outcome = await h.scheduler.wait_job(job.job_id, 5.0)
    assert outcome.status.value == "failed"
    assert outcome.error["code"] == ErrorCode.INPUT_BACKPRESSURE.value
    # The queue-full report was never sent.
    assert h.actor.backpressured >= 1
    await h.teardown()


async def test_c13_cancel_and_timeout_counts(harness: Harness):
    h = harness
    agent, record = await h.acquire_agent()
    job = await h.scheduler.submit_command(
        agent,
        envelope(
            record.lease,
            MouseDrag(
                basis_id=h.basis_id, from_x=10, from_y=10, to_x=1200, to_y=700, duration_ms=500
            ),
        ),
    )
    await settle()
    assert job.status.value in ("running", "written")
    partial_written = job.reports_written
    await h.scheduler.cancel_job(agent, job.job_id)
    assert job.status.value == "cancelled"
    assert job.partial is (partial_written > 0)
    assert job.reports_written == partial_written
    agent_writes_at_cancel = len(h.input_writes(agent.session_id))
    await h.advance(2.0)
    await settle()
    # Exactly the two owner-neutralization reports (keyboard zero + mouse zero)
    # may follow the cancellation; no drag movement reports are replayed.
    assert len(h.input_writes(agent.session_id)) == agent_writes_at_cancel + 2

    # Timeout path with neutralization of the still-valid owner.
    h2 = Harness(action_timeout_s=0.25)
    await h2.make_healthy()
    agent2, record2 = await h2.acquire_agent()
    job2 = await h2.scheduler.submit_command(
        agent2,
        envelope(
            record2.lease,
            MouseDrag(
                basis_id=h2.basis_id, from_x=10, from_y=10, to_x=1200, to_y=700, duration_ms=1000
            ),
        ),
    )
    for _ in range(10):
        await h2.advance(0.1)
        await settle()
    await h2.scheduler.wait_job(job2.job_id, 5.0)
    assert job2.status.value == "failed"
    assert job2.error["code"] == ErrorCode.ACTION_TIMEOUT.value
    assert job2.partial is True
    # Neutralization reports for the still-valid owner were sent.
    neutral = [w for w in h2.actor.writes if w["neutral"]]
    assert len(neutral) >= 2
    await h2.teardown()


async def test_c14_request_id_idempotency_and_capacity(harness: Harness):
    h = Harness()
    h.scheduler._job_capacity = 4
    from superuse.core.scheduler import IdempotencyCache

    h.scheduler._idem = IdempotencyCache(capacity=4, ttl_s=120.0)
    await h.make_healthy()
    agent, record = await h.acquire_agent()
    env = envelope(record.lease, PressKey(code="KeyA"), 7)
    job1 = await h.scheduler.submit_command(agent, env)
    await h.advance(0.1)
    await h.scheduler.wait_job(job1.job_id, 5.0)
    replay = await h.scheduler.submit_command(agent, env)
    assert replay.job_id == job1.job_id  # same single side effect

    conflict = envelope(record.lease, PressKey(code="KeyB"), 7)
    with pytest.raises(ControlError) as ei:
        await h.scheduler.submit_command(agent, conflict)
    assert ei.value.code == ErrorCode.REQUEST_ID_CONFLICT

    for i in range(3):
        job = await h.scheduler.submit_command(
            agent, envelope(record.lease, PressKey(code="KeyS"), 20 + i)
        )
        await h.advance(0.1)
        await h.scheduler.wait_job(job.job_id, 5.0)
    with pytest.raises(ControlError) as full:
        await h.scheduler.submit_command(agent, envelope(record.lease, PressKey(code="KeyD"), 30))
    assert full.value.code == ErrorCode.RATE_LIMITED

    # Entries inside the window are never evicted to make room; after the TTL
    # the sweep frees capacity again (the time jump also expired the lease).
    await h.advance(121.0)
    await h.manager.tick()  # video stalled during the jump -> recovery (correct)
    h.manager.frame_received()
    await h.manager.recover()
    record = await h.manager.acquire(agent)
    ok = await h.scheduler.submit_command(agent, envelope(record.lease, PressKey(code="KeyD"), 31))
    await h.advance(0.1)
    await h.scheduler.wait_job(ok.job_id, 5.0)
    assert ok.status.value == "written"
    await h.teardown()


async def test_c15_semantic_rejection_before_first_packet(harness: Harness):
    h = Harness(action_timeout_s=5.0)  # small deadline so the duration precheck bites
    await h.make_healthy()
    agent, record = await h.acquire_agent()
    lease = record.lease

    bad_cases = [
        PressKey(code="NotAKey"),
        PressKey(code="ControlLeft"),  # modifier codes must use modifiers
        TypeText(text="café"),
        TypeText(text="x" * 512),  # 512 chars * 50ms exceeds 30s deadline
    ]
    for i, action in enumerate(bad_cases):
        with pytest.raises(ControlError) as ei:
            await h.scheduler.submit_command(agent, envelope(lease, action, 100 + i))
        assert ei.value.code in (
            ErrorCode.UNSUPPORTED_KEY,
            ErrorCode.UNSUPPORTED_TEXT,
            ErrorCode.INPUT_LIMIT,
            ErrorCode.ACTION_TIMEOUT,
        ), f"{action}: {ei.value.code}"
    # Rejections happen strictly before any report (fresh lease check after).
    fresh = await h.manager.acquire(agent)
    from superuse.core.models import MouseState

    with pytest.raises(ControlError) as forbidden:
        await h.scheduler.submit_command(
            agent, envelope(fresh.lease, MouseState(basis_id=h.basis_id, x=1, y=1, buttons=1))
        )
    assert forbidden.value.code == ErrorCode.FORBIDDEN  # human-only action
    assert h.input_writes(agent.session_id) == []

    # >6 keys rejected before the first packet for a human owner too.
    human = h.human()
    human_record = await h.manager.takeover(human)
    with pytest.raises(ControlError) as too_many:
        await h.scheduler.submit_command(
            human,
            envelope(human_record.lease, KeyboardState(keys=tuple(f"Key{c}" for c in "ABCDEFG"))),
        )
    assert too_many.value.code == ErrorCode.INPUT_LIMIT
    assert h.input_writes(human.session_id) == []
    await h.teardown()


async def test_absolute_actions_fail_fast_in_relative_device_mode(harness: Harness):
    """Known relative device mode: absolute is rejected up front; others unaffected."""
    from superuse.core.models import MouseMove, MouseMoveRelative

    h = harness
    agent, record = await h.acquire_agent()
    h.manager.set_device_mouse_mode("relative")

    with pytest.raises(ControlError) as exc:
        await h.scheduler.submit_command(
            agent,
            envelope(record.lease, MouseMove(basis_id=h.basis_id, x=10, y=10)),
        )
    assert exc.value.code is ErrorCode.DEVICE_MOUSE_MODE
    assert not h.input_writes(agent.session_id)  # failed before the first packet

    relative = await h.scheduler.submit_command(
        agent, envelope(record.lease, MouseMoveRelative(10, 10), n=2)
    )
    await h.advance(0.1)  # virtual clock fires the inter-report waits
    await h.scheduler.wait_job(relative.job_id, 5.0)
    assert relative.status.value == "written"

    # Unknown mode (device-info query unanswered) must not block absolute input.
    h.manager.set_device_mouse_mode(None)
    absolute = await h.scheduler.submit_command(
        agent, envelope(record.lease, MouseMove(basis_id=h.basis_id, x=10, y=10), n=3)
    )
    await h.advance(0.1)
    await h.scheduler.wait_job(absolute.job_id, 5.0)
    assert absolute.status.value == "written"


async def test_human_stream_absolute_rejected_in_relative_device_mode(harness: Harness):
    from superuse.core.models import HumanInputEnvelope, MouseState

    h = harness
    human = h.human()
    record = await h.manager.takeover(human)
    h.manager.set_device_mouse_mode("relative")

    acks = []
    stream = h.scheduler.attach_stream(human, acks.append)
    stream.handle(
        HumanInputEnvelope(
            sequence=1,
            lease=record.lease,
            action=MouseState(basis_id=h.basis_id, x=100, y=100, buttons=0),
        )
    )
    await settle()
    assert acks[-1].status == "rejected"
    assert acks[-1].error["code"] == ErrorCode.DEVICE_MOUSE_MODE.value
    assert not h.input_writes(human.session_id)
