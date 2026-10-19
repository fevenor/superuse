"""Machine-readable control errors mapped to the contract error codes."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    CONTROL_REQUIRED = "CONTROL_REQUIRED"
    CONTROL_BUSY = "CONTROL_BUSY"
    HUMAN_CONTROL = "HUMAN_CONTROL"
    CONTROL_REVOKED = "CONTROL_REVOKED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    INSTANCE_CHANGED = "INSTANCE_CHANGED"
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
    DEVICE_AMBIGUOUS = "DEVICE_AMBIGUOUS"
    DEVICE_MOUSE_MODE = "DEVICE_MOUSE_MODE"
    VIDEO_STALE = "VIDEO_STALE"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    FRAME_TIMEOUT = "FRAME_TIMEOUT"
    STALE_BASIS = "STALE_BASIS"
    COORDINATE_OUT_OF_BOUNDS = "COORDINATE_OUT_OF_BOUNDS"
    INPUT_LIMIT = "INPUT_LIMIT"
    UNSUPPORTED_KEY = "UNSUPPORTED_KEY"
    UNSUPPORTED_TEXT = "UNSUPPORTED_TEXT"
    INPUT_BACKPRESSURE = "INPUT_BACKPRESSURE"
    REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"
    SEQUENCE_OUT_OF_ORDER = "SEQUENCE_OUT_OF_ORDER"
    ACTION_CANCELLED = "ACTION_CANCELLED"
    ACTION_TIMEOUT = "ACTION_TIMEOUT"
    SERIAL_WRITE_FAILED = "SERIAL_WRITE_FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    INVALID_REQUEST = "INVALID_REQUEST"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_FOUND = "NOT_FOUND"


_RETRYABLE: frozenset[str] = frozenset(
    {
        ErrorCode.BROKER_UNAVAILABLE,
        ErrorCode.DEVICE_UNAVAILABLE,
        ErrorCode.VIDEO_STALE,
        ErrorCode.VIDEO_UNAVAILABLE,
        ErrorCode.FRAME_TIMEOUT,
        ErrorCode.STALE_BASIS,
        ErrorCode.RATE_LIMITED,
    }
)


class ControlError(Exception):
    """Business error carrying a contract error code.

    ``retryable`` matches the guidance in docs/development/api-contract.md section 7 so
    clients can decide between backing off and re-observing.
    """

    def __init__(self, code: ErrorCode | str, message: str) -> None:
        self.code = ErrorCode(code)
        self.message = message[:512]
        super().__init__(f"{self.code}: {self.message}")

    @property
    def retryable(self) -> bool:
        return self.code in _RETRYABLE

    def to_wire(self) -> dict:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}
