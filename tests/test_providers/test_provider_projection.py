"""Provider/interface, graph-v6 Procedure, and Line projection facts."""

from __future__ import annotations

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.line_specs import (
    LineSpec,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.provider_interfaces import render_provider_interface
from cruxible_client.contracts.providers import render_provider
from cruxible_core.compiler.compiler import (
    artifact_kinds_for_compiler,
    current_compiler_coordinate,
    projection_registry_for_compiler,
)
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from tests.core_support._p2b1_support import (
    accepted_interface,
    accepted_provider,
)
from tests.test_providers.test_provider_invocation_journal import _accepted_one_provider


def test_projects_only_governed_provider_runtime_and_interface_authority() -> None:
    interface = accepted_interface()
    provider = accepted_provider()
    procedure = _accepted_one_provider()
    procedure_pin = ArtifactPin(
        role="procedure",
        target=procedure.procedure.identity,
        artifact_digest=procedure.artifact_digest,
    )
    line = LineSpec(
        identity=ArtifactIdentity(kind="Line", name="provider-call-line"),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={},
        max_authority="observe",
        budgets={},
        epsilon={"$decimal": "0"},
        pins=(procedure_pin,),
    )
    compiler = current_compiler_coordinate()

    projection = parse_projection_tree(
        {
            interface.path: render_provider_interface(interface.registration),
            provider.path: render_provider(provider.provider),
            procedure.path: render_procedure(procedure.procedure),
            line_spec_path(line.identity.name): render_line_spec(line),
        },
        registry=projection_registry_for_compiler(compiler),
        artifact_kinds=artifact_kinds_for_compiler(compiler),
    )

    schemas = {fact.schema_id for fact in projection.semantic_facts}
    assert {
        "cruxible.provider.runtime",
        "cruxible.provider.implementations",
        "cruxible.provider_interface.registration",
        "cruxible.provider_interface.vocabulary",
        "cruxible.provider_interface.classifier",
    } <= schemas
    graph = next(
        fact for fact in projection.semantic_facts if fact.schema_id == "cruxible.procedure.graph"
    )
    assert graph.fact_key == "graph_v6"
    projected_line = next(
        fact for fact in projection.semantic_facts if fact.schema_id == "cruxible.line.spec"
    )
    assert projected_line.value["line"]["procedure"] == procedure_pin.model_dump(mode="json")
    assert "provider_implementation_closures" not in projected_line.value["line"]
