"""CLI router: mcp / serve / open / status / diagnose.

Only stdlib + the package version are imported up front: ``--version`` and
help must work in the base dev environment (no aiohttp/pyserial), so the
serve/mcp modules are imported at dispatch time (review R10).
"""

import argparse

from superuse import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="superuse",
        description=(
            "Superuse SuperKVM control broker. See README.md and docs/README.md."  # noqa: E501
        ),
    )
    parser.add_argument("--version", action="version", version=f"superuse {__version__}")
    sub = parser.add_subparsers(dest="command")

    mcp = sub.add_parser("mcp", help="run the MCP stdio bridge (no tray, no hardware)")
    mcp.add_argument("--client-label", default="agent")
    mcp.add_argument("--runtime-dir", type=str, default=None)
    mcp.set_defaults(which="mcp")

    # Kept in sync with superuse.serve.build_argparsers (duplicated inline so
    # parsing itself never imports aiohttp).
    serve = sub.add_parser(
        "serve",
        help="tray + broker; waits for the real device by default (simulated is dev-only)",
    )
    serve.add_argument("--config", default=None)
    serve.add_argument(
        "--real", action="store_true", help="compatibility no-op: real devices are the default"
    )
    serve.add_argument(
        "--simulated",
        action="store_true",
        help="development only: run simulated devices (rejected by release builds)",
    )
    serve.add_argument("--headless", action="store_true", help="no tray; run until Ctrl+C (CI)")
    serve.add_argument("--runtime-dir", default=None)
    serve.set_defaults(which="serve")

    opener = sub.add_parser("open", help="open the UI of the running broker")
    opener.add_argument("--runtime-dir", default=None)
    opener.set_defaults(which="open")

    status = sub.add_parser("status", help="sanitized broker status JSON")
    status.add_argument("--runtime-dir", default=None)
    status.set_defaults(which="status")

    diagnose = sub.add_parser("diagnose", help="read-only enumeration and diagnostics")
    diagnose.add_argument("--config", default=None)
    diagnose.add_argument("--output", default=None)
    diagnose.set_defaults(which="diagnose")

    args = parser.parse_args(argv)
    which = getattr(args, "which", None)
    if which == "mcp":
        from superuse.mcp.server import main as mcp_main

        forwarded = ["--client-label", args.client_label]
        if args.runtime_dir:
            forwarded += ["--runtime-dir", args.runtime_dir]
        return mcp_main(forwarded)
    if which is not None:
        from superuse.serve import dispatch

        return dispatch(args, parser)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
