"""CLI commands for source artifacts."""

from __future__ import annotations

from typing import cast

import click

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import (
    service_dereference_source_evidence,
    service_register_source_artifact,
)


@click.group("source")
def source_group() -> None:
    """Register and dereference source-backed evidence."""


@source_group.command("register")
@click.option("--path", "source_path", required=True, help="Local source path.")
@click.option(
    "--kind",
    "source_kind",
    type=click.Choice(["markdown"]),
    default="markdown",
    show_default=True,
    help="Source parser kind.",
)
@click.option(
    "--retention",
    "source_retention",
    type=click.Choice(["manifest_only", "archive"]),
    default="manifest_only",
    show_default=True,
    help="Whether to store only the manifest or archive source bytes.",
)
@click.option("--original-uri", default=None, help="Optional display-safe source URI.")
@click.option("--label", default=None, help="Optional display label.")
@json_option
@handle_errors
def register_source_artifact(
    source_path: str,
    source_kind: str,
    source_retention: str,
    original_uri: str | None,
    label: str | None,
    output_json: bool,
) -> None:
    """Register a source artifact for proposal evidence."""
    source_kind_value = cast(contracts.SourceKind, source_kind)
    source_retention_value = cast(contracts.SourceRetention, source_retention)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.register_source_artifact(
            instance_id,
            source_path=source_path,
            source_kind=source_kind_value,
            source_retention=source_retention_value,
            original_uri=original_uri,
            label=label,
        ),
        lambda instance: service_register_source_artifact(
            instance,
            source_path=source_path,
            source_kind=source_kind_value,
            source_retention=source_retention_value,
            original_uri=original_uri,
            label=label,
        ),
        allow_local=True,
        command_name="source register",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Source artifact: {result.source_artifact_id}")
    click.echo(f"  Retention: {result.source_retention}")
    click.echo(f"  Hash: {result.content_hash}")
    click.echo(f"  Chunks: {len(result.chunks)}")
    if result.archived:
        click.echo(f"  Archive hash: {result.archive_content_hash}")


@source_group.command("dereference")
@click.option("--artifact", "source_artifact_id", required=True, help="Source artifact ID.")
@click.option("--chunk", "chunk_id", default=None, help="Registered chunk ID.")
@click.option(
    "--heading",
    "heading_path",
    multiple=True,
    help="Heading path segment. Repeat for nested headings.",
)
@click.option("--block-selector", default=None, help="Block selector under heading path.")
@click.option("--expected-content-hash", default=None, help="Expected chunk content hash.")
@json_option
@handle_errors
def dereference_source_evidence(
    source_artifact_id: str,
    chunk_id: str | None,
    heading_path: tuple[str, ...],
    block_selector: str | None,
    expected_content_hash: str | None,
    output_json: bool,
) -> None:
    """Return source text for a registered source-evidence locator."""
    heading = list(heading_path) if heading_path else None
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.dereference_source_evidence(
            instance_id,
            source_artifact_id=source_artifact_id,
            chunk_id=chunk_id,
            heading_path=heading,
            block_selector=block_selector,
            expected_content_hash=expected_content_hash,
        ),
        lambda instance: service_dereference_source_evidence(
            instance,
            source_artifact_id=source_artifact_id,
            chunk_id=chunk_id,
            heading_path=heading,
            block_selector=block_selector,
            expected_content_hash=expected_content_hash,
        ),
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Status: {result.status}")
    if result.reason:
        click.echo(f"Reason: {result.reason}")
    if result.body is not None:
        click.echo(result.body)
