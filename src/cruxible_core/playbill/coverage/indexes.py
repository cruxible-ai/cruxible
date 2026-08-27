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
The accepted-state caller may supply exact selected bytes after verifying them
against an accepted commitment. Those bytes are only a search needle: every
candidate occurrence is still digest-verified. When retained bytes are not
available, commitments of one length share one exhaustive window-hashing pass.
Both routes are bounded explicitly and admit each source/commitment proof only
after its full debit fits. Skipping work is never silent -- the overlay records
the truncation and withholds the affected proof. Recall shrinks; nothing lies.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import CaptureEnvelopeV1
from cruxible_client.contracts.claim_verdicts import ObservationTrustGrade
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimCitationReference,
    claim_citation_references,
)
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
    SourceAccessClass,
    SourceHandleV1,
    SourceReferenceV1,
    source_handle_digest,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageClaimCitationV2,
    CoverageCommitmentScanProofV1,
    CoverageError,
    CoverageLineOverlayV1,
    LogicalSourceIdentityV1,
    logical_sources_sorted,
    occurrence_identity_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

EVIDENCE_INDEX_DIGEST_DOMAIN = "playbill-coverage-evidence-index-v1"
EVIDENCE_INDEX_V2_DIGEST_DOMAIN = "playbill-coverage-evidence-index-v2"
OCCURRENCE_OVERLAY_DIGEST_DOMAIN = "playbill-coverage-occurrence-overlay-v1"
OCCURRENCE_OVERLAY_V2_DIGEST_DOMAIN = "playbill-coverage-occurrence-overlay-v2"

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


class CaptureCitationInputV2(CaptureCitationInputV1):
    tag: Literal["playbill-coverage-capture-citation-input-v2"] = (
        "playbill-coverage-capture-citation-input-v2"  # type: ignore[assignment]
    )
    observation_trust: ObservationTrustGrade


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


