"""Deterministic search/list/orient laws for accepted Playbill state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.contracts.knowledge_briefs import KnowledgeBriefValueV1
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.search import PlaybillSearchBudgetsV1, PlaybillSearchRequestV1
from cruxible_core.service.playbill_search import service_search_playbill
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import _seed_claim_surface
from tests.test_playbill.test_knowledge_briefs import _activate, _brief_payload

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


def _accept_brief(
    instance,
    owner,
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
    *,
    purpose: str,
    timestamp: str,
) -> str:  # type: ignore[no-untyped-def]
    intent = coordinator.create(
        actor=actor,
        payload=_brief_payload(
            KnowledgeBriefValueV1(
                purpose=purpose,
                kind="guidance",
                prose=f"Guidance for {purpose}",
            )
        ),
        canonical_timestamp=timestamp,
    ).intent
    _activate(instance, owner, coordinator.submit(intent.intent_id, actor=actor))
    return intent.semantic_identity


def test_search_and_list_are_deterministic_cursor_bound_pages(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    first_id = _accept_brief(
        instance,
        owner,
        coordinator,
        actor,
        purpose="How should a release be prepared?",
        timestamp="2026-08-21T12:00:00.000000Z",
    )
    second_id = _accept_brief(
        instance,
        owner,
        coordinator,
        actor,
        purpose="Who approves the release?",
        timestamp="2026-08-21T12:00:01.000000Z",
    )

    search = service_search_playbill(
        instance,
        request=_request(
            instance,
            mode="search",
            query="PREPARED",
            kinds=("brief",),
        ),
    )
    assert [row.identity for row in search.rows] == [first_id]
    assert [basis.basis for basis in search.rows[0].match_basis] == ["lexical"]
    assert search.rows[0].healthy is True
    assert search.rows[0].brief_health_receipt_digest is not None

    first_request = _request(
        instance,
        mode="list",
        kinds=("brief",),
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
        "brief",
        "brief",
    ]


def test_orient_has_no_arbitrary_rows_and_names_demand_as_not_installed(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)

    result = service_search_playbill(instance, request=_request(instance, mode="orient"))

    assert result.rows == ()
    assert result.orientation is not None
    availability = {item.kind: item.availability for item in result.orientation.kind_availability}
    assert availability == {
        "brief": "installed",
        "claim": "installed",
        "demand": "not_installed",
        "procedure": "installed",
    }
    assert dict((item.key, item.count) for item in result.orientation.counts_by_kind)["demand"] == 0
    assert result.orientation.follow_ups[0].mode == "list"
    assert result.orientation.follow_ups[1].mode == "search"
