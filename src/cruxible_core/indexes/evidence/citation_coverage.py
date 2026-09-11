"""Coverage's retained full export, assembled from the shared SQL relationships.

The historical digest commits the complete accepted citation set. Exporting it
must visit that set, but does not decode all Claims or rebuild a second reverse
index. Capture availability and contract trust are checked for this read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, overload

from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.captures import (
    CaptureEnvelopeAny,
    capture_contract_digest,
    capture_contract_is_self_asserted,
    parse_capture_contract,
)
from cruxible_client.contracts.cas_contracts import BodyProjectionProtocol
from cruxible_client.contracts.claim_verdicts import ObservationTrustGrade, observation_trust_grade
from cruxible_client.contracts.claims import (
    ClaimCitationReference,
    ClaimCitationV1,
    LegacyCitationReferenceV1,
)
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.coverage.contracts import CoverageClaimCitationV2
from cruxible_core.coverage.indexes import (
    EvidenceCitationIndexV1,
    EvidenceCitationIndexV2,
    EvidenceCitationV1,
    EvidenceCitationV2,
    accepted_logical_source,
)
from cruxible_core.indexes.projection import AcceptedCoordinate

if TYPE_CHECKING:
    from cruxible_core.indexes.evidence.citation_sql import CitationReader


@overload
def coverage_rows(
    reader: CitationReader,
    *,
    bodies: BodyProjectionProtocol,
    at: AcceptedCoordinate,
    version: Literal[1],
) -> tuple[EvidenceCitationIndexV1, dict[str, CaptureEnvelopeAny]]: ...


@overload
def coverage_rows(
    reader: CitationReader,
    *,
    bodies: BodyProjectionProtocol,
    at: AcceptedCoordinate,
    version: Literal[2] = 2,
) -> tuple[EvidenceCitationIndexV2, dict[str, CaptureEnvelopeAny]]: ...


def coverage_rows(
    reader: CitationReader,
    *,
    bodies: BodyProjectionProtocol,
    at: AcceptedCoordinate,
    version: Literal[1, 2] = 2,
) -> tuple[EvidenceCitationIndexV1 | EvidenceCitationIndexV2, dict[str, CaptureEnvelopeAny]]:
    """Reproduce retained V1/V2 bytes using indexed joins and exact CAS envelopes."""
    groups = reader._rows(
        "SELECT DISTINCT p.evidence_commitment_digest,s.kind,p.logical_source_id FROM captures p "
        "JOIN source_references s USING(source_ref_key) WHERE EXISTS "
        "(SELECT 1 FROM citation_uses u WHERE u.capture_digest=p.capture_digest "
        "AND u.owner_kind='Claim') "
        "ORDER BY p.evidence_commitment_digest,s.kind,p.logical_source_id"
    )
    envelopes: dict[str, CaptureEnvelopeAny] = {}
    trust_by_contract: dict[str, ObservationTrustGrade] = {}
    citations: list[EvidenceCitationV1] = []
    for group in groups:
        captures = reader._rows(
            "SELECT p.capture_digest FROM captures p "
            "JOIN source_references s USING(source_ref_key) "
            "WHERE p.evidence_commitment_digest=? AND s.kind=? AND p.logical_source_id IS ? "
            "AND EXISTS (SELECT 1 FROM citation_uses u WHERE u.capture_digest=p.capture_digest "
            "AND u.owner_kind='Claim') ORDER BY p.capture_digest",
            (group["evidence_commitment_digest"], group["kind"], group["logical_source_id"]),
        )
        addresses: set[str] = set()
        associations: dict[tuple[bytes, bytes], CoverageClaimCitationV2] = {}
        first: CaptureEnvelopeAny | None = None
        for capture in captures:
            digest = str(capture["capture_digest"])
            envelope = reader._envelope(digest, bodies, envelopes)
            if first is None:
                first = envelope
            trust: ObservationTrustGrade = "proposer_observed"
            if version == 2:
                contract_digest = envelope.capture_contract_digest
                known_trust = trust_by_contract.get(contract_digest)
                if known_trust is None:
                    path = reader.capture_contract_path(contract_digest)
                    if path is None:
                        raise ProjectionFormatError("accepted CaptureContract is unavailable")
                    contract = parse_capture_contract(reader.exact_source(path), path=path)
                    if capture_contract_digest(contract).tagged != contract_digest:
                        raise ProjectionFormatError(
                            "accepted CaptureContract differs from its digest"
                        )
                    known_trust = observation_trust_grade(
                        "self-asserted"
                        if capture_contract_is_self_asserted(contract)
                        else "daemon-fetched"
                    )
                    trust_by_contract[contract_digest] = known_trust
                trust = known_trust
            for use in reader._rows(
                "SELECT u.*,c.path FROM citation_uses u JOIN claims c ON c.identity=u.owner_key "
                "WHERE u.capture_digest=? AND u.owner_kind='Claim' AND c.lifecycle='live' "
                "ORDER BY c.path,u.use_key",
                (digest,),
            ):
                path = str(use["path"])
                addresses.add(path)
                if version == 1:
                    continue
                reference: ClaimCitationReference
                if use["origin"] == "legacy" and use["role"] == "legacy":
                    reference = LegacyCitationReferenceV1(
                        citation_id=str(use["use_key"]),
                        capture_digest=digest,
                        claim_identity=parse_artifact_identity(str(use["owner_key"])),
                    )
                else:
                    reference = ClaimCitationV1.model_validate(
                        {
                            "citation_id": use["use_key"],
                            "capture_digest": digest,
                            "role": use["role"],
                            "origin": use["origin"],
                        }
                    )
                association = CoverageClaimCitationV2(
                    claim_address=SemanticAddress.claim_statement(path),
                    capture_digest=digest,
                    reference=reference,
                    observation_trust=trust,
                )
                associations[association.sort_key] = association
        assert first is not None
        values = dict(
            commitment_digest=first.commitment.digest,
            digest_kind=first.commitment.digest_kind,
            byte_length=first.commitment.byte_length,
            accepted_source=accepted_logical_source(first.source),
            access_class="instance",
            capture_digests=tuple(str(capture["capture_digest"]) for capture in captures),
            claim_addresses=tuple(
                SemanticAddress.claim_statement(path) for path in sorted(addresses)
            ),
        )
        if version == 2:
            citations.append(
                EvidenceCitationV2.model_validate(
                    {
                        **values,
                        "citation_associations": tuple(
                            associations[key] for key in sorted(associations)
                        ),
                    }
                )
            )
        else:
            citations.append(EvidenceCitationV1.model_validate(values))
    ordered = tuple(sorted(citations, key=lambda item: item.sort_key))
    if version == 2:
        return EvidenceCitationIndexV2.model_validate({"at": at, "citations": ordered}), envelopes
    return EvidenceCitationIndexV1(at=at, citations=ordered), envelopes
