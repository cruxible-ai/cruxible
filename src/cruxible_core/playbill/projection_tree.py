"""Bounded exact-tree reader for the Python reference assembler."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.artifacts import ArtifactKindRegistry
from cruxible_client.contracts.canonical import (
    is_candidate_card_path,
    normalize_ledger_path,
    normalize_manifest_paths,
)
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_core.playbill.projection_artifacts import registered_path_kind
from cruxible_core.playbill.protocols import LedgerRepositoryProtocol

_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"


class TreeReadLimits(BaseModel):
    """Serialized resource bounds applied before parsing any artifact.

    The file-count and aggregate-byte defaults carry the pre-PC-G adoption
    posture: sharded Claim vocabularies reach six figures of members, so a
    10,000-file ceiling would refuse a real instance rather than a hostile one.
    Raising the count does not weaken any other gate -- path canonicality,
    registered-kind resolution, mode/symlink/submodule refusal, declared size
    and LFS-pointer refusal all stay exactly as they were.

    The per-blob ceiling is 64 MiB, raised from 4 MiB because 4 MiB was two
    budgets for one thing. The ledger writes its own record of a change set as
    a single blob costing up to 11,264 bytes per lowered entry, so a 4 MiB
    ceiling settled at most 372 entries while proposal admission advertised
    `max_changed_members: 5000` -- one number for what a submission may carry,
    a different one for what could then be recorded. At 64 MiB they are ONE
    number: 5,000 entries project to 56,320,000 bytes, about 53.7 MiB, which
    fits under the ceiling with room left. It closes a second gap in passing:
    receive admits a single member of up to 8 MiB against a read ceiling of 4,
    so a captured source over 4 MiB was admissible and then unreadable. Such a
    source may now back a Claim.

    Raising a READ limit is backward compatible: every blob already accepted was
    written under the 4 MiB ceiling and is therefore still under this one, so no
    accepted tree changes meaning and no accepted artifact changes bytes. It is
    still a bound -- it is what keeps a single member from exhausting memory --
    and operators may configure lower soft limits; the serialized hard ceiling
    stays bounded either way.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_files: int = Field(default=250_000, ge=1, le=1_000_000)
    max_total_bytes: int = Field(default=512 * 1024 * 1024, ge=1, le=2**44)
    max_blob_bytes: int = Field(default=64 * 1024 * 1024, ge=1)


@dataclass(frozen=True)
class GitTreeBlob:
    path: str
    oid: str
    content: bytes


def read_registered_tree(
    repository: LedgerRepositoryProtocol,
    oid: str,
    *,
    limits: TreeReadLimits,
    artifact_kinds: ArtifactKindRegistry,
) -> tuple[GitTreeBlob, ...]:
    """Read regular registered blobs only, with all metadata gates first."""

    entries = repository.list_tree_with_sizes(oid)
    if len(entries) > limits.max_files:
        raise ProjectionFormatError(
            f"ledger tree exceeds file-count limit ({len(entries)} > {limits.max_files})"
        )
    try:
        if any(normalize_ledger_path(entry.path) != entry.path for entry in entries):
            raise ProjectionFormatError("ledger tree contains a noncanonical path spelling")
        ordered_paths = normalize_manifest_paths([entry.path for entry in entries])
    except Exception as exc:
        raise ProjectionFormatError(
            "ledger tree paths are not canonical and collision-free"
        ) from exc
    by_path = {entry.path: entry for entry in entries}
    if len(by_path) != len(entries):
        raise ProjectionFormatError("ledger tree contains duplicate paths")

    declared_total = 0
    for path in ordered_paths:
        entry = by_path[path]
        try:
            if normalize_ledger_path(path) != path:
                raise ProjectionFormatError(f"ledger path is not canonical: {path}")
            if not is_candidate_card_path(path):
                registered_path_kind(path, artifact_kinds=artifact_kinds)
        except Exception as exc:
            if isinstance(exc, ProjectionFormatError):
                raise
            raise ProjectionFormatError(f"ledger path is not registered: {path}") from exc
        if entry.mode != "100644" or entry.object_type != "blob":
            if entry.mode == "120000":
                kind = "symlink"
            elif entry.mode == "160000" or entry.object_type == "commit":
                kind = "submodule"
            else:
                kind = f"{entry.mode} {entry.object_type}"
            raise ProjectionFormatError(f"ledger tree contains forbidden {kind}: {path}")
        if entry.size is None or entry.size < 0:
            raise ProjectionFormatError(f"ledger blob has no trustworthy size: {path}")
        if entry.size > limits.max_blob_bytes:
            raise ProjectionFormatError(
                f"ledger blob exceeds per-file byte limit: {path} ({entry.size})"
            )
        declared_total += entry.size
        if declared_total > limits.max_total_bytes:
            raise ProjectionFormatError(
                "ledger tree exceeds total-byte limit "
                f"({declared_total} > {limits.max_total_bytes})"
            )

    blobs: list[GitTreeBlob] = []
    cached = repository.read_blobs(
        tuple(dict.fromkeys(by_path[path].oid for path in ordered_paths))
    )
    actual_total = 0
    for path in ordered_paths:
        entry = by_path[path]
        try:
            content = cached[entry.oid]
        except KeyError as exc:
            raise ProjectionFormatError(f"repository omitted a requested blob: {path}") from exc
        if len(content) != entry.size:
            raise ProjectionFormatError(f"ledger blob size changed while reading: {path}")
        if content.startswith(_LFS_POINTER_PREFIX):
            raise ProjectionFormatError(f"Git LFS pointer is not an artifact payload: {path}")
        actual_total += len(content)
        if actual_total > limits.max_total_bytes:
            raise ProjectionFormatError("ledger tree exceeded byte limit while reading")
        blobs.append(GitTreeBlob(path=path, oid=entry.oid, content=content))
    return tuple(blobs)


__all__ = ["GitTreeBlob", "TreeReadLimits", "read_registered_tree"]
