"""Typed service operations for governed ClaimType interfaces.

ClaimTypes are not carried by the accepted projection index, so reads walk the
accepted tree at the resolved coordinate exactly as semantic expansion does.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.errors import ClaimNotFoundError
from cruxible_core.playbill.actor_context import TransportCapability
from cruxible_core.playbill.claim_type_inputs import (
    ClaimTypeInputProposalResultV1,
    ClaimTypeInputV1,
    lint_claim_type_input,
    lower_claim_type_input,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
)
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)
from cruxible_core.playbill.service.proposal_names import canonical_playbill_proposal_name

CLAIM_TYPE_PATH_PREFIX = "claim-types/"


class _StrictClaimTypeServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillClaimTypeView(_StrictClaimTypeServiceModel):
    tag: Literal["playbill-claim-type-read-v1"] = "playbill-claim-type-read-v1"
    coordinate: PlaybillAcceptedCoordinate
    path: str
    predicate: str
    identity: str
    artifact_digest: str
    envelope: dict[str, object]


class PlaybillClaimTypeList(_StrictClaimTypeServiceModel):
    tag: Literal["playbill-claim-type-list-v1"] = "playbill-claim-type-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    claim_types: tuple[PlaybillClaimTypeView, ...]


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
    claim_type: ClaimType,
    *,
    path: str,
    coordinate: AcceptedProjectionCoordinate,
) -> PlaybillClaimTypeView:
    return PlaybillClaimTypeView(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        path=path,
        predicate=claim_type.predicate,
        identity=claim_type.identity.qualified,
        artifact_digest=claim_type_digest(claim_type).tagged,
        envelope=claim_type.model_dump(mode="json"),
    )


def service_propose_playbill_claim_type(
    instance: PlaybillInstance,
    *,
    claim_type: ClaimType,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: PlaybillAcceptedCoordinate | None = None,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
) -> PlaybillProposalInspection:
    """Submit one ClaimType candidate through the generic proposal path."""

    proposed_base = _resolve_coordinate(instance, base)
    candidate_tree = instance.tree_at(proposed_base.git_oid)
    candidate_tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    ref_name = canonical_playbill_proposal_name(proposal_name, family="claim type")
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id, capabilities=capabilities),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{ref_name}",
            proposed_base_oid=proposed_base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    return PlaybillProposalInspection(
        proposal=result,
        accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        ),
    )


def service_propose_playbill_claim_type_input(
    instance: PlaybillInstance,
    *,
    input: ClaimTypeInputV1,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
) -> ClaimTypeInputProposalResultV1:
    """Lower and lint one tagless ClaimType input against one captured coordinate."""

    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    claim_type = lower_claim_type_input(input, tree=tree)
    candidate_tree = dict(tree)
    candidate_tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    ref_name = canonical_playbill_proposal_name(proposal_name, family="claim type input")
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id, capabilities=capabilities),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{ref_name}",
            proposed_base_oid=coordinate.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    inspection = PlaybillProposalInspection(
        proposal=result,
        accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        ),
    )
    return ClaimTypeInputProposalResultV1(
        proposal=inspection,
        lint=lint_claim_type_input(instance, input, coordinate=coordinate),
    )


def service_get_playbill_claim_type(
    instance: PlaybillInstance,
    *,
    predicate: str,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillClaimTypeView:
    """Return one accepted ClaimType, refusing when the predicate is absent."""

    coordinate = _resolve_coordinate(instance, at)
    path = claim_type_path(predicate)
    content = instance.tree_at(coordinate.git_oid).get(path)
    if content is None:
        raise ClaimNotFoundError(path)
    return _view(parse_claim_type(content, path=path), path=path, coordinate=coordinate)


def service_list_playbill_claim_types(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillClaimTypeList:
    """Return every accepted ClaimType in byte-sorted ledger-path order."""

    coordinate = _resolve_coordinate(instance, at)
    tree = instance.tree_at(coordinate.git_oid)
    views = tuple(
        _view(parse_claim_type(tree[path], path=path), path=path, coordinate=coordinate)
        for path in sorted(tree, key=lambda item: item.encode("utf-8"))
        if path.startswith(CLAIM_TYPE_PATH_PREFIX)
    )
    return PlaybillClaimTypeList(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        claim_types=views,
    )


__all__ = [
    "PlaybillClaimTypeList",
    "PlaybillClaimTypeView",
    "service_get_playbill_claim_type",
    "service_list_playbill_claim_types",
    "service_propose_playbill_claim_type",
    "service_propose_playbill_claim_type_input",
]
