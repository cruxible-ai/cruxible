"""Rich table formatters for CLI output."""

from __future__ import annotations

import json
from typing import Any

from rich.table import Table

from cruxible_core.config.schema import CoreConfig
from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
)
from cruxible_core.group.types import CandidateGroup, CandidateMember, GroupResolution
from cruxible_core.temporal import format_datetime


def entities_table(entities: list[EntityInstance], entity_type: str) -> Table:
    """Build a Rich table for a list of entities.

    Surfaces each entity's lifecycle status (``live`` unless retired/superseded)
    so by-id ``entity get`` reveals the canonical lifecycle state of an
    entity even though that surface intentionally bypasses live-only gating.
    """
    table = Table(title=f"{entity_type} entities")
    table.add_column("ID", style="cyan")
    table.add_column("Lifecycle")
    table.add_column("Properties")

    for e in entities:
        props = ", ".join(f"{k}={v}" for k, v in e.properties.items())
        table.add_row(
            e.entity_id,
            e.metadata.lifecycle_status(),
            props,
        )

    return table


def entity_change_history_table(items: list[Any]) -> Table:
    """Build a Rich table for receipt-derived entity property changes."""
    table = Table(title="Entity Change History")
    table.add_column("Entity", style="cyan", overflow="fold")
    table.add_column("Kind")
    table.add_column("Changes", overflow="fold")
    table.add_column("Changed At")
    table.add_column("Receipt", overflow="fold")
    table.add_column("Operation")
    table.add_column("Actor", overflow="fold")

    for item in items:
        actor_context = item.actor_context or {}
        actor = str(actor_context.get("actor_id") or "")
        table.add_row(
            f"{item.entity_type}:{item.entity_id}",
            item.change_kind,
            _format_property_changes(item.property_changes),
            format_datetime(item.changed_at),
            item.receipt_id,
            item.operation_type,
            actor,
        )

    return table


def _format_property_changes(changes: list[Any]) -> str:
    if not changes:
        return ""
    rendered: list[str] = []
    for change in changes:
        rendered.append(
            f"{change.property}: "
            f"{_format_change_value(change.from_value)} -> "
            f"{_format_change_value(change.to_value)}"
        )
    return "\n".join(rendered)


def _format_change_value(value: Any) -> str:
    if value is None:
        return "null"
    return json.dumps(value, sort_keys=True, default=str)


def receipts_table(receipts: list[dict[str, Any]]) -> Table:
    """Build a Rich table for receipt summaries."""
    table = Table(title="Receipts")
    table.add_column("ID", style="cyan")
    table.add_column("Type")
    table.add_column("Query")
    table.add_column("Created At")
    table.add_column("Duration (ms)", justify="right")

    for r in receipts:
        op_type = r.get("operation_type", "query")
        query_col = r["query_name"] if r["query_name"] else op_type
        table.add_row(
            r["receipt_id"],
            op_type,
            query_col,
            r["created_at"],
            f"{r['duration_ms']:.1f}",
        )

    return table


def feedback_table(records: list[FeedbackRecord]) -> Table:
    """Build a Rich table for feedback records."""
    table = Table(title="Feedback")
    table.add_column("ID", style="cyan")
    table.add_column("Receipt")
    table.add_column("Action")
    table.add_column("Target")
    table.add_column("Reason")

    for r in records:
        t = r.target
        target_str = f"{t.from_type}:{t.from_id}:{t.relationship_type}:{t.to_type}:{t.to_id}"
        if t.edge_key is not None:
            target_str = f"{target_str}:{t.edge_key}"
        table.add_row(
            r.feedback_id,
            r.receipt_id,
            r.action,
            target_str,
            r.reason,
        )

    return table


def outcomes_table(records: list[OutcomeRecord]) -> Table:
    """Build a Rich table for outcome records."""
    table = Table(title="Outcomes")
    table.add_column("ID", style="cyan")
    table.add_column("Anchor")
    table.add_column("Outcome")
    table.add_column("Code")
    table.add_column("Source")
    table.add_column("Created At")

    for r in records:
        anchor = f"{r.anchor_type}:{r.anchor_id or r.receipt_id}"
        table.add_row(
            r.outcome_id,
            anchor,
            r.outcome,
            r.outcome_code or "",
            r.source,
            str(r.created_at),
        )

    return table


