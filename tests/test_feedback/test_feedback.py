"""Tests for the feedback system: types, store, applier, and integration."""

import sqlite3

import pytest

from cruxible_core.config.schema import (
    CoreConfig,
    EntityTypeSchema,
    NamedQuerySchema,
    PropertySchema,
    RelationshipSchema,
    TraversalStep,
)
from cruxible_core.errors import RelationshipAmbiguityError
from cruxible_core.feedback.applier import apply_feedback
from cruxible_core.feedback.store import FeedbackStore
from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
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
            relationship_type="fits",
            from_type="Part",
            from_id="P-2",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": True, "confidence": 0.4},
        )
    )
    return g


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
                    "review_status": PropertySchema(type="string", optional=True),
                },
            ),
        ],
        named_queries={
            "parts_for_vehicle": NamedQuerySchema(
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
            ),
            "approved_parts_for_vehicle": NamedQuerySchema(
                description="Find approved parts that fit a vehicle",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="incoming",
                        filter={
                            "review_status": ["human_approved", "auto_approved"],
                        },
                    )
                ],
                returns="list[Part]",
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
    def test_approve(self, graph: EntityGraph, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="approve",
            target=target,
        )
        assert apply_feedback(graph, fb) is True

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["review_status"] == "human_approved"

    def test_reject(self, graph: EntityGraph, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="reject",
            target=target,
            reason="Wrong fitment",
        )
        assert apply_feedback(graph, fb) is True

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["review_status"] == "human_rejected"

    def test_flag(self, graph: EntityGraph, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="flag",
            target=target,
        )
        assert apply_feedback(graph, fb) is True

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["review_status"] == "pending_review"

    def test_correct(self, graph: EntityGraph, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="correct",
            target=target,
            corrections={"confidence": 0.95, "fitment_notes": "confirmed"},
        )
        assert apply_feedback(graph, fb) is True

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["confidence"] == 0.95
        assert rel.properties["fitment_notes"] == "confirmed"
        assert rel.properties["review_status"] == "human_approved"

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
        )
        assert apply_feedback(graph, fb) is False

    def test_preserves_existing_properties(
        self,
        graph: EntityGraph,
        target: RelationshipInstance,
    ):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="approve",
            target=target,
        )
        apply_feedback(graph, fb)

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["verified"] is True
        assert rel.properties["confidence"] == 0.9
        assert rel.properties["review_status"] == "human_approved"

    def test_agent_with_model_id(
        self,
        graph: EntityGraph,
        target: RelationshipInstance,
    ):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="approve",
            target=target,
            source="agent",
            model_id="claude-opus-4-6",
        )
        assert apply_feedback(graph, fb) is True
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["review_status"] == "agent_approved"

    def test_agent_reject(
        self,
        graph: EntityGraph,
        target: RelationshipInstance,
    ):
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="reject",
            target=target,
            source="agent",
            reason="AI flagged wrong fitment",
        )
        assert apply_feedback(graph, fb) is True
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["review_status"] == "agent_rejected"

    def test_correct_user_review_status_stripped(
        self, graph: EntityGraph, target: RelationshipInstance
    ):
        """System-owned review metadata is not accepted from user corrections."""
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="correct",
            target=target,
            corrections={"review_status": "human_rejected", "fitment_notes": "checked"},
        )
        assert apply_feedback(graph, fb) is True
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["review_status"] == "human_approved"
        assert rel.properties["fitment_notes"] == "checked"

    def test_approve_updates_provenance(self, graph: EntityGraph, target: RelationshipInstance):
        """Feedback actions update _provenance with modification fields."""
        # First add provenance to the edge
        graph.update_edge_properties(
            "Part",
            "P-1",
            "Vehicle",
            "V-1",
            "fits",
            {"_provenance": {"source": "ingest", "created_at": "2026-01-01T00:00:00+00:00"}},
        )
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="approve",
            target=target,
        )
        apply_feedback(graph, fb)
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        prov = rel.properties["_provenance"]
        assert prov["source"] == "ingest"
        assert "last_modified_at" in prov
        assert prov["last_modified_by"] == "feedback:approve"

    def test_reject_updates_provenance(self, graph: EntityGraph, target: RelationshipInstance):
        graph.update_edge_properties(
            "Part",
            "P-1",
            "Vehicle",
            "V-1",
            "fits",
            {"_provenance": {"source": "ingest", "created_at": "2026-01-01T00:00:00+00:00"}},
        )
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="reject",
            target=target,
            reason="Wrong",
        )
        apply_feedback(graph, fb)
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        prov = rel.properties["_provenance"]
        assert prov["last_modified_by"] == "feedback:reject"

    def test_correct_updates_provenance(self, graph: EntityGraph, target: RelationshipInstance):
        graph.update_edge_properties(
            "Part",
            "P-1",
            "Vehicle",
            "V-1",
            "fits",
            {"_provenance": {"source": "ingest", "created_at": "2026-01-01T00:00:00+00:00"}},
        )
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="correct",
            target=target,
            corrections={"confidence": 0.99},
        )
        apply_feedback(graph, fb)
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        prov = rel.properties["_provenance"]
        assert prov["last_modified_by"] == "feedback:correct"

    def test_correct_strips_provenance_from_corrections(
        self, graph: EntityGraph, target: RelationshipInstance
    ):
        """_provenance in corrections is stripped — system-owned field."""
        graph.update_edge_properties(
            "Part",
            "P-1",
            "Vehicle",
            "V-1",
            "fits",
            {"_provenance": {"source": "ingest", "created_at": "2026-01-01T00:00:00+00:00"}},
        )
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="correct",
            target=target,
            corrections={"confidence": 0.99, "_provenance": {"source": "spoofed"}},
        )
        apply_feedback(graph, fb)
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        prov = rel.properties["_provenance"]
        # Should NOT be spoofed — should be original provenance with modification
        assert prov["source"] == "ingest"
        assert prov["last_modified_by"] == "feedback:correct"

    def test_no_provenance_no_crash(self, graph: EntityGraph, target: RelationshipInstance):
        """Feedback on edges without _provenance works fine (no crash)."""
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="approve",
            target=target,
        )
        assert apply_feedback(graph, fb) is True
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["review_status"] == "human_approved"
        # No _provenance added when there was none to begin with
        assert "_provenance" not in rel.properties

    def test_ambiguous_target_requires_edge_key(self, graph: EntityGraph):
        graph.add_relationship(
            RelationshipInstance(
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
            action="approve",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            ),
        )
        with pytest.raises(RelationshipAmbiguityError):
            apply_feedback(graph, fb)

    def test_apply_with_edge_key_targets_single_edge(self, graph: EntityGraph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": True, "confidence": 0.8},
            )
        )
        refs = graph.get_neighbors_with_edge_refs(
            "Part",
            "P-1",
            relationship_type="fits",
            direction="outgoing",
        )
        edge_key = next(edge_key for _, props, edge_key in refs if props.get("confidence") == 0.8)
        fb = FeedbackRecord(
            receipt_id="RCP-test",
            action="approve",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
                edge_key=edge_key,
            ),
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
        )
        fid = store.save_feedback(fb)
        loaded = store.get_feedback(fid)

        assert loaded is not None
        assert loaded.feedback_id == fb.feedback_id
        assert loaded.action == "reject"
        assert loaded.target == target
        assert loaded.reason == "Bad fitment"

    def test_get_nonexistent(self, store: FeedbackStore):
        assert store.get_feedback("FB-nope") is None

    def test_list_by_receipt(self, store: FeedbackStore, target: RelationshipInstance):
        fb1 = FeedbackRecord(receipt_id="RCP-1", action="approve", target=target)
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
                    action="approve",
                    target=target,
                )
            )
        assert len(store.list_feedback()) == 3

    def test_model_id_persisted(self, store: FeedbackStore, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-1",
            action="approve",
            target=target,
            source="agent",
            model_id="claude-opus-4-6",
        )
        store.save_feedback(fb)
        loaded = store.get_feedback(fb.feedback_id)
        assert loaded.model_id == "claude-opus-4-6"
        assert loaded.source == "agent"

    def test_corrections_persisted(self, store: FeedbackStore, target: RelationshipInstance):
        fb = FeedbackRecord(
            receipt_id="RCP-1",
            action="correct",
            target=target,
            corrections={"confidence": 0.99},
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
            source="agent",
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
        fb1 = FeedbackRecord(receipt_id="RCP-1", action="approve", target=target)
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
        store.save_feedback(FeedbackRecord(receipt_id="RCP-1", action="approve", target=target))
        store.save_feedback(FeedbackRecord(receipt_id="RCP-2", action="reject", target=target))
        assert store.count_feedback() == 2
        assert store.count_feedback(receipt_id="RCP-1") == 1


# ---------------------------------------------------------------------------
# OutcomeStore
# ---------------------------------------------------------------------------


class TestOutcomeStore:
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
            source="agent",
            detail={"installed": True},
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

    def test_migrates_legacy_outcomes_schema(self, tmp_path):
        db_path = tmp_path / "feedback.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE outcomes ("
            "outcome_id TEXT PRIMARY KEY, "
            "receipt_id TEXT NOT NULL, "
            "outcome TEXT NOT NULL, "
            "detail TEXT NOT NULL DEFAULT '{}', "
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO outcomes (outcome_id, receipt_id, outcome, detail, created_at) "
            "VALUES ('OUT-1', 'RCP-1', 'correct', '{}', '2026-03-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        migrated = FeedbackStore(db_path)
        try:
            loaded = migrated.get_outcome("OUT-1")
            assert loaded is not None
            assert loaded.anchor_type == "receipt"
            assert loaded.anchor_id == "RCP-1"

            conn2 = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1] for row in conn2.execute("PRAGMA table_info(outcomes)").fetchall()
                }
                indexes = {
                    row[1] for row in conn2.execute("PRAGMA index_list(outcomes)").fetchall()
                }
            finally:
                conn2.close()
        finally:
            migrated.close()

        assert {"anchor_type", "anchor_id", "outcome_code", "decision_context"} <= columns
        assert "idx_outcomes_anchor_type" in indexes
        assert "idx_outcomes_outcome_code" in indexes


