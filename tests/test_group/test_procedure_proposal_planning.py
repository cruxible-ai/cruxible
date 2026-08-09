"""Shared, non-persisting proposal planning used by procedure previews."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.types import EntityInstance
from cruxible_core.group.types import CandidateMember
from cruxible_core.service import service_lock, service_propose_group
from cruxible_core.service.groups import plan_group_proposal
from tests.test_procedures.conftest import CONFIG_YAML, actor


@pytest.fixture
def planning_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    service_lock(instance)
    return instance


def _member(from_id: str) -> CandidateMember:
    return CandidateMember(
        from_type="Task",
        from_id=from_id,
        to_type="Incident",
        to_id="I-1",
        relationship_type="blocks",
    )


def test_procedure_planner_retains_missing_members_without_persisting(
    planning_instance: CruxibleInstance,
) -> None:
    graph = planning_instance.load_graph()
    entities = [
        EntityInstance(entity_type="Task", entity_id="T-1", properties={"status": "open"}),
        EntityInstance(entity_type="Task", entity_id="T-2", properties={"status": "open"}),
        EntityInstance(entity_type="Incident", entity_id="I-1", properties={"status": "open"}),
    ]
    for entity in entities:
        graph.add_entity(entity)
    planning_instance.save_graph_delta(graph, entities=entities)
    facts = {
        "origin": {"kind": "procedure", "procedure_id": "PROC-test"},
        "proposal_scope": "retain-preview",
    }
    proposed = service_propose_group(
        planning_instance,
        "blocks",
        [_member("T-1")],
        thesis_facts=facts,
        pending_refresh_mode="retain_missing",
        source_workflow_name="procedure:test",
        actor_context=actor("first-proposer"),
        force_review=True,
    )
    assert proposed.group_id is not None

    plan = plan_group_proposal(
        planning_instance,
        "blocks",
        [_member("T-2")],
        thesis_facts=facts,
        pending_refresh_mode="retain_missing",
        source_workflow_name="procedure:test",
        actor_context=actor("previewer"),
        force_review=True,
    )

    assert [(member.from_id, member.to_id) for member in plan.effective_members] == [
        ("T-1", "I-1"),
        ("T-2", "I-1"),
    ]
    store = planning_instance.get_group_store()
    try:
        pending = store.get_group(proposed.group_id)
        assert pending.pending_version == 1
        assert [member.from_id for member in store.get_members(proposed.group_id)] == ["T-1"]
    finally:
        store.close()
