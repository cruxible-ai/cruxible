"""CLI command for deterministic wiki rendering."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import click

from cruxible_core.cli.commands import _common
from cruxible_core.cli.main import handle_errors
from cruxible_core.wiki import WikiOptions, render_wiki, write_wiki_pages
from cruxible_core.wiki.generator import SubjectRef, WikiScope, parse_subject_ref


def _parse_focus_values(values: tuple[str, ...]) -> tuple[SubjectRef, ...]:
    refs = []
    for raw in values:
        try:
            refs.append(parse_subject_ref(raw))
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--focus") from exc
    return tuple(refs)


@click.command("render-wiki")
@click.option(
    "--output",
    "output_dir",
    required=True,
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Directory to write the rendered wiki into.",
)
@click.option(
    "--focus",
    "focus_values",
    multiple=True,
    help="Render a focused wiki around EntityType:EntityId.",
)
@click.option(
    "--include-type",
    "include_types",
    multiple=True,
    help="Limit rendered subject pages to specific entity types.",
)
@click.option(
    "--scope",
    type=click.Choice(["local", "evidence", "all"]),
    default=None,
    help="Wiki projection scope. Defaults to local for CLI renders.",
)
@click.option(
    "--max-per-type",
    type=click.IntRange(min=1),
    default=50,
    show_default=True,
    help="Maximum subject links or high-fanout neighbors to render per type.",
)
@click.option(
    "--all-subjects",
    is_flag=True,
    default=False,
    help="Deprecated alias for --scope all.",
)
@handle_errors
def render_wiki_cmd(
    output_dir: Path,
    focus_values: tuple[str, ...],
    include_types: tuple[str, ...],
    scope: str | None,
    max_per_type: int,
    all_subjects: bool,
) -> None:
    """Render a deterministic Markdown wiki from the current state."""
    focus = _parse_focus_values(focus_values)
    effective_scope = cast(WikiScope, scope or ("all" if all_subjects else "local"))
    if all_subjects:
        if scope not in (None, "all"):
            raise click.UsageError("--all-subjects can only be combined with --scope all")
        click.echo(
            "Warning: --all-subjects is deprecated; use --scope all.",
            err=True,
        )
    client = _common._get_client()
    if client is not None:
        result = client.render_wiki(
            _common._require_instance_id(),
            focus=[ref.key for ref in focus],
            include_types=list(include_types),
            scope=effective_scope,
            max_per_type=max_per_type,
            all_subjects=all_subjects,
        )
        pages = {Path(page.path): page.content for page in result.pages}
        written = write_wiki_pages(output_dir, pages)
        click.echo(f"Rendered {len(written)} files into {output_dir}")
        return

    instance = _common._require_local_instance("render-wiki")
    options = WikiOptions(
        output_dir=output_dir,
        focus=focus,
        include_types=tuple(include_types),
        scope=effective_scope,
        max_per_type=max_per_type,
        all_subjects=all_subjects,
    )
    written = render_wiki(instance, options)
    click.echo(f"Rendered {len(written)} files into {output_dir}")
