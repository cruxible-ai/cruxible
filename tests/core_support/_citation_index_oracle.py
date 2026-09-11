"""Frozen pre-SQL coverage builders used only as independent parity oracles."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes
from cruxible_client.contracts.captures import CaptureEnvelopeAny
from cruxible_client.contracts.claim_verdicts import ObservationTrustGrade
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimCitationReference,
    claim_citation_references,
)
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    SourceAccessClass,
    SourceHandleV1,
    source_handle_digest,
)
from cruxible_core.coverage.contracts import CoverageClaimCitationV2, LogicalSourceIdentityV1
from cruxible_core.coverage.indexes import (
    EvidenceCitationIndexV1,
    EvidenceCitationIndexV2,
    EvidenceCitationV1,
    EvidenceCitationV2,
    _StrictCoverageIndexModel,
    accepted_logical_source,
)
from cruxible_core.indexes.projection import AcceptedCoordinate


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
    envelope: CaptureEnvelopeAny
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
