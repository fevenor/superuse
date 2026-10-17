"""Session, credential and one-time bootstrap registry.

Threat model per docs/development/windows-operations.md §3: local loopback plus bearer
secrets. Roles come from the creating endpoint, never from request bodies or
headers. All tokens are >=256-bit random values; none are ever logged.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

from superuse.core.errors import ControlError, ErrorCode
from superuse.core.models import Session, SessionKind

BROWSER_COOKIE = "superuse_browser"
SESSION_TTL_S = 15.0
BOOTSTRAP_TTL_S = 30.0
HEARTBEAT_INTERVAL_S = 5.0


def new_token() -> str:
    return secrets.token_urlsafe(32)  # 43 chars, >=256 bits


@dataclass
class AgentRecord:
    session: Session
    token: str
    expires_at: float
    event_connections: int = 0


@dataclass
class PageRecord:
    session: Session
    page_token: str
    csrf: str
    browser: BrowserIdentity
    expires_at: float
    event_connections: int = 0
    input_connection: bool = False
    # Entrance/origin tagging (task 2026-10-17 T3): "local" pages come from
    # the loopback UI bootstrap; "lan" pages were minted from a LAN identity
    # and die with their network generation.
    entrance: str = "local"
    network_generation: int | None = None
    # LAN pages remember which browser identity minted them so a logout can
    # revoke exactly that browser's sessions (review 2026-10-17 R4).
    owner_identity: str | None = None
    # Live human sockets for this page (events/video/input), so a logout or
    # shutdown can close them instead of waiting for client-side teardown.
    websockets: set = field(default_factory=set)


@dataclass
class BrowserIdentity:
    cookie_value: str
    csrf: str


# LAN page records have no loopback BrowserIdentity; the record's own csrf and
# the network generation carry their validity (task 2026-10-17 T3).
_LAN_PLACEHOLDER = BrowserIdentity(cookie_value="", csrf="")


@dataclass
class Credentials:
    """Runtime credentials written to the broker.json runtime file."""

    agent_bootstrap: str = field(default_factory=new_token)
    launcher: str = field(default_factory=new_token)


class SessionRegistry:
    def __init__(self, *, credentials: Credentials, instance_id: str) -> None:
        self._credentials = credentials
        self._instance_id = instance_id
        self._agents_by_token: dict[str, AgentRecord] = {}
        self._pages_by_token: dict[str, PageRecord] = {}
        self._browsers: dict[str, BrowserIdentity] = {}
        self._bootstraps: dict[str, float] = {}  # value -> expiry

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    # -- agent sessions --------------------------------------------------------

    def create_agent_session(
        self, *, bootstrap_token: str, label: str, now: float
    ) -> tuple[Session, str]:
        if not secrets.compare_digest(bootstrap_token, self._credentials.agent_bootstrap):
            raise ControlError(ErrorCode.AUTH_FAILED, "Bad agent bootstrap token")
        session = Session(
            session_id=str(uuid.uuid4()), kind=SessionKind.AGENT, label=(label or "agent")[:80]
        )
        token = new_token()
        self._agents_by_token[token] = AgentRecord(session, token, now + SESSION_TTL_S)
        return session, token

    def authenticate_agent(self, bearer: str | None) -> Session:
        if not bearer or not bearer.startswith("Bearer "):
            raise ControlError(ErrorCode.AUTH_FAILED, "Missing bearer token")
        record = self._agents_by_token.get(bearer[7:])
        if record is None or time.monotonic() >= record.expires_at:
            raise ControlError(ErrorCode.AUTH_FAILED, "Unknown or expired agent session")
        return record.session

    def agent_record(self, session_id: str) -> AgentRecord | None:
        for record in self._agents_by_token.values():
            if record.session.session_id == session_id:
                return record
        return None

    # -- browser identity / page sessions --------------------------------------

    def issue_bootstrap(self, now: float) -> str:
        value = new_token()
        self._bootstraps[value] = now + BOOTSTRAP_TTL_S
        return value

    def exchange_bootstrap(self, bootstrap: str, now: float) -> BrowserIdentity:
        expiry = self._bootstraps.pop(bootstrap, None)
        if expiry is None or now > expiry:
            raise ControlError(ErrorCode.AUTH_FAILED, "Bootstrap invalid or expired")
        cookie_value = new_token()
        identity = BrowserIdentity(cookie_value=cookie_value, csrf=new_token())
        self._browsers[cookie_value] = identity
        return identity

    def browser_identity(self, cookie_value: str | None) -> BrowserIdentity:
        if not cookie_value:
            raise ControlError(ErrorCode.AUTH_FAILED, "No browser identity")
        identity = self._browsers.get(cookie_value)
        if identity is None:
            raise ControlError(ErrorCode.AUTH_FAILED, "Unknown browser identity")
        return identity

    def create_page_session(
        self,
        *,
        identity: BrowserIdentity | None = None,
        csrf: str,
        label: str,
        now: float,
        entrance: str = "local",
        network_generation: int | None = None,
        owner_identity: str | None = None,
    ) -> tuple[Session, PageRecord]:
        if entrance == "local":
            if identity is None:
                raise ControlError(ErrorCode.AUTH_FAILED, "No browser identity")
            if not secrets.compare_digest(csrf, identity.csrf):
                raise ControlError(ErrorCode.FORBIDDEN, "CSRF mismatch")
            browser = identity
        else:
            browser = _LAN_PLACEHOLDER
        session = Session(
            session_id=str(uuid.uuid4()), kind=SessionKind.HUMAN, label=(label or "page")[:80]
        )
        page_token = new_token()
        record = PageRecord(
            session=session,
            page_token=page_token,
            csrf=csrf,
            browser=browser,
            expires_at=now + SESSION_TTL_S,
            entrance=entrance,
            network_generation=network_generation,
            owner_identity=owner_identity,
        )
        self._pages_by_token[page_token] = record
        return session, record

    def authenticate_page(self, page_token: str | None) -> Session:
        if not page_token:
            raise ControlError(ErrorCode.AUTH_FAILED, "Missing page token")
        record = self._pages_by_token.get(page_token)
        if record is None or time.monotonic() >= record.expires_at:
            raise ControlError(ErrorCode.AUTH_FAILED, "Unknown or expired page session")
        return record.session

    def page_record(self, session_id: str) -> PageRecord | None:
        for record in self._pages_by_token.values():
            if record.session.session_id == session_id:
                return record
        return None

    def page_record_by_token(self, page_token: str) -> PageRecord | None:
        return self._pages_by_token.get(page_token)

    def lan_page_records(self, *, network_generation: int | None = None) -> list[PageRecord]:
        """LAN-minted pages, optionally only those of one network generation."""
        return [
            record
            for record in self._pages_by_token.values()
            if record.entrance == "lan"
            and (network_generation is None or record.network_generation == network_generation)
        ]

    def close_entrance(self, entrance: str) -> list[Session]:
        """Drop every session of one entrance; returns them for owner cleanup."""
        closed: list[Session] = []
        for token, record in list(self._pages_by_token.items()):
            if record.entrance == entrance:
                closed.append(record.session)
                del self._pages_by_token[token]
        return closed

    def close_session(self, session_id: str) -> None:
        for token, record in list(self._agents_by_token.items()):
            if record.session.session_id == session_id:
                del self._agents_by_token[token]
        for token, record in list(self._pages_by_token.items()):
            if record.session.session_id == session_id:
                del self._pages_by_token[token]

    # -- heartbeat and expiry ----------------------------------------------------

    def heartbeat(self, session: Session) -> int:
        now = time.monotonic()
        record = (
            self.agent_record(session.session_id) if session.kind is SessionKind.AGENT else None
        )
        if record is None:
            record_p = self.page_record(session.session_id)
            if record_p is None:
                raise ControlError(ErrorCode.AUTH_FAILED, "Unknown session")
            record_p.expires_at = now + SESSION_TTL_S
            return int(SESSION_TTL_S * 1000)
        record.expires_at = now + SESSION_TTL_S
        return int(SESSION_TTL_S * 1000)

    def sweep(self, now: float) -> list[Session]:
        """Drop expired sessions; returns them for owner cleanup."""
        expired: list[Session] = []
        for token, record in list(self._agents_by_token.items()):
            if now >= record.expires_at:
                expired.append(record.session)
                del self._agents_by_token[token]
        for token, record in list(self._pages_by_token.items()):
            if now >= record.expires_at:
                expired.append(record.session)
                del self._pages_by_token[token]
        for value, expiry in list(self._bootstraps.items()):
            if now > expiry:
                del self._bootstraps[value]
        return expired


def parse_cookie(header: str | None) -> dict[str, str]:
    if not header:
        return {}
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:  # pragma: no cover - malformed cookie headers
        return {}
    return {key: morsel.value for key, morsel in cookie.items()}
