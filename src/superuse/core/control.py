"""Control manager: ownership modes, leases and release barriers.

Implements docs/development/control-state-machine.md. All timing is monotonic. State
mutations happen in synchronous blocks; the only awaits are barrier waits
whose completions are validated against the pending transition ticket, so a
stale fence result can never overwrite a newer takeover, fault or shutdown
(C07/C16). The manager holds no lock across serial I/O.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from superuse.core.clock import Clock
from superuse.core.errors import ControlError, ErrorCode
from superuse.core.models import (
    DeviceStatus,
    Lease,
    Mode,
    Owner,
    Session,
    SessionKind,
    State,
    StateEvent,
)
from superuse.core.ports import FenceResult, FenceTicket, LatchStore

logger = logging.getLogger(__name__)

EVENT_QUEUE_LIMIT = 16
DETAIL_MAX_CHARS = 120


def _bound_detail(detail: str | None) -> str | None:
    """Bound and de-sensitize device detail strings before they hit state.

    Enumeration errors quote identities (``sn=...``, device paths); those do
    not belong in status wires or logs, so only the leading token survives.
    """
    if detail is None:
        return None
    first = detail.split(" (")[0].split(" sn=")[0].split(" at path")[0]
    first = " ".join(first.split())
    return first[:DETAIL_MAX_CHARS] or None


@dataclass
class LeaseRecord:
    lease: Lease
    session: Session
    expires_at: float  # monotonic deadline

    def expires_in_ms(self, now: float) -> int:
        return max(1, int((self.expires_at - now) * 1000))


OwnerCancelHook = Callable[[str], Awaitable[None]]


class ControlManager:
    def __init__(
        self,
        *,
        clock: Clock,
        actor,  # ActorPort; typed loosely to avoid import cycle with device
        latch_store: LatchStore,
        source: str,
        lease_ttl_s: float = 15.0,
        fence_timeout_s: float = 2.0,
        freshness_s: float = 1.0,
        stall_timeout_s: float = 2.0,
    ) -> None:
        if source not in ("real", "simulated"):
            raise ValueError("source must be real or simulated")
        self.instance_id = str(uuid.uuid4())
        self._clock = clock
        self._actor = actor
        self._latch_store = latch_store
        self._source = source
        self._lease_ttl_s = lease_ttl_s
        self._fence_timeout_s = fence_timeout_s
        self._freshness_s = freshness_s
        self._stall_timeout_s = stall_timeout_s

        # The store returns None when no state file exists (first boot ->
        # false); a present-but-corrupt file is conservatively reported as
        # True by the store itself (docs/development/control-state-machine.md §6).
        self._latch = bool(latch_store.load())

        self._generation = 0
        self._revision = 0
        self._event_id = 0
        self._mode = Mode.RECOVERY
        self._owner: Owner | None = None
        self._lease: LeaseRecord | None = None
        self._serial_status = DeviceStatus.DISCONNECTED
        self._video_status = DeviceStatus.DISCONNECTED
        self._serial_detail: str | None = None
        self._video_detail: str | None = None
        self._last_frame: float | None = None
        self._pending_ticket: FenceTicket | None = None
        self._pending_future: asyncio.Future[FenceResult] | None = None
        self._subscribers: list[asyncio.Queue[StateEvent]] = []
        self._cancel_hook: OwnerCancelHook | None = None
        self._stopping = False

    # ------------------------------------------------------------------
    # Wiring and introspection
    # ------------------------------------------------------------------

    def set_owner_cancel_hook(self, hook: OwnerCancelHook) -> None:
        """App wires scheduler.cancel_owner_jobs here (avoids core cycle)."""
        self._cancel_hook = hook

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def human_latched(self) -> bool:
        return self._latch

    @property
    def current_lease(self) -> LeaseRecord | None:
        return self._lease

    @property
    def pending_ticket(self) -> FenceTicket | None:
        """Current in-flight barrier ticket (diagnostics/white-box tests)."""
        return self._pending_ticket

    def get_state(self) -> State:
        frame_age_ms = None
        if self._last_frame is not None:
            frame_age_ms = max(0.0, (self._clock.now - self._last_frame) * 1000)
        return State(
            instance_id=self.instance_id,
            state_revision=self._revision,
            generation=self._generation,
            mode=self._mode,
            owner=self._owner,
            human_latched=self._latch,
            source=self._source,
            serial_status=self._serial_status,
            video_status=self._video_status,
            frame_age_ms=frame_age_ms,
            serial_detail=self._serial_detail,
            video_detail=self._video_detail,
        )

    def subscribe(self) -> asyncio.Queue[StateEvent]:
        queue: asyncio.Queue[StateEvent] = asyncio.Queue(maxsize=EVENT_QUEUE_LIMIT)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[StateEvent]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def _publish(self, reason: str) -> None:
        self._revision += 1
        logger.info(
            "control mode=%s generation=%s reason=%s",
            self._mode.value,
            self._generation,
            reason[:80],
        )
        self._event_id += 1
        event = StateEvent(
            event_id=self._event_id,
            instance_id=self.instance_id,
            state_revision=self._revision,
            reason=reason[:80],
            state=self.get_state(),
        )
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow subscriber: drop the oldest event; consumers merge to
                # the latest full state anyway (docs/development/api-contract.md §4).
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    # ------------------------------------------------------------------
    # Health inputs (device layer pushes these)
    # ------------------------------------------------------------------

    def set_serial_status(self, status: DeviceStatus) -> None:
        self._serial_status = status
        if status in (DeviceStatus.FAULT, DeviceStatus.DISCONNECTED):
            self._enter_recovery(f"serial_{status.value}")

    def set_serial_detail(self, detail: str | None) -> None:
        """Why the serial device is not ready; None clears it (task T2)."""
        self._serial_detail = _bound_detail(detail)
        self._publish_if_detail_changed("serial_detail")

    def set_video_status(self, status: DeviceStatus) -> None:
        self._video_status = status
        if status is DeviceStatus.FAULT:
            self._enter_recovery("video_fault")

    def set_video_detail(self, detail: str | None) -> None:
        self._video_detail = _bound_detail(detail)
        self._publish_if_detail_changed("video_detail")

    def _publish_if_detail_changed(self, reason: str) -> None:
        """Detail changes are state revisions so subscribers re-render hints."""
        self._publish(reason)

    def frame_received(self) -> None:
        self._last_frame = self._clock.now

    def geometry_changed(self) -> None:
        """Resolution/source change: revoke and re-observe (docs §6)."""
        self._enter_recovery("geometry_changed")

    def _frame_age(self) -> float | None:
        if self._last_frame is None:
            return None
        return self._clock.now - self._last_frame

    def _video_fresh(self) -> bool:
        age = self._frame_age()
        return age is not None and age <= self._freshness_s

    def _healthy(self) -> None:
        if self._serial_status is not DeviceStatus.READY:
            raise ControlError(ErrorCode.DEVICE_UNAVAILABLE, "Serial device not ready")
        if not self._video_fresh():
            if self._last_frame is None:
                raise ControlError(ErrorCode.VIDEO_UNAVAILABLE, "No video frame yet")
            raise ControlError(ErrorCode.VIDEO_STALE, "Video frame older than freshness limit")

    # ------------------------------------------------------------------
    # Authorization for normal input (scheduler calls per step)
    # ------------------------------------------------------------------

    def authorize_input(
        self, session: Session, lease: Lease, *, require_fresh_video: bool = True
    ) -> None:
        """Validate that ``session`` may currently send input with ``lease``.

        Raises ControlError with the contract code otherwise. Diagnostics and
        owner-scoped release actions pass ``require_fresh_video=False``
        (docs/development/control-state-machine.md §1).
        """
        if self._stopping or self._mode is Mode.STOPPING:
            raise ControlError(ErrorCode.BROKER_UNAVAILABLE, "Broker is stopping")
        if lease.instance_id != self.instance_id:
            raise ControlError(
                ErrorCode.INSTANCE_CHANGED, "Lease belongs to another broker instance"
            )
        if self._mode is Mode.RECOVERY:
            raise ControlError(ErrorCode.RECOVERY_REQUIRED, "Device recovery in progress")
        if self._mode is Mode.TRANSITION:
            raise ControlError(ErrorCode.CONTROL_REVOKED, "Control transition in progress")
        if self._mode is Mode.HUMAN_HOLD:
            raise ControlError(
                ErrorCode.HUMAN_CONTROL, "Human latch active; takeover or handback required"
            )
        if self._mode is not Mode.AGENT and self._mode is not Mode.HUMAN:
            raise ControlError(ErrorCode.CONTROL_REQUIRED, "No active control owner")
        record = self._lease
        if record is None or self._owner is None or self._owner.session_id != session.session_id:
            raise ControlError(ErrorCode.CONTROL_REVOKED, "Session does not own the current lease")
        if record.lease.lease_id != lease.lease_id or record.lease.generation != lease.generation:
            raise ControlError(ErrorCode.CONTROL_REVOKED, "Lease revoked or superseded")
        if self._clock.now >= record.expires_at:
            raise ControlError(ErrorCode.LEASE_EXPIRED, "Control lease expired")
        if self._serial_status is not DeviceStatus.READY:
            raise ControlError(ErrorCode.DEVICE_UNAVAILABLE, "Serial device not ready")
        if require_fresh_video:
            if not self._video_fresh():
                age = self._frame_age()
                if age is None:
                    raise ControlError(ErrorCode.VIDEO_UNAVAILABLE, "No video frame yet")
                raise ControlError(ErrorCode.VIDEO_STALE, "Video frame too old for input")

    def authorize_generation(self, generation: int) -> None:
        """Cheap check used between report-level steps."""
        if self._stopping or self._mode is Mode.STOPPING:
            raise ControlError(ErrorCode.BROKER_UNAVAILABLE, "Broker is stopping")
        if generation != self._generation:
            raise ControlError(ErrorCode.CONTROL_REVOKED, "Generation superseded")

    # ------------------------------------------------------------------
    # Barrier machinery
    # ------------------------------------------------------------------

    def _begin_barrier(self, reason: str, *, latch: bool | None = None) -> FenceTicket:
        """Mark transition, revoke old authorization and bump generation.

        Synchronous — this is the revocation point (docs §4 step 1). The old
        owner's queued reports become stale for the actor; its long jobs are
        cancelled via the hook.
        """
        if latch is not None and latch != self._latch:
            self._latch = latch
            self._latch_store.save(latch)
        old_session = self._owner.session_id if self._owner else None
        self._owner = None
        self._lease = None
        self._generation += 1
        ticket = FenceTicket(generation=self._generation, transition_id=uuid.uuid4().hex)
        self._pending_ticket = ticket
        loop = asyncio.get_running_loop()
        self._pending_future = loop.create_future()
        self._mode = Mode.TRANSITION
        self._publish(reason)
        if old_session and self._cancel_hook is not None:
            asyncio.ensure_future(self._cancel_hook(old_session))
        return ticket

    def _supersede_pending(self) -> None:
        """Clear the pending barrier so its late completion is ignored."""
        ticket, future = self._pending_ticket, self._pending_future
        self._pending_ticket = None
        self._pending_future = None
        if ticket is not None and future is not None and not future.done():
            future.set_result(FenceResult(ok=False, error="SUPERSEDED"))

    def complete_barrier(self, ticket: FenceTicket, result: FenceResult) -> None:
        """Deliver a fence completion; stale tickets are dropped (C07/C16)."""
        if self._pending_ticket is None or ticket != self._pending_ticket:
            return
        future = self._pending_future
        if future is not None and not future.done():
            future.set_result(result)

    async def _await_barrier(self, ticket: FenceTicket, reason: str) -> FenceResult:
        # Run the actor fence; it resolves via complete_barrier (ticket-checked).
        asyncio.ensure_future(self._drive_fence(ticket))
        assert self._pending_future is not None
        result = await asyncio.shield(self._pending_future)
        if self._pending_ticket != ticket:
            raise ControlError(ErrorCode.CONTROL_REVOKED, f"Barrier {reason} superseded")
        # Consume the completed barrier: a later fault (video stall, geometry
        # change) must not mistake it for an in-flight transition and skip its
        # own generation bump + neutralization (docs/control-state-machine §6).
        self._pending_ticket = None
        self._pending_future = None
        return result

    async def _drive_fence(self, ticket: FenceTicket) -> None:
        try:
            result = await self._actor.fence(ticket, self._fence_timeout_s)
        except Exception as exc:  # actor contract violation; treat as failure
            logger.warning("fence raised: %r", exc)
            result = FenceResult(ok=False, error="WRITE_FAILED")
        self.complete_barrier(ticket, result)

    def _finish_barrier_failure(self, reason: str) -> None:
        self._supersede_pending()
        self._owner = None
        self._lease = None
        self._mode = Mode.RECOVERY
        self._publish(reason)

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    async def acquire(self, session: Session, ttl_ms: int | None = None) -> LeaseRecord:
        if session.kind is not SessionKind.AGENT:
            raise ControlError(ErrorCode.FORBIDDEN, "Only agent sessions can acquire")
        self._reject_stopping()
        if (
            self._mode is Mode.AGENT
            and self._lease
            and self._owner
            and self._owner.session_id == session.session_id
        ):
            if self._clock.now >= self._lease.expires_at:
                # expired: fall through to revocation + fresh acquire
                await self._expire_lease("agent_lease_expired")
            else:
                return self._lease  # same session re-acquire: keep lease & inputs (C01)
        if self._mode is Mode.RECOVERY:
            raise ControlError(ErrorCode.RECOVERY_REQUIRED, "Device recovery in progress")
        if self._mode is Mode.TRANSITION:
            raise ControlError(ErrorCode.CONTROL_BUSY, "Control transition in progress")
        if self._latch:
            raise ControlError(ErrorCode.HUMAN_CONTROL, "Human latch active")
        if self._mode is not Mode.IDLE:
            raise ControlError(ErrorCode.CONTROL_BUSY, "Control held by another owner")
        self._healthy()
        ttl_s = (ttl_ms / 1000) if ttl_ms else self._lease_ttl_s
        ticket = self._begin_barrier("acquire_barrier")
        result = await self._await_barrier(ticket, "acquire")
        if not result.ok:
            self._finish_barrier_failure("acquire_fence_failed")
            raise ControlError(
                ErrorCode.RECOVERY_REQUIRED, "Release barrier failed; recovery started"
            )
        self._grant(session, ttl_s)
        self._mode = Mode.AGENT
        self._publish("agent_acquired")
        return self._lease  # type: ignore[return-value]

    async def takeover(self, session: Session) -> LeaseRecord:
        if session.kind is not SessionKind.HUMAN:
            raise ControlError(ErrorCode.FORBIDDEN, "Only human sessions can take over")
        self._reject_stopping()
        if (
            self._mode is Mode.HUMAN
            and self._lease
            and self._owner
            and self._owner.session_id == session.session_id
            and self._clock.now < self._lease.expires_at
        ):
            return self._lease  # repeated takeover keeps lease, no re-fence (§2)
        if self._mode is Mode.RECOVERY or not self._device_ready():
            # Persist the latch first so recovery cannot hand control back to
            # an agent (docs/development/control-state-machine.md §3).
            if not self._latch:
                self._latch = True
                self._latch_store.save(True)
                self._publish("human_latch_set")
            raise ControlError(
                ErrorCode.RECOVERY_REQUIRED, "Device not healthy; takeover pending recovery"
            )
        if self._mode is Mode.TRANSITION:
            raise ControlError(ErrorCode.CONTROL_BUSY, "Control transition in progress")
        if self._mode is Mode.STOPPING:
            raise ControlError(ErrorCode.BROKER_UNAVAILABLE, "Broker is stopping")
        # idle / agent / human (other session) / human_hold: take over.
        ticket = self._begin_barrier("takeover_barrier", latch=True)
        result = await self._await_barrier(ticket, "takeover")
        if not result.ok:
            self._finish_barrier_failure("takeover_fence_failed")
            raise ControlError(
                ErrorCode.RECOVERY_REQUIRED, "Release barrier failed; recovery started"
            )
        self._grant(session, self._lease_ttl_s)
        self._mode = Mode.HUMAN
        self._publish("human_takeover")
        return self._lease  # type: ignore[return-value]

    async def release_control(self, session: Session, lease: Lease) -> State:
        self._reject_stopping()
        if not self._owns(session, lease):
            raise ControlError(ErrorCode.CONTROL_REVOKED, "Lease not current for this session")
        ticket = self._begin_barrier("release_barrier")
        result = await self._await_barrier(ticket, "release")
        if not result.ok:
            self._finish_barrier_failure("release_fence_failed")
            raise ControlError(
                ErrorCode.RECOVERY_REQUIRED, "Release barrier failed; recovery started"
            )
        self._mode = Mode.IDLE
        self._publish("control_released")
        return self.get_state()

    async def handback(self, session: Session) -> State:
        """Explicit human handback: clear latch, barrier, land in idle."""
        self._reject_stopping()
        allowed = (
            self._mode is Mode.HUMAN
            and self._owner is not None
            and self._owner.session_id == session.session_id
        ) or (self._mode is Mode.HUMAN_HOLD and session.kind is SessionKind.HUMAN)
        if not allowed:
            raise ControlError(ErrorCode.CONTROL_REQUIRED, "Session cannot hand back now")
        ticket = self._begin_barrier("handback_barrier", latch=False)
        result = await self._await_barrier(ticket, "handback")
        if not result.ok:
            self._finish_barrier_failure("handback_fence_failed")
            raise ControlError(
                ErrorCode.RECOVERY_REQUIRED, "Release barrier failed; recovery started"
            )
        self._mode = Mode.IDLE
        self._publish("human_handback")
        return self.get_state()

    async def human_suspend(
        self, session: Session, reason: str, *, expected_lease: Lease | None = None
    ) -> None:
        """Owner-scoped cleanup for blur/hidden/input-WS loss/lease expiry.

        Late calls from an old session must not touch the new owner (C06):
        they are ignored unless the session still owns the current lease.
        """
        if self._stopping or self._mode is Mode.STOPPING:
            return
        if expected_lease is not None and (
            self._lease is None or self._lease.lease != expected_lease
        ):
            return
        if (
            self._mode is not Mode.HUMAN
            or self._owner is None
            or self._owner.session_id != session.session_id
        ):
            return
        ticket = self._begin_barrier(f"{reason}_barrier")
        try:
            result = await self._await_barrier(ticket, reason)
        except ControlError:
            return  # superseded by fault/stop; state already reflects it
        if not result.ok:
            self._finish_barrier_failure(f"{reason}_fence_failed")
            return
        self._mode = Mode.HUMAN_HOLD  # latch retained
        self._publish(reason)

    async def session_closed(self, session: Session) -> None:
        """Session teardown: release agent owners; suspend human owners."""
        if self._stopping or self._mode is Mode.STOPPING:
            return
        if self._owner is None or self._owner.session_id != session.session_id:
            return
        if session.kind is SessionKind.HUMAN:
            await self.human_suspend(session, "human_session_closed")
        else:
            ticket = self._begin_barrier("agent_session_closed_barrier")
            try:
                result = await self._await_barrier(ticket, "agent_session_closed")
            except ControlError:
                return
            if not result.ok:
                self._finish_barrier_failure("agent_session_closed_fence_failed")
                return
            self._mode = Mode.IDLE
            self._publish("agent_session_closed")

    async def renew(self, session: Session, lease: Lease, ttl_ms: int | None = None) -> int:
        """Extend the current lease; never resurrects a revoked one (C10)."""
        if lease.instance_id != self.instance_id:
            raise ControlError(
                ErrorCode.INSTANCE_CHANGED, "Lease belongs to another broker instance"
            )
        record = self._lease
        if record is None or record.lease.lease_id != lease.lease_id:
            raise ControlError(ErrorCode.CONTROL_REVOKED, "Lease revoked or superseded")
        if self._owner is None or self._owner.session_id != session.session_id:
            raise ControlError(ErrorCode.CONTROL_REVOKED, "Session does not own this lease")
        if self._clock.now >= record.expires_at:
            raise ControlError(ErrorCode.LEASE_EXPIRED, "Lease already expired")
        ttl_s = (ttl_ms / 1000) if ttl_ms else self._lease_ttl_s
        record.expires_at = self._clock.now + ttl_s
        return record.expires_in_ms(self._clock.now)

    async def recover(self) -> None:
        """Complete recovery: healthy device + fresh frame + neutralize."""
        if self._stopping or self._mode is Mode.STOPPING:
            return
        if self._mode is not Mode.RECOVERY:
            return
        if not self._device_ready() or not self._video_fresh():
            raise ControlError(ErrorCode.RECOVERY_REQUIRED, "Device not healthy yet")
        ticket = self._begin_barrier("recovery_barrier")
        try:
            result = await self._await_barrier(ticket, "recover")
        except ControlError:
            return
        if not result.ok:
            self._finish_barrier_failure("recovery_fence_failed")
            return
        self._mode = Mode.HUMAN_HOLD if self._latch else Mode.IDLE
        self._publish("recovered")

    async def stop(self) -> None:
        if self._mode is Mode.STOPPING:
            return
        self._stopping = True
        self._supersede_pending()  # late fence results must not restore state (C16)
        old_session = self._owner.session_id if self._owner else None
        self._owner = None
        self._lease = None
        self._generation += 1
        self._mode = Mode.STOPPING
        self._publish("stopping")
        if old_session and self._cancel_hook is not None:
            asyncio.ensure_future(self._cancel_hook(old_session))
        # Best-effort neutralization; result cannot change STOPPING.
        ticket = FenceTicket(generation=self._generation, transition_id=uuid.uuid4().hex)
        try:
            await self._actor.fence(ticket, self._fence_timeout_s)
        except Exception as exc:
            logger.warning("stop fence failed: %r", exc)

    # ------------------------------------------------------------------
    # Periodic maintenance
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        now = self._clock.now
        if self._stopping or self._mode is Mode.STOPPING:
            return
        age = self._frame_age()
        if (
            age is not None
            and age > self._stall_timeout_s
            and self._mode not in (Mode.RECOVERY, Mode.TRANSITION)
            and self._video_status is not DeviceStatus.FAULT
        ):
            self._enter_recovery("video_stall")
            return
        if self._lease is not None and now >= self._lease.expires_at:
            owner_kind = self._lease.session.kind
            if owner_kind is SessionKind.HUMAN:
                await self.human_suspend(self._lease.session, "human_lease_expired")
            else:
                await self._expire_lease("agent_lease_expired")

    async def _expire_lease(self, reason: str) -> None:
        if self._mode is not Mode.AGENT:
            return
        ticket = self._begin_barrier(f"{reason}_barrier")
        try:
            result = await self._await_barrier(ticket, reason)
        except ControlError:
            return
        if not result.ok:
            self._finish_barrier_failure(f"{reason}_fence_failed")
            return
        self._mode = Mode.IDLE
        self._publish(reason)

    # ------------------------------------------------------------------
    # Fault entry
    # ------------------------------------------------------------------

    def _enter_recovery(self, reason: str) -> None:
        """Revoke and enter recovery; best-effort neutralization follows."""
        if self._stopping or self._mode is Mode.STOPPING:
            return
        if self._mode is Mode.RECOVERY:
            return
        was_pending = self._pending_ticket is not None
        self._supersede_pending()
        old_session = self._owner.session_id if self._owner else None
        self._owner = None
        self._lease = None
        self._mode = Mode.RECOVERY
        self._publish(reason)
        if old_session and self._cancel_hook is not None:
            asyncio.ensure_future(self._cancel_hook(old_session))
        if not was_pending:
            # Best-effort purge+neutralize without state claims. Only when no
            # barrier was in flight (that one already adopted a generation).
            self._generation += 1
            ticket = FenceTicket(generation=self._generation, transition_id=uuid.uuid4().hex)

            async def _neutralize_best_effort() -> None:
                try:
                    await self._actor.fence(ticket, self._fence_timeout_s)
                except Exception as exc:
                    logger.warning("best-effort neutralize failed: %r", exc)

            asyncio.ensure_future(_neutralize_best_effort())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _device_ready(self) -> bool:
        return self._serial_status is DeviceStatus.READY

    def _owns(self, session: Session, lease: Lease) -> bool:
        record = self._lease
        return (
            record is not None
            and record.lease.lease_id == lease.lease_id
            and self._owner is not None
            and self._owner.session_id == session.session_id
            and lease.instance_id == self.instance_id
        )

    def _grant(self, session: Session, ttl_s: float) -> LeaseRecord:
        lease = Lease(
            instance_id=self.instance_id,
            lease_id=str(uuid.uuid4()),
            generation=self._generation,
        )
        record = LeaseRecord(lease=lease, session=session, expires_at=self._clock.now + ttl_s)
        self._lease = record
        self._owner = Owner(kind=session.kind, session_id=session.session_id, label=session.label)
        return record

    def _reject_stopping(self) -> None:
        if self._stopping or self._mode is Mode.STOPPING:
            raise ControlError(ErrorCode.BROKER_UNAVAILABLE, "Broker is stopping")
