"""Data ingestion: load CSV/JSON data into EntityGraph using config schema.

Two-stage flow:
1. CoreConfig validated at load time (config module)
2. Data validated against config during ingestion (this module)

Core functions validate DataFrame columns, then iterate rows into
EntityInstance/RelationshipInstance objects. Mapping-driven functions
use IngestionMapping from config for column names and renames.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import polars as pl

from cruxible_core.errors import (
    DataValidationError,
    EntityTypeNotFoundError,
    IngestionError,
    RelationshipNotFoundError,
)
from cruxible_core.graph.operations import validate_entity, validate_relationship
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, make_provenance

if TYPE_CHECKING:
    from cruxible_core.config.schema import CoreConfig
    from cruxible_core.graph.entity_graph import EntityGraph


def ingest_entities(
    config: CoreConfig,
    graph: EntityGraph,
    entity_type: str,
    df: pl.DataFrame,
    id_column: str | None = None,
) -> int:
    """Ingest entities from a DataFrame into the graph.

    Validates entity type against config and checks the ID column
    exists before adding rows.

    Args:
        config: Config with entity type definitions
        graph: Target graph
        entity_type: Entity type to create (must exist in config)
        df: DataFrame with entity data
        id_column: Column with entity IDs (auto-detects from primary_key if None)

    Returns:
        Number of entities added
    """
    schema = config.get_entity_type(entity_type)
    if schema is None:
        raise EntityTypeNotFoundError(entity_type)

    if id_column is None:
        id_column = schema.get_primary_key()
        if id_column is None:
            raise DataValidationError(
                f"No primary key defined for '{entity_type}' and id_column not specified"
            )

    if id_column not in df.columns:
        raise DataValidationError(
            f"ID column '{id_column}' not found in DataFrame (columns: {df.columns})"
        )

    errors: list[str] = []
    pending: list[EntityInstance] = []
    for row_idx, row in enumerate(df.iter_rows(named=True), start=1):
        entity_id = str(row[id_column])
        properties = {k: v for k, v in row.items() if k != id_column}
        try:
            validated = validate_entity(config, graph, entity_type, entity_id, properties)
        except DataValidationError as exc:
            detail = "; ".join(exc.errors) if exc.errors else str(exc)
            errors.append(f"Row {row_idx}: {detail}")
            continue
        pending.append(validated.entity)

    if errors:
        raise DataValidationError(
            f"Entity validation failed for '{entity_type}'",
            errors=errors,
        )

    added = 0
    for entity in pending:
        graph.add_entity(entity)
        added += 1

    return added


def ingest_relationships(
    config: CoreConfig,
    graph: EntityGraph,
    relationship_type: str,
    df: pl.DataFrame,
    from_column: str,
    to_column: str,
    source_ref: str | None = None,
) -> tuple[int, int]:
    """Ingest relationships from a DataFrame into the graph (upsert).

    Validates relationship type against config and checks key columns
    exist. Extra columns become edge properties. Re-ingesting existing
    relationships updates provided properties; omitted properties are
    preserved (merge, not overwrite).

    Args:
        config: Config with relationship definitions
        graph: Target graph
        relationship_type: Relationship type to create (must exist in config)
        df: DataFrame with relationship data
        from_column: Column with source entity IDs
        to_column: Column with target entity IDs

    Returns:
        Tuple of (added, updated) counts
    """
    rel_schema = config.get_relationship(relationship_type)
    if rel_schema is None:
        raise RelationshipNotFoundError(relationship_type)

    errors: list[str] = []
    if from_column not in df.columns:
        errors.append(f"From column '{from_column}' not found in DataFrame")
    if to_column not in df.columns:
        errors.append(f"To column '{to_column}' not found in DataFrame")
    if errors:
        raise DataValidationError(
            f"Relationship validation failed for '{relationship_type}'",
            errors=errors,
        )

    key_cols = {from_column, to_column}
    errors = []
    pending: list[tuple[RelationshipInstance, bool]] = []
    batch_seen: set[tuple[str, str, str, str, str]] = set()

    for row_idx, row in enumerate(df.iter_rows(named=True), start=1):
        from_id = str(row[from_column])
        to_id = str(row[to_column])
        key = (
            rel_schema.from_entity,
            from_id,
            rel_schema.to_entity,
            to_id,
            relationship_type,
        )

        if not graph.has_entity(rel_schema.from_entity, from_id):
            errors.append(
                f"Row {row_idx}: missing source entity "
                f"{rel_schema.from_entity}:{from_id} for relationship '{relationship_type}'"
            )
            continue
        if not graph.has_entity(rel_schema.to_entity, to_id):
            errors.append(
                f"Row {row_idx}: missing target entity "
                f"{rel_schema.to_entity}:{to_id} for relationship '{relationship_type}'"
            )
            continue
        if key in batch_seen:
            errors.append(
                f"Row {row_idx}: duplicate relationship in input "
                f"{rel_schema.from_entity}:{from_id} -[{relationship_type}]-> "
                f"{rel_schema.to_entity}:{to_id}"
            )
            continue

        is_update = graph.has_relationship(
            rel_schema.from_entity,
            from_id,
            rel_schema.to_entity,
            to_id,
            relationship_type,
        )

        # Ambiguity guard: if multiple same-type edges exist, upsert is ambiguous
        if is_update:
            count = graph.relationship_count_between(
                rel_schema.from_entity,
                from_id,
                rel_schema.to_entity,
                to_id,
                relationship_type,
            )
            if count > 1:
                errors.append(
                    f"Row {row_idx}: ambiguous upsert — {count} edges of type "
                    f"'{relationship_type}' exist between "
                    f"{rel_schema.from_entity}:{from_id} and "
                    f"{rel_schema.to_entity}:{to_id}"
                )
                continue

        batch_seen.add(key)
        properties = {k: v for k, v in row.items() if k not in key_cols}
        try:
            validated = validate_relationship(
                config,
                graph,
                rel_schema.from_entity,
                from_id,
                relationship_type,
                rel_schema.to_entity,
                to_id,
                properties,
            )
        except DataValidationError as exc:
            detail = "; ".join(exc.errors) if exc.errors else str(exc)
            errors.append(f"Row {row_idx}: {detail}")
            continue

        pending.append(
            (
                validated.relationship,
                is_update,
            )
        )

    if errors:
        raise DataValidationError(
            f"Relationship validation failed for '{relationship_type}'",
            errors=errors,
        )

    prov_ref = source_ref or relationship_type
    added = 0
    updated = 0
    for rel, is_update in pending:
        if is_update:
            # Read existing provenance and add modification fields
            existing_rel = graph.get_relationship(
                rel.from_type,
                rel.from_id,
                rel.to_type,
                rel.to_id,
                rel.relationship_type,
            )
            update_props = dict(rel.properties)
            if existing_rel:
                old_prov = existing_rel.properties.get("_provenance")
                if old_prov:
                    prov = dict(old_prov)
                    prov["last_modified_at"] = datetime.now(timezone.utc).isoformat()
                    prov["last_modified_by"] = "ingest"
                    update_props["_provenance"] = prov
            graph.update_edge_properties(
                rel.from_type,
                rel.from_id,
                rel.to_type,
                rel.to_id,
                rel.relationship_type,
                update_props,
            )
            updated += 1
        else:
            rel.properties["_provenance"] = make_provenance("ingest", prov_ref)
            graph.add_relationship(rel)
            added += 1

    return (added, updated)


def ingest_from_mapping(
    config: CoreConfig,
    graph: EntityGraph,
    mapping_name: str,
    df: pl.DataFrame,
) -> tuple[int, int]:
    """Ingest data using a named IngestionMapping from the config.

    Looks up the mapping, applies column renames, then delegates
    to ingest_entities or ingest_relationships.

    Args:
        config: Config with ingestion mappings
        graph: Target graph
        mapping_name: Key in config.ingestion
        df: DataFrame to ingest

    Returns:
        Tuple of (added, updated) counts
    """
    mapping = config.ingestion.get(mapping_name)
    if mapping is None:
        raise IngestionError(f"Ingestion mapping '{mapping_name}' not found in config")

    if mapping.column_map:
        df = df.rename(mapping.column_map)

    if mapping.is_entity:
        assert mapping.entity_type is not None, "entity mapping must have entity_type"
        assert mapping.id_column is not None, "entity mapping must have id_column"
        id_column = mapping.column_map.get(mapping.id_column, mapping.id_column)
        count = ingest_entities(config, graph, mapping.entity_type, df, id_column=id_column)
        return (count, 0)
    assert mapping.relationship_type is not None, "relationship mapping must have relationship_type"
    assert mapping.from_column is not None, "relationship mapping must have from_column"
    assert mapping.to_column is not None, "relationship mapping must have to_column"
    from_column = mapping.column_map.get(mapping.from_column, mapping.from_column)
    to_column = mapping.column_map.get(mapping.to_column, mapping.to_column)
    return ingest_relationships(
        config,
        graph,
        mapping.relationship_type,
        df,
        from_column=from_column,
        to_column=to_column,
        source_ref=mapping_name,
    )


def load_data_from_string(data: str, fmt: Literal["csv", "json", "ndjson"]) -> pl.DataFrame:
    """Parse an inline data string into a Polars DataFrame.

    Args:
        data: Raw CSV or JSON content.
        fmt: ``"csv"``, ``"json"`` (array of row objects), or
            ``"ndjson"`` (one JSON object per line).

    Returns:
        Polars DataFrame.

    Raises:
        IngestionError: If parsing fails.
    """
    try:
        if fmt == "csv":
            return pl.read_csv(io.StringIO(data))
        if fmt == "ndjson":
            return pl.read_ndjson(io.BytesIO(data.encode("utf-8")))
        return pl.read_json(io.BytesIO(data.encode("utf-8")))
    except Exception as e:
        raise IngestionError(f"Failed to parse inline {fmt} data: {e}") from e


def load_file(path: str | Path) -> pl.DataFrame:
    """Load a CSV, JSON, or NDJSON file into a Polars DataFrame."""
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            return pl.read_csv(path)
        except Exception as e:
            raise IngestionError(f"Failed to parse CSV file {path}: {e}") from e
    if suffix == ".json":
        try:
            return pl.read_json(path)
        except Exception:
            # Fall back to NDJSON — some .json files use newline-delimited format
            try:
                return pl.read_ndjson(path)
            except Exception as e:
                raise IngestionError(f"Failed to parse JSON file {path}: {e}") from e
    if suffix in (".jsonl", ".ndjson"):
        try:
            return pl.read_ndjson(path)
        except Exception as e:
            raise IngestionError(f"Failed to parse NDJSON file {path}: {e}") from e
    raise IngestionError(
        f"Unsupported file format: '{suffix}' (expected .csv, .json, .jsonl, or .ndjson)"
    )


def ingest_file(
    config: CoreConfig,
    graph: EntityGraph,
    mapping_name: str,
    file_path: str | Path,
) -> tuple[int, int]:
    """Load a file and ingest using a named mapping.

    Convenience combining load_file + ingest_from_mapping.

    Returns:
        Tuple of (added, updated) counts
    """
    df = load_file(file_path)
    return ingest_from_mapping(config, graph, mapping_name, df)
