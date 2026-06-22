"""Tests for service layer mutation functions."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import CandidateGroup, CandidateMember, CandidateSignal
from cruxible_core.query.relationship_state import relationship_matches_query_state
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    FeedbackItemInput,
    RelationshipTargetInput,
    RelationshipWriteInput,
    SharedEvidenceInput,
    service_add_entities,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_add_relationships,
    service_batch_direct_write,
    service_feedback_input,
    service_propose_group,
    service_query_inline_surface,
    service_register_source_artifact,
    service_resolve_group,
)
from cruxible_core.storage.sqlite import SQLiteGraphRepository
from cruxible_core.temporal import utc_now


def _vehicle(
    vid: str, year: int = 2024, make: str = "Honda", model: str = "Civic"
) -> EntityInstance:
    return EntityInstance(
        entity_type="Vehicle",
        entity_id=vid,
        properties={"vehicle_id": vid, "year": year, "make": make, "model": model},
    )


def _part(pid: str, name: str = "Pads", category: str = "brakes") -> EntityInstance:
    return EntityInstance(
        entity_type="Part",
        entity_id=pid,
        properties={"part_number": pid, "name": name, "category": category},
    )


def _batch_payload() -> BatchDirectWriteInput:
    return BatchDirectWriteInput(
        entities=[
            EntityWriteInput(
                entity_type="Vehicle",
                entity_id="V-BATCH",
                properties={
                    "vehicle_id": "V-BATCH",
                    "year": 2026,
                    "make": "Honda",
                    "model": "Pilot",
                },
            ),
            EntityWriteInput(
                entity_type="Part",
                entity_id="BP-BATCH",
                properties={
                    "part_number": "BP-BATCH",
                    "name": "Batch Pads",
                    "category": "brakes",
                },
            ),
        ],
        relationships=[
            BatchRelationshipWriteInput(
                from_type="Part",
                from_id="BP-BATCH",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-BATCH",
                properties={"verified": True, "source": "batch"},
                shared_evidence_keys=["doc"],
                evidence_rationale="Batch payload establishes the fitment.",
            )
        ],
        shared_evidence={
            "doc": SharedEvidenceInput(
                evidence_refs=[{"source": "roadmap_doc", "source_record_id": "batch-section"}],
            )
        },
    )


def _parts_for_vehicle_inline_query() -> dict[str, object]:
    return {
        "name": "inline_fits_for_vehicle",
        "mode": "traversal",
        "entry_point": "Vehicle",
        "traversal": [
            {
                "relationship": "fits",
                "direction": "incoming",
                "filter": {"verified": True},
            }
        ],
        "returns": "fits",
        "result_shape": "relationship",
        "allow_relationship_state_override": True,
    }


def _batch_fit_target() -> RelationshipTargetInput:
    return RelationshipTargetInput(
        from_type="Part",
        from_id="BP-BATCH",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-BATCH",
    )


GUARDED_STATE_YAML = """\
version: "1.0"
name: guarded_project_state

enums:
  lifecycle_status:
    values: [planned, active, closed]
  review_status:
    values: [pending, approved]

entity_types:
  WorkItem:
    properties:
      work_item_id:
        type: string
        primary_key: true
      status:
        type: string
        enum_ref: lifecycle_status
      title:
        type: string
        optional: true
  Review:
    properties:
      review_id:
        type: string
        primary_key: true
      status:
        type: string
        enum_ref: review_status

relationships:
  - name: review_approves_work_item
    from: Review
    to: WorkItem

named_queries:
  approved_review_for_work_item:
    mode: traversal
    entry_point: WorkItem
    traversal:
      - relationship: review_approves_work_item
        direction: incoming
        where:
          candidate.properties.status:
            eq: approved
    returns: list[Review]
    result_shape: entity
  trusted_batch_approved_review_for_work_item:
    mode: traversal
    entry_point: WorkItem
    traversal:
      - relationship: review_approves_work_item
        direction: incoming
        where:
          candidate.properties.status:
            eq: approved
          edge.metadata.provenance.source:
            eq: trusted_batch
    returns: list[Review]
    result_shape: entity

mutation_guards:
  - name: work_item_closed_requires_review
    entity_type: WorkItem
    property: status
    new_value: closed
    condition:
      type: query
      query_name: approved_review_for_work_item
      params:
        work_item_id: "$entity.entity_id"
      min_count: 1
    message: "Work item cannot be closed until approved review exists."
  - name: work_item_active_requires_trusted_batch_review
    entity_type: WorkItem
    property: status
    new_value: active
    condition:
      type: query
      query_name: trusted_batch_approved_review_for_work_item
      params:
        work_item_id: "$entity.entity_id"
      min_count: 1
    message: "Work item cannot be activated until a trusted batch review exists."
"""


def _guarded_instance(tmp_path: Path) -> CruxibleInstance:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(dedent(GUARDED_STATE_YAML))
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _actor_guarded_instance(tmp_path: Path) -> CruxibleInstance:
    actor_guard = """\
  - name: review_approval_requires_authorized_actor
    entity_type: Review
    property: status
    new_value: approved
    condition:
      type: actor
      allowed_actor_ids: [robert]
    message: "Review approvals require an authorized actor."
"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_yaml = GUARDED_STATE_YAML.replace(
        "mutation_guards:\n",
        f"mutation_guards:\n{actor_guard}",
    )
    (tmp_path / "config.yaml").write_text(dedent(config_yaml))
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _actor_context(actor_id: str) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org_1",
        operation_id=f"op_{actor_id}",
        timestamp=utc_now(),
    )


CO_WRITE_GUARD_YAML = """\
version: "1.0"
name: co_write_guard_state

enums:
  lifecycle_status:
    values: [planned, active, closed]

entity_types:
  WorkItem:
    properties:
      work_item_id:
        type: string
        primary_key: true
      status:
        type: string
        enum_ref: lifecycle_status
      title:
        type: string
        optional: true
  Review:
    properties:
      review_id:
        type: string
        primary_key: true
      kind:
        type: string
        optional: true

relationships:
  - name: review_approves_work_item
    from: Review
    to: WorkItem

mutation_guards:
  - name: work_item_closed_requires_co_written_review
    entity_type: WorkItem
    property: status
    new_value: closed
    condition:
      type: co_write
      requires:
        entity_type: Review
        via_relationship: review_approves_work_item
    message: "Closing requires a co-written review."
"""


def _co_write_guarded_instance(tmp_path: Path, *, kind: str | None = None) -> CruxibleInstance:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_text = CO_WRITE_GUARD_YAML
    if kind is not None:
        config_text = config_text.replace(
            "        via_relationship: review_approves_work_item\n",
            (f"        via_relationship: review_approves_work_item\n        kind: {kind}\n"),
        )
    (tmp_path / "config.yaml").write_text(dedent(config_text))
    return CruxibleInstance.init(tmp_path, "config.yaml")


EVIDENCE_GUARD_YAML = """\
version: "1.0"
name: evidence_guard_state

entity_types:
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
        optional: true
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      model:
        type: string
        optional: true

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        optional: true
  - name: part_selected_for_vehicle
    from: Part
    to: Vehicle

mutation_guards:
  - name: fits_requires_source_evidence
    relationship_type: fits
    condition:
      type: evidence
      require_evidence: source_evidence
    message: "Fitment observations require source evidence."
"""


