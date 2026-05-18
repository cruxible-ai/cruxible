"""CLI commands for query, explain, schema, stats, sample, evaluate,
inspect, analysis, and lookups."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal, cast

import click
import yaml

from cruxible_client import CruxibleClient, contracts
from cruxible_core.canonical_views import (
    GovernanceRelationshipView,
    GovernanceView,
    OntologyEntityView,
    OntologyRelationshipView,
    OntologyView,
    OverviewView,
    PendingBucketView,
    QuerySummaryView,
    QueryView,
    WorkflowDependencyView,
    WorkflowProviderSummaryView,
    WorkflowStepSummaryView,
    WorkflowSummaryView,
    WorkflowView,
    render_governance_markdown,
    render_ontology_markdown,
    render_ontology_mermaid,
    render_overview_markdown,
    render_query_markdown,
    render_query_mermaid,
    render_workflow_dependency_mermaid,
    render_workflow_markdown,
    render_workflow_mermaid,
    render_workflow_steps_mermaid,
)
from cruxible_core.cli.commands import _common
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    _entities_from_payload,
    _get_client,
    _lookup_query_param_hints_local,
    _lookup_query_param_hints_server,
    _operation_context,
    _parse_params,
    _print_query_param_hints,
    _require_instance_id,
    _require_local_instance,
    _resolve_decision_record_id,
    console,
    decision_record_option,
    json_option,
)
from cruxible_core.cli.formatting import (
    entities_table,
    inspect_neighbors_table,
    query_definitions_table,
    relationship_table,
    schema_table,
    stats_table,
)
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import handle_errors
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import CoreError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.service import (
    InspectEntityResult,
    service_analyze_feedback,
    service_analyze_outcomes,
    service_describe_query,
    service_evaluate,
    service_explain_receipt,
    service_get_entity,
    service_get_relationship,
    service_get_relationship_lineage,
    service_get_trace,
    service_inspect_entity,
    service_inspect_view,
    service_lint,
    service_list_queries,
    service_query_surface,
    service_sample,
    service_schema,
    service_stats,
)


def _query_definition_payload(query: Any) -> dict[str, Any]:
    return {
        "name": query.name,
        "entry_point": query.entry_point,
        "required_params": list(query.required_params),
        "returns": query.returns,
        "result_shape": getattr(query, "result_shape", "entity"),
        "dedupe": getattr(query, "dedupe", "entity"),
        "description": query.description,
        "example_ids": list(query.example_ids),
    }


def _query_rows_payload(rows: list[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for row in rows:
        if hasattr(row, "model_dump"):
            payload.append(row.model_dump(mode="python"))
        else:
            payload.append(dict(row))
    return payload


def _print_structured_query_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    click.echo(yaml.safe_dump({"results": rows}, sort_keys=False).rstrip())


CanonicalViewName = Literal["ontology", "workflows", "queries", "governance", "overview"]


def _load_inspect_view(
    view: CanonicalViewName,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.inspect_view(instance_id, view, limit=limit),
        lambda instance: service_inspect_view(instance, view, limit=limit),
    )
    return result.payload


def _ontology_view_from_payload(payload: dict[str, Any]) -> OntologyView:
    return OntologyView(
        entity_count=payload["entity_count"],
        relationship_count=payload["relationship_count"],
        governed_relationship_count=payload["governed_relationship_count"],
        entity_types=[
            OntologyEntityView(**entity) for entity in payload.get("entity_types", [])
        ],
        relationships=[
            OntologyRelationshipView(**relationship)
            for relationship in payload.get("relationships", [])
        ],
    )


def _workflow_view_from_payload(payload: dict[str, Any]) -> WorkflowView:
    return WorkflowView(
        workflow_count=payload["workflow_count"],
        workflows=[
            WorkflowSummaryView(
                **{
                    **workflow,
                    "provider_details": [
                        WorkflowProviderSummaryView(**provider)
                        for provider in workflow.get("provider_details", [])
                    ],
                    "steps": [
                        WorkflowStepSummaryView(**step)
                        for step in workflow.get("steps", [])
                    ],
                }
            )
            for workflow in payload.get("workflows", [])
        ],
        dependencies=[
            WorkflowDependencyView(**dependency)
            for dependency in payload.get("dependencies", [])
        ],
    )


def _query_view_from_payload(payload: dict[str, Any]) -> QueryView:
    return QueryView(
        query_count=payload["query_count"],
        queries=[QuerySummaryView(**query) for query in payload.get("queries", [])],
    )


def _governance_view_from_payload(payload: dict[str, Any]) -> GovernanceView:
    return GovernanceView(
        governed_relationship_count=payload["governed_relationship_count"],
        pending_group_count=payload["pending_group_count"],
        total_pending_groups=payload["total_pending_groups"],
        approved_resolution_count=payload["approved_resolution_count"],
        total_resolutions=payload["total_resolutions"],
        pending_truncated=payload["pending_truncated"],
        resolutions_truncated=payload["resolutions_truncated"],
        relationships=[
            GovernanceRelationshipView(**relationship)
            for relationship in payload.get("relationships", [])
        ],
        pending_buckets=[
            PendingBucketView(**bucket) for bucket in payload.get("pending_buckets", [])
        ],
    )


def _overview_view_from_payload(payload: dict[str, Any]) -> OverviewView:
    return OverviewView(
        ontology=_ontology_view_from_payload(payload["ontology"]),
        workflows=_workflow_view_from_payload(payload["workflows"]),
        queries=_query_view_from_payload(payload["queries"]),
        governance=_governance_view_from_payload(payload["governance"]),
    )


def _run_query_command(
    *,
    query_name: str,
    param: tuple[str, ...],
    limit: int | None,
    count_only: bool,
    output_json: bool,
    decision_record_id: str | None,
) -> None:
    params = _parse_params(param)
    resolved_decision_record_id = _resolve_decision_record_id(decision_record_id)
    decision_kwargs = (
        {"decision_record_id": resolved_decision_record_id}
        if resolved_decision_record_id is not None
        else {}
    )
    client = _common._get_client()
    if client is not None:
        effective_limit = 1 if count_only and limit is None else limit
        instance_id = _require_instance_id()
        try:
            result = client.query(
                instance_id,
                query_name,
                params,
                limit=effective_limit,
                **decision_kwargs,
            )
        except CoreError:
            hints = _lookup_query_param_hints_server(
                client,
                instance_id,
                query_name,
            )
            _print_query_param_hints(hints)
            raise
        entity_results = (
            _entities_from_payload(result.results)
            if result.result_shape == "entity"
            else []
        )
        structured_results = [] if result.result_shape == "entity" else list(result.results)
        total = result.total_results
        if output_json:
            items = (
                []
                if count_only
                else (
                    [r.model_dump(mode="python") for r in entity_results]
                    if result.result_shape == "entity"
                    else structured_results
                )
            )
            if limit is not None and not count_only:
                items = items[:limit]
            _emit_json({
                "results": items,
                "total_results": total,
                "steps_executed": result.steps_executed,
                "result_shape": result.result_shape,
                "dedupe": result.dedupe,
                "receipt_id": result.receipt_id,
                "param_hints": (
                    result.param_hints.model_dump(mode="python")
                    if result.param_hints
                    else None
                ),
                "policy_summary": (
                    result.policy_summary.model_dump(mode="python")
                    if hasattr(result, "policy_summary") and result.policy_summary
                    else None
                ),
            })
            return
        click.echo(f"{total} result(s), {result.steps_executed} step(s) executed.")
        if count_only:
            _print_query_param_hints(result.param_hints)
        elif limit is not None and result.truncated:
            if result.result_shape == "entity":
                console.print(entities_table(entity_results, query_name))
            else:
                _print_structured_query_rows(structured_results)
            visible_count = (
                len(entity_results) if result.result_shape == "entity" else len(structured_results)
            )
            click.echo(f"Showing {visible_count} of {total} results (use --limit to adjust).")
        elif result.result_shape == "entity":
            console.print(entities_table(entity_results, query_name))
        else:
            _print_structured_query_rows(structured_results)
        if total == 0 and not count_only:
            _print_query_param_hints(result.param_hints)
        if result.receipt_id:
            click.echo(f"Receipt: {result.receipt_id}")
        return

    instance = CruxibleInstance.load()
    effective_limit = 1 if count_only and limit is None else limit
    try:
        result = service_query_surface(
            instance,
            query_name,
            params,
            limit=effective_limit,
            context=_operation_context(resolved_decision_record_id),
        )
    except CoreError:
        _print_query_param_hints(_lookup_query_param_hints_local(instance, query_name))
        raise

    results = result.results
    entity_results = [row for row in results if isinstance(row, EntityInstance)]
    structured_results = _query_rows_payload(results)
    total = result.total_results
    if output_json:
        items = (
            []
            if count_only
            else (
                [
                    {
                        "entity_type": e.entity_type,
                        "entity_id": e.entity_id,
                        "properties": dict(e.properties),
                    }
                    for e in entity_results
                ]
                if result.result_shape == "entity"
                else structured_results
            )
        )
        _emit_json({
            "results": items,
            "total_results": total,
            "steps_executed": result.steps_executed,
            "result_shape": result.result_shape,
            "dedupe": result.dedupe,
            "receipt_id": result.receipt_id,
            "param_hints": asdict(result.param_hints) if result.param_hints is not None else None,
            "policy_summary": result.policy_summary if result.policy_summary else None,
        })
        return
    click.echo(f"{total} result(s), {result.steps_executed} step(s) executed.")
    if count_only:
        hints = None
        if result.param_hints is not None:
            hints = contracts.QueryParamHints(
                entry_point=result.param_hints.entry_point,
                required_params=result.param_hints.required_params,
                primary_key=result.param_hints.primary_key,
                example_ids=result.param_hints.example_ids,
            )
        _print_query_param_hints(hints)
    elif limit is not None and result.truncated:
        if result.result_shape == "entity":
            console.print(entities_table(entity_results, query_name))
        else:
            _print_structured_query_rows(structured_results)
        click.echo(f"Showing {len(results)} of {total} results (use --limit to adjust).")
    elif result.result_shape == "entity":
        console.print(entities_table(entity_results, query_name))
    else:
        _print_structured_query_rows(structured_results)
    if total == 0 and not count_only and result.param_hints is not None:
        _print_query_param_hints(
            contracts.QueryParamHints(
                entry_point=result.param_hints.entry_point,
                required_params=result.param_hints.required_params,
                primary_key=result.param_hints.primary_key,
                example_ids=result.param_hints.example_ids,
            )
        )
    if result.receipt_id:
        click.echo(f"Receipt: {result.receipt_id}")


@click.group(invoke_without_command=True)
@click.option("--query", "query_name", required=False, help="Named query from config.")
@click.option("--param", multiple=True, help="Query parameter as KEY=VALUE.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Max results to display.")
@click.option("--count", "count_only", is_flag=True, help="Show only summary metadata.")
@decision_record_option
@json_option
@click.pass_context
@handle_errors
def query(
    ctx: click.Context,
    query_name: str | None,
    param: tuple[str, ...],
    limit: int | None,
    count_only: bool,
    decision_record_id: str | None,
    output_json: bool,
) -> None:
    """Execute a named query, or discover the query surfaces on this instance."""
    if ctx.invoked_subcommand is not None:
        return
    if not query_name:
        raise click.UsageError("--query is required unless using a subcommand")
    _run_query_command(
        query_name=query_name,
        param=param,
        limit=limit,
        count_only=count_only,
        output_json=output_json,
        decision_record_id=decision_record_id,
    )


@query.command("list")
@json_option
@handle_errors
def query_list_cmd(output_json: bool) -> None:
    """List named queries with entry points and required params."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_queries(instance_id),
        service_list_queries,
    )
    queries = (
        result.queries
        if isinstance(result, contracts.QueryListResult)
        else cast(list[Any], result)
    )
    payload = [_query_definition_payload(query) for query in queries]
    if output_json:
        _emit_json({"queries": payload})
        return
    console.print(query_definitions_table(payload))


