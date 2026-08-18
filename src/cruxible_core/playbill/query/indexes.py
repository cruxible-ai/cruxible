"""Disposable, grep-friendly discovery indexes derived from the materialized view.

An index is a presentation projection and nothing else. It carries addresses,
the accepted coordinate, labels, and match text so a file explorer or a plain
``grep`` can find a semantic object; it carries no verdict, no currency, no
authority, no lifecycle, and no locator or secret, because a coordinate-pure
file cannot honestly hold a time-relative or access-controlled fact.

Every byte is a pure function of the F3 materialized Subject view and the
accepted vocabulary built beside it, so deleting the whole index directory and
rebuilding it from accepted projection facts reproduces the same digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.discovery import reject_locator_or_secret
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.backends import SubjectQueryViewV1
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.query.semantic_discovery import (
    DISCOVERY_ENTRY_KINDS,
    DiscoveryEntryV1,
    DiscoveryError,
    DiscoveryVocabularyV1,
)

INDEX_MARKDOWN_NAME = "INDEX.md"
DISCOVERY_JSONL_NAME = "discovery.jsonl"
DISCOVERY_INDEX_FILE_NAMES: tuple[str, ...] = (DISCOVERY_JSONL_NAME, INDEX_MARKDOWN_NAME)
DISCOVERY_INDEX_DIGEST_DOMAIN = "playbill-discovery-index-v1"


class _StrictIndexModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscoveryIndexFileV1(_StrictIndexModel):
    """One generated presentation file, addressed by content rather than by path."""

    tag: Literal["playbill-discovery-index-file-v1"] = "playbill-discovery-index-file-v1"
    name: str
    content_digest: str
    byte_length: int = Field(ge=0)

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class DiscoveryIndexManifestV1(_StrictIndexModel):
    """The rebuild commitment of one disposable local index."""

    tag: Literal["playbill-discovery-index-manifest-v1"] = "playbill-discovery-index-manifest-v1"
    at: AcceptedCoordinate
    files: tuple[DiscoveryIndexFileV1, ...]
    index_digest: str

    @field_validator("files")
    @classmethod
    def _files(cls, value: tuple[DiscoveryIndexFileV1, ...]) -> tuple[DiscoveryIndexFileV1, ...]:
        names = tuple(item.name for item in value)
        if names != byte_sorted(names):
            raise ValueError("discovery index files must be sorted and unique by name")
        return value

    @field_validator("index_digest")
    @classmethod
    def _index_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


def _terms(values: tuple[str, ...]) -> str:
    return ",".join(values)


def _entry_line(entry: DiscoveryEntryV1) -> str:
    return " ".join(
        (
            f"kind={entry.kind}",
            f"path={entry.address.artifact_path}",
            f"selector={entry.address.selector.scheme}",
            f"identity={entry.identity}",
            f"label={entry.label}",
            f"entrypoint={entry.entrypoint_name or ''}",
            f"aliases={_terms(entry.aliases)}",
            f"tags={_terms(entry.tags)}",
            f"match={_terms(entry.lexical_terms)}",
        )
    )


def _render_markdown(view: SubjectQueryViewV1, vocabulary: DiscoveryVocabularyV1) -> bytes:
    at = vocabulary.at
    lines = [
        "# Playbill discovery index",
        "",
        "Disposable presentation projection: coordinate-pure naming and match text only.",
        "",
        (
            f"coordinate git_oid={at.git_oid} semantic_root={at.semantic_root} "
            f"generation_root={at.generation_root} compiler_digest={at.compiler_digest}"
        ),
        (
            f"view subjects={len(view.subjects)} claims={len(view.claims)} "
            f"adjacency={len(view.adjacency)}"
        ),
        (
            f"vocabulary entries={len(vocabulary.entries)} "
            f"excluded_claims={vocabulary.excluded_claim_count}"
        ),
        "",
    ]
    for kind in DISCOVERY_ENTRY_KINDS:
        entries = tuple(item for item in vocabulary.entries if item.kind == kind)
        lines.extend((f"## {kind}", ""))
        lines.extend(_entry_line(entry) for entry in entries)
        lines.append("")
    lines.extend(("## Adjacency", ""))
    lines.extend(
        " ".join(
            (
                f"subject={row.subject_path}",
                f"predicate={row.predicate}",
                f"asserted={len(row.asserted_claim_paths)}",
                f"incident={len(row.incident_claim_paths)}",
            )
        )
        for row in view.adjacency
    )
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _render_jsonl(vocabulary: DiscoveryVocabularyV1) -> bytes:
    at = vocabulary.at.model_dump(mode="json")
    rows = [
        canonical_bytes(
            {
                "address": entry.address.model_dump(mode="json"),
                "aliases": list(entry.aliases),
                "at": at,
                "dependency_addresses": [
                    item.model_dump(mode="json") for item in entry.dependency_addresses
                ],
                "dependent_addresses": [
                    item.model_dump(mode="json") for item in entry.dependent_addresses
                ],
                "description": entry.description,
                "entrypoint_name": entry.entrypoint_name,
                "identity": entry.identity,
                "kind": entry.kind,
                "label": entry.label,
                "match_text": list(entry.lexical_terms),
                "tags": list(entry.tags),
            }
        )
        for entry in vocabulary.entries
    ]
    return b"".join(row + b"\n" for row in rows)


def render_discovery_index(
    *,
    view: SubjectQueryViewV1,
    vocabulary: DiscoveryVocabularyV1,
) -> dict[str, bytes]:
    """Render the deterministic index files for one accepted coordinate."""

    if vocabulary.at != AcceptedCoordinate.from_internal(view.coordinate):
        raise DiscoveryError("discovery index requires one accepted coordinate")
    files = {
        DISCOVERY_JSONL_NAME: _render_jsonl(vocabulary),
        INDEX_MARKDOWN_NAME: _render_markdown(view, vocabulary),
    }
    for name, content in files.items():
        reject_locator_or_secret(content.decode("utf-8"), label=f"discovery index {name}")
    return files


def discovery_index_digest(files: Mapping[str, bytes]) -> str:
    """Digest the complete file set so a rebuild can be compared byte for byte."""

    return typed_digest(
        Sha256Value,
        DISCOVERY_INDEX_DIGEST_DOMAIN,
        {
            "files": [
                {
                    "byte_length": len(files[name]),
                    "content_digest": Sha256Value(hashlib.sha256(files[name]).hexdigest()).tagged,
                    "name": name,
                }
                for name in sorted(files, key=lambda item: item.encode("utf-8"))
            ]
        },
    ).tagged


def discovery_index_manifest(
    files: Mapping[str, bytes],
    *,
    at: AcceptedCoordinate,
) -> DiscoveryIndexManifestV1:
    """Return the rebuild commitment of one rendered index file set."""

    return DiscoveryIndexManifestV1(
        at=at,
        files=tuple(
            DiscoveryIndexFileV1(
                name=name,
                content_digest=Sha256Value(hashlib.sha256(files[name]).hexdigest()).tagged,
                byte_length=len(files[name]),
            )
            for name in sorted(files, key=lambda item: item.encode("utf-8"))
        ),
        index_digest=discovery_index_digest(files),
    )


def write_discovery_index(
    root: Path,
    files: Mapping[str, bytes],
    *,
    at: AcceptedCoordinate,
) -> DiscoveryIndexManifestV1:
    """Materialize the disposable index under ``root`` and return its commitment."""

    if root.is_symlink():
        raise DiscoveryError("discovery index root must be a regular directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in sorted(files, key=lambda item: item.encode("utf-8")):
        if name not in DISCOVERY_INDEX_FILE_NAMES:
            raise DiscoveryError(f"unregistered discovery index file name: {name!r}")
        (root / name).write_bytes(files[name])
    return discovery_index_manifest(files, at=at)


def delete_discovery_index(root: Path) -> None:
    """Delete every generated index file; accepted state is untouched by design."""

    for name in DISCOVERY_INDEX_FILE_NAMES:
        (root / name).unlink(missing_ok=True)


def load_discovery_index(root: Path) -> dict[str, bytes]:
    """Read back whichever generated index files are currently materialized."""

    return {
        name: (root / name).read_bytes()
        for name in DISCOVERY_INDEX_FILE_NAMES
        if (root / name).is_file()
    }


def parse_discovery_index_rows(content: bytes) -> tuple[dict[str, object], ...]:
    """Parse ``discovery.jsonl`` back into rows for explorer and parity checks."""

    rows: list[dict[str, object]] = []
    for line in content.split(b"\n"):
        if not line:
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise DiscoveryError("discovery index row is not a JSON object")
        rows.append(parsed)
    return tuple(rows)


__all__ = [
    "DISCOVERY_INDEX_DIGEST_DOMAIN",
    "DISCOVERY_INDEX_FILE_NAMES",
    "DISCOVERY_JSONL_NAME",
    "INDEX_MARKDOWN_NAME",
    "DiscoveryIndexFileV1",
    "DiscoveryIndexManifestV1",
    "delete_discovery_index",
    "discovery_index_digest",
    "discovery_index_manifest",
    "load_discovery_index",
    "parse_discovery_index_rows",
    "render_discovery_index",
    "write_discovery_index",
]