def relationship_table(rel: RelationshipInstance) -> Table:
    """Build a Rich table for a single relationship."""
    table = Table(title=f"{rel.relationship_type} relationship")
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    table.add_row("From", f"{rel.from_type}:{rel.from_id}")
    table.add_row("To", f"{rel.to_type}:{rel.to_id}")
    table.add_row("Type", rel.relationship_type)
    if rel.edge_key is not None:
        table.add_row("Edge Key", str(rel.edge_key))
    for k, v in rel.properties.items():
        table.add_row(f"  {k}", str(v))

    return table


def edges_table(edges: list[dict[str, Any]]) -> Table:
    """Build a Rich table for a list of edges."""
    table = Table(title="Edges")
    table.add_column("From", style="cyan")
    table.add_column("To", style="cyan")
    table.add_column("Relationship")
    table.add_column("Edge Key", justify="right")
    table.add_column("Properties")

    for e in edges:
        from_label = f"{e['from_type']}:{e['from_id']}"
        to_label = f"{e['to_type']}:{e['to_id']}"
        props = e.get("properties", {})
        props_str = ", ".join(f"{k}={v}" for k, v in props.items())
        table.add_row(
            from_label,
            to_label,
            e.get("relationship_type", ""),
            str(e.get("edge_key", "")),
            props_str,
        )

    return table


def inspect_neighbors_table(neighbors: list[dict[str, Any]]) -> Table:
    """Build a Rich table for entity-neighbor inspection results."""
    table = Table(title="Neighbors")
    table.add_column("Direction")
    table.add_column("Relationship")
    table.add_column("Neighbor", style="cyan")
    table.add_column("Edge Key", justify="right")
    table.add_column("Properties")

    for neighbor in neighbors:
        entity = neighbor.get("entity", {})
        label = f"{entity.get('entity_type', '')}:{entity.get('entity_id', '')}"
        props = neighbor.get("properties", {})
        props_str = ", ".join(f"{k}={v}" for k, v in props.items())
        table.add_row(
            str(neighbor.get("direction", "")),
            str(neighbor.get("relationship_type", "")),
            label,
            str(neighbor.get("edge_key", "")),
            props_str,
        )

    return table


def stats_table(
    entity_counts: dict[str, int],
    relationship_counts: dict[str, int],
) -> Table:
    """Build a Rich table for graph counts by type."""
    table = Table(title="Graph Stats")
    table.add_column("Section", style="cyan")
    table.add_column("Name")
    table.add_column("Count", justify="right")

    for name, count in sorted(entity_counts.items()):
        table.add_row("Entity", name, str(count))
    for name, count in sorted(relationship_counts.items()):
        table.add_row("Relationship", name, str(count))
    return table


def query_definitions_table(queries: list[dict[str, Any]]) -> Table:
    """Build a Rich table for named-query discovery surfaces."""
    table = Table(title="Named Queries", expand=True)
    table.add_column("Name", style="cyan", overflow="fold", min_width=18)
    table.add_column("Entry", overflow="fold")
    table.add_column("Params", overflow="fold")
    table.add_column("Returns", overflow="fold")
    table.add_column("State", overflow="fold")
    table.add_column("Description", overflow="fold", ratio=2)

    for query in queries:
        params = ", ".join(query.get("required_params", []))
        table.add_row(
            str(query.get("name", "")),
            str(query.get("entry_point", "")),
            params,
            str(query.get("returns", "")),
            str(query.get("relationship_state", "live")),
            str(query.get("description") or ""),
        )
    return table


def groups_table(groups: list[CandidateGroup]) -> Table:
    """Build a Rich table for a list of candidate groups."""
    table = Table(title="Candidate Groups", expand=True)
    table.add_column("Group ID", style="cyan", overflow="fold", min_width=12, max_width=14)
    table.add_column("Relationship", overflow="fold", min_width=18, max_width=24)
    table.add_column("Status", overflow="fold", min_width=12, max_width=14)
    table.add_column("Thesis", overflow="fold", ratio=2, min_width=18)

    for g in groups:
        table.add_row(
            g.group_id,
            g.relationship_type,
            g.status,
            g.thesis_text or "",
        )

    return table


