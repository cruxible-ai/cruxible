"""Typed service operations for governed ClaimType interfaces."""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.errors import ClaimNotFoundError
from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeInputProposalResultV1,
    ClaimTypeInputV1,
    identity_rules_supported,
    lint_claim_type_input,
    lower_claim_type_input,
)
from cruxible_core.governance.actor_context import TransportCapability
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)
from cruxible_core.service.proposals.proposal_names import canonical_playbill_proposal_name

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
    candidate_tree = instance.immutable_tree_at(proposed_base.git_oid).fork()
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
        workspace_advertisement=result.workspace_advertisement,
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
    tree = instance.immutable_tree_at(coordinate.git_oid)
    claim_type = lower_claim_type_input(
        input, tree=tree, identity_rules=identity_rules_supported(coordinate.compiler)
    )
    candidate_tree = tree.fork()
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
        workspace_advertisement=result.workspace_advertisement,
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
    with instance.bind_accepted_projection(coordinate) as projection:
        claim_type = projection.typed.source(f"ClaimType:{predicate}")
        if claim_type is None:
            declared = tuple(
                row.identity.removeprefix("ClaimType:")
                for row in projection.typed.envelopes(kind="claim-type")
            )
            raise ClaimTypeNotFoundError(predicate, nearest=nearest_names(predicate, declared))
    return _view(claim_type, path=path, coordinate=coordinate)


def nearest_names(value: str, names: Iterable[str], *, limit: int = 5) -> tuple[str, ...]:
    """Name the declared entries a mistyped or shortened name most likely meant."""

    ordered = sorted(set(names))
    by_leaf = [name for name in ordered if name.endswith(f".{value}")]
    close = difflib.get_close_matches(value, ordered, n=limit, cutoff=0.6)
    return tuple(dict.fromkeys([*by_leaf, *close]))[:limit]


class ClaimTypeNotFoundError(ClaimNotFoundError):
    """No accepted ClaimType has this predicate; names the nearest declared ones."""

    error_code = "playbill.claim_type_not_found"

    def __init__(self, predicate: str, *, nearest: tuple[str, ...]) -> None:
        self.predicate = predicate
        self.nearest = nearest
        self.repair = RepairOperationV1(
            operation="playbill.orient", arguments={"section": "claim_types"}
        )
        hint = f"; nearest: {', '.join(nearest)}" if nearest else ""
        super().__init__(
            f"{self.error_code}: no accepted ClaimType has predicate {predicate!r}{hint}; "
            "run `cruxible playbill orient --section claim_types` for every declared predicate"
        )


__all__ = [
    "ClaimTypeNotFoundError",
    "PlaybillClaimTypeView",
    "service_get_playbill_claim_type",
    "service_propose_playbill_claim_type",
    "service_propose_playbill_claim_type_input",
    "nearest_names",
]
