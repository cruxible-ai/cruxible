"""Service operations for reusable read/render surfaces."""

from __future__ import annotations

import json
from typing import Literal

from cruxible_core.canonical_views import (
    GovernanceView,
    build_governance_view,
    build_ontology_view,
    build_overview_view,
    build_query_view,
    build_workflow_view,
    canonical_view_payload,
)
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.query.relationship_state import relationship_review_is_rejected
from cruxible_core.receipt import serializer
from cruxible_core.service.groups import service_list_groups, service_list_resolutions
from cruxible_core.service.queries import (
    service_get_receipt,
    service_list_queries,
    service_schema,
    service_stats,
)
from cruxible_core.service.types import (
    CanonicalViewResult,
    ExportEdgesResult,
    ReceiptExplanationResult,
)

CanonicalViewName = Literal["ontology", "workflows", "queries", "governance", "overview"]
ReceiptExplanationFormat = Literal["json", "markdown", "mermaid"]


def service_inspect_view(
    instance: InstanceProtocol,
    view: CanonicalViewName,
    *,
    limit: int = 200,
) -> CanonicalViewResult:
    """Build a canonical structured inspect view for an instance."""
    config = service_schema(instance)
    if view == "ontology":
        stats = service_stats(instance)
        payload = canonical_view_payload(
            build_ontology_view(config, relationship_counts=stats.relationship_counts)
        )
        return CanonicalViewResult(view=view, payload=payload)
    if view == "workflows":
        return CanonicalViewResult(
            view=view,
            payload=canonical_view_payload(build_workflow_view(config)),
        )
    if view == "queries":
        query_infos = [
            {
                "name": query.name,
                "mode": query.mode,
                "entry_point": query.entry_point,
                "required_params": list(query.required_params),
                "returns": query.returns,
                "result_shape": query.result_shape,
                "dedupe": query.dedupe,
                "relationship_state": query.relationship_state,
                "allow_relationship_state_override": query.allow_relationship_state_override,
                "select": query.select,
                "order_by": list(query.order_by),
                "limit": query.limit,
                "max_paths": query.max_paths,
                "max_paths_per_result": query.max_paths_per_result,
                "description": query.description,
                "example_ids": list(query.example_ids),
            }
            for query in service_list_queries(instance)
        ]
        return CanonicalViewResult(
            view=view,
            payload=canonical_view_payload(build_query_view(config, query_infos=query_infos)),
        )
    if view == "governance":
        return CanonicalViewResult(
            view=view,
            payload=canonical_view_payload(_build_governance(instance, limit=limit)),
        )
    if view == "overview":
        stats = service_stats(instance)
        ontology = build_ontology_view(
            config,
            relationship_counts=stats.relationship_counts,
        )
        workflows = build_workflow_view(config)
        query_infos = [
            {
                "name": query.name,
                "mode": query.mode,
                "entry_point": query.entry_point,
                "required_params": list(query.required_params),
                "returns": query.returns,
                "result_shape": query.result_shape,
                "dedupe": query.dedupe,
                "relationship_state": query.relationship_state,
                "allow_relationship_state_override": query.allow_relationship_state_override,
                "select": query.select,
                "order_by": list(query.order_by),
                "limit": query.limit,
                "max_paths": query.max_paths,
                "max_paths_per_result": query.max_paths_per_result,
                "description": query.description,
                "example_ids": list(query.example_ids),
            }
            for query in service_list_queries(instance)
        ]
        queries = build_query_view(config, query_infos=query_infos)
        governance = _build_governance(instance, limit=limit)
        return CanonicalViewResult(
            view=view,
            payload=canonical_view_payload(
                build_overview_view(
                    ontology=ontology,
                    workflows=workflows,
                    queries=queries,
                    governance=governance,
                )
            ),
        )
    raise ConfigError(f"Unsupported inspect view '{view}'")


def _build_governance(instance: InstanceProtocol, *, limit: int) -> GovernanceView:
    config = service_schema(instance)
    groups = service_list_groups(instance, status="pending_review", limit=limit)
    resolutions = service_list_resolutions(instance, limit=limit)
    return build_governance_view(
        config,
        pending_groups=groups.items,
        pending_total=groups.total,
        resolutions=resolutions.items,
        resolution_total=resolutions.total,
    )


def service_explain_receipt(
    instance: InstanceProtocol,
    receipt_id: str,
    *,
    format: ReceiptExplanationFormat = "markdown",
) -> ReceiptExplanationResult:
    """Render a stored receipt in a user-facing explanation format."""
    receipt = service_get_receipt(instance, receipt_id)
    if format == "json":
        content = serializer.to_json(receipt)
    elif format == "mermaid":
        content = serializer.to_mermaid(receipt)
    else:
        content = serializer.to_markdown(receipt)
    return ReceiptExplanationResult(receipt_id=receipt_id, format=format, content=content)


def service_export_edges(
    instance: InstanceProtocol,
    *,
    relationship_type: str | None = None,
    exclude_rejected: bool = False,
) -> ExportEdgesResult:
    """Build CSV-ready edge export rows."""
    fieldnames = [
        "from_type",
        "from_id",
        "to_type",
        "to_id",
        "relationship_type",
        "edge_key",
        "properties_json",
        "metadata_json",
    ]
    graph = instance.load_graph()
    rows: list[dict[str, object]] = []
    for edge in graph.iter_edges(relationship_type=relationship_type):
        if exclude_rejected and relationship_review_is_rejected(edge.get("metadata", {})):
            continue
        rows.append(
            {
                "from_type": edge["from_type"],
                "from_id": edge["from_id"],
                "to_type": edge["to_type"],
                "to_id": edge["to_id"],
                "relationship_type": edge["relationship_type"],
                "edge_key": edge["edge_key"],
                "properties_json": json.dumps(edge["properties"], sort_keys=True),
                "metadata_json": json.dumps(edge.get("metadata", {}), sort_keys=True),
            }
        )
    return ExportEdgesResult(fieldnames=fieldnames, rows=rows, count=len(rows))
