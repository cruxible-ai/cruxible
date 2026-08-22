"""Shared CLI/MCP adapter for deterministic local source compilation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

import yaml

from cruxible_client import contracts
from cruxible_core.errors import DataValidationError
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.source_catalog import (
    SourceCatalog,
    SourceCompilationBundle,
    compile_source_catalog,
    merge_source_catalogs,
)


class WorkspaceSourceError(DataValidationError):
    """Local catalog paths or aliases are not a valid compilation request."""


class SourceContextClient(Protocol):
    def playbill_source_context(self, instance_id: str) -> contracts.PlaybillSourceContext: ...


def root_aliases(values: Iterable[str]) -> dict[str, Path]:
    aliases: dict[str, Path] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in aliases:
            raise WorkspaceSourceError("root aliases must be unique NAME=PATH values")
        aliases[name] = Path(path).expanduser()
    return aliases


def mapped_root_aliases(values: Mapping[str, Path]) -> dict[str, Path]:
    aliases: dict[str, Path] = {}
    for name, path in values.items():
        if not name or "=" in name or name in aliases:
            raise WorkspaceSourceError("root alias names must be unique nonempty text")
        aliases[name] = path
    return aliases


def load_source_catalog(
    portable_path: Path,
    local_path: Path | None,
) -> SourceCatalog:
    portable = SourceCatalog.model_validate(yaml.safe_load(portable_path.read_bytes()))
    local = (
        None
        if local_path is None
        else SourceCatalog.model_validate(yaml.safe_load(local_path.read_bytes()))
    )
    return merge_source_catalogs(portable, local)


def compile_client_source_context(
    client: SourceContextClient,
    instance_id: str,
    *,
    catalog: SourceCatalog,
    repository_root: Path,
    aliases: Mapping[str, Path],
) -> SourceCompilationBundle:
    """Read local bytes against path-free accepted context from the daemon."""

    context = client.playbill_source_context(instance_id)
    accepted = {
        shell.document_id: shell
        for value in context.documents
        for shell in (DocumentShell.model_validate(value),)
    }
    return compile_source_catalog(
        catalog,
        repository_root=repository_root,
        root_aliases=dict(aliases),
        accepted_base=AcceptedCoordinate.model_validate(
            context.accepted_coordinate.model_dump(mode="json")
        ),
        accepted_documents=accepted,
    )


__all__ = [
    "WorkspaceSourceError",
    "compile_client_source_context",
    "load_source_catalog",
    "mapped_root_aliases",
    "root_aliases",
]
