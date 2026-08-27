"""Atomic ClaimType succession over the complete reverse-pin closure."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV3,
    ClaimRetirementAttributionV1,
    ClaimRetirementReason,
    claim_artifact_digest,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.models import ProcedureDefinitionV3
from cruxible_core.playbill.claim_type_inputs import (
    ClaimTypeInputV1,
    ClaimTypeProposalLintV1,
    lint_claim_type_input,
    lower_claim_type_input,
)
from cruxible_core.playbill.closure import (
    ArtifactDependencyStateV1,
    parse_dependency_artifact,
    reverse_pin_closure,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
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


class ClaimTypeDependentDispositionV2(_StrictMigrationModel):
    identity: ArtifactIdentity
    disposition: MigrationInputDisposition
    successor: dict[str, object] | None = None


class ClaimTypeDependentDispositionV3(_StrictMigrationModel):
    tag: Literal["playbill-claim-type-dependent-disposition-v3"] = (
        "playbill-claim-type-dependent-disposition-v3"
    )
    identity: ArtifactIdentity
    disposition: MigrationInputDisposition
    successor: dict[str, object] | None = None
    claim_retirement_reason: ClaimRetirementReason | None = None
    claim_effective_until: datetime | None = None

    @field_validator("claim_effective_until")
    @classmethod
    def _effective_until(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("claim_effective_until must be timezone-aware")
        return value


class ClaimTypeMigrationRequestV2(_StrictMigrationModel):
    tag: Literal["playbill-claim-type-migration-request-v2"] = (
        "playbill-claim-type-migration-request-v2"
    )
    mode: Literal["preflight", "submit"]
    successor: ClaimTypeInputV1 | ClaimType
    dependents: tuple[ClaimTypeDependentDispositionV2, ...] = ()

    @field_validator("dependents")
    @classmethod
    def _dependents(
        cls,
        value: tuple[ClaimTypeDependentDispositionV2, ...],
    ) -> tuple[ClaimTypeDependentDispositionV2, ...]:
        identities = tuple(item.identity.qualified for item in value)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("migration dependents must be UTF-8 byte-sorted and unique")
        return value


class ClaimTypeMigrationRequestV3(_StrictMigrationModel):
    tag: Literal["playbill-claim-type-migration-request-v3"] = (
        "playbill-claim-type-migration-request-v3"
    )
    mode: Literal["preflight", "submit"]
    successor: ClaimTypeInputV1 | ClaimType
    dependents: tuple[ClaimTypeDependentDispositionV3, ...] = ()

    @field_validator("dependents")
    @classmethod
    def _dependents(
        cls,
        value: tuple[ClaimTypeDependentDispositionV3, ...],
    ) -> tuple[ClaimTypeDependentDispositionV3, ...]:
        identities = tuple(item.identity.qualified for item in value)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("migration dependents must be UTF-8 byte-sorted and unique")
        return value


ClaimTypeMigrationRequest: TypeAlias = (
    ClaimTypeMigrationRequestV1 | ClaimTypeMigrationRequestV2 | ClaimTypeMigrationRequestV3
)


class ClaimTypeMigrationDispositionV1(_StrictMigrationModel):
    claim_id: str
    disposition: MigrationResultDisposition


class ClaimTypeMigrationWarningV1(_StrictMigrationModel):
    code: Literal["playbill.claim_type.invalidation_deprecated"]
    field_path: str
    repair_operation: Literal["playbill.claim_type.migrate"] = "playbill.claim_type.migrate"


class ClaimTypeMigrationResultV1(_StrictMigrationModel):
    tag: Literal["playbill-claim-type-migration-result-v1"] = (
        "playbill-claim-type-migration-result-v1"
    )
    operation_digest: str
    dependents: tuple[ClaimTypeMigrationDispositionV1, ...]
    proposal: PlaybillProposalInspection
    warnings: tuple[ClaimTypeMigrationWarningV1, ...] = ()
    lint: ClaimTypeProposalLintV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ClaimTypeMigrationInventoryItemV1(_StrictMigrationModel):
    identity: ArtifactIdentity
    path: str
    artifact_kind: str
    current_artifact_digest: str
    triggering_identity: ArtifactIdentity
    dependency_edge_role: str
    permitted_dispositions: tuple[MigrationResultDisposition, ...]
    diagnostics: tuple[str, ...] = ()


class ClaimTypeMigrationPreflightV1(_StrictMigrationModel):
    tag: Literal["playbill-claim-type-migration-preflight-v1"] = (
        "playbill-claim-type-migration-preflight-v1"
    )
    coordinate: PlaybillAcceptedCoordinate
    successor_artifact_digest: str
    dependents: tuple[ClaimTypeMigrationInventoryItemV1, ...]
    warnings: tuple[ClaimTypeMigrationWarningV1, ...] = ()
    lint: ClaimTypeProposalLintV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ClaimTypeMigrationDispositionV2(_StrictMigrationModel):
    identity: ArtifactIdentity
    disposition: MigrationResultDisposition


class ClaimTypeMigrationResultV2(_StrictMigrationModel):
    tag: Literal["playbill-claim-type-migration-result-v2"] = (
        "playbill-claim-type-migration-result-v2"
    )
    operation_digest: str
    dependents: tuple[ClaimTypeMigrationDispositionV2, ...]
    proposal: PlaybillProposalInspection
    warnings: tuple[ClaimTypeMigrationWarningV1, ...] = ()
    lint: ClaimTypeProposalLintV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ClaimTypeMigrationDispositionV3(_StrictMigrationModel):
    identity: ArtifactIdentity
    disposition: MigrationResultDisposition
    claim_retirement_reason: ClaimRetirementReason | None = None
    claim_effective_until: datetime | None = None


class ClaimTypeMigrationResultV3(_StrictMigrationModel):
    tag: Literal["playbill-claim-type-migration-result-v3"] = (
        "playbill-claim-type-migration-result-v3"
    )
    operation_digest: str
    dependents: tuple[ClaimTypeMigrationDispositionV3, ...]
    proposal: PlaybillProposalInspection
    warnings: tuple[ClaimTypeMigrationWarningV1, ...] = ()
    lint: ClaimTypeProposalLintV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


ClaimTypeMigrationResponse: TypeAlias = (
    ClaimTypeMigrationResultV1
    | ClaimTypeMigrationPreflightV1
    | ClaimTypeMigrationResultV2
    | ClaimTypeMigrationResultV3
)


class ClaimTypeMigrationError(PlaybillFormatError):
    code = "playbill.claim_type.migration_invalid"


class ClaimTypeMigrationDependentSetMismatch(ClaimTypeMigrationError):
    code = "playbill.claim_type.migration_dependent_set_mismatch"


class ClaimTypeMigrationDependentInvalid(ClaimTypeMigrationError):
    code = "playbill.claim_type.migration_dependent_invalid"


class ClaimTypeMigrationIncomplete(ClaimTypeMigrationError):
    code = "playbill.claim_type.migration_incomplete"


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


def _invalidation_warnings(
    dependents: tuple[
        ClaimTypeDependentDispositionV1
        | ClaimTypeDependentDispositionV2
        | ClaimTypeDependentDispositionV3,
        ...,
    ],
) -> tuple[ClaimTypeMigrationWarningV1, ...]:
    return tuple(
        ClaimTypeMigrationWarningV1(
            code="playbill.claim_type.invalidation_deprecated",
            field_path=f"$.dependents[{index}].disposition",
        )
        for index, item in enumerate(dependents)
        if item.disposition == "invalidation"
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
    """Return every live or retired Claim directly governed by one ClaimType."""

    result: dict[str, tuple[str, ClaimArtifactAny]] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if (
            not _include_migration_dependent(
                artifact_kind="claim",
                lifecycle_state=claim.lifecycle.state,
            )
            or claim.statement.claim_type.qualified != identity
        ):
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
            "authority": successor_type.authority,
            "pins": tuple(pins),
            "lifecycle": ArtifactLifecycle(
                state=(
                    "retired"
                    if claim.lifecycle.state == "retired" or disposition == "retire"
                    else "live"
                ),
                predecessor_digest=claim_artifact_digest(claim).tagged,
            ),
        }
    )


def _successor_claim_type(
    tree: Mapping[str, bytes],
    value: ClaimTypeInputV1 | ClaimType,
) -> tuple[str, ClaimType, ClaimType]:
    successor = (
        value if isinstance(value, ClaimType) else lower_claim_type_input(value, tree=dict(tree))
    )
    if any(
        pin.role == "predecessor" and pin.target == successor.identity for pin in successor.pins
    ):
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: predecessor succession is machine-owned; "
            "remove the hand-authored $.successor.pins predecessor entry"
        )
    path = claim_type_path(successor.predicate)
    predecessor_content = tree.get(path)
    if predecessor_content is None:
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: migration requires an accepted predecessor"
        )
    predecessor = parse_claim_type(predecessor_content, path=path)
    if successor.identity != predecessor.identity:
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: successor changes ClaimType identity"
        )
    if successor.lifecycle.predecessor_digest != claim_type_digest(predecessor).tagged:
        raise ClaimTypeMigrationError(
            f"{ClaimTypeMigrationError.code}: successor does not pin the current ClaimType"
        )
    return path, predecessor, successor


def _include_migration_dependent(
    *,
    artifact_kind: str,
    lifecycle_state: str,
) -> bool:
    """Include the live closure plus retired Claims whose law evidence is still read."""

    return lifecycle_state == "live" or artifact_kind == "claim"


def _closure_inventory(
    tree: Mapping[str, bytes],
    *,
    root: ArtifactIdentity,
) -> tuple[ClaimTypeMigrationInventoryItemV1, ...]:
    """Return the live reverse-pin closure plus its retired Claim members."""

    try:
        closure = reverse_pin_closure(
            tree,
            root=root,
            include=lambda state: _include_migration_dependent(
                artifact_kind=state.artifact_kind,
                lifecycle_state=state.lifecycle.state,
            ),
        )
    except ValueError as exc:
        raise ClaimTypeMigrationIncomplete(f"{ClaimTypeMigrationIncomplete.code}: {exc}") from exc
    return tuple(
        ClaimTypeMigrationInventoryItemV1(
            identity=item.state.identity,
            path=item.state.path,
            artifact_kind=item.state.artifact_kind,
            current_artifact_digest=item.state.artifact_digest,
            triggering_identity=item.triggering_identity,
            dependency_edge_role=item.dependency_edge_roles[0],
            permitted_dispositions=(
                ("successor",)
                if item.state.artifact_kind == "document" or item.state.lifecycle.state == "retired"
                else ("retire", "successor")
            ),
        )
        for item in closure
    )


def _replace_exact_digests(value: object, replacements: Mapping[str, str]) -> object:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_exact_digests(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_exact_digests(item, replacements) for key, item in value.items()}
    return value


def _canonical_successor_bytes(
    *,
    current: ArtifactDependencyStateV1,
    content: bytes,
    disposition: MigrationResultDisposition,
    replacements: Mapping[str, str],
    supplied: dict[str, object] | None,
    successor_type: ClaimType,
    claim_retirement_reason: ClaimRetirementReason | None = None,
    claim_effective_until: datetime | None = None,
) -> bytes:
    if disposition == "retire" and current.artifact_kind == "document":
        raise ClaimTypeMigrationDependentInvalid(
            f"{ClaimTypeMigrationDependentInvalid.code}: Document has no retired v1 state"
        )
    if current.artifact_kind != "claim" and (
        claim_retirement_reason is not None or claim_effective_until is not None
    ):
        raise ClaimTypeMigrationDependentInvalid(
            f"{ClaimTypeMigrationDependentInvalid.code}: retirement attribution fields are "
            "Claim-only"
        )
    if current.artifact_kind == "claim":
        current_claim = parse_claim(content, path=current.path)
        if current.lifecycle.state == "live" and disposition == "retire":
            if claim_retirement_reason is None:
                raise ClaimTypeMigrationDependentInvalid(
                    "playbill.claim.retirement_reason_required: live Claim retirement "
                    "requires claim_retirement_reason"
                )
            if supplied is not None:
                raise ClaimTypeMigrationDependentInvalid(
                    f"{ClaimTypeMigrationDependentInvalid.code}: attributed Claim retirement "
                    "successor bytes are machine-owned"
                )
            successor_digest = claim_type_digest(successor_type).tagged
            statement = current_claim.statement.model_copy(
                update={
                    "claim_type_digest": successor_digest,
                    **(
                        {}
                        if claim_effective_until is None
                        else {"effective_until": claim_effective_until}
                    ),
                }
            )
            pins = tuple(
                pin.model_copy(
                    update={
                        "artifact_digest": replacements.get(
                            pin.artifact_digest, pin.artifact_digest
                        )
                    }
                )
                for pin in current_claim.pins
            )
            retired = ClaimArtifactV3(
                identity=current_claim.identity,
                statement=statement,
                backing=current_claim.backing,
                authority=successor_type.authority,
                pins=pins,
                lifecycle=ArtifactLifecycle(
                    state="retired",
                    predecessor_digest=current.artifact_digest,
                ),
                retirement=ClaimRetirementAttributionV1(reason=claim_retirement_reason),
            )
            return render_claim(retired)
        if claim_retirement_reason is not None or claim_effective_until is not None:
            raise ClaimTypeMigrationDependentInvalid(
                f"{ClaimTypeMigrationDependentInvalid.code}: retirement attribution is valid "
                "only for a live Claim retire disposition"
            )
    if supplied is None:
        payload = _replace_exact_digests(json.loads(content), replacements)
        if not isinstance(payload, dict):
            raise ClaimTypeMigrationDependentInvalid(
                f"{ClaimTypeMigrationDependentInvalid.code}: dependent is not an envelope"
            )
        if current.artifact_kind == "document":
            lifecycle = payload.get("lifecycle")
            if not isinstance(lifecycle, dict) or not isinstance(lifecycle.get("revision"), int):
                raise ClaimTypeMigrationDependentInvalid(
                    f"{ClaimTypeMigrationDependentInvalid.code}: Document lifecycle is malformed"
                )
            lifecycle["revision"] += 1
            payload["predecessor_digest"] = current.artifact_digest
        else:
            payload["lifecycle"] = {
                "state": (
                    "retired"
                    if current.lifecycle.state == "retired" or disposition == "retire"
                    else "live"
                ),
                "predecessor_digest": current.artifact_digest,
            }
        if current.artifact_kind == "claim":
            payload["authority"] = successor_type.authority.model_dump(mode="json")
        if current.artifact_kind == "procedure":
            try:
                definition = ProcedureDefinitionV3.model_validate(payload["definition"])
            except ValidationError as exc:
                validation_details = []
                for error in exc.errors(include_url=False):
                    path = "$.definition" + "".join(
                        f"[{part}]" if isinstance(part, int) else f".{part}"
                        for part in error["loc"]
                    )
                    validation_details.append(f"{path}: {error['msg']}")
                details = "; ".join(validation_details)
                raise ClaimTypeMigrationDependentInvalid(
                    f"{ClaimTypeMigrationDependentInvalid.code}: dependent "
                    f"{current.identity.qualified} has an invalid procedure definition; {details}"
                ) from exc
            payload["definition_digest"] = compute_procedure_definition_digest_v3(definition).tagged
    else:
        payload = supplied
    try:
        candidate = canonical_bytes(payload) + b"\n"
        parsed = parse_dependency_artifact(current.path, candidate)
    except (PlaybillError, TypeError, ValueError) as exc:
        raise ClaimTypeMigrationDependentInvalid(
            f"{ClaimTypeMigrationDependentInvalid.code}: supplied successor is invalid for "
            f"{current.identity.qualified}"
        ) from exc
    if parsed is None or parsed.identity != current.identity:
        raise ClaimTypeMigrationDependentInvalid(
            f"{ClaimTypeMigrationDependentInvalid.code}: successor changes dependent identity"
        )
    if parsed.lifecycle.predecessor_digest != current.artifact_digest:
        raise ClaimTypeMigrationDependentInvalid(
            f"{ClaimTypeMigrationDependentInvalid.code}: successor lacks exact predecessor"
        )
    expected_state = (
        "retired" if current.lifecycle.state == "retired" or disposition == "retire" else "live"
    )
    if parsed.lifecycle.state != expected_state:
        raise ClaimTypeMigrationDependentInvalid(
            f"{ClaimTypeMigrationDependentInvalid.code}: successor lifecycle disagrees "
            "with disposition"
        )
    return candidate


def _v2_operation_digest(
    *,
    actor_id: str,
    coordinate: AcceptedCoordinate,
    successor: ClaimType,
    dependents: tuple[ClaimTypeMigrationDispositionV2, ...],
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


def _build_v2_candidate(
    *,
    tree: Mapping[str, bytes],
    type_path: str,
    successor: ClaimType,
    inventory: tuple[ClaimTypeMigrationInventoryItemV1, ...],
    dispositions: tuple[ClaimTypeDependentDispositionV2, ...],
) -> tuple[dict[str, bytes], tuple[ClaimTypeMigrationDispositionV2, ...]]:
    by_identity = {item.identity.qualified: item for item in inventory}
    supplied = {item.identity.qualified: item for item in dispositions}
    if set(by_identity) != set(supplied):
        missing = sorted(set(by_identity) - set(supplied), key=lambda item: item.encode("utf-8"))
        extra = sorted(set(supplied) - set(by_identity), key=lambda item: item.encode("utf-8"))
        raise ClaimTypeMigrationDependentSetMismatch(
            f"{ClaimTypeMigrationDependentSetMismatch.code}: missing={missing!r}; "
            f"extra_or_stale={extra!r}"
        )

    candidate_tree = dict(tree)
    candidate_tree[type_path] = render_claim_type(successor)
    replacements = {
        claim_type_digest(
            parse_claim_type(tree[type_path], path=type_path)
        ).tagged: claim_type_digest(successor).tagged
    }
    remaining = set(by_identity)
    normalized: dict[str, ClaimTypeMigrationDispositionV2] = {}
    while remaining:
        progressed = False
        for identity in sorted(remaining, key=lambda item: item.encode("utf-8")):
            row = by_identity[identity]
            current = parse_dependency_artifact(row.path, tree[row.path])
            if current is None:
                raise ClaimTypeMigrationIncomplete(
                    f"{ClaimTypeMigrationIncomplete.code}: inventory member disappeared"
                )
            unresolved_changed_targets = {
                pin.target.qualified for pin in current.pins if pin.target.qualified in remaining
            }
            if unresolved_changed_targets:
                continue
            entry = supplied[identity]
            disposition: MigrationResultDisposition = (
                "retire" if entry.disposition == "invalidation" else entry.disposition
            )
            if disposition not in row.permitted_dispositions:
                raise ClaimTypeMigrationDependentInvalid(
                    f"{ClaimTypeMigrationDependentInvalid.code}: {identity} does not permit "
                    f"{disposition}"
                )
            content = _canonical_successor_bytes(
                current=current,
                content=tree[row.path],
                disposition=disposition,
                replacements=replacements,
                supplied=entry.successor,
                successor_type=successor,
            )
            candidate_tree[row.path] = content
            successor_state = parse_dependency_artifact(row.path, content)
            if successor_state is None:
                raise ClaimTypeMigrationIncomplete(
                    f"{ClaimTypeMigrationIncomplete.code}: successor did not parse"
                )
            replacements[current.artifact_digest] = successor_state.artifact_digest
            normalized[identity] = ClaimTypeMigrationDispositionV2(
                identity=current.identity,
                disposition=disposition,
            )
            remaining.remove(identity)
            progressed = True
            break
        if not progressed:
            raise ClaimTypeMigrationIncomplete(
                f"{ClaimTypeMigrationIncomplete.code}: dependent closure contains a cycle"
            )
    return candidate_tree, tuple(
        normalized[identity]
        for identity in sorted(normalized, key=lambda item: item.encode("utf-8"))
    )


def _v3_operation_digest(
    *,
    actor_id: str,
    coordinate: AcceptedCoordinate,
    successor: ClaimType,
    dependents: tuple[ClaimTypeMigrationDispositionV3, ...],
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


def _build_v3_candidate(
    *,
    tree: Mapping[str, bytes],
    type_path: str,
    successor: ClaimType,
    inventory: tuple[ClaimTypeMigrationInventoryItemV1, ...],
    dispositions: tuple[ClaimTypeDependentDispositionV3, ...],
) -> tuple[
    dict[str, bytes],
    tuple[ClaimTypeMigrationDispositionV3, ...],
    tuple[ClaimTypeMigrationWarningV1, ...],
]:
    by_identity = {item.identity.qualified: item for item in inventory}
    supplied = {item.identity.qualified: item for item in dispositions}
    if set(by_identity) != set(supplied):
        missing = sorted(set(by_identity) - set(supplied), key=lambda item: item.encode("utf-8"))
        extra = sorted(set(supplied) - set(by_identity), key=lambda item: item.encode("utf-8"))
        raise ClaimTypeMigrationDependentSetMismatch(
            f"{ClaimTypeMigrationDependentSetMismatch.code}: missing={missing!r}; "
            f"extra_or_stale={extra!r}"
        )

    candidate_tree = dict(tree)
    candidate_tree[type_path] = render_claim_type(successor)
    replacements = {
        claim_type_digest(parse_claim_type(tree[type_path], path=type_path)).tagged: (
            claim_type_digest(successor).tagged
        )
    }
    remaining = set(by_identity)
    normalized: dict[str, ClaimTypeMigrationDispositionV3] = {}
    while remaining:
        progressed = False
        for identity in sorted(remaining, key=lambda item: item.encode("utf-8")):
            row = by_identity[identity]
            current = parse_dependency_artifact(row.path, tree[row.path])
            if current is None:
                raise ClaimTypeMigrationIncomplete(
                    f"{ClaimTypeMigrationIncomplete.code}: inventory member disappeared"
                )
            if {pin.target.qualified for pin in current.pins}.intersection(remaining):
                continue
            entry = supplied[identity]
            disposition: MigrationResultDisposition = (
                "retire" if entry.disposition == "invalidation" else entry.disposition
            )
            if disposition not in row.permitted_dispositions:
                raise ClaimTypeMigrationDependentInvalid(
                    f"{ClaimTypeMigrationDependentInvalid.code}: {identity} does not permit "
                    f"{disposition}"
                )
            content = _canonical_successor_bytes(
                current=current,
                content=tree[row.path],
                disposition=disposition,
                replacements=replacements,
                supplied=entry.successor,
                successor_type=successor,
                claim_retirement_reason=entry.claim_retirement_reason,
                claim_effective_until=entry.claim_effective_until,
            )
            candidate_tree[row.path] = content
            successor_state = parse_dependency_artifact(row.path, content)
            if successor_state is None:
                raise ClaimTypeMigrationIncomplete(
                    f"{ClaimTypeMigrationIncomplete.code}: successor did not parse"
                )
            replacements[current.artifact_digest] = successor_state.artifact_digest
            normalized[identity] = ClaimTypeMigrationDispositionV3(
                identity=current.identity,
                disposition=disposition,
                claim_retirement_reason=entry.claim_retirement_reason,
                claim_effective_until=entry.claim_effective_until,
            )
            remaining.remove(identity)
            progressed = True
            break
        if not progressed:
            raise ClaimTypeMigrationIncomplete(
                f"{ClaimTypeMigrationIncomplete.code}: dependent closure contains a cycle"
            )
    return (
        candidate_tree,
        tuple(
            normalized[identity]
            for identity in sorted(normalized, key=lambda item: item.encode("utf-8"))
        ),
        _invalidation_warnings(dispositions),
    )


def _service_migrate_claim_type_v1(
    instance: PlaybillInstance,
    *,
    request: ClaimTypeMigrationRequestV1,
    actor: AuthenticatedActor,
) -> ClaimTypeMigrationResultV1:
    """Build and submit one exact ClaimType + dependent-Claim migration candidate."""

    current = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(current)
    tree = instance.tree_at(current.git_oid)
    type_path, _predecessor, successor = _successor_claim_type(tree, request.successor)
    lint = lint_claim_type_input(instance, request.successor, coordinate=current)

    inventory = _closure_inventory(tree, root=successor.identity)
    if any(item.artifact_kind != "claim" for item in inventory):
        kinds = tuple(sorted({item.artifact_kind for item in inventory}))
        raise ClaimTypeMigrationDependentSetMismatch(
            f"{ClaimTypeMigrationDependentSetMismatch.code}: v1 cannot disposition complete "
            f"closure families {kinds!r}; use playbill-claim-type-migration-request-v2"
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
        if claim.lifecycle.state == "live" and disposition.disposition == "retire":
            raise ClaimTypeMigrationDependentInvalid(
                "playbill.claim.retirement_reason_required: use "
                "playbill-claim-type-migration-request-v3"
            )
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
        warnings=_invalidation_warnings(request.dependents),
        lint=lint if lint.warnings else None,
    )


def _service_migrate_claim_type_v2(
    instance: PlaybillInstance,
    *,
    request: ClaimTypeMigrationRequestV2,
    actor: AuthenticatedActor,
) -> ClaimTypeMigrationPreflightV1 | ClaimTypeMigrationResultV2:
    current = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(current)
    tree = instance.tree_at(current.git_oid)
    type_path, _predecessor, successor = _successor_claim_type(tree, request.successor)
    lint = lint_claim_type_input(instance, request.successor, coordinate=current)
    inventory = _closure_inventory(tree, root=successor.identity)
    dispositions = request.dependents
    if request.mode == "preflight" and not dispositions:
        dispositions = tuple(
            ClaimTypeDependentDispositionV2(identity=item.identity, disposition="successor")
            for item in inventory
        )
    candidate_tree, normalized = _build_v2_candidate(
        tree=tree,
        type_path=type_path,
        successor=successor,
        inventory=inventory,
        dispositions=dispositions,
    )
    timestamp = _current_generation_timestamp(instance)
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
    )
    if evaluation.candidate is None:
        diagnostics = tuple(item.code for item in evaluation.diagnostics)
        raise ClaimTypeMigrationIncomplete(
            f"{ClaimTypeMigrationIncomplete.code}: candidate refused before admission; "
            f"diagnostics={diagnostics!r}"
        )
    if request.mode == "preflight":
        return ClaimTypeMigrationPreflightV1(
            coordinate=PlaybillAcceptedCoordinate.from_internal(current),
            successor_artifact_digest=claim_type_digest(successor).tagged,
            dependents=inventory,
            warnings=_invalidation_warnings(request.dependents),
            lint=lint if lint.warnings else None,
        )

    operation_digest = _v2_operation_digest(
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
        timestamp=timestamp,
    )
    return ClaimTypeMigrationResultV2(
        operation_digest=operation_digest,
        dependents=normalized,
        proposal=PlaybillProposalInspection(
            proposal=proposal,
            accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
                instance.accepted_coordinate()
            ),
        ),
        warnings=_invalidation_warnings(request.dependents),
        lint=lint if lint.warnings else None,
    )


def _service_migrate_claim_type_v3(
    instance: PlaybillInstance,
    *,
    request: ClaimTypeMigrationRequestV3,
    actor: AuthenticatedActor,
) -> ClaimTypeMigrationPreflightV1 | ClaimTypeMigrationResultV3:
    current = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(current)
    tree = instance.tree_at(current.git_oid)
    type_path, _predecessor, successor = _successor_claim_type(tree, request.successor)
    lint = lint_claim_type_input(instance, request.successor, coordinate=current)
    inventory = _closure_inventory(tree, root=successor.identity)
    dispositions = request.dependents
    if request.mode == "preflight" and not dispositions:
        dispositions = tuple(
            ClaimTypeDependentDispositionV3(identity=item.identity, disposition="successor")
            for item in inventory
        )
    candidate_tree, normalized, warnings = _build_v3_candidate(
        tree=tree,
        type_path=type_path,
        successor=successor,
        inventory=inventory,
        dispositions=dispositions,
    )
    timestamp = _current_generation_timestamp(instance)
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
    )
    if evaluation.candidate is None:
        diagnostics = tuple(item.code for item in evaluation.diagnostics)
        raise ClaimTypeMigrationIncomplete(
            f"{ClaimTypeMigrationIncomplete.code}: candidate refused before admission; "
            f"diagnostics={diagnostics!r}"
        )
    if request.mode == "preflight":
        return ClaimTypeMigrationPreflightV1(
            coordinate=PlaybillAcceptedCoordinate.from_internal(current),
            successor_artifact_digest=claim_type_digest(successor).tagged,
            dependents=inventory,
            warnings=warnings,
            lint=lint if lint.warnings else None,
        )

    operation_digest = _v3_operation_digest(
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
        timestamp=timestamp,
    )
    return ClaimTypeMigrationResultV3(
        operation_digest=operation_digest,
        dependents=normalized,
        proposal=PlaybillProposalInspection(
            proposal=proposal,
            accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
                instance.accepted_coordinate()
            ),
        ),
        warnings=warnings,
        lint=lint if lint.warnings else None,
    )


def service_migrate_claim_type(
    instance: PlaybillInstance,
    *,
    request: ClaimTypeMigrationRequest,
    actor: AuthenticatedActor,
) -> ClaimTypeMigrationResponse:
    """Preflight or submit one complete ClaimType migration changeset."""

    if isinstance(request, ClaimTypeMigrationRequestV3):
        return _service_migrate_claim_type_v3(instance, request=request, actor=actor)
    if isinstance(request, ClaimTypeMigrationRequestV2):
        return _service_migrate_claim_type_v2(instance, request=request, actor=actor)
    return _service_migrate_claim_type_v1(instance, request=request, actor=actor)


__all__ = [
    "CLAIM_TYPE_MIGRATION_DOMAIN",
    "ClaimTypeDependentDispositionV1",
    "ClaimTypeDependentDispositionV2",
    "ClaimTypeDependentDispositionV3",
    "ClaimTypeMigrationDispositionV1",
    "ClaimTypeMigrationDispositionV2",
    "ClaimTypeMigrationDispositionV3",
    "ClaimTypeMigrationDependentInvalid",
    "ClaimTypeMigrationDependentSetMismatch",
    "ClaimTypeMigrationError",
    "ClaimTypeMigrationIncomplete",
    "ClaimTypeMigrationInventoryItemV1",
    "ClaimTypeMigrationPreflightV1",
    "ClaimTypeMigrationRequest",
    "ClaimTypeMigrationRequestV1",
    "ClaimTypeMigrationRequestV2",
    "ClaimTypeMigrationRequestV3",
    "ClaimTypeMigrationResponse",
    "ClaimTypeMigrationResultV1",
    "ClaimTypeMigrationResultV2",
    "ClaimTypeMigrationResultV3",
    "service_migrate_claim_type",
]
