"""Local-only source catalogs, exact-byte compilation, and alignment reporting."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.canonical import (
    CasDigest,
    Sha256Value,
    typed_digest,
)
from cruxible_core.playbill.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
    document_path,
    render_document,
)
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.projection import AcceptedCoordinate

SourceCatalogKind = Literal["portable", "local", "merged"]
SourceAlignmentState = Literal[
    "aligned",
    "modified",
    "ahead",
    "pending",
    "behind",
    "diverged",
    "untracked",
]


class _StrictCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceCatalogEntry(_StrictCatalogModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    locator: str = Field(min_length=1, max_length=4096)
    document_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,255}$")
    document_kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,255}$")
    title: str = Field(min_length=1, max_length=1024)
    media_type: str
    compiler_profile: Literal["document-v1"] = "document-v1"
    required_tier: Literal["governed_write", "graph_write", "admin"] = "graph_write"
    approval_roles: tuple[Literal["owner", "reviewer"], ...] = ("owner",)
    governance_scope: tuple[str, ...]
    public_uri: str | None = None
    root_alias: str | None = None

    @model_validator(mode="after")
    def _locator_shape(self) -> SourceCatalogEntry:
        path = Path(self.locator)
        if self.root_alias is not None and path.is_absolute():
            raise ValueError("root-aliased locators must be relative to their declared root")
        parts = path.parts if path.is_absolute() else PurePosixPath(self.locator).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("catalog locator must be an explicit normalized file path")
        return self

    @field_validator("public_uri")
    @classmethod
    def _public_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if not parsed.scheme or parsed.scheme.lower() == "file":
            raise ValueError("public_uri must be an explicit non-file URI")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("public_uri must not contain embedded credentials")
        if parsed.scheme.lower() in {"http", "https"} and not parsed.netloc:
            raise ValueError("HTTP public_uri requires an authority")
        return value


class SourceCatalog(_StrictCatalogModel):
    tag: Literal["playbill-source-catalog-v1"] = "playbill-source-catalog-v1"
    catalog_kind: SourceCatalogKind
    entries: tuple[SourceCatalogEntry, ...]

    @field_validator("entries")
    @classmethod
    def _entries(cls, value: tuple[SourceCatalogEntry, ...]) -> tuple[SourceCatalogEntry, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.name.encode("utf-8")))
        if value != ordered or len({item.name for item in value}) != len(value):
            raise ValueError("source catalog entries must be sorted and unique by name")
        targets = [item.document_id for item in value]
        if len(set(targets)) != len(targets):
            raise ValueError("source catalog contains duplicate Document targets")
        if not value:
            raise ValueError("source catalog must declare at least one source")
        return value

    @model_validator(mode="after")
    def _catalog_locator_scope(self) -> SourceCatalog:
        if self.catalog_kind == "portable" and any(
            Path(entry.locator).is_absolute() or entry.root_alias is not None
            for entry in self.entries
        ):
            raise ValueError(
                "portable catalogs require repository-relative locators without root aliases"
            )
        return self


class ResolvedSourceInput(_StrictCatalogModel):
    name: str
    document_id: str
    media_type: str
    compiler_profile: str
    body_digest: str
    byte_length: int = Field(ge=0)
    public_uri: str | None


class CompiledSourceDocument(_StrictCatalogModel):
    source: ResolvedSourceInput
    body_base64: str
    envelope: DocumentShell
    envelope_bytes_base64: str
    envelope_digest: str
    ledger_path: str


class SourceCompilationManifest(_StrictCatalogModel):
    tag: Literal["playbill-source-compilation-v1"] = "playbill-source-compilation-v1"
    catalog_digest: str
    inputs: tuple[ResolvedSourceInput, ...]
    proposed_envelope_digests: tuple[str, ...]
    proposed_tree_digest: str
    accepted_base: AcceptedCoordinate
    compiler_digest: str
    compilation_digest: str

    @field_validator(
        "catalog_digest",
        "proposed_tree_digest",
        "compiler_digest",
        "compilation_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _self_digest(self) -> SourceCompilationManifest:
        values = self.model_dump(mode="json")
        values.pop("tag")
        values.pop("compilation_digest")
        expected = typed_digest(
            Sha256Value,
            "playbill-source-compilation-v1",
            values,
        ).tagged
        if expected != self.compilation_digest:
            raise ValueError("source-compilation digest does not reproduce")
        return self


class SourceCompilationBundle(_StrictCatalogModel):
    tag: Literal["playbill-source-compilation-bundle-v1"] = "playbill-source-compilation-bundle-v1"
    manifest: SourceCompilationManifest
    documents: tuple[CompiledSourceDocument, ...]

    @model_validator(mode="after")
    def _binding(self) -> SourceCompilationBundle:
        if tuple(item.source for item in self.documents) != self.manifest.inputs:
            raise ValueError("source bundle documents differ from manifest inputs")
        if tuple(item.envelope_digest for item in self.documents) != (
            self.manifest.proposed_envelope_digests
        ):
            raise ValueError("source bundle envelopes differ from manifest digests")
        for item in self.documents:
            try:
                body = base64.b64decode(item.body_base64, validate=True)
                envelope = base64.b64decode(item.envelope_bytes_base64, validate=True)
            except ValueError as exc:
                raise ValueError("source bundle contains malformed base64 bytes") from exc
            if content_digest_bytes(body) != item.source.body_digest:
                raise ValueError("source bundle body bytes differ from their digest")
            if envelope != render_document(item.envelope):
                raise ValueError("source bundle envelope bytes are not canonical")
            if document_digest(item.envelope).tagged != item.envelope_digest:
                raise ValueError("source bundle envelope digest does not reproduce")
            if document_path(item.source.document_id) != item.ledger_path:
                raise ValueError("source bundle ledger path differs from its Document identity")
        expected_tree = _proposed_tree_digest(self.documents)
        if expected_tree != self.manifest.proposed_tree_digest:
            raise ValueError("source bundle proposed-tree digest does not reproduce")
        return self


class SourceAlignment(_StrictCatalogModel):
    name: str
    document_id: str
    state: SourceAlignmentState
    local_body_digest: str
    accepted_body_digest: str | None
    accepted_envelope_digest: str | None
    pending_body_digest: str | None
    accepted_coordinate: AcceptedCoordinate


def merge_source_catalogs(
    portable: SourceCatalog,
    local: SourceCatalog | None,
) -> SourceCatalog:
    """Merge a portable catalog with an explicit local override by source name only."""

    if portable.catalog_kind != "portable":
        raise PlaybillFormatError("the base source catalog must be portable")
    if local is None:
        return portable
    if local.catalog_kind != "local":
        raise PlaybillFormatError("source-catalog overlay must be local")
    merged = {entry.name: entry for entry in portable.entries}
    for entry in local.entries:
        previous = merged.get(entry.name)
        if previous is not None and (
            previous.document_id != entry.document_id
            or previous.compiler_profile != entry.compiler_profile
        ):
            raise PlaybillFormatError("local catalog ambiguously changes a source target/profile")
        merged[entry.name] = entry
    try:
        return SourceCatalog(
            catalog_kind="merged",
            entries=tuple(sorted(merged.values(), key=lambda item: item.name.encode("utf-8"))),
        )
    except ValueError as exc:
        raise PlaybillFormatError("merged source catalogs are ambiguous") from exc


def source_catalog_digest(catalog: SourceCatalog) -> str:
    payload = catalog.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, "playbill-source-catalog-v1", payload).tagged


def compile_source_catalog(
    catalog: SourceCatalog,
    *,
    repository_root: Path,
    root_aliases: dict[str, Path],
    accepted_base: AcceptedCoordinate,
    accepted_documents: dict[str, DocumentShell],
) -> SourceCompilationBundle:
    """Read only declared local bytes and freeze a deterministic proposal bundle."""

    repository = _trusted_root(repository_root, label="repository root")
    aliases = {
        alias: _trusted_root(path, label=f"root alias {alias!r}")
        for alias, path in root_aliases.items()
    }
    inputs: list[ResolvedSourceInput] = []
    documents: list[CompiledSourceDocument] = []
    for entry in catalog.entries:
        locator = Path(entry.locator)
        if locator.is_absolute():
            candidates = tuple(
                root for root in aliases.values() if locator == root or root in locator.parents
            )
            if len(candidates) != 1:
                raise PlaybillFormatError(
                    "absolute source locator must belong to exactly one configured local root"
                )
            root = candidates[0]
            relative = locator.relative_to(root).as_posix()
        else:
            resolved_root = (
                repository if entry.root_alias is None else aliases.get(entry.root_alias)
            )
            if resolved_root is None:
                raise PlaybillFormatError(
                    f"source root alias is not configured: {entry.root_alias}"
                )
            root = resolved_root
            relative = entry.locator
        path = _resolve_declared_file(root, relative)
        content = _read_stable_file(path)
        body_digest = content_digest_bytes(content)
        previous = accepted_documents.get(entry.document_id)
        revision = 1 if previous is None else previous.lifecycle.revision + 1
        predecessor = None if previous is None else document_digest(previous).tagged
        shell = DocumentShell(
            identity=f"document:{entry.document_id}",
            document_kind=entry.document_kind,
            title=entry.title,
            media_type=entry.media_type,
            body_digest=body_digest,
            authority=DocumentAuthority(
                required_tier=entry.required_tier,
                approval_roles=entry.approval_roles,
            ),
            governance_scope=entry.governance_scope,
            predecessor_digest=predecessor,
            lifecycle=DocumentLifecycle(revision=revision),
        )
        source = ResolvedSourceInput(
            name=entry.name,
            document_id=entry.document_id,
            media_type=entry.media_type,
            compiler_profile=entry.compiler_profile,
            body_digest=body_digest,
            byte_length=len(content),
            public_uri=entry.public_uri,
        )
        envelope_bytes = render_document(shell)
        inputs.append(source)
        documents.append(
            CompiledSourceDocument(
                source=source,
                body_base64=base64.b64encode(content).decode("ascii"),
                envelope=shell,
                envelope_bytes_base64=base64.b64encode(envelope_bytes).decode("ascii"),
                envelope_digest=document_digest(shell).tagged,
                ledger_path=document_path(entry.document_id),
            )
        )
    input_tuple = tuple(inputs)
    document_tuple = tuple(documents)
    catalog_value = source_catalog_digest(catalog)
    envelope_digests = tuple(item.envelope_digest for item in document_tuple)
    tree_digest = _proposed_tree_digest(document_tuple)
    digest_values: dict[str, object] = {
        "catalog_digest": catalog_value,
        "inputs": [item.model_dump(mode="json") for item in input_tuple],
        "proposed_envelope_digests": list(envelope_digests),
        "proposed_tree_digest": tree_digest,
        "accepted_base": accepted_base.model_dump(mode="json"),
        "compiler_digest": accepted_base.compiler_digest,
    }
    compilation_digest = typed_digest(
        Sha256Value,
        "playbill-source-compilation-v1",
        digest_values,
    ).tagged
    manifest = SourceCompilationManifest(
        catalog_digest=catalog_value,
        inputs=input_tuple,
        proposed_envelope_digests=envelope_digests,
        proposed_tree_digest=tree_digest,
        accepted_base=accepted_base,
        compiler_digest=accepted_base.compiler_digest,
        compilation_digest=compilation_digest,
    )
    return SourceCompilationBundle(manifest=manifest, documents=document_tuple)


def content_digest_bytes(content: bytes) -> str:
    """Return the exact Playbill CAS spelling for local source bytes."""

    return CasDigest(hashlib.sha256(content).hexdigest()).tagged


def _proposed_tree_digest(documents: tuple[CompiledSourceDocument, ...]) -> str:
    return typed_digest(
        Sha256Value,
        "playbill-source-proposed-tree-v1",
        {
            "members": [
                {
                    "ledger_path": item.ledger_path,
                    "envelope_digest": item.envelope_digest,
                    "body_digest": item.source.body_digest,
                }
                for item in documents
            ]
        },
    ).tagged


def _trusted_root(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise PlaybillFormatError(f"{label} must be an existing nonsymlink directory")
    return path.resolve(strict=True)


def _resolve_declared_file(root: Path, locator: str) -> Path:
    candidate = root / locator
    cursor = root
    for part in PurePosixPath(locator).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PlaybillFormatError(f"source catalog refuses symlink locator: {locator}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PlaybillFormatError(f"declared source is missing: {locator}") from exc
    if resolved == root or root not in resolved.parents:
        raise PlaybillFormatError(f"declared source escapes its configured root: {locator}")
    metadata = os.lstat(resolved)
    if not resolved.is_file() or os.path.islink(resolved) or metadata.st_nlink < 1:
        raise PlaybillFormatError(f"declared source must be a regular file: {locator}")
    return resolved


def _read_stable_file(path: Path) -> bytes:
    """Read one resolved regular file once, refusing final-link swaps and in-read edits."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PlaybillFormatError(f"declared source cannot be opened safely: {path.name}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PlaybillFormatError(f"declared source must be a regular file: {path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read()
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    stable_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if stable_before != stable_after or len(content) != after.st_size:
        raise PlaybillFormatError(f"declared source changed while compiling: {path.name}")
    return content


__all__ = [
    "CompiledSourceDocument",
    "ResolvedSourceInput",
    "SourceAlignment",
    "SourceAlignmentState",
    "SourceCatalog",
    "SourceCatalogEntry",
    "SourceCatalogKind",
    "SourceCompilationBundle",
    "SourceCompilationManifest",
    "compile_source_catalog",
    "content_digest_bytes",
    "merge_source_catalogs",
    "source_catalog_digest",
]