@query.command("describe")
@click.option("--query", "query_name", required=True, help="Named query from config.")
@json_option
@handle_errors
def query_describe_cmd(query_name: str, output_json: bool) -> None:
    """Describe one named query with required params and example IDs."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.describe_query(instance_id, query_name),
        lambda instance: service_describe_query(instance, query_name),
    )
    payload = _query_definition_payload(cast(Any, result))
    if output_json:
        _emit_json(payload)
        return
    click.echo(f"Query: {payload['name']}")
    click.echo(f"Entry point: {payload['entry_point']}")
    click.echo(f"Returns: {payload['returns']}")
    if payload["required_params"]:
        click.echo(f"Required params: {', '.join(payload['required_params'])}")
    if payload["example_ids"]:
        click.echo(f"Example IDs: {', '.join(payload['example_ids'])}")
    if payload["description"]:
        click.echo(f"Description: {payload['description']}")


@click.command()
@click.option("--receipt", "receipt_id", required=True, help="Receipt ID to explain.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "markdown", "mermaid"]),
    default="markdown",
    help="Output format.",
)
@handle_errors
def explain(receipt_id: str, fmt: str) -> None:
    """Explain a query result using its receipt."""
    instance = _require_local_instance("explain")
    result = service_explain_receipt(instance, receipt_id, format=fmt)  # type: ignore[arg-type]
    click.echo(result.content)


@click.command()
@json_option
@handle_errors
def schema(output_json: bool) -> None:
    """Display the config schema for this instance."""
    config = _dispatch_cli_instance(
        lambda client, instance_id: CoreConfig.model_validate(client.schema(instance_id)),
        service_schema,
    )
    if output_json:
        _emit_json(config.model_dump(mode="python"))
        return
    console.print(schema_table(config))


@click.command("stats")
@json_option
@handle_errors
def stats_cmd(output_json: bool) -> None:
    """Display entity and relationship counts for this instance."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.stats(instance_id),
        service_stats,
    )
    entity_count = result.entity_count
    edge_count = result.edge_count
    entity_counts = result.entity_counts
    relationship_counts = result.relationship_counts
    head_snapshot_id = result.head_snapshot_id
    if output_json:
        _emit_json({
            "entity_count": entity_count,
            "edge_count": edge_count,
            "entity_counts": entity_counts,
            "relationship_counts": relationship_counts,
            "head_snapshot_id": head_snapshot_id,
        })
        return
    click.echo(f"Graph: {entity_count} entities, {edge_count} edges")
    if head_snapshot_id:
        click.echo(f"Head snapshot: {head_snapshot_id}")
    console.print(stats_table(entity_counts, relationship_counts))


