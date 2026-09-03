"""Procedure and LineSpec deterministic projection and semantic-address tests."""

from __future__ import annotations

from typing import get_args

import pytest

import cruxible_client.contracts.projection_extensions as projection_extensions_module
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_PATH,
    ProcedureRuntimePolicyV1,
    render_procedure_runtime_policy,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.line_specs import render_line_spec
from cruxible_client.contracts.projection_extensions import ProjectionFactClassification
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.compiler import (
    P2_B0_COMPILER,
    P2_B1_COMPILER,
    P2_B2_COMPILER,
    P2_B4_COMPILER,
    P2_B4_UNIT2_COMPILER,
    P2_C_COMPILER,
    PC_C_COMPILER,
    PC_D_COMPILER,
    PC_E1_COMPILER,
    PC_HR_COMPILER,
    current_compiler_coordinate,
    projection_registry_for_compiler,
)
from cruxible_core.playbill.projection_artifacts import ProjectionFormatError, parse_projection_tree
from tests.test_playbill.test_line_specs import _accepted_procedure, _line


def test_pc_d_projects_procedure_graph_line_and_exact_source_mappings() -> None:
    accepted, _query_pin, _interfaces = _accepted_procedure()
    line, _accepted, _line_interfaces = _line()
    procedure_content = render_procedure(accepted.procedure)
    line_content = render_line_spec(line)

    projection = parse_projection_tree(
        {
            accepted.path: procedure_content,
            "lines/triage-hourly.json": line_content,
        },
        registry=projection_registry_for_compiler(PC_D_COMPILER),
    )

    assert tuple((row.kind, row.identity) for row in projection.envelopes) == (
        ("line", "Line:triage-hourly"),
        ("procedure", "Procedure:triage"),
    )
    schemas = {fact.schema_id for fact in projection.semantic_facts}
    assert {
        "playbill.line.source_mapping",
        "playbill.line.spec",
        "playbill.procedure.definition",
        "playbill.procedure.graph",
        "playbill.procedure.source_mapping",
    } <= schemas
    source_mappings = tuple(
        fact
        for fact in projection.semantic_facts
        if fact.schema_id == "playbill.procedure.source_mapping"
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
    assert P2_B2_COMPILER != P2_B4_COMPILER
    assert P2_B4_COMPILER != P2_B4_UNIT2_COMPILER
    assert current_compiler_coordinate() == P2_B4_UNIT2_COMPILER
    assert (
        projection_registry_for_compiler(PC_HR_COMPILER).supports(
            "playbill.provider.runtime",
            1,
            classification="semantic",
        )
        is False
    )
    assert (
        projection_registry_for_compiler(P2_B1_COMPILER).supports(
            "playbill.provider.runtime",
            1,
            classification="semantic",
        )
        is True
    )
    assert (
        projection_registry_for_compiler(P2_C_COMPILER).supports(
            "playbill.procedure_mandate.authority",
            1,
            classification="semantic",
        )
        is True
    )
    assert (
        projection_registry_for_compiler(PC_C_COMPILER).supports(
            "playbill.procedure.definition",
            1,
            classification="semantic",
        )
        is False
    )
    assert (
        projection_registry_for_compiler(PC_D_COMPILER).supports(
            "playbill.procedure.definition",
            1,
            classification="semantic",
        )
        is True
    )
    assert (
        projection_registry_for_compiler(PC_E1_COMPILER).supports(
            "playbill.procedure.resolution_activation",
            1,
            classification="semantic",
        )
        is True
    )


def test_runtime_policy_artifact_kind_begins_at_the_p2_b0_compiler() -> None:
    tree = {
        PROCEDURE_RUNTIME_POLICY_PATH: render_procedure_runtime_policy(
            ProcedureRuntimePolicyV1(provider_output_bytes_cap=1_048_576)
        )
    }

    with pytest.raises(
        ProjectionFormatError,
        match="does not recognize ProcedureRuntimePolicy",
    ):
        parse_projection_tree(tree, registry=projection_registry_for_compiler(PC_E1_COMPILER))

    parsed = parse_projection_tree(tree, registry=projection_registry_for_compiler(P2_B0_COMPILER))
    assert [(row.kind, row.identity) for row in parsed.envelopes] == [
        ("procedure-runtime-policy", "ProcedureRuntimePolicy:instance")
    ]


def test_replay_registry_copies_the_authoritative_runtime_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = projection_extensions_module.playbill_runtime_extension_registry()

    def classification_listing_is_not_a_copy(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("registry copy must not enumerate known classifications")

    with monkeypatch.context() as guarded:
        guarded.setattr(
            projection_extensions_module,
            "playbill_runtime_extension_registry",
            lambda: runtime,
        )
        guarded.setattr(
            projection_extensions_module.ProjectionExtensionRegistry,
            "declarations",
            classification_listing_is_not_a_copy,
        )
        replay = projection_extensions_module.playbill_replay_extension_registry()

    for classification in get_args(ProjectionFactClassification):
        assert replay.declarations(classification) == runtime.declarations(classification)
    assert replay.supports_artifact_kind("procedure-runtime-policy")
