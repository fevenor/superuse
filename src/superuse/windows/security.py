"""File ACLs and diagnostics sanitization (docs/development/windows-operations.md §2/§5).

Runtime/state files must be readable only by the current user (plus SYSTEM
and Administrators as necessary); temporary files in the same directories
inherit the same restriction. No secrets are ever logged or exported.
"""

from __future__ import annotations

import os
from pathlib import Path


def apply_user_only_acl(path: Path) -> bool:
    """Restrict ``path`` to the current user (+SYSTEM/Administrators).

    Returns True when the ACL was applied; a clean False means the platform
    or permission level does not allow it (non-Windows dev environments).
    """
    try:
        import ntsecuritycon as con
        import pywintypes
        import win32api
        import win32security
    except ImportError:
        return False
    try:
        account = win32api.GetUserNameEx(win32api.NameSamCompatible)
        user, _domain, _type = win32security.LookupAccountName(None, account)
        system = win32security.ConvertStringSidToSid("S-1-5-18")
        admins = win32security.ConvertStringSidToSid("S-1-5-32-544")
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(con.FILE_ALL_ACCESS, user)
        dacl.AddAccessAllowedAce(con.FILE_ALL_ACCESS, system)
        dacl.AddAccessAllowedAce(con.FILE_ALL_ACCESS, admins)
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION, descriptor
        )
        return True
    except (pywintypes.error, OSError):
        return False


def read_dacl_entries(path: Path) -> list[str]:
    """List trustee SIDs in the DACL for verification (diagnostics/tests)."""
    try:
        import win32security
    except ImportError:
        return []
    try:
        security = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
        dacl = security.GetSecurityDescriptorDacl()
        if dacl is None:
            return []
        entries = []
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            trustee_sid = ace[2]
            entries.append(win32security.ConvertSidToStringSid(trustee_sid))
        return entries
    except Exception:  # pragma: no cover - platform dependent
        return []


def sanitize_runtime_record(data: dict) -> dict:
    """Runtime view safe for logs/status output: no credentials."""
    cleaned = dict(data)
    cleaned.pop("credentials", None)
    return cleaned


def sanitize_text(value: str, keep: int = 4) -> str:
    """Shorten identifiers (serial numbers, paths) for diagnostics."""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def secure_write_json(path: Path, payload: dict) -> None:
    """Atomic write with the same-directory temp file and a user-only ACL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(_json_dumps(payload), encoding="utf-8")
    apply_user_only_acl(tmp)
    os.replace(tmp, path)


def _json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, indent=2)
