"""HID keycode tables ported from upstream serial-hid-kvm (MIT).

Data sources (see third_party/CHANGES.md):
- ``_web_viewer.py`` ``_JS_CODE_TO_HID``: W3C ``KeyboardEvent.code`` names.
- ``hid_keycodes.py`` US104 ``_CHAR_MAP``: ASCII to (modifier, keycode).

Changes versus upstream: contract modifier names (left_ctrl/.../right_gui)
replace the ad-hoc alias table; global mutable layout state was dropped —
this module is pure data and pure functions. Modifier codes
(ControlLeft/ShiftLeft/...) deliberately have no entry in CODE_TO_HID: the
contract models them as the ``modifiers`` field, not as key slots.
"""

from __future__ import annotations

# W3C KeyboardEvent.code -> HID usage ID (non-modifier keys only)
CODE_TO_HID: dict[str, int] = {
    "Escape": 0x29,
    "F1": 0x3A,
    "F2": 0x3B,
    "F3": 0x3C,
    "F4": 0x3D,
    "F5": 0x3E,
    "F6": 0x3F,
    "F7": 0x40,
    "F8": 0x41,
    "F9": 0x42,
    "F10": 0x43,
    "F11": 0x44,
    "F12": 0x45,
    "PrintScreen": 0x46,
    "ScrollLock": 0x47,
    "Pause": 0x48,
    "Backquote": 0x35,
    "Digit1": 0x1E,
    "Digit2": 0x1F,
    "Digit3": 0x20,
    "Digit4": 0x21,
    "Digit5": 0x22,
    "Digit6": 0x23,
    "Digit7": 0x24,
    "Digit8": 0x25,
    "Digit9": 0x26,
    "Digit0": 0x27,
    "Minus": 0x2D,
    "Equal": 0x2E,
    "Backspace": 0x2A,
    "Tab": 0x2B,
    "KeyQ": 0x14,
    "KeyW": 0x1A,
    "KeyE": 0x08,
    "KeyR": 0x15,
    "KeyT": 0x17,
    "KeyY": 0x1C,
    "KeyU": 0x18,
    "KeyI": 0x0C,
    "KeyO": 0x12,
    "KeyP": 0x13,
    "BracketLeft": 0x2F,
    "BracketRight": 0x30,
    "Backslash": 0x31,
    "CapsLock": 0x39,
    "KeyA": 0x04,
    "KeyS": 0x16,
    "KeyD": 0x07,
    "KeyF": 0x09,
    "KeyG": 0x0A,
    "KeyH": 0x0B,
    "KeyJ": 0x0D,
    "KeyK": 0x0E,
    "KeyL": 0x0F,
    "Semicolon": 0x33,
    "Quote": 0x34,
    "Enter": 0x28,
    "KeyZ": 0x1D,
    "KeyX": 0x1B,
    "KeyC": 0x06,
    "KeyV": 0x19,
    "KeyB": 0x05,
    "KeyN": 0x11,
    "KeyM": 0x10,
    "Comma": 0x36,
    "Period": 0x37,
    "Slash": 0x38,
    "Space": 0x2C,
    "Insert": 0x49,
    "Home": 0x4A,
    "PageUp": 0x4B,
    "Delete": 0x4C,
    "End": 0x4D,
    "PageDown": 0x4E,
    "ArrowUp": 0x52,
    "ArrowDown": 0x51,
    "ArrowLeft": 0x50,
    "ArrowRight": 0x4F,
    "NumLock": 0x53,
    "NumpadDivide": 0x54,
    "NumpadMultiply": 0x55,
    "NumpadSubtract": 0x56,
    "NumpadAdd": 0x57,
    "NumpadEnter": 0x58,
    "NumpadDecimal": 0x63,
    "Numpad0": 0x62,
    "Numpad1": 0x59,
    "Numpad2": 0x5A,
    "Numpad3": 0x5B,
    "Numpad4": 0x5C,
    "Numpad5": 0x5D,
    "Numpad6": 0x5E,
    "Numpad7": 0x5F,
    "Numpad8": 0x60,
    "Numpad9": 0x61,
    "IntlBackslash": 0x64,
    "IntlRo": 0x87,
    "IntlYen": 0x89,
    "KanaMode": 0x88,
    "Convert": 0x8A,
    "NonConvert": 0x8B,
    "Lang1": 0x90,
    "Lang2": 0x91,
    "ContextMenu": 0x65,
}

