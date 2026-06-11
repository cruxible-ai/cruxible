"""Tests for shared validation/apply helpers in graph/operations.py."""

from __future__ import annotations

import pytest

from cruxible_core.config.loader import load_config_from_string
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.operations import (
    apply_entity,
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.provenance import make_provenance
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, RelationshipMetadata

CONFIG_YAML = """\
version: "1.0"
name: test_ops
entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      confidence:
        type: float
        required: true
      note:
        type: string
        optional: true
"""


@pytest.fixture
def config():
    return load_config_from_string(CONFIG_YAML)


@pytest.fixture
def graph():
    g = EntityGraph()
    g.add_entity(
        EntityInstance(entity_type="Vehicle", entity_id="V1", properties={"vehicle_id": "V1"})
    )
    g.add_entity(
        EntityInstance(entity_type="Part", entity_id="P1", properties={"part_number": "P1"})
    )
    g.add_entity(
        EntityInstance(entity_type="Part", entity_id="P2", properties={"part_number": "P2"})
    )
    return g


# ---------------------------------------------------------------------------
# validate_entity
# ---------------------------------------------------------------------------


class TestValidateEntity:
    def test_valid_new_entity(self, config, graph):
        result = validate_entity(
            config,
            graph,
            "Vehicle",
            "V2",
            {"vehicle_id": "V2"},
            metadata={"source": "test"},
        )
        assert result.entity.entity_type == "Vehicle"
        assert result.entity.entity_id == "V2"
        assert "vehicle_id" not in result.entity.properties
        assert result.entity.metadata == {"source": "test"}
        assert result.is_update is False

    def test_valid_update_entity(self, config, graph):
        result = validate_entity(config, graph, "Vehicle", "V1", {"vehicle_id": "V1"})
        assert result.is_update is True

    def test_bad_type(self, config, graph):
        with pytest.raises(DataValidationError, match="not found in config"):
            validate_entity(config, graph, "NoSuchType", "X1")

    def test_empty_id(self, config, graph):
        with pytest.raises(DataValidationError, match="must not be empty"):
            validate_entity(config, graph, "Vehicle", "   ")


# ---------------------------------------------------------------------------
# validate_relationship
# ---------------------------------------------------------------------------


class TestValidateRelationship:
    def test_valid_new_relationship(self, config, graph):
        result = validate_relationship(
            config, graph, "Part", "P1", "fits", "Vehicle", "V1", {"confidence": 0.8}
        )
        assert result.relationship.relationship_type == "fits"
        assert result.is_update is False
        assert result.relationship.properties["confidence"] == 0.8

    def test_bad_direction(self, config, graph):
        """from_type doesn't match config from_entity -> error."""
        with pytest.raises(DataValidationError, match="from_type.*does not match"):
            validate_relationship(config, graph, "Vehicle", "V1", "fits", "Part", "P1")

    def test_missing_source_entity(self, config, graph):
        with pytest.raises(DataValidationError, match="Part:MISSING not found"):
            validate_relationship(config, graph, "Part", "MISSING", "fits", "Vehicle", "V1")

    def test_missing_target_entity(self, config, graph):
        with pytest.raises(DataValidationError, match="Vehicle:MISSING not found"):
            validate_relationship(config, graph, "Part", "P1", "fits", "Vehicle", "MISSING")

    def test_confidence_bool_rejected(self, config, graph):
        with pytest.raises(DataValidationError, match="must be a float"):
            validate_relationship(
                config,
                graph,
                "Part",
                "P1",
                "fits",
                "Vehicle",
                "V1",
                {"confidence": True},
            )

    def test_confidence_string_coerced(self, config, graph):
        with pytest.raises(DataValidationError, match="must be a float"):
            validate_relationship(
                config,
                graph,
                "Part",
                "P1",
                "fits",
                "Vehicle",
                "V1",
                {"confidence": "0.75"},
            )

    def test_confidence_non_numeric_rejected(self, config, graph):
        with pytest.raises(DataValidationError, match="must be a float"):
            validate_relationship(
                config,
                graph,
                "Part",
                "P1",
                "fits",
                "Vehicle",
                "V1",
                {"confidence": "high"},
            )

    def test_relationship_metadata_keys_are_rejected_as_domain_properties(self, config, graph):
        with pytest.raises(DataValidationError, match="unexpected property '_assertion'"):
            validate_relationship(
                config,
                graph,
                "Part",
                "P1",
                "fits",
                "Vehicle",
                "V1",
                {
                    "confidence": 0.9,
                    "_provenance": {"source": "evil"},
                    "_assertion": {"review": {"status": "approved", "source": "human"}},
                    "review_status": "human_approved",
                },
            )

    def test_unknown_relationship(self, config, graph):
        with pytest.raises(DataValidationError, match="not found in config"):
            validate_relationship(config, graph, "Part", "P1", "no_such_rel", "Vehicle", "V1")

    def test_new_relationship_missing_required_property_rejected(self, config, graph):
        with pytest.raises(DataValidationError, match="missing required property 'confidence'"):
            validate_relationship(
                config,
                graph,
                "Part",
                "P1",
                "fits",
                "Vehicle",
                "V1",
                {"note": "partial insert"},
            )

    def test_relationship_write_does_not_enforce_json_schema(self):
        json_config = load_config_from_string(
            """\
version: "1.0"
name: json_schema_graph_write
entity_types:
  Source:
    properties:
      source_id:
        type: string
        primary_key: true
  Target:
    properties:
      target_id:
        type: string
        primary_key: true
relationships:
  - name: links
    from: Source
    to: Target
    properties:
      evidence:
        type: json
        json_schema:
          type: object
          required: [status]
          properties:
            status:
              type: string
              enum: [approved]
"""
        )
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Source", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Target", entity_id="T1", properties={}))

        result = validate_relationship(
            json_config,
            graph,
            "Source",
            "S1",
            "links",
            "Target",
            "T1",
            {"evidence": {"status": "rejected"}},
        )

        assert result.relationship.properties["evidence"] == {"status": "rejected"}

    def test_update_relationship_merges_and_validates_full_payload(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                properties={"confidence": 0.5},
                metadata=RelationshipMetadata(
                    provenance=make_provenance("ingest", "fitments"),
                    assertion=RelationshipAssertion(
                        review=RelationshipReviewState(
                            status="approved",
                            source="human",
                        )
                    ),
                ),
            )
        )

        validated = validate_relationship(
            config,
            graph,
            "Part",
            "P1",
            "fits",
            "Vehicle",
            "V1",
            {"note": "verified manually"},
        )

        assert validated.is_update is True
        assert validated.relationship.properties == {
            "confidence": 0.5,
            "note": "verified manually",
        }
        apply_relationship(graph, validated, "cli_add", "add_relationship")
        rel = graph.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
        assert rel is not None
        assert rel.properties["confidence"] == 0.5
        assert rel.properties["note"] == "verified manually"
        assert rel.metadata.assertion.review.status == "approved"
        assert rel.metadata.assertion.review.source == "human"

    def test_invalid_relationship_update_leaves_existing_edge_unchanged(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                properties={"confidence": 0.5},
            )
        )

        with pytest.raises(DataValidationError, match="value may not be null"):
            validate_relationship(
                config,
                graph,
                "Part",
                "P1",
                "fits",
                "Vehicle",
                "V1",
                {"confidence": None},
            )

        rel = graph.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
        assert rel is not None
        assert rel.properties == {"confidence": 0.5}