@click.command()
@click.option("--type", "entity_type", required=True, help="Entity type to sample.")
@click.option("--limit", default=5, help="Number of entities to show.")
@json_option
@handle_errors
def sample(entity_type: str, limit: int, output_json: bool) -> None:
    """Show a sample of entities of a given type."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.sample(instance_id, entity_type, limit=limit),
        lambda instance: service_sample(instance, entity_type, limit=limit),
    )
    entities = (
        _entities_from_payload(result.entities)
        if isinstance(result, contracts.SampleResult)
        else result
    )
    if output_json:
        _emit_json({
            "entities": [e.model_dump(mode="python") for e in entities],
            "entity_type": entity_type,
        })
        return
    console.print(entities_table(entities, entity_type))


@click.command()
@click.option("--limit", default=100, type=int, help="Max findings to show.")
@json_option
@handle_errors
def evaluate(limit: int, output_json: bool) -> None:
    """Assess graph quality: orphans, gaps, violations, unreviewed co-members."""
    report = _dispatch_cli_instance(
        lambda client, instance_id: client.evaluate(
            instance_id,
            max_findings=limit,
        ),
        lambda instance: service_evaluate(
            instance,
            max_findings=limit,
        ),
    )
    findings = (
        report.findings
        if isinstance(report, contracts.EvaluateResult)
        else [finding.model_dump(mode="json") for finding in report.findings]
    )
    entity_count = report.entity_count
    edge_count = report.edge_count
    summary = report.summary
    quality_summary = report.quality_summary
    constraint_summary = getattr(report, "constraint_summary", {})

    if output_json:
        _emit_json({
            "findings": findings,
            "entity_count": entity_count,
            "edge_count": edge_count,
            "summary": summary,
            "quality_summary": quality_summary,
            "constraint_summary": constraint_summary,
        })
        return

    click.echo(f"Graph: {entity_count} entities, {edge_count} edges")
    click.echo(f"Findings: {len(findings)}")
    if summary:
        for category, count in sorted(summary.items()):
            click.echo(f"  {category}: {count}")
    if quality_summary:
        click.echo("Quality checks:")
        for check_name, count in quality_summary.items():
            click.echo(f"  {check_name}: {count}")

    for finding in findings:
        severity = finding["severity"]
        message = finding["message"]
        severity_color = {"error": "red", "warning": "yellow", "info": "blue"}.get(
            severity, "white"
        )
        click.secho(f"  [{severity.upper()}] {message}", fg=severity_color)


@click.command("lint")
@click.option("--max-findings", default=100, type=int, help="Max graph findings to include.")
@click.option(
    "--analysis-limit",
    default=200,
    type=int,
    help="Rows to inspect for feedback and outcome analysis.",
)
@click.option(
    "--min-support",
    default=5,
    type=int,
    help="Minimum support for lint suggestions.",
)
@click.option(
    "--exclude-orphan-type",
    "exclude_orphan_types",
    multiple=True,
    help="Entity type to exclude from orphan checks.",
)
@json_option
@handle_errors
def lint_cmd(
    max_findings: int,
    analysis_limit: int,
    min_support: int,
    exclude_orphan_types: tuple[str, ...],
    output_json: bool,
) -> None:
    """Run the aggregate read-only corpus lint pass."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.lint(
            instance_id,
            max_findings=max_findings,
            analysis_limit=analysis_limit,
            min_support=min_support,
            exclude_orphan_types=list(exclude_orphan_types) or None,
        ),
        lambda instance: service_lint(
            instance,
            max_findings=max_findings,
            analysis_limit=analysis_limit,
            min_support=min_support,
            exclude_orphan_types=list(exclude_orphan_types) or None,
        ),
    )

    payload = (
        result.model_dump(mode="python")
        if isinstance(result, contracts.LintResult)
        else asdict(result)
    )

    if output_json:
        _emit_json(payload)
        if payload["has_issues"]:
            raise SystemExit(1)
        return

    summary = payload["summary"]
    click.echo(f"Lint report for '{payload['config_name']}'")
    click.echo(
        "Summary: "
        f"config_warnings={summary['config_warning_count']}, "
        f"compatibility_warnings={summary['compatibility_warning_count']}, "
        f"graph_findings={summary['evaluation_finding_count']}, "
        f"feedback_reports={summary['feedback_report_count']}, "
        f"feedback_issues={summary['feedback_issue_count']}, "
        f"outcome_reports={summary['outcome_report_count']}, "
        f"outcome_issues={summary['outcome_issue_count']}"
    )

    if payload["config_warnings"]:
        click.echo("Config warnings:")
        for warning in payload["config_warnings"]:
            click.secho(f"  Warning: {warning}", fg="yellow")

    if payload["compatibility_warnings"]:
        click.echo("Compatibility warnings:")
        for warning in payload["compatibility_warnings"]:
            click.secho(f"  Warning: {warning}", fg="yellow")

    evaluation = payload["evaluation"]
    if evaluation["findings"]:
        click.echo("Graph findings:")
        for finding in evaluation["findings"]:
            severity = finding["severity"]
            severity_color = {"error": "red", "warning": "yellow", "info": "blue"}.get(
                severity,
                "white",
            )
            click.secho(
                f"  [{severity.upper()}] {finding['message']}",
                fg=severity_color,
            )

    if payload["feedback_reports"]:
        click.echo("Feedback maintenance suggestions:")
        for report in payload["feedback_reports"]:
            click.echo(f"  {report['relationship_type']}:")
            if report["warnings"]:
                click.echo(f"    warnings={len(report['warnings'])}")
            if report["uncoded_feedback_count"]:
                click.echo(f"    uncoded_feedback={report['uncoded_feedback_count']}")
            for suggestion in report["constraint_suggestions"]:
                click.echo(
                    f"    constraint {suggestion['name']}: {suggestion['rule']} "
                    f"(support={suggestion['support_count']})"
                )
            for suggestion in report["decision_policy_suggestions"]:
                click.echo(
                    f"    policy {suggestion['name']}: {suggestion['applies_to']}/"
                    f"{suggestion['effect']} (support={suggestion['support_count']})"
                )
            for candidate in report["quality_check_candidates"]:
                click.echo(
                    f"    quality_check {candidate['reason_code']} "
                    f"(support={candidate['support_count']})"
                )
            for candidate in report["provider_fix_candidates"]:
                click.echo(
                    f"    provider_fix {candidate['reason_code']} "
                    f"(support={candidate['support_count']})"
                )

    if payload["outcome_reports"]:
        click.echo("Outcome maintenance suggestions:")
        for report in payload["outcome_reports"]:
            click.echo(f"  {report['anchor_type']}:")
            if report["warnings"]:
                click.echo(f"    warnings={len(report['warnings'])}")
            if report["uncoded_outcome_count"]:
                click.echo(f"    uncoded_outcomes={report['uncoded_outcome_count']}")
            for suggestion in report["trust_adjustment_suggestions"]:
                click.echo(
                    f"    trust_adjustment {suggestion['resolution_id']} -> "
                    f"{suggestion['suggested_trust_status']} "
                    f"(support={suggestion['support_count']})"
                )
            for suggestion in report["workflow_review_policy_suggestions"]:
                click.echo(
                    f"    workflow_review {suggestion['name']} "
                    f"(support={suggestion['support_count']})"
                )
            for suggestion in report["query_policy_suggestions"]:
                click.echo(
                    f"    query_policy {suggestion['surface_name']}:{suggestion['outcome_code']} "
                    f"(support={suggestion['support_count']})"
                )
            for candidate in report["provider_fix_candidates"]:
                click.echo(
                    f"    provider_fix {candidate['surface_name']}:{candidate['outcome_code']} "
                    f"(support={candidate['support_count']})"
                )
            if report["debug_packages"]:
                click.echo(f"    debug_packages={len(report['debug_packages'])}")
            if report["workflow_debug_packages"]:
                click.echo(f"    workflow_debug_packages={len(report['workflow_debug_packages'])}")

    if payload["has_issues"]:
        click.secho("Lint found issues.", fg="yellow")
        raise SystemExit(1)

    click.secho("Lint clean.", fg="green")