def _evidence_guard_instance(
    tmp_path: Path,
    *,
    min_source_evidence_count: int = 1,
) -> CruxibleInstance:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_text = dedent(EVIDENCE_GUARD_YAML)
    if min_source_evidence_count != 1:
        config_text = config_text.replace(
            "      require_evidence: source_evidence\n",
            (
                "      require_evidence: source_evidence\n"
                f"      min_count: {min_source_evidence_count}\n"
            ),
        )
    (tmp_path / "config.yaml").write_text(config_text)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _seed_guarded_fitment_endpoints(instance: CruxibleInstance) -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="Part",
                entity_id="BP-1002",
                properties={"part_number": "BP-1002", "name": "Brake pads"},
            ),
            EntityWriteInput(
                entity_type="Vehicle",
                entity_id="V-ACCORD",
                properties={"vehicle_id": "V-ACCORD", "model": "Accord"},
            ),
        ],
    )


def _fitment_source_evidence(
    instance: CruxibleInstance,
    *,
    filename: str = "fitment.md",
    text: str = "# Fitment\n\nBP-1002 fits Accord.\n",
) -> dict[str, str]:
    source_path = instance.root / filename
    source_path.write_text(text)
    registered = service_register_source_artifact(
        instance,
        source_path=str(source_path),
    )
    paragraph = next(chunk for chunk in registered.chunks if chunk.block_selector == "paragraph:1")
    return {
        "source_artifact_id": registered.source_artifact_id,
        "chunk_id": paragraph.chunk_id,
    }


GROUP_WRITE_CONFIG_YAML = """\
version: "1.0"
name: direct_write_group_interaction_test

entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
      make:
        type: string
      model:
        type: string
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
      category:
        type: string
        enum: [brakes, suspension, engine, electrical, body, interior]

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
      source:
        type: string
        optional: true
    proposal_policy:
      signals:
        check_v1:
          role: required
      auto_resolve_when: all_support
      auto_resolve_requires_prior_trust: trusted_only

constraints: []
"""


def _group_write_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(GROUP_WRITE_CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = instance.load_graph()
    graph.add_entity(_part("BP-1"))
    graph.add_entity(_part("BP-2"))
    graph.add_entity(_vehicle("V-1"))
    graph.add_entity(_vehicle("V-2", model="Accord"))
    instance.save_graph(graph)
    return instance


def _candidate_member(from_id: str = "BP-1", to_id: str = "V-1") -> CandidateMember:
    return CandidateMember(
        from_type="Part",
        from_id=from_id,
        to_type="Vehicle",
        to_id=to_id,
        relationship_type="fits",
        signals=[CandidateSignal(signal_source="check_v1", signal="support")],
        properties={"verified": False},
    )


def _propose_fits_group(
    instance: CruxibleInstance,
    *,
    from_id: str = "BP-1",
    to_id: str = "V-1",
    members: list[CandidateMember] | None = None,
) -> str:
    result = service_propose_group(
        instance,
        "fits",
        members or [_candidate_member(from_id=from_id, to_id=to_id)],
        thesis_text="test proposal",
        thesis_facts={"source": "test"},
        source_workflow_name="test_group_flow",
        signal_sources_used=["check_v1"],
    )
    return result.group_id


def _stored_group(instance: CruxibleInstance, group_id: str) -> CandidateGroup:
    store = instance.get_group_store()
    try:
        group = store.get_group(group_id)
    finally:
        store.close()
    assert group is not None
    return group


def _stored_group_status(instance: CruxibleInstance, group_id: str) -> str:
    return _stored_group(instance, group_id).status


def _receipt(instance: CruxibleInstance, receipt_id: str | None) -> Receipt:
    assert receipt_id is not None
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
    finally:
        store.close()
    assert receipt is not None
    return receipt


def _relationship_receipt_detail(receipt: Receipt) -> dict[str, object]:
    node = next(node for node in receipt.nodes if node.node_type == "relationship_write")
    return node.detail


def _direct_write_conflicts(instance: CruxibleInstance, group_id: str) -> list[dict[str, object]]:
    conflicts = _stored_group(instance, group_id).analysis_state.get("direct_write_conflicts", [])
    assert isinstance(conflicts, list)
    return conflicts


def _direct_write_conflict_summary(
    instance: CruxibleInstance,
    group_id: str,
) -> dict[str, object]:
    summary = _stored_group(instance, group_id).analysis_state.get(
        "direct_write_conflict_summary",
        {},
    )
    assert isinstance(summary, dict)
    return summary


def _seed_work_item(instance: CruxibleInstance, status: str = "planned") -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="WorkItem",
                entity_id="wi-guarded",
                properties={
                    "work_item_id": "wi-guarded",
                    "status": status,
                    "title": "Guarded item",
                },
            )
        ],
    )


def _seed_approved_review(instance: CruxibleInstance) -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="Review",
                entity_id="rev-approved",
                properties={"review_id": "rev-approved", "status": "approved"},
            )
        ],
    )
    service_add_relationship_inputs(
        instance,
        [
            RelationshipWriteInput(
                from_type="Review",
                from_id="rev-approved",
                relationship_type="review_approves_work_item",
                to_type="WorkItem",
                to_id="wi-guarded",
            )
        ],
        source="test",
        source_ref="approved-review",
    )


# ---------------------------------------------------------------------------
# service_add_entities
# ---------------------------------------------------------------------------


class TestAddEntities:
    def test_single(self, initialized_instance: CruxibleInstance) -> None:
        result = service_add_entities(initialized_instance, [_vehicle("V-1")])
        assert result.added == 1
        assert result.updated == 0

        graph = initialized_instance.load_graph()
        entity = graph.get_entity("Vehicle", "V-1")
        assert entity is not None
        assert entity.properties["make"] == "Honda"

    def test_input_wrapper(self, initialized_instance: CruxibleInstance) -> None:
        result = service_add_entity_inputs(
            initialized_instance,
            [
                EntityWriteInput(
                    entity_type="Vehicle",
                    entity_id="V-1",
                    properties={
                        "vehicle_id": "V-1",
                        "year": 2024,
                        "make": "Honda",
                        "model": "Civic",
                    },
                    metadata={"source": "input-wrapper"},
                )
            ],
        )

        assert result.added == 1
        graph = initialized_instance.load_graph()
        entity = graph.get_entity("Vehicle", "V-1")
        assert entity is not None
        assert entity.metadata == {"source": "input-wrapper"}

    def test_batch(self, initialized_instance: CruxibleInstance) -> None:
        entities = [
            _vehicle("V-1"),
            _vehicle("V-2", make="Toyota", model="Camry"),
            _part("BP-1"),
        ]
        result = service_add_entities(initialized_instance, entities)
        assert result.added == 3
        assert result.updated == 0

    def test_dedup_error(self, initialized_instance: CruxibleInstance) -> None:
        entities = [_vehicle("V-1"), _vehicle("V-1", year=2025)]
        with pytest.raises(DataValidationError, match="duplicate in batch"):
            service_add_entities(initialized_instance, entities)

    def test_bad_type(self, initialized_instance: CruxibleInstance) -> None:
        with pytest.raises(DataValidationError, match="not found in config"):
            service_add_entities(
                initialized_instance,
                [EntityInstance(entity_type="Spaceship", entity_id="X-1")],
            )

    def test_update(self, populated_instance: CruxibleInstance) -> None:
        result = service_add_entities(
            populated_instance,
            [_vehicle("V-2024-CIVIC-EX", year=2025)],
        )
        assert result.added == 0
        assert result.updated == 1

        graph = populated_instance.load_graph()
        entity = graph.get_entity("Vehicle", "V-2024-CIVIC-EX")
        assert entity is not None
        assert entity.properties["year"] == 2025

    def test_update_merges_entity_metadata(self, populated_instance: CruxibleInstance) -> None:
        graph = populated_instance.load_graph()
        graph.update_entity_metadata("Vehicle", "V-2024-CIVIC-EX", {"origin": "fixture"})
        populated_instance.save_graph(graph)

        result = service_add_entity_inputs(
            populated_instance,
            [
                EntityWriteInput(
                    entity_type="Vehicle",
                    entity_id="V-2024-CIVIC-EX",
                    properties={"year": 2025},
                    metadata={"last_seen": "service"},
                )
            ],
        )

        assert result.added == 0
        assert result.updated == 1
        entity = populated_instance.load_graph().get_entity("Vehicle", "V-2024-CIVIC-EX")
        assert entity is not None
        assert entity.metadata == {"origin": "fixture", "last_seen": "service"}

    def test_incremental_entity_mutation_does_not_full_save(
        self, initialized_instance: CruxibleInstance
    ) -> None:
        def fail_full_save(_self: SQLiteGraphRepository, _graph) -> None:
            raise AssertionError("service_add_entities should not replace the full graph")

        with patch.object(SQLiteGraphRepository, "save_graph", fail_full_save):
            result = service_add_entities(initialized_instance, [_vehicle("V-1")])

        assert result.added == 1
        restarted = CruxibleInstance.load(initialized_instance.root)
        assert restarted.load_graph().get_entity("Vehicle", "V-1") is not None


