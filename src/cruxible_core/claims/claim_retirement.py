"""Attributed Claim retirement over the complete Claim dependency closure."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimArtifactAny,
    ClaimRetireDependent,
    ClaimRetirementAttribution,
    ClaimRetirementReason,
    ClaimStatement,
    claim_artifact_digest,
    claim_path,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.claims.closure import ReversePinClosureItem, reverse_pin_closure
from cruxible_core.derived.derived_state import CandidateTree, fork_tree
from cruxible_core.runtime.instance import PlaybillInstance


class _StrictRetirementModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimRetireInventoryItemV1(_StrictRetirementModel):
    artifact_identity: ArtifactIdentity
    predecessor_digest: str
    triggering_identity: ArtifactIdentity
    triggering_edge_roles: tuple[str, ...]


class ClaimRetirementResultItemV1(_StrictRetirementModel):
    artifact_identity: ArtifactIdentity
    predecessor_digest: str
    reason: ClaimRetirementReason
    effective_until: datetime | None
    successor_digest: str


class ClaimRetireError(FormatError):
    error_code = "playbill.claim.retire_invalid"


class ClaimRetireClosureMismatch(ClaimRetireError):
    error_code = "playbill.claim.retire_closure_mismatch"


class ClaimRetireDependentUnsupported(ClaimRetireError):
    error_code = "playbill.claim.retire_dependent_unsupported"


class ClaimRetireStale(ClaimRetireError):
    error_code = "playbill.claim.retire_stale"


def _inventory(
    closure: tuple[ReversePinClosureItem, ...],
) -> tuple[ClaimRetireInventoryItemV1, ...]:
    return tuple(
        ClaimRetireInventoryItemV1(
            artifact_identity=item.state.identity,
            predecessor_digest=item.state.artifact_digest,
            triggering_identity=item.triggering_identity,
            triggering_edge_roles=item.dependency_edge_roles,
        )
        for item in closure
        if item.state.artifact_kind == "claim"
    )


def _retired_claim(
    claim: ClaimArtifactAny,
    *,
    reason: ClaimRetirementReason,
    effective_until: datetime | None,
    successor_digests: Mapping[str, str],
) -> ClaimArtifact:
    statement = claim.statement
    if effective_until is not None:
        try:
            statement = ClaimStatement.model_validate(
                {
                    **statement.model_dump(mode="python"),
                    "effective_until": effective_until,
                }
            )
        except ValidationError as exc:
            raise ClaimRetireError(
                f"{ClaimRetireError.error_code}: effective_until produces an invalid Claim "
                "effective interval"
            ) from exc
    pins = tuple(
        pin.model_copy(update={"artifact_digest": successor_digests[pin.target.qualified]})
        if pin.target.kind == "Claim" and pin.target.qualified in successor_digests
        else pin
        for pin in claim.pins
    )
    return ClaimArtifact(
        identity=claim.identity,
        statement=statement,
        backing=claim.backing,
        pins=pins,
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=claim_artifact_digest(claim).tagged,
        ),
        retirement=ClaimRetirementAttribution(reason=reason),
    )


def build_claim_retirement_candidate(
    tree: Mapping[str, bytes],
    *,
    root: ClaimRetireDependent,
    dependents: tuple[ClaimRetireDependent, ...],
) -> tuple[CandidateTree, tuple[ClaimRetirementResultItemV1, ...]]:
    requests = {item.artifact_identity.qualified: item for item in (root, *dependents)}
    claims = {
        identity: parse_claim(
            tree[claim_path(item.artifact_identity.name)],
            path=claim_path(item.artifact_identity.name),
        )
        for identity, item in requests.items()
    }
    for identity, item in requests.items():
        if claim_artifact_digest(claims[identity]).tagged != item.predecessor_digest:
            raise ClaimRetireStale(
                f"{ClaimRetireStale.error_code}: predecessor changed for {identity}"
            )

    candidate_tree = fork_tree(tree)
    successor_digests: dict[str, str] = {}
    successors: dict[str, ClaimArtifact] = {}
    pending = set(requests)
    while pending:
        progressed = False
        for identity in sorted(pending, key=lambda item: item.encode("utf-8")):
            claim = claims[identity]
            unresolved_targets = {
                pin.target.qualified
                for pin in claim.pins
                if pin.target.kind == "Claim" and pin.target.qualified in pending
            }
            if unresolved_targets:
                continue
            request = requests[identity]
            successor = _retired_claim(
                claim,
                reason=request.reason,
                effective_until=request.effective_until,
                successor_digests=successor_digests,
            )
            path = claim_path(claim.identity.name)
            candidate_tree[path] = render_claim(successor)
            successors[identity] = successor
            successor_digests[identity] = claim_artifact_digest(successor).tagged
            pending.remove(identity)
            progressed = True
            break
        if not progressed:
            raise ClaimRetireClosureMismatch(
                f"{ClaimRetireClosureMismatch.error_code}: Claim-target pin cycle"
            )
    results = tuple(
        ClaimRetirementResultItemV1(
            artifact_identity=requests[identity].artifact_identity,
            predecessor_digest=requests[identity].predecessor_digest,
            reason=requests[identity].reason,
            effective_until=successors[identity].statement.effective_until,
            successor_digest=successor_digests[identity],
        )
        for identity in sorted(successors, key=lambda item: item.encode("utf-8"))
    )
    return candidate_tree, results


def claim_retirement_inventory(
    instance: PlaybillInstance,
    *,
    tree: Mapping[str, bytes],
    coordinate: AcceptedCoordinate,
    claim: ClaimArtifactAny,
) -> tuple[ClaimRetireInventoryItemV1, ...]:
    """Return the live Claim closure one retirement must carry, or refuse it.

    The change-set retirement member demands exactly this closure over exactly
    the tree the retirement is being written onto; the retire verb computes it
    for the writer from the same function.
    """

    from cruxible_core.derived.derived_state import snapshot_against

    tree = snapshot_against(tree, instance.immutable_tree_at(coordinate.git_oid))
    with instance.accepted_history_reader(at=coordinate) as history:

        def resolve(digest: str) -> ArtifactIdentity | None:
            try:
                location = history.artifact(digest)
            except FormatError as exc:
                raise ClaimRetireError(
                    "one accepted Claim digest names multiple identities"
                ) from exc
            if location is None:
                return None
            kind, name = location.identity.split(":", 1)
            if kind != "Claim":
                raise ClaimRetireError("accepted Claim input digest names another artifact kind")
            return ArtifactIdentity(kind="Claim", name=name)

        closure = reverse_pin_closure(
            tree,
            root=claim.identity,
            include=lambda state: state.lifecycle.state == "live",
            resolve_claim_digest=resolve,
        )
    unsupported = tuple(item for item in closure if item.state.artifact_kind != "claim")
    if unsupported:
        names = tuple(item.state.identity.qualified for item in unsupported)
        raise ClaimRetireDependentUnsupported(
            f"{ClaimRetireDependentUnsupported.error_code}: {names!r}"
        )
    return _inventory(closure)