# Contract modifier enum -> HID modifier byte bit
MODIFIER_BITS: dict[str, int] = {
    "left_ctrl": 0x01,
    "left_shift": 0x02,
    "left_alt": 0x04,
    "left_gui": 0x08,
    "right_ctrl": 0x10,
    "right_shift": 0x20,
    "right_alt": 0x40,
    "right_gui": 0x80,
}

# Modifier keys expressed as W3C codes must go through the modifiers field.
MODIFIER_CODES = frozenset(
    {
        "ControlLeft",
        "ControlRight",
        "ShiftLeft",
        "ShiftRight",
        "AltLeft",
        "AltRight",
        "MetaLeft",
        "MetaRight",
    }
)

# US104 ASCII map: char -> (modifier bits, HID keycode)
_CHAR_MAP: dict[str, tuple[int, int]] = {}

for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _CHAR_MAP[_c] = (0x00, 0x04 + _i)
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _CHAR_MAP[_c] = (MODIFIER_BITS["left_shift"], 0x04 + _i)
for _i, _c in enumerate("123456789"):
    _CHAR_MAP[_c] = (0x00, 0x1E + _i)
_CHAR_MAP["0"] = (0x00, 0x27)

for _c, _k in {
    "!": 0x1E,
    "@": 0x1F,
    "#": 0x20,
    "$": 0x21,
    "%": 0x22,
    "^": 0x23,
    "&": 0x24,
    "*": 0x25,
    "(": 0x26,
    ")": 0x27,
}.items():
    _CHAR_MAP[_c] = (MODIFIER_BITS["left_shift"], _k)

_CHAR_MAP.update(
    {
        " ": (0x00, 0x2C),
        "-": (0x00, 0x2D),
        "=": (0x00, 0x2E),
        "[": (0x00, 0x2F),
        "]": (0x00, 0x30),
        "\\": (0x00, 0x31),
        ";": (0x00, 0x33),
        "'": (0x00, 0x34),
        "`": (0x00, 0x35),
        ",": (0x00, 0x36),
        ".": (0x00, 0x37),
        "/": (0x00, 0x38),
    }
)

_CHAR_MAP.update(
    {
        "_": (MODIFIER_BITS["left_shift"], 0x2D),
        "+": (MODIFIER_BITS["left_shift"], 0x2E),
        "{": (MODIFIER_BITS["left_shift"], 0x2F),
        "}": (MODIFIER_BITS["left_shift"], 0x30),
        "|": (MODIFIER_BITS["left_shift"], 0x31),
        ":": (MODIFIER_BITS["left_shift"], 0x33),
        '"': (MODIFIER_BITS["left_shift"], 0x34),
        "~": (MODIFIER_BITS["left_shift"], 0x35),
        "<": (MODIFIER_BITS["left_shift"], 0x36),
        ">": (MODIFIER_BITS["left_shift"], 0x37),
        "?": (MODIFIER_BITS["left_shift"], 0x38),
    }
)

_CHAR_MAP["\t"] = (0x00, 0x2B)
_CHAR_MAP["\n"] = (0x00, 0x28)


def hid_for_code(code: str) -> int | None:
    """HID usage for a W3C physical code, or None if unknown/modifier-only."""
    return CODE_TO_HID.get(code)


def is_modifier_code(code: str) -> bool:
    return code in MODIFIER_CODES


def modifier_bit(name: str) -> int:
    """Contract modifier name -> bitmask; raises KeyError for unknown names."""
    return MODIFIER_BITS[name]


def modifier_bits(names: tuple[str, ...] | list[str]) -> int:
    bits = 0
    for name in names:
        bits |= MODIFIER_BITS[name]
    return bits


def char_to_hid(char: str) -> tuple[int, int] | None:
    """US ASCII char -> (modifier bits, HID keycode); None when unmappable."""
    return _CHAR_MAP.get(char)


def normalize_text(text: str) -> str:
    """Contract normalization: CRLF and CR become LF before expansion."""
    return text.replace("\r\n", "\n").replace("\r", "\n")