class TestEntityMutationGuards:
    def test_unmatched_guard_allows_unrelated_update(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-guarded",
                    properties={"title": "Updated title"},
                )
            ],
        )

        assert result.updated == 1
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "planned"
        assert entity.properties["title"] == "Updated title"

    def test_add_entity_update_rejects_closed_without_review(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(DataValidationError) as exc_info:
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    )
                ],
            )

        assert "work_item_closed_requires_review" in str(exc_info.value)
        assert "Work item cannot be closed" in str(exc_info.value)
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "planned"

    def test_add_entity_guard_failure_receipt_records_guard_error(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(DataValidationError) as exc_info:
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    )
                ],
            )

        receipt_id = exc_info.value.mutation_receipt_id
        assert receipt_id is not None
        store = instance.get_receipt_store()
        try:
            receipt = store.get_receipt(receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.committed is False
        failed_validations = [
            node
            for node in receipt.nodes
            if node.node_type == "validation" and node.detail.get("passed") is False
        ]
        assert any(
            "work_item_closed_requires_review" in node.detail.get("guard_error", "")
            for node in failed_validations
        )

    def test_add_entity_update_allows_closed_with_approved_review(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)
        _seed_approved_review(instance)

        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-guarded",
                    properties={"status": "closed"},
                )
            ],
        )

        assert result.updated == 1
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "closed"

    def test_actor_identity_guard_rejects_approval_without_actor(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _actor_guarded_instance(tmp_path)
        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Review",
                    entity_id="rev-pending",
                    properties={"review_id": "rev-pending", "status": "pending"},
                )
            ],
        )

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_authorized_actor",
        ):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-pending",
                        properties={"status": "approved"},
                    )
                ],
            )

        entity = instance.load_graph().get_entity("Review", "rev-pending")
        assert entity is not None
        assert entity.properties["status"] == "pending"

    def test_actor_identity_guard_rejects_unauthorized_actor(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _actor_guarded_instance(tmp_path)

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_authorized_actor",
        ):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-approved",
                        properties={"review_id": "rev-approved", "status": "approved"},
                    )
                ],
                actor_context=_actor_context("codex-core"),
            )

        assert instance.load_graph().get_entity("Review", "rev-approved") is None

    def test_actor_identity_guard_allows_authorized_actor(self, tmp_path: Path) -> None:
        instance = _actor_guarded_instance(tmp_path)

        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Review",
                    entity_id="rev-approved",
                    properties={"review_id": "rev-approved", "status": "approved"},
                )
            ],
            actor_context=_actor_context("robert"),
        )

        assert result.added == 1
        entity = instance.load_graph().get_entity("Review", "rev-approved")
        assert entity is not None
        assert entity.properties["status"] == "approved"

    def test_actor_identity_guard_dry_run_reports_unauthorized_actor(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _actor_guarded_instance(tmp_path)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-approved",
                        properties={"review_id": "rev-approved", "status": "approved"},
                    )
                ]
            ),
            dry_run=True,
            actor_context=_actor_context("codex-core"),
        )

        assert result.valid is False
        assert any(
            "review_approval_requires_authorized_actor" in error
            for error in result.validation_errors
        )
        assert instance.load_graph().get_entity("Review", "rev-approved") is None

    def test_batch_dry_run_reports_guard_failure_without_mutating(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    )
                ],
            ),
            dry_run=True,
        )

        assert result.valid is False
        assert any(
            "work_item_closed_requires_review" in error for error in result.validation_errors
        )
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "planned"

    def test_batch_apply_rejects_guard_atomically(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[
                        EntityWriteInput(
                            entity_type="WorkItem",
                            entity_id="wi-guarded",
                            properties={"status": "closed"},
                        ),
                        EntityWriteInput(
                            entity_type="Review",
                            entity_id="rev-pending",
                            properties={"review_id": "rev-pending", "status": "pending"},
                        ),
                    ],
                ),
            )

        graph = instance.load_graph()
        work_item = graph.get_entity("WorkItem", "wi-guarded")
        assert work_item is not None
        assert work_item.properties["status"] == "planned"
        assert graph.get_entity("Review", "rev-pending") is None

    def test_batch_apply_allows_closed_with_approved_review(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)
        _seed_approved_review(instance)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    )
                ],
            ),
        )

        assert result.valid is True
        assert result.entities_updated == 1
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "closed"

    def test_batch_apply_allows_closed_with_same_batch_approved_review(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-approved",
                        properties={"review_id": "rev-approved", "status": "approved"},
                    ),
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    ),
                ],
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Review",
                        from_id="rev-approved",
                        relationship_type="review_approves_work_item",
                        to_type="WorkItem",
                        to_id="wi-guarded",
                    )
                ],
            ),
        )

        assert result.valid is True
        assert result.entities_added == 1
        assert result.entities_updated == 1
        assert result.relationships_added == 1
        graph = instance.load_graph()
        entity = graph.get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "closed"
        assert (
            graph.get_relationship(
                "Review",
                "rev-approved",
                "WorkItem",
                "wi-guarded",
                "review_approves_work_item",
            )
            is not None
        )

    def test_batch_guard_evaluates_same_batch_relationship_with_real_source(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)
        payload = BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="Review",
                    entity_id="rev-approved",
                    properties={"review_id": "rev-approved", "status": "approved"},
                ),
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-guarded",
                    properties={"status": "active"},
                ),
            ],
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="Review",
                    from_id="rev-approved",
                    relationship_type="review_approves_work_item",
                    to_type="WorkItem",
                    to_id="wi-guarded",
                )
            ],
        )

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(
                instance,
                payload,
                source="untrusted_batch",
                source_ref="untrusted",
            )
        graph = instance.load_graph()
        entity = graph.get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "planned"
        assert graph.get_entity("Review", "rev-approved") is None

        trusted_instance = _guarded_instance(tmp_path / "trusted")
        _seed_work_item(trusted_instance)
        result = service_batch_direct_write(
            trusted_instance,
            payload,
            source="trusted_batch",
            source_ref="trusted",
        )

        assert result.valid is True
        relationship = trusted_instance.load_graph().get_relationship(
            "Review",
            "rev-approved",
            "WorkItem",
            "wi-guarded",
            "review_approves_work_item",
        )
        assert relationship is not None
        assert relationship.metadata.provenance is not None
        assert relationship.metadata.provenance.source == "trusted_batch"

    def test_dry_run_validates_guards_without_mutating(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)

        with pytest.raises(DataValidationError, match="work_item_closed_requires_review"):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-dry",
                        properties={"work_item_id": "wi-dry", "status": "closed"},
                    )
                ],
                dry_run=True,
            )

        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-dry",
                    properties={"work_item_id": "wi-dry", "status": "planned"},
                )
            ],
            dry_run=True,
        )

        assert result.added == 1
        assert result.updated == 0
        assert result.receipt_id is None
        assert instance.load_graph().get_entity("WorkItem", "wi-dry") is None

    def test_dry_run_add_relationships_does_not_mutate(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)
        _seed_approved_review(instance)

        result = service_add_relationship_inputs(
            instance,
            [
                RelationshipWriteInput(
                    from_type="Review",
                    from_id="rev-approved",
                    relationship_type="review_approves_work_item",
                    to_type="WorkItem",
                    to_id="wi-guarded",
                )
            ],
            source="test",
            source_ref="dry-run",
            dry_run=True,
        )

        assert result.added == 0
        assert result.updated == 1
        assert result.receipt_id is None

    def test_create_with_guarded_value_rejected(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)

        with pytest.raises(DataValidationError, match="work_item_closed_requires_review"):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-born-closed",
                        properties={"work_item_id": "wi-born-closed", "status": "closed"},
                    )
                ],
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None

    def test_batch_create_with_guarded_value_rejected_atomically(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[
                        EntityWriteInput(
                            entity_type="WorkItem",
                            entity_id="wi-born-closed",
                            properties={
                                "work_item_id": "wi-born-closed",
                                "status": "closed",
                            },
                        )
                    ],
                ),
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None

    def test_batch_create_with_guarded_value_allowed_with_same_batch_review(
        self, tmp_path: Path
    ) -> None:
        instance = _guarded_instance(tmp_path)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-born-closed",
                        properties={"work_item_id": "wi-born-closed", "status": "closed"},
                    ),
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-import",
                        properties={"review_id": "rev-import", "status": "approved"},
                    ),
                ],
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Review",
                        from_id="rev-import",
                        relationship_type="review_approves_work_item",
                        to_type="WorkItem",
                        to_id="wi-born-closed",
                    )
                ],
            ),
        )

        assert result.valid is True
        entity = instance.load_graph().get_entity("WorkItem", "wi-born-closed")
        assert entity is not None
        assert entity.properties["status"] == "closed"

    def test_reasserting_guarded_value_is_not_a_transition(self, tmp_path: Path) -> None:
        # Work closed before a guard existed must stay editable: re-asserting
        # status=closed alongside other changes is not a transition.
        tmp_path.mkdir(parents=True, exist_ok=True)
        unguarded = GUARDED_STATE_YAML.split("mutation_guards:")[0]
        (tmp_path / "config.yaml").write_text(dedent(unguarded))
        instance = CruxibleInstance.init(tmp_path, "config.yaml")
        _seed_work_item(instance, status="closed")

        (tmp_path / "config.yaml").write_text(dedent(GUARDED_STATE_YAML))
        guarded = CruxibleInstance.load(tmp_path)

        result = service_add_entity_inputs(
            guarded,
            [
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-guarded",
                    properties={"status": "closed", "title": "Retitled closed item"},
                )
            ],
        )

        assert result.updated == 1
        entity = guarded.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["title"] == "Retitled closed item"

    def test_current_ref_guard_rejects_creates_fail_closed(self, tmp_path: Path) -> None:
        # $current.* params have no prior state on creates; such guards are
        # transition-only in practice and must reject creates, not pass them.
        current_ref_guard = dedent(
            """\
            mutation_guards:
              - name: closed_requires_prior_state_lookup
                entity_type: WorkItem
                property: status
                new_value: closed
                condition:
                  type: query
                  query_name: approved_review_for_work_item
                  params:
                    work_item_id: "$current.properties.work_item_id"
                  min_count: 1
            """
        )
        config_yaml = GUARDED_STATE_YAML.split("mutation_guards:")[0] + current_ref_guard
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.yaml").write_text(dedent(config_yaml))
        instance = CruxibleInstance.init(tmp_path, "config.yaml")

        with pytest.raises(DataValidationError, match="Missing mutation guard param reference"):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-born-closed",
                        properties={"work_item_id": "wi-born-closed", "status": "closed"},
                    )
                ],
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None


