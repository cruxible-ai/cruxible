"""A Procedure with an acquiring call and a plain call admits on both run lanes.

Occurrences are planned in occurrence-path order (``call/...`` sorts before
``source/...``, an acquiring call's path) while admission requires its Provider
bindings in node-id order. With an acquiring call ``export`` and a plain call
``records`` the two orders differ, and both lanes used to fail admission with an
internal validation error.
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
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.provider_execution import ProviderEgressObservation
from cruxible_core.providers.provider_local_runtime import ProviderDriverOutcomeV1
from cruxible_core.providers.provider_runtime_contract import ProviderRuntimeResultEnvelope
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRunRequest,
    service_run_playbill_procedure,
)
from tests.support.procedures import procedure_members
from tests.test_procedures import test_procedure_source_runs as fixtures


class _Invoker(fixtures._WorkspaceInvoker):
    """The workspace adapter for the Source read; ``{"value": 2}`` for the Call."""

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
            envelope=ProviderRuntimeResultEnvelope(
                protocol_version="2.0", run_id=context.run_id, status="ok", output={"value": 2}
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(
                observer_backend="test-attribution", observer_grade="attribution"
            ),
            verified_binding=bound.binding,
        )


def _source_then_call(procedure: ProcedureArtifact) -> ProcedureArtifact:
    """Add a second, plain, Call node through its own ``compute`` slot.

    A ``workspace.file`` slot's result is the Capture core reads, so every call
    on it acquires; the plain call names the fixture ``test.compute``
    capability, which a provider build serves. ``fixtures._world`` always
    lowers through ``name_procedure``, so this is the name-referenced node set.
    """

    from cruxible_client.contracts.procedures.models import CallNode
    from cruxible_client.contracts.procedures.slots import ProcedureSlot
    from tests.support.capabilities import pinned_slot
    from tests.support.operation_world import compute_capability

    source, shape = procedure.definition.nodes
    call_node = CallNode(
        node_id="records",
        slot="compute",
        input={"value": 1},
        **{"as": "records"},
        next="shape",
    )
    definition = procedure.definition.model_copy(
        update={
            "slots": {
                **procedure.definition.slots,
                "compute": ProcedureSlot.model_validate(pinned_slot(compute_capability())),
            },
            "nodes": (
                source.model_copy(update={"node_id": "export", "next": "records"}),
                call_node,
                shape,
            ),
        }
    )
    return procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )


def _world(tmp_path: Path) -> tuple[Any, Any, Path, Any]:
    from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
    from cruxible_client.contracts.providers import provider_digest
    from tests.support.operation_world import build_files, contract_files, operation_build

    instance, owner, procedure, root, policy = fixtures._world(tmp_path, accept_procedure=False)
    build = operation_build()
    fixtures._accept_more(
        instance, owner, {**contract_files(), **build_files(build)}, name="compute-provider"
    )
    both = _source_then_call(procedure)
    served = fixtures._served_line(instance, both, policy)
    line = served.model_copy(
        update={
            "bindings": {
                **served.bindings,
                "compute": LineBinding(
                    build=binding_pin(build.identity, provider_digest(build).tagged),
                    operation="compute",
                ),
            }
        }
    )
    mandate = fixtures._line_mandate(line, both)
    fixtures._accept_more(
        instance,
        owner,
        {
            **procedure_members(both),
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
