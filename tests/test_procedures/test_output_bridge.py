"""Procedure-only governed output bridge compilation and landing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.procedure.proposal import (
    build_procedure_proposal_facts,
    parse_candidate_edge_rows,
)
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import (
    service_accept_procedure,
    service_lock,
    service_propose_group,
    service_propose_procedure,
    service_resolve_group,
    service_run_procedure,
    service_update_trust_status,
)
from cruxible_core.service.groups import plan_group_proposal
from tests.test_procedures.conftest import CONFIG_YAML, actor, bridge_definition


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


def test_pending_procedure_dry_run_uses_real_proposal_validation(
    procedure_instance: CruxibleInstance,
) -> None:
    _seed_entities(procedure_instance)
    row = {
        "from_type": "Task",
        "from_id": "T-1",
        "to_type": "Incident",
        "to_id": "I-1",
    }
    proposed = service_propose_procedure(
        procedure_instance,
        bridge_definition("duplicate_preview", rows=[row, row]),
        actor_context=actor("author"),
    )

    with pytest.raises(ConfigError, match="Duplicate member"):
        service_run_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            {},
            actor("reviewer"),
            dry_run=True,
        )


def test_pending_procedure_dry_run_previews_retained_pending_members(
    procedure_instance: CruxibleInstance,
) -> None:
    _seed_entities(procedure_instance)
    graph = procedure_instance.load_graph()
    task_two = EntityInstance(entity_type="Task", entity_id="T-2", properties={"status": "open"})
    graph.add_entity(task_two)
    procedure_instance.save_graph_delta(graph, entities=[task_two])
    procedure_id = _accept(
        procedure_instance,
        bridge_definition(
            "retain_preview",
            rows=[
                {
                    "from_type": "Task",
                    "from_id": "T-2",
                    "to_type": "Incident",
                    "to_id": "I-1",
                }
            ],
            pending_refresh_mode="retain_missing",
        ),
    )
    store = procedure_instance.get_procedure_store()
    try:
        procedure = store.get_procedure(procedure_id)
    finally:
        store.close()
    assert procedure is not None
    facts = build_procedure_proposal_facts(
        procedure_id=procedure_id,
        procedure_name=procedure.definition.name,
        definition_digest=procedure.definition_digest,
        step_id="land",
        proposal_scope="blocker-triage",
        relationship_type="blocks",
        edges_from="proposal",
    )
    pending = service_propose_group(
        procedure_instance,
        "blocks",
        [
            CandidateMember(
                from_type="Task",
                from_id="T-1",
                to_type="Incident",
                to_id="I-1",
                relationship_type="blocks",
            )
        ],
        thesis_facts=facts,
        pending_refresh_mode="retain_missing",
        source_workflow_name="procedure:retain_preview",
        actor_context=actor("initial-author"),
        force_review=True,
    )
    assert pending.group_id is not None

    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        {},
        actor("previewer"),
        dry_run=True,
    )

    assert result.output["would_change"]["sections"]["edges"]["counts"]["added"] == 2
    group_store = procedure_instance.get_group_store()
    try:
        stored = group_store.get_group(pending.group_id)
        assert stored.pending_version == 1
        assert [member.from_id for member in group_store.get_members(pending.group_id)] == ["T-1"]
    finally:
        group_store.close()


def test_bridge_reference_must_be_available_on_every_converging_arm(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "graph_format": 2,
            "name": "path_conditional_bridge_input",
            "steps": [
                {
                    "id": "gate",
                    "guard": {"left": True, "op": "eq", "right": True},
                    "on_true": "land",
                    "on_false": "rows",
                    "message": "choose an arm",
                },
                {
                    "id": "rows",
                    "shape_items": {"items": [], "include_input": True},
                    "as": "decision",
                },
                {
                    "id": "land",
                    "propose_group_from": {
                        "relationship_type": "blocks",
                        "edges_from": "$steps.decision.items",
                        "proposal_scope": "conditional",
                    },
                    "as": "proposal",
                },
            ],
            "returns": "proposal",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 0},
        }
    )

    with pytest.raises(ConfigError, match="decision"):
        service_propose_procedure(
            procedure_instance,
            definition,
            actor_context=actor("author"),
        )


def test_trusted_precedent_cannot_auto_resolve_a_procedure_bridge(tmp_path: Path) -> None:
    config_yaml = CONFIG_YAML.replace(
        "      reason:\n        type: string\n        optional: true\n",
        "      reason:\n        type: string\n        optional: true\n"
        "    proposal_policy:\n"
        "      signals:\n"
        "        precedent_check:\n"
        "          role: required\n"
        "      auto_resolve_when: all_support\n"
        "      auto_resolve_requires_prior_trust: trusted_only\n",
        1,
    )
    (tmp_path / "config.yaml").write_text(config_yaml)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    service_lock(instance)
    entities = [
        EntityInstance(entity_type="Task", entity_id="T-prior", properties={"status": "open"}),
        EntityInstance(entity_type="Task", entity_id="T-1", properties={"status": "open"}),
        EntityInstance(entity_type="Incident", entity_id="I-prior", properties={"status": "open"}),
        EntityInstance(entity_type="Incident", entity_id="I-1", properties={"status": "open"}),
    ]
    graph = instance.load_graph()
    for entity in entities:
        graph.add_entity(entity)
    instance.save_graph_delta(graph, entities=entities)
    row = {
        "from_type": "Task",
        "from_id": "T-1",
        "to_type": "Incident",
        "to_id": "I-1",
        "signals": [{"signal_source": "precedent_check", "signal": "support"}],
    }
    procedure_id = _accept(instance, bridge_definition("trusted_bridge", rows=[row]))
    procedure_store = instance.get_procedure_store()
    try:
        procedure = procedure_store.get_procedure(procedure_id)
    finally:
        procedure_store.close()
    assert procedure is not None
    facts = build_procedure_proposal_facts(
        procedure_id=procedure_id,
        procedure_name=procedure.definition.name,
        definition_digest=procedure.definition_digest,
        step_id="land",
        proposal_scope="blocker-triage",
        relationship_type="blocks",
        edges_from="proposal",
    )
    signal = CandidateSignal(signal_source="precedent_check", signal="support")
    prior = service_propose_group(
        instance,
        "blocks",
        [
            CandidateMember(
                from_type="Task",
                from_id="T-prior",
                to_type="Incident",
                to_id="I-prior",
                relationship_type="blocks",
                signals=[signal],
            )
        ],
        thesis_facts=facts,
        signal_sources_used=["precedent_check"],
        source_workflow_name="procedure:trusted_bridge",
        actor_context=actor("precedent-author"),
    )
    assert prior.group_id is not None
    resolved = service_resolve_group(
        instance,
        prior.group_id,
        "approve",
        expected_pending_version=1,
        actor_context=actor("precedent-reviewer"),
    )
    assert resolved.resolution_id is not None
    service_update_trust_status(
        instance,
        resolved.resolution_id,
        "trusted",
        actor_context=actor("trust-reviewer"),
    )

    candidate = CandidateMember(
        from_type="Task",
        from_id="T-1",
        to_type="Incident",
        to_id="I-1",
        relationship_type="blocks",
        signals=[signal],
    )
    eligible = plan_group_proposal(
        instance,
        "blocks",
        [candidate],
        thesis_facts=facts,
        signal_sources_used=["precedent_check"],
        source_workflow_name="procedure:trusted_bridge",
        actor_context=actor("eligibility-check"),
    )
    assert eligible.auto_resolve is True

    result = service_run_procedure(instance, procedure_id, {}, actor("runner"))

    assert result.output["group_status"] == "pending_review"
    assert result.output["group_id"] is not None
    assert all(
        not (relationship.from_id == "T-1" and relationship.to_id == "I-1")
        for relationship in instance.load_graph().iter_relationships("blocks")
    )