@click.group("inspect")
def inspect_group() -> None:
    """Inspect entities plus canonical read-only system views."""


@inspect_group.command("ontology")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "markdown", "mermaid"]),
    default="markdown",
    show_default=True,
    help="Output format.",
)
@handle_errors
def inspect_ontology_cmd(fmt: str) -> None:
    """Show the canonical ontology view for the current instance config."""
    payload = _load_inspect_view("ontology")
    if fmt == "json":
        _emit_json(payload)
        return
    view = _ontology_view_from_payload(payload)
    if fmt == "mermaid":
        click.echo(render_ontology_mermaid(view))
        return
    click.echo(render_ontology_markdown(view))


@inspect_group.command("workflows")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(
        ["json", "markdown", "mermaid", "mermaid-dependencies", "mermaid-steps"]
    ),
    default="markdown",
    show_default=True,
    help="Output format.",
)
@handle_errors
def inspect_workflows_cmd(fmt: str) -> None:
    """Show the canonical workflow view for the current instance config."""
    payload = _load_inspect_view("workflows")
    if fmt == "json":
        _emit_json(payload)
        return
    view = _workflow_view_from_payload(payload)
    if fmt == "mermaid":
        click.echo(render_workflow_mermaid(view))
        return
    if fmt == "mermaid-dependencies":
        click.echo(render_workflow_dependency_mermaid(view))
        return
    if fmt == "mermaid-steps":
        click.echo(render_workflow_steps_mermaid(view))
        return
    click.echo(render_workflow_markdown(view))


