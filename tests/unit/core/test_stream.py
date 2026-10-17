"""Human input stream races C12(stream)/C17."""

from __future__ import annotations

from superuse.core.models import HumanInputEnvelope, KeyboardState, Mode, MouseState
from tests.unit.conftest import Harness, settle


def mouse_event(seq: int, lease, basis_id: str, x: float = 10, y: float = 10, buttons: int = 0):
    return HumanInputEnvelope(
        sequence=seq, lease=lease, action=MouseState(basis_id=basis_id, x=x, y=y, buttons=buttons)
    )


async def test_c17_stream_coalescing_sequence_and_bounds(harness: Harness):
    h = harness
    human = h.human()
    record = await h.manager.takeover(human)
    lease = record.lease
    acks: list = []
    stream = h.scheduler.attach_stream(human, acks.append)

    stream.handle(mouse_event(1, lease, h.basis_id))
    await settle()
    assert acks[-1].status == "written"

    # Burst without yielding: intermediate moves merge into the newest value,
    # so only the last of the burst ever hits the wire.
    stream.handle(mouse_event(2, lease, h.basis_id, x=100))
    stream.handle(mouse_event(3, lease, h.basis_id, x=200))
    stream.handle(mouse_event(4, lease, h.basis_id, x=300))
    await settle()
    by_seq = {a.sequence: a.status for a in acks}
    assert by_seq[2] == "coalesced"
    assert by_seq[3] == "coalesced"
    assert by_seq[4] == "written"
    written_moves = [
        w for w in h.actor.writes if w["session_id"] == human.session_id and w["report"].x
    ]
    assert len(written_moves) == 2  # seq 1 and seq 4 only

    # Duplicate sequence replays the cached ack without re-sending.
    count_before = len(h.actor.writes)
    stream.handle(mouse_event(2, lease, h.basis_id, x=999))
    await settle()
    assert len(h.actor.writes) == count_before
    seq2_acks = [a for a in acks if a.sequence == 2]
    assert len(seq2_acks) == 2 and seq2_acks[0].status == seq2_acks[1].status == "coalesced"

    # Gap: rejected with SEQUENCE_OUT_OF_ORDER and does not consume.
    stream.handle(mouse_event(9, lease, h.basis_id))
    assert acks[-1].status == "rejected"
    assert acks[-1].error["code"] == "SEQUENCE_OUT_OF_ORDER"
    stream.handle(mouse_event(5, lease, h.basis_id))
    await settle()
    assert acks[-1].status == "written"

    # Stream events never occupy the persistent job table.
    assert len(h.scheduler._jobs) == 0
    # Ack cache stays bounded.
    for seq in range(6, 80):
        stream.handle(mouse_event(seq, lease, h.basis_id))
        await settle()
    assert len(stream._acks) == 64


async def test_c12_stream_overflow_fences_owner_for_release_critical(harness: Harness):
    h = harness
    human = h.human()
    record = await h.manager.takeover(human)
    lease = record.lease
    acks: list = []
    stream = h.scheduler.attach_stream(human, acks.append)

    h.actor.set_gate(False)  # execution stalls; the queue then fills up
    stream.handle(mouse_event(1, lease, h.basis_id, buttons=1))  # non-coalescible
    await settle()
    # Button-changing events can never coalesce, so they fill the queue.
    for seq in range(2, 129):
        stream.handle(mouse_event(seq, lease, h.basis_id, x=seq, buttons=seq % 2))
    assert len(stream._pending) == 128
    overflow = mouse_event(129, lease, h.basis_id, buttons=1)  # non-coalescible
    stream.handle(overflow)
    assert acks[-1].status == "rejected"
    assert acks[-1].error["code"] == "INPUT_BACKPRESSURE"

    # A release-critical keyboard event cannot be dropped: owner fence.
    key_up = HumanInputEnvelope(
        sequence=130,
        lease=lease,
        action=KeyboardState(keys=(), modifiers=()),
    )
    stream.handle(key_up)
    assert acks[-1].error["code"] == "INPUT_BACKPRESSURE"

    h.actor.set_gate(True)  # let the suspend barrier complete
    await h.advance(0.1)
    await settle()
    assert h.manager.mode is Mode.HUMAN_HOLD  # controlled release, latch kept
    assert h.manager.get_state().human_latched is True
    # No keyboard state was silently swallowed after the fence.
    key_writes = [w for w in h.actor.writes if w["session_id"] == human.session_id]
    assert all(w["generation"] <= h.manager.generation for w in key_writes)


async def test_stream_events_from_non_owner_rejected(harness: Harness):
    h = harness
    human = h.human()
    record = await h.manager.takeover(human)
    other = h.human("other-page")
    acks: list = []
    stream = h.scheduler.attach_stream(other, acks.append)
    stream.handle(mouse_event(1, record.lease, h.basis_id))
    await settle()
    assert acks[-1].status == "rejected"
    assert acks[-1].error["code"] == "CONTROL_REVOKED"
    assert h.input_writes(other.session_id) == []


async def test_absolute_coalescing_never_replaces_the_report_being_written(harness: Harness):
    h = harness
    human = h.human()
    lease = (await h.manager.takeover(human)).lease
    acks = []
    stream = h.scheduler.attach_stream(human, acks.append)
    h.actor.set_gate(False)
    stream.handle(mouse_event(1, lease, h.basis_id, x=10))
    await settle()  # first report is waiting for the serial writer
    stream.handle(mouse_event(2, lease, h.basis_id, x=100))
    stream.handle(mouse_event(3, lease, h.basis_id, x=300))
    h.actor.set_gate(True)
    await settle()
    terminal = [(ack.sequence, ack.status) for ack in acks]
    assert terminal == [(2, "coalesced"), (1, "written"), (3, "written")]
    assert len(h.input_writes(human.session_id)) == 2
