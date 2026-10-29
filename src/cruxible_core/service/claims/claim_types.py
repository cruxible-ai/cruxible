"""Typed service operations for governed ClaimType interfaces."""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.builtin_claim_types import BuiltinClaimTypeName, builtin_claim_type
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.errors import ClaimNotFoundError, FormatError
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeInputProposalResultV1,
    ClaimTypeInputRecord,
    install_builtin_capture_dependencies,
    lint_claim_type_input,
    lower_claim_type_input,
)
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    build_claim_type_migration_candidate,
    claim_type_migration_inventory,
)
from cruxible_core.governance.actor_context import TransportCapability
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.proposals.proposals import (
    ProposalAdmissionRequest,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    AcceptedCoordinate,
    ProposalInspection,
    admit_proposal,
)
from cruxible_core.service.proposals.proposal_names import canonical_playbill_proposal_name

CLAIM_TYPE_PATH_PREFIX = "claim-types/"


class _StrictClaimTypeServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillClaimTypeView(_StrictClaimTypeServiceModel):
    tag: Literal["playbill-claim-type-read-v1"] = "playbill-claim-type-read-v1"
    coordinate: AcceptedCoordinate
    path: str
    predicate: str
    identity: str
    artifact_digest: str
    envelope: dict[str, object]


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


def _view(
    claim_type: ClaimType,
    *,
    path: str,
    coordinate: AcceptedProjectionCoordinate,
) -> PlaybillClaimTypeView:
    return PlaybillClaimTypeView(
        coordinate=AcceptedCoordinate.from_internal(coordinate),
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
    base: AcceptedCoordinate | None = None,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
    dry_run: bool | None = None,
    at: str | None = None,
) -> ProposalInspection:
    """Submit (or preview) one ClaimType candidate through the generic proposal path."""

    proposed_base = _resolve_coordinate(instance, base)
    candidate_tree = instance.immutable_tree_at(proposed_base.git_oid).fork()
    candidate_tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    install_builtin_capture_dependencies(claim_type, candidate_tree)
    ref_name = canonical_playbill_proposal_name(proposal_name, family="claim type")
    return admit_proposal(
        instance,
        dry_run=dry_run,
        at=at,
        operation="cruxible.claim-type.propose",
        describe=f"proposing ClaimType {claim_type.predicate}",
        actor_id=actor_id,
        proposed_base=proposed_base,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{ref_name}",
            proposed_base_oid=proposed_base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
        capabilities=capabilities,
    )


def service_propose_playbill_builtin_claim_type(
    instance: PlaybillInstance,
    *,
    builtin: BuiltinClaimTypeName,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: AcceptedCoordinate | None = None,
    dry_run: bool | None = None,
    at: str | None = None,
) -> ClaimTypeInputProposalResultV1:
    """Adopt the daemon's exact builtin bytes, never a caller-supplied envelope."""

    claim_type = builtin_claim_type(builtin)
    coordinate = _resolve_coordinate(instance, base)
    held = instance.immutable_tree_at(coordinate.git_oid).get(claim_type_path(builtin))
    if held is not None:
        raise (
            BuiltinClaimTypeAlreadyAdopted(builtin)
            if held == render_claim_type(claim_type)
            else BuiltinClaimTypeSuperseded(builtin)
        )
    inspection = service_propose_playbill_claim_type(
        instance,
        claim_type=claim_type,
        actor_id=actor_id,
        proposal_name=proposal_name,
        timestamp=timestamp,
        base=AcceptedCoordinate.from_internal(coordinate),
        dry_run=dry_run,
        at=at,
    )
    return ClaimTypeInputProposalResultV1(
        proposal=inspection,
        lint=lint_claim_type_input(instance, claim_type, coordinate=coordinate),
    )


