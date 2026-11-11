"""Console-script launchers that refuse unsupported platforms first.

The CLI and the MCP server import POSIX-only modules (``fcntl``, ``pwd``,
``grp``) at module load, so on native Windows the import itself fails with a
traceback. These launchers check the platform before importing either one.
This module imports only the standard library; keep it that way.
"""

from __future__ import annotations

import sys

SUPPORTED_PLATFORMS_URL = (
    "https://github.com/cruxible-ai/cruxible/blob/main/docs/quickstart.md#supported-platforms"
)
WINDOWS_UNSUPPORTED_MESSAGE = (
    "Cruxible does not run natively on Windows yet. "
    f"Install and run it inside WSL2: {SUPPORTED_PLATFORMS_URL}"
)


def require_supported_platform() -> None:
    """Exit with a plain stderr message on native Windows; do nothing elsewhere."""
    if sys.platform == "win32":
        # stderr only: the MCP stdio protocol owns stdout.
        sys.stderr.write(WINDOWS_UNSUPPORTED_MESSAGE + "\n")
        sys.stderr.flush()
        raise SystemExit(1)


def cli() -> None:
    """Entry point for the ``cruxible`` console script."""
    require_supported_platform()
    from cruxible_core.cli.main import cli as _cli

    _cli()


def mcp() -> None:
    """Entry point for the ``cruxible-mcp`` console script and ``python -m cruxible_core.mcp``."""
    require_supported_platform()
    from cruxible_core.mcp.server import main as _main

    _main()
