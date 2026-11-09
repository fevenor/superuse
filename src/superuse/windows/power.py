from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class PowerRequest:
    def __init__(self):
        self._handle = None
        self._kernel = None
        self.error: str | None = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self.active:
            return
        self.error = None
        try:
            if not enabled:
                self.close()
                return
            if sys.platform != "win32":
                raise OSError("Keep awake requires Windows")

            class Detailed(ctypes.Structure):
                _fields_ = [
                    ("module", wintypes.HMODULE),
                    ("id", wintypes.ULONG),
                    ("count", wintypes.ULONG),
                    ("strings", ctypes.POINTER(wintypes.LPWSTR)),
                ]

            class Reason(ctypes.Union):
                _fields_ = [("simple", wintypes.LPWSTR), ("detailed", Detailed)]

            class Context(ctypes.Structure):
                _fields_ = [
                    ("version", wintypes.ULONG),
                    ("flags", wintypes.DWORD),
                    ("reason", Reason),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.PowerCreateRequest.argtypes = [ctypes.POINTER(Context)]
            kernel.PowerCreateRequest.restype = wintypes.HANDLE
            for name in ("PowerSetRequest", "PowerClearRequest"):
                function = getattr(kernel, name)
                function.argtypes = [wintypes.HANDLE, ctypes.c_int]
                function.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            context = Context(0, 1, Reason(simple="Superuse: keep this computer awake"))
            handle = kernel.PowerCreateRequest(ctypes.byref(context))
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.PowerSetRequest(handle, 1):
                error = ctypes.get_last_error()
                kernel.CloseHandle(handle)
                raise ctypes.WinError(error)
            self._kernel, self._handle = kernel, handle
        except OSError as exc:
            self.error = f"Power request failed ({getattr(exc, 'winerror', None) or 'unsupported'})"
            raise

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                self._kernel.PowerClearRequest(handle, 1)
            finally:
                self._kernel.CloseHandle(handle)
