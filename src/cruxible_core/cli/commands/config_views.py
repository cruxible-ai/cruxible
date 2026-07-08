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
    resolve_overlay_scope,
    selected_view_keys,
    update_readme_file,
)
from cruxible_core.cli.main import handle_errors


@click.command("views")
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
        "ontology/query surfaces but strips upstream build-only workflows. The "
        "ontology view renders overlay-scoped (own structure + ghost base seam "
        "endpoints); use --composed-ontology for the full composed diagram."
    ),
)
@click.option(
    "--composed-ontology",
    is_flag=True,
    help=(
        "With --runtime on an extends config: render the full composed ontology instead "
        "of the overlay-scoped view."
    ),
)
@handle_errors
def config_views_cmd(
    config_path: Path,
    view: str,
    bare: bool,
    update_readme: Path | None,
    runtime: bool,
    composed_ontology: bool,
) -> None:
    """Render canonical Mermaid/Markdown views for a Cruxible config."""
    config = load_config_for_rendering(config_path, runtime=runtime)
    overlay_scope = None
    if runtime and not composed_ontology:
        overlay_scope = resolve_overlay_scope(config_path)

    selected_keys = selected_view_keys(view)
    if update_readme is not None:
        try:
            update_readme_file(update_readme, config, selected_keys, overlay_scope)
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
            overlay_scope=overlay_scope,
        )
    )


@click.command("expand")
@click.option(
    "--in",
    "in_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the compact authoring YAML to expand.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the expanded explicit YAML here (default: stdout).",
)
@click.option(
    "--validate/--no-validate",
    "validate_output",
    default=True,
    show_default=True,
    help="Validate the expanded config as a CoreConfig before writing.",
)
@handle_errors
def config_expand_cmd(
    in_path: Path,
    out_path: Path | None,
    validate_output: bool,
) -> None:
    """Expand a compact authoring config to the explicit engine config.

    The compact form is the single source of truth: the loader expands it on load,
    so this is a deterministic inspection/review transform (e.g. to diff the resolved
    explicit graph), not a committed artifact.
    """
    from cruxible_core.config.compact import dump_expanded, expand_compact_file_full

    result = expand_compact_file_full(in_path)

    if validate_output:
        from cruxible_core.config.schema import CoreConfig

        CoreConfig.model_validate(result.config)

    rendered = dump_expanded(result.config)

    for warning in result.warnings:
        click.echo(f"warning: {warning}", err=True)
    if result.metadata:
        click.echo(f"metadata (stripped from engine config): {result.metadata}", err=True)

    if out_path is not None:
        out_path.write_text(rendered, encoding="utf-8")
        click.echo(f"Wrote {out_path}")
    else:
        click.echo(rendered, nl=False)