@inspect_group.command("queries")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "markdown", "mermaid"]),
    default="markdown",
    show_default=True,
    help="Output format.",
)
@handle_errors
def inspect_queries_cmd(fmt: str) -> None:
    """Show the canonical query view for the current instance config."""
    payload = _load_inspect_view("queries")
    if fmt == "json":
        _emit_json(payload)
        return
    view = _query_view_from_payload(payload)
    if fmt == "mermaid":
        click.echo(render_query_mermaid(view))
        return
    click.echo(render_query_markdown(view))


@inspect_group.command("governance")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "markdown"]),
    default="markdown",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=200,
    show_default=True,
    help="Max pending groups and resolutions to inspect.",
)
@handle_errors
def inspect_governance_cmd(fmt: str, limit: int) -> None:
    """Show the canonical governance view for the current instance."""
    payload = _load_inspect_view("governance", limit=limit)
    if fmt == "json":
        _emit_json(payload)
        return
    view = _governance_view_from_payload(payload)
    click.echo(render_governance_markdown(view))


@inspect_group.command("overview")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "markdown"]),
    default="markdown",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=200,
    show_default=True,
    help="Max pending groups and resolutions to inspect.",
)
@handle_errors
def inspect_overview_cmd(fmt: str, limit: int) -> None:
    """Show the generated config overview built from canonical views."""
    payload = _load_inspect_view("overview", limit=limit)
    if fmt == "json":
        _emit_json(payload)
        return
    overview = _overview_view_from_payload(payload)
    click.echo(render_overview_markdown(overview))


