"""Windowed tray entry for the packaged Superuse.exe.

Defaults to `serve` when launched without arguments (double-click); all CLI
arguments still route through the standard command parser.
"""

import sys

from superuse.__main__ import main

argv = sys.argv[1:]
if not argv:
    argv = ["serve"]
try:
    result = main(argv)
except (OSError, ValueError) as exc:
    error = str(exc)
except SystemExit as exc:
    if not isinstance(exc.code, str):
        raise
    error = exc.code
else:
    raise SystemExit(result)

# A windowed portable app must explain an unwritable data directory even
# when it has no console. CLI/stdout behavior is unchanged when attached.
if (
    sys.platform == "win32"
    and sys.stderr is None
    and argv[0] == "serve"
    and "--headless" not in argv
):
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, error, "Superuse", 0x10)
elif sys.stderr is not None:
    print(error, file=sys.stderr)
raise SystemExit(1)
