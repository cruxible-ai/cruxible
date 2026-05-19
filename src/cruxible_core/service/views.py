"""Service operations for reusable read/render surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

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
    RenderWikiPageResult,
    RenderWikiResult,
)
from cruxible_core.wiki import WikiOptions, build_wiki_pages
from cruxible_core.wiki.generator import WikiScope, parse_subject_ref

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
                "entry_point": query.entry_point,
                "required_params": list(query.required_params),
                "returns": query.returns,
                "result_shape": query.result_shape,
                "dedupe": query.dedupe,
                "relationship_state": query.relationship_state,
                "allow_relationship_state_override": query.allow_relationship_state_override,
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
                "entry_point": query.entry_point,
                "required_params": list(query.required_params),
                "returns": query.returns,
                "result_shape": query.result_shape,
                "dedupe": query.dedupe,
                "relationship_state": query.relationship_state,
                "allow_relationship_state_override": query.allow_relationship_state_override,
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
        pending_groups=groups.groups,
        pending_total=groups.total,
        resolutions=resolutions.resolutions,
        resolution_total=resolutions.total,
    )


def service_render_wiki(
    instance: InstanceProtocol,
    *,
    focus: list[str] | None = None,
    include_types: list[str] | None = None,
    scope: str | None = None,
    max_per_type: int = 50,
    all_subjects: bool = False,
) -> RenderWikiResult:
    """Build wiki pages for a governed instance and return them as payloads."""
    options = WikiOptions(
        output_dir=Path("."),
        focus=tuple(parse_subject_ref(raw) for raw in (focus or [])),
        include_types=tuple(include_types or []),
        scope=cast(WikiScope, scope or ("all" if all_subjects else "evidence")),
        max_per_type=max_per_type,
        all_subjects=all_subjects,
    )
    pages = build_wiki_pages(instance, options)
    serialized_pages = [
        RenderWikiPageResult(path=path.as_posix(), content=content)
        for path, content in sorted(pages.items())
    ]
    return RenderWikiResult(pages=serialized_pages, page_count=len(serialized_pages))


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
    relationship: str | None = None,
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
    for edge in graph.iter_edges(relationship_type=relationship):
        if exclude_rejected:
            if (
                edge.get("metadata", {})
                .get("assertion", {})
                .get("review", {})
                .get("status")
                == "rejected"
            ):
                continue
        rows.append({
            "from_type": edge["from_type"],
            "from_id": edge["from_id"],
            "to_type": edge["to_type"],
            "to_id": edge["to_id"],
            "relationship_type": edge["relationship_type"],
            "edge_key": edge["edge_key"],
            "properties_json": json.dumps(edge["properties"], sort_keys=True),
            "metadata_json": json.dumps(edge.get("metadata", {}), sort_keys=True),
        })
    return ExportEdgesResult(fieldnames=fieldnames, rows=rows, count=len(rows))