class EvidenceCitationV2(EvidenceCitationV1):
    tag: Literal["playbill-coverage-evidence-citation-v2"] = (
        "playbill-coverage-evidence-citation-v2"  # type: ignore[assignment]
    )
    citation_associations: tuple[CoverageClaimCitationV2, ...] = ()

    @field_validator("citation_associations")
    @classmethod
    def _associations(
        cls,
        value: tuple[CoverageClaimCitationV2, ...],
    ) -> tuple[CoverageClaimCitationV2, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("evidence citation associations must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _association_projection(self) -> "EvidenceCitationV2":
        if not {item.capture_digest for item in self.citation_associations}.issubset(
            self.capture_digests
        ):
            raise ValueError("evidence citation associations must name its Capture set")
        if not {item.claim_address for item in self.citation_associations}.issubset(
            self.claim_addresses
        ):
            raise ValueError("evidence citation associations must name its Claim set")
        return self


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


class EvidenceCitationIndexV2(EvidenceCitationIndexV1):
    tag: Literal["playbill-coverage-evidence-index-v2"] = "playbill-coverage-evidence-index-v2"  # type: ignore[assignment]
    citations: tuple[EvidenceCitationV2, ...] = ()


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


def build_evidence_citation_index_v2(
    *,
    at: AcceptedCoordinate,
    captures: Iterable[CaptureCitationInputV2],
    claims: Iterable[AcceptedClaim] = (),
    truncated: bool = False,
) -> EvidenceCitationIndexV2:
    """Build the association-native reverse index without changing v1 interpretation."""

    claim_references: dict[
        str,
        list[tuple[SemanticAddress, ClaimCitationReference]],
    ] = {}
    for accepted in claims:
        if accepted.claim.lifecycle.state != "live":
            continue
        address = SemanticAddress.claim_statement(accepted.path)
        for reference in claim_citation_references(accepted.claim):
            claim_references.setdefault(reference.capture_digest, []).append((address, reference))

    rows: dict[tuple[bytes, bytes], _CitationRow] = {}
    associations: dict[tuple[bytes, bytes], dict[tuple[bytes, bytes], CoverageClaimCitationV2]] = {}
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
        for claim_address, raw_reference in claim_references.get(entry.capture_digest, ()):
            association = CoverageClaimCitationV2.model_validate(
                {
                    "claim_address": claim_address.model_dump(mode="json"),
                    "capture_digest": entry.capture_digest,
                    "reference": raw_reference.model_dump(mode="json"),
                    "observation_trust": entry.observation_trust,
                }
            )
            associations.setdefault(key, {})[association.sort_key] = association
            row.claim_paths.add(claim_address.artifact_path)
        if entry.source_handle is not None and row.dereference_handle_digest is None:
            row.dereference_handle_digest = source_handle_digest(entry.source_handle)
        row.access_class = _strictest_access(row.access_class, entry.access_class)

    citations = tuple(
        EvidenceCitationV2(
            commitment_digest=row.commitment_digest,
            digest_kind=row.digest_kind,
            byte_length=row.byte_length,
            accepted_source=row.accepted_source,
            access_class=row.access_class,
            capture_digests=byte_sorted(tuple(row.capture_digests)),
            claim_addresses=tuple(
                SemanticAddress.claim_statement(path) for path in sorted(row.claim_paths)
            ),
            dereference_handle_digest=row.dereference_handle_digest,
            citation_associations=tuple(
                associations.get(key, {})[association_key]
                for association_key in sorted(associations.get(key, {}))
            ),
        )
        for key, row in sorted(rows.items())
    )
    return EvidenceCitationIndexV2(at=at, citations=citations, truncated=truncated)


_ACCESS_STRICTNESS: Mapping[str, int] = {"public": 0, "instance": 1, "restricted": 2}


def _strictest_access(left: SourceAccessClass, right: SourceAccessClass) -> SourceAccessClass:
    return left if _ACCESS_STRICTNESS[left] >= _ACCESS_STRICTNESS[right] else right


def evidence_citation_index_digest(
    index: EvidenceCitationIndexV1 | EvidenceCitationIndexV2,
) -> str:
    """Digest the index so a manifest can name exactly which generation it bound."""

    payload = index.model_dump(mode="json")
    payload.pop("tag")
    domain = (
        EVIDENCE_INDEX_V2_DIGEST_DOMAIN
        if isinstance(index, EvidenceCitationIndexV2)
        else EVIDENCE_INDEX_DIGEST_DOMAIN
    )
    return typed_digest(Sha256Value, domain, payload).tagged


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


class WorkingOccurrenceOverlayV2(_StrictCoverageIndexModel):
    """Per-source complete scan proofs beside disposable source occurrences."""

    tag: Literal["playbill-coverage-occurrence-overlay-v2"] = (
        "playbill-coverage-occurrence-overlay-v2"
    )
    sources: tuple[WorkingSourceCommitmentV1, ...] = ()
    occurrences: tuple[WorkingOccurrenceV1, ...] = ()
    source_scan_proofs: tuple[CoverageCommitmentScanProofV1, ...] = ()
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

    @field_validator("source_scan_proofs")
    @classmethod
    def _source_scan_proofs(
        cls, value: tuple[CoverageCommitmentScanProofV1, ...]
    ) -> tuple[CoverageCommitmentScanProofV1, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("source scan proofs must be sorted and unique")
        return value

    @field_validator("truncation_reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("overlay truncation reasons must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _truncation_is_explained(self) -> "WorkingOccurrenceOverlayV2":
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

    def scanned(
        self,
        source: LogicalSourceIdentityV1,
        commitment_digest: str,
        byte_length: int,
    ) -> bool:
        key = (source.sort_key, commitment_digest.encode("ascii"), byte_length)
        return any(item.sort_key == key for item in self.source_scan_proofs)

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
    wanted: Iterable[tuple[str, int, bytes | None]] = (),
    budget: CoverageScanBudgetV1 = CoverageScanBudgetV1(),
) -> WorkingOccurrenceOverlayV2:
    """Observe the working snapshot: whole-source content, then cited selections.

    Two passes, both deterministic and both free of a clock:

    1. every working source contributes its whole observed content as one
       occurrence, which is what makes a whole-file citation resolvable and what
       the manifest binds its per-source commitments to;
    2. every wanted `(digest, byte_length, verified_needle)` drawn from accepted
       evidence is exhaustively searched in every named source, by overlapping
       needle search when retained bytes are available and by one length-shared
       digest pass otherwise.

    Ordinals are assigned per `(source, observed digest)` in byte order, so an
    occurrence that is unique in its source keeps ordinal 0 no matter where it
    moves to, and duplicated content produces the several distinct identities the
    resolver needs in order to refuse to bind one of them.
    """

    materials = tuple(sorted(sources, key=lambda item: item.source.sort_key))
    keys = [item.source.sort_key for item in materials]
    if len(set(keys)) != len(keys):
        raise CoverageError("a working snapshot names each logical source at most once")

    wanted_values: dict[tuple[str, int], bytes | None] = {}
    for digest, byte_length, verified_needle in wanted:
        Sha256Value.from_tagged(digest)
        if byte_length < 0:
            raise CoverageError("a wanted selection length must be non-negative")
        if byte_length == 0:
            # The empty string has an occurrence at every byte boundary and
            # therefore cannot be enumerated soundly under the bounded scanner.
            continue
        if verified_needle is not None:
            if len(verified_needle) != byte_length:
                raise CoverageError("a verified needle must have the committed byte length")
            if Sha256Value(hashlib.sha256(verified_needle).hexdigest()).tagged != digest:
                raise CoverageError("a verified needle must reproduce the accepted commitment")
        selection_key = (digest, byte_length)
        if selection_key in wanted_values and wanted_values[selection_key] != verified_needle:
            raise CoverageError("a wanted commitment has conflicting materializations")
        wanted_values[selection_key] = verified_needle

    wanted_by_length: dict[int, tuple[tuple[str, bytes | None], ...]] = {}
    for byte_length in sorted({item[1] for item in wanted_values}):
        wanted_by_length[byte_length] = tuple(
            (digest, wanted_values[(digest, byte_length)])
            for digest, length in sorted(wanted_values)
            if length == byte_length
        )

    commitments: list[WorkingSourceCommitmentV1] = []
    found: dict[tuple[bytes, str], list[tuple[int, int]]] = {}
    line_starts: dict[bytes, Sequence[int]] = {}
    proofs: list[CoverageCommitmentScanProofV1] = []
    reasons: set[str] = set()
    remaining = budget.max_scanned_bytes

    for material in materials:
        content = material.content
        source_key = material.source.sort_key
        line_starts[source_key] = _line_starts(content)
        whole = Sha256Value(hashlib.sha256(content).hexdigest()).tagged
        commitments.append(
            WorkingSourceCommitmentV1(
                source=material.source,
                content_digest=whole,
                byte_length=len(content),
            )
        )
        found.setdefault((source_key, whole), []).append((0, len(content)))

    for material in materials:
        source_key = material.source.sort_key
        content = material.content
        for byte_length in sorted(wanted_by_length):
            selections = wanted_by_length[byte_length]
            if len(content) < byte_length:
                proofs.extend(
                    CoverageCommitmentScanProofV1(
                        source=material.source,
                        commitment_digest=digest,
                        byte_length=byte_length,
                    )
                    for digest, _ in selections
                )
                continue

            for digest, needle in selections:
                if needle is None:
                    continue
                offsets: list[int] = []
                start = 0
                while True:
                    offset = content.find(needle, start)
                    if offset < 0:
                        break
                    offsets.append(offset)
                    start = offset + 1
                debit = len(content) + len(needle) + len(offsets) * byte_length
                if debit > remaining:
                    reasons.add("scan_budget_exceeded")
                    continue
                candidate_spans: list[tuple[int, int]] = []
                for offset in offsets:
                    window = content[offset : offset + byte_length]
                    observed = Sha256Value(hashlib.sha256(window).hexdigest()).tagged
                    if observed != digest:  # pragma: no cover - digest equality follows bytes
                        raise CoverageError("needle occurrence failed commitment verification")
                    candidate_spans.append((offset, offset + byte_length))
                remaining -= debit
                found.setdefault((source_key, digest), []).extend(candidate_spans)
                proofs.append(
                    CoverageCommitmentScanProofV1(
                        source=material.source,
                        commitment_digest=digest,
                        byte_length=byte_length,
                    )
                )

            fallback_digests = {digest for digest, needle in selections if needle is None}
            if not fallback_digests:
                continue
            windows = len(content) - byte_length + 1
            debit = windows * byte_length
            if debit > remaining:
                reasons.add("scan_budget_exceeded")
                continue
            fallback_spans: dict[str, list[tuple[int, int]]] = {
                digest: [] for digest in fallback_digests
            }
            for offset in range(windows):
                window = content[offset : offset + byte_length]
                observed = Sha256Value(hashlib.sha256(window).hexdigest()).tagged
                if observed in fallback_digests:
                    fallback_spans[observed].append((offset, offset + byte_length))
            remaining -= debit
            for digest in sorted(fallback_digests):
                found.setdefault((source_key, digest), []).extend(fallback_spans[digest])
                proofs.append(
                    CoverageCommitmentScanProofV1(
                        source=material.source,
                        commitment_digest=digest,
                        byte_length=byte_length,
                    )
                )

    by_key = {item.source.sort_key: item.source for item in materials}
    occurrences: list[WorkingOccurrenceV1] = []
    for (source_key, digest), spans in found.items():
        source = by_key[source_key]
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
                        line_starts[source_key], start_byte=start_byte, end_byte=end_byte
                    ),
                )
            )

    return WorkingOccurrenceOverlayV2(
        sources=tuple(sorted(commitments, key=lambda item: item.source.sort_key)),
        occurrences=tuple(sorted(occurrences, key=lambda item: item.sort_key)),
        source_scan_proofs=tuple(sorted(proofs, key=lambda item: item.sort_key)),
        truncated=bool(reasons),
        truncation_reason_codes=byte_sorted(tuple(reasons)),
    )


