"""Regression tests for the 2026-10-17 implementation review (R01/R03).

R01: a *completed* barrier must never suppress a later fault's generation
bump and neutralization. R03: click/drag release reports must lift the
action's own button, not replay the freshly pressed state.
"""

from __future__ import annotations

import asyncio

import pytest

from superuse.core.models import CommandEnvelope, DeviceStatus, Mode
from tests.unit.conftest import Harness, settle

pytestmark = pytest.mark.asyncio


def _rid(n: int) -> str:
    return f"0000000{n:08d}-0000-0000-0000-000000000000"[-36:]


async def test_r01_completed_barrier_does_not_skip_fault_neutralization(harness: Harness):
    """acquire -> input held -> geometry change must bump generation and
    neutralize, even though a (completed) acquire barrier came before."""
    h = harness
    agent, record = await h.acquire_agent()
    generation_after_acquire = h.manager.generation
    # The acquire barrier completed; no pending transition may remain.
    assert h.manager.pending_ticket is None

    # Hold a key via the agent's own press_key job is bounded; instead park a
    # pressed state through the scheduler's owner state using a long drag...
    # simplest deterministic held-input: submit and let it finish, then keep
    # the owner state pressed via a human? Agent-held input uses jobs; the
    # fault path is what matters here: revoke + neutralize on geometry change.
    from superuse.core.models import MouseDrag

    job = await h.scheduler.submit_command(
        agent,
        CommandEnvelope(
            request_id=_rid(1),
            lease=record.lease,
            action=MouseDrag(
                basis_id=h.basis_id, from_x=5, from_y=5, to_x=600, to_y=400, duration_ms=300
            ),
        ),
    )
    await h.advance(0.05)  # drag is mid-flight with the button held
    writes_before = len(h.actor.writes)

    h.manager.geometry_changed()
    await settle(6)

    assert h.manager.mode is Mode.RECOVERY
    # A NEW generation was adopted (queued stale reports get rejected) ...
    assert h.manager.generation > generation_after_acquire
    # ... and best-effort neutralization actually hit the wire after the fault
    # (two zero reports: keyboard + mouse).
    neutral_after = [w for w in h.actor.writes[writes_before:] if w["neutral"]]
    assert len(neutral_after) >= 2
    job_done = await h.scheduler.wait_job(job.job_id, 1.0)
    assert job_done.status.value in ("cancelled", "failed")


async def test_r01_real_actor_video_fault_after_acquire():
    """Same guarantee through the real SerialActor + simulated transport."""
    from superuse.core.clock import MonotonicClock
    from superuse.core.control import ControlManager
    from superuse.core.frames import BasisRegistry
    from superuse.core.models import KeyboardReport, MouseRelReport, Session, SessionKind
    from superuse.core.scheduler import InputScheduler
    from superuse.device.protocol import encode_hid_report
    from superuse.device.serial_actor import SerialActor
    from superuse.device.simulated import SimulatedSerial
    from tests.unit.conftest import MemoryLatchStore

    serial = SimulatedSerial()
    loop = asyncio.get_running_loop()
    actor = SerialActor(lambda: serial, loop=loop, on_status=lambda status: None)
    actor.start(query_device_info=True)
    manager = ControlManager(
        clock=MonotonicClock(),
        actor=actor,
        latch_store=MemoryLatchStore(),
        source="simulated",
    )
    bases = BasisRegistry(manager.instance_id)
    scheduler = InputScheduler(clock=MonotonicClock(), actor=actor, manager=manager, bases=bases)
    manager.set_owner_cancel_hook(scheduler.cancel_owner_jobs)
    try:
        manager.set_serial_status(DeviceStatus.READY)
        manager.set_video_status(DeviceStatus.READY)
        manager.frame_received()
        await manager.recover()
        agent = Session(
            session_id="a" * 8 + "-0000-0000-0000-" + "a" * 12, kind=SessionKind.AGENT, label="a"
        )
        record = await manager.acquire(agent)
        assert manager.pending_ticket is None

        def hid_writes():
            return [w for w in serial.writes if w[:3] == b"\x57\xab\x00" and w[3] in (2, 4, 5)]

        before = len(hid_writes())
        manager.set_video_status(DeviceStatus.FAULT)  # video fault while agent owns
        for _ in range(50):
            await asyncio.sleep(0.02)
            if manager.mode is Mode.RECOVERY:
                break
        assert manager.mode is Mode.RECOVERY
        # Generation moved and the fence neutralization reached the transport
        # after the fault (keyboard zero + mouse zero).
        assert manager.generation >= record.lease.generation + 1
        after = hid_writes()[before:]
        keyboard_zero = encode_hid_report(KeyboardReport(0, ()))
        mouse_zero = encode_hid_report(MouseRelReport(0, 0, 0, 0))
        assert keyboard_zero in after and mouse_zero in after
    finally:
        await manager.stop()
        actor.stop()


