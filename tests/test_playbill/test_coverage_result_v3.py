"""PC-G12b coverage-v3 local-health and observation contract laws."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.authoring.workspace import _coverage_v3_fields
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.claims import LegacyCitationReferenceV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.coverage.adapter import observe_working_source
from cruxible_core.playbill.coverage.contracts import (
    CoverageCardBudgetV1,
    CoverageRequestV1,
    CoverageResultAny,
    CoverageResultV3,
    CoverageSpanRequestV1,
    LogicalSourceIdentityV1,
    PlaybillCitationWindowObservationV1,
)
from cruxible_core.playbill.coverage.indexes import (
    CaptureCitationInputV2,
    CoverageClaimCitationV2,
    CoverageScanBudgetV1,
    EvidenceCitationIndexV2,
    WorkingSourceContent,
    build_evidence_citation_index_v2,
    build_working_occurrence_overlay,
)
from cruxible_core.playbill.coverage.manifest import coverage_manifest_body_v2
from cruxible_core.playbill.coverage.resolver import resolve_coverage_v3
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_coverage import _citation_window_observations
from cruxible_core.service.playbill_next import (
    PlaybillNextSourceObservationV4,
    _CitationCommitment,
    _source_citation_item,
)
from tests.test_playbill._coverage_support import CITED, capture, coordinate, profile, sha256

SOURCE_A = LogicalSourceIdentityV1(plane="ledger", identity="documents/a.md")
SOURCE_B = LogicalSourceIdentityV1(plane="ledger", identity="documents/b.md")


def test_local_proof_stays_complete_and_exact_inside_a_partial_batch() -> None:
    accepted = capture(SOURCE_A, CITED)
    index = build_evidence_citation_index_v2(
        at=coordinate(),
        captures=(
            CaptureCitationInputV2(
                capture_digest=accepted.capture_digest,
                envelope=accepted.envelope,
                access_class=accepted.access_class,
                observation_trust="proposer_observed",
            ),
        ),
    )
    overlay = build_working_occurrence_overlay(
        (
            WorkingSourceContent(source=SOURCE_A, content=CITED),
            WorkingSourceContent(source=SOURCE_B, content=b"x" * len(CITED)),
        ),
        wanted=((sha256(CITED), len(CITED), None),),
        budget=CoverageScanBudgetV1(max_scanned_bytes=len(CITED)),
    )
    manifest = coverage_manifest_body_v2(
        instance_id="inst_coverage",
        index=index,
        overlay=overlay,
        access_profile=profile(),
    )

    result = resolve_coverage_v3(
        CoverageRequestV1(
            instance_id="inst_coverage",
            at=coordinate(),
            spans=(
                CoverageSpanRequestV1(source=SOURCE_A),
                CoverageSpanRequestV1(source=SOURCE_B),
            ),
        ),
        index=index,
        overlay=overlay,
        access=profile(),
        manifest=manifest,
    )

    assert result.global_scan_complete is False
    assert result.health == "partial"
    assert result.truncation_reason_codes == ("scan_budget_exceeded",)
    assert result.spans[0].match_state == "exact"
    assert result.spans[0].health == "complete"
    assert result.spans[0].commitment_scan_proofs == overlay.source_scan_proofs[:1]
    assert result.spans[1].health == "partial"
    assert result.spans[1].absence_is_factual is False
    assert result.spans[1].coverage.reason_codes == ("unscanned_selection",)


def test_the_served_coverage_result_type_is_v3_only() -> None:
    assert CoverageResultAny is CoverageResultV3


def test_citation_window_observation_refuses_incoherent_addressability() -> None:
    values = {
        "source": SOURCE_A,
        "citation_id": sha256(b"citation"),
        "commitment_digest": sha256(CITED),
        "original_start": 0,
        "original_end": len(CITED),
    }
    with pytest.raises(ValidationError, match="addressable citation window"):
        PlaybillCitationWindowObservationV1(
            **values,
            addressable=True,
            observed_window_digest=None,
        )
    with pytest.raises(ValidationError, match="addressable citation window"):
        PlaybillCitationWindowObservationV1(
            **values,
            addressable=False,
            observed_window_digest=sha256(CITED),
        )


def test_same_bytes_in_another_source_never_count_as_the_cited_source() -> None:
    accepted = capture(SOURCE_A, CITED)
    index = build_evidence_citation_index_v2(
        at=coordinate(),
        captures=(
            CaptureCitationInputV2(
                capture_digest=accepted.capture_digest,
                envelope=accepted.envelope,
                access_class=accepted.access_class,
                observation_trust="proposer_observed",
            ),
        ),
    )
    overlay = build_working_occurrence_overlay(
        (
            WorkingSourceContent(source=SOURCE_A, content=b"x" * len(CITED)),
            WorkingSourceContent(source=SOURCE_B, content=CITED),
        ),
        wanted=((sha256(CITED), len(CITED), CITED),),
    )
    manifest = coverage_manifest_body_v2(
        instance_id="inst_coverage",
        index=index,
        overlay=overlay,
        access_profile=profile(),
    )

    result = resolve_coverage_v3(
        CoverageRequestV1(
            instance_id="inst_coverage",
            at=coordinate(),
            spans=(
                CoverageSpanRequestV1(source=SOURCE_A),
                CoverageSpanRequestV1(source=SOURCE_B),
            ),
            budget=CoverageCardBudgetV1(
                max_cards_per_span=8,
                max_candidate_cards_per_span=8,
            ),
        ),
        index=index,
        overlay=overlay,
        access=profile(),
        manifest=manifest,
    )

    assert result.spans[0].match_state == "drifted"
    assert result.spans[1].match_state == "candidate"
    assert all(card.match_state != "exact" for span in result.spans for card in span.cards)


def test_clipped_occurrence_enumeration_never_keeps_its_scan_proof() -> None:
    source = LogicalSourceIdentityV1(plane="external", identity="corpus.clipped")
    accepted = capture(source, CITED)
    index = build_evidence_citation_index_v2(
        at=coordinate(),
        captures=(
            CaptureCitationInputV2(
                capture_digest=accepted.capture_digest,
                envelope=accepted.envelope,
                access_class=accepted.access_class,
                observation_trust="proposer_observed",
            ),
        ),
    )
    content = b"---".join(CITED for _ in range(257))
    overlay = build_working_occurrence_overlay(
        (WorkingSourceContent(source=source, content=content),),
        wanted=((sha256(CITED), len(CITED), CITED),),
    )
    manifest = coverage_manifest_body_v2(
        instance_id="inst_coverage",
        index=index,
        overlay=overlay,
        access_profile=profile(),
    )

    result = resolve_coverage_v3(
        CoverageRequestV1(
            instance_id="inst_coverage",
            at=coordinate(),
            spans=(CoverageSpanRequestV1(source=source),),
            budget=CoverageCardBudgetV1(
                max_cards_per_span=256,
                max_candidate_cards_per_span=256,
            ),
        ),
        index=index,
        overlay=overlay,
        access=profile(),
        manifest=manifest,
    )

    (span,) = result.spans
    assert span.omitted_card_count == 1
    assert span.commitment_scan_proofs == ()
    assert span.health == "partial"
    assert span.absence_is_factual is False


def test_duplicate_anchor_card_clipping_remains_ambiguous_instead_of_false_current() -> None:
    source = LogicalSourceIdentityV1(plane="external", identity="corpus.source000")
    entries: list[CaptureCitationInputV2] = []
    for ordinal in range(250):
        accepted = capture(
            LogicalSourceIdentityV1(plane="external", identity=f"corpus.source{ordinal:03d}"),
            CITED,
            name=f"source-{ordinal:03d}",
        )
        entries.append(
            CaptureCitationInputV2(
                capture_digest=accepted.capture_digest,
                envelope=accepted.envelope,
                access_class=accepted.access_class,
                observation_trust="proposer_observed",
            )
        )
    index = build_evidence_citation_index_v2(at=coordinate(), captures=tuple(entries))
    content = CITED + b"---" + CITED
    overlay = build_working_occurrence_overlay(
        (WorkingSourceContent(source=source, content=content),),
        wanted=((sha256(CITED), len(CITED), CITED),),
    )
    manifest = coverage_manifest_body_v2(
        instance_id="inst_coverage",
        index=index,
        overlay=overlay,
        access_profile=profile(),
    )
    result = resolve_coverage_v3(
        CoverageRequestV1(
            instance_id="inst_coverage",
            at=coordinate(),
            spans=(CoverageSpanRequestV1(source=source),),
            budget=CoverageCardBudgetV1(
                max_cards_per_span=256,
                max_candidate_cards_per_span=256,
            ),
        ),
        index=index,
        overlay=overlay,
        access=profile(),
        manifest=manifest,
    )
    (span,) = result.spans
    assert span.omitted_card_count == 244
    assert len(span.commitment_scan_proofs) == 1

    occurrences, proofs, windows, notes = _coverage_v3_fields(
        span.model_dump(mode="json"),
        source_id=source.identity,
        content=content,
    )
    assert len(occurrences) == 2
    observation = PlaybillNextSourceObservationV4.model_validate(
        {
            "source_id": source.identity,
            "observed_source_digest": sha256(content),
            "byte_length": len(content),
            "marker_summaries": [],
            "occurrences": occurrences,
            "commitment_scan_proofs": proofs,
            "citation_window_observations": windows,
            "scan_notes": notes,
            "marker_notes": [],
        }
    )
    item = _source_citation_item(
        citation_id=sha256(b"citation"),
        commitment=_CitationCommitment(
            citation_id=sha256(b"citation"),
            commitment_digest=sha256(CITED),
            byte_length=len(CITED),
            claim_identity="Claim:CLM-00000000000000000000000000000000",
            source_id=source.identity,
            source_digest=sha256(CITED),
            original_start=0,
            original_end=len(CITED),
        ),
        observed=observation,
        coordinate=PlaybillAcceptedCoordinate.model_validate(coordinate().model_dump()),
    )
    assert item is not None
    assert item.reason == "citation_drifted"
    assert item.detail["drift_state"] == "ambiguous"


def test_ledger_reference_without_a_window_never_fabricates_one() -> None:
    accepted = capture(SOURCE_A, CITED)
    base = build_evidence_citation_index_v2(
        at=coordinate(),
        captures=(
            CaptureCitationInputV2(
                capture_digest=accepted.capture_digest,
                envelope=accepted.envelope,
                access_class=accepted.access_class,
                observation_trust="proposer_observed",
            ),
        ),
    )
    claim_identity = ArtifactIdentity(kind="Claim", name="CLM-00000000000000000000000000000000")
    citation_id = typed_digest(
        Sha256Value,
        "playbill-legacy-claim-citation-v1",
        {
            "claim_identity": claim_identity.model_dump(mode="json"),
            "capture_digest": accepted.capture_digest,
        },
    ).tagged
    reference = LegacyCitationReferenceV1(
        citation_id=citation_id,
        claim_identity=claim_identity,
        capture_digest=accepted.capture_digest,
    )
    address = SemanticAddress.claim_statement("claims/00/CLM-00000000000000000000000000000000.yaml")
    association = CoverageClaimCitationV2(
        claim_address=address,
        capture_digest=accepted.capture_digest,
        reference=reference,
        observation_trust="proposer_observed",
    )
    citation = base.citations[0].model_copy(
        update={
            "claim_addresses": (address,),
            "citation_associations": (association,),
        }
    )
    index = EvidenceCitationIndexV2(at=base.at, citations=(citation,))

    windows = _citation_window_observations(
        index=index,
        observations=(observe_working_source(SOURCE_A, CITED),),
        envelopes={accepted.capture_digest: accepted.envelope},
    )

    assert windows == ()
