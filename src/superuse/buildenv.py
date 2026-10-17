"""Release-build boundary.

`sys.frozen` is set by PyInstaller in the packaged EXEs and cannot be flipped
from a normal user environment; it is the single authority deciding whether
simulated devices may exist in this process. Development checkouts and test
suites run unfrozen and may opt into simulation explicitly.
"""

from __future__ import annotations

import sys


def is_frozen_build() -> bool:
    return bool(getattr(sys, "frozen", False))


def simulated_devices_allowed() -> bool:
    """Simulated hardware exists only in development/test processes."""
    return not is_frozen_build()
