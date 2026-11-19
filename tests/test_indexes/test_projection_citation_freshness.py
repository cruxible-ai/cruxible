"""Citation freshness follows verified selections, never unrelated source bytes."""

from __future__ import annotations

from cruxible_core.coverage.contracts import (
    CitationWindowObservation,
    CoverageCommitmentScanProof,
    CoverageLineOverlay,
    LogicalSourceIdentity,
    occurrence_identity_digest,
)
from cruxible_core.coverage.indexes import WorkingOccurrence
from cruxible_core.service.authoring.documents import AcceptedCoordinate
from cruxible_core.service.discovery.next import (
    NextSourceObservation,
    _CitationCommitment,
    _source_citation_item,
)
from tests.core_support._coverage_support import coordinate

CITATION = "sha256:" + "1" * 64
SELECTION = "sha256:" + "2" * 64
ORIGINAL_SOURCE = "sha256:" + "3" * 64
CHANGED_SOURCE = "sha256:" + "4" * 64
SOURCE_ID = "corpus.runbook"


def _commitment(*, length: int = 10, start: int = 5) -> _CitationCommitment:
    return _CitationCommitment(
        citation_id=CITATION,
        commitment_digest=SELECTION,
        byte_length=length,
        claim_identity="Claim:CLM-example",
        source_id=SOURCE_ID,
        source_digest=ORIGINAL_SOURCE,
        original_start=start,
        original_end=start + length,
    )


def _occurrence(start: int = 5, *, ordinal: int = 0, length: int = 10) -> WorkingOccurrence:
    source = LogicalSourceIdentity(plane="external", identity=SOURCE_ID)
    return WorkingOccurrence(
        source=source,
        observed_commitment_digest=SELECTION,
        byte_length=length,
        ordinal=ordinal,
        identity_digest=occurrence_identity_digest(
            source=source,
            observed_commitment_digest=SELECTION,
            ordinal=ordinal,
        ),
        line_overlay=CoverageLineOverlay(
            start_byte=start,
            end_byte=start + length,
            start_line=1,
            end_line=1,
        ),
    )


def _observed(
    *,
    occurrences: tuple[WorkingOccurrence, ...] = (),
    proof: bool = True,
    addressable: bool = True,
    observed_window_digest: str | None = CHANGED_SOURCE,
    length: int = 10,
    start: int = 5,
    byte_length: int = 100,
) -> NextSourceObservation:
    source = LogicalSourceIdentity(plane="external", identity=SOURCE_ID)
    return NextSourceObservation(
        source_id=SOURCE_ID,
        observed_source_digest=CHANGED_SOURCE,
        byte_length=byte_length,
        marker_summaries=(),
        occurrences=occurrences,
        commitment_scan_proofs=(
            CoverageCommitmentScanProof(
                source=source,
                commitment_digest=SELECTION,
                byte_length=length,
            ),
        )
        if proof
        else (),
        citation_window_observations=(
            CitationWindowObservation(
                source=source,
                citation_id=CITATION,
                commitment_digest=SELECTION,
                original_start=start,
                original_end=start + length,
                addressable=addressable,
                observed_window_digest=observed_window_digest if addressable else None,
            ),
        ),
        scan_notes=(),
        marker_notes=(),
    )


def _repair(
    observed: NextSourceObservation | None,
    commitment: _CitationCommitment | None = None,
):  # type: ignore[no-untyped-def]
    return _source_citation_item(
        citation_id=CITATION,
        commitment=commitment or _commitment(),
        observed=observed,
        coordinate=AcceptedCoordinate.model_validate(coordinate().model_dump()),
    )


def test_unchanged_uniquely_rediscovered_span_survives_unrelated_source_edits() -> None:
    assert _repair(_observed(occurrences=(_occurrence(),))) is None
    # The source digest changed and the span moved: the quoted bytes still occur once.
    assert _repair(_observed(occurrences=(_occurrence(80),))) is None


def test_unproved_or_unobserved_selection_is_unobserved_never_drift() -> None:
    for observation in (_observed(proof=False), None):
        result = _repair(observation)
        assert result is not None
        assert result.reason == "citation_source_unobserved"


def test_a_citation_spanning_its_whole_source_is_its_quoted_bytes() -> None:
    """A citation commits to the bytes it quotes, however much of the file they are.

    Spanning the whole captured file gives it no stricter freshness: appending
    to the file keeps those bytes in place, so it stays current; changing them
    drifts it. A Claim that rests on the file saying nothing else rests on a
    Document (``document_modified``), not on a citation.
    """

    whole = _commitment(length=100, start=0)

    # The captured file was 100 bytes; 40 more were appended after it.
    appended = _observed(
        occurrences=(_occurrence(0, length=100),), length=100, start=0, byte_length=140
    )
    assert _repair(appended, whole) is None

    edited = _repair(_observed(length=100, start=0), whole)
    assert edited is not None
    assert edited.reason == "citation_drifted"
    assert edited.detail["drift_state"] == "changed"
    assert edited.detail["observed_window_digest"] == CHANGED_SOURCE


def test_zero_occurrence_distinguishes_changed_gone_and_unobserved() -> None:
    changed = _repair(_observed())
    gone = _repair(_observed(addressable=False, observed_window_digest=None))
    unobserved = _repair(_observed(proof=False))

    assert changed is not None and changed.detail["drift_state"] == "changed"
    assert changed.repair.required_change == "rebind_or_retire_drifted_claim"
    assert gone is not None and gone.detail["drift_state"] == "gone"
    assert unobserved is not None and unobserved.reason == "citation_source_unobserved"


def test_multiple_exact_occurrences_are_ambiguous_not_unobserved() -> None:
    result = _repair(
        _observed(
            occurrences=(
                _occurrence(5, ordinal=0),
                _occurrence(50, ordinal=1),
            )
        )
    )

    assert result is not None
    assert result.detail["drift_state"] == "ambiguous"
    assert result.detail["exact_occurrence_count"] == 2
