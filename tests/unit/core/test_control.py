"""C-series control state machine races (docs/development/testing.md §2).

All timing is virtual; blocking writes are scheduled through the actor gate.
"""

from __future__ import annotations

import asyncio

import pytest

from superuse.core.control import ControlManager
from superuse.core.errors import ControlError, ErrorCode
from superuse.core.models import Mode
from superuse.core.ports import FenceResult, FenceTicket
from tests.unit.conftest import Harness, MemoryLatchStore, settle


async def test_c01_idle_to_agent_and_same_session_reentry(harness: Harness):
    h = harness
    agent, record = await h.acquire_agent()
    assert h.manager.mode is Mode.AGENT
    assert h.manager.current_lease is record
    fences_after_acquire = len(h.actor.fences)
    generation = h.manager.generation

    # Same session re-acquire: same lease, no new barrier, inputs untouched.
    again = await h.manager.acquire(agent)
    assert again.lease.lease_id == record.lease.lease_id
    assert len(h.actor.fences) == fences_after_acquire
    assert h.manager.generation == generation


async def test_c02_two_agents_only_one_wins(harness: Harness):
    h = harness
    winner, record = await h.acquire_agent()
    loser = h.agent("loser")
    with pytest.raises(ControlError) as ei:
        await h.manager.acquire(loser)
    assert ei.value.code == ErrorCode.CONTROL_BUSY
    # The loser never produced a report.
    assert h.input_writes(loser.session_id) == []
    assert h.manager.current_lease.lease.lease_id == record.lease.lease_id


async def test_c05_blocked_write_during_takeover_bounded_failure(harness: Harness):
    h = Harness(fence_timeout_s=2.0)
    await h.make_healthy()
    agent, record = await h.acquire_agent()
    neutral_baseline = len(h.neutral_writes())  # recover/acquire barriers already sent theirs
    h.actor.set_gate(False)  # one write is now blocked mid-flight
    task = asyncio.ensure_future(h.manager.takeover(h.human()))
    await settle()
    await h.advance(2.5)  # fence deadline fires
    with pytest.raises(ControlError) as ei:
        await task
    assert ei.value.code == ErrorCode.RECOVERY_REQUIRED
    state = h.manager.get_state()
    assert state.mode is Mode.RECOVERY
    assert state.human_latched is True  # takeover latch persists
    assert h.manager.current_lease is None  # human NOT granted
    assert len(h.neutral_writes()) == neutral_baseline  # failed fence sent no neutralization
    await h.teardown()


async def test_c06_late_old_owner_cleanup_never_releases_new_owner(harness: Harness):
    h = harness
    stale_agent, _ = await h.acquire_agent()
    human1 = h.human("h1")
    await h.manager.takeover(human1)
    await settle()

    # human1 loses the input connection -> human_hold (latch kept).
    await h.manager.human_suspend(human1, "human_blur")
    assert h.manager.mode is Mode.HUMAN_HOLD

    human2 = h.human("h2")
    await h.manager.takeover(human2)
    await settle()
    fences_before = len(h.actor.fences)

    # Late cleanup from the *old* sessions arrives now.
    await h.manager.human_suspend(human1, "human_blur")  # late blur/close
    await h.manager.session_closed(stale_agent)
    await h.manager.session_closed(human1)
    await settle()

    assert h.manager.mode is Mode.HUMAN
    assert h.manager.get_state().owner.session_id == human2.session_id
    assert len(h.actor.fences) == fences_before  # no spurious barriers
    # Late agent job finally (neutralize_owner) wrote nothing for the old owner.
    late = [w for w in h.actor.writes if w["session_id"] == stale_agent.session_id]
    assert all(w["generation"] < h.manager.generation for w in late)


