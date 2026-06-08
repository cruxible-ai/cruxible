"""Tests for service layer mutation functions."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    RelationshipWriteInput,
    SharedEvidenceInput,
    service_add_entities,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_add_relationships,
    service_batch_direct_write,
    service_register_source_artifact,
)
from cruxible_core.storage.sqlite import SQLiteGraphRepository


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
                evidence_refs=[
                    {"source": "roadmap_doc", "source_record_id": "batch-section"}
                ],
            )
        },
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
        assert graph.get_relationship(
            "Part",
            "BP-BATCH",
            "Vehicle",
            "V-BATCH",
            "fits",
        ) is None

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

        store = initialized_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.operation_type == "batch_direct_write"
        assert receipt.committed is True

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
        assert [ref.source for ref in rel.metadata.evidence.evidence_refs] == [
            "roadmap_doc"
        ]
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
