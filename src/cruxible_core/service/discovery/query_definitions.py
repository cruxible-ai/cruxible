"""Typed service operations for governed QueryDefinition declarations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.errors import ClaimNotFoundError
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinitionV1,
    QueryDefinitionV1,
    query_definition_digest,
    query_definition_path,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    PlaybillAcceptedCoordinate,
)

QUERY_DEFINITION_PATH_PREFIX = "query-definitions/"


class _StrictQueryDefinitionServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillQueryDefinitionView(_StrictQueryDefinitionServiceModel):
    tag: Literal["playbill-query-definition-read-v1"] = "playbill-query-definition-read-v1"
    coordinate: PlaybillAcceptedCoordinate
    path: str
    name: str
    identity: str
    artifact_digest: str
    envelope: dict[str, object]


class PlaybillQueryDefinitionList(_StrictQueryDefinitionServiceModel):
    tag: Literal["playbill-query-definition-list-v1"] = "playbill-query-definition-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    query_definitions: tuple[PlaybillQueryDefinitionView, ...]


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: PlaybillAcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def _view(
    query: QueryDefinitionV1,
    *,
    path: str,
    coordinate: AcceptedProjectionCoordinate,
) -> PlaybillQueryDefinitionView:
    return PlaybillQueryDefinitionView(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        path=path,
        name=query.identity.name,
        identity=query.identity.qualified,
        artifact_digest=query_definition_digest(query).tagged,
        envelope=query.model_dump(mode="json"),
    )


def accepted_query_definition(
    instance: PlaybillInstance,
    *,
    name: str,
    coordinate: AcceptedProjectionCoordinate,
) -> AcceptedQueryDefinitionV1:
    """Return one accepted QueryDefinition bound to its exact accepted digest."""

    path = query_definition_path(name)
    identity = f"QueryDefinition:{name}"
    with instance.bind_accepted_projection(coordinate) as projection:
        assert projection.typed is not None
        envelope = projection.typed.envelope(identity)
        if envelope is None:
            raise ClaimNotFoundError(path)
        return AcceptedQueryDefinitionV1(
            path=envelope.path,
            query=projection.typed.source(identity),
            artifact_digest=envelope.artifact_digest,
        )


def service_get_playbill_query_definition(
    instance: PlaybillInstance,
    *,
    name: str,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillQueryDefinitionView:
    """Return one accepted QueryDefinition, refusing when the name is absent."""

    coordinate = _resolve_coordinate(instance, at)
    accepted = accepted_query_definition(instance, name=name, coordinate=coordinate)
    return _view(accepted.query, path=accepted.path, coordinate=coordinate)


def service_list_playbill_query_definitions(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillQueryDefinitionList:
    """Return every accepted QueryDefinition in byte-sorted ledger-path order."""

    coordinate = _resolve_coordinate(instance, at)
    with instance.bind_accepted_projection(coordinate) as projection:
        assert projection.typed is not None
        views = tuple(
            _view(
                projection.typed.source(envelope.identity),
                path=envelope.path,
                coordinate=coordinate,
            )
            for envelope in sorted(
                projection.typed.envelopes(kind="query-definition"),
                key=lambda item: item.path.encode("utf-8"),
            )
        )
    return PlaybillQueryDefinitionList(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        query_definitions=views,
    )


__all__ = [
    "PlaybillQueryDefinitionList",
    "PlaybillQueryDefinitionView",
    "accepted_query_definition",
    "service_get_playbill_query_definition",
    "service_list_playbill_query_definitions",
]
