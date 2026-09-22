"""Read-only Procedure source preview through the same resolver used by prepare."""

from cruxible_client.contracts.procedures.source_compiler import SourceCompileError
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreviewRequestV1,
    ProcedureSourcePreviewV1,
)
from cruxible_core.authoring.procedure_source import resolve_indexed_source
from cruxible_core.indexes.evaluated_state import EvaluationRows
from cruxible_core.runtime.instance import PlaybillInstance


def service_preview_procedure_source(
    instance: PlaybillInstance, *, request: ProcedureSourcePreviewRequestV1
) -> ProcedureSourcePreviewV1:
    coordinate = instance.resolve_accepted_coordinate(
        **request.at.model_dump(mode="json", exclude={"tag"})
    )
    try:
        with instance.bind_accepted_projection(coordinate) as projection:
            compiled = resolve_indexed_source(request.source, EvaluationRows(projection))
        return ProcedureSourcePreviewV1(
            name=request.source.name,
            coordinate=request.at,
            definition=compiled.definition,
            contracts=compiled.contracts,
            source_map=compiled.source_map,
        )
    except SourceCompileError as exc:
        return ProcedureSourcePreviewV1(
            name=request.source.name, coordinate=request.at, errors=(exc.diagnostic,)
        )
