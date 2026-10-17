"""Fault-injection regressions for F01-F03; simulated devices only."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from superuse.app import Broker, DeviceSupervisor
from superuse.config import load_config
from superuse.core.clock import VirtualClock
from superuse.core.errors import ControlError
from superuse.core.models import DeviceStatus, Mode, Session, SessionKind
from superuse.device.capture import CaptureWorker
from superuse.device.protocol import encode_hid_report
from superuse.mcp.broker_client import BrokerClient


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("kind", [SessionKind.AGENT, SessionKind.HUMAN])
@pytest.mark.parametrize("running_job", [False, True])
async def test_disconnect_revokes_owner_and_neutralizes_new_actor(
    broker, monkeypatch, kind, running_job
):
    from superuse.core.models import CommandEnvelope, KeyboardReport, MouseRelReport, TypeText

    # Drive the supervisor explicitly so observations cannot race auto-recovery.
    broker._supervisor_task.cancel()
    await broker._supervisor_task
    monkeypatch.setattr(broker._supervisor, "BACKOFF_S", (0,))
    owner = Session(session_id=str(uuid.uuid4()), kind=kind, label="disconnect-owner")
    record = await (
        broker.manager.acquire(owner)
        if kind is SessionKind.AGENT
        else broker.manager.takeover(owner)
    )
    job = None
    if running_job:
        job = await broker.scheduler.submit_command(
            owner,
            CommandEnvelope(
                request_id=str(uuid.uuid4()),
                lease=record.lease,
                action=TypeText(text="x" * 100, char_interval_ms=50),
            ),
        )
        await until(lambda: job.reports_written >= 2)
    old = broker.actor
    old._transport.closed_mode = True
    old._transport.close()
    await until(lambda: broker.manager.get_state().serial_status is DeviceStatus.DISCONNECTED)
    assert broker.manager.mode is Mode.RECOVERY
    assert broker.manager.current_lease is None
    assert broker.manager.generation > record.lease.generation
    if job:
        await until(lambda: job.terminal)
        assert job.status.value in ("cancelled", "failed")

    await broker._supervisor._maybe_reconnect_serial()
    await until(lambda: broker.manager.get_state().serial_status is DeviceStatus.READY)
    assert broker.manager.mode is Mode.RECOVERY
    await broker._supervisor._maybe_recover()
    assert broker.manager.mode is (Mode.IDLE if kind is SessionKind.AGENT else Mode.HUMAN_HOLD)
    writes = [w for w in broker._serial_transport.writes if w[3] in (2, 4, 5)]
    assert writes == [
        encode_hid_report(KeyboardReport(0, ())),
        encode_hid_report(MouseRelReport(0, 0, 0, 0)),
    ]
    assert broker.actor._generation == broker.manager.generation
    with pytest.raises(ControlError):
        broker.manager.authorize_input(owner, record.lease)


@pytest.mark.parametrize("phase", ["before_accept", "before_response", "polling", "human_takeover"])
async def test_cancel_covers_entire_submission(broker, monkeypatch, phase):
    gate = asyncio.Event()
    reached = asyncio.Event()
    original_submit = broker.scheduler.submit_command

    async def delayed_submit(*args, **kwargs):
        if phase == "before_accept":
            reached.set()
            await gate.wait()
        job = await original_submit(*args, **kwargs)
        if phase != "before_accept":
            reached.set()
            if phase != "polling":
                await gate.wait()
        return job

    monkeypatch.setattr(broker.scheduler, "submit_command", delayed_submit)
    client = BrokerClient(runtime_dir=broker.runtime_dir, label="submission-cancel")
    await client.start()
    task = None
    try:
        await client.acquire()
        task = asyncio.create_task(
            client.command({"kind": "type_text", "text": "x" * 100, "char_interval_ms": 50})
        )
        await asyncio.wait_for(reached.wait(), timeout=3)
        job = None
        if phase != "before_accept":
            job = next(record.job for record in broker.scheduler._jobs.values())
            await until(lambda: job.reports_written >= 2)
        if phase == "polling":
            await until(lambda: bool(client._running_jobs))
        human = None
        if phase == "human_takeover":
            human = Session(session_id=str(uuid.uuid4()), kind=SessionKind.HUMAN, label="human")
            human_lease = await broker.manager.takeover(human)
            generation = broker.manager.generation
            writes_before = len(broker._serial_transport.writes)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client._running_jobs
        if job:
            await until(lambda: job.terminal)
            count = job.reports_written
            # No timer-based continuation can write after cancellation completes.
            await asyncio.sleep(0.12)
            assert job.reports_written == count
        if human:
            assert broker.manager.mode is Mode.HUMAN
            assert broker.manager.current_lease == human_lease
            assert broker.manager.generation == generation
            assert len(broker._serial_transport.writes) == writes_before
        elif phase != "polling":
            assert client.lease is None
            assert broker.manager.mode is Mode.IDLE
        if phase == "before_accept":
            gate.set()
            await asyncio.sleep(0.05)
            assert not broker.scheduler._jobs  # revoked submission cannot start late
    finally:
        gate.set()
        if task and not task.done():
            task.cancel()
        await client.close()


async def test_capture_retries_after_closed_source_and_long_absence(tmp_path, monkeypatch):
    broker = Broker(load_config(), runtime_dir=tmp_path / "runtime")
    clock = VirtualClock()
    broker.clock = clock
    supervisor = DeviceSupervisor(broker)
    old = CaptureWorker(backend="dshow", index=0, width=32, height=24, fps=10)
    old.status, old.fault_reason = "fault", "read_failed"
    broker.capture = old
    attempts = []
    available = False

    class Replacement:
        def start(self):
            self.started = True

    replacement = Replacement()

    def build():
        attempts.append(clock.now)
        if not available:
            raise RuntimeError("selected camera absent")
        return replacement

    monkeypatch.setattr(broker, "_build_capture", build)
    generation = broker.hub.capture_generation
    await supervisor._maybe_restart_capture()
    assert old.status == "disconnected"
    assert broker.hub.capture_generation == generation + 1
    assert not attempts
    # More than the former five-attempt cutoff, with the documented backoff.
    for delay in (1, 2, 4, 8, 10, 10):
        await clock.advance(delay)
        await supervisor._maybe_restart_capture()
    assert len(attempts) == 6
    assert attempts == [1, 3, 7, 15, 25, 35]
    available = True
    await clock.advance(10)
    await supervisor._maybe_restart_capture()
    assert broker.capture is replacement
    assert replacement.started
    assert not supervisor._capture_reconnect_pending


@pytest.mark.parametrize("close_raises", [False, True])
async def test_capture_hung_close_never_opens_second_source(tmp_path, monkeypatch, close_raises):
    broker = Broker(load_config(), runtime_dir=tmp_path / "runtime")
    broker.clock = VirtualClock()
    supervisor = DeviceSupervisor(broker)

    class HungCapture:
        status = "fault"
        fault_reason = "read_failed"

        def stop(self):
            if close_raises:
                raise RuntimeError("native close failed")
            self.fault_reason = "capture_close_hung"

    capture = HungCapture()
    broker.capture = capture
    monkeypatch.setattr(broker, "_build_capture", lambda: pytest.fail("second source opened"))
    await supervisor._maybe_restart_capture()
    await broker.clock.advance(100)
    await supervisor._maybe_restart_capture()
    assert broker.capture is capture


async def test_capture_restart_discards_old_callbacks_and_basis(broker):
    from superuse.device.capture import Frame

    old_generation = broker.hub.capture_generation
    frame = broker.hub.latest
    basis = broker.bases.register(
        old_generation, frame.geometry_id, 32, 24, frame.width, frame.height, broker.clock.now
    )
    broker.hub.begin_capture_generation()
    broker._on_capture_frame(
        old_generation,
        Frame(b"\0" * (32 * 24 * 3), "bgr", 32, 24, 99, broker.clock.now, Frame.now_iso()),
    )
    assert broker.hub.latest is None
    with pytest.raises(ControlError, match="STALE_BASIS|Unknown"):
        broker.bases.resolve(basis.basis_id)
