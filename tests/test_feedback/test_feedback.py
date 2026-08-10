"""Tests for the feedback system: types, store, applier, and integration."""

import json
from typing import get_args

import pytest
from pydantic import ValidationError

from cruxible_core.config.schema import (
    CoreConfig,
    EntityTypeSchema,
    NamedQuerySchema,
    PropertySchema,
    RelationshipSchema,
    TraversalStep,
)
from cruxible_core.errors import ConfigError, RelationshipAmbiguityError
from cruxible_core.feedback.applier import apply_feedback
from cruxible_core.feedback.store import FeedbackStore
from cruxible_core.feedback.types import FeedbackBatchItem, FeedbackRecord, OutcomeRecord
from cruxible_core.governance.actors import GovernedActorContext, derived_actor_kind
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
    mint_claim_id,
)
from cruxible_core.query.engine import execute_query

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def target() -> RelationshipInstance:
    return RelationshipInstance(
        from_type="Part",
        from_id="P-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
    )


@pytest.fixture
def graph() -> EntityGraph:
    g = EntityGraph()
    g.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1", "make": "Honda"},
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="P-1",
            properties={"part_number": "P-1", "name": "Brake Pad", "category": "brakes"},
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="P-2",
            properties={"part_number": "P-2", "name": "Rotor", "category": "brakes"},
        )
    )
    g.add_relationship(
        RelationshipInstance(
            claim_id=mint_claim_id(),
            relationship_type="fits",
            from_type="Part",
            from_id="P-1",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": True, "confidence": 0.9},
        )
    )
    g.add_relationship(
        RelationshipInstance(
            claim_id=mint_claim_id(),
            relationship_type="fits",
            from_type="Part",
            from_id="P-2",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": True, "confidence": 0.4},
        )
    )
    return g


def human_actor() -> GovernedActorContext:
    """A resolved person. Derives to review source "human"."""
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="usr_reviewer",
        org_id="org_1",
        operation_id="op_human_feedback",
        timestamp="2026-06-05T12:00:00Z",
    )


def agent_actor() -> GovernedActorContext:
    """A service account. Derives to review source "agent"."""
    return GovernedActorContext(
        actor_type="service_account",
        actor_id="svc_triage",
        org_id="org_1",
        operation_id="op_agent_feedback",
        timestamp="2026-06-05T12:00:00Z",
    )


def assert_review_state(
    rel: RelationshipInstance,
    *,
    status: str,
    source: str,
) -> None:
    assert rel.metadata.assertion.review.status == status
    assert rel.metadata.assertion.review.source == source
    assert rel.metadata.assertion.lifecycle.status == "active"


def set_edge_provenance(graph: EntityGraph, *, part_id: str = "P-1") -> None:
    graph.update_relationship_state(
        "Part",
        part_id,
        "Vehicle",
        "V-1",
        "fits",
        metadata=RelationshipMetadata(provenance=RelationshipProvenance(source="ingest")),
    )


def actor_context() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="usr_feedback",
        org_id="org_1",
        operation_id="op_feedback",
        timestamp="2026-06-05T12:00:00Z",
    )


@pytest.fixture
def config() -> CoreConfig:
    return CoreConfig(
        name="test",
        entity_types={
            "Vehicle": EntityTypeSchema(
                properties={
                    "vehicle_id": PropertySchema(type="string", primary_key=True),
                    "make": PropertySchema(type="string"),
                }
            ),
            "Part": EntityTypeSchema(
                properties={
                    "part_number": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                    "category": PropertySchema(type="string"),
                }
            ),
        },
        relationships=[
            RelationshipSchema(
                name="fits",
                from_entity="Part",
                to_entity="Vehicle",
                properties={
                    "verified": PropertySchema(type="bool"),
                    "confidence": PropertySchema(type="float", optional=True),
                },
            ),
        ],
        named_queries={
            "parts_for_vehicle": NamedQuerySchema(
                mode="traversal",
                description="Find parts that fit a vehicle",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="incoming",
                        filter={"verified": True},
                    )
                ],
                returns="list[Part]",
                result_shape="entity",
            ),
            "approved_parts_for_vehicle": NamedQuerySchema(
                mode="traversal",
                description="Find approved parts that fit a vehicle",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="incoming",
                        filter={
                            "verified": True,
                        },
                    )
                ],
                returns="list[Part]",
                result_shape="entity",
            ),
        },
    )


