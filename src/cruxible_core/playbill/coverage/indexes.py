"""The two disposable indexes coverage is delivered from.

```
evidence commitment digest                     -> citing Captures / Claims
working source identity + observed occurrence  -> observed commitment digest
```

Neither is an authority and neither is a second compiler. The first is a pure
function of accepted Capture envelopes and accepted Claim backing at exactly one
accepted coordinate -- the read-time consumer of the §11.3 write-time reuse
check, turned around. The second is a pure function of the working snapshot's
bytes. Delete both and rebuild them from the same accepted state and the same
snapshot and every digest reproduces; delete both and never rebuild them and
nothing accepted is lost, because neither ever held anything accepted.

Why exact coverage is not `content_digest -> Claims`
----------------------------------------------------
Identical bytes occur in several sources with entirely different meaning, so the
evidence index alone cannot answer "is this occurrence governed?" It answers
"who cited these bytes," and the overlay answers "where do these bytes currently
sit and under which logical source." Only the resolver's join of the two, over a
matching logical source identity, is `exact`; a join across differing logical
sources is at most a labeled `content_equivalent` candidate.

The scan, and why truncation is honest
--------------------------------------
Finding a cited selection that moved means finding a window whose SHA-256 equals
an accepted commitment, and a commitment is a digest, not a needle: no rolling
search can be seeded from it, so every window of the committed length has to be
hashed. That is affordable at working-set scale and unaffordable without a
bound, so the builder takes an explicit scan budget in hashed bytes. Skipping a
pass is never silent -- the overlay records the truncation, health falls to
`partial`, and an absence stops being factual. Recall shrinks; nothing lies.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.captures import CaptureEnvelopeV1
from cruxible_core.playbill.claims import AcceptedClaim
from cruxible_core.playbill.coverage.contracts import (
    CoverageError,
    CoverageLineOverlayV1,
    LogicalSourceIdentityV1,
    logical_sources_sorted,
    occurrence_identity_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_references import (
    CasSourceReferenceV1,
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
    SourceAccessClass,
    SourceHandleV1,
    SourceReferenceV1,
    source_handle_digest,
)

EVIDENCE_INDEX_DIGEST_DOMAIN = "playbill-coverage-evidence-index-v1"
OCCURRENCE_OVERLAY_DIGEST_DOMAIN = "playbill-coverage-occurrence-overlay-v1"

DEFAULT_MAX_SCANNED_BYTES = 32 * 1024 * 1024


class _StrictCoverageIndexModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def accepted_logical_source(source: SourceReferenceV1) -> LogicalSourceIdentityV1 | None:
    """Name the logical source an accepted reference cites, when it has one.

    A CAS reference returns ``None`` deliberately: it is addressed by content,
    so it names no source that an edit could relocate content within, and the
    resolver treats a byte match against it as foreign rather than inventing a
    logical source the ledger never recorded.
    """

    if isinstance(source, LedgerSourceReferenceV1):
        return LogicalSourceIdentityV1(plane="ledger", identity=source.address.artifact_path)
    if isinstance(source, ExternalSourceReferenceV1):
        return LogicalSourceIdentityV1(plane="external", identity=source.source_identity)
    if isinstance(source, CasSourceReferenceV1):
        return None
    raise CoverageError("unknown source reference kind in coverage index")


# -- reverse evidence index ------------------------------------------------


class CaptureCitationInputV1(_StrictCoverageIndexModel):
    """One accepted Capture as the evidence index reads it.

    ``source_handle`` is optional because a `SourceHandleV1` is a read-seam
    projection rather than a stored accepted artifact: the ledger holds the
    Capture envelope, and a handle is built when a read needs one. A caller that
    already projected the handle passes it, and drift cards then bind the
    dereference handle digest §11.6.2 asks for; a caller that has not projected
    one still gets the durable accepted facts -- Capture digest, commitment, and
    source -- in the card.
    """

    tag: Literal["playbill-coverage-capture-citation-input-v1"] = (
        "playbill-coverage-capture-citation-input-v1"
    )
    capture_digest: str
    envelope: CaptureEnvelopeV1
    access_class: SourceAccessClass = "instance"
    source_handle: SourceHandleV1 | None = None

    @field_validator("capture_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _handle_agrees(self) -> "CaptureCitationInputV1":
        if self.source_handle is None:
            return self
        if self.source_handle.commitment != self.envelope.commitment:
            raise ValueError("projected source handle commits to different evidence")
        if canonical_bytes(self.source_handle.source.model_dump(mode="json")) != canonical_bytes(
            self.envelope.source.model_dump(mode="json")
        ):
            raise ValueError("projected source handle cites a different source")
        return self


class EvidenceCitationV1(_StrictCoverageIndexModel):
    """Every accepted citation of one evidence commitment, in one row."""

    tag: Literal["playbill-coverage-evidence-citation-v1"] = (
        "playbill-coverage-evidence-citation-v1"
    )
    commitment_digest: str
    digest_kind: Literal["exact_bytes", "canonical_value", "query_result", "provider_statement"]
    byte_length: int | None = Field(default=None, ge=0)
    accepted_source: LogicalSourceIdentityV1 | None = None
    access_class: SourceAccessClass
    capture_digests: tuple[str, ...] = ()
    claim_addresses: tuple[SemanticAddress, ...] = ()
    dereference_handle_digest: str | None = None

    @field_validator("commitment_digest", "dereference_handle_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("capture_digests")
    @classmethod
    def _captures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("evidence citation capture digests must be sorted and unique")
        return value

    @property
    def dependent_claim_count(self) -> int:
        return len(self.claim_addresses)

    @property
    def sort_key(self) -> tuple[bytes, bytes]:
        return (
            self.commitment_digest.encode("ascii"),
            self.accepted_source.sort_key if self.accepted_source is not None else b"",
        )


class EvidenceCitationIndexV1(_StrictCoverageIndexModel):
    """`evidence commitment digest -> citing Captures/Claims` at one coordinate."""

    tag: Literal["playbill-coverage-evidence-index-v1"] = "playbill-coverage-evidence-index-v1"
    at: AcceptedCoordinate
    citations: tuple[EvidenceCitationV1, ...] = ()
    truncated: bool = False

    @field_validator("citations")
    @classmethod
    def _citations(cls, value: tuple[EvidenceCitationV1, ...]) -> tuple[EvidenceCitationV1, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError(
                "evidence citations must be sorted and unique by commitment and source"
            )
        return value

    def by_commitment(self, commitment_digest: str) -> tuple[EvidenceCitationV1, ...]:
        return tuple(item for item in self.citations if item.commitment_digest == commitment_digest)

    def by_logical_source(self, source: LogicalSourceIdentityV1) -> tuple[EvidenceCitationV1, ...]:
        key = source.sort_key
        return tuple(
            item
            for item in self.citations
            if item.accepted_source is not None and item.accepted_source.sort_key == key
        )

    def wanted_selections(self) -> tuple[tuple[str, int], ...]:
        """Return the `(digest, byte_length)` pairs a working scan must look for."""

        wanted = {
            (item.commitment_digest, item.byte_length)
            for item in self.citations
            if item.digest_kind == "exact_bytes" and item.byte_length is not None
        }
        return tuple(sorted(wanted))


@dataclass
class _CitationRow:
    """One index row under construction, before it becomes a frozen citation."""

    commitment_digest: str
    digest_kind: Literal["exact_bytes", "canonical_value", "query_result", "provider_statement"]
    byte_length: int | None
    accepted_source: LogicalSourceIdentityV1 | None
    access_class: SourceAccessClass
    capture_digests: set[str] = field(default_factory=set)
    claim_paths: set[str] = field(default_factory=set)
    dereference_handle_digest: str | None = None


def build_evidence_citation_index(
    *,
    at: AcceptedCoordinate,
    captures: Iterable[CaptureCitationInputV1],
    claims: Iterable[AcceptedClaim] = (),
    truncated: bool = False,
) -> EvidenceCitationIndexV1:
    """Turn accepted Capture and Claim facts into the reverse evidence index.

    The Claim side is the reverse of `ClaimBacking.capture_digests`: a Claim that
    pins a Capture is a citation of that Capture's commitment, and the count of
    such Claims is the bounded dependent count a drift card reports. A Capture
    nobody has pinned yet is still indexed -- it is accepted evidence with no
    accepted meaning attached, which is exactly what a candidate card should say.
    """

    citing_claims: dict[str, set[str]] = {}
    claim_addresses: dict[str, SemanticAddress] = {}
    for accepted in claims:
        if accepted.claim.lifecycle.state != "live":
            continue
        address = SemanticAddress.claim_statement(accepted.path)
        claim_addresses[accepted.path] = address
        for capture in accepted.claim.backing.capture_digests:
            citing_claims.setdefault(capture, set()).add(accepted.path)

    rows: dict[tuple[bytes, bytes], _CitationRow] = {}
    for entry in captures:
        envelope = entry.envelope
        source = accepted_logical_source(envelope.source)
        key = (
            envelope.commitment.digest.encode("ascii"),
            source.sort_key if source is not None else b"",
        )
        row = rows.get(key)
        if row is None:
            row = _CitationRow(
                commitment_digest=envelope.commitment.digest,
                digest_kind=envelope.commitment.digest_kind,
                byte_length=envelope.commitment.byte_length,
                accepted_source=source,
                access_class=entry.access_class,
            )
            rows[key] = row
        row.capture_digests.add(entry.capture_digest)
        row.claim_paths.update(citing_claims.get(entry.capture_digest, ()))
        if entry.source_handle is not None and row.dereference_handle_digest is None:
            row.dereference_handle_digest = source_handle_digest(entry.source_handle)
        # A commitment cited under several access classes takes the most
        # restrictive one: coverage may never widen disclosure by aggregation.
        row.access_class = _strictest_access(row.access_class, entry.access_class)

    citations = tuple(
        EvidenceCitationV1(
            commitment_digest=row.commitment_digest,
            digest_kind=row.digest_kind,
            byte_length=row.byte_length,
            accepted_source=row.accepted_source,
            access_class=row.access_class,
            capture_digests=byte_sorted(tuple(row.capture_digests)),
            claim_addresses=tuple(claim_addresses[path] for path in sorted(row.claim_paths)),
            dereference_handle_digest=row.dereference_handle_digest,
        )
        for _, row in sorted(rows.items())
    )
    return EvidenceCitationIndexV1(at=at, citations=citations, truncated=truncated)


_ACCESS_STRICTNESS: Mapping[str, int] = {"public": 0, "instance": 1, "restricted": 2}


def _strictest_access(left: SourceAccessClass, right: SourceAccessClass) -> SourceAccessClass:
    return left if _ACCESS_STRICTNESS[left] >= _ACCESS_STRICTNESS[right] else right


def evidence_citation_index_digest(index: EvidenceCitationIndexV1) -> str:
    """Digest the index so a manifest can name exactly which generation it bound."""

    payload = index.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, EVIDENCE_INDEX_DIGEST_DOMAIN, payload).tagged


# -- working-source occurrence overlay -------------------------------------


@dataclass(frozen=True)
class WorkingSourceContent:
    """One observed working source: its declared logical identity and its bytes.

    The bytes stay out of every model. An overlay is a map of where content sits,
    not a second copy of the content, and a disposable local record that carried
    working bodies would be a body store nobody asked for.
    """

    source: LogicalSourceIdentityV1
    content: bytes


class CoverageScanBudgetV1(_StrictCoverageIndexModel):
    """The bound on relocation scanning, in bytes fed through SHA-256."""

    tag: Literal["playbill-coverage-scan-budget-v1"] = "playbill-coverage-scan-budget-v1"
    max_scanned_bytes: int = Field(default=DEFAULT_MAX_SCANNED_BYTES, ge=0)


class WorkingSourceCommitmentV1(_StrictCoverageIndexModel):
    """One working source's whole observed content, as a commitment."""

    tag: Literal["playbill-coverage-working-source-commitment-v1"] = (
        "playbill-coverage-working-source-commitment-v1"
    )
    source: LogicalSourceIdentityV1
    content_digest: str
    byte_length: int = Field(ge=0)

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class WorkingOccurrenceV1(_StrictCoverageIndexModel):
    """One observed occurrence: stable identity, presentation overlay beside it."""

    tag: Literal["playbill-coverage-working-occurrence-v1"] = (
        "playbill-coverage-working-occurrence-v1"
    )
    source: LogicalSourceIdentityV1
    observed_commitment_digest: str
    byte_length: int = Field(ge=0)
    ordinal: int = Field(ge=0)
    identity_digest: str
    line_overlay: CoverageLineOverlayV1

    @field_validator("observed_commitment_digest", "identity_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _identity_reproduces(self) -> "WorkingOccurrenceV1":
        expected = occurrence_identity_digest(
            source=self.source,
            observed_commitment_digest=self.observed_commitment_digest,
            ordinal=self.ordinal,
        )
        if expected != self.identity_digest:
            raise ValueError("working occurrence identity does not reproduce from its own fields")
        return self

    @property
    def sort_key(self) -> tuple[bytes, bytes, int]:
        return (
            self.source.sort_key,
            self.observed_commitment_digest.encode("ascii"),
            self.ordinal,
        )


class WorkingOccurrenceOverlayV1(_StrictCoverageIndexModel):
    """`working source identity + observed occurrence -> observed commitment`."""

    tag: Literal["playbill-coverage-occurrence-overlay-v1"] = (
        "playbill-coverage-occurrence-overlay-v1"
    )
    sources: tuple[WorkingSourceCommitmentV1, ...] = ()
    occurrences: tuple[WorkingOccurrenceV1, ...] = ()
    scanned_selections: tuple[str, ...] = ()
    truncated: bool = False
    truncation_reason_codes: tuple[str, ...] = ()

    @field_validator("sources")
    @classmethod
    def _sources(
        cls, value: tuple[WorkingSourceCommitmentV1, ...]
    ) -> tuple[WorkingSourceCommitmentV1, ...]:
        keys = tuple(item.source.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("overlay sources must be sorted and unique by logical source")
        return value

    @field_validator("occurrences")
    @classmethod
    def _occurrences(
        cls, value: tuple[WorkingOccurrenceV1, ...]
    ) -> tuple[WorkingOccurrenceV1, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("overlay occurrences must be sorted and unique by occurrence identity")
        return value

    @field_validator("scanned_selections", "truncation_reason_codes")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("overlay string sets must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _truncation_is_explained(self) -> "WorkingOccurrenceOverlayV1":
        if self.truncated != bool(self.truncation_reason_codes):
            raise ValueError("overlay truncation must be stated with its reason codes")
        return self

    def commitment_for(self, source: LogicalSourceIdentityV1) -> WorkingSourceCommitmentV1 | None:
        key = source.sort_key
        for item in self.sources:
            if item.source.sort_key == key:
                return item
        return None

    def occurrences_for(self, source: LogicalSourceIdentityV1) -> tuple[WorkingOccurrenceV1, ...]:
        key = source.sort_key
        return tuple(item for item in self.occurrences if item.source.sort_key == key)

    def scanned(self, commitment_digest: str) -> bool:
        """Whether absence of this commitment was actually looked for."""

        return commitment_digest in self.scanned_selections

    @property
    def scope(self) -> tuple[LogicalSourceIdentityV1, ...]:
        return logical_sources_sorted(tuple(item.source for item in self.sources))


def _line_starts(content: bytes) -> Sequence[int]:
    starts = [0]
    offset = content.find(b"\n")
    while offset != -1:
        starts.append(offset + 1)
        offset = content.find(b"\n", offset + 1)
    return starts


def _overlay(starts: Sequence[int], *, start_byte: int, end_byte: int) -> CoverageLineOverlayV1:
    start_line = bisect.bisect_right(starts, start_byte)
    last_byte = end_byte - 1 if end_byte > start_byte else start_byte
    end_line = bisect.bisect_right(starts, last_byte)
    return CoverageLineOverlayV1(
        start_byte=start_byte,
        end_byte=end_byte,
        start_line=max(start_line, 1),
        end_line=max(end_line, 1),
    )


def build_working_occurrence_overlay(
    sources: Iterable[WorkingSourceContent],
    *,
    wanted: Iterable[tuple[str, int]] = (),
    budget: CoverageScanBudgetV1 = CoverageScanBudgetV1(),
) -> WorkingOccurrenceOverlayV1:
    """Observe the working snapshot: whole-source content, then cited selections.

    Two passes, both deterministic and both free of a clock:

    1. every working source contributes its whole observed content as one
       occurrence, which is what makes a whole-file citation resolvable and what
       the manifest binds its per-source commitments to;
    2. every wanted `(digest, byte_length)` drawn from the evidence index is
       looked for at every offset of every source long enough to hold it, which
       is what makes relocated cited content stay `exact` after it moves.

    Ordinals are assigned per `(source, observed digest)` in byte order, so an
    occurrence that is unique in its source keeps ordinal 0 no matter where it
    moves to, and duplicated content produces the several distinct identities the
    resolver needs in order to refuse to bind one of them.
    """

    materials = tuple(sources)
    keys = [item.source.sort_key for item in materials]
    if len(set(keys)) != len(keys):
        raise CoverageError("a working snapshot names each logical source at most once")

    wanted_by_length: dict[int, set[str]] = {}
    for digest, byte_length in wanted:
        Sha256Value.from_tagged(digest)
        if byte_length <= 0:
            continue
        wanted_by_length.setdefault(byte_length, set()).add(digest)

    commitments: list[WorkingSourceCommitmentV1] = []
    found: dict[tuple[bytes, str], list[tuple[int, int]]] = {}
    line_starts: dict[bytes, Sequence[int]] = {}
    scanned: set[str] = set()
    reasons: set[str] = set()
    remaining = budget.max_scanned_bytes

    for material in materials:
        content = material.content
        key = material.source.sort_key
        line_starts[key] = _line_starts(content)
        whole = Sha256Value(hashlib.sha256(content).hexdigest()).tagged
        commitments.append(
            WorkingSourceCommitmentV1(
                source=material.source,
                content_digest=whole,
                byte_length=len(content),
            )
        )
        found.setdefault((key, whole), []).append((0, len(content)))

    for byte_length in sorted(wanted_by_length):
        digests = wanted_by_length[byte_length]
        complete = True
        for material in materials:
            content = material.content
            if len(content) < byte_length:
                continue
            windows = len(content) - byte_length + 1
            cost = windows * byte_length
            if cost > remaining:
                reasons.add("scan_budget_exceeded")
                complete = False
                continue
            remaining -= cost
            key = material.source.sort_key
            for offset in range(windows):
                observed = Sha256Value(
                    hashlib.sha256(content[offset : offset + byte_length]).hexdigest()
                ).tagged
                if observed in digests:
                    found.setdefault((key, observed), []).append((offset, offset + byte_length))
        if complete:
            # Absence of these commitments was genuinely looked for everywhere in
            # scope, so the resolver may read "not here" as drift rather than as
            # an unexamined gap.
            scanned.update(digests)

    by_key = {item.source.sort_key: item.source for item in materials}
    occurrences: list[WorkingOccurrenceV1] = []
    for (key, digest), spans in found.items():
        source = by_key[key]
        for ordinal, (start_byte, end_byte) in enumerate(sorted(set(spans))):
            occurrences.append(
                WorkingOccurrenceV1(
                    source=source,
                    observed_commitment_digest=digest,
                    byte_length=end_byte - start_byte,
                    ordinal=ordinal,
                    identity_digest=occurrence_identity_digest(
                        source=source,
                        observed_commitment_digest=digest,
                        ordinal=ordinal,
                    ),
                    line_overlay=_overlay(
                        line_starts[key], start_byte=start_byte, end_byte=end_byte
                    ),
                )
            )

    return WorkingOccurrenceOverlayV1(
        sources=tuple(sorted(commitments, key=lambda item: item.source.sort_key)),
        occurrences=tuple(sorted(occurrences, key=lambda item: item.sort_key)),
        scanned_selections=byte_sorted(tuple(scanned)),
        truncated=bool(reasons),
        truncation_reason_codes=byte_sorted(tuple(reasons)),
    )


def working_occurrence_overlay_digest(overlay: WorkingOccurrenceOverlayV1) -> str:
    """Digest the overlay so a manifest binds the exact snapshot it observed."""

    payload = overlay.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, OCCURRENCE_OVERLAY_DIGEST_DOMAIN, payload).tagged


__all__ = [
    "DEFAULT_MAX_SCANNED_BYTES",
    "EVIDENCE_INDEX_DIGEST_DOMAIN",
    "OCCURRENCE_OVERLAY_DIGEST_DOMAIN",
    "CaptureCitationInputV1",
    "CoverageScanBudgetV1",
    "EvidenceCitationIndexV1",
    "EvidenceCitationV1",
    "WorkingOccurrenceOverlayV1",
    "WorkingOccurrenceV1",
    "WorkingSourceCommitmentV1",
    "WorkingSourceContent",
    "accepted_logical_source",
    "build_evidence_citation_index",
    "build_working_occurrence_overlay",
    "evidence_citation_index_digest",
    "working_occurrence_overlay_digest",
]