def group_detail_table(
    group: CandidateGroup,
    members: list[CandidateMember],
    resolution: GroupResolution | None = None,
) -> Table:
    """Build a Rich table showing group details and members."""
    table = Table(title=f"Group {group.group_id}")
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    table.add_row("Group ID", group.group_id)
    table.add_row("Relationship", group.relationship_type)
    table.add_row("Status", group.status)
    table.add_row("Priority", group.review_priority)
    table.add_row("Signature", group.signature[:16] + "...")
    table.add_row("Pending Version", str(group.pending_version))
    table.add_row("Members", str(group.member_count))
    if group.thesis_text:
        table.add_row("Thesis", group.thesis_text)
    if resolution is not None:
        table.add_row("Resolution", resolution.action)
        table.add_row("Trust Status", resolution.trust_status)

    for m in members:
        edge = f"{m.from_type}:{m.from_id} → {m.to_type}:{m.to_id}"
        signals_str = ", ".join(f"{s.signal_source}={s.signal}" for s in m.signals)
        table.add_row("  Member", f"{edge}  [{signals_str}]")

    return table


def resolutions_table(resolutions: list[GroupResolution]) -> Table:
    """Build a Rich table for group resolutions."""
    table = Table(title="Group Resolutions")
    table.add_column("Resolution ID", style="cyan", no_wrap=True)
    table.add_column("Relationship")
    table.add_column("Action")
    table.add_column("Trust Status")
    table.add_column("Resolved By")
    table.add_column("Resolved At")

    for r in resolutions:
        table.add_row(
            r.resolution_id,
            r.relationship_type,
            r.action,
            r.trust_status,
            r.resolved_by,
            format_datetime(r.resolved_at) or "",
        )

    return table


def source_artifacts_table(items: list[Any]) -> Table:
    """Build a Rich table for registered source artifact summaries."""
    table = Table(title="Source Artifacts", expand=True)
    table.add_column("Artifact ID", style="cyan", overflow="fold", min_width=11)
    table.add_column("Kind", overflow="fold", min_width=4)
    table.add_column("Label", overflow="fold", min_width=8)
    table.add_column("Retention", overflow="fold", min_width=13)
    table.add_column("Chunks", justify="right", overflow="fold", min_width=6)
    table.add_column("Registered", overflow="fold", min_width=10)

    for item in items:
        table.add_row(
            item.source_artifact_id,
            item.kind,
            item.label or "",
            item.retention,
            str(item.chunk_count),
            item.registered_at,
        )
    return table


def source_artifact_chunks_table(chunks: list[Any]) -> Table:
    """Build a Rich table for source artifact chunk summaries."""
    table = Table(title="Source Artifact Chunks", expand=True)
    table.add_column("Chunk ID", style="cyan", overflow="fold", min_width=12)
    table.add_column("Heading Path", overflow="fold", ratio=2)
    table.add_column("Block Type")
    table.add_column("Lines", justify="right")

    for chunk in chunks:
        heading_path = " > ".join(chunk.heading_path)
        table.add_row(
            chunk.chunk_id,
            heading_path,
            chunk.block_type,
            f"{chunk.line_start}-{chunk.line_end}",
        )
    return table


def schema_table(config: CoreConfig) -> Table:
    """Build a Rich table showing the config schema."""
    table = Table(title=f"Schema: {config.name}")
    table.add_column("Section", style="cyan")
    table.add_column("Name")
    table.add_column("Details")

    for name, et in config.entity_types.items():
        pk = et.get_primary_key() or "-"
        props = ", ".join(
            _format_property_name(prop_name, prop) for prop_name, prop in et.properties.items()
        )
        table.add_row("Entity", name, f"pk={pk}  props=[{props}]")

    for rel in config.relationships:
        prop_names = ", ".join(
            _format_property_name(prop_name, prop) for prop_name, prop in rel.properties.items()
        )
        details = f"{rel.from_entity} -> {rel.to_entity}  ({rel.cardinality})"
        if prop_names:
            details = f"{details}  props=[{prop_names}]"
        table.add_row(
            "Relationship",
            rel.name,
            details,
        )

    for name, q in config.named_queries.items():
        steps = len(q.traversal)
        table.add_row("Query", name, f"entry={q.entry_point}  steps={steps}")

    for name, contract in config.contracts.items():
        fields = ", ".join(
            _format_property_name(field_name, field_schema)
            for field_name, field_schema in contract.fields.items()
        )
        table.add_row("Contract", name, f"fields=[{fields}]")

    return table


def _format_property_name(name: str, schema: Any) -> str:
    if getattr(schema, "json_schema", None) is not None:
        return f"{name}{{json}}"
    return name
