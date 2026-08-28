"""Citation freshness follows verified selections, never unrelated source bytes."""

from __future__ import annotations

from cruxible_core.playbill.coverage.contracts import (
    CoverageCommitmentScanProofV1,
    CoverageLineOverlayV1,
    LogicalSourceIdentityV1,
    PlaybillCitationWindowObservationV1,
    occurrence_identity_digest,
)
from cruxible_core.playbill.coverage.indexes import WorkingOccurrenceV1
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_next import (
    PlaybillNextSourceObservationV3,
    PlaybillNextSourceObservationV4,
    _CitationCommitment,
    _source_citation_item,
)
from tests.test_playbill._coverage_support import coordinate

CITATION = "sha256:" + "1" * 64
SELECTION = "sha256:" + "2" * 64
ORIGINAL_SOURCE = "sha256:" + "3" * 64
CHANGED_SOURCE = "sha256:" + "4" * 64
SOURCE_ID = "corpus.runbook"


def _commitment(
    *, whole_source: bool = False, with_original_span: bool = False
) -> _CitationCommitment:
    return _CitationCommitment(
        citation_id=CITATION,
        commitment_digest=SELECTION,
        byte_length=10,
        claim_identity="Claim:CLM-example",
        source_id=SOURCE_ID,
        source_digest=ORIGINAL_SOURCE,
        original_start=5 if with_original_span else None,
        original_end=15 if with_original_span else None,
        whole_source=whole_source,
    )


def _occurrence(start: int = 5, *, ordinal: int = 0) -> WorkingOccurrenceV1:
    source = LogicalSourceIdentityV1(plane="external", identity=SOURCE_ID)
    return WorkingOccurrenceV1(
        source=source,
        observed_commitment_digest=SELECTION,
        byte_length=10,
        ordinal=ordinal,
        identity_digest=occurrence_identity_digest(
            source=source,
            observed_commitment_digest=SELECTION,
            ordinal=ordinal,
        ),
        line_overlay=CoverageLineOverlayV1(
            start_byte=start,
            end_byte=start + 10,
            start_line=1,
            end_line=1,
        ),
    )


def _observed(
    *,
    occurrences: tuple[WorkingOccurrenceV1, ...] = (),
    scanned: tuple[str, ...] = (),
    complete: bool = True,
    digest: str = CHANGED_SOURCE,
) -> PlaybillNextSourceObservationV3:
    return PlaybillNextSourceObservationV3(
        tag="playbill-next-source-observation-v3",
        source_id=SOURCE_ID,
        observed_source_digest=digest,
        byte_length=100,
        marker_summaries=(),
        occurrences=occurrences,
        scanned_commitment_digests=scanned,
        scan_complete=complete,
        scan_notes=() if complete else ("coverage_partial",),
        marker_notes=(),
    )


def _repair(
    observed: PlaybillNextSourceObservationV3 | None,
    *,
    whole_source: bool = False,
):  # type: ignore[no-untyped-def]
    return _source_citation_item(
        citation_id=CITATION,
        commitment=_commitment(whole_source=whole_source),
        observed=observed,
        coordinate=PlaybillAcceptedCoordinate.model_validate(coordinate().model_dump()),
    )


def _observed_v4(
    *,
    occurrences: tuple[WorkingOccurrenceV1, ...] = (),
    proof: bool = True,
    addressable: bool = True,
    observed_window_digest: str | None = CHANGED_SOURCE,
) -> PlaybillNextSourceObservationV4:
    source = LogicalSourceIdentityV1(plane="external", identity=SOURCE_ID)
    return PlaybillNextSourceObservationV4(
        source_id=SOURCE_ID,
        observed_source_digest=CHANGED_SOURCE,
        byte_length=100,
        marker_summaries=(),
        occurrences=occurrences,
        commitment_scan_proofs=(
            CoverageCommitmentScanProofV1(
                source=source,
                commitment_digest=SELECTION,
                byte_length=10,
            ),
        )
        if proof
        else (),
        citation_window_observations=(
            PlaybillCitationWindowObservationV1(
                source=source,
                citation_id=CITATION,
                commitment_digest=SELECTION,
                original_start=5,
                original_end=15,
                addressable=addressable,
                observed_window_digest=observed_window_digest if addressable else None,
            ),
        ),
        scan_notes=(),
        marker_notes=(),
    )


def _repair_v4(observed: PlaybillNextSourceObservationV4):  # type: ignore[no-untyped-def]
    return _source_citation_item(
        citation_id=CITATION,
        commitment=_commitment(with_original_span=True),
        observed=observed,
        coordinate=PlaybillAcceptedCoordinate.model_validate(coordinate().model_dump()),
    )


def test_unchanged_uniquely_rediscovered_span_survives_unrelated_source_edits() -> None:
    assert _repair(_observed(occurrences=(_occurrence(),), scanned=(SELECTION,))) is None
    assert _repair(_observed(occurrences=(_occurrence(80),), scanned=(SELECTION,))) is None


def test_zero_matches_after_complete_selection_scan_is_drift() -> None:
    result = _repair(_observed(scanned=(SELECTION,)))

    assert result is not None
    assert result.reason == "citation_drifted"
    assert result.detail["expected_commitment_digest"] == SELECTION
    assert result.detail["drift_state"] == "changed"


def test_incomplete_or_unscanned_selection_is_unobserved_never_drift() -> None:
    for observation in (_observed(complete=False), _observed(), None):
        result = _repair(observation)
        assert result is not None
        assert result.reason == "citation_source_unobserved"


def test_whole_source_citation_drifts_on_any_byte_change_even_if_span_is_found() -> None:
    moved = _observed(occurrences=(_occurrence(),), scanned=(SELECTION,))

    result = _repair(moved, whole_source=True)

    assert result is not None
    assert result.reason == "citation_drifted"
    assert _repair(_observed(digest=ORIGINAL_SOURCE), whole_source=True) is None


def test_occurrence_presentation_offsets_never_change_the_queue_item_identity() -> None:
    first = _repair(
        _observed(occurrences=(_occurrence(5),), scanned=(SELECTION,)), whole_source=True
    )
    relocated = _repair(
        _observed(occurrences=(_occurrence(80),), scanned=(SELECTION,)), whole_source=True
    )

    assert first is not None and relocated is not None
    assert first.item_id == relocated.item_id
    assert first == relocated


def test_v4_unique_moved_occurrence_is_current_even_when_source_digest_changed() -> None:
    assert _repair_v4(_observed_v4(occurrences=(_occurrence(80),))) is None


def test_v4_zero_occurrence_distinguishes_changed_gone_and_unobserved() -> None:
    changed = _repair_v4(_observed_v4())
    gone = _repair_v4(_observed_v4(addressable=False, observed_window_digest=None))
    unobserved = _repair_v4(_observed_v4(proof=False))

    assert changed is not None and changed.detail["drift_state"] == "changed"
    assert changed.repair.required_change == "adjudicate_citation_drift"
    assert gone is not None and gone.detail["drift_state"] == "gone"
    assert unobserved is not None and unobserved.reason == "citation_source_unobserved"


def test_v4_multiple_exact_occurrences_are_ambiguous_not_unobserved() -> None:
    result = _repair_v4(
        _observed_v4(
            occurrences=(
                _occurrence(5, ordinal=0),
                _occurrence(50, ordinal=1),
            )
        )
    )

    assert result is not None
    assert result.detail["drift_state"] == "ambiguous"
    assert result.detail["exact_occurrence_count"] == 2
