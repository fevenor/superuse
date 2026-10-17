"""Shared deterministic test doubles for the control core.

FakeSerialActor reproduces the real serial executor's contract precisely:
one writer, generation re-check at write time (TOCTOU gate), bounded input
queue, and fences that purge old-generation reports and send the two
neutralization reports before any new owner's input. Blocking is controlled
by an explicit gate plus the VirtualClock, so races are scheduled, not slept.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque

import pytest

from superuse.core.clock import VirtualClock
from superuse.core.control import ControlManager
from superuse.core.frames import BasisRegistry
from superuse.core.models import (
    DeviceStatus,
    KeyboardReport,
    Lease,
    MouseRelReport,
    Session,
    SessionKind,
)
from superuse.core.ports import AuthorizedReport, FenceResult, FenceTicket, WriteOutcome
from superuse.core.scheduler import InputScheduler
from superuse.device import protocol


class MemoryLatchStore:
    def __init__(self, initial: bool | None = None) -> None:
        self.value = initial
        self.saved: list[bool] = []

    def save(self, latched: bool) -> None:
        self.value = latched
        self.saved.append(latched)

    def load(self) -> bool | None:
        return self.value


class FakeSerialActor:
    """Single-writer actor double with gate-controlled write timing."""

    def __init__(self, clock: VirtualClock, *, queue_limit: int = 128) -> None:
        self._clock = clock
        self._queue_limit = queue_limit
        self._generation = 0
        self._queue: deque[tuple[AuthorizedReport, asyncio.Future]] = deque()
        self._gate = asyncio.Event()
        self._gate.set()  # open by default
        self._wakeup = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._fencing = False
        self._closed = False
        self._inflight: asyncio.Future | None = None
        self._task: asyncio.Task | None = None

        # Fault injection.
        self.fail_writes = False
        self.fence_neutral_fail = False

        # Records.
        self.writes: list[dict] = []
        self.revoked_reports = 0
        self.backpressured = 0
        self.fences: list[FenceTicket] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def close(self) -> None:
        self._closed = True
        self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def set_gate(self, open_: bool) -> None:
        if open_:
            self._gate.set()
        else:
            self._gate.clear()

    @property
    def gate_open(self) -> bool:
        return self._gate.is_set()

    # -- ActorPort -----------------------------------------------------------

    def submit(self, report: AuthorizedReport):
        loop = asyncio.get_running_loop()
        future: asyncio.Future[WriteOutcome] = loop.create_future()
        if not report.priority and len(self._queue) >= self._queue_limit:
            self.backpressured += 1
            future.set_result(WriteOutcome(0, "BACKPRESSURE"))
            return future
        self._queue.append((report, future))
        self._wakeup.set()
        return future

    async def fence(self, ticket: FenceTicket, timeout_s: float) -> FenceResult:
        self.fences.append(ticket)
        self._fencing = True  # writer pauses at the next loop top
        self._wakeup.set()
        if not await self._wait_event_or_timeout(self._idle, timeout_s):
            self._fencing = False
            self._wakeup.set()
            return FenceResult(ok=False, error="TIMEOUT")
        # Purge every queued report (all older than the new generation).
        while self._queue:
            report, future = self._queue.popleft()
            self.revoked_reports += 1
            if not future.done():
                future.set_result(WriteOutcome(0, "REVOKED"))
        self._generation = ticket.generation
        if not self.gate_open and not await self._wait_event_or_timeout(self._gate, timeout_s):
            self._fencing = False
            self._wakeup.set()
            return FenceResult(ok=False, error="TIMEOUT")
        self._fencing = False
        self._wakeup.set()
        if self.fence_neutral_fail:
            return FenceResult(ok=False, error="WRITE_FAILED")
        neutral = (KeyboardReport(0, ()), MouseRelReport(0, 0, 0, 0))
        for report in neutral:
            self._record(report, session_id="__fence__", generation=ticket.generation, neutral=True)
        return FenceResult(ok=True, neutral_reports=neutral)

    # -- internals -----------------------------------------------------------

    async def _wait_event_or_timeout(self, event: asyncio.Event, timeout_s: float) -> bool:
        if event.is_set():
            return True
        event_task = asyncio.ensure_future(event.wait())
        timer = self._clock.sleep(timeout_s)
        try:
            await asyncio.wait({event_task, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            event_task.cancel()
            timer.cancel()
        return event.is_set()

    def _record(
        self,
        report,
        *,
        session_id: str,
        generation: int,
        neutral: bool = False,
    ) -> WriteOutcome:
        data = protocol.encode_hid_report(report)
        self.writes.append(
            {
                "report": report,
                "data": data,
                "session_id": session_id,
                "generation": generation,
                "neutral": neutral,
            }
        )
        if self.fail_writes and not neutral:
            return WriteOutcome(0, "FAILED")
        return WriteOutcome(len(data), None)

    async def _loop(self) -> None:
        while not self._closed:
            if self._fencing or not self._queue:
                self._idle.set()
                await self._wakeup.wait()
                self._wakeup.clear()
                self._idle.clear()
                continue
            report, future = self._queue.popleft()
            self._inflight = future
            try:
                if report.generation < self._generation:
                    self.revoked_reports += 1
                    if not future.done():
                        future.set_result(WriteOutcome(0, "REVOKED"))
                else:
                    if not self.gate_open:
                        await self._gate.wait()
                    # Re-check after the gate: a fence may have bumped the
                    # generation while this write was blocked (C04).
                    if report.generation < self._generation:
                        self.revoked_reports += 1
                        if not future.done():
                            future.set_result(WriteOutcome(0, "REVOKED"))
                    else:
                        outcome = self._record(
                            report.report,
                            session_id=report.session_id,
                            generation=report.generation,
                        )
                        if not future.done():
                            future.set_result(outcome)
            finally:
                self._inflight = None
            await asyncio.sleep(0)


async def settle(rounds: int = 8) -> None:
    """Yield to the loop so queued writes and tasks progress deterministically."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class Harness:
    """One broker core (manager+scheduler+actor+bases) on a virtual clock."""

    def __init__(
        self,
        *,
        queue_limit: int = 128,
        action_timeout_s: float = 30.0,
        lease_ttl_s: float = 15.0,
        fence_timeout_s: float = 2.0,
        latch: MemoryLatchStore | None = None,
    ) -> None:
        self.clock = VirtualClock()
        self.actor = FakeSerialActor(self.clock, queue_limit=queue_limit)
        self.actor.start()
        self.latch = latch or MemoryLatchStore()
        self.manager = ControlManager(
            clock=self.clock,
            actor=self.actor,
            latch_store=self.latch,
            source="simulated",
            lease_ttl_s=lease_ttl_s,
            fence_timeout_s=fence_timeout_s,
        )
        self.bases = BasisRegistry(self.manager.instance_id)
        self.scheduler = InputScheduler(
            clock=self.clock,
            actor=self.actor,
            manager=self.manager,
            bases=self.bases,
            action_timeout_s=action_timeout_s,
            queue_limit=queue_limit,
        )
        self.manager.set_owner_cancel_hook(self.scheduler.cancel_owner_jobs)

        self.basis_id = self.bases.register(
            capture_generation=1,
            geometry_id=str(uuid.uuid4()),
            image_width=1280,
            image_height=720,
            native_width=1920,
            native_height=1080,
            now=0.0,
        ).basis_id

    # -- helpers -------------------------------------------------------------

    async def advance(self, duration: float, *, settle_rounds: int = 6) -> None:
        """Advance virtual time safely: let tasks register their timers first,
        advance, then let them observe the tick."""
        await settle(settle_rounds)
        await self.clock.advance(duration)
        await settle(settle_rounds)

    def session(self, kind: SessionKind, label: str, session_id: str | None = None) -> Session:
        return Session(session_id=session_id or str(uuid.uuid4()), kind=kind, label=label)

    def agent(self, label: str = "agent") -> Session:
        return self.session(SessionKind.AGENT, label)

    def human(self, label: str = "human") -> Session:
        return self.session(SessionKind.HUMAN, label)

    async def make_healthy(self) -> None:
        """Drive the manager out of initial recovery into idle."""
        self.manager.set_serial_status(DeviceStatus.READY)
        self.manager.set_video_status(DeviceStatus.READY)
        self.manager.frame_received()
        await self.clock.advance(0)
        await self.manager.recover()
        await settle()

    def lease_wire(self, record) -> Lease:
        return record.lease

    def input_writes(self, session_id: str) -> list[dict]:
        return [w for w in self.actor.writes if w["session_id"] == session_id and not w["neutral"]]

    def neutral_writes(self) -> list[dict]:
        return [w for w in self.actor.writes if w["neutral"]]

    async def acquire_agent(self, session: Session | None = None) -> tuple[Session, object]:
        session = session or self.agent()
        record = await self.manager.acquire(session)
        await settle()
        return session, record

    async def teardown(self) -> None:
        await self.actor.close()


@pytest.fixture
async def harness():
    h = Harness()
    await h.make_healthy()
    yield h
    await h.teardown()