@pytest.fixture
def store() -> FeedbackStore:
    return FeedbackStore(":memory:")


# ---------------------------------------------------------------------------
# RelationshipInstance (feedback target)
# ---------------------------------------------------------------------------


class TestFeedbackTarget:
    def test_roundtrip(self, target: RelationshipInstance):
        json_str = target.model_dump_json()
        restored = RelationshipInstance.model_validate_json(json_str)
        assert restored == target

    def test_fields(self, target: RelationshipInstance):
        assert target.from_type == "Part"
        assert target.from_id == "P-1"
        assert target.relationship_type == "fits"
        assert target.to_type == "Vehicle"
        assert target.to_id == "V-1"


# ---------------------------------------------------------------------------
# Applier
# ---------------------------------------------------------------------------


class TestApplier:
    def test_accept(self, graph: EntityGraph, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="accept",
            target=target,
            actor_context=human_actor(),
        )
        assert apply_feedback(graph, fb) is True

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert_review_state(rel, status="approved", source="human")

    def test_reject(self, graph: EntityGraph, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="reject",
            target=target,
            reason="Wrong fitment",
            actor_context=human_actor(),
        )
        assert apply_feedback(graph, fb) is True

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert_review_state(rel, status="rejected", source="human")

    def test_a_historical_flag_record_still_applies_as_a_no_op(
        self, graph: EntityGraph, target: RelationshipInstance
    ):
        """A legacy ``flag`` row is READABLE and inert, not a crash.

        ``flag`` is gone from every write path, but 0.2.x instances persisted
        rows with it and the feedback store is append-only history. The applier
        has no ``flag`` branch, so such a record moves nothing and reports that
        it applied nothing — it never resurrects the old un-approve behaviour.
        """
        apply_feedback(
            graph,
            FeedbackRecord(
                receipt_id="RCP-seed",
                action="accept",
                target=target,
                actor_context=human_actor(),
            ),
        )
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="flag",
            target=target,
            actor_context=human_actor(),
        )

        assert apply_feedback(graph, fb) is False

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel is not None
        assert rel.metadata.assertion.review.status == "approved", (
            "a historical flag record must not un-approve the edge it names"
        )

    def test_correct(self, graph: EntityGraph, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="correct",
            target=target,
            corrections={"confidence": 0.95, "fitment_notes": "confirmed"},
            actor_context=human_actor(),
        )
        assert apply_feedback(graph, fb) is True

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["confidence"] == 0.95
        assert rel.properties["fitment_notes"] == "confirmed"
        assert_review_state(rel, status="approved", source="human")

    def test_missing_edge(self, graph: EntityGraph):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="reject",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-999",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            ),
            actor_context=human_actor(),
        )
        assert apply_feedback(graph, fb) is False

    def test_preserves_existing_properties(
        self,
        graph: EntityGraph,
        target: RelationshipInstance,
    ):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="accept",
            target=target,
            actor_context=human_actor(),
        )
        apply_feedback(graph, fb)

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["verified"] is True
        assert rel.properties["confidence"] == 0.9
        assert_review_state(rel, status="approved", source="human")

    def test_agent_with_model_id(
        self,
        graph: EntityGraph,
        target: RelationshipInstance,
    ):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="accept",
            target=target,
            model_id="claude-opus-4-6",
            actor_context=agent_actor(),
        )
        assert apply_feedback(graph, fb) is True
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert_review_state(rel, status="approved", source="agent")

    def test_agent_reject(
        self,
        graph: EntityGraph,
        target: RelationshipInstance,
    ):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="reject",
            target=target,
            reason="AI flagged wrong fitment",
            actor_context=agent_actor(),
        )
        assert apply_feedback(graph, fb) is True
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert_review_state(rel, status="rejected", source="agent")

    def test_correct_applies_domain_corrections(
        self, graph: EntityGraph, target: RelationshipInstance
    ):
        """Corrections are domain properties; assertion state still comes from feedback."""
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="correct",
            target=target,
            corrections={"review_note": "checked by reviewer", "fitment_notes": "checked"},
            actor_context=human_actor(),
        )
        assert apply_feedback(graph, fb) is True
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["review_note"] == "checked by reviewer"
        assert_review_state(rel, status="approved", source="human")
        assert rel.properties["fitment_notes"] == "checked"

    def test_accept_updates_provenance(self, graph: EntityGraph, target: RelationshipInstance):
        """Feedback actions update provenance metadata with modification fields."""
        set_edge_provenance(graph)
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="accept",
            target=target,
            actor_context=actor_context(),
        )
        apply_feedback(graph, fb)
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.source == "ingest"
        assert prov.last_modified_at is not None
        assert prov.last_modified_by == "feedback:accept"
        assert prov.last_modified_actor_context is not None
        assert prov.last_modified_actor_context.actor_id == "usr_feedback"
        assert rel.metadata.assertion.review.actor_context is not None
        assert rel.metadata.assertion.review.actor_context.operation_id == "op_feedback"

    def test_reject_updates_provenance(self, graph: EntityGraph, target: RelationshipInstance):
        set_edge_provenance(graph)
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="reject",
            target=target,
            reason="Wrong",
            actor_context=human_actor(),
        )
        apply_feedback(graph, fb)
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.last_modified_by == "feedback:reject"

    def test_correct_updates_provenance(self, graph: EntityGraph, target: RelationshipInstance):
        set_edge_provenance(graph)
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="correct",
            target=target,
            corrections={"confidence": 0.99},
            actor_context=human_actor(),
        )
        apply_feedback(graph, fb)
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.last_modified_by == "feedback:correct"

    def test_correct_metadata_looking_keys_do_not_mutate_metadata(
        self, graph: EntityGraph, target: RelationshipInstance
    ):
        """Low-level corrections cannot spoof first-class metadata."""
        set_edge_provenance(graph)
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="correct",
            target=target,
            corrections={
                "confidence": 0.99,
                "_provenance": {"source": "spoofed"},
                "_assertion": {"review": {"status": "rejected", "source": "human"}},
            },
            actor_context=human_actor(),
        )
        apply_feedback(graph, fb)
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.source == "ingest"
        assert prov.last_modified_by == "feedback:correct"
        assert_review_state(rel, status="approved", source="human")
        assert rel.properties["_provenance"] == {"source": "spoofed"}
        assert rel.properties["_assertion"] == {"review": {"status": "rejected", "source": "human"}}

    def test_no_provenance_backfilled_on_touch(
        self, graph: EntityGraph, target: RelationshipInstance
    ):
        """Feedback on a null-provenance edge marks it, without claiming authorship.

        The touch cannot know where the edge came from, so the origin is
        recorded as unknown-and-backfilled rather than as the feedback
        channel. What IS known — which channel backfilled it, and when — is
        recorded separately.
        """
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="accept",
            target=target,
            actor_context=human_actor(),
        )
        assert apply_feedback(graph, fb) is True
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert_review_state(rel, status="approved", source="human")
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.source == "unknown_backfilled"
        assert prov.source_ref == "unknown_backfilled"
        assert prov.touched_by == "feedback:accept"
        assert prov.backfilled_at is not None
        assert prov.last_modified_by == "feedback:accept"
        assert prov.last_modified_at is not None

    def test_ambiguous_target_requires_edge_key(self, graph: EntityGraph):
        graph.add_relationship(
            RelationshipInstance(
                claim_id=mint_claim_id(),
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": True, "confidence": 0.8},
            )
        )
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="accept",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            ),
            actor_context=human_actor(),
        )
        with pytest.raises(RelationshipAmbiguityError):
            apply_feedback(graph, fb)

    def test_apply_with_edge_key_targets_single_edge(self, graph: EntityGraph):
        graph.add_relationship(
            RelationshipInstance(
                claim_id=mint_claim_id(),
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": True, "confidence": 0.8},
            )
        )
        refs = graph.get_neighbors_with_relationship_refs(
            "Part",
            "P-1",
            relationship_type="fits",
            direction="outgoing",
        )
        edge_key = next(
            edge_key for _, props, _metadata, edge_key in refs if props.get("confidence") == 0.8
        )
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="accept",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
                edge_key=edge_key,
            ),
            actor_context=human_actor(),
        )
        assert apply_feedback(graph, fb) is True


