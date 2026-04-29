"""Tests for data ingestion pipeline."""

from pathlib import Path

import polars as pl
import pytest

from cruxible_core.config.loader import load_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import (
    DataValidationError,
    EntityTypeNotFoundError,
    IngestionError,
    RelationshipNotFoundError,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.ingest import (
    ingest_entities,
    ingest_file,
    ingest_from_mapping,
    ingest_relationships,
    load_data_from_string,
    load_file,
)


@pytest.fixture
def config(configs_dir) -> CoreConfig:
    return load_config(configs_dir / "car_parts.yaml")


@pytest.fixture
def graph() -> EntityGraph:
    return EntityGraph()


@pytest.fixture
def vehicles_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "vehicle_id": ["V-CIVIC", "V-ACCORD", "V-CAMRY"],
            "year": [2024, 2024, 2023],
            "make": ["Honda", "Honda", "Toyota"],
            "model": ["Civic", "Accord", "Camry"],
        }
    )


@pytest.fixture
def parts_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "part_number": ["BP-1234", "BP-5678"],
            "name": ["Ceramic Brake Pad", "Performance Rotor"],
            "category": ["brakes", "brakes"],
            "brand": ["StopTech", "Brembo"],
            "price": [45.99, 89.99],
        }
    )


@pytest.fixture
def fitments_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "part_number": ["BP-1234", "BP-1234", "BP-5678"],
            "vehicle_id": ["V-CIVIC", "V-ACCORD", "V-CIVIC"],
            "verified": [True, True, False],
            "confidence": [0.95, 0.9, 0.7],
        }
    )


# ---------------------------------------------------------------------------
# ingest_entities
# ---------------------------------------------------------------------------


class TestIngestEntities:
    def test_basic(self, config: CoreConfig, graph: EntityGraph, vehicles_df):
        count = ingest_entities(config, graph, "Vehicle", vehicles_df)
        assert count == 3
        assert graph.entity_count("Vehicle") == 3

    def test_auto_detect_pk(self, config: CoreConfig, graph: EntityGraph, vehicles_df):
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        vehicle = graph.get_entity("Vehicle", "V-CIVIC")
        assert vehicle is not None
        assert vehicle.properties["make"] == "Honda"
        # PK column is not stored as a property
        assert "vehicle_id" not in vehicle.properties

    def test_explicit_id_column(self, config: CoreConfig, graph: EntityGraph, vehicles_df):
        ingest_entities(config, graph, "Vehicle", vehicles_df, id_column="vehicle_id")
        assert graph.has_entity("Vehicle", "V-CIVIC")

    def test_properties_preserved(self, config: CoreConfig, graph: EntityGraph, parts_df):
        ingest_entities(config, graph, "Part", parts_df)
        part = graph.get_entity("Part", "BP-1234")
        assert part is not None
        assert part.properties["name"] == "Ceramic Brake Pad"
        assert part.properties["price"] == 45.99
        assert part.properties["brand"] == "StopTech"

    def test_invalid_entity_type(self, config: CoreConfig, graph: EntityGraph, vehicles_df):
        with pytest.raises(EntityTypeNotFoundError):
            ingest_entities(config, graph, "BadType", vehicles_df)

    def test_missing_id_column(self, config: CoreConfig, graph: EntityGraph):
        df = pl.DataFrame({"wrong_col": ["a", "b"]})
        with pytest.raises(DataValidationError, match="ID column"):
            ingest_entities(config, graph, "Vehicle", df)

    def test_no_primary_key_no_id_column(self, graph: EntityGraph):
        """Entity type with no primary key and no explicit id_column."""
        from cruxible_core.config.schema import (
            CoreConfig,
            EntityTypeSchema,
            PropertySchema,
        )

        config = CoreConfig(
            name="test",
            entity_types={
                "Thing": EntityTypeSchema(properties={"name": PropertySchema(type="string")})
            },
            relationships=[],
        )
        df = pl.DataFrame({"name": ["a"]})
        with pytest.raises(DataValidationError, match="No primary key"):
            ingest_entities(config, graph, "Thing", df)

    def test_extra_columns_rejected(self, config: CoreConfig, graph: EntityGraph):
        """Columns not in schema are rejected by strict write validation."""
        df = pl.DataFrame(
            {
                "vehicle_id": ["V-1"],
                "year": [2024],
                "make": ["Honda"],
                "model": ["Civic"],
                "custom_field": ["extra"],
            }
        )
        with pytest.raises(DataValidationError) as exc_info:
            ingest_entities(config, graph, "Vehicle", df)
        assert any("unexpected property 'custom_field'" in e for e in exc_info.value.errors)

    def test_empty_dataframe(self, config: CoreConfig, graph: EntityGraph):
        df = pl.DataFrame({"vehicle_id": [], "year": [], "make": [], "model": []}).cast(
            {"vehicle_id": pl.Utf8, "year": pl.Int64, "make": pl.Utf8, "model": pl.Utf8}
        )
        count = ingest_entities(config, graph, "Vehicle", df)
        assert count == 0
        assert graph.entity_count() == 0


