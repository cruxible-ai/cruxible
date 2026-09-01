"""P2-B1 Provider/interface, graph-v4, and Line-v2 projection facts."""

from __future__ import annotations

from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.line_specs import render_line_spec
from cruxible_client.contracts.provider_interfaces import render_provider_interface
from cruxible_client.contracts.providers import render_provider
from cruxible_core.playbill.compiler import (
    P2_B1_COMPILER,
    artifact_kinds_for_compiler,
    projection_registry_for_compiler,
)
from cruxible_core.playbill.projection_artifacts import parse_projection_tree
from tests.test_playbill._p2b1_support import (
    accepted_interface,
    accepted_provider,
)
from tests.test_playbill.test_graph_v4_provider_closure import (
    _accepted_procedure,
    _line,
)


def test_p2_b1_projects_only_governed_provider_runtime_and_interface_authority() -> None:
    interface = accepted_interface()
    provider = accepted_provider()
    procedure = _accepted_procedure()
    line = _line()
    line_spec_path = "lines/provider-v4-line.json"

    projection = parse_projection_tree(
        {
            interface.path: render_provider_interface(interface.registration),
            provider.path: render_provider(provider.provider),
            procedure.path: render_procedure(procedure.procedure),
            line_spec_path: render_line_spec(line),
        },
        registry=projection_registry_for_compiler(P2_B1_COMPILER),
        artifact_kinds=artifact_kinds_for_compiler(P2_B1_COMPILER),
    )

    schemas = {fact.schema_id for fact in projection.semantic_facts}
    assert {
        "playbill.provider.runtime",
        "playbill.provider.implementations",
        "playbill.provider_interface.registration",
        "playbill.provider_interface.vocabulary",
        "playbill.provider_interface.classifier",
    } <= schemas
    graph = next(
        fact for fact in projection.semantic_facts if fact.schema_id == "playbill.procedure.graph"
    )
    assert graph.fact_key == "graph_v4"
    projected_line = next(
        fact for fact in projection.semantic_facts if fact.schema_id == "playbill.line.spec"
    )
    assert [
        item["node_id"] for item in projected_line.value["line"]["provider_implementation_closures"]
    ] == ["slot"]