@inspect_group.command("entity")
@click.option("--type", "entity_type", required=True, help="Entity type.")
@click.option("--id", "entity_id", required=True, help="Entity ID.")
@click.option(
    "--direction",
    type=click.Choice(["incoming", "outgoing", "both"]),
    default="both",
    show_default=True,
    help="Neighbor traversal direction.",
)
@click.option(
    "--relationship", "relationship_type",
    default=None, help="Optional relationship filter.",
)
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Max neighbors to show.")
@json_option
@handle_errors
def inspect_entity_cmd(
    entity_type: str,
    entity_id: str,
    direction: str,
    relationship_type: str | None,
    limit: int | None,
    output_json: bool,
) -> None:
    """Inspect an entity and its immediate neighbors."""
    def _remote_fetch(
        client: CruxibleClient,
        instance_id: str,
    ) -> tuple[InspectEntityResult, list[dict[str, Any]]]:
        result = client.inspect_entity(
            instance_id,
            entity_type,
            entity_id,
            direction=direction,
            relationship_type=relationship_type,
            limit=limit,
        )
        inspect_result = InspectEntityResult(
            found=result.found,
            entity_type=result.entity_type,
            entity_id=result.entity_id,
            properties=result.properties,
            metadata=result.metadata,
            neighbors=[],
            total_neighbors=result.total_neighbors,
        )
        neighbor_rows = [
            {
                "direction": neighbor.direction,
                "relationship_type": neighbor.relationship_type,
                "edge_key": neighbor.edge_key,
                "properties": neighbor.properties,
                "metadata": neighbor.metadata,
                "entity": neighbor.entity,
            }
            for neighbor in result.neighbors
        ]
        return inspect_result, neighbor_rows

    def _local_fetch(
        instance: CruxibleInstance,
    ) -> tuple[InspectEntityResult, list[dict[str, Any]]]:
        inspect_result = service_inspect_entity(
            instance,
            entity_type,
            entity_id,
            direction=cast(Any, direction),
            relationship_type=relationship_type,
            limit=limit,
        )
        neighbor_rows = [
            {
                "direction": neighbor.direction,
                "relationship_type": neighbor.relationship_type,
                "edge_key": neighbor.edge_key,
                "properties": neighbor.properties,
                "metadata": neighbor.metadata,
                "entity": neighbor.entity.model_dump(mode="json") if neighbor.entity else {},
            }
            for neighbor in inspect_result.neighbors
        ]
        return inspect_result, neighbor_rows

    inspect_result, neighbor_rows = _dispatch_cli_instance(
        _remote_fetch,
        _local_fetch,
    )
    if output_json:
        _emit_json({
            "found": inspect_result.found,
            "entity_type": inspect_result.entity_type,
            "entity_id": inspect_result.entity_id,
            "properties": inspect_result.properties,
            "metadata": inspect_result.metadata,
            "neighbors": neighbor_rows,
            "total_neighbors": inspect_result.total_neighbors,
        })
        return
    if not inspect_result.found:
        click.echo("Not found.")
        return
    console.print(
        entities_table(
            [
                EntityInstance(
                    entity_type=inspect_result.entity_type,
                    entity_id=inspect_result.entity_id,
                    properties=inspect_result.properties,
                    metadata=inspect_result.metadata,
                )
            ],
            inspect_result.entity_type,
        )
    )
    click.echo(f"Neighbors: {inspect_result.total_neighbors}")
    if neighbor_rows:
        console.print(inspect_neighbors_table(neighbor_rows))


@inspect_group.command("trace")
@click.argument("trace_id")
@json_option
@handle_errors
def inspect_trace_cmd(trace_id: str, output_json: bool) -> None:
    """Inspect a full provider execution trace by ID."""
    payload = _dispatch_cli_instance(
        lambda client, instance_id: client.get_trace(instance_id, trace_id),
        lambda instance: service_get_trace(instance, trace_id).model_dump(mode="json"),
    )
    if output_json:
        _emit_json(payload)
        return
    click.echo(yaml.safe_dump(payload, sort_keys=False))


