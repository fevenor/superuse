"""LAN access policy: the shared key, entry origins and network generation.

Task 2026-10-17 §3.4. The trusted-LAN model is deliberately simple: one key
from config.toml admits both remote browsers and HTTP MCP agents. The key is
never an identity — every admitted client gets its own server-side session,
and origin (local vs LAN) is decided by which listener served the request,
never by client claims or X-Forwarded-* headers.

No HTTPS, certificates, OAuth, accounts, pairing or strength rules here by
design; lease/CSRF/Host/Origin checks in the existing transports stay.
"""

from __future__ import annotations

import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from superuse.core.errors import ControlError, ErrorCode

LAN_BROWSER_COOKIE = "superuse_lan"  # distinct namespace from the local superuse_browser
LAN_IDENTITY_TTL_S = 12 * 3600.0
LAN_MAX_IDENTITIES = 32  # bounded: one browser per machine is the normal shape
LAN_LOGIN_COOLDOWN_S = 1.0
LAN_LOGIN_MAX_FAILURES = 20


def local_ipv4_addresses() -> list[str]:
    """Actual local IPv4 addresses (never 0.0.0.0 itself).

    Used for display (copyable LAN URLs) and for the Host/Origin allowlists
    of wildcard-bound listeners (review 2026-10-17 R2): a client reaching a
    0.0.0.0 listener through a real NIC sends that NIC's address as Host,
    not the bind address.
    """
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:
        # Default-route probe: no packet is sent for UDP connect.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    addresses.discard("127.0.0.1")
    return sorted(addresses)


def _now() -> float:
    return time.monotonic()


class NetworkAuthority:
    """Owns the shared key and the LAN enable/generation state.

    All mutations happen on the broker loop. ``generation`` increments on
    every enable/disable transition, so anything bound to an older generation
    (LAN cookies, page tokens, MCP client contexts) is invalid after a toggle
    and cannot be resurrected by re-enabling.
    """

    def __init__(self, *, key: str) -> None:
        # Empty key means "network access not configured"; it must never be
        # equivalent to "no auth".
        self._key: str | None = key or None
        self._enabled = False
        self._generation = 0
        self.last_bind_error: str | None = None
        # Filled by the broker while LAN is enabled: yields the actual local
        # IPv4 addresses (never the 0.0.0.0 placeholder) for display.
        self.addresses_provider: Callable[[], list[str]] | None = None

    # -- key ---------------------------------------------------------------

    @property
    def key_configured(self) -> bool:
        return self._key is not None

    def check_key(self, presented: str | None) -> bool:
        """Exact, normalization-free comparison against the configured key."""
        if self._key is None or presented is None:
            return False
        return secrets.compare_digest(
            presented.encode("utf-8", errors="strict"), self._key.encode("utf-8")
        )

    def check_key_bytes(self, raw: bytes | None) -> bool:
        """Header-path comparison: the Bearer token is the key's UTF-8 bytes.

        HTTP header values arrive as latin-1-decoded bytes; comparing the raw
        byte sequence keeps non-ASCII keys usable without transformation
        (task §3.2 encoding rule).
        """
        if self._key is None or raw is None:
            return False
        return secrets.compare_digest(raw, self._key.encode("utf-8"))

    # -- enablement ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def generation(self) -> int:
        return self._generation

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._generation += 1

    def status_wire(self, *, addresses: list[str] | None = None) -> dict[str, Any]:
        """Key-free status for /v1/network and MCP get_status."""
        reason = None
        if not self.key_configured:
            reason = "key_missing"
        elif self.last_bind_error is not None:
            reason = "bind_error"
        elif not self._enabled:
            reason = "disabled"
        if addresses is None and self._enabled and self.addresses_provider is not None:
            addresses = self.addresses_provider()
        wire: dict[str, Any] = {
            "enabled": self._enabled,
            "key_configured": self.key_configured,
            "generation": self._generation,
            "reason": reason,
            "addresses": addresses or [],
        }
        if self.last_bind_error is not None:
            wire["bind_error"] = self.last_bind_error
        return wire


@dataclass
class LanIdentity:
    """A remote browser admitted by the shared key.

    The cookie value doubles as the registry key. Page sessions created from
    this identity inherit ``entrance='lan'`` and the network generation, so
    disabling LAN invalidates the whole chain at once.
    """

    cookie_value: str
    csrf: str
    generation: int
    created_at: float = field(default_factory=_now)
    expires_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = self.created_at + LAN_IDENTITY_TTL_S


class LanIdentityRegistry:
    """Bounded registry of LAN browser identities (one per login)."""

    def __init__(self, authority: NetworkAuthority) -> None:
        self._authority = authority
        self._by_cookie: dict[str, LanIdentity] = {}
        self._failures = 0
        self._cooldown_until = 0.0

    def login(self, presented_key: str | None) -> LanIdentity:
        """Exchange the shared key for a LAN browser identity."""
        now = _now()
        if now < self._cooldown_until:
            raise ControlError(ErrorCode.RATE_LIMITED, "Too many login attempts; retry shortly")
        if not self._authority.key_configured or not self._authority.enabled:
            # The listener is torn down on disable; this guards a race where
            # a request already in flight hits the closed window.
            raise ControlError(ErrorCode.FORBIDDEN, "LAN access is not enabled")
        if not self._authority.check_key(presented_key):
            self._failures += 1
            if self._failures >= LAN_LOGIN_MAX_FAILURES:
                self._cooldown_until = now + LAN_LOGIN_COOLDOWN_S
                self._failures = 0
            raise ControlError(ErrorCode.AUTH_FAILED, "Wrong key")
        self._failures = 0
        self._sweep(now)
        if len(self._by_cookie) >= LAN_MAX_IDENTITIES:
            raise ControlError(ErrorCode.RATE_LIMITED, "Too many LAN browsers; close one first")
        identity = LanIdentity(
            cookie_value=secrets.token_urlsafe(32),
            csrf=secrets.token_urlsafe(32),
            generation=self._authority.generation,
        )
        self._by_cookie[identity.cookie_value] = identity
        return identity

    def authenticate(self, cookie_value: str | None) -> LanIdentity:
        """Validate a LAN cookie against the current network generation."""
        if not cookie_value:
            raise ControlError(ErrorCode.AUTH_FAILED, "No LAN session; log in with the key")
        identity = self._by_cookie.get(cookie_value)
        if identity is None:
            raise ControlError(ErrorCode.AUTH_FAILED, "Unknown or expired LAN session")
        if _now() >= identity.expires_at:
            del self._by_cookie[cookie_value]
            raise ControlError(ErrorCode.AUTH_FAILED, "LAN session expired; log in again")
        if identity.generation != self._authority.generation:
            # Network toggled since login: the old identity is dead for good.
            raise ControlError(ErrorCode.AUTH_FAILED, "LAN session invalidated; log in again")
        return identity

    def logout(self, cookie_value: str | None) -> None:
        if cookie_value:
            self._by_cookie.pop(cookie_value, None)

    def invalidate_all(self) -> list[LanIdentity]:
        """Drop every identity (LAN disable); returns them for session cleanup."""
        dropped = list(self._by_cookie.values())
        self._by_cookie.clear()
        self._failures = 0
        self._cooldown_until = 0.0
        return dropped

    def _sweep(self, now: float) -> None:
        for cookie_value, identity in list(self._by_cookie.items()):
            if now >= identity.expires_at:
                del self._by_cookie[cookie_value]
