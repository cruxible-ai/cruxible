"""Tests for service layer mutation functions."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.service import (
    EntityWriteInput,
    RelationshipWriteInput,
    service_add_entities,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_add_relationships,
)


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
