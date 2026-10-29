"""Read-only Procedure source preview through the same resolver used by prepare."""

from cruxible_client.contracts.artifacts import ArtifactNameRef
from cruxible_client.contracts.procedures.models import iter_pin_bindings
from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition
from cruxible_client.contracts.procedures.names import named_graph
from cruxible_client.contracts.procedures.source_compiler import SourceCompileError
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreview,
    ProcedureSourcePreviewRequest,
)
from cruxible_core.authoring.procedure_source import resolve_indexed_source
from cruxible_core.compiler.compiler import LIGHT_ARTIFACTS_COMPILER
from cruxible_core.indexes.evaluated_state import EvaluationRows
from cruxible_core.runtime.instance import PlaybillInstance


def service_preview_procedure_source(
    instance: PlaybillInstance, *, request: ProcedureSourcePreviewRequest
) -> ProcedureSourcePreview:
    coordinate = instance.resolve_accepted_coordinate(
        **request.at.model_dump(mode="json", exclude={"tag"})
    )
    try:
        with instance.bind_accepted_projection(coordinate) as projection:
            rows = EvaluationRows(projection)
            compiled = resolve_indexed_source(request.source, rows)
        definition = compiled.definition
        contracts = compiled.contracts
        if coordinate.compiler == LIGHT_ARTIFACTS_COMPILER:
            definition = NamedProcedureDefinition.model_validate(
                named_graph(definition.model_dump(mode="json", by_alias=True))
            )
            required = {
                ref.target.name
                for ref in iter_pin_bindings(definition)
                if isinstance(ref, ArtifactNameRef) and ref.target.kind == "Contract"
            }
            contracts = tuple(contract for contract in contracts if contract.name in required)
        return ProcedureSourcePreview(
            name=request.source.name,
            coordinate=request.at,
            definition=definition,
            contracts=contracts,
            source_map=compiled.source_map,
        )
    except SourceCompileError as exc:
        return ProcedureSourcePreview(
            name=request.source.name, coordinate=request.at, errors=(exc.diagnostic,)
        )