def working_occurrence_overlay_digest(
    overlay: WorkingOccurrenceOverlayV1 | WorkingOccurrenceOverlayV2,
) -> str:
    """Digest the overlay so a manifest binds the exact snapshot it observed."""

    payload = overlay.model_dump(mode="json")
    payload.pop("tag")
    domain = (
        OCCURRENCE_OVERLAY_V2_DIGEST_DOMAIN
        if isinstance(overlay, WorkingOccurrenceOverlayV2)
        else OCCURRENCE_OVERLAY_DIGEST_DOMAIN
    )
    return typed_digest(Sha256Value, domain, payload).tagged


__all__ = [
    "DEFAULT_MAX_SCANNED_BYTES",
    "EVIDENCE_INDEX_DIGEST_DOMAIN",
    "EVIDENCE_INDEX_V2_DIGEST_DOMAIN",
    "OCCURRENCE_OVERLAY_DIGEST_DOMAIN",
    "OCCURRENCE_OVERLAY_V2_DIGEST_DOMAIN",
    "CaptureCitationInputV1",
    "CaptureCitationInputV2",
    "CoverageScanBudgetV1",
    "EvidenceCitationIndexV1",
    "EvidenceCitationIndexV2",
    "EvidenceCitationV1",
    "EvidenceCitationV2",
    "WorkingOccurrenceOverlayV1",
    "WorkingOccurrenceOverlayV2",
    "WorkingOccurrenceV1",
    "WorkingSourceCommitmentV1",
    "WorkingSourceContent",
    "accepted_logical_source",
    "build_evidence_citation_index",
    "build_evidence_citation_index_v2",
    "build_working_occurrence_overlay",
    "evidence_citation_index_digest",
    "working_occurrence_overlay_digest",
]
