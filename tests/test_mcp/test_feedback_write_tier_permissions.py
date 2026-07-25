"""Adversarial write-tier tests for the FEEDBACK write channels.

Companion to ``test_write_tier_permissions.py`` (direct-write channels).
Covered channels:

* direct relationship writes (baseline, unchanged),
* ``feedback`` correct / approve / reject / flag,
* ``feedback_batch`` (all-or-nothing, gated at its strictest action),
* ``feedback_from_query`` (target selected from a query receipt).

The rail these exercise is wi-feedback-approval-rail: the adjudication actions
(``approve`` / ``reject`` / ``correct``) sit at a GRAPH_WRITE floor, because
attributing WHO promoted an edge never established that they were entitled to.
That floor is a property of the ACTION, so it wins over the per-type
``write_tier`` opt-out this fixture declares: a type owner may lower who can
direct-write their type, but not who can adjudicate claims on it. ``flag`` is
the one action left at the governed floor (it ASKS for review). The
resolved-actor identity requirement is unchanged and additive, and is tested at
the end.

It supersedes wi-feedback-write-tier-bypass, whose facade pre-gate
(``_feedback_correction_tier_gate``) checked corrections against the corrected
type's declared ``write_tier``. An action-level GRAPH_WRITE floor is at least
as tight as any declarable tier, so the pre-gate could never bind — and its
denials landed UNRECEIPTED, ahead of the chokepoint. It is gone; each of the
three channels below asserts that its refusal now carries a PERSISTED mutation
receipt (``_assert_refusal_is_receipted``).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import AuthenticationError, PermissionDeniedError
from cruxible_core.mcp import contracts
from cruxible_core.mcp.permissions import (
    PermissionMode,
    init_permissions,
    request_permission_scope,
)
from cruxible_core.runtime import api
from cruxible_core.runtime.instance_manager import get_manager

# note_about_task declares the governed_write surface; task_blocks_task keeps
# the default graph_write requirement. Both carry a schema property so a
# ``correct`` has something real to mutate.
FEEDBACK_TIER_YAML = dedent(
    """
    version: "1.0"
    name: feedback_write_tier_kit

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
        properties:
          confidence: float
      - task_blocks_task: Task -> Task
        properties:
          severity: string

    named_queries:
      blocking_edges:
        explicit: true
        mode: traversal
        entry_point: Task
        traversal:
          - relationship: task_blocks_task
            direction: incoming
        returns: task_blocks_task
        result_shape: relationship
      note_edges:
        explicit: true
        mode: traversal
        entry_point: Note
        traversal:
          - relationship: note_about_task
            direction: outgoing
        returns: note_about_task
        result_shape: relationship
    """
)


@pytest.fixture
def feedback_tier_instance_id(tmp_path: Path) -> str:
    (tmp_path / "config.yaml").write_text(FEEDBACK_TIER_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    instance_id = str(tmp_path)
    get_manager().register(instance_id, instance)
    init_permissions(PermissionMode.ADMIN)
    # Seed both edges as a full-tier actor.
    api.add_entities(
        instance_id,
        [
            _entity("Task", "t-1"),
            _entity("Task", "t-2"),
            _entity("Note", "n-1"),
        ],
    )
    api.add_relationships(
        instance_id,
        [
            contracts.RelationshipInput(
                from_type="Task",
                from_id="t-1",
                relationship_type="task_blocks_task",
                to_type="Task",
                to_id="t-2",
                properties={"severity": "high"},
            ),
            contracts.RelationshipInput(
                from_type="Note",
                from_id="n-1",
                relationship_type="note_about_task",
                to_type="Task",
                to_id="t-1",
                properties={"confidence": 0.5},
            ),
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


def _feedback(instance_id: str, action: str, **overrides):
    kwargs = {
        "instance_id": instance_id,
        "action": action,
        "source": "human",
        "from_type": "Task",
        "from_id": "t-1",
        "relationship_type": "task_blocks_task",
        "to_type": "Task",
        "to_id": "t-2",
        "reason": "adversarial tier test",
    }
    kwargs.update(overrides)
    return api.feedback(**kwargs)


def _blocks_edge_severity(instance_id: str) -> str:
    with request_permission_scope(PermissionMode.ADMIN):
        edge = api.get_relationship(
            instance_id,
            from_type="Task",
            from_id="t-1",
            relationship_type="task_blocks_task",
            to_type="Task",
            to_id="t-2",
        )
    return edge.properties["severity"]


def _note_edge_confidence(instance_id: str) -> float:
    with request_permission_scope(PermissionMode.ADMIN):
        edge = api.get_relationship(
            instance_id,
            from_type="Note",
            from_id="n-1",
            relationship_type="note_about_task",
            to_type="Task",
            to_id="t-1",
        )
    return edge.properties["confidence"]


def _assert_refusal_is_receipted(instance_id: str, error: PermissionDeniedError) -> None:
    """The refusal came from the RECEIPTED service chokepoint, not a pre-gate.

    The facade used to pre-gate corrections (``_feedback_correction_tier_gate``)
    and its denials landed before any receipt existed. The rail now lives wholly
    inside the service's ``mutation_receipt`` scope, so every refusal is
    persisted and auditable — assert the id is not just set on the exception but
    actually readable back out of the receipt store.
    """
    assert error.mutation_receipt_id is not None
    with request_permission_scope(PermissionMode.ADMIN):
        stored = api.receipt(instance_id, error.mutation_receipt_id)
    assert stored["receipt_id"] == error.mutation_receipt_id
    assert stored["operation_type"] in ("feedback", "feedback_batch")


def _batch_item(
    receipt_id: str, *, on_note_edge: bool, corrections: dict
) -> contracts.FeedbackBatchItemInput:
    if on_note_edge:
        target = contracts.EdgeTargetInput(
            from_type="Note",
            from_id="n-1",
            relationship_type="note_about_task",
            to_type="Task",
            to_id="t-1",
        )
    else:
        target = contracts.EdgeTargetInput(
            from_type="Task",
            from_id="t-1",
            relationship_type="task_blocks_task",
            to_type="Task",
            to_id="t-2",
        )
    return contracts.FeedbackBatchItemInput(
        receipt_id=receipt_id,
        action="correct",
        target=target,
        reason="adversarial tier test",
        corrections=corrections,
    )


class TestDirectWriteBaseline:
    def test_governed_direct_write_of_graph_write_edge_denied(self, feedback_tier_instance_id):
        edge = contracts.RelationshipInput(
            from_type="Task",
            from_id="t-2",
            relationship_type="task_blocks_task",
            to_type="Task",
            to_id="t-1",
        )
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.add_relationships(feedback_tier_instance_id, [edge])


class TestFeedbackCorrectTierGate:
    def test_governed_correct_on_graph_write_edge_denied(self, feedback_tier_instance_id):
        """The core hole: governed feedback ``correct`` may not mutate a
        graph_write-tier edge's properties.

        Single-payload path of the receipted-chokepoint guarantee.
        """
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE") as exc:
                _feedback(
                    feedback_tier_instance_id,
                    "correct",
                    corrections={"severity": "low"},
                )
        # Refused with the write transaction open, then rolled back: the edge
        # property is untouched and the refusal itself is on the record.
        assert _blocks_edge_severity(feedback_tier_instance_id) == "high"
        _assert_refusal_is_receipted(feedback_tier_instance_id, exc.value)

    def test_correct_on_governed_write_edge_needs_graph_write(self, feedback_tier_instance_id):
        """A per-type ``write_tier`` opt-out does NOT open ``correct``.

        ``note_about_task`` declares ``write_tier: governed_write``, so a
        GOVERNED_WRITE actor may direct-write it. ``correct`` is still refused:
        the adjudication floor is a property of the ACTION, and a type owner may
        lower who direct-writes their type but not who adjudicates claims on it
        (wi-feedback-approval-rail, superseding the earlier allowance).
        """
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                _feedback(
                    feedback_tier_instance_id,
                    "correct",
                    from_type="Note",
                    from_id="n-1",
                    relationship_type="note_about_task",
                    to_type="Task",
                    to_id="t-1",
                    corrections={"confidence": 0.9},
                )
        assert _note_edge_confidence(feedback_tier_instance_id) != 0.9

        with request_permission_scope(PermissionMode.GRAPH_WRITE):
            result = _feedback(
                feedback_tier_instance_id,
                "correct",
                from_type="Note",
                from_id="n-1",
                relationship_type="note_about_task",
                to_type="Task",
                to_id="t-1",
                corrections={"confidence": 0.9},
            )
        assert result.applied is True

    def test_correct_without_corrections_still_needs_graph_write(self, feedback_tier_instance_id):
        """Empty-corrections ``correct`` mutates no schema property, but it is
        the approve-equivalent review transition — so the adjudication floor,
        not the property-tier gate, is what governs it."""
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                _feedback(feedback_tier_instance_id, "correct")
        assert _blocks_edge_severity(feedback_tier_instance_id) == "high"

    @pytest.mark.parametrize("action", ["approve", "reject"])
    def test_governed_adjudication_transitions_refused(self, feedback_tier_instance_id, action):
        """approve/reject adjudicate: they make an edge live or retract it, so
        they require the same tier as a direct write or a group resolution."""
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                _feedback(feedback_tier_instance_id, action)

    @pytest.mark.parametrize("action", ["approve", "reject"])
    def test_graph_write_adjudication_transitions_allowed(self, feedback_tier_instance_id, action):
        with request_permission_scope(PermissionMode.GRAPH_WRITE):
            result = _feedback(feedback_tier_instance_id, action)
        assert result.applied is True

    def test_governed_flag_still_allowed(self, feedback_tier_instance_id):
        """``flag`` moves an edge to pending — it ASKS for review rather than
        granting it, so it stays at the governed-operator floor."""
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            result = _feedback(feedback_tier_instance_id, "flag")
        assert result.applied is True

    def test_graph_write_correct_unaffected(self, feedback_tier_instance_id):
        with request_permission_scope(PermissionMode.GRAPH_WRITE):
            result = _feedback(
                feedback_tier_instance_id,
                "correct",
                corrections={"severity": "medium"},
            )
        assert result.applied is True
        assert _blocks_edge_severity(feedback_tier_instance_id) == "medium"

    def test_read_only_denied_at_static_floor(self, feedback_tier_instance_id):
        with request_permission_scope(PermissionMode.READ_ONLY):
            with pytest.raises(PermissionDeniedError, match="GOVERNED_WRITE"):
                _feedback(feedback_tier_instance_id, "correct", corrections={"severity": "low"})


class TestFeedbackBatchTierGate:
    def _query_receipt(self, instance_id: str, query_name: str, params: dict) -> str:
        with request_permission_scope(PermissionMode.ADMIN):
            result = api.query(instance_id, query_name, params)
        assert result.receipt_id is not None
        return result.receipt_id

    def test_mixed_batch_gated_at_strictest_corrected_type(self, feedback_tier_instance_id):
        """One governed-tier correction plus one graph_write-tier correction:
        the whole batch is refused and nothing is applied.

        Batch path of the receipted-chokepoint guarantee.
        """
        note_receipt = self._query_receipt(
            feedback_tier_instance_id, "note_edges", {"note_id": "n-1"}
        )
        blocks_receipt = self._query_receipt(
            feedback_tier_instance_id, "blocking_edges", {"task_id": "t-2"}
        )
        confidence_before = _note_edge_confidence(feedback_tier_instance_id)
        items = [
            _batch_item(note_receipt, on_note_edge=True, corrections={"confidence": 0.7}),
            _batch_item(blocks_receipt, on_note_edge=False, corrections={"severity": "low"}),
        ]
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE") as exc:
                api.feedback_batch(feedback_tier_instance_id, items, source="human")
        # All-or-nothing: neither the graph_write edge nor the governed-tier
        # edge (which a per-item gate would have let through) may be touched.
        assert _blocks_edge_severity(feedback_tier_instance_id) == "high"
        assert _note_edge_confidence(feedback_tier_instance_id) == confidence_before
        assert _note_edge_confidence(feedback_tier_instance_id) != 0.7
        _assert_refusal_is_receipted(feedback_tier_instance_id, exc.value)

    def test_batch_of_governed_tier_corrections_needs_graph_write(self, feedback_tier_instance_id):
        """Even an all-governed_write-tier batch is refused: every item is a
        ``correct``, and adjudication is gated by action, not by type tier."""
        note_receipt = self._query_receipt(
            feedback_tier_instance_id, "note_edges", {"note_id": "n-1"}
        )
        items = [_batch_item(note_receipt, on_note_edge=True, corrections={"confidence": 0.8})]
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.feedback_batch(feedback_tier_instance_id, items, source="human")
        assert _note_edge_confidence(feedback_tier_instance_id) != 0.8

        with request_permission_scope(PermissionMode.GRAPH_WRITE):
            result = api.feedback_batch(feedback_tier_instance_id, items, source="human")
        assert result.applied_count == 1


class TestFeedbackFromQueryTierGate:
    def _query_receipt(self, instance_id: str, query_name: str, params: dict) -> str:
        with request_permission_scope(PermissionMode.ADMIN):
            result = api.query(instance_id, query_name, params)
        assert result.receipt_id is not None
        return result.receipt_id

    def test_governed_correct_on_graph_write_edge_denied(self, feedback_tier_instance_id):
        """From-query path of the receipted-chokepoint guarantee.

        The target is chosen by receipt coordinates rather than named outright,
        so this is the path where the deleted facade pre-gate had to
        pre-resolve the edge just to learn its type. It resolves once now, in
        the service chokepoint, and the refusal is receipted like the others.
        """
        receipt_id = self._query_receipt(
            feedback_tier_instance_id, "blocking_edges", {"task_id": "t-2"}
        )
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE") as exc:
                api.feedback_from_query(
                    feedback_tier_instance_id,
                    receipt_id=receipt_id,
                    result_index=0,
                    action="correct",
                    corrections={"severity": "low"},
                    reason="adversarial tier test",
                )
        assert _blocks_edge_severity(feedback_tier_instance_id) == "high"
        _assert_refusal_is_receipted(feedback_tier_instance_id, exc.value)

    def test_correct_on_governed_write_edge_needs_graph_write(self, feedback_tier_instance_id):
        receipt_id = self._query_receipt(
            feedback_tier_instance_id, "note_edges", {"note_id": "n-1"}
        )
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.feedback_from_query(
                    feedback_tier_instance_id,
                    receipt_id=receipt_id,
                    result_index=0,
                    action="correct",
                    corrections={"confidence": 0.95},
                    reason="adjudication from a query receipt",
                )
        with request_permission_scope(PermissionMode.GRAPH_WRITE):
            result = api.feedback_from_query(
                feedback_tier_instance_id,
                receipt_id=receipt_id,
                result_index=0,
                action="correct",
                corrections={"confidence": 0.95},
                reason="adjudication from a query receipt",
            )
        assert result.applied is True

    def test_reject_from_query_needs_graph_write(self, feedback_tier_instance_id):
        receipt_id = self._query_receipt(
            feedback_tier_instance_id, "blocking_edges", {"task_id": "t-2"}
        )
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
                api.feedback_from_query(
                    feedback_tier_instance_id,
                    receipt_id=receipt_id,
                    result_index=0,
                    action="reject",
                    reason="review retraction with identity",
                )
        with request_permission_scope(PermissionMode.GRAPH_WRITE):
            result = api.feedback_from_query(
                feedback_tier_instance_id,
                receipt_id=receipt_id,
                result_index=0,
                action="reject",
                reason="review retraction with identity",
            )
        assert result.applied is True


class TestAnonymousReviewTransitionRefused:
    """Under server auth every feedback action needs a resolved actor identity
    — anonymous retraction (reject/flag) ends with this work item."""

    @pytest.mark.parametrize("action", ["approve", "correct", "reject", "flag"])
    def test_auth_on_anonymous_action_refused(
        self,
        feedback_tier_instance_id,
        monkeypatch: pytest.MonkeyPatch,
        action: str,
    ):
        monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            with pytest.raises(AuthenticationError, match="resolved actor identity"):
                _feedback(feedback_tier_instance_id, action)

    def test_auth_off_governed_flag_still_usable(
        self,
        feedback_tier_instance_id,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """``flag`` is the action a governed operator keeps at every auth
        setting. ``reject`` moved to the adjudication floor
        (wi-feedback-approval-rail), so it is covered by the tier tests above,
        not here — the actor-identity rail and the tier rail are independent."""
        monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            result = _feedback(feedback_tier_instance_id, "flag")
        assert result.applied is True