# ---------------------------------------------------------------------------
# Integration: feedback reject → re-query excludes edge
# ---------------------------------------------------------------------------


class TestFeedbackQueryIntegration:
    def test_reject_excludes_from_approved_query(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """The gate test: reject an edge, re-query with review_status filter."""
        # Both parts fit V-1 initially
        result = execute_query(
            config,
            graph,
            "parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        assert len(result.results) == 2

        # Set both edges to auto_approved first
        for part_id in ["P-1", "P-2"]:
            graph.update_edge_properties(
                "Part",
                part_id,
                "Vehicle",
                "V-1",
                "fits",
                {"review_status": "auto_approved"},
            )

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
        )
        apply_feedback(graph, fb)

        # Re-query with approved-only filter
        result2 = execute_query(
            config,
            graph,
            "approved_parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        result_ids = {r.entity_id for r in result2.results}
        assert "P-1" in result_ids
        assert "P-2" not in result_ids

    def test_approve_includes_in_approved_query(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """Approved edges pass the review_status filter."""
        # Approve both edges
        for part_id in ["P-1", "P-2"]:
            fb = FeedbackRecord(
                receipt_id="RCP-test",
                action="approve",
                target=RelationshipInstance(
                    from_type="Part",
                    from_id=part_id,
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-1",
                ),
            )
            apply_feedback(graph, fb)

        result = execute_query(
            config,
            graph,
            "approved_parts_for_vehicle",
            {"vehicle_id": "V-1"},
        )
        assert len(result.results) == 2

    def test_correct_updates_and_includes(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """Corrected edges get human_approved status and updated properties."""
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
        )
        apply_feedback(graph, fb)

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel.properties["confidence"] == 0.99
        assert rel.properties["review_status"] == "human_approved"

    def test_rejected_edge_excluded_without_filter(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """Hard safety check: rejected edges are excluded even from queries
        that have no review_status filter."""
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
        )
        apply_feedback(graph, fb)

        # parts_for_vehicle only filters on verified, not review_status —
        # but the engine hard-skips human_rejected and agent_rejected edges
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
            source="agent",
            target=RelationshipInstance(
                from_type="Part",
                from_id="P-2",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            ),
            reason="AI flagged wrong fitment",
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
