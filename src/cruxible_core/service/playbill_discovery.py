"""Accepted-state semantic discovery over the governed naming layer.

The vocabulary is rebuilt from accepted facts at the resolved coordinate, so a
discovery page is a pure function of that coordinate and the request. Refusal
conventions match the sibling expand service: a coordinate that is not an
accepted one is refused before any state is read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_core.playbill.claim_types import ClaimType, parse_claim_type
from cruxible_core.playbill.discovery import DiscoveryBudgetV1, DiscoveryPageV1, DiscoveryRequestV1
from cruxible_core.playbill.errors import ProposalIntegrityError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.procedures.artifacts import AcceptedProcedureV1
from cruxible_core.playbill.procedures.line_specs import AcceptedLineSpecV1
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.query.backends import ClaimQueryFactsV1, subject_query_view
from cruxible_core.playbill.query.definitions import (
    AcceptedQueryDefinitionV1,
    parse_query_definition,
    query_definition_digest,
)
from cruxible_core.playbill.query.semantic_discovery import (
    DiscoveryVocabularyV1,
    build_discovery_vocabulary,
    discover,
)
from cruxible_core.playbill.service.claim_types import CLAIM_TYPE_PATH_PREFIX
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import QUERY_DEFINITION_PATH_PREFIX
from cruxible_core.playbill.source_readers import ExternalSourceReaderProtocol
from cruxible_core.service.playbill_query import build_accepted_query_facts


class _StrictDiscoveryServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillDiscoveryResultV1(_StrictDiscoveryServiceModel):
    """One discovery page bound to the accepted coordinate that produced it."""

    tag: Literal["playbill-discovery-result-v1"] = "playbill-discovery-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    page: DiscoveryPageV1
    vocabulary_entry_count: int


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


def accepted_claim_types(tree: Mapping[str, bytes]) -> tuple[ClaimType, ...]:
    """Return every accepted ClaimType in byte-sorted ledger-path order."""

    return tuple(
        parse_claim_type(tree[path], path=path)
        for path in sorted(tree, key=lambda item: item.encode("utf-8"))
        if path.startswith(CLAIM_TYPE_PATH_PREFIX)
    )


def accepted_query_definitions(
    tree: Mapping[str, bytes],
) -> tuple[AcceptedQueryDefinitionV1, ...]:
    """Return every accepted QueryDefinition in byte-sorted ledger-path order."""

    definitions: list[AcceptedQueryDefinitionV1] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith(QUERY_DEFINITION_PATH_PREFIX):
            continue
        query = parse_query_definition(tree[path], path=path)
        definitions.append(
            AcceptedQueryDefinitionV1(
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
    procedures: Iterable[AcceptedProcedureV1] = (),
    line_specs: Iterable[AcceptedLineSpecV1] = (),
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
) -> DiscoveryVocabularyV1:
    """Project the accepted naming layer at one coordinate into a vocabulary."""

    resolved_facts = facts or build_accepted_query_facts(
        instance,
        coordinate=coordinate,
        external_readers=external_readers,
    )
    tree = instance.tree_at(coordinate.git_oid)
    return build_discovery_vocabulary(
        view=subject_query_view(resolved_facts),
        facts=resolved_facts,
        claim_types=accepted_claim_types(tree),
        definitions=accepted_query_definitions(tree),
        procedures=procedures,
        line_specs=line_specs,
    )


def service_discover_playbill_semantic(
    instance: PlaybillInstance,
    *,
    evaluation_time: str,
    query: str | None = None,
    entrypoint: str | None = None,
    at: PlaybillAcceptedCoordinate | None = None,
    profile: Literal["interfaces", "subjects", "all"] = "interfaces",
    budget: DiscoveryBudgetV1 = DiscoveryBudgetV1(),
    procedures: Iterable[AcceptedProcedureV1] = (),
    line_specs: Iterable[AcceptedLineSpecV1] = (),
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
) -> PlaybillDiscoveryResultV1:
    """Answer one exact/lexical discovery request without writing anything.

    Exactly one of ``query`` or ``entrypoint`` selects the page; the accepted
    discovery law refuses the ambiguous and the empty request alike.
    """

    if at is not None and not isinstance(at, PlaybillAcceptedCoordinate):
        raise ProposalIntegrityError("discovery accepts only verified accepted coordinates")
    coordinate = _resolve_coordinate(instance, at)
    vocabulary = build_accepted_discovery_vocabulary(
        instance,
        coordinate=coordinate,
        procedures=procedures,
        line_specs=line_specs,
        external_readers=external_readers,
    )
    page = discover(
        DiscoveryRequestV1(
            query=query,
            entrypoint=entrypoint,
            at=vocabulary.at,
            evaluation_time=evaluation_time,
            profile=profile,
            budget=budget,
        ),
        vocabulary=vocabulary,
    )
    return PlaybillDiscoveryResultV1(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        page=page,
        vocabulary_entry_count=len(vocabulary.entries),
    )


__all__ = [
    "PlaybillDiscoveryResultV1",
    "accepted_claim_types",
    "accepted_query_definitions",
    "build_accepted_discovery_vocabulary",
    "service_discover_playbill_semantic",
]
