"""Single-instance broker mutex and runtime discovery.

The named mutex ``Local\\Superuse.<user SID>.broker`` prevents a second broker
in the same interactive login session. PID liveness is never trusted alone:
runtime files are reconciled with instance id + /healthz before reuse
(docs/development/windows-operations.md §2).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

RUNTIME_FILENAME = "broker.json"


def _user_sid() -> str:
    try:
        import win32security

        token = win32security.OpenProcessToken(
            win32security.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        info = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(info[0])
    except Exception:
        return "unknown"


def mutex_name() -> str:
    return f"Local\\Superuse.{_user_sid()}.broker"


class BrokerMutex:
    """Owns the named mutex for the process lifetime."""

    def __init__(self) -> None:
        self._handle = None
        self.acquired = False

    def acquire(self) -> bool:
        try:
            import win32api
            import win32event
            import winerror

        except ImportError:
            # Non-Windows/dev environments: single instance by convention.
            self.acquired = True
            return True
        handle = win32event.CreateMutex(None, False, mutex_name())
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            handle.Close()
            self.acquired = False
            return False
        win32event.WaitForSingleObject(handle, 0)
        self._handle = handle
        self.acquired = True
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._handle.Release()
            except Exception:  # pragma: no cover - handle edge cases
                pass
            self._handle = None
        self.acquired = False


def runtime_path(runtime_dir: Path) -> Path:
    return runtime_dir / RUNTIME_FILENAME


def read_runtime_file(runtime_dir: Path) -> dict | None:
    try:
        data = json.loads(runtime_path(runtime_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "port" not in data or "instance_id" not in data:
        return None
    return data


async def find_healthy_broker(runtime_dir: Path) -> dict | None:
    """Return the runtime record of a live broker, or None.

    Old files whose owner died are reported so the caller can decide about
    cleanup; nothing is deleted here (only after instance reconciliation).
    """
    data = read_runtime_file(runtime_dir)
    if data is None:
        return None
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            base_url=f"http://127.0.0.1:{data['port']}", timeout=aiohttp.ClientTimeout(total=2)
        ) as http:
            async with http.get("/healthz") as response:
                body = await response.json()
                if response.status == 200 and body.get("alive"):
                    return data
    except (aiohttp.ClientError, TimeoutError, OSError):
        return None
    return None


def process_alive(pid: int) -> bool:
    try:
        import pywintypes
        import win32api
        import win32con
        import win32process

        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            win32process.GetExitCodeProcess(handle)
            return True
        finally:
            handle.Close()
    except (pywintypes.error, OSError, ImportError):
        return False


def stale_runtime_owner(runtime_dir: Path) -> bool:
    """True when a runtime file exists but its PID is gone.

    Callers must still verify instance identity before deleting anything.
    """
    data = read_runtime_file(runtime_dir)
    if data is None:
        return False
    pid = data.get("pid")
    return isinstance(pid, int) and not process_alive(pid)


def new_instance_marker() -> str:
    return str(uuid.uuid4())


def owner_matches(runtime_dir: Path, instance_id: str) -> bool:
    data = read_runtime_file(runtime_dir)
    return (
        data is not None
        and data.get("instance_id") == instance_id
        and data.get("pid") == os.getpid()
    )
