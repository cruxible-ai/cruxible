"""Read-only Procedure source preview through the same resolver used by prepare."""

from cruxible_client.contracts.procedures.source_compiler import SourceCompileError
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreview,
    ProcedureSourcePreviewRequest,
)
from cruxible_core.authoring.procedure_source import (
    accepted_build_resolver,
    resolve_indexed_source,
)
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
            builds = accepted_build_resolver(instance, coordinate, rows)
            compiled = resolve_indexed_source(request.source, rows, builds=builds)
        return ProcedureSourcePreview(
            name=request.source.name,
            coordinate=request.at,
            definition=compiled.definition,
            contracts=compiled.contracts,
            source_map=compiled.source_map,
            provider_builds=tuple(builds.resolutions),
        )
    except SourceCompileError as exc:
        return ProcedureSourcePreview(
            name=request.source.name, coordinate=request.at, errors=(exc.diagnostic,)
        )
