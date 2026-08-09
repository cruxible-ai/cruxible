"""Worked KEV three-arm convergence through one R7-compliant bridge."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.types import EntityInstance
from cruxible_core.procedure.types import ProcedureBridgeStepSchema
from cruxible_core.service import service_propose_procedure, service_run_procedure
from tests.test_procedures.conftest import actor, kev_three_arm_definition


def _seed_kev_entities(instance: CruxibleInstance) -> None:
    entities = [
        EntityInstance(
            entity_type="Vulnerability",
            entity_id="CVE-2026-0001",
            properties={"status": "known-exploited"},
        ),
        *[
            EntityInstance(
                entity_type="Response",
                entity_id=response_id,
                properties={"decision": response_id},
            )
            for response_id in ("patch", "mitigate", "accept")
        ],
    ]
    graph = instance.load_graph()
    for entity in entities:
        graph.add_entity(entity)
    instance.save_graph_delta(graph, entities=entities)


@pytest.mark.parametrize(
    ("payload", "arm", "skipped", "response_id"),
    [
        (
            {"cve_id": "CVE-2026-0001", "exposed": True, "critical": True},
            "patch",
            {"critical_guard", "mitigate", "accept"},
            "patch",
        ),
        (
            {"cve_id": "CVE-2026-0001", "exposed": False, "critical": True},
            "mitigate",
            {"patch", "accept"},
            "mitigate",
        ),
        (
            {"cve_id": "CVE-2026-0001", "exposed": False, "critical": False},
            "accept",
            {"patch", "mitigate"},
            "accept",
        ),
    ],
    ids=["patch", "mitigate", "accept"],
)
def test_each_kev_arm_converges_on_the_single_bridge(
    procedure_instance: CruxibleInstance,
    payload: dict[str, object],
    arm: str,
    skipped: set[str],
    response_id: str,
) -> None:
    _seed_kev_entities(procedure_instance)
    definition = kev_three_arm_definition()
    bridges = [step for step in definition.steps if isinstance(step, ProcedureBridgeStepSchema)]
    assert [bridge.id for bridge in bridges] == ["land_decision"]
    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("author"),
    )

    result = service_run_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        payload,
        actor("reviewer"),
        dry_run=True,
    )

    executed = {
        str(node.detail["step_id"])
        for node in result.receipt.nodes
        if node.node_type == "plan_step" and "step_id" in node.detail
    }
    assert arm in executed
    assert "land_decision" in executed
    assert executed.isdisjoint(skipped)
    added = result.output["would_change"]["sections"]["edges"]["added"]
    assert [edge["to_id"] for edge in added] == [response_id]
    assert result.output["group_status"] == "would_propose"