@inspect_group.command("relationship-lineage")
@click.option("--from-type", required=True, help="Source entity type.")
@click.option("--from-id", required=True, help="Source entity ID.")
@click.option("--relationship", "relationship_type", required=True, help="Relationship type.")
@click.option("--to-type", required=True, help="Target entity type.")
@click.option("--to-id", required=True, help="Target entity ID.")
@click.option("--edge-key", default=None, type=int, help="Edge key (multi-edge disambiguation).")
@json_option
@handle_errors
def inspect_relationship_lineage_cmd(
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None,
    output_json: bool,
) -> None:
    """Inspect a relationship's stored provenance lineage."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.get_relationship_lineage(
            instance_id,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
        ),
        lambda instance: service_get_relationship_lineage(
            instance,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
        ),
    )
    if isinstance(result, contracts.RelationshipLineageResult):
        payload = result.model_dump(mode="python", by_alias=True)
    else:
        payload = {
            "found": result.found,
            "relationship": (
                result.relationship.model_dump(mode="python")
                if result.relationship is not None
                else None
            ),
            "provenance": result.provenance,
            "assertion": result.assertion,
            "group": result.group.model_dump(mode="python") if result.group is not None else None,
            "resolution": (
                result.resolution.model_dump(mode="python")
                if result.resolution is not None
                else None
            ),
            "source_workflow_receipt_id": result.source_workflow_receipt_id,
            "source_trace_ids": result.source_trace_ids,
            "warnings": result.warnings,
        }
    if output_json:
        _emit_json(payload)
        return
    click.echo(yaml.safe_dump(payload, sort_keys=False))


@click.command("get-entity")
@click.option("--type", "entity_type", required=True, help="Entity type.")
@click.option("--id", "entity_id", required=True, help="Entity ID.")
@json_option
@handle_errors
def get_entity_cmd(entity_type: str, entity_id: str, output_json: bool) -> None:
    """Look up a specific entity by type and ID."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.get_entity(instance_id, entity_type, entity_id),
        lambda instance: service_get_entity(instance, entity_type, entity_id),
    )
    if isinstance(result, contracts.GetEntityResult):
        if not result.found:
            if output_json:
                _emit_json({"found": False, "entity_type": entity_type, "entity_id": entity_id})
                return
            click.echo("Not found.")
            return
        entity = EntityInstance(
            entity_type=result.entity_type,
            entity_id=result.entity_id,
            properties=result.properties,
            metadata=result.metadata,
        )
    else:
        if result is None:
            if output_json:
                _emit_json({"found": False, "entity_type": entity_type, "entity_id": entity_id})
                return
            click.echo("Not found.")
            return
        entity = result
    if output_json:
        _emit_json({
            "entity_type": entity.entity_type,
            "entity_id": entity.entity_id,
            "properties": dict(entity.properties),
            "metadata": dict(entity.metadata),
        })
        return
    console.print(entities_table([entity], entity_type))