class TestCoWriteMutationGuard:
    """co_write guards reject a guarded transition unless THIS write co-creates
    the required linked entity in the same write delta."""

    @staticmethod
    def _work_item(entity_id: str = "wi-1", status: str = "closed") -> EntityWriteInput:
        return EntityWriteInput(
            entity_type="WorkItem",
            entity_id=entity_id,
            properties={"work_item_id": entity_id, "status": status},
        )

    @staticmethod
    def _review(entity_id: str = "rev-1", *, kind: str | None = None) -> EntityWriteInput:
        props: dict[str, object] = {"review_id": entity_id}
        if kind is not None:
            props["kind"] = kind
        return EntityWriteInput(
            entity_type="Review",
            entity_id=entity_id,
            properties=props,
        )

    @staticmethod
    def _link(review_id: str = "rev-1", work_item_id: str = "wi-1") -> BatchRelationshipWriteInput:
        return BatchRelationshipWriteInput(
            from_type="Review",
            from_id=review_id,
            relationship_type="review_approves_work_item",
            to_type="WorkItem",
            to_id=work_item_id,
        )

    def test_rejects_when_no_co_written_required_entity(self, tmp_path: Path) -> None:
        instance = _co_write_guarded_instance(tmp_path)

        with pytest.raises(DataValidationError, match="Closing requires a co-written review"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(entities=[self._work_item()]),
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-1") is None

    def test_passes_with_required_entity_in_same_batch(self, tmp_path: Path) -> None:
        instance = _co_write_guarded_instance(tmp_path)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[self._work_item(), self._review()],
                relationships=[self._link()],
            ),
        )

        assert result.valid is True
        entity = instance.load_graph().get_entity("WorkItem", "wi-1")
        assert entity is not None
        assert entity.properties["status"] == "closed"

    def test_stale_pre_existing_linked_entity_does_not_satisfy(self, tmp_path: Path) -> None:
        # The review + link are written in a PRIOR write; the later guarded
        # close must still be rejected because nothing is co-created this write.
        instance = _co_write_guarded_instance(tmp_path)

        seed = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[self._work_item(status="planned"), self._review()],
                relationships=[self._link()],
            ),
        )
        assert seed.valid is True

        with pytest.raises(DataValidationError, match="Closing requires a co-written review"):
            service_add_entity_inputs(
                instance,
                [self._work_item(status="closed")],
            )

        entity = instance.load_graph().get_entity("WorkItem", "wi-1")
        assert entity is not None
        # The stale link must NOT have let the close through.
        assert entity.properties["status"] == "planned"

    def test_non_trigger_new_value_does_not_fire(self, tmp_path: Path) -> None:
        instance = _co_write_guarded_instance(tmp_path)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[self._work_item(status="active")],
            ),
        )

        assert result.valid is True
        entity = instance.load_graph().get_entity("WorkItem", "wi-1")
        assert entity is not None
        assert entity.properties["status"] == "active"

    def test_link_to_pre_existing_review_does_not_satisfy(self, tmp_path: Path) -> None:
        # Review pre-exists; this write co-creates only the linking edge, not the
        # required entity. The required ENTITY must be created in this write.
        instance = _co_write_guarded_instance(tmp_path)
        seed = service_add_entity_inputs(instance, [self._review()])
        assert seed.added == 1

        with pytest.raises(DataValidationError, match="Closing requires a co-written review"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[self._work_item()],
                    relationships=[self._link()],
                ),
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-1") is None

    def test_kind_filter_respected(self, tmp_path: Path) -> None:
        instance = _co_write_guarded_instance(tmp_path, kind="approval")

        # Wrong kind: rejected.
        with pytest.raises(DataValidationError, match="Closing requires a co-written review"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[self._work_item(), self._review(kind="comment")],
                    relationships=[self._link()],
                ),
            )
        assert instance.load_graph().get_entity("WorkItem", "wi-1") is None

        # Matching kind: passes.
        accepted = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    self._work_item(entity_id="wi-2"),
                    self._review(entity_id="rev-2", kind="approval"),
                ],
                relationships=[self._link(review_id="rev-2", work_item_id="wi-2")],
            ),
        )
        assert accepted.valid is True
        entity = instance.load_graph().get_entity("WorkItem", "wi-2")
        assert entity is not None
        assert entity.properties["status"] == "closed"

    def test_list_new_value_fires_on_each_listed_value(self, tmp_path: Path) -> None:
        config_text = CO_WRITE_GUARD_YAML.replace(
            "    new_value: closed\n",
            "    new_value: [closed, active]\n",
        )
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.yaml").write_text(dedent(config_text))
        instance = CruxibleInstance.init(tmp_path, "config.yaml")

        for status, wi_id in (("closed", "wi-closed"), ("active", "wi-active")):
            # Each listed value triggers the guard: bare transition rejected.
            with pytest.raises(DataValidationError, match="Closing requires a co-written review"):
                service_batch_direct_write(
                    instance,
                    BatchDirectWriteInput(
                        entities=[self._work_item(entity_id=wi_id, status=status)]
                    ),
                )

            # And is allowed only with the co-written review.
            review_id = f"rev-{wi_id}"
            accepted = service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[
                        self._work_item(entity_id=wi_id, status=status),
                        self._review(entity_id=review_id),
                    ],
                    relationships=[self._link(review_id=review_id, work_item_id=wi_id)],
                ),
            )
            assert accepted.valid is True, f"{status} should pass with a co-written review"

        # A value NOT in the list does not fire.
        unguarded = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[self._work_item(entity_id="wi-planned", status="planned")]
            ),
        )
        assert unguarded.valid is True


