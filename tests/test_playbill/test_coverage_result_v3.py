"""PC-G12b coverage-v3 local-health and observation contract laws."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.coverage.contracts import (
    CoverageRequestV1,
    CoverageResultV1,
    CoverageResultV3,
    CoverageSpanRequestV1,
    LogicalSourceIdentityV1,
    PlaybillCitationWindowObservationV1,
)
from cruxible_core.playbill.coverage.indexes import (
    CaptureCitationInputV2,
    CoverageScanBudgetV1,
    WorkingSourceContent,
    build_evidence_citation_index_v2,
    build_working_occurrence_overlay,
)
from cruxible_core.playbill.coverage.manifest import coverage_manifest_body_v2
from cruxible_core.playbill.coverage.resolver import resolve_coverage_v3
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
    assert result.spans[1].health == "complete"


def test_coverage_v3_is_fresh_instead_of_inheriting_the_v1_batch_validator() -> None:
    assert not issubclass(CoverageResultV3, CoverageResultV1)


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
