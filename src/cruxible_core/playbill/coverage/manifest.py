"""The local coverage manifest: freshness that fails closed without a socket.

Why this exists
---------------
A coverage answer is only as good as the claim "the working snapshot I resolved
against is the one in front of you now." §11.6.6 makes that provable without a
live watcher: an atomically published manifest plus a monotonic epoch that a
harness may poll when it returns a tool result. A socket, a monitor, or a stream
is a low-latency delivery adapter over this, never a second source of truth, and
this slice ships none of them.

What a manifest is, and is not
------------------------------
It is **not** accepted state and **not** a wire record. It never enters the
ledger, the CAS, an exhaust journal, or an export, and `inspect()` does not
report it. It is one local file under a fixed directory in the instance's
rebuildable cache root, exactly like a projection build or a replay checkpoint.
Deleting it can only cost freshness: the resolver reports `unavailable`, stops
returning `exact`, and keeps working.

Failing closed, precisely
-------------------------
Nothing in the file is believed because it is written down.

1. The body's per-source commitments are compared against the *observed*
   overlay on every resolve. A source whose working bytes have changed since
   publication makes the manifest stale for that source, and stale coverage
   cannot be `exact`.
2. The accepted coordinate, the evidence-index digest, and the overlay digest
   are compared against the ones the caller actually resolved with. A manifest
   published at a different generation or over a different index is stale, not
   authoritative.
3. The file's self-digest is recomputed on load, and a mismatch deletes the file
   rather than trusting it -- a corrupted or hand-edited manifest must never be
   able to assert freshness it cannot back.
4. The epoch is a counter, never a time. Publishing refuses a non-advancing
   epoch, so a stale writer cannot quietly reinstate an older snapshot, and two
   readers can order two manifests without consulting a clock.

`written_at` records when the file was published and sits **outside** the digest
preimage, so an identical accepted coordinate and snapshot always produce an
identical manifest digest.

A local format is superseded by rewriting it, never by migrating it.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageError,
    CoverageWatcherHealthV1,
    LogicalSourceIdentityV1,
    logical_sources_sorted,
)
from cruxible_core.playbill.coverage.indexes import (
    EvidenceCitationIndexV1,
    EvidenceCitationIndexV2,
    WorkingOccurrenceOverlayV1,
    WorkingSourceCommitmentV1,
    evidence_citation_index_digest,
    working_occurrence_overlay_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

COVERAGE_DIRECTORY: Final = "coverage"
COVERAGE_MANIFEST_FILE: Final = "coverage-manifest-v1.json"
COVERAGE_MANIFEST_TAG: Final = "playbill-coverage-manifest-v1"
COVERAGE_MANIFEST_FILE_V2: Final = "coverage-manifest-v2.json"
COVERAGE_MANIFEST_TAG_V2: Final = "playbill-coverage-manifest-v2"

_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class CoverageManifestDigest(Sha256Value):
    """The self-digest of one local coverage manifest.

    Declared here rather than beside the wire digest kinds, exactly as the
    replay checkpoint's digest is: this value commits to a local operational
    cache, never to accepted state, and no ledger record, journal record, or
    exported format may ever carry it.
    """

    kind = "coverage manifest digest"


class CoverageManifestError(CoverageError):
    """A coverage manifest is malformed, regressive, or does not reproduce."""


class _StrictManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoverageWorkingSetScopeV1(_StrictManifestModel):
    """The declared boundary a `none` is only ever factual inside of."""

    tag: Literal["playbill-coverage-working-set-scope-v1"] = (
        "playbill-coverage-working-set-scope-v1"
    )
    sources: tuple[LogicalSourceIdentityV1, ...] = ()
    complete: bool = True
    truncation_reason_codes: tuple[str, ...] = ()

    @field_validator("sources")
    @classmethod
    def _sources(
        cls, value: tuple[LogicalSourceIdentityV1, ...]
    ) -> tuple[LogicalSourceIdentityV1, ...]:
        if value != logical_sources_sorted(value):
            raise ValueError("working-set scope sources must be sorted and unique")
        return value

    @field_validator("truncation_reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("scope truncation reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _completeness_is_explained(self) -> "CoverageWorkingSetScopeV1":
        if self.complete == bool(self.truncation_reason_codes):
            raise ValueError("an incomplete scope states its reasons and a complete one has none")
        return self

    def covers(self, source: LogicalSourceIdentityV1) -> bool:
        key = source.sort_key
        return any(item.sort_key == key for item in self.sources)


class CoverageManifestBodyV1(_StrictManifestModel):
    """Exactly the fields the manifest digest commits to.

    No wall-clock value appears here. The epoch is a monotonic counter and the
    publication time lives outside this preimage, so an identical coordinate and
    an identical snapshot always produce an identical digest -- which is what
    makes "delete the manifest and rebuild it" a deterministic operation rather
    than a new record.
    """

    tag: Literal["playbill-coverage-manifest-v1"] = COVERAGE_MANIFEST_TAG
    format_version: Literal[1] = 1
    instance_id: str
    at: AcceptedCoordinate
    index_digest: str
    overlay_digest: str
    scope: CoverageWorkingSetScopeV1
    sources: tuple[WorkingSourceCommitmentV1, ...] = ()
    access_profile: CoverageAccessProfileV1
    epoch: int = Field(ge=0)
    watcher_health: CoverageWatcherHealthV1 = "absent"
    completeness: Literal["complete", "partial"] = "complete"
    truncation_reason_codes: tuple[str, ...] = ()

    @field_validator("instance_id")
    @classmethod
    def _instance_id(cls, value: str) -> str:
        if not _INSTANCE_ID_RE.fullmatch(value):
            raise ValueError("coverage manifest instance_id must be a canonical identifier")
        return value

    @field_validator("index_digest", "overlay_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("sources")
    @classmethod
    def _sources(
        cls, value: tuple[WorkingSourceCommitmentV1, ...]
    ) -> tuple[WorkingSourceCommitmentV1, ...]:
        keys = tuple(item.source.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("manifest source commitments must be sorted and unique")
        return value

    @field_validator("truncation_reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("manifest truncation reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _manifest_shape(self) -> "CoverageManifestBodyV1":
        if (self.completeness == "partial") != bool(self.truncation_reason_codes):
            raise ValueError("manifest completeness must agree with its truncation reasons")
        if self.completeness == "complete" and not self.scope.complete:
            raise ValueError("a manifest cannot be complete over an incomplete scope")
        for item in self.sources:
            if not self.scope.covers(item.source):
                raise ValueError("a manifest commitment names a source outside its declared scope")
        return self

    def commitment_for(self, source: LogicalSourceIdentityV1) -> WorkingSourceCommitmentV1 | None:
        key = source.sort_key
        for item in self.sources:
            if item.source.sort_key == key:
                return item
        return None


class CoverageManifestFileV1(_StrictManifestModel):
    """The on-disk record: one digest-committed body plus non-committed metadata."""

    tag: Literal["playbill-coverage-manifest-file-v1"] = "playbill-coverage-manifest-file-v1"
    body: CoverageManifestBodyV1
    manifest_digest: str
    written_at: str

    @field_validator("manifest_digest")
    @classmethod
    def _manifest_digest(cls, value: str) -> str:
        CoverageManifestDigest.from_tagged(value)
        return value


class CoverageManifestBodyV2(CoverageManifestBodyV1):
    tag: Literal["playbill-coverage-manifest-v2"] = COVERAGE_MANIFEST_TAG_V2  # type: ignore[assignment]
    format_version: Literal[2] = 2  # type: ignore[assignment]


class CoverageManifestFileV2(_StrictManifestModel):
    tag: Literal["playbill-coverage-manifest-file-v2"] = "playbill-coverage-manifest-file-v2"
    body: CoverageManifestBodyV2
    manifest_digest: str
    written_at: str

    @field_validator("manifest_digest")
    @classmethod
    def _manifest_digest(cls, value: str) -> str:
        CoverageManifestDigest.from_tagged(value)
        return value


def coverage_manifest_digest(body: CoverageManifestBodyV1) -> CoverageManifestDigest:
    return typed_digest(
        CoverageManifestDigest,
        COVERAGE_MANIFEST_TAG,
        {key: value for key, value in body.model_dump(mode="json").items() if key != "tag"},
    )


def render_coverage_manifest(body: CoverageManifestBodyV1, *, written_at: str) -> bytes:
    record = CoverageManifestFileV1(
        body=body,
        manifest_digest=coverage_manifest_digest(body).tagged,
        written_at=written_at,
    )
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


def coverage_manifest_body(
    *,
    instance_id: str,
    index: EvidenceCitationIndexV1,
    overlay: WorkingOccurrenceOverlayV1,
    access_profile: CoverageAccessProfileV1,
    epoch: int = 0,
    watcher_health: CoverageWatcherHealthV1 = "absent",
    scope: CoverageWorkingSetScopeV1 | None = None,
) -> CoverageManifestBodyV1:
    """Summarize one already-built index and overlay as a publishable manifest.

    The scope defaults to exactly the sources the overlay observed, and its
    completeness inherits every truncation the index or the overlay recorded, so
    a manifest can never declare a boundary broader than the work that was
    actually done inside it.
    """

    reasons = set(overlay.truncation_reason_codes)
    if index.truncated:
        reasons.add("evidence_index_truncated")
    declared = scope or CoverageWorkingSetScopeV1(
        sources=overlay.scope,
        complete=not reasons,
        truncation_reason_codes=byte_sorted(tuple(reasons)),
    )
    if not declared.complete:
        reasons.update(declared.truncation_reason_codes)
    return CoverageManifestBodyV1(
        instance_id=instance_id,
        at=index.at,
        index_digest=evidence_citation_index_digest(index),
        overlay_digest=working_occurrence_overlay_digest(overlay),
        scope=declared,
        sources=overlay.sources,
        access_profile=access_profile,
        epoch=epoch,
        watcher_health=watcher_health,
        completeness="partial" if reasons else "complete",
        truncation_reason_codes=byte_sorted(tuple(reasons)),
    )


def coverage_manifest_digest_v2(body: CoverageManifestBodyV2) -> CoverageManifestDigest:
    return typed_digest(
        CoverageManifestDigest,
        COVERAGE_MANIFEST_TAG_V2,
        {key: value for key, value in body.model_dump(mode="json").items() if key != "tag"},
    )


def render_coverage_manifest_v2(body: CoverageManifestBodyV2, *, written_at: str) -> bytes:
    record = CoverageManifestFileV2(
        body=body,
        manifest_digest=coverage_manifest_digest_v2(body).tagged,
        written_at=written_at,
    )
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


def coverage_manifest_body_v2(
    *,
    instance_id: str,
    index: EvidenceCitationIndexV2,
    overlay: WorkingOccurrenceOverlayV1,
    access_profile: CoverageAccessProfileV1,
    epoch: int = 0,
    watcher_health: CoverageWatcherHealthV1 = "absent",
    scope: CoverageWorkingSetScopeV1 | None = None,
) -> CoverageManifestBodyV2:
    reasons = set(overlay.truncation_reason_codes)
    if index.truncated:
        reasons.add("evidence_index_truncated")
    declared = scope or CoverageWorkingSetScopeV1(
        sources=overlay.scope,
        complete=not reasons,
        truncation_reason_codes=byte_sorted(tuple(reasons)),
    )
    if not declared.complete:
        reasons.update(declared.truncation_reason_codes)
    return CoverageManifestBodyV2(
        instance_id=instance_id,
        at=index.at,
        index_digest=evidence_citation_index_digest(index),
        overlay_digest=working_occurrence_overlay_digest(overlay),
        scope=declared,
        sources=overlay.sources,
        access_profile=access_profile,
        epoch=epoch,
        watcher_health=watcher_health,
        completeness="partial" if reasons else "complete",
        truncation_reason_codes=byte_sorted(tuple(reasons)),
    )


def advance_coverage_manifest(
    previous: CoverageManifestBodyV1,
    *,
    index: EvidenceCitationIndexV1,
    overlay: WorkingOccurrenceOverlayV1,
    access_profile: CoverageAccessProfileV1 | None = None,
    watcher_health: CoverageWatcherHealthV1 | None = None,
    scope: CoverageWorkingSetScopeV1 | None = None,
) -> CoverageManifestBodyV1:
    """Republish over a fresh observation, advancing the epoch by exactly one."""

    return coverage_manifest_body(
        instance_id=previous.instance_id,
        index=index,
        overlay=overlay,
        access_profile=access_profile or previous.access_profile,
        epoch=previous.epoch + 1,
        watcher_health=watcher_health or previous.watcher_health,
        scope=scope,
    )


def coverage_manifest_path(directory: Path) -> Path:
    return directory / COVERAGE_MANIFEST_FILE


def coverage_manifest_path_v2(directory: Path) -> Path:
    return directory / COVERAGE_MANIFEST_FILE_V2


def write_coverage_manifest(
    directory: Path,
    body: CoverageManifestBodyV1,
    *,
    written_at: str = "",
) -> Path:
    """Publish one manifest atomically, refusing to move the epoch backwards.

    A reader either sees the whole previous manifest or the whole new one; there
    is no window in which it can see half of either, which is the entire reason
    a coverage answer may cite an epoch at all.
    """

    existing = load_coverage_manifest_file(directory)
    if existing is not None and body.epoch <= existing.body.epoch:
        raise CoverageManifestError(
            "a coverage manifest epoch is monotonic; publishing must advance it"
        )
    directory.mkdir(parents=True, exist_ok=True)
    target = coverage_manifest_path(directory)
    temporary = directory / f".coverage-manifest-{secrets.token_hex(12)}.tmp"
    try:
        temporary.write_bytes(render_coverage_manifest(body, written_at=written_at))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def write_coverage_manifest_v2(
    directory: Path,
    body: CoverageManifestBodyV2,
    *,
    written_at: str = "",
) -> Path:
    """Publish v2 atomically after discarding any superseded local v1 cache."""

    coverage_manifest_path(directory).unlink(missing_ok=True)
    existing = load_coverage_manifest_file_v2(directory)
    if existing is not None and body.epoch <= existing.body.epoch:
        raise CoverageManifestError(
            "a coverage manifest epoch is monotonic; publishing must advance it"
        )
    directory.mkdir(parents=True, exist_ok=True)
    target = coverage_manifest_path_v2(directory)
    temporary = directory / f".coverage-manifest-v2-{secrets.token_hex(12)}.tmp"
    try:
        temporary.write_bytes(render_coverage_manifest_v2(body, written_at=written_at))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_coverage_manifest_file(directory: Path) -> CoverageManifestFileV1 | None:
    """Load and re-verify the published manifest, deleting one that does not hold."""

    target = coverage_manifest_path(directory)
    try:
        content = target.read_bytes()
    except FileNotFoundError:
        return None
    try:
        record = CoverageManifestFileV1.model_validate_json(content)
    except ValidationError:
        target.unlink(missing_ok=True)
        return None
    if record.manifest_digest != coverage_manifest_digest(record.body).tagged:
        target.unlink(missing_ok=True)
        return None
    return record


def load_coverage_manifest_file_v2(directory: Path) -> CoverageManifestFileV2 | None:
    """Read v2 only; a local v1 cache is discarded rather than migrated."""

    coverage_manifest_path(directory).unlink(missing_ok=True)
    target = coverage_manifest_path_v2(directory)
    try:
        content = target.read_bytes()
    except FileNotFoundError:
        return None
    try:
        record = CoverageManifestFileV2.model_validate_json(content)
    except ValidationError:
        target.unlink(missing_ok=True)
        return None
    if record.manifest_digest != coverage_manifest_digest_v2(record.body).tagged:
        target.unlink(missing_ok=True)
        return None
    return record


def discard_coverage_manifest(directory: Path) -> None:
    """Delete the manifest; the only cost is provable freshness."""

    coverage_manifest_path(directory).unlink(missing_ok=True)


__all__ = [
    "COVERAGE_DIRECTORY",
    "COVERAGE_MANIFEST_FILE",
    "COVERAGE_MANIFEST_FILE_V2",
    "COVERAGE_MANIFEST_TAG",
    "COVERAGE_MANIFEST_TAG_V2",
    "CoverageManifestBodyV1",
    "CoverageManifestBodyV2",
    "CoverageManifestDigest",
    "CoverageManifestError",
    "CoverageManifestFileV1",
    "CoverageManifestFileV2",
    "CoverageWorkingSetScopeV1",
    "advance_coverage_manifest",
    "coverage_manifest_body",
    "coverage_manifest_body_v2",
    "coverage_manifest_digest",
    "coverage_manifest_digest_v2",
    "coverage_manifest_path",
    "coverage_manifest_path_v2",
    "discard_coverage_manifest",
    "load_coverage_manifest_file",
    "load_coverage_manifest_file_v2",
    "render_coverage_manifest",
    "render_coverage_manifest_v2",
    "write_coverage_manifest",
    "write_coverage_manifest_v2",
]
