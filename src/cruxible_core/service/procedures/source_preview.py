"""Read-only Procedure source preview through the same resolver used by prepare."""

from cruxible_client.contracts.procedures.source_compiler import SourceCompileError
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreviewRequestV1,
    ProcedureSourcePreviewV1,
)
from cruxible_core.authoring.procedure_source import resolve_source
from cruxible_core.runtime.instance import PlaybillInstance


def service_preview_procedure_source(
    instance: PlaybillInstance, *, request: ProcedureSourcePreviewRequestV1
) -> ProcedureSourcePreviewV1:
    coordinate = instance.resolve_accepted_coordinate(
        **request.at.model_dump(mode="json", exclude={"tag"})
    )
    try:
        with instance.bind_accepted_projection(coordinate) as projection:
            types = (
                projection.typed.source(row.identity)
                for row in projection.typed.envelopes(kind="claim-type")
            )
            compiled = resolve_source(
                request.source, lookup=projection.typed.source, claim_types=types
            )
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
