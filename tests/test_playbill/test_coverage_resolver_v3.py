"""The surviving coverage-v3 resolver retains the frozen coverage laws."""

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
    CoverageCardV2,
    CoverageSelectionV1,
    CoverageSpanRequestV1,
    CoverageSpanResultV3,
)
from cruxible_core.playbill.coverage.resolver import resolve_coverage_v3
from tests.test_playbill._coverage_support import (
    CITED,
    EPILOGUE,
    HANDBOOK,
    PREAMBLE,
    capture,
    index_v2,
    manifest_v2,
    overlay,
    profile,
    request,
    working,
)

GOLDEN = Path(__file__).parents[1] / "goldens" / "playbill" / "coverage-grammar-v3.json"
HANDBOOK_BODY = PREAMBLE + CITED + EPILOGUE


def _resolve(
    *,
    access=None,
    published: bool = True,
    watcher_health: str = "absent",
):
    citations = index_v2(capture(HANDBOOK, CITED))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    body = manifest_v2(citations, snapshot, watcher_health=watcher_health) if published else None
    return resolve_coverage_v3(
        request(HANDBOOK),
        index=citations,
        overlay=snapshot,
        access=access or profile(),
        manifest=body,
    )


def test_unprovable_freshness_never_emits_exact() -> None:
    unpublished = _resolve(published=False)
    degraded = _resolve(watcher_health="degraded")

    for result, health in ((unpublished, "unavailable"), (degraded, "stale")):
        span = result.spans[0]
        assert span.health == health
        assert COVERAGE_HEALTH_PROVES_FRESHNESS[span.health] is False
        assert span.match_state == "candidate"
        assert all("freshness_unprovable" in card.reason_codes for card in span.cards)
        assert span.absence_is_factual is False


def test_restricted_coverage_is_gated_without_a_false_none_or_disclosure() -> None:
    citations = index_v2(capture(HANDBOOK, CITED, access_class="restricted"))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    body = manifest_v2(citations, snapshot)

    disclosed = resolve_coverage_v3(
        request(HANDBOOK),
        index=citations,
        overlay=snapshot,
        access=profile(),
        manifest=body,
    ).spans[0]
    hidden = resolve_coverage_v3(
        request(HANDBOOK),
        index=citations,
        overlay=snapshot,
        access=profile(disclose=False),
        manifest=body,
    ).spans[0]
    permitted = resolve_coverage_v3(
        request(HANDBOOK),
        index=citations,
        overlay=snapshot,
        access=profile(permitted=("instance", "public", "restricted")),
        manifest=body,
    ).spans[0]

    assert (disclosed.match_state, disclosed.health, disclosed.absence_is_factual) == (
        "none",
        "denied",
        False,
    )
    assert "restricted_access_class" in disclosed.coverage.reason_codes
    assert disclosed.coverage.omitted_for_access == ("coverage",)
    assert (hidden.match_state, hidden.health, hidden.absence_is_factual) == (
        "none",
        "partial",
        False,
    )
    assert "boundary_incomplete" in hidden.coverage.reason_codes
    assert "restricted_access_class" not in hidden.coverage.reason_codes
    assert hidden.coverage.omitted_for_access == ()
    assert (permitted.match_state, permitted.health) == ("exact", "complete")


def test_selection_windowing_excludes_an_out_of_window_citation() -> None:
    citations = index_v2(capture(HANDBOOK, CITED))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    body = manifest_v2(citations, snapshot)
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

    span = resolve_coverage_v3(
        windowed,
        index=citations,
        overlay=snapshot,
        access=profile(),
        manifest=body,
    ).spans[0]

    assert span.match_state == "none"
    assert span.health == "complete"
    assert span.absence_is_factual is True


def test_card_and_span_laws_refuse_authority_equivalence_and_false_freshness() -> None:
    span = _resolve().spans[0]
    card = span.cards[0]

    with pytest.raises(ValidationError, match="equivalence-resolving match basis"):
        CoverageCardV2.model_validate(
            card.model_dump(mode="json")
            | {"match_basis": "exact_alias", "match_state": "candidate"}
        )
    with pytest.raises(ValidationError):
        CoverageCardV2.model_validate(
            card.model_dump(mode="json")
            | {"grants_mutation_authority": True, "match_state": "candidate"}
        )
    with pytest.raises(ValidationError, match="proves freshness"):
        CoverageSpanResultV3.model_validate(span.model_dump(mode="json") | {"health": "stale"})
    with pytest.raises(ValidationError, match="factual exactly when"):
        CoverageSpanResultV3(
            request=CoverageSpanRequestV1(source=HANDBOOK),
            match_state="none",
            health="denied",
            absence_is_factual=True,
            coverage=span.coverage,
        )


def test_same_v3_inputs_resolve_byte_identically() -> None:
    first = _resolve()
    second = _resolve()

    assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
        second.model_dump(mode="json")
    )


def test_the_frozen_coverage_v3_grammar_matches_its_golden() -> None:
    fixture = json.loads(GOLDEN.read_bytes())

    assert fixture == {
        "health_absence_is_factual": dict(COVERAGE_HEALTH_ABSENCE_IS_FACTUAL),
        "health_proves_freshness": dict(COVERAGE_HEALTH_PROVES_FRESHNESS),
        "health_rank": dict(COVERAGE_HEALTH_RANK),
        "health_states": list(COVERAGE_HEALTH_STATES),
        "match_state_precedence": dict(MATCH_STATE_PRECEDENCE),
        "match_states": list(COVERAGE_MATCH_STATES),
    }
