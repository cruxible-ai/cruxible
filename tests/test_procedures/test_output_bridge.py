"""Procedure-only governed output bridge compilation and landing."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.procedure.proposal import parse_candidate_edge_rows
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import (
    service_accept_procedure,
    service_propose_procedure,
    service_run_procedure,
)
from tests.test_procedures.conftest import actor, bridge_definition


def _accept(instance: CruxibleInstance, definition: ProcedureDefinition) -> str:
    proposed = service_propose_procedure(instance, definition, actor_context=actor("proposer"))
    accepted = service_accept_procedure(
        instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer"),
    )
    return accepted.procedure.procedure_id


def _seed_entities(instance: CruxibleInstance) -> None:
    entities = [
        EntityInstance(entity_type="Task", entity_id="T-1", properties={"status": "open"}),
        EntityInstance(entity_type="Incident", entity_id="I-1", properties={"status": "open"}),
    ]
    graph = instance.load_graph()
    for entity in entities:
        graph.add_entity(entity)
    instance.save_graph_delta(graph, entities=entities)


def _pending_groups(instance: CruxibleInstance) -> list[Any]:
    store = instance.get_group_store()
    try:
        return [group for group in store.list_groups() if group.status == "pending_review"]
    finally:
        store.close()


def test_bridge_rows_are_strict() -> None:
    with pytest.raises(ValueError, match="row 0 is not a valid candidate edge"):
        parse_candidate_edge_rows(
            [
                {
                    "from_type": "Task",
                    "from_id": "T-1",
                    "to_type": "Incident",
                    "to_id": "I-1",
                    "propertys": {},
                }
            ]
        )


def test_bridge_refuses_an_unknown_relationship(
    procedure_instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError, match="unknown relationship type 'missing'"):
        service_propose_procedure(
            procedure_instance,
            bridge_definition(relationship_type="missing"),
            actor_context=actor("proposer"),
        )


def test_bridge_lands_pending_group_only_after_success(
    procedure_instance: CruxibleInstance,
) -> None:
    _seed_entities(procedure_instance)
    procedure_id = _accept(procedure_instance, bridge_definition())

    result = service_run_procedure(procedure_instance, procedure_id, {}, actor("runner"))

    assert result.run.verdict == "succeeded"
    assert result.output["group_status"] == "pending_review"
    assert result.output["group_id"] is not None
    pending = _pending_groups(procedure_instance)
    assert [group.group_id for group in pending] == [result.output["group_id"]]
    assert pending[0].source_workflow_receipt_id == result.receipt.receipt_id
    bridge_nodes = [
        node for node in result.receipt.nodes if node.detail.get("kind") == "propose_group_from"
    ]
    assert len(bridge_nodes) == 1
    assert bridge_nodes[0].detail["group_id"] == result.output["group_id"]
    assert list(procedure_instance.load_graph().iter_relationships("blocks")) == []


def test_distinct_declared_scopes_do_not_replace_each_other(
    procedure_instance: CruxibleInstance,
) -> None:
    _seed_entities(procedure_instance)
    first_id = _accept(
        procedure_instance,
        bridge_definition("scope_one", proposal_scope={"case": "one"}),
    )
    second_id = _accept(
        procedure_instance,
        bridge_definition("scope_two", proposal_scope={"case": "two"}),
    )

    first = service_run_procedure(procedure_instance, first_id, {}, actor("runner-one"))
    second = service_run_procedure(procedure_instance, second_id, {}, actor("runner-two"))

    assert first.output["group_id"] != second.output["group_id"]
    assert len(_pending_groups(procedure_instance)) == 2


def test_pending_procedure_dry_run_previews_without_landing_group(
    procedure_instance: CruxibleInstance,
) -> None:
    _seed_entities(procedure_instance)
    proposed = service_propose_procedure(
        procedure_instance,
        bridge_definition("pending_preview"),
        actor_context=actor("author"),
    )

    result = service_run_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        {},
        actor("reviewer"),
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.run.verdict == "succeeded"
    assert result.procedure.status == "pending"
    assert result.output["group_status"] == "would_propose"
    assert result.output["group_id"] is None
    assert result.output["would_change"]["sections"]["edges"]["counts"]["added"] == 1
    assert _pending_groups(procedure_instance) == []
