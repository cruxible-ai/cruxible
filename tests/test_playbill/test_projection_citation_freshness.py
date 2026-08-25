"""Citation freshness follows verified selections, never unrelated source bytes."""

from __future__ import annotations

from cruxible_core.playbill.coverage.contracts import (
    CoverageLineOverlayV1,
    LogicalSourceIdentityV1,
    occurrence_identity_digest,
)
from cruxible_core.playbill.coverage.indexes import WorkingOccurrenceV1
from cruxible_core.service.playbill_next import (
    PlaybillNextSourceObservationV1,
    PlaybillNextSourceObservationV2,
    _CitationCommitment,
    _source_citation_item,
)

CITATION = "sha256:" + "1" * 64
SELECTION = "sha256:" + "2" * 64
ORIGINAL_SOURCE = "sha256:" + "3" * 64
CHANGED_SOURCE = "sha256:" + "4" * 64
SOURCE_ID = "corpus.runbook"


def _commitment(*, whole_source: bool = False) -> _CitationCommitment:
    return _CitationCommitment(
        commitment_digest=SELECTION,
        claim_identity="Claim:CLM-example",
        source_id=SOURCE_ID,
        source_digest=ORIGINAL_SOURCE,
        whole_source=whole_source,
    )


def _occurrence(start: int = 5) -> WorkingOccurrenceV1:
    source = LogicalSourceIdentityV1(plane="external", identity=SOURCE_ID)
    return WorkingOccurrenceV1(
        source=source,
        observed_commitment_digest=SELECTION,
        byte_length=10,
        ordinal=0,
        identity_digest=occurrence_identity_digest(
            source=source,
            observed_commitment_digest=SELECTION,
            ordinal=0,
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
) -> PlaybillNextSourceObservationV2:
    return PlaybillNextSourceObservationV2(
        tag="playbill-next-source-observation-v2",
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
    observed: PlaybillNextSourceObservationV1 | PlaybillNextSourceObservationV2 | None,
    *,
    whole_source: bool = False,
):  # type: ignore[no-untyped-def]
    return _source_citation_item(
        citation_id=CITATION,
        commitment=_commitment(whole_source=whole_source),
        observed=observed,
    )


def test_unchanged_uniquely_rediscovered_span_survives_unrelated_source_edits() -> None:
    assert _repair(_observed(occurrences=(_occurrence(),), scanned=(SELECTION,))) is None
    assert _repair(_observed(occurrences=(_occurrence(80),), scanned=(SELECTION,))) is None


def test_zero_matches_after_complete_selection_scan_is_drift() -> None:
    result = _repair(_observed(scanned=(SELECTION,)))

    assert result is not None
    assert result.reason == "citation_drifted"
    assert result.detail["expected_source_digest"] == ORIGINAL_SOURCE


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


def test_v1_source_observation_keeps_its_original_whole_source_semantics() -> None:
    assert (
        _repair(
            PlaybillNextSourceObservationV1(
                source_id=SOURCE_ID,
                observed_source_digest=ORIGINAL_SOURCE,
            )
        )
        is None
    )
    result = _repair(
        PlaybillNextSourceObservationV1(
            source_id=SOURCE_ID,
            observed_source_digest=CHANGED_SOURCE,
        )
    )
    assert result is not None
    assert result.reason == "citation_drifted"


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
