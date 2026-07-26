"""Governance rails on the feedback ADJUDICATION actions (wi-feedback-approval-rail).

``cruxible_feedback`` sits wholly at GOVERNED_WRITE in the per-tool permission
map, but the tool multiplexes two different governance acts: RECORDING an
observation, and ADJUDICATING a claim. Two rails, both keyed on the payload's
``action`` and both enforced at the service chokepoint every surface funnels
through:

  1. **Tier.** ``approve`` / ``reject`` / ``correct`` require GRAPH_WRITE. Without
     it one GOVERNED_WRITE actor could attest an edge into ``pending`` and then
     approve their own proposal, arriving at a live approved claim on a
     ``proposal_only`` type with no reviewer above them. ``flag`` stays at
     GOVERNED_WRITE — it moves an edge TO pending, i.e. it asks for review
     rather than granting it.
  2. **Kill-switch.** ``CRUXIBLE_REFUSE_DIRECT_WRITES`` now also refuses the
     feedback actions that transition an edge INTO accepted state
     (``approve``/``correct``). It previously disclaimed the feedback channel
     entirely, which made it a half-switch: an operator who froze live writes
     could still have state promoted to live through feedback approve.
     ``reject``/``flag`` are deliberately NOT covered — they move an edge OUT of
     live state, the direction the kill-switch wants.

The tier rail's facade coverage (per-type ``write_tier`` interaction, batch,
from_query) lives in ``tests/test_mcp/test_feedback_write_tier_permissions.py``;
the end-to-end attest-then-approve loop lives in
``tests/test_server/test_hosted_runtime_routes.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DirectWriteRefusedError, PermissionDeniedError
from cruxible_core.feedback.types import FeedbackBatchItem
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope
from cruxible_core.server.errors import error_to_response
from cruxible_core.service.direct_write_policy import env_refuses_feedback_acceptance
from cruxible_core.service.feedback import service_feedback, service_feedback_batch
from cruxible_core.service.mutations import service_add_relationship_inputs
from cruxible_core.service.types import RelationshipWriteInput

CONFIG_YAML = """\
version: "1.0"
name: feedback_approval_rail_test
description: feedback adjudication rail fixture

entity_types:
  Vehicle:
    properties:
      vehicle_id: {type: string, primary_key: true}
  Part:
    properties:
      part_number: {type: string, primary_key: true}

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified: {type: bool, default: false}