def service_propose_playbill_claim_type_input(
    instance: PlaybillInstance,
    *,
    input: ClaimTypeInputRecord,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: AcceptedCoordinate | None = None,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
    dry_run: bool | None = None,
    at: str | None = None,
) -> ClaimTypeInputProposalResultV1:
    """Lower and lint one tagless ClaimType input against one captured coordinate."""

    coordinate = _resolve_coordinate(instance, base)
    tree = instance.immutable_tree_at(coordinate.git_oid)
    claim_type = lower_claim_type_input(input, tree=tree)
    path = claim_type_path(claim_type.predicate)
    inventory = (
        claim_type_migration_inventory(tree, root=claim_type.identity, by_name=True)
        if path in tree
        else ()
    )
    candidate_tree: Mapping[str, bytes]
    if inventory:
        # An edit of a ClaimType with live Claims is a succession: every
        # dependent is carried to the successor as it stands (carry_all), the
        # same candidate `claim-type migrate` builds. Work grows with this
        # ClaimType's closure only.
        candidate_tree, _dispositions, _warnings = build_claim_type_migration_candidate(
            tree=tree,
            type_path=path,
            successor=claim_type,
            inventory=inventory,
            dispositions=tuple(
                ClaimTypeDependentDisposition(identity=item.identity, disposition="successor")
                for item in inventory
            ),
        )
    else:
        forked = tree.fork()
        forked[path] = render_claim_type(claim_type)
        candidate_tree = forked
    from cruxible_core.derived.derived_state import fork_tree

    with_dependencies = fork_tree(candidate_tree)
    install_builtin_capture_dependencies(claim_type, with_dependencies)
    candidate_tree = with_dependencies
    ref_name = canonical_playbill_proposal_name(proposal_name, family="claim type input")
    inspection = admit_proposal(
        instance,
        dry_run=dry_run,
        at=at,
        operation="cruxible.claim-type.propose",
        describe=f"proposing ClaimType {claim_type.predicate}",
        actor_id=actor_id,
        proposed_base=coordinate,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{ref_name}",
            proposed_base_oid=coordinate.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
        capabilities=capabilities,
    )
    return ClaimTypeInputProposalResultV1(
        proposal=inspection,
        lint=lint_claim_type_input(instance, input, coordinate=coordinate),
    )


class BuiltinClaimTypeAlreadyAdopted(FormatError):
    """The instance already holds this built-in ClaimType, byte for byte."""

    error_code = "cruxible.claim_type.builtin_already_adopted"

    def __init__(self, builtin: str) -> None:
        self.repair = RepairOperation(
            operation="cruxible.get", arguments={"ref": f"ClaimType:{builtin}"}
        )
        super().__init__(
            f"{self.error_code}: this instance already holds the built-in ClaimType "
            f"{builtin}; nothing to adopt. Read it: `cruxible get ClaimType:{builtin}`"
        )


class BuiltinClaimTypeSuperseded(FormatError):
    """The instance holds its own version of a built-in ClaimType's predicate."""

    error_code = "cruxible.claim_type.builtin_superseded"

    def __init__(self, builtin: str) -> None:
        self.repair = RepairOperation(
            operation="cruxible.get", arguments={"ref": f"ClaimType:{builtin}"}
        )
        super().__init__(
            f"{self.error_code}: this instance holds a ClaimType {builtin} that differs from "
            "the built-in (an edit or an earlier version), so adopting would replace it; "
            f"read it with `cruxible get ClaimType:{builtin}` and change it with "
            "`cruxible claim-type propose --input`"
        )


def service_get_playbill_claim_type(
    instance: PlaybillInstance,
    *,
    predicate: str,
    at: AcceptedCoordinate | None = None,
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

    error_code = "cruxible.claim_type_not_found"

    def __init__(self, predicate: str, *, nearest: tuple[str, ...]) -> None:
        self.predicate = predicate
        self.nearest = nearest
        self.repair = RepairOperation(
            operation="cruxible.orient", arguments={"section": "claim_types"}
        )
        hint = f"; nearest: {', '.join(nearest)}" if nearest else ""
        super().__init__(
            f"{self.error_code}: no accepted ClaimType has predicate {predicate!r}{hint}; "
            "run `cruxible orient --section claim_types` for every declared predicate"
        )


__all__ = [
    "BuiltinClaimTypeAlreadyAdopted",
    "BuiltinClaimTypeSuperseded",
    "ClaimTypeNotFoundError",
    "PlaybillClaimTypeView",
    "service_get_playbill_claim_type",
    "service_propose_playbill_claim_type",
    "service_propose_playbill_claim_type_input",
    "nearest_names",
]