# ---------------------------------------------------------------------------
# FeedbackStore
# ---------------------------------------------------------------------------


class TestFeedbackStore:
    def test_save_and_get(self, store: FeedbackStore, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-1",
            action="reject",
            target=target,
            reason="Bad fitment",
            actor_context=human_actor(),
        )
        fid = store.save_feedback(fb)
        loaded = store.get_feedback(fid)

        assert loaded is not None
        assert loaded.feedback_id == fb.feedback_id
        assert loaded.action == "reject"
        assert loaded.target == target
        assert loaded.reason == "Bad fitment"

    def test_save_and_get_without_source_receipt(
        self,
        store: FeedbackStore,
        target: RelationshipInstance,
    ) -> None:
        fb = FeedbackRecord(action="accept", target=target, reason="Reviewed coordinates")
        fid = store.save_feedback(fb)

        loaded = store.get_feedback(fid)

        assert loaded is not None
        assert loaded.receipt_id is None
        assert loaded.reason == "Reviewed coordinates"

    def test_get_nonexistent(self, store: FeedbackStore):
        assert store.get_feedback("FB-nope") is None

    def test_list_by_receipt(self, store: FeedbackStore, target: RelationshipInstance):
        fb1 = FeedbackRecord(receipt_id="RCP-1", action="accept", target=target)
        fb2 = FeedbackRecord(receipt_id="RCP-2", action="reject", target=target)
        store.save_feedback(fb1)
        store.save_feedback(fb2)

        items = store.list_feedback(receipt_id="RCP-1")
        assert len(items) == 1
        assert items[0].receipt_id == "RCP-1"

    def test_list_all(self, store: FeedbackStore, target: RelationshipInstance):
        for i in range(3):
            store.save_feedback(
                FeedbackRecord(
                    receipt_id=f"RCP-{i}",
                    action="accept",
                    target=target,
                )
            )
        assert len(store.list_feedback()) == 3

    def test_model_id_persisted(self, store: FeedbackStore, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-1",
            action="accept",
            target=target,
            model_id="claude-opus-4-6",
            actor_context=agent_actor(),
        )
        store.save_feedback(fb)
        loaded = store.get_feedback(fb.feedback_id)
        assert loaded.model_id == "claude-opus-4-6"
        # The declared axis is retired; what survives on the record is the
        # actor context the kind is derived from.
        assert loaded.actor_context is not None
        assert loaded.actor_context.actor_type == "service_account"
        assert derived_actor_kind(loaded.actor_context) == "agent"

    def test_corrections_persisted(self, store: FeedbackStore, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-1",
            action="correct",
            target=target,
            corrections={"confidence": 0.99},
            actor_context=human_actor(),
        )
        store.save_feedback(fb)
        loaded = store.get_feedback(fb.feedback_id)
        assert loaded.corrections == {"confidence": 0.99}

    def test_structured_feedback_fields_persisted(
        self, store: FeedbackStore, target: RelationshipInstance
    ):
        fb = FeedbackRecord(
            receipt_id="RCP-1",
            action="reject",
            target=target,
            reason="Legacy unsupported",
            reason_code="legacy_unsupported",
            reason_remediation_hint="decision_policy",
            scope_hints={"category": "brakes"},
            feedback_profile_key="fits",
            feedback_profile_version=2,
            decision_context={
                "surface_type": "query",
                "surface_name": "parts_for_vehicle",
                "operation_type": "query",
            },
            context_snapshot={
                "from": {"entity_id": "P-1", "properties": {"category": "brakes"}},
                "to": {"entity_id": "V-1", "properties": {}},
                "edge": {"relationship": "fits", "properties": {}},
                "context": {"surface_type": "query"},
            },
            actor_context=human_actor(),
        )
        store.save_feedback(fb)
        loaded = store.get_feedback(fb.feedback_id)
        assert loaded is not None
        assert loaded.reason_code == "legacy_unsupported"
        assert loaded.reason_remediation_hint == "decision_policy"
        assert loaded.scope_hints == {"category": "brakes"}
        assert loaded.feedback_profile_key == "fits"
        assert loaded.feedback_profile_version == 2
        assert loaded.decision_context["surface_name"] == "parts_for_vehicle"
        assert loaded.context_snapshot["from"]["properties"] == {"category": "brakes"}

    def test_list_feedback_by_entity_ids(self, store: FeedbackStore, target: RelationshipInstance):
        fb1 = FeedbackRecord(receipt_id="RCP-1", action="accept", target=target)
        fb2 = FeedbackRecord(
            receipt_id="RCP-2",
            action="reject",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-2",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-2",
            ),
        )
        store.save_feedback(fb1)
        store.save_feedback(fb2)
        matches = store.list_feedback_by_entity_ids(["Part:P-1", "Vehicle:V-2"])
        ids = {m.feedback_id for m in matches}
        assert fb1.feedback_id in ids
        assert fb2.feedback_id in ids

    def test_count_feedback(self, store: FeedbackStore, target: RelationshipInstance):
        store.save_feedback(FeedbackRecord(receipt_id="RCP-1", action="accept", target=target))
        store.save_feedback(FeedbackRecord(receipt_id="RCP-2", action="reject", target=target))
        assert store.count_feedback() == 2
        assert store.count_feedback(receipt_id="RCP-1") == 1


