"""Actor-context normalization: token-derived GovernedActorContext is preserved
through governance + mutation paths where available.

Covers the four areas of wi-governance-actor-context-normalization:
  1. governed relationship REVIEW stamps (review-status/assertion on edges),
  2. review RESOLUTIONS (group resolution + the GroupResolution record),
  3. mutation RECEIPTS,
  4. direct AND group write PROVENANCE.

Each area is exercised once with a HUMAN actor and once with an AGENT
(service_account) actor. A backward-compat case proves older string-only stamps
(no actor_context) still load and round-trip. The threading is load-bearing:
reverting the source edits makes the "preserved" assertions fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.governance.actors import GovernedActorContext, load_actor_context
from cruxible_core.graph.assertion_state import RelationshipReviewState
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import (
    service_add_relationships,
    service_feedback,
    service_propose_group,
    service_resolve_group,
)

CONFIG_YAML = """\
version: "1.0"
name: actor_context_test
description: For actor-context normalization tests

entity_types:
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
    proposal_policy:
      signals:
        check_v1:
          role: required
      auto_resolve_when: all_support
      auto_resolve_requires_prior_trust: trusted_only

constraints: []
"""


# ---------------------------------------------------------------------------
# Actor fixtures: one human, one agent (service_account).
# ---------------------------------------------------------------------------


def _human_actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="usr_robert",
        org_id="org_1",
        operation_id="op_human",
        timestamp="2026-06-05T12:00:00Z",
    )


def _agent_actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="service_account",
        actor_id="agent_codex",
        org_id="org_1",
        operation_id="op_agent",
        timestamp="2026-06-05T12:00:00Z",
    )


ACTORS = pytest.mark.parametrize(
    ("make_actor", "expected_id", "expected_type"),
    [
        (_human_actor, "usr_robert", "human_user"),
        (_agent_actor, "agent_codex", "service_account"),
    ],
    ids=["human", "agent"],
)


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    inst = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = inst.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1",
            properties={"part_number": "BP-1", "name": "Pads"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1", "year": 2024},
        )
    )
    inst.save_graph(graph)
    return inst


def _get_receipt(instance: CruxibleInstance, receipt_id: str) -> Receipt | None:
    store = instance.get_receipt_store()
    try:
        return store.get_receipt(receipt_id)
    finally:
        store.close()


def _member() -> CandidateMember:
    return CandidateMember(
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
        relationship_type="fits",
        signals=[CandidateSignal(signal_source="check_v1", signal="support")],
        properties={},
    )


# ---------------------------------------------------------------------------
# AREA 1 + AREA 2 + AREA 4: group resolution stamps review state, resolution
# record, and provenance with the resolving actor identity.
# ---------------------------------------------------------------------------


class TestGroupResolveActorContext:
    @ACTORS
    def test_resolve_preserves_actor_on_review_resolution_and_provenance(
        self,
        instance: CruxibleInstance,
        make_actor,
        expected_id: str,
        expected_type: str,
    ) -> None:
        actor = make_actor()
        proposed = service_propose_group(
            instance,
            "fits",
            [_member()],
            thesis_text="t",
            thesis_facts={"k": "v"},
            actor_context=actor,
        )
        resolved = service_resolve_group(
            instance,
            proposed.group_id,
            "approve",
            expected_pending_version=1,
            actor_context=actor,
        )

        rel = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert rel is not None

        # AREA 1: review stamp on the newly group-resolved edge carries actor.
        review = rel.metadata.assertion.review
        assert review.status == "approved"
        assert review.source == "group"
        assert review.actor_context is not None
        assert review.actor_context.actor_id == expected_id
        assert review.actor_context.actor_type == expected_type
        assert review.updated_by == f"group:{proposed.group_id}"

        # AREA 4: created provenance carries actor.
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.created_actor_context is not None
        assert prov.created_actor_context.actor_id == expected_id

        # AREA 2: the GroupResolution record carries the resolving actor.
        store = instance.get_group_store()
        try:
            resolution = store.get_resolution(resolved.resolution_id)
            stored_group = store.get_group(proposed.group_id)
        finally:
            store.close()
        assert resolution is not None
        assert resolution.resolved_actor_context is not None
        assert resolution.resolved_actor_context.actor_id == expected_id
        # The proposal also preserved its proposing actor (review request side).
        assert stored_group is not None
        assert stored_group.proposed_actor_context is not None
        assert stored_group.proposed_actor_context.actor_id == expected_id

    @ACTORS
    def test_resolve_records_actor_on_receipt(
        self,
        instance: CruxibleInstance,
        make_actor,
        expected_id: str,
        expected_type: str,
    ) -> None:
        # AREA 3: the group_resolve mutation receipt carries the actor.
        actor = make_actor()
        proposed = service_propose_group(
            instance,
            "fits",
            [_member()],
            thesis_text="t",
            thesis_facts={"k": "v"},
            actor_context=actor,
        )
        resolved = service_resolve_group(
            instance,
            proposed.group_id,
            "approve",
            expected_pending_version=1,
            actor_context=actor,
        )
        assert resolved.receipt_id is not None
        receipt = _get_receipt(instance, resolved.receipt_id)
        assert receipt is not None
        assert receipt.actor_context is not None
        assert receipt.actor_context.actor_id == expected_id
        assert receipt.actor_context.actor_type == expected_type

    def test_resolve_without_actor_leaves_review_actor_null(
        self, instance: CruxibleInstance
    ) -> None:
        # Preserve-where-available: no actor context supplied -> none fabricated.
        proposed = service_propose_group(
            instance, "fits", [_member()], thesis_text="t", thesis_facts={"k": "v"}
        )
        resolved = service_resolve_group(
            instance, proposed.group_id, "approve", expected_pending_version=1
        )
        rel = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert rel is not None
        review = rel.metadata.assertion.review
        assert review.status == "approved"
        assert review.source == "group"
        assert review.actor_context is None
        assert rel.metadata.provenance.created_actor_context is None
        assert resolved.receipt_id is not None
        receipt = _get_receipt(instance, resolved.receipt_id)
        assert receipt is not None
        assert receipt.actor_context is None


# ---------------------------------------------------------------------------
# AREA 3 + AREA 4: direct relationship write stamps receipt + provenance.
# ---------------------------------------------------------------------------


class TestDirectWriteActorContext:
    @ACTORS
    def test_add_relationships_preserves_actor_on_provenance_and_receipt(
        self,
        instance: CruxibleInstance,
        make_actor,
        expected_id: str,
        expected_type: str,
    ) -> None:
        actor = make_actor()
        result = service_add_relationships(
            instance,
            [
                RelationshipInstance(
                    relationship_type="fits",
                    from_type="Part",
                    from_id="BP-1",
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": True},
                )
            ],
            source="cli",
            source_ref="add_relationship",
            actor_context=actor,
        )
        rel = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert rel is not None
        # AREA 4: direct-write provenance carries actor.
        assert rel.metadata.provenance.created_actor_context is not None
        assert rel.metadata.provenance.created_actor_context.actor_id == expected_id

        # AREA 3: the add_relationship receipt carries actor.
        assert result.receipt_id is not None
        receipt = _get_receipt(instance, result.receipt_id)
        assert receipt is not None
        assert receipt.actor_context is not None
        assert receipt.actor_context.actor_id == expected_id
        assert receipt.actor_context.actor_type == expected_type


# ---------------------------------------------------------------------------
# AREA 1 + AREA 3 + AREA 4: feedback review stamps review state, provenance,
# and the feedback receipt.
# ---------------------------------------------------------------------------


class TestFeedbackActorContext:
    @ACTORS
    def test_feedback_preserves_actor_on_review_provenance_and_receipt(
        self,
        instance: CruxibleInstance,
        make_actor,
        expected_id: str,
        expected_type: str,
    ) -> None:
        # Seed a live edge to give feedback on.
        service_add_relationships(
            instance,
            [
                RelationshipInstance(
                    relationship_type="fits",
                    from_type="Part",
                    from_id="BP-1",
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": False},
                )
            ],
            source="cli",
            source_ref="add_relationship",
        )
        actor = make_actor()
        source = "human" if expected_type == "human_user" else "agent"
        result = service_feedback(
            instance,
            None,
            "approve",
            source,  # type: ignore[arg-type]
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={},
            ),
            reason="looks right",
            actor_context=actor,
        )
        rel = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert rel is not None
        # AREA 1: feedback review stamp carries actor.
        review = rel.metadata.assertion.review
        assert review.status == "approved"
        assert review.actor_context is not None
        assert review.actor_context.actor_id == expected_id
        # AREA 4: feedback-touched provenance carries actor.
        assert rel.metadata.provenance is not None
        assert rel.metadata.provenance.last_modified_actor_context is not None
        assert rel.metadata.provenance.last_modified_actor_context.actor_id == expected_id
        # AREA 3: feedback receipt carries actor.
        assert result.receipt_id is not None
        receipt = _get_receipt(instance, result.receipt_id)
        assert receipt is not None
        assert receipt.actor_context is not None
        assert receipt.actor_context.actor_id == expected_id


# ---------------------------------------------------------------------------
# AREA 3 (group propose): the group_propose receipt carries the proposing actor.
# ---------------------------------------------------------------------------


class TestGroupProposeReceiptActorContext:
    @ACTORS
    def test_propose_records_actor_on_receipt(
        self,
        instance: CruxibleInstance,
        make_actor,
        expected_id: str,
        expected_type: str,
    ) -> None:
        actor = make_actor()
        proposed = service_propose_group(
            instance,
            "fits",
            [_member()],
            thesis_text="t",
            thesis_facts={"k": "v"},
            actor_context=actor,
        )
        assert proposed.receipt_id is not None
        receipt = _get_receipt(instance, proposed.receipt_id)
        assert receipt is not None
        assert receipt.actor_context is not None
        assert receipt.actor_context.actor_id == expected_id
        assert receipt.actor_context.actor_type == expected_type


# ---------------------------------------------------------------------------
# Backward compatibility: older string-only stamps (no actor_context) still
# load and round-trip across all three persisted shapes.
# ---------------------------------------------------------------------------


class TestBackwardCompatibleLoading:
    def test_review_state_loads_without_actor_context(self) -> None:
        # Older persisted review state shape, no actor_context key.
        legacy = {
            "status": "approved",
            "source": "group",
            "updated_at": "2025-01-01T00:00:00Z",
            "updated_by": "group:GRP-legacy",
        }
        review = RelationshipReviewState.model_validate(legacy)
        assert review.status == "approved"
        assert review.actor_context is None
        # Round-trips cleanly (exclude_none drops the absent field).
        dumped = review.model_dump(mode="json", exclude_none=True)
        assert "actor_context" not in dumped
        assert RelationshipReviewState.model_validate(dumped).actor_context is None

    def test_provenance_loads_without_actor_context(self) -> None:
        legacy = {
            "source": "group",
            "source_ref": "group:GRP-legacy",
            "created_at": "2025-01-01T00:00:00Z",
        }
        prov = RelationshipProvenance.model_validate(legacy)
        assert prov.source == "group"
        assert prov.created_actor_context is None
        assert prov.last_modified_actor_context is None

    def test_receipt_loads_without_actor_context(self) -> None:
        # Older receipt JSON predating the actor_context field.
        legacy = {
            "receipt_id": "RCP-legacy",
            "query_name": "",
            "parameters": {},
            "execution_options": {},
            "nodes": [],
            "edges": [],
            "results": [],
            "created_at": "2025-01-01T00:00:00Z",
            "operation_type": "add_relationship",
            "committed": True,
        }
        receipt = Receipt.model_validate(legacy)
        assert receipt.receipt_id == "RCP-legacy"
        assert receipt.actor_context is None

    def test_actor_context_round_trips_through_json(self) -> None:
        # A populated receipt with actor_context survives a JSON round-trip,
        # and the legacy-shaped one (no key) survives too.
        actor = _agent_actor()
        receipt = Receipt(
            nodes=[],
            edges=[],
            operation_type="add_relationship",
            actor_context=actor,
        )
        loaded = Receipt.model_validate_json(receipt.model_dump_json())
        assert loaded.actor_context is not None
        assert loaded.actor_context.actor_id == "agent_codex"

        stripped = json.loads(receipt.model_dump_json())
        stripped.pop("actor_context")
        assert Receipt.model_validate(stripped).actor_context is None

    def test_load_actor_context_helper_accepts_both_shapes(self) -> None:
        assert load_actor_context(None) is None
        assert load_actor_context({}) is None  # malformed/empty -> None, not raise
        good = load_actor_context(
            {
                "actor_type": "human_user",
                "actor_id": "usr_x",
                "org_id": "org_1",
                "operation_id": "op_1",
                "timestamp": "2026-06-05T12:00:00Z",
            }
        )
        assert good is not None
        assert good.actor_id == "usr_x"
