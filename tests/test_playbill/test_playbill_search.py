"""Deterministic search/list/orient laws for accepted Playbill state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.claims import claim_statement_digest
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.search import PlaybillSearchBudgetsV1, PlaybillSearchRequestV1
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_next import PlaybillNextRequestV1, service_playbill_next
from cruxible_core.service.playbill_search import service_search_playbill
from tests.test_playbill._claim_authoring_support import (
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
)
from tests.test_playbill._knowledge_loop_support import (
    activate as activate_work_item_claim,
)
from tests.test_playbill._knowledge_loop_support import (
    authoring as work_item_authoring,
)
from tests.test_playbill._knowledge_loop_support import (
    seed_claims,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import _seed_claim_surface

EVALUATION_TIME = datetime(2026, 8, 21, 14, tzinfo=UTC)
ACCESS = CoverageAccessProfileV1(profile_id="search-test")


def _request(instance, *, mode: str, **values: object) -> PlaybillSearchRequestV1:  # type: ignore[no-untyped-def]
    return PlaybillSearchRequestV1(
        mode=mode,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=ACCESS,
        **values,
    )


def test_search_and_list_are_deterministic_cursor_bound_pages(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    seeded = tuple(_claim_from_view(view) for view in service_list_playbill_claims(instance).claims)
    first_id = next(
        claim.identity.name
        for claim in seeded
        if claim.statement.subject.artifact_path.endswith("/wi-42.yaml")
    )
    second_id = next(claim.identity.name for claim in seeded if claim.identity.name != first_id)

    search = service_search_playbill(
        instance,
        request=_request(
            instance,
            mode="search",
            query="WI-42",
            kinds=("claim",),
        ),
    )
    assert [row.identity for row in search.rows] == [first_id]
    assert [basis.basis for basis in search.rows[0].match_basis] == ["lexical"]
    assert "healthy" not in search.rows[0].model_dump(mode="json")
    assert "brief_health_receipt_digest" not in search.rows[0].model_dump(mode="json")

    first_request = _request(
        instance,
        mode="list",
        kinds=("claim",),
        budgets=PlaybillSearchBudgetsV1(max_rows=1),
    )
    first = service_search_playbill(instance, request=first_request)
    assert service_search_playbill(instance, request=first_request) == first
    assert len(first.rows) == 1
    assert first.truncated is True
    assert first.next_cursor is not None

    second = service_search_playbill(
        instance,
        request=first_request.model_copy(update={"cursor": first.next_cursor}),
    )
    assert len(second.rows) == 1
    assert second.next_cursor is None
    assert {first.rows[0].identity, second.rows[0].identity} == {first_id, second_id}
    all_kinds = service_search_playbill(
        instance,
        request=_request(instance, mode="list"),
    )
    assert [row.kind for row in all_kinds.rows if row.identity in {first_id, second_id}] == [
        "claim",
        "claim",
    ]


def test_orient_has_no_arbitrary_rows_and_names_demand_as_not_installed(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)

    result = service_search_playbill(instance, request=_request(instance, mode="orient"))

    assert result.rows == ()
    assert result.orientation is not None
    assert "unhealthy_brief_count" not in result.orientation.model_dump(mode="json")
    assert "approval_authorities" not in result.orientation.model_dump(mode="json")
    availability = {item.kind: item.availability for item in result.orientation.kind_availability}
    assert availability == {
        "claim": "installed",
        "demand": "not_installed",
        "procedure": "installed",
    }
    assert dict((item.key, item.count) for item in result.orientation.counts_by_kind)["demand"] == 0
    assert result.orientation.follow_ups[0].mode == "list"
    assert result.orientation.follow_ups[1].mode == "search"


@pytest.mark.parametrize(
    ("second_value", "qualifier", "expected_conflicted_count"),
    [
        ("ready", None, 0),
        ("blocked", "alternative", 0),
        ("blocked", None, 2),
    ],
)
def test_orient_list_and_next_share_the_same_structural_claim_slot_classifier(
    tmp_path: Path,
    second_value: str,
    qualifier: str | None,
    expected_conflicted_count: int,
) -> None:
    instance, owner = seed_claims(tmp_path)
    current = next(
        claim
        for claim in (
            _claim_from_view(view) for view in service_list_playbill_claims(instance).claims
        )
        if claim.statement.subject.artifact_path.endswith("/wi-42.yaml")
    )
    authoring = work_item_authoring("wi-42", second_value, with_claim_type=False)
    second = service_propose_playbill_claim(
        instance,
        authoring=authoring.model_copy(
            update={
                "statement": authoring.statement.model_copy(update={"qualifier": qualifier}),
                "existing_statement_handoffs": (
                    ExistingStatementHandoffV1(
                        statement_digest=claim_statement_digest(current.statement).tagged,
                        disposition="support" if second_value == "ready" else "contradict",
                    ),
                ),
            }
        ),
        actor_id="owner",
        proposal_name="same-work-item-slot",
        timestamp="2026-08-21T12:00:03.000000Z",
    )
    activate_work_item_claim(instance, owner, second)

    listed = service_search_playbill(
        instance,
        request=_request(instance, mode="list", kinds=("claim",)),
    )
    rows = tuple(
        row
        for row in listed.rows
        if row.subject is not None and row.subject == current.statement.subject
    )
    assert len(rows) == 2
    assert {row.status for row in rows} == (
        {"conflicted"} if expected_conflicted_count else {"accepted"}
    )
    orientation = service_search_playbill(
        instance,
        request=_request(instance, mode="orient", kinds=("claim",)),
    ).orientation
    assert orientation is not None
    assert orientation.conflicted_count == expected_conflicted_count
    next_result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(evaluation_time=EVALUATION_TIME, access_profile=ACCESS),
    )
    conflicts = tuple(item for item in next_result.items if item.reason == "claim_conflicted")
    assert len(conflicts) == int(bool(expected_conflicted_count))
