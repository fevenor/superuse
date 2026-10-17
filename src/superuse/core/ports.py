"""Device-side ports the control core depends on.

The core never imports serial or capture implementations; it talks to the
:class:`ActorPort` (the unique serial executor) and a :class:`LatchStore`.
Real implementations live in ``superuse.device`` (T02), fakes in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from superuse.core.models import InputReport


@dataclass(frozen=True)
class AuthorizedReport:
    """A report scheduled for the serial write thread.

    Carries its authorization snapshot (session, lease, generation). The actor
    re-checks the generation when the report enters the write thread: the
    authorization decision and the write are ordered through the same serial
    executor, so a fence that bumped the generation between scheduling and
    writing rejects the report instead of sending stale input.
    """

    session_id: str
    lease_id: str
    generation: int
    report: InputReport
    priority: bool = False  # control-plane reports bypass the input queue


@dataclass(frozen=True)
class WriteOutcome:
    written: int  # bytes accepted by the transport
    error: str | None  # None on full write; else REVOKED|TIMEOUT|SHORT_WRITE|FAILED|CLOSED

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class FenceTicket:
    """Identifies one release barrier; stale completions are ignored."""

    generation: int
    transition_id: str


@dataclass(frozen=True)
class FenceResult:
    ok: bool
    error: str | None = None  # TIMEOUT | WRITE_FAILED
    neutral_reports: tuple[InputReport, ...] = ()


class ActorPort(Protocol):
    """The unique serial executor owned by the broker's device module."""

    def submit(self, report: AuthorizedReport) -> object:
        """Queue one report; returns an awaitable WriteOutcome future.

        The awaitable resolves when the write thread has written (or failed)
        the report. Awaiting is optional; the scheduler awaits per step.
        """
        ...

    async def fence(self, ticket: FenceTicket, timeout_s: float) -> FenceResult:
        """Release barrier: drain/purge old-generation writes, neutralize.

        - lets an already-started single write finish or time out,
        - drops queued reports of older generations and cancels their futures
          with REVOKED,
        - sends the keyboard-zero and mouse-zero neutralization reports from
          the write thread (these may cross owners; only fence may),
        - adopts ``ticket.generation`` as the actor's current generation.
        """
        ...


class LatchStore(Protocol):
    """Persistence for the human latch flag (crash-safe, no secrets)."""

    def save(self, latched: bool) -> None: ...

    def load(self) -> bool | None: ...
