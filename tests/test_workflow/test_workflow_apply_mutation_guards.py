"""Regression tests for the two CRITICAL canonical-write audit findings.

G1 — workflow ``apply_entities`` must enforce config mutation guards. Before the
fix the canonical apply path called ``graph.add_entity`` /
``graph.update_entity_properties`` directly after schema/ownership validation,
bypassing the proposed-graph guard evaluation the direct-write path runs. A
canonical workflow could therefore close a ``WorkItem`` (set ``status=closed``)
without an approved ``ReviewRequest``, defeating the review gate on a
GRAPH_WRITE path.

G2 — a canonical preview/dry-run must not mutate the live cached graph. The
executor clones the live graph for canonical previews via ``_clone_graph``, but
that clone shared the nested ``properties`` dicts with the live cache, so an
in-place ``update_entity_properties`` during preview silently mutated live
state.

These tests build a real instance from the shipped agent-operation kit config
(real guard + named query), append a canonical close-work-item workflow, and
exercise ``execute_workflow`` in preview and apply modes.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from tests.support.workflow_helpers import write_lock_for_instance

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import QueryExecutionError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    service_add_entity_inputs,
    service_batch_direct_write,
)
from cruxible_core.temporal import utc_now
from cruxible_core.workflow.executor import execute_workflow

KIT_CONFIG = Path(__file__).resolve().parents[2] / "kits" / "agent-operation" / "config.yaml"

# A canonical close-work-item workflow appended to the agent-operation kit config.
# make_entities builds the WorkItem upsert (status from input); apply_entities
# commits it on the cloned canonical graph, which is exactly the path the G1 fix
# wires the mutation guard into.
_CLOSE_WORK_ITEM_WORKFLOW = dedent(
    """
    contracts:
      CloseWorkItemInput:
        fields:
          work_item_id:
            type: string
          status:
            type: string

    workflows:
      close_work_item:
        type: canonical
        contract_in: CloseWorkItemInput
        steps:
          - id: work_items
            make_entities:
              entity_type: WorkItem
              items:
                - work_item_id: $input.work_item_id
                  status: $input.status
              entity_id: $item.work_item_id
              properties:
                status: $item.status
            as: work_items
          - id: apply_work_items
            apply_entities:
              entities_from: work_items
            as: apply_work_items
        returns: apply_work_items
    """
)


def _instance_with_close_workflow(tmp_path: Path) -> CruxibleInstance:
    config_text = KIT_CONFIG.read_text() + "\n" + _CLOSE_WORK_ITEM_WORKFLOW
    (tmp_path / "config.yaml").write_text(config_text)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    write_lock_for_instance(instance)
    return instance


_IMPLEMENTER = "implementer"


def _actor_context(actor_id: str = "authorized-reviewer") -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org_1",
        operation_id=f"op_{actor_id}",
        timestamp=utc_now(),
    )


def _seed_work_item(instance: CruxibleInstance, status: str = "active") -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="WorkItem",
                entity_id="wi-gated",
                properties={
                    "work_item_id": "wi-gated",
                    "title": "Gated work item",
                    "type": "feature",
                    "status": status,
                    "priority": "high",
                },
            )
        ],
    )


def _seed_approved_review(instance: CruxibleInstance) -> None:
    """Add an approved ReviewRequest linked to wi-gated across two governed writes.

    agent-operation gates approval with three guards stricter than the legacy
    project-state kit: the approving actor must be ``authorized-reviewer``
    (``review_request_approval_requires_authorized_actor``); the verdict must
    co-write a ``StateNote(kind=review_note)`` linked via
    ``state_note_about_review_request`` in the same write
    (``review_verdict_requires_rationale_note``); and the approver must differ
    from the actor recorded in the ReviewRequest's creation receipt
    (``distinct_from_creation_actor``), which makes create-with-approved
    impossible. So the review is seeded in two steps: created ``requested`` by
    the implementer actor, then approved by the ``authorized-reviewer`` actor.
    """
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-gated",
                    properties={
                        "review_request_id": "rr-gated",
                        "title": "Review gated work item",
                        "status": "requested",
                    },
                ),
            ],
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="ReviewRequest",
                    from_id="rr-gated",
                    relationship_type="review_request_for_work_item",
                    to_type="WorkItem",
                    to_id="wi-gated",
                ),
            ],
        ),
        actor_context=_actor_context(_IMPLEMENTER),
    )
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-gated",
                    properties={"status": "approved"},
                ),
                EntityWriteInput(
                    entity_type="StateNote",
                    entity_id="sn-gated",
                    properties={
                        "note_id": "sn-gated",
                        "kind": "review_note",
                        "title": "Approval rationale",
                        "summary": "Approved after review.",
                        "body": "Gated work item reviewed and approved.",
                        "created_at": utc_now(),
                    },
                ),
            ],
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="StateNote",
                    from_id="sn-gated",
                    relationship_type="state_note_about_review_request",
                    to_type="ReviewRequest",
                    to_id="rr-gated",
                ),
            ],
        ),
        actor_context=_actor_context(),
    )


def _work_item_status(instance: CruxibleInstance) -> str:
    entity = instance.load_graph().get_entity("WorkItem", "wi-gated")
    assert entity is not None
    return entity.properties["status"]


class TestWorkflowApplyMutationGuards:
    """G1: workflow apply_entities enforces the same mutation guards as direct writes."""

    def test_canonical_preview_close_rejected_without_review(self, tmp_path: Path) -> None:
        instance = _instance_with_close_workflow(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(
            QueryExecutionError,
            match="work_item_closed_requires_approved_review",
        ):
            execute_workflow(
                instance,
                instance.load_config(),
                "close_work_item",
                {"work_item_id": "wi-gated", "status": "closed"},
                mode="preview",
            )

        assert _work_item_status(instance) == "active"

    def test_canonical_apply_close_rejected_without_review(self, tmp_path: Path) -> None:
        instance = _instance_with_close_workflow(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(
            QueryExecutionError,
            match="work_item_closed_requires_approved_review",
        ):
            execute_workflow(
                instance,
                instance.load_config(),
                "close_work_item",
                {"work_item_id": "wi-gated", "status": "closed"},
                mode="apply",
            )

        assert _work_item_status(instance) == "active"

    def test_canonical_apply_close_allowed_with_approved_review(self, tmp_path: Path) -> None:
        instance = _instance_with_close_workflow(tmp_path)
        _seed_work_item(instance)
        _seed_approved_review(instance)

        preview = execute_workflow(
            instance,
            instance.load_config(),
            "close_work_item",
            {"work_item_id": "wi-gated", "status": "closed"},
            mode="preview",
        )
        assert preview.mode == "preview"
        # Preview must leave live state untouched (overlaps G2).
        assert _work_item_status(instance) == "active"

        applied = execute_workflow(
            instance,
            instance.load_config(),
            "close_work_item",
            {"work_item_id": "wi-gated", "status": "closed"},
            mode="apply",
        )
        assert applied.mode == "apply"
        assert applied.committed_snapshot_id is not None
        assert _work_item_status(instance) == "closed"

    def test_canonical_apply_noop_close_allowed_when_already_closed(self, tmp_path: Path) -> None:
        # A re-apply that does not change status is a noop and must not trip the
        # guard (the guard only fires on the closed *transition*).
        instance = _instance_with_close_workflow(tmp_path)
        _seed_work_item(instance, status="active")
        _seed_approved_review(instance)
        execute_workflow(
            instance,
            instance.load_config(),
            "close_work_item",
            {"work_item_id": "wi-gated", "status": "closed"},
            mode="apply",
        )
        assert _work_item_status(instance) == "closed"

        # Closing an already-closed item is old_value == new_value -> no guard
        # context -> allowed.
        applied = execute_workflow(
            instance,
            instance.load_config(),
            "close_work_item",
            {"work_item_id": "wi-gated", "status": "closed"},
            mode="apply",
        )
        assert applied.mode == "apply"
        assert _work_item_status(instance) == "closed"


class TestCanonicalPreviewDoesNotMutateLiveCache:
    """G2: a canonical preview/dry-run leaves the live cached graph byte-identical."""

    def test_rejected_preview_leaves_live_graph_byte_identical(self, tmp_path: Path) -> None:
        instance = _instance_with_close_workflow(tmp_path)
        _seed_work_item(instance, status="active")

        live_before = instance.load_graph().to_dict()

        # This preview attempts a guarded close and is rejected; even on the
        # allowed path, preview must not touch live state.
        with pytest.raises(QueryExecutionError):
            execute_workflow(
                instance,
                instance.load_config(),
                "close_work_item",
                {"work_item_id": "wi-gated", "status": "closed"},
                mode="preview",
            )

        live_after = instance.load_graph().to_dict()
        assert live_after == live_before
        assert _work_item_status(instance) == "active"

    def test_allowed_preview_does_not_persist_and_does_not_mutate_cache(
        self, tmp_path: Path
    ) -> None:
        instance = _instance_with_close_workflow(tmp_path)
        _seed_work_item(instance, status="active")
        _seed_approved_review(instance)

        live_graph = instance.load_graph()
        wi_props_before = dict(live_graph.get_entity("WorkItem", "wi-gated").properties)
        live_before = live_graph.to_dict()

        result = execute_workflow(
            instance,
            instance.load_config(),
            "close_work_item",
            {"work_item_id": "wi-gated", "status": "closed"},
            mode="preview",
        )
        assert result.mode == "preview"
        assert result.committed_snapshot_id is None

        # The live cached graph object must be untouched: same status, and the
        # full serialized image byte-identical (proves nested properties dicts
        # were not shared with the preview clone).
        live_after = instance.load_graph()
        assert live_after.get_entity("WorkItem", "wi-gated").properties == wi_props_before
        assert live_after.get_entity("WorkItem", "wi-gated").properties["status"] == "active"
        assert live_after.to_dict() == live_before

        # And a fresh load from storage confirms nothing was persisted.
        reloaded = CruxibleInstance.load(instance.root)
        assert reloaded.load_graph().get_entity("WorkItem", "wi-gated").properties["status"] == (
            "active"
        )
