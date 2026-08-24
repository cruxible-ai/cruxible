"""PC-F2 coverage resolution: match state, orthogonal health, and the cards.

This file is the §11.6 law suite. Every test is one sentence of the ratified
amendment made executable: copied text gets no exact coverage, relocated cited
content stays discoverable, indistinguishable duplicates refuse to bind, changed
content drifts immediately, unprovable freshness never emits exact, restricted
coverage never becomes a false none, and no card anywhere grants authority.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.coverage.contracts import (
    COVERAGE_HEALTH_ABSENCE_IS_FACTUAL,
    COVERAGE_HEALTH_PROVES_FRESHNESS,
    COVERAGE_HEALTH_RANK,
    COVERAGE_HEALTH_STATES,
    COVERAGE_MATCH_STATES,
    MATCH_STATE_PRECEDENCE,
    CoverageCardV1,
    CoverageSelectionV1,
    CoverageSpanRequestV1,
    CoverageSpanResultV1,
)
from cruxible_core.playbill.coverage.resolver import resolve_coverage
from cruxible_core.playbill.query.semantic_discovery import MATCH_BASIS_RESOLVES_EQUIVALENCE
from tests.test_playbill._coverage_support import (
    CITED,
    EPILOGUE,
    HANDBOOK,
    PREAMBLE,
    SCRATCH,
    capture,
    coordinate,
    index,
    manifest,
    overlay,
    profile,
    request,
    sha256,
    working,
)

GOLDEN = Path(__file__).parents[1] / "goldens" / "playbill" / "coverage-grammar-v1.json"

HANDBOOK_BODY = PREAMBLE + CITED + EPILOGUE
SCRATCH_BODY = b"Notes to self.\n\n" + CITED + b"\nSomething else.\n"


def _resolve(
    *sources,
    citations=None,
    snapshot=None,
    access=None,
    published=True,
    watcher_health="absent",
):
    citations = citations if citations is not None else index(capture(HANDBOOK, CITED))
    snapshot = (
        snapshot
        if snapshot is not None
        else overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    )
    body = manifest(citations, snapshot, watcher_health=watcher_health) if published else None
    return resolve_coverage(
        request(*sources),
        index=citations,
        overlay=snapshot,
        access=access or profile(),
        manifest=body,
    )


# -- the four match states -------------------------------------------------


def test_unchanged_cited_content_relocated_in_its_source_stays_exact() -> None:
    citations = index(capture(HANDBOOK, CITED, with_handle=True))
    moved = overlay(
        working(HANDBOOK, PREAMBLE + EPILOGUE + b"\n\n" + CITED),
        citations=citations,
    )

    result = _resolve(HANDBOOK, citations=citations, snapshot=moved)
    span = result.spans[0]

    assert span.match_state == "exact"
    assert span.health == "complete"
    card = next(item for item in span.cards if item.match_state == "exact")
    assert card.accepted_source == HANDBOOK
    assert card.observed_source == HANDBOOK
    assert card.observed_commitment_digest == card.expected_commitment_digest == sha256(CITED)
    # Line movement alone never breaks exact: the overlay moved, the identity
    # the match was made on did not.
    assert card.line_overlay is not None
    assert card.line_overlay.start_line > 1


def test_identical_text_copied_to_another_source_receives_no_exact_coverage() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(
        working(HANDBOOK, HANDBOOK_BODY),
        working(SCRATCH, SCRATCH_BODY),
        citations=citations,
    )

    result = _resolve(SCRATCH, HANDBOOK, citations=citations, snapshot=snapshot)
    scratch, handbook = result.spans

    assert handbook.match_state == "exact"
    assert scratch.match_state == "candidate"
    card = scratch.cards[0]
    assert card.match_basis == "content_equivalent"
    assert card.resolves_equivalence is False
    assert MATCH_BASIS_RESOLVES_EQUIVALENCE[card.match_basis] is False
    assert card.accepted_source == HANDBOOK
    assert card.observed_source == SCRATCH
    assert "foreign_occurrence" in card.reason_codes
    # Copied bytes inherit no governance: the card points at the accepted
    # citation and states that this occurrence is not it.
    assert not any(item.match_state == "exact" for item in scratch.cards)


def test_ambiguous_duplicate_occurrences_refuse_a_silent_exact_binding() -> None:
    citations = index(capture(HANDBOOK, CITED))
    doubled = overlay(
        working(HANDBOOK, PREAMBLE + CITED + EPILOGUE + CITED),
        citations=citations,
    )

    span = _resolve(HANDBOOK, citations=citations, snapshot=doubled).spans[0]

    assert span.match_state == "candidate"
    assert span.ambiguous_occurrence_count == 2
    assert len(span.cards) == 2
    assert {item.occurrence_identity_digest for item in span.cards} != {None}
    assert len({item.occurrence_identity_digest for item in span.cards}) == 2
    assert all("occurrence_ambiguous" in item.reason_codes for item in span.cards)
    assert all(item.match_state == "candidate" for item in span.cards)


def test_changed_cited_content_drifts_immediately_and_binds_the_whole_tuple() -> None:
    citations = index(capture(HANDBOOK, CITED, with_handle=True))
    edited = PREAMBLE + b"The reviewer rejected the migration plan.\n" + EPILOGUE
    snapshot = overlay(working(HANDBOOK, edited), citations=citations)

    span = _resolve(HANDBOOK, citations=citations, snapshot=snapshot).spans[0]

    assert span.match_state == "drifted"
    card = span.cards[0]
    assert card.at == coordinate()
    assert card.expected_commitment_digest == sha256(CITED)
    assert card.observed_commitment_digest == sha256(edited)
    assert card.accepted_source == card.observed_source == HANDBOOK
    assert card.capture_digests
    assert card.dereference_handle_digest is not None
    assert card.dependent_claim_count == 0
    assert "commitment_superseded" in card.reason_codes
    # Drift is its own state, never exact with a flag and never a lexical
    # candidate.
    assert card.match_basis is None


def test_an_absence_is_factual_only_inside_a_complete_boundary() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(
        working(HANDBOOK, HANDBOOK_BODY),
        working(SCRATCH, b"Nothing governed here.\n"),
        citations=citations,
    )

    span = _resolve(SCRATCH, HANDBOOK, citations=citations, snapshot=snapshot).spans[0]

    assert span.match_state == "none"
    assert span.health == "complete"
    assert span.absence_is_factual is True
    assert span.cards == ()


# -- freshness fails closed ------------------------------------------------


def test_unprovable_freshness_never_emits_exact() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)

    unpublished = _resolve(HANDBOOK, citations=citations, snapshot=snapshot, published=False)
    degraded = _resolve(HANDBOOK, citations=citations, snapshot=snapshot, watcher_health="degraded")

    for result, health in ((unpublished, "unavailable"), (degraded, "stale")):
        span = result.spans[0]
        assert span.health == health
        assert COVERAGE_HEALTH_PROVES_FRESHNESS[span.health] is False
        assert span.match_state == "candidate"
        assert all("freshness_unprovable" in item.reason_codes for item in span.cards)
        assert span.absence_is_factual is False


def test_a_superseded_snapshot_makes_that_source_stale_without_hiding_drift() -> None:
    citations = index(capture(HANDBOOK, CITED, with_handle=True))
    published = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    body = manifest(citations, published)

    edited = overlay(working(HANDBOOK, PREAMBLE + EPILOGUE), citations=citations)
    result = resolve_coverage(
        request(HANDBOOK),
        index=citations,
        overlay=edited,
        access=profile(),
        manifest=body,
    )
    span = result.spans[0]

    assert span.health == "stale"
    assert "manifest_snapshot_superseded" in span.coverage.reason_codes
    # Staleness suppresses exact, never the drift the edit created.
    assert span.match_state == "drifted"


def test_an_unobserved_source_is_unavailable_rather_than_absent() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)

    span = _resolve(SCRATCH, citations=citations, snapshot=snapshot).spans[0]

    assert span.health == "unavailable"
    assert span.match_state == "none"
    assert span.absence_is_factual is False
    assert "source_not_observed" in span.coverage.reason_codes


def test_an_unscanned_selection_lowers_the_boundary_instead_of_claiming_drift() -> None:
    from cruxible_core.playbill.coverage.indexes import (
        CoverageScanBudgetV1,
        build_working_occurrence_overlay,
    )

    citations = index(capture(HANDBOOK, CITED))
    starved = build_working_occurrence_overlay(
        (working(HANDBOOK, PREAMBLE + EPILOGUE),),
        wanted=citations.wanted_selections(),
        budget=CoverageScanBudgetV1(max_scanned_bytes=0),
    )
    body = manifest(citations, starved)
    span = resolve_coverage(
        request(HANDBOOK),
        index=citations,
        overlay=starved,
        access=profile(),
        manifest=body,
    ).spans[0]

    assert span.match_state == "none"
    assert span.health == "partial"
    assert span.absence_is_factual is False
    assert "unscanned_selection" in span.coverage.reason_codes


# -- access, and the false `none` --------------------------------------------


def test_restricted_coverage_never_becomes_a_false_none() -> None:
    citations = index(capture(HANDBOOK, CITED, access_class="restricted"))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)

    span = _resolve(HANDBOOK, citations=citations, snapshot=snapshot).spans[0]

    assert span.match_state == "none"
    assert span.health == "denied"
    assert span.absence_is_factual is False
    assert span.cards == ()
    assert "restricted_access_class" in span.coverage.reason_codes
    assert span.coverage.omitted_for_access == ("coverage",)


def test_non_disclosure_reports_an_incomplete_boundary_without_naming_it() -> None:
    citations = index(capture(HANDBOOK, CITED, access_class="restricted"))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)

    span = _resolve(
        HANDBOOK,
        citations=citations,
        snapshot=snapshot,
        access=profile(disclose=False),
    ).spans[0]

    assert span.health == "partial"
    assert span.absence_is_factual is False
    assert "boundary_incomplete" in span.coverage.reason_codes
    # Nothing in the answer reveals that restricted coverage exists at all.
    assert "restricted_access_class" not in span.coverage.reason_codes
    assert span.coverage.omitted_for_access == ()


def test_permitting_the_restricted_class_returns_the_coverage_it_was_hiding() -> None:
    citations = index(capture(HANDBOOK, CITED, access_class="restricted"))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)

    span = _resolve(
        HANDBOOK,
        citations=citations,
        snapshot=snapshot,
        access=profile(permitted=("instance", "public", "restricted")),
    ).spans[0]

    assert span.match_state == "exact"
    assert span.health == "complete"


# -- the laws that hold regardless of any input ----------------------------


def test_no_coverage_card_grants_mutation_authority_or_equivalence() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(
        working(HANDBOOK, HANDBOOK_BODY),
        working(SCRATCH, SCRATCH_BODY),
        citations=citations,
    )
    result = _resolve(SCRATCH, HANDBOOK, citations=citations, snapshot=snapshot)

    cards = tuple(card for span in result.spans for card in span.cards)
    assert cards
    for card in cards:
        assert card.grants_mutation_authority is False
        assert card.resolves_equivalence is False
        if card.match_basis is not None:
            assert MATCH_BASIS_RESOLVES_EQUIVALENCE[card.match_basis] is False

    with pytest.raises(ValidationError, match="equivalence-resolving match basis"):
        cards[0].model_copy(update={"match_basis": "exact_alias"}).model_validate(
            cards[0].model_dump() | {"match_basis": "exact_alias", "match_state": "candidate"}
        )
    with pytest.raises(ValidationError):
        CoverageCardV1(
            **(
                cards[0].model_dump()
                | {"grants_mutation_authority": True, "match_state": "candidate"}
            )
        )


def test_a_freshness_free_exact_and_a_false_none_are_unrepresentable() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    span = _resolve(HANDBOOK, citations=citations, snapshot=snapshot).spans[0]

    with pytest.raises(ValidationError, match="proves freshness"):
        span.model_copy().model_validate(span.model_dump() | {"health": "stale"})
    with pytest.raises(ValidationError, match="factual exactly when"):
        CoverageSpanResultV1(
            request=CoverageSpanRequestV1(source=HANDBOOK),
            match_state="none",
            health="denied",
            absence_is_factual=True,
            coverage=span.coverage,
        )
    with pytest.raises(ValidationError, match="never silently bound"):
        span.model_validate(span.model_dump() | {"ambiguous_occurrence_count": 2})


def test_the_same_inputs_resolve_byte_identically() -> None:
    citations = index(capture(HANDBOOK, CITED, with_handle=True))
    snapshot = overlay(
        working(HANDBOOK, HANDBOOK_BODY),
        working(SCRATCH, SCRATCH_BODY),
        citations=citations,
    )

    first = _resolve(HANDBOOK, SCRATCH, citations=citations, snapshot=snapshot)
    second = _resolve(HANDBOOK, SCRATCH, citations=citations, snapshot=snapshot)

    assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
        second.model_dump(mode="json")
    )
    assert first.summary.exact == 1
    assert first.summary.candidate == 1
    assert first.health == "complete"


def test_a_requested_window_selects_occurrences_without_filtering_drift() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    body = manifest(citations, snapshot)
    windowed = request(HANDBOOK).model_copy(
        update={
            "spans": (
                CoverageSpanRequestV1(
                    source=HANDBOOK,
                    selection=CoverageSelectionV1(start_byte=0, end_byte=4),
                ),
            )
        }
    )

    span = resolve_coverage(
        windowed, index=citations, overlay=snapshot, access=profile(), manifest=body
    ).spans[0]

    # The window covers the file head, which the whole-source occurrence spans
    # but the cited paragraph does not.
    assert span.match_state == "none"
    assert span.health == "complete"


# -- the frozen grammar ----------------------------------------------------


def test_the_frozen_coverage_grammar_matches_its_golden() -> None:
    citations = index(
        capture(HANDBOOK, CITED, with_handle=True),
        capture(SCRATCH, PREAMBLE, name="scratch"),
    )
    snapshot = overlay(
        working(HANDBOOK, HANDBOOK_BODY),
        working(SCRATCH, SCRATCH_BODY),
        citations=citations,
    )
    body = manifest(citations, snapshot)
    result = resolve_coverage(
        request(HANDBOOK, SCRATCH),
        index=citations,
        overlay=snapshot,
        access=profile(),
        manifest=body,
    )
    fixture = json.loads(GOLDEN.read_bytes())

    assert fixture["match_states"] == list(COVERAGE_MATCH_STATES)
    assert fixture["health_states"] == list(COVERAGE_HEALTH_STATES)
    assert fixture["match_state_precedence"] == dict(MATCH_STATE_PRECEDENCE)
    assert fixture["health_rank"] == dict(COVERAGE_HEALTH_RANK)
    assert fixture["health_proves_freshness"] == dict(COVERAGE_HEALTH_PROVES_FRESHNESS)
    assert fixture["health_absence_is_factual"] == dict(COVERAGE_HEALTH_ABSENCE_IS_FACTUAL)
    assert fixture["evidence_index"] == citations.model_dump(mode="json")
    assert fixture["working_overlay"] == snapshot.model_dump(mode="json")
    assert fixture["manifest_body"] == body.model_dump(mode="json")
    assert fixture["coverage_result"] == result.model_dump(mode="json")
