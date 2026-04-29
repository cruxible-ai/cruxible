"""Mutation service functions — add_entities, add_relationships, ingest."""

from __future__ import annotations

import json as _json
from collections.abc import Sequence
from typing import Any

from cruxible_core.errors import ConfigError, CoreError, DataValidationError
from cruxible_core.graph.operations import (
    apply_entity,
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.ingest import ingest_file, ingest_from_mapping, load_data_from_string
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.service._helpers import (
    MutationReceiptContext,
    _config_digest,
    _save_graph,
    mutation_receipt,
)
from cruxible_core.service._ownership import check_type_ownership
from cruxible_core.service.types import (
    AddEntityResult,
    AddRelationshipResult,
    IngestResult,
)


def service_add_entities(
    instance: InstanceProtocol,
    entities: Sequence[EntityInstance],
    *,
    _create_receipt: bool = True,
) -> AddEntityResult:
    """Add or update entities in the graph (batch upsert).

    Validates all entities first, then applies atomically.
    Raises DataValidationError on duplicates within the batch or schema violations.
    """
    check_type_ownership(instance, entity_types=[entity.entity_type for entity in entities])
    config = instance.load_config()
    graph = instance.load_graph()

    ctx: MutationReceiptContext[AddEntityResult]
    with mutation_receipt(
        instance,
        "add_entity",
        {"count": len(entities)},
        enabled=_create_receipt,
    ) as ctx:
        builder = ctx.builder
        errors: list[str] = []
        batch_seen: set[tuple[str, str]] = set()
        pending = []

        for i, ent in enumerate(entities, start=1):
            key = (ent.entity_type, ent.entity_id)
            if key in batch_seen:
                errors.append(f"Entity {i}: duplicate in batch {ent.entity_type}:{ent.entity_id}")
                if builder:
                    builder.record_validation(
                        passed=False,
                        detail={"entity": i, "error": "duplicate in batch"},
                    )
                continue

            try:
                validated = validate_entity(
                    config,
                    graph,
                    ent.entity_type,
                    ent.entity_id,
                    ent.properties,
                )
            except DataValidationError as exc:
                errors.append(f"Entity {i}: {exc}")
                if builder:
                    builder.record_validation(passed=False, detail={"entity": i, "error": str(exc)})
                continue

            batch_seen.add(key)
            pending.append(validated)
            if builder:
                builder.record_validation(
                    passed=True,
                    detail={"entity_type": ent.entity_type, "entity_id": ent.entity_id},
                )

        if errors:
            raise DataValidationError(
                f"Entity validation failed with {len(errors)} error(s)",
                errors=errors,
            )

        added = 0
        updated = 0
        for validated in pending:
            apply_entity(graph, validated)
            if builder:
                builder.record_entity_write(
                    validated.entity.entity_type,
                    validated.entity.entity_id,
                    is_update=validated.is_update,
                )
            if validated.is_update:
                updated += 1
            else:
                added += 1

        _save_graph(instance, graph)
        ctx.set_result(AddEntityResult(added=added, updated=updated))

    result = ctx.result
    assert result is not None
    return result


def service_add_relationships(
    instance: InstanceProtocol,
    relationships: Sequence[RelationshipInstance],
    source: str,
    source_ref: str,
    *,
    _create_receipt: bool = True,
) -> AddRelationshipResult:
    """Add or update relationships in the graph (batch upsert).

    Validates all relationships first, then applies atomically.
    New edges get provenance stamped. Updated edges merge domain properties and
    preserve existing system review metadata.
    Raises DataValidationError on duplicates within the batch or schema violations.
    """
    check_type_ownership(
        instance,
        relationship_types=[relationship.relationship_type for relationship in relationships],
    )
    config = instance.load_config()
    graph = instance.load_graph()

    ctx: MutationReceiptContext[AddRelationshipResult]
    with mutation_receipt(
        instance,
        "add_relationship",
        {"count": len(relationships), "source": source},
        enabled=_create_receipt,
    ) as ctx:
        builder = ctx.builder
        errors: list[str] = []
        batch_seen: set[tuple[str, str, str, str, str]] = set()
        pending = []

        for i, edge in enumerate(relationships, start=1):
            key = (
                edge.from_type,
                edge.from_id,
                edge.to_type,
                edge.to_id,
                edge.relationship_type,
            )
            if key in batch_seen:
                errors.append(
                    f"Edge {i}: duplicate in batch "
                    f"{edge.from_type}:{edge.from_id} "
                    f"-[{edge.relationship_type}]-> "
                    f"{edge.to_type}:{edge.to_id}"
                )
                if builder:
                    builder.record_validation(
                        passed=False, detail={"edge": i, "error": "duplicate in batch"}
                    )
                continue

            try:
                validated = validate_relationship(
                    config,
                    graph,
                    edge.from_type,
                    edge.from_id,
                    edge.relationship_type,
                    edge.to_type,
                    edge.to_id,
                    edge.properties,
                )
            except DataValidationError as exc:
                errors.append(f"Edge {i}: {exc}")
                if builder:
                    builder.record_validation(passed=False, detail={"edge": i, "error": str(exc)})
                continue

            batch_seen.add(key)
            pending.append((validated, edge))
            if builder:
                builder.record_validation(
                    passed=True,
                    detail={
                        "from": f"{edge.from_type}:{edge.from_id}",
                        "to": f"{edge.to_type}:{edge.to_id}",
                        "relationship": edge.relationship_type,
                    },
                )

        if errors:
            raise DataValidationError(
                f"Relationship validation failed with {len(errors)} error(s)",
                errors=errors,
            )

        added = 0
        updated = 0
        for validated, edge in pending:
            apply_relationship(graph, validated, source, source_ref)
            if builder:
                builder.record_relationship_write(
                    edge.from_type,
                    edge.from_id,
                    edge.to_type,
                    edge.to_id,
                    edge.relationship_type,
                    is_update=validated.is_update,
                )
            if validated.is_update:
                updated += 1
            else:
                added += 1

        _save_graph(instance, graph)
        ctx.set_result(AddRelationshipResult(added=added, updated=updated))

    result = ctx.result
    assert result is not None
    return result


def service_ingest(
    instance: InstanceProtocol,
    mapping_name: str,
    file_path: str | None = None,
    data_csv: str | None = None,
    data_json: str | list[dict[str, Any]] | None = None,
    data_ndjson: str | None = None,
    upload_id: str | None = None,
) -> IngestResult:
    """Ingest data through an ingestion mapping.

    Accepts exactly one data source. Raises ConfigError on source violations.
    """
    # Input validation — no receipt for these
    sources = sum(x is not None for x in (file_path, data_csv, data_json, data_ndjson, upload_id))
    if sources == 0:
        raise ConfigError(
            "Provide exactly one of file_path, data_csv, data_json, data_ndjson, or upload_id"
        )
    if sources > 1:
        raise ConfigError(
            "Provide exactly one of file_path, data_csv, data_json, data_ndjson, or upload_id"
        )

    if upload_id is not None:
        raise ConfigError("upload_id is not supported in local mode")

    config = instance.load_config()
    graph = instance.load_graph()

    ctx: MutationReceiptContext[IngestResult]
    with mutation_receipt(
        instance,
        "ingest",
        {"mapping": mapping_name, "config_digest": _config_digest(config)},
    ) as ctx:
        assert ctx.builder is not None
        try:
            if file_path is not None:
                added, updated = ingest_file(config, graph, mapping_name, file_path)
            elif data_csv is not None:
                df = load_data_from_string(data_csv, "csv")
                added, updated = ingest_from_mapping(config, graph, mapping_name, df)
            elif data_ndjson is not None:
                df = load_data_from_string(data_ndjson, "ndjson")
                added, updated = ingest_from_mapping(config, graph, mapping_name, df)
            else:
                assert data_json is not None
                if not isinstance(data_json, str):
                    data_json = _json.dumps(data_json)
                df = load_data_from_string(data_json, "json")
                added, updated = ingest_from_mapping(config, graph, mapping_name, df)
        except CoreError as exc:
            ctx.builder.record_validation(passed=False, detail={"error": str(exc)})
            raise

        ctx.builder.record_ingest_batch(mapping_name, added, updated)
        _save_graph(instance, graph)
        mapping = config.ingestion[mapping_name]
        ctx.set_result(
            IngestResult(
                records_ingested=added,
                records_updated=updated,
                mapping=mapping_name,
                entity_type=mapping.entity_type,
                relationship_type=mapping.relationship_type,
            )
        )

    result = ctx.result
    assert result is not None
    return result