# ---------------------------------------------------------------------------
# apply_entity
# ---------------------------------------------------------------------------


class TestApplyEntity:
    def test_apply_new(self, config, graph):
        validated = validate_entity(config, graph, "Vehicle", "V2", {"vehicle_id": "V2"})
        apply_entity(graph, validated)
        assert graph.has_entity("Vehicle", "V2")

    def test_apply_update(self, config, graph):
        validated = validate_entity(
            config,
            graph,
            "Vehicle",
            "V1",
            {"vehicle_id": "V1"},
            metadata={"last_seen": "service"},
        )
        apply_entity(graph, validated)
        entity = graph.get_entity("Vehicle", "V1")
        assert "vehicle_id" in entity.properties
        assert entity.metadata["last_seen"] == "service"

    def test_apply_update_preserves_existing_metadata_when_no_metadata_provided(
        self, config, graph
    ):
        graph.update_entity_metadata("Vehicle", "V1", {"origin": "fixture"})

        validated = validate_entity(config, graph, "Vehicle", "V1", {"vehicle_id": "V1"})
        apply_entity(graph, validated)

        entity = graph.get_entity("Vehicle", "V1")
        assert entity is not None
        assert entity.metadata == {"origin": "fixture"}

    def test_unknown_property_rejected(self, config, graph):
        with pytest.raises(DataValidationError, match="unexpected property 'extra'"):
            validate_entity(config, graph, "Vehicle", "V1", {"extra": "x"})


# ---------------------------------------------------------------------------
# apply_relationship
# ---------------------------------------------------------------------------


class TestApplyRelationship:
    def test_new_provenance(self, config, graph):
        """New edge gets make_provenance(source, source_ref)."""
        validated = validate_relationship(
            config,
            graph,
            "Part",
            "P1",
            "fits",
            "Vehicle",
            "V1",
            {"confidence": 0.9},
        )
        apply_relationship(graph, validated, "mcp_add", "add_relationship")
        rel = graph.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
        assert rel is not None
        assert rel.properties == {"confidence": 0.9}
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.source == "mcp_add"
        assert prov.source_ref == "add_relationship"
        assert prov.created_at is not None
        assert rel.metadata.assertion.review.status == "unreviewed"
        assert rel.metadata.assertion.lifecycle.status == "active"

    def test_update_provenance(self, config, graph):
        """Existing provenance preserved with last_modified_at/last_modified_by."""
        # First add the relationship
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                properties={"confidence": 0.5},
                metadata=RelationshipMetadata(provenance=make_provenance("ingest", "fitments")),
            )
        )
        original_prov = graph.get_relationship(
            "Part", "P1", "Vehicle", "V1", "fits"
        ).metadata.provenance
        assert original_prov is not None

        # Now update via apply_relationship
        validated = validate_relationship(
            config,
            graph,
            "Part",
            "P1",
            "fits",
            "Vehicle",
            "V1",
            {"confidence": 0.9},
        )
        assert validated.is_update is True
        apply_relationship(graph, validated, "cli_add", "add_relationship")

        rel = graph.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
        assert rel.properties["confidence"] == 0.9
        prov = rel.metadata.provenance
        assert prov is not None
        # Original provenance fields preserved
        assert prov.source == original_prov.source
        assert prov.created_at == original_prov.created_at
        # Modification fields added
        assert prov.last_modified_by == "cli_add"
        assert prov.last_modified_at is not None

    def test_cli_provenance(self, config, graph):
        """CLI source values are preserved."""
        validated = validate_relationship(
            config,
            graph,
            "Part",
            "P2",
            "fits",
            "Vehicle",
            "V1",
            {"confidence": 0.9},
        )
        apply_relationship(graph, validated, "cli_add", "add_relationship")
        rel = graph.get_relationship("Part", "P2", "Vehicle", "V1", "fits")
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.source == "cli_add"
        assert prov.source_ref == "add_relationship"
