"""Smoke tests for the agent-operation kit scratchpad surface + Decision guard.

Proves the shipped kit config:

* keeps ``kind=scratchpad`` StateNotes out of the curated note reads
  (``recent_state_notes``, ``state_notes_for_work_item``,
  ``state_notes_for_review_request``, and the bounded note/include sets of the
  context queries), while the new ``work_item_scratchpad`` query returns exactly
  a work item's scratchpad notes for mid-flight pickup;
* rejects any direct write resulting in ``Decision.status=accepted`` — creating
  a decision as accepted included — unless the authenticated actor is the
  ``authorized-reviewer`` placeholder (the same generic actor the review
  approval guard uses). Proposed/rejected/deferred statuses stay unguarded.

Known language limits (asserted nowhere, documented here): the all_adjacent
context queries (``work_item_context``, ``state_note_context``,
``subject_operation_context``) cannot filter their fan-out PATH rows per
relationship, so scratchpad notes still appear as raw context rows there; their
bounded include sets ARE filtered, which these tests pin.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.query.engine import execute_query
from cruxible_core.query.types import dump_query_row
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    service_add_entity_inputs,
    service_batch_direct_write,
)
from cruxible_core.temporal import utc_now

KIT_CONFIG = Path(__file__).resolve().parents[2] / "kits" / "agent-operation" / "config.yaml"

AUTHORIZED_REVIEWER = "authorized-reviewer"


def _agent_operation_instance(tmp_path: Path) -> CruxibleInstance:
    shutil.copy(KIT_CONFIG, tmp_path / "config.yaml")
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _actor_context(actor_id: str) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org_1",
        operation_id=f"op_{actor_id}",
        timestamp=utc_now(),
    )


def _note(note_id: str, kind: str) -> EntityWriteInput:
    return EntityWriteInput(
        entity_type="StateNote",
        entity_id=note_id,
        properties={
            "note_id": note_id,
            "kind": kind,
            "title": f"{kind} {note_id}",
            "summary": "s",
            "body": "b",
            "created_at": utc_now(),
        },
    )


def _note_about(note_id: str, relationship: str, to_type: str, to_id: str):
    return BatchRelationshipWriteInput(
        from_type="StateNote",
        from_id=note_id,
        relationship_type=relationship,
        to_type=to_type,
        to_id=to_id,
    )


def _decision(decision_id: str, status: str) -> EntityWriteInput:
    return EntityWriteInput(
        entity_type="Decision",
        entity_id=decision_id,
        properties={"decision_id": decision_id, "title": "A decision", "status": status},
    )


# ── Decision acceptance guard ─────────────────────────────────────────


class TestDecisionAcceptanceGuard:
    def test_proposed_decision_writable_without_actor(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        service_add_entity_inputs(instance, [_decision("d-1", "proposed")])

    def test_transition_to_accepted_requires_authorized_actor(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        service_add_entity_inputs(instance, [_decision("d-1", "proposed")])
        with pytest.raises(DataValidationError, match="decision_acceptance_requires"):
            service_add_entity_inputs(
                instance,
                [_decision("d-1", "accepted")],
                actor_context=_actor_context("some-writer"),
            )

    def test_transition_to_accepted_rejected_without_actor_context(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        service_add_entity_inputs(instance, [_decision("d-1", "proposed")])
        with pytest.raises(DataValidationError, match="decision_acceptance_requires"):
            service_add_entity_inputs(instance, [_decision("d-1", "accepted")])

    def test_authorized_reviewer_can_accept(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        service_add_entity_inputs(instance, [_decision("d-1", "proposed")])
        service_add_entity_inputs(
            instance,
            [_decision("d-1", "accepted")],
            actor_context=_actor_context(AUTHORIZED_REVIEWER),
        )
        stored = instance.load_graph().get_entity("Decision", "d-1")
        assert stored is not None
        assert stored.properties["status"] == "accepted"

    def test_create_with_accepted_value_is_guarded(self, tmp_path: Path) -> None:
        """Guards fire on create-with-value, not only on transitions."""
        instance = _agent_operation_instance(tmp_path)
        with pytest.raises(DataValidationError, match="decision_acceptance_requires"):
            service_add_entity_inputs(
                instance,
                [_decision("d-new", "accepted")],
                actor_context=_actor_context("some-writer"),
            )
        service_add_entity_inputs(
            instance,
            [_decision("d-new", "accepted")],
            actor_context=_actor_context(AUTHORIZED_REVIEWER),
        )

    @pytest.mark.parametrize("status", ["rejected", "deferred"])
    def test_other_verdict_statuses_stay_unguarded(self, tmp_path: Path, status: str) -> None:
        instance = _agent_operation_instance(tmp_path)
        service_add_entity_inputs(instance, [_decision("d-1", "proposed")])
        service_add_entity_inputs(instance, [_decision("d-1", status)])


# ── Scratchpad note reads ─────────────────────────────────────────────


@pytest.fixture
def seeded_instance(tmp_path: Path) -> CruxibleInstance:
    """Kit instance with one work item carrying curated + scratchpad notes."""
    instance = _agent_operation_instance(tmp_path)
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-1",
                    properties={
                        "work_item_id": "wi-1",
                        "title": "Work",
                        "type": "feature",
                        "status": "active",
                        "priority": "high",
                    },
                ),
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-1",
                    properties={
                        "review_request_id": "rr-1",
                        "title": "Review",
                        "status": "requested",
                    },
                ),
                _note("sn-curated", "implementation_note"),
                _note("sn-review", "review_note"),
                _note("sn-pad-1", "scratchpad"),
                _note("sn-pad-2", "scratchpad"),
            ],
            relationships=[
                _note_about("sn-curated", "state_note_about_work_item", "WorkItem", "wi-1"),
                _note_about("sn-pad-1", "state_note_about_work_item", "WorkItem", "wi-1"),
                _note_about("sn-pad-2", "state_note_about_work_item", "WorkItem", "wi-1"),
                _note_about(
                    "sn-review", "state_note_about_review_request", "ReviewRequest", "rr-1"
                ),
                _note_about("sn-pad-1", "state_note_about_review_request", "ReviewRequest", "rr-1"),
            ],
        ),
    )
    return instance


def _result_note_ids(instance: CruxibleInstance, query: str, params: dict) -> set[str]:
    """Note ids of the primary results of a traversal query."""
    result = execute_query(instance.load_config(), instance.load_graph(), query, params)
    ids: set[str] = set()
    for row in result.results:
        data = dump_query_row(row)
        ids.add(data["result"]["entity_id"])
    return ids


class TestScratchpadReads:
    def test_work_item_scratchpad_returns_only_scratchpad_notes(
        self, seeded_instance: CruxibleInstance
    ) -> None:
        ids = _result_note_ids(seeded_instance, "work_item_scratchpad", {"work_item_id": "wi-1"})
        assert ids == {"sn-pad-1", "sn-pad-2"}

    def test_work_item_scratchpad_replays_in_created_order(
        self, seeded_instance: CruxibleInstance
    ) -> None:
        result = execute_query(
            seeded_instance.load_config(),
            seeded_instance.load_graph(),
            "work_item_scratchpad",
            {"work_item_id": "wi-1"},
        )
        ordered = [dump_query_row(row)["result"]["entity_id"] for row in result.results]
        assert ordered == ["sn-pad-1", "sn-pad-2"]

    def test_state_notes_for_work_item_excludes_scratchpad(
        self, seeded_instance: CruxibleInstance
    ) -> None:
        ids = _result_note_ids(
            seeded_instance, "state_notes_for_work_item", {"work_item_id": "wi-1"}
        )
        assert ids == {"sn-curated"}

    def test_state_notes_for_review_request_excludes_scratchpad(
        self, seeded_instance: CruxibleInstance
    ) -> None:
        ids = _result_note_ids(
            seeded_instance,
            "state_notes_for_review_request",
            {"review_request_id": "rr-1"},
        )
        assert ids == {"sn-review"}

    def test_recent_state_notes_excludes_scratchpad(
        self, seeded_instance: CruxibleInstance
    ) -> None:
        result = execute_query(
            seeded_instance.load_config(),
            seeded_instance.load_graph(),
            "recent_state_notes",
            {},
        )
        ids = {dump_query_row(row)["values"]["note_id"] for row in result.results}
        assert ids == {"sn-curated", "sn-review"}

    def test_work_item_context_bounded_note_set_excludes_scratchpad(
        self, seeded_instance: CruxibleInstance
    ) -> None:
        """The bounded include set is filtered; raw fan-out path rows are not
        (the compact grammar has no per-relationship path filter there)."""
        result = execute_query(
            seeded_instance.load_config(),
            seeded_instance.load_graph(),
            "work_item_context",
            {"work_item_id": "wi-1"},
        )
        assert result.results, "work_item_context returned no rows"
        # work_item_context projects a select, so the include set lives on the
        # preserved source row rather than the dumped values.
        row = result.results[0]
        source = getattr(row, "source", row)
        include = source.includes["state_note_about_work_item"]
        note_ids = {item.source.entity_id for item in include.items}
        assert note_ids == {"sn-curated"}

    def test_state_note_context_incoming_supersession_excludes_scratchpad(
        self, seeded_instance: CruxibleInstance
    ) -> None:
        service_batch_direct_write(
            seeded_instance,
            BatchDirectWriteInput(
                entities=[_note("sn-pad-super", "scratchpad"), _note("sn-fix", "correction")],
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="StateNote",
                        from_id=from_id,
                        relationship_type="state_note_supersedes_state_note",
                        to_type="StateNote",
                        to_id="sn-curated",
                        properties={"supersession_basis": "basis"},
                    )
                    for from_id in ("sn-pad-super", "sn-fix")
                ],
            ),
        )
        result = execute_query(
            seeded_instance.load_config(),
            seeded_instance.load_graph(),
            "state_note_context",
            {"note_id": "sn-curated"},
        )
        assert result.results, "state_note_context returned no rows"
        row = dump_query_row(result.results[0])
        items = row["includes"]["state_note_supersedes_state_note_in"]["items"]
        superseding_ids = {item["source"]["entity_id"] for item in items}
        assert superseding_ids == {"sn-fix"}
