"""CLI interface — secondary interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    CruxibleInstance: Any
    cli: Any
else:
    from cruxible_core.cli.instance import CruxibleInstance
    from cruxible_core.cli.main import cli

__all__ = ["CruxibleInstance", "cli"]
