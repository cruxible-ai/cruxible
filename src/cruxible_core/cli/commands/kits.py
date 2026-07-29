"""CLI commands for managing materialized kit metadata."""

from __future__ import annotations

from pathlib import Path

import click

from cruxible_core.cli.main import handle_errors
from cruxible_core.kits import repin_materialized_kit


@click.group("kit")
def kit_group() -> None:
    """Manage local materialized kits."""


@kit_group.command("repin")
@click.option(
    "--kit-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Materialized kit root whose runtime digest should be re-recorded.",
)
@handle_errors
def kit_repin_cmd(kit_dir: Path) -> None:
    """Accept intentional runtime-file edits in a materialized kit."""
    old_digest, new_digest = repin_materialized_kit(kit_dir)
    click.echo(f"Re-pinned kit runtime digest: {old_digest} -> {new_digest}")
