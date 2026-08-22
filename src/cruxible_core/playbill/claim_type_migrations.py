"""Atomic ClaimType succession with explicit dependent-Claim dispositions."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_core.playbill.artifacts import ArtifactLifecycle, ArtifactPin
from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1, lower_claim_type_input
from cruxible_core.playbill.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_core.playbill.claims import (
    ClaimArtifactAny,
    claim_artifact_digest,
    parse_claim,
    render_claim,
)
from cruxible_core.playbill.errors import PlaybillError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)

CLAIM_TYPE_MIGRATION_DOMAIN = "playbill-claim-type-migration-v1"


class _StrictMigrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


MigrationInputDisposition: TypeAlias = Literal["successor", "retire", "invalidation"]
MigrationResultDisposition: TypeAlias = Literal["successor", "retire"]


class ClaimTypeDependentDispositionV1(_StrictMigrationModel):
    claim_id: str
    disposition: MigrationInputDisposition


class ClaimTypeMigrationRequestV1(_StrictMigrationModel):
    tag: Literal["playbill-claim-type-migration-request-v1"] = (
        "playbill-claim-type-migration-request-v1"
    )
    successor: ClaimTypeInputV1 | ClaimType
    dependents: tuple[ClaimTypeDependentDispositionV1, ...]

    @field_validator("dependents")
    @classmethod
    def _dependents(
        cls,
        value: tuple[ClaimTypeDependentDispositionV1, ...],
    ) -> tuple[ClaimTypeDependentDispositionV1, ...]:
        ids = tuple(item.claim_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("ascii"))):
            raise ValueError("migration dependents must be byte-sorted and unique")
        return value


class ClaimTypeMigrationDispositionV1(_StrictMigrationModel):
    claim_id: str
    disposition: MigrationResultDisposition


class ClaimTypeMigrationResultV1(_StrictMigrationModel):
    tag: Literal["playbill-claim-type-migration-result-v1"] = (
        "playbill-claim-type-migration-result-v1"
    )
    operation_digest: str
    dependents: tuple[ClaimTypeMigrationDispositionV1, ...]
    proposal: PlaybillProposalInspection


class ClaimTypeMigrationError(PlaybillError):
    code = "playbill.claim_type.migration_invalid"


def _current_generation_timestamp(instance: PlaybillInstance) -> str:
    record = instance.accepted_history()[-1].record
    if record is None:
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: no accepted ClaimType can exist at genesis"
        )
    return record.candidate.timestamp


def _normalized_dependents(
    dependents: tuple[ClaimTypeDependentDispositionV1, ...],
) -> tuple[ClaimTypeMigrationDispositionV1, ...]:
    return tuple(
        ClaimTypeMigrationDispositionV1(
            claim_id=item.claim_id,
            disposition=("retire" if item.disposition == "invalidation" else item.disposition),
        )
        for item in dependents
    )


def _operation_digest(
    *,
    actor_id: str,
    coordinate: AcceptedCoordinate,
    successor: ClaimType,
    dependents: tuple[ClaimTypeMigrationDispositionV1, ...],
) -> str:
    return typed_digest(
        Sha256Value,
        CLAIM_TYPE_MIGRATION_DOMAIN,
        {
            "actor_id": actor_id,
            "current_accepted_coordinate": coordinate.model_dump(mode="json"),
            "normalized_request": {
                "successor": successor.model_dump(mode="json"),
                "dependents": [item.model_dump(mode="json") for item in dependents],
            },
        },
    ).tagged


def _current_dependents(
    tree: dict[str, bytes],
    *,
    identity: str,
) -> dict[str, tuple[str, ClaimArtifactAny]]:
    result: dict[str, tuple[str, ClaimArtifactAny]] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if claim.lifecycle.state != "live" or claim.statement.claim_type.qualified != identity:
            continue
        result[claim.identity.name] = (path, claim)
    return result


def _successor_claim(
    claim: ClaimArtifactAny,
    *,
    successor_type: ClaimType,
    disposition: MigrationResultDisposition,
) -> ClaimArtifactAny:
    successor_digest = claim_type_digest(successor_type).tagged
    statement = claim.statement.model_copy(update={"claim_type_digest": successor_digest})
    found = False
    pins: list[ArtifactPin] = []
    for pin in claim.pins:
        if pin.target == successor_type.identity and pin.role == "claim-type":
            pins.append(pin.model_copy(update={"artifact_digest": successor_digest}))
            found = True
        else:
            pins.append(pin)
    if not found:
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: dependent {claim.identity.name} lacks its "
            "ClaimType pin"
        )
    pins.sort(
        key=lambda item: (
            item.role.encode("utf-8"),
            item.target.qualified.encode("utf-8"),
        )
    )
    return claim.model_copy(
        update={
            "statement": statement,
            "pins": tuple(pins),
            "lifecycle": ArtifactLifecycle(
                state="retired" if disposition == "retire" else "live",
                predecessor_digest=claim_artifact_digest(claim).tagged,
            ),
        }
    )


def service_migrate_claim_type(
    instance: PlaybillInstance,
    *,
    request: ClaimTypeMigrationRequestV1,
    actor: AuthenticatedActor,
) -> ClaimTypeMigrationResultV1:
    """Build and submit one exact ClaimType + dependent-Claim migration candidate."""

    current = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(current)
    tree = instance.tree_at(current.git_oid)
    successor = (
        request.successor
        if isinstance(request.successor, ClaimType)
        else lower_claim_type_input(request.successor, tree=tree)
    )
    type_path = claim_type_path(successor.predicate)
    predecessor_content = tree.get(type_path)
    if predecessor_content is None:
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: migration requires an accepted predecessor"
        )
    predecessor = parse_claim_type(predecessor_content, path=type_path)
    if successor.identity != predecessor.identity:
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: successor changes ClaimType identity"
        )
    if successor.lifecycle.predecessor_digest != claim_type_digest(predecessor).tagged:
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: successor does not pin the current ClaimType"
        )

    current_dependents = _current_dependents(tree, identity=successor.identity.qualified)
    normalized = _normalized_dependents(request.dependents)
    requested_ids = {item.claim_id for item in normalized}
    current_ids = set(current_dependents)
    missing = sorted(current_ids - requested_ids, key=lambda item: item.encode("ascii"))
    extra = sorted(requested_ids - current_ids, key=lambda item: item.encode("ascii"))
    if missing or extra:
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: dependent set differs from the accepted "
            f"coordinate; missing={missing!r}; extra_or_stale={extra!r}"
        )

    candidate_tree = dict(tree)
    candidate_tree[type_path] = render_claim_type(successor)
    for disposition in normalized:
        path, claim = current_dependents[disposition.claim_id]
        candidate_tree[path] = render_claim(
            _successor_claim(
                claim,
                successor_type=successor,
                disposition=disposition.disposition,
            )
        )

    operation_digest = _operation_digest(
        actor_id=actor.actor_id,
        coordinate=coordinate,
        successor=successor,
        dependents=normalized,
    )
    target_ref = (
        f"refs/proposals/{actor.actor_id}/claim-type-migration-"
        f"{operation_digest.removeprefix('sha256:')}"
    )
    proposal = instance.proposal_service().submit(
        actor=actor,
        request=ProposalAdmissionRequest(
            target_ref=target_ref,
            proposed_base_oid=current.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=_current_generation_timestamp(instance),
    )
    return ClaimTypeMigrationResultV1(
        operation_digest=operation_digest,
        dependents=normalized,
        proposal=PlaybillProposalInspection(
            proposal=proposal,
            accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
                instance.accepted_coordinate()
            ),
        ),
    )


__all__ = [
    "CLAIM_TYPE_MIGRATION_DOMAIN",
    "ClaimTypeDependentDispositionV1",
    "ClaimTypeMigrationDispositionV1",
    "ClaimTypeMigrationError",
    "ClaimTypeMigrationRequestV1",
    "ClaimTypeMigrationResultV1",
    "service_migrate_claim_type",
]
