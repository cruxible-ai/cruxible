"""PC-G-S1a governed QueryDefinition service reads and proposals."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.errors import ClaimNotFoundError
from cruxible_client.contracts.query.definitions import (
    query_definition_digest,
    query_definition_path,
)
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import (
    service_get_playbill_query_definition,
    service_list_playbill_query_definitions,
    service_propose_playbill_query_definition,
)
from tests.test_playbill._knowledge_loop_support import (
    PREDICATE,
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)


def _accept_query(tmp_path: Path):
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


def test_accepted_query_definition_reads_back_at_its_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = _accept_query(tmp_path)

    view = service_get_playbill_query_definition(instance, name=QUERY_NAME)

    assert view.name == QUERY_NAME
    assert view.identity == f"QueryDefinition:{QUERY_NAME}"
    assert view.path == query_definition_path(QUERY_NAME)
    assert view.artifact_digest == query_definition_digest(work_item_query()).tagged
    assert view.coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert view.envelope["artifact_format"] == "playbill-query-definition-v1"


def test_query_definition_listing_is_the_byte_sorted_accepted_inventory(tmp_path: Path) -> None:
    instance, _owner = _accept_query(tmp_path)

    listing = service_list_playbill_query_definitions(instance)

    paths = tuple(item.path for item in listing.query_definitions)
    assert paths == (query_definition_path(QUERY_NAME),)
    assert listing.coordinate.git_oid == instance.accepted_coordinate().git_oid


def test_absent_query_definition_is_refused_rather_than_returned_empty(tmp_path: Path) -> None:
    instance, _owner = _accept_query(tmp_path)

    with pytest.raises(ClaimNotFoundError):
        service_get_playbill_query_definition(instance, name="project.absent_query")


def test_claim_type_pin_that_does_not_resolve_at_the_base_is_refused(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    stale = work_item_query().model_copy(
        update={
            "pins": (
                ArtifactPin(
                    role="claim-type",
                    target=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
                    artifact_digest="sha256:" + "11" * 32,
                ),
            )
        }
    )

    refused = service_propose_playbill_query_definition(
        instance,
        query=stale,
        actor_id="owner",
        proposal_name="query-stale-pin",
        timestamp=TIMESTAMP,
    )

    assert refused.proposal.candidate is None
    assert [item.code for item in refused.proposal.evaluation.diagnostics] == [
        "playbill.change_set.unresolved_pin"
    ]


def test_query_definition_read_is_pinned_to_the_requested_accepted_coordinate(
    tmp_path: Path,
) -> None:
    instance, _owner = _accept_query(tmp_path)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    pinned = service_get_playbill_query_definition(instance, name=QUERY_NAME, at=accepted)

    assert pinned.coordinate == accepted
