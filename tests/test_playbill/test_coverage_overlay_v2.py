"""PC-G12b local scan-proof and retained-needle laws."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.captures import render_capture_envelope
from cruxible_client.contracts.errors import PlaybillCasError
from cruxible_core.playbill.coverage.contracts import (
    CoverageCommitmentMaterializationCorrupt,
)
from cruxible_core.playbill.coverage.indexes import (
    CaptureCitationInputV2,
    CoverageScanBudgetV1,
    WorkingOccurrenceOverlayV2,
    WorkingSourceContent,
    build_evidence_citation_index_v2,
    build_working_occurrence_overlay,
)
from cruxible_core.service.playbill_coverage import _materialized_wanted_selections
from tests.test_playbill._coverage_support import (
    CITED,
    HANDBOOK,
    SCRATCH,
    capture,
    coordinate,
    sha256,
)


def _source(source, content: bytes) -> WorkingSourceContent:  # type: ignore[no-untyped-def]
    return WorkingSourceContent(source=source, content=content)


def test_needle_and_fallback_routes_reproduce_overlapping_last_window_occurrences() -> None:
    content = b"aaaa"
    needle = b"aa"
    digest = sha256(needle)

    needle_overlay = build_working_occurrence_overlay(
        (_source(HANDBOOK, content),),
        wanted=((digest, len(needle), needle),),
    )
    fallback_overlay = build_working_occurrence_overlay(
        (_source(HANDBOOK, content),),
        wanted=((digest, len(needle), None),),
    )

    assert needle_overlay.sources == fallback_overlay.sources
    assert needle_overlay.occurrences == fallback_overlay.occurrences
    assert needle_overlay.source_scan_proofs == fallback_overlay.source_scan_proofs
    assert tuple(
        occurrence.line_overlay.start_byte
        for occurrence in needle_overlay.occurrences
        if occurrence.observed_commitment_digest == digest
    ) == (0, 1, 2)


def test_route_budget_admission_is_atomic_and_shared_fallback_is_not_k_fold() -> None:
    content = b"abcd"
    first = b"ab"
    second = b"cd"
    fallback_debit = 3 * 2

    admitted = build_working_occurrence_overlay(
        (_source(HANDBOOK, content),),
        wanted=((sha256(first), 2, None), (sha256(second), 2, None)),
        budget=CoverageScanBudgetV1(max_scanned_bytes=fallback_debit),
    )
    refused = build_working_occurrence_overlay(
        (_source(HANDBOOK, content),),
        wanted=((sha256(first), 2, None), (sha256(second), 2, None)),
        budget=CoverageScanBudgetV1(max_scanned_bytes=fallback_debit - 1),
    )

    assert len(admitted.source_scan_proofs) == 2
    assert refused.source_scan_proofs == ()
    assert refused.truncation_reason_codes == ("scan_budget_exceeded",)


def test_needle_route_debits_the_whole_pair_before_admitting_its_proof() -> None:
    content = b"ababa"
    needle = b"aba"
    pair_debit = len(content) + len(needle) + 2 * len(needle)

    admitted = build_working_occurrence_overlay(
        (_source(HANDBOOK, content),),
        wanted=((sha256(needle), len(needle), needle),),
        budget=CoverageScanBudgetV1(max_scanned_bytes=pair_debit),
    )
    refused = build_working_occurrence_overlay(
        (_source(HANDBOOK, content),),
        wanted=((sha256(needle), len(needle), needle),),
        budget=CoverageScanBudgetV1(max_scanned_bytes=pair_debit - 1),
    )

    assert len(admitted.source_scan_proofs) == 1
    assert len(admitted.occurrences) == 3  # whole source plus two overlapping needles
    assert refused.source_scan_proofs == ()
    assert len(refused.occurrences) == 1


def test_one_commitment_can_be_proven_while_another_stays_unobserved_in_one_source() -> None:
    content = b"abc---xyz"
    selections = sorted(((sha256(b"abc"), 3, b"abc"), (sha256(b"xyz"), 3, b"xyz")))
    one_pair_debit = len(content) + 3 + 3

    overlay = build_working_occurrence_overlay(
        (_source(HANDBOOK, content),),
        wanted=selections,
        budget=CoverageScanBudgetV1(max_scanned_bytes=one_pair_debit),
    )

    first_digest, first_length, _ = selections[0]
    second_digest, second_length, _ = selections[1]
    assert overlay.scanned(HANDBOOK, first_digest, first_length) is True
    assert overlay.scanned(HANDBOOK, second_digest, second_length) is False
    assert overlay.truncation_reason_codes == ("scan_budget_exceeded",)


def test_zero_length_commitments_never_bypass_the_bounded_scanner() -> None:
    empty = sha256(b"")

    overlay = build_working_occurrence_overlay(
        (_source(HANDBOOK, b"six boundaries"),),
        wanted=((empty, 0, b""),),
        budget=CoverageScanBudgetV1(max_scanned_bytes=0),
    )

    assert overlay.scanned(HANDBOOK, empty, 0) is False
    assert all(item.observed_commitment_digest != empty for item in overlay.occurrences)


def test_one_source_completion_survives_another_source_truncation() -> None:
    digest = sha256(b"abc")
    overlay = build_working_occurrence_overlay(
        (_source(HANDBOOK, b"x"), _source(SCRATCH, b"abc")),
        wanted=((digest, 3, None),),
        budget=CoverageScanBudgetV1(max_scanned_bytes=0),
    )

    assert overlay.scanned(HANDBOOK, digest, 3) is True
    assert overlay.scanned(SCRATCH, digest, 3) is False
    assert overlay.truncated is True


def test_overlay_v2_refuses_duplicate_source_proof_identity() -> None:
    digest = sha256(CITED)
    proof = build_working_occurrence_overlay(
        (_source(HANDBOOK, b"short"),),
        wanted=((digest, len(CITED), None),),
    ).source_scan_proofs[0]

    with pytest.raises(ValidationError, match="sorted and unique"):
        WorkingOccurrenceOverlayV2(source_scan_proofs=(proof, proof))


def test_present_corrupt_cas_material_refuses_instead_of_falling_back() -> None:
    accepted = capture(None, CITED)
    capture_v2 = CaptureCitationInputV2(
        capture_digest=accepted.capture_digest,
        envelope=accepted.envelope,
        access_class=accepted.access_class,
        observation_trust="proposer_observed",
    )
    index = build_evidence_citation_index_v2(at=coordinate(), captures=(capture_v2,))
    envelope_bytes = render_capture_envelope(accepted.envelope)

    class _CorruptStore:
        def metadata(self, digest: str, *, access: object) -> object:
            return SimpleNamespace(present=True)

        def read(self, digest: str, *, access: object) -> bytes:
            if digest == accepted.capture_digest:
                return envelope_bytes
            raise PlaybillCasError("corrupt retained object")

    instance = SimpleNamespace(body_store=lambda: _CorruptStore())
    with pytest.raises(
        CoverageCommitmentMaterializationCorrupt,
        match="failed CAS verification",
    ):
        _materialized_wanted_selections(instance, index=index)  # type: ignore[arg-type]