# ---------------------------------------------------------------------------
# OutcomeStore
# ---------------------------------------------------------------------------


class TestOutcomeStore:
    def test_resolution_outcome_requires_anchor_id(self):
        with pytest.raises(ValueError, match="resolution outcomes require anchor_id"):
            OutcomeRecord(
                receipt_id="RCP-1",
                anchor_type="resolution",
                outcome="correct",
            )

    def test_save_and_get(self, store: FeedbackStore):
        out = OutcomeRecord(
            receipt_id="RCP-1",
            anchor_type="receipt",
            outcome="correct",
            outcome_code="bad_result",
            outcome_remediation_hint="provider_fix",
            scope_hints={"surface": "parts_for_vehicle"},
            outcome_profile_key="query_quality",
            outcome_profile_version=2,
            decision_context={
                "surface_type": "query",
                "surface_name": "parts_for_vehicle",
                "operation_type": "query",
            },
            lineage_snapshot={
                "receipt": {"receipt_id": "RCP-1", "operation_type": "query"},
                "surface": {"type": "query", "name": "parts_for_vehicle"},
                "trace_set": {"trace_ids": [], "provider_names": [], "trace_count": 0},
            },
            detail={"installed": True},
            actor_context=human_actor(),
        )
        oid = store.save_outcome(out)
        loaded = store.get_outcome(oid)

        assert loaded is not None
        assert loaded.outcome == "correct"
        assert loaded.anchor_id == "RCP-1"
        assert loaded.outcome_code == "bad_result"
        assert loaded.outcome_remediation_hint == "provider_fix"
        assert loaded.outcome_profile_key == "query_quality"
        assert loaded.decision_context["surface_name"] == "parts_for_vehicle"
        assert loaded.detail == {"installed": True}

    def test_get_nonexistent(self, store: FeedbackStore):
        assert store.get_outcome("OUT-nope") is None

    def test_list_by_receipt(self, store: FeedbackStore):
        store.save_outcome(OutcomeRecord(receipt_id="RCP-1", outcome="correct"))
        store.save_outcome(OutcomeRecord(receipt_id="RCP-2", outcome="incorrect"))

        items = store.list_outcomes(receipt_id="RCP-1")
        assert len(items) == 1
        assert items[0].receipt_id == "RCP-1"

    def test_list_all(self, store: FeedbackStore):
        for i in range(3):
            store.save_outcome(
                OutcomeRecord(
                    receipt_id=f"RCP-{i}",
                    outcome="correct",
                )
            )
        assert len(store.list_outcomes()) == 3

    def test_count_outcomes(self, store: FeedbackStore):
        store.save_outcome(OutcomeRecord(receipt_id="RCP-1", outcome="correct"))
        store.save_outcome(OutcomeRecord(receipt_id="RCP-1", outcome="partial"))
        store.save_outcome(OutcomeRecord(receipt_id="RCP-2", outcome="incorrect"))
        assert store.count_outcomes() == 3
        assert store.count_outcomes(receipt_id="RCP-1") == 2