async def test_r03_click_release_lifts_its_own_button(harness: Harness):
    h = Harness()
    await h.make_healthy()
    agent, record = await h.acquire_agent()

    from superuse.core.models import MouseClick, MouseMove

    async def run_to_completion(job):
        """Advance virtual time in small steps, keeping the fake video fresh
        like a live stream would (a single big advance crosses freshness)."""
        for _ in range(60):
            await h.advance(0.05)
            h.manager.frame_received()
            if job.terminal:
                return
        h.manager.frame_received()

    job = await h.scheduler.submit_command(
        agent,
        CommandEnvelope(
            request_id=_rid(2),
            lease=record.lease,
            action=MouseClick(basis_id=h.basis_id, x=100, y=100, button="left"),
        ),
    )
    await run_to_completion(job)
    await h.scheduler.wait_job(job.job_id, 5.0)
    assert job.status.value == "written"

    hid = [w for w in h.actor.writes if w["session_id"] == agent.session_id]
    buttons = [w["report"].buttons for w in hid if hasattr(w["report"], "buttons")]
    # move(0) -> down(1) -> up(0); the release lifts the click's own button.
    assert buttons == [0, 1, 0]
    assert h.scheduler.owner_state(agent.session_id).buttons == 0

    # Double click repeats the press/release pair without stuck states.
    writes_before_job2 = len(h.actor.writes)  # index into the FULL write list
    job2 = await h.scheduler.submit_command(
        agent,
        CommandEnvelope(
            request_id=_rid(3),
            lease=record.lease,
            action=MouseClick(basis_id=h.basis_id, x=100, y=100, button="left", count=2),
        ),
    )
    await run_to_completion(job2)
    await h.scheduler.wait_job(job2.job_id, 5.0)
    buttons2 = [
        w["report"].buttons
        for w in h.actor.writes[writes_before_job2:]
        if w["session_id"] == agent.session_id and hasattr(w["report"], "buttons")
    ]
    assert buttons2 == [0, 1, 0, 1, 0]
    assert h.scheduler.owner_state(agent.session_id).buttons == 0

    # A later plain move does not drag: buttons stay 0.
    job3 = await h.scheduler.submit_command(
        agent,
        CommandEnvelope(
            request_id=_rid(4),
            lease=record.lease,
            action=MouseMove(basis_id=h.basis_id, x=200, y=200),
        ),
    )
    await run_to_completion(job3)
    await h.scheduler.wait_job(job3.job_id, 5.0)
    last_move = [
        w["report"]
        for w in h.actor.writes
        if w["session_id"] == agent.session_id and hasattr(w["report"], "buttons")
    ][-1]
    assert last_move.buttons == 0
    await h.teardown()


async def test_r03_drag_releases_to_base_buttons(harness: Harness):
    h = Harness()
    await h.make_healthy()
    agent, record = await h.acquire_agent()
    from superuse.core.models import MouseDrag

    job = await h.scheduler.submit_command(
        agent,
        CommandEnvelope(
            request_id=_rid(5),
            lease=record.lease,
            action=MouseDrag(
                basis_id=h.basis_id, from_x=10, from_y=10, to_x=500, to_y=400, duration_ms=100
            ),
        ),
    )
    for _ in range(40):
        await h.advance(0.05)
        h.manager.frame_received()
        if job.terminal:
            break
    await h.scheduler.wait_job(job.job_id, 5.0)
    reports = [
        w["report"]
        for w in h.actor.writes
        if w["session_id"] == agent.session_id and hasattr(w["report"], "buttons")
    ]
    pressed = [r.buttons for r in reports]
    assert all(b in (0, 1) for b in pressed)
    assert pressed[-1] == 0  # final release returns to the pre-drag state
    assert h.scheduler.owner_state(agent.session_id).buttons == 0
    await h.teardown()
