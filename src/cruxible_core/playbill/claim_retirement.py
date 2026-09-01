"""Attributed Claim retirement over the complete Claim dependency closure."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV3,
    ClaimRetireDependentV1,
    ClaimRetirementAttributionV1,
    ClaimRetirementReason,
    ClaimRetireRequestV1,
    ClaimStatement,
    claim_artifact_digest,
    claim_path,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.closure import ReversePinClosureItem, reverse_pin_closure
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    evaluate_proposal_tree,
    validate_proposal_tree,
)
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)

CLAIM_RETIRE_OPERATION_DOMAIN = "playbill-claim-retire-operation-v1"


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


class ClaimRetireCitingClaimV1(_StrictRetirementModel):
    """RESERVED. The advisory that produced these rows is withdrawn.

    Retiring a Claim withdraws an assertion, not the evidence under it, so the
    Claims that read the same bytes are not stranded by it. The only key that
    could have named a real relation -- byte equality of the committed content --
    joins unrelated Claims that happen to read identical spans of different
    sources. The sound key is an explicit copy edge on the citation itself, which
    is the maintainer's shared-Capture/copy-edge design decision to make.

    The model and its field stay on the wire, always empty, so that decision does
    not have to move the catalogued surface a second time.
    """

    artifact_identity: ArtifactIdentity
    claim_path: str
    capture_digests: tuple[str, ...]


class ClaimRetirePreflightV1(_StrictRetirementModel):
    tag: Literal["playbill-claim-retire-preflight-v1"] = "playbill-claim-retire-preflight-v1"
    operation_digest: str
    coordinate: PlaybillAcceptedCoordinate
    root_identity: ArtifactIdentity
    root_predecessor_digest: str
    reason: ClaimRetirementReason
    effective_until: datetime | None
    required_dependents: tuple[ClaimRetireInventoryItemV1, ...]
    # RESERVED, always empty -- see ClaimRetireCitingClaimV1. Retirement makes no
    # claim about who else read the same bytes.
    citing_claims: tuple[ClaimRetireCitingClaimV1, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...]
    submit_ready: bool


class ClaimRetireResultV1(_StrictRetirementModel):
    tag: Literal["playbill-claim-retire-result-v1"] = "playbill-claim-retire-result-v1"
    outcome: Literal["preflight", "proposed", "already_retired"]
    operation_digest: str
    coordinate: PlaybillAcceptedCoordinate
    retirements: tuple[ClaimRetirementResultItemV1, ...]
    proposal: PlaybillProposalInspection | None = None


ClaimRetireResponse = ClaimRetirePreflightV1 | ClaimRetireResultV1


class ClaimRetireError(PlaybillFormatError):
    error_code = "playbill.claim.retire_invalid"


class ClaimRetireClosureMismatch(ClaimRetireError):
    error_code = "playbill.claim.retire_closure_mismatch"


class ClaimRetireDependentUnsupported(ClaimRetireError):
    error_code = "playbill.claim.retire_dependent_unsupported"


class ClaimRetireStale(ClaimRetireError):
    error_code = "playbill.claim.retire_stale"


def _generation_timestamp(instance: PlaybillInstance) -> str:
    record = instance.accepted_history()[-1].record
    if record is None:
        raise ClaimRetireError("an accepted Claim cannot exist at genesis")
    return record.candidate.timestamp


def _claim_identity_by_digest(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
) -> dict[str, ArtifactIdentity]:
    """Join every accepted historical Claim digest to its stable lineage identity."""

    history = instance.accepted_history()
    target_sequence = next(item.sequence for item in history if item.oid == coordinate.git_oid)
    identities: dict[str, ArtifactIdentity] = {}
    for generation in history[1:]:
        if generation.sequence > target_sequence:
            break
        record = generation.record
        if record is None:
            continue
        paths = tuple(
            member.path for member in record.members if str(member.path).startswith("claims/")
        )
        if not paths:
            continue
        tree = instance.tree_at(generation.oid)
        for path in paths:
            content = tree.get(path)
            if content is None:
                continue
            claim = parse_claim(content, path=path)
            digest = claim_artifact_digest(claim).tagged
            previous = identities.setdefault(digest, claim.identity)
            if previous != claim.identity:
                raise ClaimRetireError("one accepted Claim digest names multiple identities")
    return identities


def _operation_digest(
    *,
    actor_id: str,
    coordinate: AcceptedCoordinate,
    root: ClaimRetireDependentV1,
    dependents: tuple[ClaimRetireDependentV1, ...],
) -> str:
    return typed_digest(
        Sha256Value,
        CLAIM_RETIRE_OPERATION_DOMAIN,
        {
            "actor_principal_id": actor_id,
            "expected_accepted_coordinate": coordinate.model_dump(mode="json"),
            "root": root.model_dump(mode="json"),
            "dependents": [item.model_dump(mode="json") for item in dependents],
        },
    ).tagged


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
) -> ClaimArtifactV3:
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
    return ClaimArtifactV3(
        identity=claim.identity,
        statement=statement,
        backing=claim.backing,
        pins=pins,
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=claim_artifact_digest(claim).tagged,
        ),
        retirement=ClaimRetirementAttributionV1(reason=reason),
    )


def _candidate(
    tree: Mapping[str, bytes],
    *,
    root: ClaimRetireDependentV1,
    dependents: tuple[ClaimRetireDependentV1, ...],
) -> tuple[dict[str, bytes], tuple[ClaimRetirementResultItemV1, ...]]:
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

    candidate_tree = dict(tree)
    successor_digests: dict[str, str] = {}
    successors: dict[str, ClaimArtifactV3] = {}
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


def _accepted_retirement_operation(
    instance: PlaybillInstance,
    *,
    claim_path_value: str,
    request: ClaimRetireRequestV1,
) -> tuple[ClaimRetireDependentV1, tuple[ClaimRetirementResultItemV1, ...]]:
    """Recover exactly the retirement ChangeSet immediately after its bound coordinate."""

    try:
        parent_coordinate = instance.coordinate_for_oid(request.expected_coordinate.git_oid)
    except PlaybillFormatError as exc:
        raise ClaimRetireStale(
            f"{ClaimRetireStale.error_code}: expected coordinate is not accepted history"
        ) from exc
    if request.expected_coordinate != AcceptedCoordinate.from_internal(parent_coordinate):
        raise ClaimRetireStale(
            f"{ClaimRetireStale.error_code}: expected coordinate does not reproduce"
        )

    history = instance.accepted_history()
    parent_index = next(
        index
        for index, generation in enumerate(history)
        if generation.oid == parent_coordinate.git_oid
    )
    if parent_index + 1 >= len(history):
        raise ClaimRetireClosureMismatch(
            f"{ClaimRetireClosureMismatch.error_code}: no accepted retirement follows the "
            "operation coordinate"
        )
    generation = history[parent_index + 1]
    record = generation.record
    if record is None:
        raise ClaimRetireClosureMismatch(
            f"{ClaimRetireClosureMismatch.error_code}: retirement generation lacks a ChangeSet"
        )
    parent_tree = instance.tree_at(parent_coordinate.git_oid)
    candidate_tree = instance.tree_at(generation.oid)
    retired: dict[str, tuple[ClaimArtifactAny, ClaimArtifactV3]] = {}
    for member in record.members:
        if not member.path.startswith("claims/") or member.path not in candidate_tree:
            continue
        predecessor_content = parent_tree.get(member.path)
        if predecessor_content is None:
            continue
        predecessor = parse_claim(predecessor_content, path=member.path)
        successor = parse_claim(candidate_tree[member.path], path=member.path)
        if (
            isinstance(successor, ClaimArtifactV3)
            and predecessor.lifecycle.state == "live"
            and successor.lifecycle.predecessor_digest == claim_artifact_digest(predecessor).tagged
        ):
            retired[successor.identity.qualified] = (predecessor, successor)

    root_pair = next(
        (
            pair
            for pair in retired.values()
            if claim_path(pair[1].identity.name) == claim_path_value
        ),
        None,
    )
    if root_pair is None:
        raise ClaimRetireClosureMismatch(
            f"{ClaimRetireClosureMismatch.error_code}: bound ChangeSet did not retire the Claim"
        )
    root_predecessor, root_successor = root_pair
    supplied = {item.artifact_identity.qualified: item for item in request.dependents}
    expected_dependents = set(retired) - {root_successor.identity.qualified}
    if set(supplied) != expected_dependents:
        raise ClaimRetireClosureMismatch(
            f"{ClaimRetireClosureMismatch.error_code}: accepted retirement closure differs"
        )

    root_request = ClaimRetireDependentV1(
        artifact_identity=root_successor.identity,
        predecessor_digest=claim_artifact_digest(root_predecessor).tagged,
        reason=request.reason,
        effective_until=request.effective_until,
    )
    entries = {root_successor.identity.qualified: root_request, **supplied}
    results: list[ClaimRetirementResultItemV1] = []
    for identity in sorted(retired, key=lambda item: item.encode("utf-8")):
        predecessor, successor = retired[identity]
        entry = entries[identity]
        expected_effective_until = (
            predecessor.statement.effective_until
            if entry.effective_until is None
            else entry.effective_until
        )
        if (
            entry.artifact_identity != successor.identity
            or entry.predecessor_digest != claim_artifact_digest(predecessor).tagged
            or entry.reason != successor.retirement.reason
            or expected_effective_until != successor.statement.effective_until
        ):
            raise ClaimRetireClosureMismatch(
                f"{ClaimRetireClosureMismatch.error_code}: accepted retirement attribution "
                f"differs for {identity}"
            )
        results.append(
            ClaimRetirementResultItemV1(
                artifact_identity=successor.identity,
                predecessor_digest=entry.predecessor_digest,
                reason=successor.retirement.reason,
                effective_until=successor.statement.effective_until,
                successor_digest=claim_artifact_digest(successor).tagged,
            )
        )
    return root_request, tuple(results)


def service_retire_claim(
    instance: PlaybillInstance,
    *,
    claim_id: str,
    request: ClaimRetireRequestV1,
    actor: AuthenticatedActor,
) -> ClaimRetireResponse:
    """Preflight or submit one complete attributed Claim retirement ChangeSet."""

    bare_id = claim_id.removeprefix("Claim:")
    path = claim_path(bare_id)
    if request.claim_ref is not None and request.claim_ref.removeprefix("Claim:") != bare_id:
        raise ClaimRetireError("request claim_ref differs from the route Claim")
    current = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(current)
    tree = instance.tree_at(current.git_oid)
    content = tree.get(path)
    if content is None:
        raise ClaimRetireError(f"Claim not found: {claim_id}")
    claim = parse_claim(content, path=path)
    if isinstance(claim, ClaimArtifactV3) and claim.lifecycle.state == "retired":
        root, terminal_retirements = _accepted_retirement_operation(
            instance,
            claim_path_value=path,
            request=request,
        )
        return ClaimRetireResultV1(
            outcome="already_retired",
            operation_digest=_operation_digest(
                actor_id=actor.actor_id,
                coordinate=request.expected_coordinate,
                root=root,
                dependents=request.dependents,
            ),
            coordinate=PlaybillAcceptedCoordinate.from_internal(current),
            retirements=terminal_retirements,
        )
    if claim.lifecycle.state == "retired":
        raise ClaimRetireError("historical unattributed retirement is already terminal")
    if request.expected_coordinate != coordinate:
        raise ClaimRetireStale(
            f"{ClaimRetireStale.error_code}: expected coordinate is not the accepted head"
        )

    closure = reverse_pin_closure(
        tree,
        root=claim.identity,
        include=lambda state: state.lifecycle.state == "live",
        claim_identity_by_digest=_claim_identity_by_digest(instance, coordinate=coordinate),
    )
    unsupported = tuple(item for item in closure if item.state.artifact_kind != "claim")
    inventory = _inventory(closure)
    root_request = ClaimRetireDependentV1(
        artifact_identity=claim.identity,
        predecessor_digest=claim_artifact_digest(claim).tagged,
        reason=request.reason,
        effective_until=request.effective_until,
    )
    operation_digest = _operation_digest(
        actor_id=actor.actor_id,
        coordinate=coordinate,
        root=root_request,
        dependents=request.dependents,
    )
    if unsupported:
        names = tuple(item.state.identity.qualified for item in unsupported)
        raise ClaimRetireDependentUnsupported(
            f"{ClaimRetireDependentUnsupported.error_code}: {names!r}"
        )

    expected = {item.artifact_identity.qualified: item.predecessor_digest for item in inventory}
    supplied = {
        item.artifact_identity.qualified: item.predecessor_digest for item in request.dependents
    }
    complete = supplied == expected
    if request.mode == "preflight" and not complete:
        return ClaimRetirePreflightV1(
            operation_digest=operation_digest,
            coordinate=PlaybillAcceptedCoordinate.from_internal(current),
            root_identity=claim.identity,
            root_predecessor_digest=root_request.predecessor_digest,
            reason=request.reason,
            effective_until=request.effective_until,
            required_dependents=inventory,
            diagnostics=(),
            submit_ready=not inventory,
        )
    if not complete:
        raise ClaimRetireClosureMismatch(
            f"{ClaimRetireClosureMismatch.error_code}: expected={expected!r}; supplied={supplied!r}"
        )

    candidate_tree, candidate_retirements = _candidate(
        tree,
        root=root_request,
        dependents=request.dependents,
    )
    timestamp = _generation_timestamp(instance)
    validated = validate_proposal_tree(
        candidate_tree,
        limits=instance.proposal_service().receive_limits,
        base_tree=tree,
    )
    evaluation = evaluate_proposal_tree(
        base_tree=tree,
        current_tree=tree,
        proposed_tree=validated,
        current=current,
        bodies=instance.body_store(),
        timestamp=timestamp,
        rebased=False,
        actor_id=actor.actor_id,
        promotion_verifier=instance.proposal_service().promotion_verifier,
        query_facts_provider=instance.proposal_service().query_facts_provider,
    )
    if request.mode == "preflight":
        return ClaimRetirePreflightV1(
            operation_digest=operation_digest,
            coordinate=PlaybillAcceptedCoordinate.from_internal(current),
            root_identity=claim.identity,
            root_predecessor_digest=root_request.predecessor_digest,
            reason=request.reason,
            effective_until=request.effective_until,
            required_dependents=inventory,
            diagnostics=evaluation.diagnostics,
            submit_ready=evaluation.candidate is not None,
        )
    if evaluation.candidate is None:
        raise ClaimRetireError(
            "candidate refused before admission: "
            + ", ".join(item.code for item in evaluation.diagnostics)
        )
    target_ref = (
        f"refs/proposals/{actor.actor_id}/claim-retire-{operation_digest.removeprefix('sha256:')}"
    )
    proposal = instance.proposal_service().submit(
        actor=actor,
        request=ProposalAdmissionRequest(
            target_ref=target_ref,
            proposed_base_oid=current.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    return ClaimRetireResultV1(
        outcome="proposed",
        operation_digest=operation_digest,
        coordinate=PlaybillAcceptedCoordinate.from_internal(current),
        retirements=candidate_retirements,
        proposal=PlaybillProposalInspection(
            proposal=proposal,
            workspace_advertisement=proposal.workspace_advertisement,
            accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
                instance.accepted_coordinate()
            ),
        ),
    )


__all__ = [
    "CLAIM_RETIRE_OPERATION_DOMAIN",
    "ClaimRetireClosureMismatch",
    "ClaimRetireDependentUnsupported",
    "ClaimRetireDependentV1",
    "ClaimRetireError",
    "ClaimRetirePreflightV1",
    "ClaimRetireRequestV1",
    "ClaimRetireResponse",
    "ClaimRetireResultV1",
    "ClaimRetireStale",
    "service_retire_claim",
]
