"""Keymap data spot checks (ported from upstream, contract-aligned)."""

from __future__ import annotations

import pytest

from superuse.device.keymap import (
    char_to_hid,
    hid_for_code,
    is_modifier_code,
    modifier_bit,
    modifier_bits,
    normalize_text,
)


def test_modifier_bits_contract_names():
    assert modifier_bit("left_ctrl") == 0x01
    assert modifier_bit("left_shift") == 0x02
    assert modifier_bit("left_alt") == 0x04
    assert modifier_bit("left_gui") == 0x08
    assert modifier_bit("right_ctrl") == 0x10
    assert modifier_bit("right_shift") == 0x20
    assert modifier_bit("right_alt") == 0x40
    assert modifier_bit("right_gui") == 0x80
    assert modifier_bits(["left_ctrl", "left_alt"]) == 0x05
    with pytest.raises(KeyError):
        modifier_bit("ctrl")  # ad-hoc aliases intentionally removed


def test_codes():
    assert hid_for_code("KeyA") == 0x04
    assert hid_for_code("Enter") == 0x28
    assert hid_for_code("Delete") == 0x4C
    assert hid_for_code("IntlRo") == 0x87
    assert hid_for_code("NotAKey") is None
    assert is_modifier_code("ControlLeft")
    assert hid_for_code("ControlLeft") is None  # modifiers never in key slots


def test_char_map_us_ascii():
    assert char_to_hid("a") == (0x00, 0x04)
    assert char_to_hid("A") == (0x02, 0x04)
    assert char_to_hid("1") == (0x00, 0x1E)
    assert char_to_hid("!") == (0x02, 0x1E)
    assert char_to_hid(" ") == (0x00, 0x2C)
    assert char_to_hid("\n") == (0x00, 0x28)
    assert char_to_hid("\t") == (0x00, 0x2B)
    assert char_to_hid("?") == (0x02, 0x38)
    # Every printable ASCII byte maps.
    for code in range(0x20, 0x7F):
        assert char_to_hid(chr(code)) is not None, hex(code)


def test_normalize_text():
    assert normalize_text("a\r\nb\rc\nd") == "a\nb\nc\nd"
