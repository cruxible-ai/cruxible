"""Accepted-state semantic discovery over the governed naming layer.

The vocabulary is rebuilt from accepted facts at the resolved coordinate, so a
discovery page is a pure function of that coordinate and the request. Refusal
conventions match the sibling expand service: a coordinate that is not an
accepted one is refused before any state is read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from cruxible_client.contracts.claim_types import ClaimType, parse_claim_type
from cruxible_client.contracts.orient import OrientOperation
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.line_specs import AcceptedLineSpec
from cruxible_client.contracts.provider_operations import operation_signature_digest
from cruxible_client.contracts.providers import ProviderV2, parse_provider
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinition,
    parse_query_definition,
    query_definition_digest,
)
from cruxible_core.evidence.source_readers import ExternalSourceReaderProtocol
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.query.backends import ClaimQueryFactsV1, subject_query_view
from cruxible_core.query.semantic_discovery import (
    DiscoveryVocabularyV1,
    build_discovery_vocabulary,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import AcceptedCoordinate
from cruxible_core.service.claims.claim_types import CLAIM_TYPE_PATH_PREFIX
from cruxible_core.service.discovery.query import build_accepted_query_facts
from cruxible_core.service.discovery.query_definitions import QUERY_DEFINITION_PATH_PREFIX


def accepted_provider_operations(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
) -> tuple[OrientOperation, ...]:
    """Every operation a live accepted build serves at one coordinate.

    The one inventory source ``orient(section="operations")`` serves: what a
    Line can bind a slot to. It reads the live build rows only (a retired build
    costs nothing here), sorted by build then operation.
    """

    with instance.bind_accepted_projection(coordinate) as projection:
        typed = projection.typed
        paths = [
            str(row[0])
            for row in typed.connection.execute(
                "SELECT path FROM main.providers WHERE lifecycle='live' ORDER BY path"
            )
        ]
        typed.prefetch_members(tuple(paths))
        builds = [
            provider
            for path in paths
            if isinstance(
                provider := parse_provider(typed.member_bytes(path), path=path), ProviderV2
            )
        ]
    rows = [
        OrientOperation(
            build=build.identity.qualified,
            operation=operation.name,
            input=operation.in_.spelling,
            output=operation.out.spelling,
            effect=operation.effect,
            refusals=tuple(sorted(operation.refusals)),
            signature_digest=operation_signature_digest(operation.signature()),
        )
        for build in builds
        for operation in build.operations
    ]
    return tuple(sorted(rows, key=lambda item: (item.build, item.operation)))


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: AcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def accepted_claim_types(tree: Mapping[str, bytes]) -> tuple[ClaimType, ...]:
    """Return every accepted ClaimType in byte-sorted ledger-path order."""

    return tuple(
        parse_claim_type(tree[path], path=path)
        for path in sorted(tree, key=lambda item: item.encode("utf-8"))
        if path.startswith(CLAIM_TYPE_PATH_PREFIX)
    )


def accepted_query_definitions(
    tree: Mapping[str, bytes],
) -> tuple[AcceptedQueryDefinition, ...]:
    """Return every accepted QueryDefinition in byte-sorted ledger-path order."""

    definitions: list[AcceptedQueryDefinition] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith(QUERY_DEFINITION_PATH_PREFIX):
            continue
        query = parse_query_definition(tree[path], path=path)
        definitions.append(
            AcceptedQueryDefinition(
                path=path,
                query=query,
                artifact_digest=query_definition_digest(query).tagged,
            )
        )
    return tuple(definitions)


def build_accepted_discovery_vocabulary(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    facts: ClaimQueryFactsV1 | None = None,
    procedures: Iterable[AcceptedProcedure] = (),
    line_specs: Iterable[AcceptedLineSpec] = (),
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
) -> DiscoveryVocabularyV1:
    """Project the accepted naming layer at one coordinate into a vocabulary."""

    resolved_facts = facts or build_accepted_query_facts(
        instance,
        coordinate=coordinate,
        external_readers=external_readers,
    )
    with instance.bind_accepted_projection(coordinate) as projection:
        tree = {
            row.path: projection.typed.member_bytes(row.path)
            for kind in ("claim-type", "query-definition")
            for row in projection.typed.envelopes(kind=kind)
        }
    return build_discovery_vocabulary(
        view=subject_query_view(resolved_facts),
        facts=resolved_facts,
        claim_types=accepted_claim_types(tree),
        definitions=accepted_query_definitions(tree),
        procedures=procedures,
        line_specs=line_specs,
    )


__all__ = [
    "accepted_claim_types",
    "accepted_provider_operations",
    "accepted_query_definitions",
    "build_accepted_discovery_vocabulary",
]
