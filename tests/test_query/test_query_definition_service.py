"""PC-G-S1a governed QueryDefinition service reads and proposals."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ClaimNotFoundError
from cruxible_client.contracts.query.definitions import (
    query_definition_digest,
    query_definition_path,
)
from cruxible_core.indexes.typed_state import TypedStateReader
from cruxible_core.service.authoring.documents import AcceptedCoordinate
from cruxible_core.service.discovery.query_definitions import (
    service_get_playbill_query_definition,
)
from tests.core_support._candidate_support import submit_query_definition_candidate
from tests.core_support._knowledge_loop_support import (
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)


def _accept_query(tmp_path: Path):
    instance, owner = seed_claims(tmp_path)
    inspection = submit_query_definition_candidate(
        instance,
        query=work_item_query(),
        actor_id="owner",
        proposal_name="work-item-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)
    return instance, owner


def test_accepted_query_definition_reads_back_at_its_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = _accept_query(tmp_path)

    view = service_get_playbill_query_definition(instance, name=QUERY_NAME)

    assert view.name == QUERY_NAME
    assert view.identity == f"QueryDefinition:{QUERY_NAME}"
    assert view.path == query_definition_path(QUERY_NAME)
    assert view.artifact_digest == query_definition_digest(work_item_query()).tagged
    assert view.coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert view.envelope["artifact_format"] == "cruxible-query-definition-v3"


def test_absent_query_definition_is_refused_rather_than_returned_empty(tmp_path: Path) -> None:
    instance, _owner = _accept_query(tmp_path)

    with pytest.raises(ClaimNotFoundError):
        service_get_playbill_query_definition(instance, name="project.absent_query")


def test_query_definition_read_is_pinned_to_the_requested_accepted_coordinate(
    tmp_path: Path,
) -> None:
    instance, _owner = _accept_query(tmp_path)
    accepted = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    pinned = service_get_playbill_query_definition(instance, name=QUERY_NAME, at=accepted)

    assert pinned.coordinate == accepted


def test_query_definition_reads_only_selected_accepted_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _accept_query(tmp_path)
    # Bind once before measuring the service read, which must not walk the tree.
    with instance.bind_accepted_projection(instance.accepted_coordinate()):
        pass
    reads: list[str] = []
    member_bytes = TypedStateReader.member_bytes

    def counted(reader: TypedStateReader, path: str) -> bytes:
        reads.append(path)
        return member_bytes(reader, path)

    def no_tree(*_args: object, **_kwargs: object) -> None:
        pytest.fail("QueryDefinition reads must use the accepted typed directory")

    monkeypatch.setattr(TypedStateReader, "member_bytes", counted)
    monkeypatch.setattr(instance, "tree_at", no_tree)
    monkeypatch.setattr(instance, "immutable_tree_at", no_tree)
    service_get_playbill_query_definition(instance, name=QUERY_NAME)
    assert reads == [query_definition_path(QUERY_NAME)]
    reads.clear()
    with pytest.raises(ClaimNotFoundError):
        service_get_playbill_query_definition(instance, name="project.absent_query")
    assert reads == []


def test_current_population_query_helpers_use_pinless_owner_models():
    from cruxible_client.contracts.query.definitions import NamedQueryDefinition
    from tests.core_support._adoption_fixture import _claim_type, _query_definition
    from tests.test_scale._worlds import _condition

    for query in (work_item_query(), _query_definition(0, _claim_type(0)), _condition()):
        assert isinstance(query, NamedQueryDefinition)
        assert "pins" not in query.model_dump(mode="json")