async def test_old_input_socket_cleanup_cannot_suspend_same_page_new_lease(harness: Harness):
    h = harness
    human = h.human("same-page")
    old = await h.manager.takeover(human)
    await h.manager.human_suspend(human, "human_blur", expected_lease=old.lease)
    new = await h.manager.takeover(human)
    fences_before = len(h.actor.fences)
    await h.manager.human_suspend(human, "human_input_closed", expected_lease=old.lease)
    assert h.manager.mode is Mode.HUMAN
    assert h.manager.current_lease is new
    assert len(h.actor.fences) == fences_before
    await h.manager.human_suspend(human, "human_input_closed", expected_lease=new.lease)
    assert h.manager.mode is Mode.HUMAN_HOLD
    assert h.manager.get_state().human_latched


async def test_c07_out_of_order_fence_completions_ignored(harness: Harness):
    h = harness
    agent, _ = await h.acquire_agent()
    h.actor.set_gate(False)  # hold the takeover fence in the gate wait
    human1 = h.human("h1")
    task = asyncio.ensure_future(h.manager.takeover(human1))
    await settle()

    ticket1 = h.manager.pending_ticket
    assert ticket1 is not None
    stale = FenceTicket(generation=ticket1.generation - 3, transition_id="stale")
    h.manager.complete_barrier(stale, FenceResult(ok=True))
    await settle()
    assert not task.done()  # stale completion did not finish the barrier

    # The genuine ticket resolves the barrier even out of band.
    h.manager.complete_barrier(ticket1, FenceResult(ok=True))
    await settle()
    record = await task
    assert h.manager.mode is Mode.HUMAN
    assert h.manager.get_state().owner.session_id == human1.session_id

    # Second takeover by another human with the gate still closed, so its
    # barrier is pending and we can inject out-of-order completions.
    human2 = h.human("h2")
    task2 = asyncio.ensure_future(h.manager.takeover(human2))
    await settle()
    ticket2 = h.manager.pending_ticket
    assert ticket2 is not None and ticket2 != ticket1
    h.manager.complete_barrier(stale, FenceResult(ok=True))
    h.manager.complete_barrier(ticket1, FenceResult(ok=True))
    await settle()
    assert not task2.done()  # neither stale nor ticket1 completed ticket2's barrier
    h.manager.complete_barrier(ticket2, FenceResult(ok=True))
    await settle()
    record2 = await task2
    assert h.manager.mode is Mode.HUMAN
    assert h.manager.get_state().owner.session_id == human2.session_id
    assert record2.lease.lease_id != record.lease.lease_id

    h.actor.set_gate(True)  # let the leftover actor fence finish harmlessly
    await settle()


async def test_c08_human_disconnect_and_ttl_keep_latch(harness: Harness):
    h = harness
    human = h.human()
    await h.manager.takeover(human)
    await h.manager.human_suspend(human, "human_ws_closed")
    assert h.manager.mode is Mode.HUMAN_HOLD
    with pytest.raises(ControlError) as ei:
        await h.manager.acquire(h.agent())
    assert ei.value.code == ErrorCode.HUMAN_CONTROL

    # TTL expiry path on a fresh harness: human lease expires to human_hold.
    h2 = Harness(lease_ttl_s=0.5)
    await h2.make_healthy()
    human2 = h2.human()
    await h2.manager.takeover(human2)
    await h2.advance(0.6)
    await h2.manager.tick()
    assert h2.manager.mode is Mode.HUMAN_HOLD
    with pytest.raises(ControlError) as ei2:
        await h2.manager.acquire(h2.agent())
    assert ei2.value.code == ErrorCode.HUMAN_CONTROL
    await h2.teardown()


