"""Network config, shared-key policy and LAN identity lifecycle (task T3)."""

from __future__ import annotations

import time

import pytest

from superuse.config import ConfigError, load_config
from superuse.core.errors import ControlError, ErrorCode
from superuse.server.network import (
    LAN_BROWSER_COOKIE,
    LanIdentityRegistry,
    NetworkAuthority,
)

# ----------------------------------------------------------------------
# Config: [network] defaults, validation and exactness
# ----------------------------------------------------------------------


def test_network_defaults_merge_when_section_absent():
    config = load_config()
    assert config.network == {
        "key": "",
        "web_port": 18765,
        "mcp_port": 18766,
        "lan_host": "0.0.0.0",
    }


def test_network_accepts_weak_keys_and_custom_ports():
    for key in ("a", "123", "  spaced  ", "中文密钥"):
        config = load_config(overrides={"network": {"key": key}})
        assert config.network["key"] == key  # exact value, no normalization
    config = load_config(overrides={"network": {"web_port": 9000, "mcp_port": 9001}})
    assert config.network["web_port"] == 9000


def test_network_rejects_bad_values():
    with pytest.raises(ConfigError, match="web_port"):
        load_config(overrides={"network": {"web_port": 70000}})
    with pytest.raises(ConfigError, match="web_port and mcp_port must differ"):
        load_config(overrides={"network": {"web_port": 18765, "mcp_port": 18765}})
    with pytest.raises(ConfigError, match="lan_host"):
        load_config(overrides={"network": {"lan_host": "example.com"}})
    with pytest.raises(ConfigError, match="lan_host"):
        load_config(overrides={"network": {"lan_host": "::1"}})
    with pytest.raises(ConfigError, match="must be a string"):
        load_config(overrides={"network": {"key": 123}})  # type: ignore[dict-item]
    with pytest.raises(ConfigError, match="unknown field"):
        load_config(overrides={"network": {"tls": True}})


def test_single_ipv4_lan_host_is_allowed():
    config = load_config(overrides={"network": {"lan_host": "192.168.1.20"}})
    assert config.network["lan_host"] == "192.168.1.20"


# ----------------------------------------------------------------------
# Shared key policy
# ----------------------------------------------------------------------


def test_empty_key_is_unconfigured_and_never_matches():
    authority = NetworkAuthority(key="")
    assert not authority.key_configured
    assert not authority.check_key("")
    assert not authority.check_key("a")
    assert not authority.check_key(None)
    assert authority.status_wire()["reason"] == "key_missing"
    assert "key" not in authority.status_wire() or authority.status_wire().get("key") is None


def test_weak_keys_authenticate_exactly():
    for key in ("a", "123"):
        authority = NetworkAuthority(key=key)
        assert authority.key_configured
        assert authority.check_key(key)
        assert not authority.check_key(key + " ")
        assert not authority.check_key("")
        assert not authority.check_key(None)
    # Case-sensitive when the key has letters; digits have no case to fold.
    authority = NetworkAuthority(key="aB9")
    assert authority.check_key("aB9")
    assert not authority.check_key("ab9")
    assert not authority.check_key("AB9")


def test_key_bytes_path_matches_utf8_encoding():
    authority = NetworkAuthority(key="中文密钥")
    assert authority.check_key_bytes("中文密钥".encode())
    assert not authority.check_key_bytes("中文密钥".encode("utf-16"))
    assert not authority.check_key_bytes(None)


def test_generation_bumps_only_on_toggle():
    authority = NetworkAuthority(key="k")
    assert authority.generation == 0
    authority.set_enabled(True)
    assert authority.enabled and authority.generation == 1
    authority.set_enabled(True)  # idempotent: no bump
    assert authority.generation == 1
    authority.set_enabled(False)
    assert not authority.enabled and authority.generation == 2
    wire = authority.status_wire(addresses=["192.168.1.9"])
    assert wire == {
        "enabled": False,
        "key_configured": True,
        "generation": 2,
        "reason": "disabled",
        "addresses": ["192.168.1.9"],
    }