# ---------------------------------------------------------------------------
# Integration: feedback reject → re-query excludes edge
# ---------------------------------------------------------------------------


class TestFeedbackQueryIntegration:
    def test_reject_excludes_from_live_query(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """Rejecting an edge removes it from canonical live traversal."""
        # Both parts fit V-1 initially
        result = execute_query(
            config,
            graph,
            "parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        assert len(result.results) == 2

        # Reject P-2's edge
        fb = FeedbackRecord(
            receipt_id=result.receipt.receipt_id,
            action="reject",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-2",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            ),
            reason="Wrong fitment for this trim",
            actor_context=human_actor(),
        )
        apply_feedback(graph, fb)

        result2 = execute_query(
            config,
            graph,
            "parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        result_ids = {r.entity_id for r in result2.results}
        assert "P-1" in result_ids
        assert "P-2" not in result_ids

    def test_accept_keeps_edge_live(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """Accepted active edges pass canonical live traversal."""
        # Accept both edges
        for part_id in ["P-1", "P-2"]:
            fb = FeedbackRecord(
                receipt_id="RCP-test",
                action="accept",
                target=RelationshipInstance(
                    from_type="Part",
                    from_id=part_id,
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-1",
                ),
                actor_context=human_actor(),
            )
            apply_feedback(graph, fb)

        result = execute_query(
            config,
            graph,
            "parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        assert len(result.results) == 2

    def test_correct_updates_and_includes(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """Corrected edges get approved assertion state and updated properties."""
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="correct",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            ),
            corrections={"confidence": 0.99},
            actor_context=human_actor(),
        )
        apply_feedback(graph, fb)

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["confidence"] == 0.99
        assert_review_state(rel, status="approved", source="human")

    def test_rejected_edge_excluded_without_filter(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """Hard safety check: rejected edges are excluded from live queries."""
        # Both parts returned initially
        result = execute_query(
            config,
            graph,
            "parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        assert len(result.results) == 2

        # Reject P-2
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="reject",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-2",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            ),
            reason="Wrong part",
            actor_context=human_actor(),
        )
        apply_feedback(graph, fb)

        # parts_for_vehicle only filters on verified; relationship metadata controls liveness.
        result2 = execute_query(
            config,
            graph,
            "parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        result_ids = {r.entity_id for r in result2.results}
        assert "P-1" in result_ids
        assert "P-2" not in result_ids

    def test_agent_rejected_edge_excluded_without_filter(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """AI-rejected edges are also excluded from query results."""
        result = execute_query(
            config,
            graph,
            "parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        assert len(result.results) == 2

        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="reject",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-2",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            ),
            reason="AI flagged wrong fitment",
            actor_context=agent_actor(),
        )
        apply_feedback(graph, fb)

        result2 = execute_query(
            config,
            graph,
            "parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        result_ids = {r.entity_id for r in result2.results}
        assert "P-1" in result_ids
        assert "P-2" not in result_ids


def test_receipt_nullable_rebuild_carries_the_target_claim_id(tmp_path) -> None:
    """The rebuild copies COLUMN BY COLUMN: an omitted column silently NULLs.

    ``target_claim_id`` was declared on the rebuilt table but absent from the
    INSERT...SELECT, so it survived only because the column-add migration
    happened to run first. Ordering is not an invariant; naming the column is.
    """
    import sqlite3

    db_path = tmp_path / "feedback.db"
    conn = sqlite3.connect(db_path)
    try:
        # A store built before receipt_id became nullable, but already carrying
        # the record-time claim stamp.
        conn.executescript(
            """
CREATE TABLE feedback (
    feedback_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_json TEXT NOT NULL,
    target_relationship TEXT NOT NULL DEFAULT '',
    target_from_type TEXT NOT NULL DEFAULT '',
    target_from_id TEXT NOT NULL DEFAULT '',
    target_to_type TEXT NOT NULL DEFAULT '',
    target_to_id TEXT NOT NULL DEFAULT '',
    target_edge_key INTEGER,
    target_claim_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    reason_code TEXT,
    reason_remediation_hint TEXT,
    scope_hints TEXT NOT NULL DEFAULT '{}',
    feedback_profile_key TEXT,
    feedback_profile_version INTEGER,
    feedback_profile_digest TEXT,
    decision_context TEXT NOT NULL DEFAULT '{}',
    context_snapshot TEXT NOT NULL DEFAULT '{}',
    decision_surface_type TEXT,
    decision_surface_name TEXT,
    source TEXT NOT NULL DEFAULT 'human',
    model_id TEXT,
    corrections TEXT NOT NULL DEFAULT '{}',
    actor_context TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO feedback (
    feedback_id, receipt_id, action, target_json, target_claim_id, created_at
) VALUES ('FB-1', 'RCP-1', 'approve', '{}', 'CLM-carried00000001', '2026-07-24T00:00:00Z');
"""
        )
        conn.commit()
    finally:
        conn.close()

    store = FeedbackStore(db_path)
    try:
        row = store._conn.execute(
            "SELECT target_claim_id FROM feedback WHERE feedback_id = 'FB-1'"
        ).fetchone()
    finally:
        store.close()
    assert row["target_claim_id"] == "CLM-carried00000001"


# ---------------------------------------------------------------------------
# Retired-action read compatibility
# ---------------------------------------------------------------------------


_HISTORICAL_TARGET_JSON = json.dumps(
    {
        "relationship_type": "fits",
        "from_type": "Part",
        "from_id": "P-1",
        "to_type": "Vehicle",
        "to_id": "V-1",
        "properties": {},
    }
)


def _seed_historical_flag_row(store: FeedbackStore) -> None:
    """Insert a row exactly as a 0.2.x instance persisted a ``flag``.

    Written as raw SQL on purpose: the point is a row that the CURRENT write
    path can no longer produce. Going through ``save_feedback`` would prove
    nothing, because the model it round-trips is the thing under test.
    """
    store._conn.execute(
        "INSERT INTO feedback ("
        "  feedback_id, receipt_id, action, target_json, target_relationship,"
        "  target_from_type, target_from_id, target_to_type, target_to_id,"
        "  reason, source, created_at"
        ") VALUES ("
        "  'FB-legacy-flag', 'RCP-legacy', 'flag', ?, 'fits',"
        "  'Part', 'P-1', 'Vehicle', 'V-1',"
        "  'looked wrong to me', 'human', '2026-01-02T00:00:00Z'"
        ")",
        (_HISTORICAL_TARGET_JSON,),
    )
    store._conn.commit()


class TestHistoricalFlagRowsStayReadable:
    """A retired WRITE action must not become an unreadable STORED action.

    ``flag`` was removed from every write path, but the feedback store is
    append-only history and 0.2.x instances already persisted rows with it.
    Every read reconstructs through ``FeedbackRecord``, so narrowing the stored
    vocabulary would have made an ordinary ``list`` raise ValidationError on any
    historical instance — a silent data-compatibility break, not a cleanup.
    """

    def test_get_feedback_reconstructs_a_legacy_flag_row(self, store: FeedbackStore):
        _seed_historical_flag_row(store)

        record = store.get_feedback("FB-legacy-flag")

        assert record is not None
        assert record.action == "flag"
        assert record.reason == "looked wrong to me"
        assert record.target.relationship_type == "fits"

    def test_list_feedback_includes_a_legacy_flag_row(self, store: FeedbackStore):
        _seed_historical_flag_row(store)

        records = store.list_feedback()

        assert [record.action for record in records] == ["flag"]

    def test_list_feedback_can_still_filter_to_the_retired_action(self, store: FeedbackStore):
        """Analysis over history has to be able to ask about retired actions."""
        _seed_historical_flag_row(store)

        assert len(store.list_feedback(action="flag")) == 1
        assert store.count_feedback(action="flag") == 1
        assert store.list_feedback(action="approve") == []

    def test_a_legacy_row_round_trips_through_serialization(self, store: FeedbackStore):
        """CLI/HTTP rendering dumps the record; that must not raise either."""
        _seed_historical_flag_row(store)

        record = store.get_feedback("FB-legacy-flag")
        assert record is not None

        payload = record.model_dump(mode="json")

        assert payload["action"] == "flag"
        assert FeedbackRecord.model_validate(payload).action == "flag"

    def test_the_retired_actions_are_refused_by_the_input_schema(
        self, target: RelationshipInstance
    ):
        """Readable is not writable: the input models no longer admit them.

        ``flag`` and ``approve`` were removed from every input vocabulary in
        0.4.0, so a caller that still sends one gets an ordinary unknown-value
        validation error instead of a compatibility warning or refusal. The
        stored vocabulary still admits both so history stays readable.
        """
        from cruxible_core.feedback.types import (
            RETIRED_FEEDBACK_ACTIONS,
            FeedbackAction,
            StoredFeedbackAction,
        )
        from cruxible_core.service.feedback import (
            _VALID_ACTIONS,
            _validate_feedback_request_values,
        )

        assert set(get_args(StoredFeedbackAction)) - set(get_args(FeedbackAction)) == (
            RETIRED_FEEDBACK_ACTIONS | {"approve"}
        )
        assert "flag" not in _VALID_ACTIONS
        assert "approve" not in _VALID_ACTIONS

        for retired in ("flag", "approve"):
            with pytest.raises(ValidationError):
                FeedbackBatchItem(
                    receipt_id="RCP-1",
                    action=retired,
                    target=target,
                )
            with pytest.raises(ConfigError, match="Invalid action"):
                _validate_feedback_request_values(action=retired, corrections=None)