async def test_c09_handback_requires_new_acquire(harness: Harness):
    h = harness
    agent, old_record = await h.acquire_agent()
    old_lease = old_record.lease
    human = h.human()
    await h.manager.takeover(human)
    await h.manager.handback(human)
    assert h.manager.mode is Mode.IDLE
    assert h.manager.get_state().human_latched is False

    # Old lease is dead: input with it fails; a fresh acquire works.
    from superuse.core.models import CommandEnvelope, MouseMove, PressKey

    with pytest.raises(ControlError) as ei:
        await h.scheduler.submit_command(
            agent,
            CommandEnvelope(
                request_id="11111111-1111-1111-1111-111111111111",
                lease=old_lease,
                action=MouseMove(basis_id=h.basis_id, x=10, y=10),
            ),
        )
    assert ei.value.code in (ErrorCode.CONTROL_REVOKED, ErrorCode.CONTROL_REQUIRED)
    record = await h.manager.acquire(agent)
    assert record.lease.lease_id != old_lease.lease_id
    job = await h.scheduler.submit_command(
        agent,
        CommandEnvelope(
            request_id="22222222-2222-2222-2222-222222222222",
            lease=record.lease,
            action=PressKey(code="KeyA"),
        ),
    )
    await h.advance(0.1)  # fire the hold timer
    await h.scheduler.wait_job(job.job_id, 5.0)
    assert job.status.value == "written"


async def test_c10_renew_takeover_interleaving_and_expired_renew(harness: Harness):
    h = harness
    agent, record = await h.acquire_agent()
    lease = record.lease
    remaining = await h.manager.renew(agent, lease)
    assert remaining > 0

    human = h.human()
    await h.manager.takeover(human)
    with pytest.raises(ControlError) as ei:
        await h.manager.renew(agent, lease)
    assert ei.value.code == ErrorCode.CONTROL_REVOKED

    # Expired-but-not-yet-ticked lease: LEASE_EXPIRED, never resurrected.
    h2 = Harness(lease_ttl_s=0.5)
    await h2.make_healthy()
    agent2, record2 = await h2.acquire_agent()
    await h2.advance(0.6)
    with pytest.raises(ControlError) as ei2:
        await h2.manager.renew(agent2, record2.lease)
    assert ei2.value.code == ErrorCode.LEASE_EXPIRED
    # After the expiry barrier the lease is gone entirely.
    await h2.manager.tick()
    with pytest.raises(ControlError) as ei3:
        await h2.manager.renew(agent2, record2.lease)
    assert ei3.value.code in (ErrorCode.LEASE_EXPIRED, ErrorCode.CONTROL_REVOKED)
    await h2.teardown()


async def test_c11_instance_change_invalidates_old_lease(harness: Harness):
    h = harness
    agent, record = await h.acquire_agent()
    old_lease = record.lease

    manager2 = ControlManager(
        clock=h.clock,
        actor=h.actor,
        latch_store=MemoryLatchStore(),
        source="simulated",
    )
    assert manager2.instance_id != h.manager.instance_id
    with pytest.raises(ControlError) as ei:
        manager2.authorize_input(agent, old_lease)
    assert ei.value.code == ErrorCode.INSTANCE_CHANGED
    with pytest.raises(ControlError) as ei2:
        await manager2.renew(agent, old_lease)
    assert ei2.value.code == ErrorCode.INSTANCE_CHANGED


async def test_c16_shutdown_wins_over_late_takeover_callback(harness: Harness):
    h = harness
    agent, _ = await h.acquire_agent()
    h.actor.set_gate(False)
    human = h.human()
    task = asyncio.ensure_future(h.manager.takeover(human))
    await settle()

    stop_task = asyncio.ensure_future(h.manager.stop())
    await settle()
    assert h.manager.get_state().mode is Mode.STOPPING
    # Late barrier completion must not resurrect an owner.
    h.manager.complete_barrier(
        FenceTicket(generation=h.manager.generation, transition_id="late"), FenceResult(ok=True)
    )
    await settle()
    assert h.manager.get_state().mode is Mode.STOPPING
    assert h.manager.current_lease is None

    await h.advance(2.5)  # let the stop fence time out on the gate
    await stop_task
    with pytest.raises((ControlError, asyncio.InvalidStateError)):
        task.result()
    assert h.manager.get_state().mode is Mode.STOPPING
