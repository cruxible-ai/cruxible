"""Decision-only ClaimType input and deterministic proposal-time contract lint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    foreign_source_capture_contract,
    parse_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicyV1,
    ClaimEvidenceFreshnessV1,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.service.documents import PlaybillProposalInspection


class _StrictClaimTypeInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimTypeInputValidationError(PlaybillFormatError):
    """A decision-only ClaimType input violates its final artifact contract."""

    error_code = "playbill.claim_type.input_invalid"

    def __init__(self, validation_error: ValidationError) -> None:
        details = []
        for error in validation_error.errors(include_url=False):
            path = "$" + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"]
            )
            details.append(f"{path}: {error['msg']}")
        super().__init__(f"{self.error_code}: {'; '.join(details)}")


class ClaimTypeInputV1(_StrictClaimTypeInputModel):
    predicate: str
    allowed_subject_kinds: tuple[str, ...]
    object_kind: Literal["literal", "subject", "exact_content"]
    literal_schema: dict[str, object] | None = None
    allowed_object_subject_kinds: tuple[str, ...] = ()
    cardinality: Literal["one", "many"]
    permitted_roles: tuple[
        Literal["normative", "observation", "environment_binding", "derivation"], ...
    ]
    referent_sensitivity: Literal["identity", "shell"] = "identity"
    evidence_admission_policy: dict[str, object]
    admission_policy: dict[str, object]
    resolution_policy: dict[str, object]
    pins: tuple[dict[str, object], ...] = ()
    evidence_freshness: ClaimEvidenceFreshnessV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    attestation_consequence_policy: ClaimAttestationConsequencePolicyV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    anticipated_source_ids: tuple[str, ...] = ()

    @field_validator("anticipated_source_ids")
    @classmethod
    def _anticipated_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("anticipated source IDs must be UTF-8 byte-sorted and unique")
        for source_id in value:
            foreign_source_capture_contract(source_id)
        return value


class ClaimTypeLintWarningV1(_StrictClaimTypeInputModel):
    code: Literal[
        "playbill.claim_type.evidence_policy_admits_no_accepted_contract",
        "playbill.claim_type.anticipated_source_contract_omitted",
    ]
    field_path: str
    source_id: str | None = None
    contract_identity: str
    contract_digest: str
    replacement_rule_fragment: dict[str, object]


class ClaimTypeProposalLintV1(_StrictClaimTypeInputModel):
    tag: Literal["playbill-claim-type-proposal-lint-v1"] = "playbill-claim-type-proposal-lint-v1"
    warnings: tuple[ClaimTypeLintWarningV1, ...]


class ClaimTypeInputProposalResultV1(_StrictClaimTypeInputModel):
    tag: Literal["playbill-claim-type-input-proposal-result-v1"] = (
        "playbill-claim-type-input-proposal-result-v1"
    )
    proposal: PlaybillProposalInspection
    lint: ClaimTypeProposalLintV1


def lower_claim_type_input(
    value: ClaimTypeInputV1,
    *,
    tree: dict[str, bytes],
) -> ClaimType:
    path = claim_type_path(value.predicate)
    predecessor = None
    if path in tree:
        predecessor = parse_claim_type(tree[path], path=path)
    payload = value.model_dump(mode="json")
    payload.pop("anticipated_source_ids", None)
    if value.attestation_consequence_policy is not None:
        payload["artifact_format"] = "playbill-claim-type-v4"
    elif value.evidence_freshness is not None:
        payload["artifact_format"] = "playbill-claim-type-v3"
    else:
        payload["artifact_format"] = "playbill-claim-type-v1"
    payload["identity"] = ArtifactIdentity(kind="ClaimType", name=value.predicate).model_dump(
        mode="json"
    )
    payload["lifecycle"] = ArtifactLifecycle(
        predecessor_digest=(None if predecessor is None else claim_type_digest(predecessor).tagged)
    ).model_dump(mode="json")
    try:
        return ClaimType.model_validate(payload)
    except ValidationError as exc:
        raise ClaimTypeInputValidationError(exc) from exc


def lint_claim_type_input(
    instance: PlaybillInstance,
    value: ClaimTypeInputV1 | ClaimType,
    *,
    coordinate: AcceptedProjectionCoordinate,
    anticipated_source_ids: tuple[str, ...] = (),
) -> ClaimTypeProposalLintV1:
    tree = instance.tree_at(coordinate.git_oid)
    accepted_contracts: dict[str, str] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("capture-contracts/"):
            continue
        contract = parse_capture_contract(tree[path], path=path)
        accepted_contracts[capture_contract_digest(contract).tagged] = contract.identity.qualified

    policy = (
        value.evidence_admission_policy
        if isinstance(value, ClaimTypeInputV1)
        else value.evidence_admission_policy.model_dump(mode="json")
    )
    raw_rules = policy.get("rules", [])
    rules = raw_rules if isinstance(raw_rules, list | tuple) else []
    warnings: list[ClaimTypeLintWarningV1] = []
    admitted: set[str] = set()
    for index, raw_rule in enumerate(rules):
        if not isinstance(raw_rule, dict):
            continue
        raw_digests = raw_rule.get("capture_contract_digests", [])
        digests = tuple(item for item in raw_digests if isinstance(item, str))
        admitted.update(digests)
        if digests and not set(digests).intersection(accepted_contracts):
            for digest in digests:
                warnings.append(
                    ClaimTypeLintWarningV1(
                        code="playbill.claim_type.evidence_policy_admits_no_accepted_contract",
                        field_path=(
                            f"$.evidence_admission_policy.rules[{index}].capture_contract_digests"
                        ),
                        contract_identity="unresolved",
                        contract_digest=digest,
                        replacement_rule_fragment={
                            "capture_contract_digests": sorted(accepted_contracts)
                        },
                    )
                )
    if not admitted.intersection(accepted_contracts) and accepted_contracts and not warnings:
        contract_digest = sorted(accepted_contracts)[0]
        warnings.append(
            ClaimTypeLintWarningV1(
                code="playbill.claim_type.evidence_policy_admits_no_accepted_contract",
                field_path="$.evidence_admission_policy.rules",
                contract_identity=accepted_contracts[contract_digest],
                contract_digest=contract_digest,
                replacement_rule_fragment={"capture_contract_digests": [contract_digest]},
            )
        )
    source_ids = set(anticipated_source_ids)
    if isinstance(value, ClaimTypeInputV1):
        source_ids.update(value.anticipated_source_ids)
    for source_id in sorted(source_ids, key=lambda item: item.encode("utf-8")):
        contract = foreign_source_capture_contract(source_id)
        contract_digest = capture_contract_digest(contract).tagged
        if contract_digest in admitted:
            continue
        warnings.append(
            ClaimTypeLintWarningV1(
                code="playbill.claim_type.anticipated_source_contract_omitted",
                field_path="$.evidence_admission_policy.rules",
                source_id=source_id,
                contract_identity=contract.identity.qualified,
                contract_digest=contract_digest,
                replacement_rule_fragment={"capture_contract_digests": [contract_digest]},
            )
        )
    warnings.sort(key=lambda item: canonical_bytes(item.model_dump(mode="json")))
    return ClaimTypeProposalLintV1(warnings=tuple(warnings))


__all__ = [
    "ClaimTypeInputProposalResultV1",
    "ClaimTypeInputValidationError",
    "ClaimTypeInputV1",
    "ClaimTypeLintWarningV1",
    "ClaimTypeProposalLintV1",
    "lint_claim_type_input",
    "lower_claim_type_input",
]
