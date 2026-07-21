"""CLI interface — secondary interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_core.cli.instance import CruxibleInstance
    from cruxible_core.cli.main import cli

__all__ = ["CruxibleInstance", "cli"]


def __getattr__(name: str) -> Any:
    """Keep importing the CLI package itself free of runtime engine imports."""
    if name == "CruxibleInstance":
        from cruxible_core.cli.instance import CruxibleInstance

        return CruxibleInstance
    if name == "cli":
        from cruxible_core.cli.main import cli

        return cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
