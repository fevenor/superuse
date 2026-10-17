"""Contract data models (wire field names follow schemas/contracts.schema.json).

These dataclasses are the single in-process representation of the contract.
Transport layers (HTTP/WS/MCP) parse wire dicts into these objects and never
re-implement control rules on raw JSON.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

API_VERSION = 1


class Mode(StrEnum):
    IDLE = "idle"
    AGENT = "agent"
    HUMAN = "human"
    HUMAN_HOLD = "human_hold"
    TRANSITION = "transition"
    RECOVERY = "recovery"
    STOPPING = "stopping"


class DeviceStatus(StrEnum):
    DISCONNECTED = "disconnected"
    STARTING = "starting"
    READY = "ready"
    FAULT = "fault"


class JobStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WRITTEN = "written"
    CANCELLED = "cancelled"
    FAILED = "failed"


class SessionKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


# ---------------------------------------------------------------------------
# Generic strict wire parsing
# ---------------------------------------------------------------------------


class WireError(ValueError):
    """Raised when a wire object does not match the contract shape."""


def _expect_object(data: Any, expected_keys: set[str], required: set[str], what: str) -> dict:
    if not isinstance(data, dict):
        raise WireError(f"{what}: expected object")
    unknown = set(data) - expected_keys
    if unknown:
        raise WireError(f"{what}: unknown field(s) {sorted(unknown)}")
    missing = required - set(data)
    if missing:
        raise WireError(f"{what}: missing field(s) {sorted(missing)}")
    return data


def _check_int(value: Any, lo: int | None, hi: int | None, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WireError(f"{what}: expected integer")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise WireError(f"{what}: out of range")
    return value


def _check_str(value: Any, max_len: int | None, what: str) -> str:
    if not isinstance(value, str):
        raise WireError(f"{what}: expected string")
    if max_len is not None and len(value) > max_len:
        raise WireError(f"{what}: longer than {max_len}")
    return value


def _check_uuid(value: Any, what: str) -> str:
    value = _check_str(value, None, what)
    if len(value) != 36 or value.count("-") != 4:
        raise WireError(f"{what}: not a UUID")
    return value


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Owner:
    kind: SessionKind
    session_id: str
    label: str

    def to_wire(self) -> dict:
        return {"kind": self.kind.value, "session_id": self.session_id, "label": self.label}


@dataclass(frozen=True)
class State:
    instance_id: str
    state_revision: int
    generation: int
    mode: Mode
    owner: Owner | None
    human_latched: bool
    source: str  # "real" | "simulated"
    serial_status: DeviceStatus
    video_status: DeviceStatus
    frame_age_ms: float | None

    def to_wire(self) -> dict:
        return {
            "api_version": API_VERSION,
            "instance_id": self.instance_id,
            "state_revision": self.state_revision,
            "generation": self.generation,
            "mode": self.mode.value,
            "owner": self.owner.to_wire() if self.owner else None,
            "human_latched": self.human_latched,
            "source": self.source,
            "serial_status": self.serial_status.value,
            "video_status": self.video_status.value,
            "frame_age_ms": self.frame_age_ms,
        }


@dataclass(frozen=True)
class StateEvent:
    event_id: int
    instance_id: str
    state_revision: int
    reason: str
    state: State

    def to_wire(self) -> dict:
        return {
            "event_id": self.event_id,
            "instance_id": self.instance_id,
            "state_revision": self.state_revision,
            "reason": self.reason,
            "state": self.state.to_wire(),
        }


# ---------------------------------------------------------------------------
# Lease and sessions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lease:
    instance_id: str
    lease_id: str
    generation: int

    @staticmethod
    def from_wire(data: Any) -> Lease:
        _expect_object(
            data,
            {"instance_id", "lease_id", "generation"},
            {"instance_id", "lease_id", "generation"},
            "lease",
        )
        return Lease(
            instance_id=_check_uuid(data["instance_id"], "lease.instance_id"),
            lease_id=_check_uuid(data["lease_id"], "lease.lease_id"),
            generation=_check_int(data["generation"], 0, 9007199254740991, "lease.generation"),
        )


@dataclass(frozen=True)
class Session:
    session_id: str
    kind: SessionKind
    label: str


# ---------------------------------------------------------------------------
# Input actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PressKey:
    code: str
    modifiers: tuple[str, ...] = ()
    hold_ms: int = 50
    kind: str = field(default="press_key", init=False)


@dataclass(frozen=True)
class TypeText:
    text: str
    char_interval_ms: int = 15
    kind: str = field(default="type_text", init=False)


@dataclass(frozen=True)
class MouseMove:
    basis_id: str
    x: float
    y: float
    kind: str = field(default="mouse_move", init=False)


@dataclass(frozen=True)
class MouseClick:
    basis_id: str
    x: float
    y: float
    button: str = "left"
    count: int = 1
    kind: str = field(default="mouse_click", init=False)


@dataclass(frozen=True)
class MouseDrag:
    basis_id: str
    from_x: float
    from_y: float
    to_x: float
    to_y: float
    button: str = "left"
    duration_ms: int = 500
    kind: str = field(default="mouse_drag", init=False)


@dataclass(frozen=True)
class MouseScroll:
    basis_id: str
    x: float
    y: float
    steps: int
    kind: str = field(default="mouse_scroll", init=False)


@dataclass(frozen=True)
class KeyboardState:
    keys: tuple[str, ...] = ()
    modifiers: tuple[str, ...] = ()
    kind: str = field(default="keyboard_state", init=False)


@dataclass(frozen=True)
class MouseState:
    basis_id: str
    x: float
    y: float
    buttons: int
    kind: str = field(default="mouse_state", init=False)


@dataclass(frozen=True)
class ReleaseInputs:
    kind: str = field(default="release_inputs", init=False)


@dataclass(frozen=True)
class MouseMoveRelative:
    dx: int
    dy: int
    kind: str = field(default="mouse_move_relative", init=False)


@dataclass(frozen=True)
class MouseClickRelative:
    button: str = "left"
    count: int = 1
    kind: str = field(default="mouse_click_relative", init=False)


@dataclass(frozen=True)
class MouseDragRelative:
    dx: int
    dy: int
    button: str = "left"
    duration_ms: int = 500
    kind: str = field(default="mouse_drag_relative", init=False)


@dataclass(frozen=True)
class MouseScrollRelative:
    steps: int
    kind: str = field(default="mouse_scroll_relative", init=False)


@dataclass(frozen=True)
class MouseRelativeState:
    dx: int
    dy: int
    buttons: int
    wheel: int = 0
    kind: str = field(default="mouse_relative_state", init=False)


Action = (
    PressKey
    | TypeText
    | MouseMove
    | MouseClick
    | MouseDrag
    | MouseScroll
    | KeyboardState
    | MouseState
    | ReleaseInputs
    | MouseMoveRelative
    | MouseClickRelative
    | MouseDragRelative
    | MouseScrollRelative
    | MouseRelativeState
)

MODIFIER_NAMES = (
    "left_ctrl",
    "left_shift",
    "left_alt",
    "left_gui",
    "right_ctrl",
    "right_shift",
    "right_alt",
    "right_gui",
)
BUTTON_NAMES = ("left", "right", "middle")


def _modifiers_from_wire(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise WireError(f"{what}: expected array")
    mods = tuple(_check_str(v, 40, what) for v in value)
    if len(set(mods)) != len(mods) or len(mods) > 8:
        raise WireError(f"{what}: duplicate or too many modifiers")
    for m in mods:
        if m not in MODIFIER_NAMES:
            raise WireError(f"{what}: unknown modifier {m!r}")
    return mods


def _code_from_wire(value: Any, what: str) -> str:
    code = _check_str(value, 40, what)
    if not code or not code[0].isalpha() or not all(c.isalnum() for c in code):
        raise WireError(f"{what}: invalid code")
    return code


def _coordinate(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WireError(f"{what}: expected number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise WireError(f"{what}: negative or non-finite")
    return number


def _uuid_field(value: Any, what: str) -> str:
    return _check_uuid(value, what)


def action_from_wire(data: Any) -> Action:
    """Strict parser for Action objects; semantic key checks live in the keymap."""
    if not isinstance(data, dict) or "kind" not in data:
        raise WireError("action: missing kind")
    if data["kind"] in (
        "mouse_move_relative",
        "mouse_drag_relative",
        "mouse_click_relative",
        "mouse_scroll_relative",
        "mouse_relative_state",
    ):
        return _relative_action_from_wire(data)
    kind = data["kind"]
    common = {"kind"}
    if kind == "press_key":
        _expect_object(data, common | {"code", "modifiers", "hold_ms"}, {"code"}, "press_key")
        return PressKey(
            code=_code_from_wire(data["code"], "press_key.code"),
            modifiers=_modifiers_from_wire(data.get("modifiers", []), "press_key.modifiers"),
            hold_ms=_check_int(data.get("hold_ms", 50), 10, 1000, "press_key.hold_ms"),
        )
    if kind == "type_text":
        _expect_object(data, common | {"text", "char_interval_ms"}, {"text"}, "type_text")
        return TypeText(
            text=_check_str(data["text"], 512, "type_text.text"),
            char_interval_ms=_check_int(
                data.get("char_interval_ms", 15), 10, 50, "type_text.char_interval_ms"
            ),
        )
    if kind == "mouse_move":
        _expect_object(data, common | {"basis_id", "x", "y"}, {"basis_id", "x", "y"}, "mouse_move")
        return MouseMove(
            basis_id=_uuid_field(data["basis_id"], "mouse_move.basis_id"),
            x=_coordinate(data["x"], "mouse_move.x"),
            y=_coordinate(data["y"], "mouse_move.y"),
        )
    if kind == "mouse_click":
        _expect_object(
            data,
            common | {"basis_id", "x", "y", "button", "count"},
            {"basis_id", "x", "y"},
            "mouse_click",
        )
        button = data.get("button", "left")
        if button not in BUTTON_NAMES:
            raise WireError("mouse_click.button: unknown button")
        return MouseClick(
            basis_id=_uuid_field(data["basis_id"], "mouse_click.basis_id"),
            x=_coordinate(data["x"], "mouse_click.x"),
            y=_coordinate(data["y"], "mouse_click.y"),
            button=button,
            count=_check_int(data.get("count", 1), 1, 2, "mouse_click.count"),
        )
    if kind == "mouse_drag":
        _expect_object(
            data,
            common | {"basis_id", "from_x", "from_y", "to_x", "to_y", "button", "duration_ms"},
            {"basis_id", "from_x", "from_y", "to_x", "to_y"},
            "mouse_drag",
        )
        button = data.get("button", "left")
        if button not in BUTTON_NAMES:
            raise WireError("mouse_drag.button: unknown button")
        return MouseDrag(
            basis_id=_uuid_field(data["basis_id"], "mouse_drag.basis_id"),
            from_x=_coordinate(data["from_x"], "mouse_drag.from_x"),
            from_y=_coordinate(data["from_y"], "mouse_drag.from_y"),
            to_x=_coordinate(data["to_x"], "mouse_drag.to_x"),
            to_y=_coordinate(data["to_y"], "mouse_drag.to_y"),
            button=button,
            duration_ms=_check_int(
                data.get("duration_ms", 500), 50, 5000, "mouse_drag.duration_ms"
            ),
        )
    if kind == "mouse_scroll":
        _expect_object(
            data,
            common | {"basis_id", "x", "y", "steps"},
            {"basis_id", "x", "y", "steps"},
            "mouse_scroll",
        )
        return MouseScroll(
            basis_id=_uuid_field(data["basis_id"], "mouse_scroll.basis_id"),
            x=_coordinate(data["x"], "mouse_scroll.x"),
            y=_coordinate(data["y"], "mouse_scroll.y"),
            steps=_check_int(data["steps"], -127, 127, "mouse_scroll.steps"),
        )
    if kind == "keyboard_state":
        _expect_object(
            data, common | {"keys", "modifiers"}, {"keys", "modifiers"}, "keyboard_state"
        )
        keys_value = data["keys"]
        if not isinstance(keys_value, list):
            raise WireError("keyboard_state.keys: expected array")
        keys = tuple(_code_from_wire(v, "keyboard_state.keys") for v in keys_value)
        if len(set(keys)) != len(keys) or len(keys) > 6:
            raise WireError("keyboard_state.keys: duplicate or more than 6 keys")
        return KeyboardState(
            keys=keys, modifiers=_modifiers_from_wire(data["modifiers"], "keyboard_state.modifiers")
        )
    if kind == "mouse_state":
        _expect_object(
            data,
            common | {"basis_id", "x", "y", "buttons"},
            {"basis_id", "x", "y", "buttons"},
            "mouse_state",
        )
        return MouseState(
            basis_id=_uuid_field(data["basis_id"], "mouse_state.basis_id"),
            x=_coordinate(data["x"], "mouse_state.x"),
            y=_coordinate(data["y"], "mouse_state.y"),
            buttons=_check_int(data["buttons"], 0, 7, "mouse_state.buttons"),
        )
    if kind == "release_inputs":
        _expect_object(data, common, set(), "release_inputs")
        return ReleaseInputs()
    raise WireError(f"action: unknown kind {kind!r}")


def _relative_action_from_wire(data: dict) -> Action:
    kind = data["kind"]
    if kind in ("mouse_move_relative", "mouse_drag_relative"):
        fields = {"dx", "dy"}
        extra = {"button", "duration_ms"} if kind == "mouse_drag_relative" else set()
        _expect_object(data, {"kind"} | fields | extra, {"kind"} | fields, kind)
        dx = _check_int(data["dx"], -4096, 4096, f"{kind}.dx")
        dy = _check_int(data["dy"], -4096, 4096, f"{kind}.dy")
        if kind == "mouse_move_relative":
            return MouseMoveRelative(dx, dy)
        button = data.get("button", "left")
        if button not in BUTTON_NAMES:
            raise WireError(f"{kind}.button: unknown button")
        return MouseDragRelative(
            dx,
            dy,
            button,
            _check_int(data.get("duration_ms", 500), 50, 5000, f"{kind}.duration_ms"),
        )
    if kind == "mouse_click_relative":
        _expect_object(data, {"kind", "button", "count"}, {"kind"}, kind)
        button = data.get("button", "left")
        if button not in BUTTON_NAMES:
            raise WireError(f"{kind}.button: unknown button")
        return MouseClickRelative(button, _check_int(data.get("count", 1), 1, 2, f"{kind}.count"))
    if kind == "mouse_scroll_relative":
        _expect_object(data, {"kind", "steps"}, {"kind", "steps"}, kind)
        steps = _check_int(data["steps"], -127, 127, f"{kind}.steps")
        if steps == 0:
            raise WireError(f"{kind}.steps: must be non-zero")
        return MouseScrollRelative(steps)
    _expect_object(
        data, {"kind", "dx", "dy", "buttons", "wheel"}, {"kind", "dx", "dy", "buttons"}, kind
    )
    return MouseRelativeState(
        _check_int(data["dx"], -127, 127, f"{kind}.dx"),
        _check_int(data["dy"], -127, 127, f"{kind}.dy"),
        _check_int(data["buttons"], 0, 7, f"{kind}.buttons"),
        _check_int(data.get("wheel", 0), -127, 127, f"{kind}.wheel"),
    )


HUMAN_ONLY_ACTIONS = frozenset({"keyboard_state", "mouse_state", "mouse_relative_state"})


# ---------------------------------------------------------------------------
# Envelopes, acks, jobs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandEnvelope:
    request_id: str
    lease: Lease
    action: Action

    @staticmethod
    def from_wire(data: Any) -> CommandEnvelope:
        _expect_object(
            data,
            {"api_version", "request_id", "lease", "action"},
            {"api_version", "request_id", "lease", "action"},
            "command",
        )
        if data["api_version"] != API_VERSION:
            raise WireError("command.api_version: must be 1")
        return CommandEnvelope(
            request_id=_check_uuid(data["request_id"], "command.request_id"),
            lease=Lease.from_wire(data["lease"]),
            action=action_from_wire(data["action"]),
        )


@dataclass(frozen=True)
class HumanInputEnvelope:
    sequence: int
    lease: Lease
    action: KeyboardState | MouseState | MouseScroll | MouseRelativeState

    @staticmethod
    def from_wire(data: Any) -> HumanInputEnvelope:
        _expect_object(
            data,
            {"api_version", "sequence", "lease", "action"},
            {"api_version", "sequence", "lease", "action"},
            "human_input",
        )
        if data["api_version"] != API_VERSION:
            raise WireError("human_input.api_version: must be 1")
        action = action_from_wire(data["action"])
        if not isinstance(action, KeyboardState | MouseState | MouseScroll | MouseRelativeState):
            raise WireError("human_input.action: kind not allowed on input stream")
        return HumanInputEnvelope(
            sequence=_check_int(data["sequence"], 1, 9007199254740991, "human_input.sequence"),
            lease=Lease.from_wire(data["lease"]),
            action=action,
        )


@dataclass(frozen=True)
class InputAck:
    sequence: int
    status: str  # accepted | written | coalesced | rejected
    error: dict | None = None

    def to_wire(self) -> dict:
        wire: dict = {"type": "input_ack", "sequence": self.sequence, "status": self.status}
        if self.error is not None:
            wire["error"] = self.error
        return wire


@dataclass
class Job:
    job_id: str
    request_id: str
    session_id: str
    status: JobStatus
    reports_written: int = 0
    steps_completed: int = 0
    partial: bool = False
    error: dict | None = None

    @property
    def terminal(self) -> bool:
        return self.status in (JobStatus.WRITTEN, JobStatus.CANCELLED, JobStatus.FAILED)

    def to_wire(self) -> dict:
        wire: dict = {
            "job_id": self.job_id,
            "request_id": self.request_id,
            "status": self.status.value,
            "terminal": self.terminal,
            "reports_written": self.reports_written,
            "steps_completed": self.steps_completed,
            "partial": self.partial,
        }
        if self.status is JobStatus.WRITTEN:
            wire["completion"] = "written"
        if self.error is not None:
            wire["error"] = self.error
        return wire


# ---------------------------------------------------------------------------
# Device-level HID reports (abstract; SerialActor encodes to wire bytes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyboardReport:
    modifier: int
    keys: tuple[int, ...]


@dataclass(frozen=True)
class MouseAbsReport:
    buttons: int
    x: int  # 0..4095 HID space
    y: int
    wheel: int  # signed, positive scrolls up


@dataclass(frozen=True)
class MouseRelReport:
    buttons: int
    dx: int
    dy: int
    wheel: int


InputReport = KeyboardReport | MouseAbsReport | MouseRelReport

KEYBOARD_RELEASE = KeyboardReport(0, ())
MOUSE_ZERO_RELEASE = MouseRelReport(0, 0, 0, 0)
