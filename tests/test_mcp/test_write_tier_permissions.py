"""Enforcement tests for config-declared direct-write tiers (``write_tier``).

Direct writes (``add_entity`` / ``add_relationship`` / ``batch_direct_write``)
classically require ``graph_write``. A type may declare
``write_tier: governed_write`` to open its direct-write surface to
``governed_write`` actors; undeclared types keep the ``graph_write``
requirement and mixed payloads are gated at the strictest touched type.
Enforcement lives at the ``runtime.api`` direct-write facades, the single
funnel for the MCP, HTTP, and CLI transports.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import PermissionDeniedError
from cruxible_core.mcp import contracts
from cruxible_core.mcp.permissions import (
    PermissionMode,
    init_permissions,
    request_permission_scope,
)
from cruxible_core.runtime import api
from cruxible_core.runtime.instance_manager import get_manager

# Note declares the governed_write surface (entity + attachment edge);
# Task and task_blocks_task stay at the default graph_write requirement.
WRITE_TIER_YAML = dedent(
    """
    version: "1.0"
    name: write_tier_kit

    entity_types:
      Note:
        id: note_id
        write_tier: governed_write
        properties:
          title: string
      Task:
        id: task_id
        properties:
          title: string

    relationships:
      - note_about_task: Note -> Task
        write_tier: governed_write
      - task_blocks_task: Task -> Task
    """
)


@pytest.fixture
def write_tier_instance_id(tmp_path: Path) -> str:
    (tmp_path / "config.yaml").write_text(WRITE_TIER_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    instance_id = str(tmp_path)
    get_manager().register(instance_id, instance)
    init_permissions(PermissionMode.ADMIN)
    # Seed endpoints for relationship writes as a full-tier actor.
    api.add_entities(
        instance_id,
        [
            _entity("Task", "t-1"),
            _entity("Task", "t-2"),
            _entity("Note", "n-seed"),
        ],
    )
    return instance_id


def _entity(entity_type: str, entity_id: str) -> contracts.EntityInput:
    pk = "note_id" if entity_type == "Note" else "task_id"
    return contracts.EntityInput(
        entity_type=entity_type,
        entity_id=entity_id,
        properties={pk: entity_id, "title": f"{entity_type} {entity_id}"},
    )


def _note_edge(note_id: str, task_id: str) -> contracts.RelationshipInput:
    return contracts.RelationshipInput(
        from_type="Note",
        from_id=note_id,
        relationship_type="note_about_task",
        to_type="Task",
        to_id=task_id,
    )


class TestGovernedWriteActor:
    def test_declared_entity_type_allowed(self, write_tier_instance_id):
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            result = api.add_entities(write_tier_instance_id, [_entity("Note", "n-1")])
        assert result.entities_added == 1

    def test_declared_entity_update_allowed(self, write_tier_instance_id):
        """Updates share the declared surface — the tier gates the type, not the verb flavor."""
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            result = api.add_entities(write_tier_instance_id, [_entity("Note", "n-seed")])
        assert result.entities_updated == 1

    def test_undeclared_entity_type_denied(self, write_tier_instance_id):
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.add_entities(write_tier_instance_id, [_entity("Task", "t-3")])

    def test_declared_relationship_allowed(self, write_tier_instance_id):
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            result = api.add_relationships(write_tier_instance_id, [_note_edge("n-seed", "t-1")])
        assert result.added == 1

    def test_undeclared_relationship_denied(self, write_tier_instance_id):
        edge = contracts.RelationshipInput(
            from_type="Task",
            from_id="t-1",
            relationship_type="task_blocks_task",
            to_type="Task",
            to_id="t-2",
        )
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.add_relationships(write_tier_instance_id, [edge])

    def test_declared_batch_payload_allowed(self, write_tier_instance_id):
        payload = contracts.BatchDirectWritePayload(
            entities=[_entity("Note", "n-2")],
            relationships=[
                contracts.BatchRelationshipInput(
                    from_type="Note",
                    from_id="n-2",
                    relationship_type="note_about_task",
                    to_type="Task",
                    to_id="t-1",
                )
            ],
        )
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            result = api.batch_direct_write(write_tier_instance_id, payload)
        assert result.entities_added == 1
        assert result.relationships_added == 1

    def test_mixed_batch_payload_denied(self, write_tier_instance_id):
        """A payload mixing declared and undeclared types requires the max tier."""
        payload = contracts.BatchDirectWritePayload(
            entities=[_entity("Note", "n-3"), _entity("Task", "t-4")],
        )
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.batch_direct_write(write_tier_instance_id, payload)

    def test_mixed_relationship_batch_denied(self, write_tier_instance_id):
        payload = contracts.BatchDirectWritePayload(
            entities=[_entity("Note", "n-4")],
            relationships=[
                contracts.BatchRelationshipInput(
                    from_type="Task",
                    from_id="t-1",
                    relationship_type="task_blocks_task",
                    to_type="Task",
                    to_id="t-2",
                )
            ],
        )
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.batch_direct_write(write_tier_instance_id, payload)

    def test_empty_batch_payload_keeps_graph_write_requirement(self, write_tier_instance_id):
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.batch_direct_write(write_tier_instance_id, contracts.BatchDirectWritePayload())

    def test_unknown_entity_type_denied_not_loosened(self, write_tier_instance_id):
        """Types absent from the config never loosen the requirement."""
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.add_entities(
                    write_tier_instance_id,
                    [contracts.EntityInput(entity_type="Ghost", entity_id="g-1", properties={})],
                )


class TestOtherTiers:
    def test_read_only_denied_before_any_config_read(self, write_tier_instance_id):
        with request_permission_scope(PermissionMode.READ_ONLY):
            with pytest.raises(PermissionDeniedError, match="GOVERNED_WRITE"):
                api.add_entities(write_tier_instance_id, [_entity("Note", "n-ro")])

    def test_graph_write_unaffected_by_declarations(self, write_tier_instance_id):
        """Declared tiers only lower the requirement — graph_write keeps full direct write."""
        with request_permission_scope(PermissionMode.GRAPH_WRITE):
            result = api.add_entities(
                write_tier_instance_id, [_entity("Task", "t-gw"), _entity("Note", "n-gw")]
            )
        assert result.entities_added == 2

    def test_admin_unaffected_by_declarations(self, write_tier_instance_id):
        with request_permission_scope(PermissionMode.ADMIN):
            result = api.add_entities(write_tier_instance_id, [_entity("Task", "t-adm")])
        assert result.entities_added == 1
