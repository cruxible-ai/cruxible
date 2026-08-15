"""Playbill CLI interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_core.cli.main import cli

__all__ = ["cli"]


def __getattr__(name: str) -> Any:
    """Keep importing the CLI package itself free of runtime engine imports."""
    if name == "cli":
        from cruxible_core.cli.main import cli

        return cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
