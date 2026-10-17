"""Input scheduler: bounded report-level action execution.

Expands contract actions into report-level steps (docs/development/control-state-machine.md
§5): every step re-validates session/lease/generation/deadline/health/basis
before the write, waits use cancellable timers, and a takeover cancels the
remainder while keeping completed counts. Hosts the human input stream
(sequence bookkeeping, coalescing, bounded acks); stream events never create
persistent Job records (C17).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from superuse.core.clock import Clock
from superuse.core.control import ControlManager
from superuse.core.errors import ControlError, ErrorCode
from superuse.core.frames import BasisRegistry, map_point
from superuse.core.models import (
    HUMAN_ONLY_ACTIONS,
    CommandEnvelope,
    DeviceStatus,
    HumanInputEnvelope,
    InputAck,
    InputReport,
    Job,
    JobStatus,
    KeyboardReport,
    KeyboardState,
    MouseAbsReport,
    MouseClick,
    MouseClickRelative,
    MouseDrag,
    MouseDragRelative,
    MouseMove,
    MouseMoveRelative,
    MouseRelativeState,
    MouseRelReport,
    MouseScroll,
    MouseScrollRelative,
    MouseState,
    PressKey,
    ReleaseInputs,
    Session,
    SessionKind,
    TypeText,
)
from superuse.core.ports import ActorPort, AuthorizedReport
from superuse.device import keymap

logger = logging.getLogger(__name__)

BUTTON_BITS = {"left": 1, "right": 2, "middle": 4}
STREAM_QUEUE_LIMIT = 128
STREAM_ACK_CACHE = 64

# Authorization failures that mean "this owner lost control mid-action":
# the job reports cancelled (control change), not failed.
_SUPERSEDED_CODES = frozenset(
    {
        ErrorCode.CONTROL_REVOKED,
        ErrorCode.LEASE_EXPIRED,
        ErrorCode.CONTROL_BUSY,
        ErrorCode.HUMAN_CONTROL,
        ErrorCode.RECOVERY_REQUIRED,
        ErrorCode.BROKER_UNAVAILABLE,
    }
)


def _body_hash(envelope: CommandEnvelope) -> str:
    payload = {
        "request_id": envelope.request_id,
        "lease": [envelope.lease.instance_id, envelope.lease.lease_id, envelope.lease.generation],
        "action": _action_wire(envelope.action),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _action_wire(action: Any) -> dict:
    kind = action.kind
    if isinstance(
        action,
        (
            MouseMoveRelative,
            MouseClickRelative,
            MouseDragRelative,
            MouseScrollRelative,
            MouseRelativeState,
        ),
    ):
        return asdict(action)
    if kind == "press_key":
        return {
            "kind": kind,
            "code": action.code,
            "modifiers": list(action.modifiers),
            "hold_ms": action.hold_ms,
        }
    if kind == "type_text":
        return {"kind": kind, "text": action.text, "char_interval_ms": action.char_interval_ms}
    if kind == "mouse_move":
        return {"kind": kind, "basis_id": action.basis_id, "x": action.x, "y": action.y}
    if kind == "mouse_click":
        return {
            "kind": kind,
            "basis_id": action.basis_id,
            "x": action.x,
            "y": action.y,
            "button": action.button,
            "count": action.count,
        }
    if kind == "mouse_drag":
        return {
            "kind": kind,
            "basis_id": action.basis_id,
            "from_x": action.from_x,
            "from_y": action.from_y,
            "to_x": action.to_x,
            "to_y": action.to_y,
            "button": action.button,
            "duration_ms": action.duration_ms,
        }
    if kind == "mouse_scroll":
        return {
            "kind": kind,
            "basis_id": action.basis_id,
            "x": action.x,
            "y": action.y,
            "steps": action.steps,
        }
    if kind == "keyboard_state":
        return {"kind": kind, "keys": list(action.keys), "modifiers": list(action.modifiers)}
    if kind == "mouse_state":
        return {
            "kind": kind,
            "basis_id": action.basis_id,
            "x": action.x,
            "y": action.y,
            "buttons": action.buttons,
        }
    return {"kind": kind}


@dataclass
class OwnerInputState:
    """Server-side mirror of what the device currently holds for the owner."""

    session_id: str
    keys: frozenset[int] = frozenset()
    modifier: int = 0
    buttons: int = 0
    hid_x: int = 2048
    hid_y: int = 2048
    relative_mouse: bool = False

    def apply(self, report: InputReport) -> None:
        if isinstance(report, KeyboardReport):
            self.modifier = report.modifier
            self.keys = frozenset(k for k in report.keys if k)
        elif isinstance(report, MouseAbsReport):
            self.buttons = report.buttons
            self.hid_x = report.x
            self.hid_y = report.y
            self.relative_mouse = False
        elif isinstance(report, MouseRelReport):
            self.buttons = report.buttons
            self.relative_mouse = True

    def mouse_release(self) -> InputReport:
        if self.relative_mouse:
            return MouseRelReport(0, 0, 0, 0)
        return MouseAbsReport(0, self.hid_x, self.hid_y, 0)


@dataclass
class Step:
    make_report: Callable[[float], InputReport]  # now -> report; may raise ControlError
    wait_s: float = 0.0
    priority: bool = False
    fresh_video: bool = True


@dataclass
class JobRecord:
    job: Job
    session: Session
    lease_id: str
    cancel_event: asyncio.Event
    done: asyncio.Event = field(default_factory=asyncio.Event)
    created_monotonic: float = 0.0


class IdempotencyCache:
    """(session, request_id) -> stored response for the dedup window."""

    def __init__(self, *, capacity: int = 1024, ttl_s: float = 120.0) -> None:
        self._capacity = capacity
        self._ttl_s = ttl_s
        # (session_id, request_id) -> (body_hash, response, stored_at)
        self._entries: OrderedDict[tuple[str, str], tuple[str, Any, float]] = OrderedDict()

    @property
    def full(self) -> bool:
        return len(self._entries) >= self._capacity

    def sweep(self, now: float) -> None:
        expired = [key for key, (_, _, at) in self._entries.items() if (now - at) > self._ttl_s]
        for key in expired:
            del self._entries[key]

    def lookup(self, session_id: str, request_id: str, body: str, now: float) -> Any | None:
        entry = self._entries.get((session_id, request_id))
        if entry is None:
            return None
        stored_hash, response, stored_at = entry
        if (now - stored_at) > self._ttl_s:
            del self._entries[(session_id, request_id)]
            return None
        if stored_hash != body:
            raise ControlError(
                ErrorCode.REQUEST_ID_CONFLICT, "request_id reused with a different body"
            )
        self._entries.move_to_end((session_id, request_id))
        return response

    def store(self, session_id: str, request_id: str, body: str, response: Any, now: float) -> None:
        self._entries[(session_id, request_id)] = (body, response, now)
        self._entries.move_to_end((session_id, request_id))

    def __len__(self) -> int:
        return len(self._entries)


class InputScheduler:
    def __init__(
        self,
        *,
        clock: Clock,
        actor: ActorPort,
        manager: ControlManager,
        bases: BasisRegistry,
        action_timeout_s: float = 30.0,
        queue_limit: int = 128,
        job_capacity: int = 1024,
        job_ttl_s: float = 120.0,
    ) -> None:
        self._clock = clock
        self._actor = actor
        self._manager = manager
        self._bases = bases
        self._action_timeout_s = action_timeout_s
        self._queue_limit = queue_limit
        self._job_capacity = job_capacity
        self._job_ttl_s = job_ttl_s
        self._jobs: OrderedDict[str, JobRecord] = OrderedDict()
        self._idem = IdempotencyCache(capacity=job_capacity, ttl_s=job_ttl_s)
        self._owner_states: dict[str, OwnerInputState] = {}
        self._inflight_reports = 0
        self._streams: dict[str, HumanInputStream] = {}
        self._closing = False

    # ------------------------------------------------------------------
    # Command jobs (HTTP /v1/commands and MCP input tools)
    # ------------------------------------------------------------------

    async def submit_command(self, session: Session, envelope: CommandEnvelope) -> Job:
        """Validate and enqueue a command; returns the accepted Job.

        Semantic failures raise before the first packet is scheduled (C15);
        accepted commands replay the same result object for the same
        (session, request_id) within the window (C14).
        """
        if self._closing:
            raise ControlError(ErrorCode.BROKER_UNAVAILABLE, "Scheduler is shutting down")
        now = self._clock.now
        body = _body_hash(envelope)
        replay = self._idem.lookup(session.session_id, envelope.request_id, body, now)
        if replay is not None:
            if isinstance(replay, Job):
                return replay
            raise ControlError(replay["code"], replay["message"])

        action = envelope.action
        if action.kind in HUMAN_ONLY_ACTIONS and session.kind is not SessionKind.HUMAN:
            raise ControlError(
                ErrorCode.FORBIDDEN, "Persistent keyboard/mouse states are human-only actions"
            )
        for existing in self._jobs.values():
            if existing.job.session_id == session.session_id and not existing.job.terminal:
                raise ControlError(ErrorCode.CONTROL_BUSY, "One active action job per owner")
        self._validate_action(action)
        self._manager.authorize_input(session, envelope.lease)

        self._sweep(now)
        if len(self._jobs) >= self._job_capacity or self._idem.full:
            raise ControlError(ErrorCode.RATE_LIMITED, "Job/result capacity full; retry later")

        job = Job(
            job_id=str(uuid.uuid4()),
            request_id=envelope.request_id,
            session_id=session.session_id,
            status=JobStatus.ACCEPTED,
        )
        record = JobRecord(
            job=job,
            session=session,
            lease_id=envelope.lease.lease_id,
            cancel_event=asyncio.Event(),
            created_monotonic=now,
        )
        self._jobs[job.job_id] = record
        self._owner_states.setdefault(
            session.session_id, OwnerInputState(session_id=session.session_id)
        )
        self._idem.store(session.session_id, envelope.request_id, body, job, now)
        asyncio.ensure_future(self._run_job(record, envelope))
        return job

    def get_job(self, session: Session, job_id: str) -> Job:
        record = self._jobs.get(job_id)
        if record is None or record.job.session_id != session.session_id:
            raise ControlError(ErrorCode.NOT_FOUND, "Unknown job for this session")
        return record.job

    async def wait_job(self, job_id: str, timeout_s: float) -> Job:
        record = self._jobs.get(job_id)
        if record is None:
            raise ControlError(ErrorCode.NOT_FOUND, "Unknown job")
        try:
            await asyncio.wait_for(record.done.wait(), timeout_s)
        except TimeoutError:
            pass
        return record.job

    async def cancel_job(self, session: Session, job_id: str) -> Job:
        record = self._jobs.get(job_id)
        if record is None or record.job.session_id != session.session_id:
            raise ControlError(ErrorCode.NOT_FOUND, "Unknown job for this session")
        if not record.job.terminal:
            record.cancel_event.set()
            try:
                await asyncio.wait_for(record.done.wait(), timeout=5.0)
            except TimeoutError:
                pass
        return record.job

    async def cancel_owner_jobs(self, session_id: str) -> None:
        """Manager hook: stop everything this session still has in flight."""
        for record in self._jobs.values():
            if record.job.session_id == session_id and not record.job.terminal:
                record.cancel_event.set()
        stream = self._streams.get(session_id)
        if stream is not None:
            stream.discard_pending()
        self._owner_states.pop(session_id, None)

    def owner_state(self, session_id: str) -> OwnerInputState | None:
        return self._owner_states.get(session_id)

    def _drop_owner_state(self, session_id: str) -> None:
        self._owner_states.pop(session_id, None)

    async def close(self) -> None:
        self._closing = True
        for record in self._jobs.values():
            if not record.job.terminal:
                record.cancel_event.set()
        for stream in self._streams.values():
            stream.discard_pending()

    # ------------------------------------------------------------------
    # Validation and expansion
    # ------------------------------------------------------------------

    def _validate_action(self, action: Any) -> None:
        if isinstance(action, PressKey):
            self._check_code(action.code)
        elif isinstance(action, TypeText):
            text = keymap.normalize_text(action.text)
            for ch in text:
                if keymap.char_to_hid(ch) is None:
                    raise ControlError(
                        ErrorCode.UNSUPPORTED_TEXT, f"Character {ch!r} cannot be typed"
                    )
            if len(text) * action.char_interval_ms / 1000 > self._action_timeout_s:
                raise ControlError(
                    ErrorCode.ACTION_TIMEOUT, "type_text estimated duration exceeds action deadline"
                )
        elif isinstance(action, KeyboardState):
            if len(action.keys) > 6:
                raise ControlError(ErrorCode.INPUT_LIMIT, "At most 6 non-modifier keys")
            for code in action.keys:
                self._check_code(code)
        elif isinstance(action, (MouseMove, MouseClick, MouseDrag, MouseScroll, MouseState)):
            # Resolvable now (fail before first packet) and re-checked per step.
            self._bases.resolve(action.basis_id, self._clock.now)

    @staticmethod
    def _check_code(code: str) -> None:
        if keymap.is_modifier_code(code):
            raise ControlError(ErrorCode.UNSUPPORTED_KEY, f"{code}: pass modifiers explicitly")
        if keymap.hid_for_code(code) is None:
            raise ControlError(ErrorCode.UNSUPPORTED_KEY, f"Unknown key code {code}")

    def _expand(self, action: Any, state: OwnerInputState) -> list[Step]:
        """Action -> report-level steps; button bits read from live state."""
        if isinstance(action, MouseMoveRelative):
            return self._relative_steps(action.dx, action.dy, state.buttons)
        if isinstance(action, MouseClickRelative):
            base = state.buttons
            down = base | BUTTON_BITS[action.button]
            return [
                step
                for _ in range(action.count)
                for step in (
                    Step(lambda now: MouseRelReport(down, 0, 0, 0), wait_s=0.05),
                    Step(lambda now: MouseRelReport(base, 0, 0, 0), wait_s=0.05, priority=True),
                )
            ]
        if isinstance(action, MouseDragRelative):
            base = state.buttons
            pressed = base | BUTTON_BITS[action.button]
            return (
                [Step(lambda now: MouseRelReport(pressed, 0, 0, 0))]
                + self._relative_steps(action.dx, action.dy, pressed, action.duration_ms)
                + [Step(lambda now: MouseRelReport(base, 0, 0, 0), priority=True)]
            )
        if isinstance(action, MouseScrollRelative):
            return [Step(lambda now: MouseRelReport(state.buttons, 0, 0, action.steps))]
        if isinstance(action, MouseRelativeState):
            return [
                Step(lambda now: MouseRelReport(action.buttons, action.dx, action.dy, action.wheel))
            ]
        if isinstance(action, PressKey):
            modifier = keymap.modifier_bits(list(action.modifiers))
            code = keymap.hid_for_code(action.code)
            assert code is not None
            return [
                Step(
                    lambda now, m=modifier, c=code: KeyboardReport(m, (c,)),
                    wait_s=action.hold_ms / 1000,
                ),
                Step(lambda now: KeyboardReport(0, ()), priority=True),
            ]
        if isinstance(action, TypeText):
            steps: list[Step] = []
            interval = action.char_interval_ms / 1000
            for ch in keymap.normalize_text(action.text):
                mod, code = keymap.char_to_hid(ch)
                steps.append(
                    Step(lambda now, m=mod, c=code: KeyboardReport(m, (c,)), wait_s=interval)
                )
                steps.append(Step(lambda now: KeyboardReport(0, ())))
            return steps
        if isinstance(action, MouseMove):
            return [self._move_step(state, action.basis_id, action.x, action.y)]
        if isinstance(action, MouseClick):
            bit = BUTTON_BITS[action.button]
            # Snapshot the button state *before* this action: the down report
            # applies it (state.apply mutates state.buttons mid-job), so the
            # release must lift only this click's button back to the base,
            # not replay the freshly pressed bit (docs/api-contract §6).
            base_buttons = state.buttons
            steps = [self._move_step(state, action.basis_id, action.x, action.y)]
            for _ in range(action.count):
                steps.append(
                    Step(
                        lambda now, b=bit, a=action, base=base_buttons: self._abs(
                            a.basis_id, a.x, a.y, base | b, now
                        ),
                        wait_s=0.03,
                    )
                )
                steps.append(
                    Step(
                        lambda now, a=action, base=base_buttons: self._abs(
                            a.basis_id, a.x, a.y, base, now
                        )
                    )
                )
            return steps
        if isinstance(action, MouseDrag):
            return self._expand_drag(action, state)
        if isinstance(action, MouseScroll):
            return [
                Step(
                    lambda now, a=action: self._abs(
                        a.basis_id, a.x, a.y, state.buttons, now, wheel=a.steps
                    )
                )
            ]
        if isinstance(action, KeyboardState):
            modifier = keymap.modifier_bits(list(action.modifiers))
            keys = tuple(keymap.hid_for_code(c) for c in action.keys)
            return [Step(lambda now, m=modifier, k=keys: KeyboardReport(m, k))]
        if isinstance(action, MouseState):
            return [Step(lambda now, a=action: self._abs(a.basis_id, a.x, a.y, a.buttons, now))]
        if isinstance(action, ReleaseInputs):
            return [
                Step(lambda now: KeyboardReport(0, ()), priority=True, fresh_video=False),
                Step(
                    lambda now: state.mouse_release(),
                    priority=True,
                    fresh_video=False,
                ),
            ]
        raise ControlError(ErrorCode.INVALID_REQUEST, f"Cannot expand {action.kind}")

    @staticmethod
    def _relative_steps(dx: int, dy: int, buttons: int, duration_ms: int = 0) -> list[Step]:
        # Counts, not pixels: preserve exact totals while bounding every HID
        # report and allowing revocation/cancellation between reports.
        segments = max(1, math.ceil(abs(dx) / 127), math.ceil(abs(dy) / 127), duration_ms // 25)
        steps = []
        previous_x = previous_y = 0
        for i in range(1, segments + 1):
            x, y = round(dx * i / segments), round(dy * i / segments)
            report = MouseRelReport(buttons, x - previous_x, y - previous_y, 0)
            steps.append(
                Step(
                    lambda now, report=report: report,
                    wait_s=(duration_ms / 1000 / segments if duration_ms else 0.01),
                )
            )
            previous_x, previous_y = x, y
        return steps

    def _abs(
        self, basis_id: str, x: float, y: float, buttons: int, now: float, wheel: int = 0
    ) -> MouseAbsReport:
        basis = self._bases.resolve(basis_id, now)
        hid_x, hid_y = map_point(basis, x, y)
        return MouseAbsReport(buttons, hid_x, hid_y, wheel)

    def _move_step(self, state: OwnerInputState, basis_id: str, x: float, y: float) -> Step:
        return Step(lambda now: self._abs(basis_id, x, y, state.buttons, now))

    def _expand_drag(self, action: MouseDrag, state: OwnerInputState) -> list[Step]:
        bit = BUTTON_BITS[action.button]
        base_buttons = state.buttons  # snapshot before the drag presses its button
        pressed = base_buttons | bit
        duration_s = action.duration_ms / 1000
        segments = max(2, min(int(action.duration_ms / 25), 200))
        wait = duration_s / segments

        def lerp(a: float, b: float, t: float) -> float:
            return a + (b - a) * t

        steps = [self._move_step(state, action.basis_id, action.from_x, action.from_y)]
        steps.append(
            Step(lambda now: self._abs(action.basis_id, action.from_x, action.from_y, pressed, now))
        )
        for i in range(1, segments + 1):
            t = i / segments
            steps.append(
                Step(
                    lambda now, t=t: self._abs(
                        action.basis_id,
                        lerp(action.from_x, action.to_x, t),
                        lerp(action.from_y, action.to_y, t),
                        pressed,
                        now,
                    ),
                    wait_s=wait,
                )
            )
        steps.append(
            Step(
                lambda now: self._abs(action.basis_id, action.to_x, action.to_y, base_buttons, now),
                priority=True,
            )
        )
        return steps

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    async def _run_job(self, record: JobRecord, envelope: CommandEnvelope) -> None:
        job = record.job
        job.status = JobStatus.RUNNING
        lease = envelope.lease
        session = record.session
        state = self._owner_states.setdefault(
            session.session_id, OwnerInputState(session_id=session.session_id)
        )
        deadline = self._clock.now + self._action_timeout_s
        steps = self._expand(envelope.action, state)
        failure: ControlError | None = None
        superseded = False
        try:
            for step in steps:
                if record.cancel_event.is_set():
                    superseded = True
                    break
                if self._clock.now >= deadline:
                    failure = ControlError(ErrorCode.ACTION_TIMEOUT, "Action deadline exceeded")
                    break
                try:
                    self._manager.authorize_input(
                        session, lease, require_fresh_video=step.fresh_video
                    )
                    report = step.make_report(self._clock.now)
                except ControlError as exc:
                    if exc.code in _SUPERSEDED_CODES:
                        superseded = True
                    failure = exc
                    break
                if not step.priority and self._inflight_reports >= self._queue_limit:
                    failure = ControlError(
                        ErrorCode.INPUT_BACKPRESSURE, "Input queue full; report not scheduled"
                    )
                    break
                try:
                    await self._write(session, lease, report, step.priority)
                except ControlError as exc:
                    if exc.code in _SUPERSEDED_CODES:
                        superseded = True
                    failure = exc
                    break
                job.reports_written += 1
                job.steps_completed += 1
                state.apply(report)
                if step.wait_s > 0 and await self._interruptible_wait(
                    step.wait_s, record.cancel_event
                ):
                    superseded = True
                    break
            else:
                job.status = JobStatus.WRITTEN
                job.partial = False
        except ControlError as exc:  # defensive: expansion/authorization paths
            failure = exc
        except Exception:
            logger.exception("job execution crashed")
            failure = ControlError(ErrorCode.RESULT_UNKNOWN, "Action crashed internally")
        finally:
            self._finish_job(job, failure, superseded, session, lease)
            record.done.set()

    def _finish_job(
        self,
        job: Job,
        failure: ControlError | None,
        superseded: bool,
        session: Session,
        lease,
    ) -> None:
        if job.status is JobStatus.WRITTEN:
            return
        if superseded and failure is None:
            job.status = JobStatus.CANCELLED
            job.error = ControlError(
                ErrorCode.ACTION_CANCELLED, "Action cancelled before completion"
            ).to_wire()
        elif superseded:
            job.status = JobStatus.CANCELLED
            job.error = failure.to_wire()
        elif failure is not None:
            job.status = JobStatus.FAILED
            job.error = failure.to_wire()
        else:  # pragma: no cover - defensive
            job.status = JobStatus.FAILED
            job.error = ControlError(
                ErrorCode.RESULT_UNKNOWN, "Action ended unexpectedly"
            ).to_wire()
        job.partial = job.reports_written > 0 or job.steps_completed > 0
        if job.error is not None and job.error["code"] in (
            "ACTION_TIMEOUT",
            "INPUT_BACKPRESSURE",
            "ACTION_CANCELLED",
        ):
            # Neutralize still-valid owners after cancellations/timeouts so no
            # key stays held (docs §5); fence covers owner changes already.
            asyncio.ensure_future(self._neutralize_owner(session, lease))

    async def _neutralize_owner(self, session: Session, lease) -> None:
        try:
            self._manager.authorize_input(session, lease, require_fresh_video=False)
            state = self._owner_states.get(session.session_id)
            mouse = state.mouse_release() if state else MouseRelReport(0, 0, 0, 0)
            await self._write(session, lease, KeyboardReport(0, ()), priority=True)
            await self._write(session, lease, mouse, priority=True)
            if state is not None:
                state.apply(KeyboardReport(0, ()))
                state.apply(mouse)
        except Exception as exc:  # owner changed or device gone: fence handles it
            logger.debug("owner neutralization skipped: %r", exc)

    async def _write(self, session: Session, lease, report: InputReport, priority: bool):
        authorized = AuthorizedReport(
            session_id=session.session_id,
            lease_id=lease.lease_id,
            generation=lease.generation,
            report=report,
            priority=priority,
        )
        if priority:
            future = self._actor.submit(authorized)
        else:
            self._inflight_reports += 1
            try:
                future = self._actor.submit(authorized)
            except BaseException:
                self._inflight_reports -= 1
                raise
        try:
            outcome = await future
        finally:
            if not priority:
                self._inflight_reports -= 1
        if outcome.ok:
            return
        error_map = {
            "REVOKED": (ErrorCode.CONTROL_REVOKED, "Report superseded by control change"),
            "BACKPRESSURE": (
                ErrorCode.INPUT_BACKPRESSURE,
                "Input queue full; report not scheduled",
            ),
            "TIMEOUT": (ErrorCode.SERIAL_WRITE_FAILED, "Serial write timed out; result uncertain"),
            "SHORT_WRITE": (ErrorCode.SERIAL_WRITE_FAILED, "Short serial write; result uncertain"),
            "FAILED": (ErrorCode.SERIAL_WRITE_FAILED, "Serial write failed"),
            "CLOSED": (ErrorCode.SERIAL_WRITE_FAILED, "Serial port closed"),
        }
        code, message = error_map.get(
            outcome.error or "", (ErrorCode.SERIAL_WRITE_FAILED, "Write failed")
        )
        if code is ErrorCode.SERIAL_WRITE_FAILED:
            # Result uncertain: enter recovery without resending (docs §6).
            self._manager.set_serial_status(DeviceStatus.FAULT)
        raise ControlError(code, message)

    async def _interruptible_wait(self, delay: float, cancel_event: asyncio.Event) -> bool:
        """Sleep with the injectable clock; True when cancelled mid-wait."""
        sleep_future = self._clock.sleep(delay)
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        try:
            await asyncio.wait({sleep_future, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancel_task.cancel()
            sleep_future.cancel()
        return cancel_event.is_set()

    # ------------------------------------------------------------------
    # Human input stream (input WS path)
    # ------------------------------------------------------------------

    def attach_stream(
        self, session: Session, on_ack: Callable[[InputAck], None]
    ) -> HumanInputStream:
        previous = self._streams.get(session.session_id)
        if previous is not None:
            previous.discard_pending()  # new connections never replay old input
        stream = HumanInputStream(self, session, on_ack)
        self._streams[session.session_id] = stream
        self._owner_states.setdefault(
            session.session_id, OwnerInputState(session_id=session.session_id)
        )
        return stream

    def detach_stream(self, session_id: str) -> None:
        stream = self._streams.pop(session_id, None)
        if stream is not None:
            stream.discard_pending()

    def _sweep(self, now: float) -> None:
        self._idem.sweep(now)
        expired = [
            job_id
            for job_id, record in self._jobs.items()
            if record.job.terminal and (now - record.created_monotonic) > self._job_ttl_s
        ]
        for job_id in expired:
            del self._jobs[job_id]


class HumanInputStream:
    """Ordered, bounded event stream for one human input connection."""

    def __init__(
        self, scheduler: InputScheduler, session: Session, on_ack: Callable[[InputAck], None]
    ) -> None:
        self._scheduler = scheduler
        self._manager = scheduler._manager
        self._clock = scheduler._clock
        self._session = session
        self._on_ack = on_ack
        self._next_sequence = 1
        self._acks: OrderedDict[int, InputAck] = OrderedDict()
        self._pending: deque[tuple[HumanInputEnvelope, bool]] = deque()  # (envelope, coalescible)
        self._worker: asyncio.Task | None = None
        self._active_sequence: int | None = None

    # -- intake (from WS receive path; never blocks) ----------------------

    def handle(self, envelope: HumanInputEnvelope) -> None:
        if envelope.sequence < self._next_sequence:
            cached = self._acks.get(envelope.sequence)
            if cached is not None:
                self._emit(cached)
            else:
                self._remember(
                    InputAck(
                        envelope.sequence,
                        "rejected",
                        ControlError(ErrorCode.SEQUENCE_OUT_OF_ORDER, "Too old").to_wire(),
                    )
                )
            return
        if envelope.sequence > self._next_sequence:
            self._remember(
                InputAck(
                    envelope.sequence,
                    "rejected",
                    ControlError(ErrorCode.SEQUENCE_OUT_OF_ORDER, "Gap detected").to_wire(),
                )
            )
            return
        # Exact next sequence: consume it even when rejected below (docs §4).
        self._next_sequence += 1
        try:
            self._manager.authorize_input(self._session, envelope.lease)
        except ControlError as exc:
            self._remember(InputAck(envelope.sequence, "rejected", exc.to_wire()))
            return
        action = envelope.action
        state = self._scheduler.owner_state(self._session.session_id)
        coalescible = (
            isinstance(action, MouseState) and state is not None and action.buttons == state.buttons
        )
        if coalescible and self._pending:
            tail_envelope, tail_coalescible = self._pending[-1]
            if (
                tail_coalescible
                and tail_envelope.sequence != self._active_sequence
                and isinstance(tail_envelope.action, MouseState)
            ):
                # Latest-value merge for buttonless moves (docs §5).
                self._pending.pop()
                self._remember(InputAck(tail_envelope.sequence, "coalesced"))
        if len(self._pending) >= STREAM_QUEUE_LIMIT:
            self._on_queue_full(envelope)
            return
        self._pending.append((envelope, coalescible))
        self._ensure_worker()

    def _on_queue_full(self, envelope: HumanInputEnvelope) -> None:
        """Overflow never drops silently; release-critical events force an
        owner fence so no key/button stays stuck (C12)."""
        action = envelope.action
        release_critical = isinstance(action, KeyboardState)
        if isinstance(action, MouseRelativeState):
            # A pending press may not yet be reflected in owner_state. Never
            # lose a release, or silently discard relative displacement.
            release_critical = True
        elif isinstance(action, MouseState):
            state = self._scheduler.owner_state(self._session.session_id)
            release_critical = state is not None and action.buttons != state.buttons
        error = ControlError(ErrorCode.INPUT_BACKPRESSURE, "Input stream queue full").to_wire()
        self._remember(InputAck(envelope.sequence, "rejected", error))
        if release_critical:
            session = self._session
            manager = self._manager

            async def _fence() -> None:
                await manager.human_suspend(session, "input_backpressure_fence")

            asyncio.ensure_future(_fence())

    def discard_pending(self) -> None:
        self._pending.clear()
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    def _remember(self, ack: InputAck) -> None:
        self._acks[ack.sequence] = ack
        self._acks.move_to_end(ack.sequence)
        while len(self._acks) > STREAM_ACK_CACHE:
            self._acks.popitem(last=False)
        self._emit(ack)

    def _emit(self, ack: InputAck) -> None:
        try:
            self._on_ack(ack)
        except Exception:  # pragma: no cover - transport callback
            logger.exception("ack callback failed")

    # -- execution --------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        while self._pending:
            envelope, _ = self._pending[0]
            self._active_sequence = envelope.sequence
            try:
                await self._execute(envelope)
            except asyncio.CancelledError:
                return
            except ControlError as exc:
                self._remember(InputAck(envelope.sequence, "rejected", exc.to_wire()))
            except Exception:
                logger.exception("stream execute error")
                self._remember(
                    InputAck(
                        envelope.sequence,
                        "rejected",
                        ControlError(
                            ErrorCode.SERIAL_WRITE_FAILED, "Stream write failed"
                        ).to_wire(),
                    )
                )
            finally:
                self._active_sequence = None
            self._pending.popleft()

    async def _execute(self, envelope: HumanInputEnvelope) -> None:
        self._manager.authorize_input(self._session, envelope.lease)
        action = envelope.action
        state = self._scheduler.owner_state(self._session.session_id) or OwnerInputState(
            session_id=self._session.session_id
        )
        now = self._clock.now
        if isinstance(action, KeyboardState):
            modifier = keymap.modifier_bits(list(action.modifiers))
            keys = tuple(keymap.hid_for_code(c) for c in action.keys)
            report: InputReport = KeyboardReport(modifier, keys)
        elif isinstance(action, MouseState):
            basis = self._scheduler._bases.resolve(action.basis_id, now)
            hid_x, hid_y = map_point(basis, action.x, action.y)
            report = MouseAbsReport(action.buttons, hid_x, hid_y, 0)
        elif isinstance(action, MouseRelativeState):
            report = MouseRelReport(action.buttons, action.dx, action.dy, action.wheel)
        elif isinstance(action, MouseScroll):
            basis = self._scheduler._bases.resolve(action.basis_id, now)
            hid_x, hid_y = map_point(basis, action.x, action.y)
            report = MouseAbsReport(state.buttons, hid_x, hid_y, action.steps)
        else:  # pragma: no cover - parser restricts kinds
            raise ControlError(ErrorCode.INVALID_REQUEST, "Unsupported stream action")
        authorized = AuthorizedReport(
            session_id=self._session.session_id,
            lease_id=envelope.lease.lease_id,
            generation=envelope.lease.generation,
            report=report,
        )
        outcome = await self._scheduler._actor.submit(authorized)
        if not outcome.ok:
            if outcome.error == "REVOKED":
                raise ControlError(ErrorCode.CONTROL_REVOKED, "Report superseded")
            self._manager.set_serial_status(DeviceStatus.FAULT)
            raise ControlError(ErrorCode.SERIAL_WRITE_FAILED, "Serial write failed")
        state.apply(report)
        self._remember(InputAck(envelope.sequence, "written"))