# ---------------------------------------------------------------------------
# ingest_relationships
# ---------------------------------------------------------------------------


class TestIngestRelationships:
    def test_basic(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
        fitments_df,
    ):
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        added, updated = ingest_relationships(
            config, graph, "fits", fitments_df, "part_number", "vehicle_id"
        )
        assert added == 3
        assert updated == 0
        assert graph.edge_count("fits") == 3

    def test_edge_properties_preserved(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
        fitments_df,
    ):
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        ingest_relationships(config, graph, "fits", fitments_df, "part_number", "vehicle_id")
        rel = graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        assert rel is not None
        assert rel.properties["verified"] is True
        assert rel.properties["confidence"] == 0.95

    def test_entity_types_from_schema(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        fitments_df,
    ):
        """Relationship ingest fails when referenced entities are missing."""
        with pytest.raises(DataValidationError) as exc_info:
            ingest_relationships(config, graph, "fits", fitments_df, "part_number", "vehicle_id")
        assert any("missing source entity" in e for e in exc_info.value.errors)

    def test_rejects_when_target_entity_missing(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        parts_df,
        fitments_df,
    ):
        """Relationship ingest fails when target entities are missing."""
        ingest_entities(config, graph, "Part", parts_df)
        with pytest.raises(DataValidationError) as exc_info:
            ingest_relationships(config, graph, "fits", fitments_df, "part_number", "vehicle_id")
        assert any("missing target entity" in e for e in exc_info.value.errors)

    def test_invalid_relationship_type(self, config: CoreConfig, graph: EntityGraph, fitments_df):
        with pytest.raises(RelationshipNotFoundError):
            ingest_relationships(config, graph, "bad_rel", fitments_df, "a", "b")

    def test_missing_from_column(self, config: CoreConfig, graph: EntityGraph):
        df = pl.DataFrame({"vehicle_id": ["V-1"]})
        with pytest.raises(DataValidationError) as exc_info:
            ingest_relationships(config, graph, "fits", df, "missing_col", "vehicle_id")
        assert any("From column" in e for e in exc_info.value.errors)

    def test_missing_to_column(self, config: CoreConfig, graph: EntityGraph):
        df = pl.DataFrame({"part_number": ["P-1"]})
        with pytest.raises(DataValidationError) as exc_info:
            ingest_relationships(config, graph, "fits", df, "part_number", "missing_col")
        assert any("To column" in e for e in exc_info.value.errors)

    def test_missing_both_columns(self, config: CoreConfig, graph: EntityGraph):
        df = pl.DataFrame({"unrelated": [1]})
        with pytest.raises(DataValidationError) as exc_info:
            ingest_relationships(config, graph, "fits", df, "from_col", "to_col")
        assert len(exc_info.value.errors) == 2

    def test_rejects_duplicate_relationship_in_input(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        duplicate_df = pl.DataFrame(
            {
                "part_number": ["BP-1234", "BP-1234"],
                "vehicle_id": ["V-CIVIC", "V-CIVIC"],
                "verified": [True, True],
            }
        )
        with pytest.raises(DataValidationError) as exc_info:
            ingest_relationships(config, graph, "fits", duplicate_df, "part_number", "vehicle_id")
        assert any("duplicate relationship in input" in e for e in exc_info.value.errors)

    def test_upserts_duplicate_relationship_in_graph(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
        fitments_df,
    ):
        """Re-ingesting existing relationships updates properties instead of erroring."""
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        added, updated = ingest_relationships(
            config, graph, "fits", fitments_df, "part_number", "vehicle_id"
        )
        assert added == 3
        assert updated == 0

        # Re-ingest with updated properties
        updated_df = pl.DataFrame(
            {
                "part_number": ["BP-1234", "BP-1234", "BP-5678"],
                "vehicle_id": ["V-CIVIC", "V-ACCORD", "V-CIVIC"],
                "verified": [False, False, True],
                "confidence": [0.99, 0.99, 0.99],
            }
        )
        added2, updated2 = ingest_relationships(
            config, graph, "fits", updated_df, "part_number", "vehicle_id"
        )
        assert added2 == 0
        assert updated2 == 3
        assert graph.edge_count("fits") == 3  # No new edges created

        # Verify properties were updated (merge)
        rel = graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        assert rel is not None
        assert rel.properties["confidence"] == 0.99

    def test_upsert_partial_relationship_payload_preserves_existing_properties(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        first_df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "verified": [True],
                "confidence": [0.95],
            }
        )
        ingest_relationships(config, graph, "fits", first_df, "part_number", "vehicle_id")

        patch_df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "verified": [False],
            }
        )
        added, updated = ingest_relationships(
            config, graph, "fits", patch_df, "part_number", "vehicle_id"
        )

        assert added == 0
        assert updated == 1
        rel = graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        assert rel is not None
        assert rel.properties["verified"] is False
        assert rel.properties["confidence"] == 0.95

    def test_invalid_relationship_update_does_not_change_existing_edge(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        first_df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": [0.95],
            }
        )
        ingest_relationships(config, graph, "fits", first_df, "part_number", "vehicle_id")

        invalid_df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": ["high"],
            }
        )
        with pytest.raises(DataValidationError, match="must be a float"):
            ingest_relationships(config, graph, "fits", invalid_df, "part_number", "vehicle_id")

        rel = graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        assert rel is not None
        assert rel.properties["confidence"] == 0.95

    def test_string_confidence_rejected(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        """String confidence values are rejected by the normal float property schema."""
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": ["high"],
            }
        )
        with pytest.raises(DataValidationError, match="must be a float") as exc_info:
            ingest_relationships(config, graph, "fits", df, "part_number", "vehicle_id")
        assert "Row 1" in str(exc_info.value)

    def test_bool_confidence_rejected(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        """Boolean confidence values are rejected (bool is subclass of int)."""
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": [True],
            }
        )
        with pytest.raises(DataValidationError, match="must be a float"):
            ingest_relationships(config, graph, "fits", df, "part_number", "vehicle_id")

    def test_declared_float_property_accepted(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        """Declared float properties are accepted like any other domain field."""
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": [0.7],
            }
        )
        added, _ = ingest_relationships(config, graph, "fits", df, "part_number", "vehicle_id")
        assert added == 1

    def test_no_confidence_accepted(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        """Relationships without confidence column are accepted."""
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
            }
        )
        added, _ = ingest_relationships(config, graph, "fits", df, "part_number", "vehicle_id")
        assert added == 1

    def test_provenance_on_new_edge(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        """New edges get _provenance metadata with source='ingest'."""
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": [0.9],
            }
        )
        ingest_relationships(config, graph, "fits", df, "part_number", "vehicle_id")
        rel = graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        prov = rel.properties.get("_provenance")
        assert prov is not None
        assert prov["source"] == "ingest"
        assert "created_at" in prov
        assert prov["source_ref"] == "fits"

    def test_provenance_preserved_on_upsert(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        """Upsert updates provenance with modification fields."""
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": [0.9],
            }
        )
        ingest_relationships(config, graph, "fits", df, "part_number", "vehicle_id")

        # Re-ingest with updated confidence
        df2 = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": [0.95],
            }
        )
        _, updated = ingest_relationships(config, graph, "fits", df2, "part_number", "vehicle_id")
        assert updated == 1
        rel = graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        prov = rel.properties.get("_provenance")
        assert prov is not None
        assert prov["source"] == "ingest"
        assert "last_modified_at" in prov
        assert prov["last_modified_by"] == "ingest"

    def test_provenance_source_ref_from_mapping(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
        fitments_df,
    ):
        """Mapping-based ingestion uses mapping name as source_ref."""
        ingest_from_mapping(config, graph, "vehicles", vehicles_df)
        ingest_from_mapping(config, graph, "parts", parts_df)
        ingest_from_mapping(config, graph, "fitments", fitments_df)
        rel = graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        prov = rel.properties.get("_provenance")
        assert prov is not None
        assert prov["source_ref"] == "fitments"

    def test_incoming_provenance_stripped(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        """_provenance in CSV data is stripped — system-owned field."""
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)
        df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "_provenance": ["spoofed"],
            }
        )
        ingest_relationships(config, graph, "fits", df, "part_number", "vehicle_id")
        rel = graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        prov = rel.properties.get("_provenance")
        # Should have system-generated provenance, not the spoofed one
        assert prov is not None
        assert isinstance(prov, dict)
        assert prov["source"] == "ingest"

    def test_upsert_preserves_review_status(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
    ):
        """Re-ingesting doesn't erase review_status set by feedback."""
        ingest_entities(config, graph, "Vehicle", vehicles_df)
        ingest_entities(config, graph, "Part", parts_df)

        single_df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": [0.9],
            }
        )
        ingest_relationships(config, graph, "fits", single_df, "part_number", "vehicle_id")

        # Simulate feedback setting review_status
        graph.update_edge_properties(
            "Part",
            "BP-1234",
            "Vehicle",
            "V-CIVIC",
            "fits",
            {"review_status": "human_rejected"},
        )

        # Re-ingest with new confidence
        updated_df = pl.DataFrame(
            {
                "part_number": ["BP-1234"],
                "vehicle_id": ["V-CIVIC"],
                "confidence": [0.95],
            }
        )
        added, updated = ingest_relationships(
            config, graph, "fits", updated_df, "part_number", "vehicle_id"
        )
        assert added == 0
        assert updated == 1

        rel = graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        assert rel is not None
        assert rel.properties["review_status"] == "human_rejected"
        assert rel.properties["confidence"] == 0.95