constraints: []
"""

ADJUDICATION_ACTIONS = ("approve", "reject", "correct")


def _target() -> RelationshipInstance:
    return RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
    )


class Rail(NamedTuple):
    """The seeded instance plus the receipt its staging write produced.

    Batch feedback items must cite a prior receipt, so the seed receipt rides
    along rather than being re-derived in every batch test.
    """

    instance: CruxibleInstance
    receipt_id: str


@pytest.fixture
def rail(tmp_path: Path) -> Rail:
    """An instance carrying one staged (pending) ``fits`` proposal."""
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = instance.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1",
            properties={"part_number": "BP-1"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1"},
        )
    )
    instance.save_graph(graph)
    result = service_add_relationship_inputs(
        instance,
        [
            RelationshipWriteInput(
                from_type="Part",
                from_id="BP-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": False},
                pending=True,
            )
        ],
        source="add_relationship",
        source_ref="add_relationship",
    )
    assert result.receipt_id is not None
    return Rail(instance=instance, receipt_id=result.receipt_id)


def _review_status(rail: Rail) -> str:
    edge = rail.instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert edge is not None
    return edge.metadata.assertion.review.status


def _feedback(rail: Rail, action: str, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "receipt_id": None,
        "action": action,
        "target": _target(),
        "reason": "rail test",
    }
    kwargs.update(overrides)
    return service_feedback(rail.instance, **kwargs)


def _batch_item(rail: Rail, action: str) -> FeedbackBatchItem:
    return FeedbackBatchItem(
        receipt_id=rail.receipt_id,
        action=action,
        target=_target(),
        reason="rail test",
    )


# ---------------------------------------------------------------------------
# Rail 1: adjudication requires GRAPH_WRITE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ADJUDICATION_ACTIONS)
def test_governed_write_adjudication_refused(rail: Rail, action: str) -> None:
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        with pytest.raises(PermissionDeniedError) as exc:
            _feedback(rail, action)
    # The refusal names the tier the caller must present, and the one they had.
    assert exc.value.required_mode == "GRAPH_WRITE"
    assert exc.value.current_mode == "GOVERNED_WRITE"
    assert "GRAPH_WRITE" in str(exc.value)
    # Refused before any mutation: the proposal is still awaiting review.
    assert _review_status(rail) == "pending"


def test_governed_write_adjudication_refusal_is_receipted(rail: Rail) -> None:
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        with pytest.raises(PermissionDeniedError) as exc:
            _feedback(rail, "approve")
    assert exc.value.mutation_receipt_id is not None


def test_graph_write_approve_allowed(rail: Rail) -> None:
    with request_permission_scope(PermissionMode.GRAPH_WRITE):
        result = _feedback(rail, "approve")
    assert result.applied is True
    assert _review_status(rail) == "approved"


def test_governed_write_flag_allowed(rail: Rail) -> None:
    """``flag`` asks for review; it does not grant it, so it stays governed."""
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        result = _feedback(rail, "flag")
    assert result.applied is True
    assert _review_status(rail) == "pending"


def test_batch_is_gated_at_its_strictest_action(rail: Rail) -> None:
    """One adjudication item lifts the whole batch's requirement, and the batch
    is all-or-nothing: the governed-tier ``flag`` beside it is not applied."""
    items = [_batch_item(rail, "flag"), _batch_item(rail, "approve")]
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE"):
            service_feedback_batch(rail.instance, items)
    assert _review_status(rail) == "pending"

    with request_permission_scope(PermissionMode.GRAPH_WRITE):
        result = service_feedback_batch(rail.instance, items)
    assert result.applied_count == 2
    assert _review_status(rail) == "approved"


def test_batch_of_flags_stays_governed(rail: Rail) -> None:
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        result = service_feedback_batch(rail.instance, [_batch_item(rail, "flag")])
    assert result.applied_count == 1


# ---------------------------------------------------------------------------
# Rail 2: the CRUXIBLE_REFUSE_DIRECT_WRITES kill-switch covers acceptance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["approve", "correct"])
def test_kill_switch_refuses_acceptance_actions(
    rail: Rail,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    monkeypatch.setenv("CRUXIBLE_REFUSE_DIRECT_WRITES", "1")
    with pytest.raises(DirectWriteRefusedError) as exc:
        _feedback(rail, action)
    assert exc.value.kind == "feedback"
    assert exc.value.type_name == "fits"
    assert exc.value.source == action
    assert "CRUXIBLE_REFUSE_DIRECT_WRITES" in str(exc.value)
    assert exc.value.mutation_receipt_id is not None
    assert _review_status(rail) == "pending"


@pytest.mark.parametrize("action", ["reject", "flag"])
def test_kill_switch_does_not_refuse_retraction_actions(
    rail: Rail,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """``reject``/``flag`` move an edge OUT of live state — the direction the
    kill-switch wants. Refusing them would strand pending edges."""
    monkeypatch.setenv("CRUXIBLE_REFUSE_DIRECT_WRITES", "1")
    result = _feedback(rail, action)
    assert result.applied is True


def test_kill_switch_refuses_a_batch_containing_acceptance(
    rail: Rail,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_REFUSE_DIRECT_WRITES", "true")
    items = [_batch_item(rail, "flag"), _batch_item(rail, "approve")]
    with pytest.raises(DirectWriteRefusedError):
        service_feedback_batch(rail.instance, items)
    assert _review_status(rail) == "pending"


def test_kill_switch_unset_allows_acceptance(
    rail: Rail,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CRUXIBLE_REFUSE_DIRECT_WRITES", raising=False)
    result = _feedback(rail, "approve")
    assert result.applied is True
    assert _review_status(rail) == "approved"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
def test_kill_switch_env_spellings_match_the_direct_write_switch(value: str) -> None:
    assert env_refuses_feedback_acceptance("approve", {"CRUXIBLE_REFUSE_DIRECT_WRITES": value})


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_kill_switch_falsy_spellings(value: str) -> None:
    assert not env_refuses_feedback_acceptance("approve", {"CRUXIBLE_REFUSE_DIRECT_WRITES": value})


def test_kill_switch_scope_is_the_acceptance_actions_only() -> None:
    env = {"CRUXIBLE_REFUSE_DIRECT_WRITES": "1"}
    assert env_refuses_feedback_acceptance("approve", env)
    assert env_refuses_feedback_acceptance("correct", env)
    assert not env_refuses_feedback_acceptance("reject", env)
    assert not env_refuses_feedback_acceptance("flag", env)


def test_feedback_kill_switch_refusal_maps_to_403() -> None:
    status, body = error_to_response(DirectWriteRefusedError("feedback", "fits", "approve"))
    assert status == 403
    assert body.error_code == "direct_write_refused"
    assert body.context == {
        "kind": "feedback",
        "type_name": "fits",
        "source": "approve",
    }