@click.command("get-relationship")
@click.option("--from-type", required=True, help="Source entity type.")
@click.option("--from-id", required=True, help="Source entity ID.")
@click.option("--relationship", required=True, help="Relationship type.")
@click.option("--to-type", required=True, help="Target entity type.")
@click.option("--to-id", required=True, help="Target entity ID.")
@click.option("--edge-key", default=None, type=int, help="Edge key (multi-edge disambiguation).")
@json_option
@handle_errors
def get_relationship_cmd(
    from_type: str,
    from_id: str,
    relationship: str,
    to_type: str,
    to_id: str,
    edge_key: int | None,
    output_json: bool,
) -> None:
    """Look up a specific relationship by its endpoints and type."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.get_relationship(
            instance_id,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
        ),
        lambda instance: service_get_relationship(
            instance,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
        ),
    )
    if isinstance(result, contracts.GetRelationshipResult):
        if not result.found:
            if output_json:
                _emit_json({"found": False, "relationship_type": relationship})
                return
            click.echo("Not found.")
            return
        rel = RelationshipInstance(
            relationship_type=result.relationship_type,
            from_type=result.from_type,
            from_id=result.from_id,
            to_type=result.to_type,
            to_id=result.to_id,
            edge_key=result.edge_key,
            properties=result.properties,
        )
    else:
        if result is None:
            if output_json:
                _emit_json({"found": False, "relationship_type": relationship})
                return
            click.echo("Not found.")
            return
        rel = result
    if output_json:
        _emit_json(rel.model_dump(mode="python"))
        return
    console.print(relationship_table(rel))


@click.command("analyze-feedback")
@click.option("--relationship", "relationship_type", required=True, help="Relationship type.")
@click.option("--limit", default=200, type=click.IntRange(min=1), help="Rows to inspect.")
@click.option(
    "--min-support",
    default=5,
    type=click.IntRange(min=1),
    help="Minimum support for suggestions.",
)
@click.option(
    "--decision-surface-type",
    default=None,
    type=click.Choice(["query", "workflow", "operation"]),
    help="Optional decision surface type filter.",
)
@click.option(
    "--decision-surface-name",
    default=None,
    help="Optional decision surface name filter.",
)
@click.option(
    "--pair",
    "pair_values",
    multiple=True,
    help="Explicit mismatch pair as FROM_PROP=TO_PROP.",
)
@handle_errors
def analyze_feedback_cmd(
    relationship_type: str,
    limit: int,
    min_support: int,
    decision_surface_type: str | None,
    decision_surface_name: str | None,
    pair_values: tuple[str, ...],
) -> None:
    """Analyze structured feedback and print remediation suggestions."""
    property_pairs = []
    for raw_pair in pair_values:
        parts = raw_pair.split("=", 1)
        if len(parts) != 2:
            raise click.BadParameter(f"--pair must be FROM_PROP=TO_PROP, got: {raw_pair}")
        property_pairs.append(
            contracts.PropertyPairInput(from_property=parts[0], to_property=parts[1])
        )

    client = _get_client()
    if client is not None:
        result = client.analyze_feedback(
            _require_instance_id(),
            relationship_type=relationship_type,
            limit=limit,
            min_support=min_support,
            decision_surface_type=decision_surface_type,
            decision_surface_name=decision_surface_name,
            property_pairs=property_pairs or None,
        )
        payload = result.model_dump(mode="json")
    else:
        instance = CruxibleInstance.load()
        result = service_analyze_feedback(
            instance,
            relationship_type,
            limit=limit,
            min_support=min_support,
            decision_surface_type=decision_surface_type,
            decision_surface_name=decision_surface_name,
            property_pairs=[(pair.from_property, pair.to_property) for pair in property_pairs]
            or None,
        )
        payload = asdict(result)

    click.echo(f"Feedback analyzed: {payload['feedback_count']} row(s)")
    if payload["action_counts"]:
        click.echo(
            "Actions: "
            + ", ".join(
                f"{name}={count}" for name, count in sorted(payload["action_counts"].items())
            )
        )
    if payload["reason_code_counts"]:
        click.echo(
            "Reason codes: "
            + ", ".join(
                f"{name}={count}" for name, count in sorted(payload["reason_code_counts"].items())
            )
        )
    if payload["constraint_suggestions"]:
        click.echo("Constraint suggestions:")
        for suggestion in payload["constraint_suggestions"]:
            click.echo(
                f"  {suggestion['name']}: {suggestion['rule']} "
                f"(support={suggestion['support_count']})"
            )
    if payload["decision_policy_suggestions"]:
        click.echo("Decision policy suggestions:")
        for suggestion in payload["decision_policy_suggestions"]:
            click.echo(
                f"  {suggestion['name']}: {suggestion['applies_to']}/{suggestion['effect']} "
                f"(support={suggestion['support_count']})"
            )
    if payload["quality_check_candidates"]:
        click.echo("Quality check candidates:")
        for candidate in payload["quality_check_candidates"]:
            click.echo(
                f"  {candidate['reason_code']}: support={candidate['support_count']}"
            )
    if payload["provider_fix_candidates"]:
        click.echo("Provider fix candidates:")
        for candidate in payload["provider_fix_candidates"]:
            click.echo(
                f"  {candidate['reason_code']}: support={candidate['support_count']}"
            )
    if payload["uncoded_feedback_count"]:
        click.echo(f"Uncoded feedback: {payload['uncoded_feedback_count']}")
        for example in payload["uncoded_examples"]:
            click.echo(f"  {example['feedback_id']}: {example['reason']}")
    for warning in payload["warnings"]:
        click.secho(f"Warning: {warning}", fg="yellow")


@click.command("analyze-outcomes")
@click.option(
    "--anchor-type",
    required=True,
    type=click.Choice(["receipt", "resolution"]),
    help="Outcome anchor type to analyze.",
)
@click.option("--relationship", "relationship_type", default=None, help="Relationship type.")
@click.option("--workflow", "workflow_name", default=None, help="Workflow name filter.")
@click.option("--query", "query_name", default=None, help="Query name filter.")
@click.option(
    "--surface-type",
    default=None,
    type=click.Choice(["query", "workflow", "operation"]),
    help="Explicit surface type filter.",
)
@click.option("--surface-name", default=None, help="Explicit surface name filter.")
@click.option("--limit", default=200, type=click.IntRange(min=1), help="Rows to inspect.")
@click.option(
    "--min-support",
    default=5,
    type=click.IntRange(min=1),
    help="Minimum support for suggestions.",
)
@handle_errors
def analyze_outcomes_cmd(
    anchor_type: str,
    relationship_type: str | None,
    workflow_name: str | None,
    query_name: str | None,
    surface_type: str | None,
    surface_name: str | None,
    limit: int,
    min_support: int,
) -> None:
    """Analyze structured outcomes and print trust/debugging suggestions."""
    client = _get_client()
    if client is not None:
        result = client.analyze_outcomes(
            _require_instance_id(),
            anchor_type=cast(contracts.OutcomeAnchorType, anchor_type),
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            query_name=query_name,
            surface_type=surface_type,
            surface_name=surface_name,
            limit=limit,
            min_support=min_support,
        )
        payload = result.model_dump(mode="json")
    else:
        instance = CruxibleInstance.load()
        result = service_analyze_outcomes(
            instance,
            anchor_type=cast(contracts.OutcomeAnchorType, anchor_type),
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            query_name=query_name,
            surface_type=surface_type,
            surface_name=surface_name,
            limit=limit,
            min_support=min_support,
        )
        payload = asdict(result)

    click.echo(yaml.safe_dump(payload, sort_keys=False))