# ---------------------------------------------------------------------------
# ingest_from_mapping
# ---------------------------------------------------------------------------


class TestIngestFromMapping:
    def test_entity_mapping(self, config: CoreConfig, graph: EntityGraph, vehicles_df):
        added, updated = ingest_from_mapping(config, graph, "vehicles", vehicles_df)
        assert added == 3
        assert updated == 0
        assert graph.has_entity("Vehicle", "V-CIVIC")

    def test_relationship_mapping(
        self,
        config: CoreConfig,
        graph: EntityGraph,
        vehicles_df,
        parts_df,
        fitments_df,
    ):
        ingest_from_mapping(config, graph, "vehicles", vehicles_df)
        ingest_from_mapping(config, graph, "parts", parts_df)
        added, updated = ingest_from_mapping(config, graph, "fitments", fitments_df)
        assert added == 3
        assert updated == 0
        assert graph.edge_count("fits") == 3

    def test_missing_mapping(self, config: CoreConfig, graph: EntityGraph):
        df = pl.DataFrame({"x": [1]})
        with pytest.raises(IngestionError, match="not found in config"):
            ingest_from_mapping(config, graph, "nonexistent", df)

    def test_column_map(self, graph: EntityGraph):
        """column_map renames DataFrame columns before ingestion."""
        from cruxible_core.config.schema import (
            CoreConfig,
            EntityTypeSchema,
            IngestionMapping,
            PropertySchema,
        )

        config = CoreConfig(
            name="test",
            entity_types={
                "Item": EntityTypeSchema(
                    properties={
                        "item_id": PropertySchema(type="string", primary_key=True),
                        "label": PropertySchema(type="string"),
                    }
                )
            },
            relationships=[],
            ingestion={
                "items": IngestionMapping(
                    entity_type="Item",
                    id_column="item_id",
                    column_map={"ID": "item_id", "NAME": "label"},
                )
            },
        )
        df = pl.DataFrame({"ID": ["I-1", "I-2"], "NAME": ["Foo", "Bar"]})
        added, updated = ingest_from_mapping(config, graph, "items", df)
        assert added == 2
        assert updated == 0
        item = graph.get_entity("Item", "I-1")
        assert item is not None
        assert item.properties["label"] == "Foo"


