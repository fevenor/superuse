"""Console entry for the packaged SuperuseMCP.exe.

stdout is the MCP protocol stream; diagnostics go to stderr. The exe must
keep its console (never --windowed) or stdio breaks.
"""

import sys

from superuse.mcp.server import main

raise SystemExit(main(sys.argv[1:]))
