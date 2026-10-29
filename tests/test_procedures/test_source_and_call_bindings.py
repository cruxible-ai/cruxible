"""A Procedure with a Source node and a Call node admits on both run lanes.

Occurrences are planned in occurrence-path order (``call/...`` sorts before
``source/...``) while admission requires its Provider bindings in node-id order.
With a Source node ``export`` and a Call node ``records`` the two orders differ,
and both lanes used to fail admission with an internal validation error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.provider_execution import ProviderEgressObservation
from cruxible_core.providers.provider_local_runtime import ProviderDriverOutcomeV1
from cruxible_core.providers.provider_runtime_contract import ProviderRuntimeResultEnvelopeV1
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRunRequest,
    service_run_playbill_procedure,
)
from tests.test_procedures import test_procedure_source_runs as fixtures


class _Invoker(fixtures._WorkspaceInvoker):
    """The workspace adapter for the Source read; any plain output for the Call."""

    def invoke_provider(self, *, occurrence, context, invocation_id, bound, deadline):  # type: ignore[no-untyped-def]
        if isinstance(context.input, dict) and "bytes" in context.input:
            return super().invoke_provider(
                occurrence=occurrence,
                context=context,
                invocation_id=invocation_id,
                bound=bound,
                deadline=deadline,
            )
        self.spawn_calls += 1
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0", run_id=context.run_id, status="ok", output={}
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(
                observer_backend="test-attribution", observer_grade="attribution"
            ),
            verified_binding=bound.binding,
        )


def _source_then_call(procedure: ProcedureArtifact) -> ProcedureArtifact:
    """Add a second, Call, node through the same ``read`` slot the Source uses.

    Both nodes bind the same slot (a Line binds every slot once, regardless of
    how many nodes name it), so no extra Contract pins are needed -- the
    Call's input/output are exactly the slot's signature. ``fixtures._world``
    always lowers through ``name_procedure`` now, so this is always the
    name-referenced (format 7) node set.
    """

    from cruxible_client.contracts.procedures.models import CallNode

    source, shape = procedure.definition.nodes
    call_node = CallNode(
        node_id="records",
        slot="read",
        input={},
        **{"as": "records"},
        next="shape",
    )
    definition = procedure.definition.model_copy(
        update={
            "nodes": (
                source.model_copy(update={"node_id": "export", "next": "records"}),
                call_node,
                shape,
            )
        }
    )
    return procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )


def _world(tmp_path: Path) -> tuple[Any, Any, Path, Any]:
    instance, owner, procedure, root, policy = fixtures._world(tmp_path, accept_procedure=False)
    both = _source_then_call(procedure)
    line = fixtures._served_line(instance, both, policy)
    mandate = fixtures._line_mandate(line, both)
    fixtures._accept_more(
        instance,
        owner,
        {
            procedure_path(both.identity.name): render_procedure(both),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="source-and-call",
    )
    return instance, line, root, both


def test_a_direct_run_with_a_source_and_a_call_admits(tmp_path: Path) -> None:
    instance, _line, root, both = _world(tmp_path)

    state = service_run_playbill_procedure(
        instance,
        name=both.identity.name,
        request=ProcedureRunRequest(input={}, evaluation_time=fixtures.NOW),
        actor_context=fixtures._actor(instance),
        provider_runtime_operator=fixtures._Operator(_Invoker()),  # type: ignore[arg-type]
        workspace_file_reader=fixtures._reader(instance, root),
    )

    assert state.run_id is not None, state.terminal


def test_a_line_run_with_a_source_and_a_call_admits(tmp_path: Path) -> None:
    instance, line, root, _both = _world(tmp_path)

    state, _invoker = fixtures._run_line(instance, root, line, invoker=_Invoker())

    assert state.run_id is not None, state.terminal