# ---------------------------------------------------------------------------
# load_file / ingest_file
# ---------------------------------------------------------------------------


class TestLoadFile:
    def test_load_csv(self, tmp_path: Path):
        csv_path = tmp_path / "test.csv"
        csv_path.write_text("id,name\n1,Alice\n2,Bob\n")
        df = load_file(csv_path)
        assert len(df) == 2
        assert "id" in df.columns

    def test_load_json(self, tmp_path: Path):
        json_path = tmp_path / "test.json"
        json_path.write_text('[{"id": 1, "name": "Alice"}]')
        df = load_file(json_path)
        assert len(df) == 1

    def test_file_not_found(self):
        with pytest.raises(IngestionError, match="File not found"):
            load_file("/nonexistent/file.csv")

    def test_load_data_from_string_ndjson(self):
        data = '{"id": 1, "name": "Alice"}\n{"id": 2, "name": "Bob"}\n'
        df = load_data_from_string(data, "ndjson")
        assert len(df) == 2
        assert "id" in df.columns

    def test_load_ndjson(self, tmp_path: Path):
        ndjson_path = tmp_path / "test.jsonl"
        ndjson_path.write_text('{"id": 1, "name": "Alice"}\n{"id": 2, "name": "Bob"}\n')
        df = load_file(ndjson_path)
        assert len(df) == 2
        assert "id" in df.columns

    def test_load_ndjson_ext(self, tmp_path: Path):
        ndjson_path = tmp_path / "test.ndjson"
        ndjson_path.write_text('{"id": 1, "name": "Alice"}\n')
        df = load_file(ndjson_path)
        assert len(df) == 1

    def test_load_json_with_ndjson_content(self, tmp_path: Path):
        """A .json file containing NDJSON content should be auto-detected."""
        json_path = tmp_path / "entities.json"
        json_path.write_text('{"id": 1, "name": "Alice"}\n{"id": 2, "name": "Bob"}\n')
        df = load_file(json_path)
        assert len(df) == 2
        assert "id" in df.columns

    def test_unsupported_format(self, tmp_path: Path):
        txt_path = tmp_path / "test.txt"
        txt_path.write_text("hello")
        with pytest.raises(IngestionError, match="Unsupported file format"):
            load_file(txt_path)


class TestIngestFile:
    def test_csv_to_graph(self, config: CoreConfig, graph: EntityGraph, tmp_path: Path):
        csv_path = tmp_path / "vehicles.csv"
        csv_path.write_text(
            "vehicle_id,year,make,model\nV-CIVIC,2024,Honda,Civic\nV-ACCORD,2024,Honda,Accord\n"
        )
        added, updated = ingest_file(config, graph, "vehicles", csv_path)
        assert added == 2
        assert updated == 0
        assert graph.has_entity("Vehicle", "V-CIVIC")
        assert graph.has_entity("Vehicle", "V-ACCORD")