# ----------------------------------------------------------------------
# LAN identities
# ----------------------------------------------------------------------


def make_registry(*, key="k", enabled=True):
    authority = NetworkAuthority(key=key)
    authority.set_enabled(enabled)
    return authority, LanIdentityRegistry(authority)


def test_login_exchanges_key_for_generation_bound_identity():
    authority, registry = make_registry(key="a")
    identity = registry.login("a")
    assert registry.authenticate(identity.cookie_value) is identity
    assert identity.generation == authority.generation


def test_wrong_key_rejected_with_clear_error():
    _, registry = make_registry(key="a")
    with pytest.raises(ControlError) as raised:
        registry.login("b")
    assert raised.value.code == ErrorCode.AUTH_FAILED


def test_login_requires_configured_key_and_enabled_network():
    unconfigured = LanIdentityRegistry(NetworkAuthority(key=""))
    with pytest.raises(ControlError):
        unconfigured.login("")
    authority = NetworkAuthority(key="a")  # never enabled
    with pytest.raises(ControlError) as raised:
        LanIdentityRegistry(authority).login("a")
    assert raised.value.code == ErrorCode.FORBIDDEN


def test_toggle_invalidates_old_identities_permanently():
    authority, registry = make_registry(key="a")
    identity = registry.login("a")
    authority.set_enabled(False)
    dropped = registry.invalidate_all()
    assert dropped == [identity]
    with pytest.raises(ControlError):
        registry.authenticate(identity.cookie_value)
    authority.set_enabled(True)  # re-enable must not resurrect it
    with pytest.raises(ControlError):
        registry.authenticate(identity.cookie_value)


def test_expired_identity_is_rejected_and_swept():
    authority, registry = make_registry(key="a")
    identity = registry.login("a")
    identity.expires_at = time.monotonic() - 1
    with pytest.raises(ControlError, match="expired"):
        registry.authenticate(identity.cookie_value)


def test_identity_capacity_is_bounded():
    authority, registry = make_registry(key="a")
    from superuse.server.network import LAN_MAX_IDENTITIES

    for _ in range(LAN_MAX_IDENTITIES):
        registry.login("a")
    with pytest.raises(ControlError) as raised:
        registry.login("a")
    assert raised.value.code == ErrorCode.RATE_LIMITED


def test_login_failure_cooldown_is_bounded():
    authority, registry = make_registry(key="a")
    from superuse.server.network import LAN_LOGIN_MAX_FAILURES

    for _ in range(LAN_LOGIN_MAX_FAILURES):
        with pytest.raises(ControlError):
            registry.login("nope")
    with pytest.raises(ControlError) as raised:
        registry.login("a")  # even the right key waits out the cooldown
    assert raised.value.code == ErrorCode.RATE_LIMITED


def test_cookie_namespaces_differ_from_local():
    from superuse.server.sessions import BROWSER_COOKIE

    assert LAN_BROWSER_COOKIE != BROWSER_COOKIE


def test_lan_page_sessions_are_tagged_and_closable():
    from superuse.server.sessions import Credentials, SessionRegistry

    registry = SessionRegistry(credentials=Credentials(), instance_id="i")
    session, record = registry.create_page_session(
        identity=None,
        csrf="csrf-1",
        label="lan-page",
        now=time.monotonic(),
        entrance="lan",
        network_generation=3,
    )
    assert record.entrance == "lan"
    assert record.network_generation == 3
    assert registry.authenticate_page(record.page_token) is session
    closed = registry.close_entrance("lan")
    assert [s.session_id for s in closed] == [session.session_id]
    with pytest.raises(ControlError):
        registry.authenticate_page(record.page_token)
    # Local pages are untouched by the LAN cleanup.
    from superuse.server.sessions import BrowserIdentity

    local_identity = BrowserIdentity(cookie_value="c", csrf="csrf-2")
    _, local = registry.create_page_session(
        identity=local_identity, csrf="csrf-2", label="local", now=time.monotonic()
    )
    registry.close_entrance("lan")
    assert registry.authenticate_page(local.page_token)
