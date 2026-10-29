"""Procedure and LineSpec deterministic projection and semantic-address tests."""

from __future__ import annotations

from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_PATH,
    ProcedureRuntimePolicy,
    render_procedure_runtime_policy,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.line_specs import render_line_spec
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.compiler.compiler import PROJECTION_REGISTRY
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from tests.test_procedures.test_line_specs import _accepted_procedure, _line


def test_projects_procedure_graph_line_and_exact_source_mappings() -> None:
    accepted, _query_pin, _interfaces = _accepted_procedure()
    line, _accepted, _line_interfaces = _line()
    procedure_content = render_procedure(accepted.procedure)
    line_content = render_line_spec(line)

    projection = parse_projection_tree(
        {
            accepted.path: procedure_content,
            "lines/triage-hourly.json": line_content,
        },
    )

    assert tuple((row.kind, row.identity) for row in projection.envelopes) == (
        ("line", "Line:triage-hourly"),
        ("procedure", "Procedure:triage"),
    )
    schemas = {fact.schema_id for fact in projection.semantic_facts}
    assert {
        "cruxible.line.source_mapping",
        "cruxible.line.spec",
        "cruxible.procedure.definition",
        "cruxible.procedure.graph",
        "cruxible.procedure.source_mapping",
    } <= schemas
    source_mappings = tuple(
        fact
        for fact in projection.semantic_facts
        if fact.schema_id == "cruxible.procedure.source_mapping"
    )
    assert {fact.fact_key for fact in source_mappings} == {
        "unit",
        "node.read",
        "node.shape",
        "arm.0002",
    }
    node_mapping = next(fact for fact in source_mappings if fact.fact_key == "node.read")
    assert node_mapping.value["subject"] == SemanticAddress.procedure_node(
        accepted.path, "read"
    ).model_dump(mode="json")
    span = node_mapping.value["spans"][0]
    assert span["start_byte"] < span["end_byte"] <= len(procedure_content)


def test_procedure_semantic_identity_is_stable_across_exact_coordinates() -> None:
    accepted, _query_pin, _interfaces = _accepted_procedure()
    before = SemanticAddress.procedure_node(accepted.path, "read")
    after = SemanticAddress.procedure_node(accepted.path, "read")

    assert before == after
    for schema_id in (
        "cruxible.provider.runtime",
        "cruxible.procedure_mandate.authority",
        "cruxible.procedure.definition",
        "cruxible.procedure.resolution_activation",
    ):
        assert PROJECTION_REGISTRY.supports(schema_id, 1, classification="semantic")


def test_the_runtime_policy_and_its_resource_budgets_project() -> None:
    for policy in (
        ProcedureRuntimePolicy(provider_output_bytes_cap=1_048_576),
        ProcedureRuntimePolicy(
            provider_output_bytes_cap=64 * 1024 * 1024,
            result_bytes_cap=16 * 1024 * 1024,
            repeat_attempts_cap=100,
        ),
    ):
        parsed = parse_projection_tree(
            {PROCEDURE_RUNTIME_POLICY_PATH: render_procedure_runtime_policy(policy)}
        )
        assert [(row.kind, row.identity) for row in parsed.envelopes] == [
            ("procedure-runtime-policy", "ProcedureRuntimePolicy:instance")
        ]