class TestBatchDirectWrite:
    def test_dry_run_validates_same_payload_relationship_without_mutating(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        payload = _batch_payload()

        result = service_batch_direct_write(initialized_instance, payload, dry_run=True)

        assert result.valid is True
        assert result.dry_run is True
        assert result.entities_added == 2
        assert result.relationships_added == 1
        assert result.evidence_sources_used == ["roadmap_doc"]
        assert result.receipt_id is None
        graph = initialized_instance.load_graph()
        assert graph.get_entity("Vehicle", "V-BATCH") is None
        assert (
            graph.get_relationship(
                "Part",
                "BP-BATCH",
                "Vehicle",
                "V-BATCH",
                "fits",
            )
            is None
        )

    def test_apply_writes_batch_and_compact_receipt_summary(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        result = service_batch_direct_write(
            initialized_instance,
            _batch_payload(),
            source="test_batch",
            source_ref="test-batch",
        )

        assert result.valid is True
        assert result.dry_run is False
        assert result.entities_added == 2
        assert result.relationships_added == 1
        assert result.receipt_id is not None
        graph = initialized_instance.load_graph()
        relationship = graph.get_relationship(
            "Part",
            "BP-BATCH",
            "Vehicle",
            "V-BATCH",
            "fits",
        )
        assert relationship is not None
        assert relationship.metadata.evidence is not None
        assert relationship.metadata.evidence.rationale == (
            "Batch payload establishes the fitment."
        )
        assert relationship.metadata.evidence.evidence_refs[0].source == "roadmap_doc"
        assert relationship.metadata.provenance is not None
        assert relationship.metadata.provenance.receipt_id == result.receipt_id
        assert relationship.metadata.provenance.resolution_id is None

        store = initialized_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.operation_type == "batch_direct_write"
        assert receipt.committed is True

    def test_pending_relationship_write_is_reviewable_not_live(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        payload = _batch_payload()
        payload.relationships[0].pending = True

        result = service_batch_direct_write(
            initialized_instance,
            payload,
            source="test_batch",
            source_ref="test-batch",
        )

        assert result.valid is True
        assert result.relationships_added == 1
        graph = initialized_instance.load_graph()
        relationship = graph.get_relationship("Part", "BP-BATCH", "Vehicle", "V-BATCH", "fits")
        assert relationship is not None
        assert relationship.metadata.assertion.review.status == "pending"
        assert relationship.metadata.assertion.review.source == "agent"

        query = _parts_for_vehicle_inline_query()
        params = {"vehicle_id": "V-BATCH"}
        live = service_query_inline_surface(initialized_instance, query, params)
        pending = service_query_inline_surface(
            initialized_instance,
            query,
            params,
            relationship_state="pending",
        )

        assert live.items == []
        assert len(pending.items) == 1
        assert relationship_matches_query_state(relationship.metadata, "reviewable")

    def test_receiptless_feedback_approves_pending_relationship(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        payload = _batch_payload()
        payload.relationships[0].pending = True
        service_batch_direct_write(initialized_instance, payload)

        result = service_feedback_input(
            initialized_instance,
            FeedbackItemInput(
                action="approve",
                target=_batch_fit_target(),
            ),
            source="human",
        )

        assert result.applied is True
        assert result.receipt_id is not None
        store = initialized_instance.get_feedback_store()
        try:
            record = store.get_feedback(result.feedback_id)
        finally:
            store.close()
        assert record is not None
        assert record.receipt_id is None
        graph = initialized_instance.load_graph()
        relationship = graph.get_relationship("Part", "BP-BATCH", "Vehicle", "V-BATCH", "fits")
        assert relationship is not None
        assert relationship.metadata.assertion.review.status == "approved"
        assert relationship.metadata.assertion.review.source == "human"

        live = service_query_inline_surface(
            initialized_instance,
            _parts_for_vehicle_inline_query(),
            {"vehicle_id": "V-BATCH"},
        )
        assert len(live.items) == 1

    def test_receiptless_feedback_rejects_pending_relationship(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        payload = _batch_payload()
        payload.relationships[0].pending = True
        service_batch_direct_write(initialized_instance, payload)

        result = service_feedback_input(
            initialized_instance,
            FeedbackItemInput(
                action="reject",
                target=_batch_fit_target(),
            ),
            source="human",
        )

        assert result.applied is True
        graph = initialized_instance.load_graph()
        relationship = graph.get_relationship("Part", "BP-BATCH", "Vehicle", "V-BATCH", "fits")
        assert relationship is not None
        assert relationship.metadata.assertion.review.status == "rejected"
        live = service_query_inline_surface(
            initialized_instance,
            _parts_for_vehicle_inline_query(),
            {"vehicle_id": "V-BATCH"},
        )
        pending = service_query_inline_surface(
            initialized_instance,
            _parts_for_vehicle_inline_query(),
            {"vehicle_id": "V-BATCH"},
            relationship_state="pending",
        )
        assert live.items == []
        assert pending.items == []

    def test_pending_relationship_duplicate_tuple_is_rejected(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        payload = _batch_payload()
        payload.relationships[0].pending = True
        service_batch_direct_write(initialized_instance, payload)

        with pytest.raises(DataValidationError, match="pending relationship writes"):
            service_batch_direct_write(initialized_instance, payload)

    def test_invalid_apply_does_not_commit_partial_entities(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        payload = BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="Vehicle",
                    entity_id="V-BATCH",
                    properties={
                        "vehicle_id": "V-BATCH",
                        "year": 2026,
                        "make": "Honda",
                        "model": "Pilot",
                    },
                )
            ],
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-MISSING",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-BATCH",
                    shared_evidence_keys=["missing"],
                )
            ],
        )

        dry_run = service_batch_direct_write(initialized_instance, payload, dry_run=True)
        assert dry_run.valid is False
        assert "shared_evidence key 'missing' not found" in dry_run.validation_errors[0]

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(initialized_instance, payload)

        graph = initialized_instance.load_graph()
        assert graph.get_entity("Vehicle", "V-BATCH") is None


class TestDirectWriteGroupInteractions:
    def test_add_relationships_reports_pending_group_conflict_and_receipt_detail(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _group_write_instance(tmp_path)
        group_id = _propose_fits_group(instance)

        result = service_add_relationships(
            instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": True, "source": "direct"},
                )
            ],
            source="test",
            source_ref="direct",
        )

        assert result.added == 1
        assert result.updated == 0
        assert len(result.pending_conflicts) == 1
        conflict = result.pending_conflicts[0]
        assert conflict.group_id == group_id
        assert conflict.group_status == "pending_review"
        assert conflict.group_signature is not None
        assert conflict.source_workflow_name == "test_group_flow"
        assert conflict.edge_key is None
        assert result.updated_group_backed_edges == []
        assert _stored_group_status(instance, group_id) == "pending_review"
        conflicts = _direct_write_conflicts(instance, group_id)
        assert len(conflicts) == 1
        assert conflicts[0]["relationship_type"] == "fits"
        assert conflicts[0]["from_id"] == "BP-1"
        assert conflicts[0]["to_id"] == "V-1"
        assert conflicts[0]["receipt_id"] == result.receipt_id
        assert conflicts[0]["edge_key"] is not None
        assert conflicts[0]["source"] == "test"
        assert conflicts[0]["source_ref"] == "direct"
        summary = _direct_write_conflict_summary(instance, group_id)
        assert summary == {
            "conflicted_member_count": 1,
            "member_count": 1,
            "coverage": "full",
            "last_receipt_id": result.receipt_id,
            "review_hint": "live_state_changed_since_proposal",
        }

        receipt = _receipt(instance, result.receipt_id)
        validation_details = [
            node.detail for node in receipt.nodes if node.node_type == "validation"
        ]
        assert any(
            detail.get("pending_conflicts", [{}])[0].get("group_id") == group_id
            for detail in validation_details
            if detail.get("pending_conflicts")
        )
        assert any(
            detail.get("direct_write_group_annotations", [{}])[0].get("group_id") == group_id
            for detail in validation_details
            if detail.get("direct_write_group_annotations")
        )
        write_detail = _relationship_receipt_detail(receipt)
        assert write_detail["pending_conflicts"][0]["group_id"] == group_id

    def test_batch_direct_write_reports_pending_conflict_in_dry_run_and_apply(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _group_write_instance(tmp_path)
        group_id = _propose_fits_group(instance)
        payload = BatchDirectWriteInput(
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": True, "source": "batch"},
                )
            ],
        )

        dry_run = service_batch_direct_write(instance, payload, dry_run=True)

        assert dry_run.valid is True
        assert dry_run.pending_conflicts[0].group_id == group_id
        assert dry_run.updated_group_backed_edges == []
        assert _direct_write_conflicts(instance, group_id) == []
        assert (
            instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits") is None
        )

        applied = service_batch_direct_write(instance, payload)

        assert applied.valid is True
        assert applied.relationships_added == 1
        assert applied.pending_conflicts[0].group_id == group_id
        assert applied.updated_group_backed_edges == []
        assert (
            instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
            is not None
        )
        receipt = _receipt(instance, applied.receipt_id)
        write_detail = _relationship_receipt_detail(receipt)
        assert write_detail["pending_conflicts"][0]["group_id"] == group_id
        assert _direct_write_conflicts(instance, group_id)[0]["receipt_id"] == applied.receipt_id
        assert _direct_write_conflict_summary(instance, group_id)["coverage"] == "full"

    def test_group_conflict_summary_tracks_partial_and_full_coverage(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _group_write_instance(tmp_path)
        group_id = _propose_fits_group(
            instance,
            members=[
                _candidate_member(from_id="BP-1", to_id="V-1"),
                _candidate_member(from_id="BP-2", to_id="V-2"),
            ],
        )

        first = service_add_relationships(
            instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": True, "source": "direct"},
                )
            ],
            source="test",
            source_ref="direct-first",
        )

        assert first.pending_conflicts[0].group_id == group_id
        summary = _direct_write_conflict_summary(instance, group_id)
        assert summary["coverage"] == "partial"
        assert summary["conflicted_member_count"] == 1
        assert summary["member_count"] == 2

        second = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-2",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2",
                        properties={"verified": True, "source": "batch"},
                    )
                ],
            ),
        )

        assert second.pending_conflicts[0].group_id == group_id
        conflicts = _direct_write_conflicts(instance, group_id)
        assert len(conflicts) == 2
        summary = _direct_write_conflict_summary(instance, group_id)
        assert summary["coverage"] == "full"
        assert summary["conflicted_member_count"] == 2
        assert summary["member_count"] == 2
        assert summary["last_receipt_id"] == second.receipt_id

    def test_repeated_direct_write_updates_existing_conflict_record(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _group_write_instance(tmp_path)
        group_id = _propose_fits_group(instance)
        relationship = RelationshipInstance(
            from_type="Part",
            from_id="BP-1",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": True, "source": "first"},
        )
        first = service_add_relationships(
            instance,
            [relationship],
            source="test",
            source_ref="direct-first",
        )

        second = service_add_relationships(
            instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": True, "source": "second"},
                )
            ],
            source="test",
            source_ref="direct-second",
        )

        conflicts = _direct_write_conflicts(instance, group_id)
        assert len(conflicts) == 1
        assert conflicts[0]["receipt_id"] == second.receipt_id
        assert conflicts[0]["source_ref"] == "direct-second"
        assert conflicts[0]["edge_key"] is not None
        assert first.receipt_id != second.receipt_id

    def test_direct_write_update_reports_group_backed_edge(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _group_write_instance(tmp_path)
        group_id = _propose_fits_group(instance)
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

        result = service_add_relationships(
            instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": True, "source": "direct-update"},
                )
            ],
            source="test",
            source_ref="direct",
        )

        assert result.added == 0
        assert result.updated == 1
        assert result.pending_conflicts == []
        assert len(result.updated_group_backed_edges) == 1
        updated = result.updated_group_backed_edges[0]
        assert updated.group_id == group_id
        assert updated.group_status == "resolved"
        assert updated.group_signature is not None
        assert updated.source_workflow_name == "test_group_flow"
        assert updated.edge_key is not None

        relationship = instance.load_graph().get_relationship(
            "Part",
            "BP-1",
            "Vehicle",
            "V-1",
            "fits",
        )
        assert relationship is not None
        assert relationship.metadata.provenance is not None
        assert relationship.metadata.provenance.source_ref == f"group:{group_id}"

        receipt = _receipt(instance, result.receipt_id)
        write_detail = _relationship_receipt_detail(receipt)
        assert write_detail["updated_group_backed_edges"][0]["group_id"] == group_id

    def test_resolved_rejected_group_is_not_pending_conflict(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _group_write_instance(tmp_path)
        group_id = _propose_fits_group(instance)
        service_resolve_group(instance, group_id, "reject", expected_pending_version=1)

        result = service_add_relationships(
            instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": True, "source": "direct"},
                )
            ],
            source="test",
            source_ref="direct",
        )

        assert result.added == 1
        assert result.pending_conflicts == []
        assert result.updated_group_backed_edges == []
        assert _direct_write_conflicts(instance, group_id) == []

    def test_no_conflict_direct_write_returns_empty_interaction_lists(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _group_write_instance(tmp_path)

        result = service_add_relationships(
            instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-2",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2",
                    properties={"verified": True, "source": "direct"},
                )
            ],
            source="test",
            source_ref="direct",
        )

        assert result.added == 1
        assert result.pending_conflicts == []
        assert result.updated_group_backed_edges == []

    def test_invalid_direct_write_does_not_annotate_pending_group(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _group_write_instance(tmp_path)
        group_id = _propose_fits_group(instance)

        with pytest.raises(DataValidationError, match="Relationship validation failed"):
            service_add_relationships(
                instance,
                [
                    RelationshipInstance(
                        from_type="Part",
                        from_id="BP-1",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-1",
                        properties={"verified": "not-a-bool"},
                    )
                ],
                source="test",
                source_ref="invalid-direct",
            )

        assert _direct_write_conflicts(instance, group_id) == []


# ---------------------------------------------------------------------------
# service_add_relationships
# ---------------------------------------------------------------------------


class TestAddRelationships:
    def test_single(self, populated_instance: CruxibleInstance) -> None:
        result = service_add_relationships(
            populated_instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                )
            ],
            source="test",
            source_ref="test_single",
        )
        assert result.added == 1
        assert result.updated == 0

        graph = populated_instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1002", "Vehicle", "V-2024-ACCORD-SPORT", "fits")
        assert rel is not None
        assert rel.metadata.provenance is not None

    def test_input_wrapper(self, populated_instance: CruxibleInstance) -> None:
        result = service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                )
            ],
            source="test",
            source_ref="test_input_wrapper",
        )

        assert result.added == 1
        graph = populated_instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1002", "Vehicle", "V-2024-ACCORD-SPORT", "fits")
        assert rel is not None

    def test_input_wrapper_persists_explicit_evidence_refs(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                    evidence_refs=[
                        {
                            "source": "roadmap_doc",
                            "source_record_id": "section-p0",
                        }
                    ],
                    evidence_rationale="Accepted direct source-backed assertion.",
                )
            ],
            source="test",
            source_ref="test_input_wrapper_evidence",
        )

        assert result.added == 1
        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "unreviewed"
        assert rel.metadata.evidence is not None
        assert rel.metadata.evidence.rationale == "Accepted direct source-backed assertion."
        assert [ref.source for ref in rel.metadata.evidence.evidence_refs] == ["roadmap_doc"]
        assert rel.metadata.evidence.evidence_refs[0].source_record_id == "section-p0"

    def test_input_wrapper_persists_source_evidence_refs(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        source_path = populated_instance.root / "fitment.md"
        source_path.write_text("# Fitment\n\nBP-1002 fits Accord Sport.\n")
        registered = service_register_source_artifact(
            populated_instance,
            source_path=str(source_path),
        )
        paragraph = next(
            chunk for chunk in registered.chunks if chunk.block_selector == "paragraph:1"
        )

        result = service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                    source_evidence=[
                        {
                            "source_artifact_id": registered.source_artifact_id,
                            "chunk_id": paragraph.chunk_id,
                        }
                    ],
                )
            ],
            source="test",
            source_ref="test_input_wrapper_source_evidence",
        )

        assert result.added == 1
        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is not None
        assert rel.metadata.evidence is not None
        evidence_ref = rel.metadata.evidence.evidence_refs[0]
        assert evidence_ref.source == "source_artifact"
        assert evidence_ref.artifact_id == registered.source_artifact_id
        assert evidence_ref.source_record_id == paragraph.chunk_id
        assert evidence_ref.metadata["content_hash"] == paragraph.content_hash

    @pytest.mark.parametrize(
        "evidence_kwargs",
        [
            {"evidence_rationale": "Looks right from context."},
            {"evidence_refs": [{"source": "catalog", "source_record_id": "row-1"}]},
        ],
        ids=["rationale_only", "generic_ref"],
    )
    def test_relationship_evidence_guard_rejects_non_source_evidence(
        self,
        tmp_path: Path,
        evidence_kwargs: dict[str, object],
    ) -> None:
        instance = _evidence_guard_instance(tmp_path)
        _seed_guarded_fitment_endpoints(instance)

        with pytest.raises(DataValidationError, match="fits_requires_source_evidence"):
            service_add_relationship_inputs(
                instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-ACCORD",
                        properties={"verified": True},
                        **evidence_kwargs,
                    )
                ],
                source="test",
                source_ref="guarded_non_source_evidence",
            )

        rel = instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-ACCORD",
            "fits",
        )
        assert rel is None

    def test_relationship_evidence_guard_rejects_fabricated_source_artifact_ref(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _evidence_guard_instance(tmp_path)
        _seed_guarded_fitment_endpoints(instance)

        with pytest.raises(DataValidationError, match="fits_requires_source_evidence"):
            service_add_relationship_inputs(
                instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-ACCORD",
                        evidence_refs=[
                            {
                                "source": "source_artifact",
                                "artifact_id": "SRC-does-not-exist",
                                "source_record_id": "chunk-fake",
                                "metadata": {
                                    "chunk_id": "chunk-fake",
                                    "content_hash": "sha256:deadbeef",
                                },
                            }
                        ],
                    )
                ],
                source="test",
                source_ref="guarded_fabricated_source_evidence",
            )

        rel = instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-ACCORD",
            "fits",
        )
        assert rel is None

    def test_relationship_evidence_guard_accepts_source_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _evidence_guard_instance(tmp_path)
        _seed_guarded_fitment_endpoints(instance)
        source_evidence = _fitment_source_evidence(instance)

        result = service_add_relationship_inputs(
            instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-ACCORD",
                    properties={"verified": True},
                    source_evidence=[source_evidence],
                )
            ],
            source="test",
            source_ref="guarded_source_evidence",
        )

        assert result.added == 1
        rel = instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-ACCORD",
            "fits",
        )
        assert rel is not None
        assert rel.metadata.evidence is not None
        evidence_ref = rel.metadata.evidence.evidence_refs[0]
        assert evidence_ref.source == "source_artifact"
        assert evidence_ref.artifact_id == source_evidence["source_artifact_id"]

    def test_relationship_evidence_guard_enforces_min_count(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _evidence_guard_instance(tmp_path, min_source_evidence_count=2)
        _seed_guarded_fitment_endpoints(instance)
        first_evidence = _fitment_source_evidence(instance)
        second_evidence = _fitment_source_evidence(
            instance,
            filename="fitment-review.md",
            text="# Review\n\nA second reviewer confirms BP-1002 fits Accord.\n",
        )

        with pytest.raises(DataValidationError, match="found 1"):
            service_add_relationship_inputs(
                instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-ACCORD",
                        source_evidence=[first_evidence],
                    )
                ],
                source="test",
                source_ref="guarded_one_source_evidence",
            )

        result = service_add_relationship_inputs(
            instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-ACCORD",
                    source_evidence=[first_evidence, second_evidence],
                )
            ],
            source="test",
            source_ref="guarded_two_source_evidence",
        )

        assert result.added == 1

    def test_relationship_evidence_guard_allows_unguarded_relationship_without_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _evidence_guard_instance(tmp_path)
        _seed_guarded_fitment_endpoints(instance)

        result = service_add_relationship_inputs(
            instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="part_selected_for_vehicle",
                    to_type="Vehicle",
                    to_id="V-ACCORD",
                )
            ],
            source="test",
            source_ref="unguarded_decision_relationship",
        )

        assert result.added == 1

    def test_relationship_evidence_guard_dry_run_validates_without_mutating(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _evidence_guard_instance(tmp_path)
        _seed_guarded_fitment_endpoints(instance)
        source_evidence = _fitment_source_evidence(instance)

        with pytest.raises(DataValidationError, match="fits_requires_source_evidence"):
            service_add_relationship_inputs(
                instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-ACCORD",
                    )
                ],
                source="test",
                source_ref="guarded_dry_run_failure",
                dry_run=True,
            )

        result = service_add_relationship_inputs(
            instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-ACCORD",
                    source_evidence=[source_evidence],
                )
            ],
            source="test",
            source_ref="guarded_dry_run_success",
            dry_run=True,
        )

        assert result.added == 1
        assert result.receipt_id is None
        rel = instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-ACCORD",
            "fits",
        )
        assert rel is None

    def test_relationship_evidence_guard_preserves_existing_source_evidence_on_update(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _evidence_guard_instance(tmp_path)
        _seed_guarded_fitment_endpoints(instance)
        source_evidence = _fitment_source_evidence(instance)
        service_add_relationship_inputs(
            instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-ACCORD",
                    source_evidence=[source_evidence],
                )
            ],
            source="test",
            source_ref="initial_source_evidence",
        )

        result = service_add_relationship_inputs(
            instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-ACCORD",
                    properties={"verified": True},
                )
            ],
            source="test",
            source_ref="preserve_source_evidence",
        )

        assert result.updated == 1
        with pytest.raises(DataValidationError, match="fits_requires_source_evidence"):
            service_add_relationship_inputs(
                instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-ACCORD",
                        evidence_refs=[{"source": "catalog", "source_record_id": "replacement"}],
                    )
                ],
                source="test",
                source_ref="reject_bad_replacement_evidence",
            )

        rel = instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-ACCORD",
            "fits",
        )
        assert rel is not None
        assert rel.metadata.evidence is not None
        assert rel.metadata.evidence.evidence_refs[0].source == "source_artifact"

    def test_batch_relationship_evidence_guard_reports_dry_run_errors(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _evidence_guard_instance(tmp_path)
        _seed_guarded_fitment_endpoints(instance)
        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-ACCORD",
                        evidence_rationale="Looks right from context.",
                    )
                ],
            ),
            dry_run=True,
        )

        assert result.valid is False
        assert any("fits_requires_source_evidence" in error for error in result.validation_errors)
        rel = instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-ACCORD",
            "fits",
        )
        assert rel is None

    def test_batch_relationship_evidence_guard_accepts_shared_source_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _evidence_guard_instance(tmp_path)
        _seed_guarded_fitment_endpoints(instance)
        source_evidence = _fitment_source_evidence(instance)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-ACCORD",
                        shared_evidence_keys=["fitment_doc"],
                    )
                ],
                shared_evidence={
                    "fitment_doc": SharedEvidenceInput(
                        source_evidence=[source_evidence],
                    )
                },
            ),
        )

        assert result.valid is True
        assert result.relationships_added == 1
        assert result.evidence_sources_used == ["source_artifact"]

    def test_update_preserves_or_replaces_relationship_evidence(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        target = RelationshipWriteInput(
            from_type="Part",
            from_id="BP-1002",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-ACCORD-SPORT",
            properties={"verified": True},
            evidence_refs=[{"source": "doc", "source_record_id": "first"}],
        )
        service_add_relationship_inputs(
            populated_instance,
            [target],
            source="test",
            source_ref="initial",
        )

        service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type=target.from_type,
                    from_id=target.from_id,
                    relationship_type=target.relationship_type,
                    to_type=target.to_type,
                    to_id=target.to_id,
                    properties={"verified": True, "source": "updated"},
                )
            ],
            source="test",
            source_ref="preserve",
        )
        rel = populated_instance.load_graph().get_relationship(
            target.from_type,
            target.from_id,
            target.to_type,
            target.to_id,
            target.relationship_type,
        )
        assert rel is not None
        assert rel.metadata.evidence is not None
        assert rel.metadata.evidence.evidence_refs[0].source_record_id == "first"

        service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type=target.from_type,
                    from_id=target.from_id,
                    relationship_type=target.relationship_type,
                    to_type=target.to_type,
                    to_id=target.to_id,
                    properties={"verified": True, "source": "replaced"},
                    evidence_refs=[{"source": "doc", "source_record_id": "second"}],
                    evidence_rationale="Replacement evidence.",
                )
            ],
            source="test",
            source_ref="replace",
        )
        rel = populated_instance.load_graph().get_relationship(
            target.from_type,
            target.from_id,
            target.to_type,
            target.to_id,
            target.relationship_type,
        )
        assert rel is not None
        assert rel.metadata.evidence is not None
        assert rel.metadata.evidence.evidence_refs[0].source_record_id == "second"
        assert rel.metadata.evidence.rationale == "Replacement evidence."

    def test_missing_source_evidence_does_not_commit_relationship(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(ConfigError, match="Source artifact 'SRC-missing' not found"):
            service_add_relationship_inputs(
                populated_instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-ACCORD-SPORT",
                        properties={"verified": True},
                        source_evidence=[
                            {
                                "source_artifact_id": "SRC-missing",
                                "chunk_id": "chunk-missing",
                            }
                        ],
                    )
                ],
                source="test",
                source_ref="missing_source",
            )

        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is None

    def test_malformed_evidence_ref_does_not_commit_relationship(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(DataValidationError, match="Invalid evidence_ref"):
            service_add_relationship_inputs(
                populated_instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-ACCORD-SPORT",
                        properties={"verified": True},
                        evidence_refs=[{"source": "roadmap_doc"}],
                    )
                ],
                source="test",
                source_ref="malformed_evidence",
            )

        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is None

    def test_malformed_source_evidence_does_not_commit_relationship(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(DataValidationError, match="Invalid source_evidence"):
            service_add_relationship_inputs(
                populated_instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-ACCORD-SPORT",
                        properties={"verified": True},
                        source_evidence=[{"source_artifact_id": "SRC-1"}],
                    )
                ],
                source="test",
                source_ref="malformed_source_evidence",
            )

        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is None

    def test_batch(self, populated_instance: CruxibleInstance) -> None:
        result = service_add_relationships(
            populated_instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                ),
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1001",
                    relationship="replaces",
                    to_type="Part",
                    to_id="BP-1002",
                    properties={"direction": "downgrade", "confidence": 0.8},
                ),
            ],
            source="test",
            source_ref="test_batch",
        )
        assert result.added == 2
        assert result.updated == 0

    def test_dedup_error(self, populated_instance: CruxibleInstance) -> None:
        edges = [
            RelationshipInstance(
                from_type="Part",
                from_id="BP-1002",
                relationship="fits",
                to_type="Vehicle",
                to_id="V-2024-ACCORD-SPORT",
            ),
            RelationshipInstance(
                from_type="Part",
                from_id="BP-1002",
                relationship="fits",
                to_type="Vehicle",
                to_id="V-2024-ACCORD-SPORT",
            ),
        ]
        with pytest.raises(DataValidationError, match="duplicate in batch"):
            service_add_relationships(populated_instance, edges, source="test", source_ref="test")

    def test_source_provenance(self, populated_instance: CruxibleInstance) -> None:
        service_add_relationships(
            populated_instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                )
            ],
            source="agent_review",
            source_ref="review-123",
        )
        graph = populated_instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1002", "Vehicle", "V-2024-ACCORD-SPORT", "fits")
        assert rel is not None
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.source == "agent_review"
        assert prov.source_ref == "review-123"

    def test_incremental_relationship_mutation_does_not_full_save(
        self, populated_instance: CruxibleInstance
    ) -> None:
        def fail_full_save(_self: SQLiteGraphRepository, _graph) -> None:
            raise AssertionError("service_add_relationships should not replace the full graph")

        with patch.object(SQLiteGraphRepository, "save_graph", fail_full_save):
            result = service_add_relationships(
                populated_instance,
                [
                    RelationshipInstance(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship="fits",
                        to_type="Vehicle",
                        to_id="V-2024-ACCORD-SPORT",
                        properties={"verified": True},
                    )
                ],
                source="test",
                source_ref="incremental",
            )

        assert result.added == 1
        restarted = CruxibleInstance.load(populated_instance.root)
        assert (
            restarted.load_graph().get_relationship(
                "Part",
                "BP-1002",
                "Vehicle",
                "V-2024-ACCORD-SPORT",
                "fits",
            )
            is not None
        )
