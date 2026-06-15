"""CLI command for rendered config review surfaces."""
# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

from pathlib import Path

import click

from cruxible_core.canonical_views.config import (
    MissingReadmeMarkersError,
    available_view_keys,
    load_config_for_rendering,
    render_config_views,
    selected_view_keys,
    update_readme_file,
)
from cruxible_core.cli.main import handle_errors


@click.command("config-views")
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to config YAML file.",
)
@click.option(
    "--view",
    type=click.Choice(("all", *available_view_keys())),
    default="all",
    show_default=True,
    help="View to render. 'all' emits the standard config-drafting diagrams.",
)
@click.option(
    "--bare",
    is_flag=True,
    help="Emit the raw selected view without Markdown wrapping.",
)
@click.option(
    "--update-readme",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Replace matching CRUXIBLE marker blocks in a README.",
)
@click.option(
    "--runtime",
    is_flag=True,
    help=(
        "Compose extends overlays as a runtime composed view. This includes inherited "
        "ontology/query surfaces but strips upstream build-only workflows."
    ),
)
@handle_errors
def config_views_cmd(
    config_path: Path,
    view: str,
    bare: bool,
    update_readme: Path | None,
    runtime: bool,
) -> None:
    """Render canonical Mermaid/Markdown views for a Cruxible config."""
    config = load_config_for_rendering(config_path, runtime=runtime)

    selected_keys = selected_view_keys(view)
    if update_readme is not None:
        try:
            update_readme_file(update_readme, config, selected_keys)
        except MissingReadmeMarkersError as exc:
            raise click.UsageError(str(exc)) from exc
        click.echo(f"Updated {update_readme}")
        return

    click.echo(
        render_config_views(
            config,
            view=view,
            source=config_path,
            bare=bare,
        )
    )
