"""Windowed tray entry for the packaged Superuse.exe.

Defaults to `serve` when launched without arguments (double-click); all CLI
arguments still route through the standard command parser.
"""

import sys

from superuse.__main__ import main

argv = sys.argv[1:]
if not argv:
    argv = ["serve"]
raise SystemExit(main(argv))
