"""PC-G-S1a accepted-state semantic discovery over the governed naming layer."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.claim_types import claim_type_path
from cruxible_core.playbill.discovery import DiscoveryBudgetV1
from cruxible_core.playbill.query.semantic_discovery import DiscoveryError
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import (
    service_propose_playbill_query_definition,
)
from cruxible_core.service.playbill_discovery import (
    build_accepted_discovery_vocabulary,
    service_discover_playbill_semantic,
)
from tests.test_playbill._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)


def _instance_with_query(tmp_path: Path):
    instance, owner = seed_claims(tmp_path)
    inspection = service_propose_playbill_query_definition(
        instance,
        query=work_item_query(),
        actor_id="owner",
        proposal_name="work-item-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection, sequence=3)
    return instance, owner


def test_vocabulary_covers_accepted_subjects_claim_types_and_queries(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    vocabulary = build_accepted_discovery_vocabulary(
        instance,
        coordinate=instance.accepted_coordinate(),
    )

    kinds = {entry.kind for entry in vocabulary.entries}
    assert {"Subject", "ClaimType", "QueryDefinition"} <= kinds
    assert vocabulary.at.git_oid == instance.accepted_coordinate().git_oid
    assert tuple(item.entrypoint_name for item in vocabulary.entrypoints()) == (QUERY_NAME,)


def test_lexical_query_finds_the_claim_type_that_names_the_term(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    result = service_discover_playbill_semantic(
        instance,
        query="status",
        evaluation_time=EVALUATION_TIME,
        profile="interfaces",
    )

    addresses = {hit.address.artifact_path for hit in result.page.hits}
    assert claim_type_path(PREDICATE) in addresses
    assert result.coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert result.page.coordinate_kind == "accepted"
    assert result.page.receipt_digest.startswith("sha256:")


def test_entrypoint_selection_resolves_the_accepted_query_definition(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    result = service_discover_playbill_semantic(
        instance,
        entrypoint=QUERY_NAME,
        evaluation_time=EVALUATION_TIME,
        profile="all",
    )

    assert [(hit.kind, hit.label) for hit in result.page.hits] == [("QueryDefinition", QUERY_NAME)]


def test_discovery_page_is_byte_stable_for_one_coordinate_and_request(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    first = service_discover_playbill_semantic(
        instance, query="work_item", evaluation_time=EVALUATION_TIME, profile="all"
    )
    second = service_discover_playbill_semantic(
        instance, query="work_item", evaluation_time=EVALUATION_TIME, profile="all"
    )

    assert first.page == second.page


def test_budget_clipping_is_stated_rather_than_silently_narrowing(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    result = service_discover_playbill_semantic(
        instance,
        query="work_item",
        evaluation_time=EVALUATION_TIME,
        profile="all",
        budget=DiscoveryBudgetV1(max_hits=1),
    )

    assert len(result.page.hits) == 1
    assert result.page.coverage.truncated_facets == ("hits",)
    assert "hit_budget_exceeded" in result.page.coverage.reason_codes


def test_request_naming_neither_a_query_nor_an_entrypoint_is_refused(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    with pytest.raises(ValidationError, match="exactly one query or entrypoint"):
        service_discover_playbill_semantic(instance, evaluation_time=EVALUATION_TIME)


def test_blank_query_is_refused_rather_than_listing_everything(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    with pytest.raises(DiscoveryError, match="blank discovery query"):
        service_discover_playbill_semantic(instance, query="   ", evaluation_time=EVALUATION_TIME)


def test_discovery_is_pinned_to_the_requested_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    result = service_discover_playbill_semantic(
        instance,
        query="status",
        evaluation_time=EVALUATION_TIME,
        at=accepted,
    )

    assert result.coordinate == accepted
    assert result.page.at == accepted
