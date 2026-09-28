"""Decision-only ClaimType input and deterministic proposal-time contract lint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactRef,
    parse_artifact_identity,
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
    ClaimFreshnessDurationV1,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimEvidenceAdmissionPolicyV2,
    ClaimEvidenceAdmissionPolicyV3,
)
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.compiler import IDENTITY_REFS_COMPILER
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillProposalInspection


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
    unsure_hold_for: ClaimFreshnessDurationV1 | None = Field(
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
        "playbill.claim_type.attestation_threshold_disabled",
    ]
    field_path: str
    source_id: str | None = None
    # Evidence-policy warnings name the contract they concern; others name none.
    contract_identity: str | None = None
    contract_digest: str | None = None
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


def claim_type_input_template() -> ClaimTypeInputV1:
    """Return the complete literal ClaimType input shown by the CLI template surface."""

    source_id = "repo.replace-me"
    contract = foreign_source_capture_contract(source_id)
    return ClaimTypeInputV1(
        predicate="project.work_item.status",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy={
            "rules": [
                {
                    "rule_id": f"source-{source_id}",
                    "claim_roles": ["normative", "observation"],
                    "capture_contracts": [contract.identity.qualified],
                    "evidence_kinds": ["self_asserted"],
                    "admission": "direct",
                    "subject_binding": "exact_claim_subject",
                }
            ]
        },
        admission_policy={
            "corroboration_requirements": [],
            "freeze_requirements": [],
        },
        resolution_policy={
            "cardinality": "one",
            "eligible_verdicts": ["supported"],
            "required_basis_kinds": [],
            "require_current": True,
            "selector": "only_contender",
            "conflict_result": "unresolved",
        },
        anticipated_source_ids=(source_id,),
    )


def identity_rules_supported(compiler: CompilerCoordinate) -> bool:
    """Whether this compiler accepts ClaimType v6 identity evidence rules."""

    return compiler == IDENTITY_REFS_COMPILER


def _contract_identities(tree: Mapping[str, bytes], source_ids: tuple[str, ...]) -> dict[str, str]:
    """Accepted (and anticipated foreign-source) contract digests to their identities."""

    identities: dict[str, str] = {}
    for source_id in source_ids:
        contract = foreign_source_capture_contract(source_id)
        identities[capture_contract_digest(contract).tagged] = contract.identity.qualified
    for path in tree:
        if path.startswith("capture-contracts/") and path.endswith(".json"):
            contract = parse_capture_contract(tree[path], path=path)
            identities[capture_contract_digest(contract).tagged] = contract.identity.qualified
    return identities


def _contract_ref(value: object) -> ArtifactRef:
    if isinstance(value, dict):
        return ArtifactRef.model_validate(value)
    if not isinstance(value, str):
        raise ClaimTypeInputReferenceError("a capture_contracts entry must be a contract name")
    identity = (
        parse_artifact_identity(value)
        if value.partition(":")[0] == "CaptureContract" and ":" in value
        else ArtifactIdentity(kind="CaptureContract", name=value)
    )
    return ArtifactRef(role=CAPTURE_CONTRACT_REF_ROLE, target=identity)


def _identity_evidence_policy(
    raw: Mapping[str, object], *, identities: Mapping[str, str]
) -> dict[str, object]:
    """Lower authored evidence rules to identity rules.

    A rule names contracts by `capture_contracts` (a name, `CaptureContract:<name>`
    or a reference). An exact `capture_contract_digests` list is still accepted as
    input and lowered to the identities those accepted versions belong to.
    """

    rules: list[object] = []
    raw_rules = raw.get("rules", [])
    for raw_rule in raw_rules if isinstance(raw_rules, list | tuple) else ():
        if not isinstance(raw_rule, dict):
            rules.append(raw_rule)
            continue
        rule = dict(raw_rule)
        refs = [_contract_ref(item) for item in rule.pop("capture_contracts", []) or []]
        for digest in rule.pop("capture_contract_digests", []) or []:
            identity = identities.get(digest) if isinstance(digest, str) else None
            if identity is None:
                raise ClaimTypeInputReferenceError(
                    f"capture contract digest {digest!r} is not an accepted CaptureContract; "
                    "name the contract in capture_contracts instead"
                )
            refs.append(_contract_ref(identity))
        unique = {item.target.qualified: item for item in refs}
        rule["capture_contracts"] = [
            unique[key].model_dump(mode="json")
            for key in sorted(unique, key=lambda item: item.encode("utf-8"))
        ]
        rule.pop("tag", None)
        rules.append(rule)
    return {**{k: v for k, v in raw.items() if k not in {"rules", "tag"}}, "rules": rules}


class ClaimTypeInputReferenceError(PlaybillFormatError):
    """An authored evidence rule names a contract that cannot be referenced."""

    error_code = "playbill.claim_type.input_invalid"


def lower_claim_type_input(
    value: ClaimTypeInputV1,
    *,
    tree: Mapping[str, bytes],
    identity_rules: bool = False,
) -> ClaimType:
    path = claim_type_path(value.predicate)
    predecessor = None
    if path in tree:
        predecessor = parse_claim_type(tree[path], path=path)
    payload = value.model_dump(mode="json")
    payload.pop("anticipated_source_ids", None)
    payload["artifact_format"] = (
        "playbill-claim-type-v6" if identity_rules else "playbill-claim-type-v5"
    )
    try:
        if identity_rules:
            payload["evidence_admission_policy"] = ClaimEvidenceAdmissionPolicyV3.model_validate(
                _identity_evidence_policy(
                    value.evidence_admission_policy,
                    identities=_contract_identities(tree, value.anticipated_source_ids),
                )
            ).model_dump(mode="json")
        else:
            payload["evidence_admission_policy"] = ClaimEvidenceAdmissionPolicyV2.model_validate(
                value.evidence_admission_policy
            ).model_dump(mode="json")
    except ValidationError as exc:
        raise ClaimTypeInputValidationError(exc) from exc
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
    accepted_contracts: dict[str, str] = {}
    source_ids = set(anticipated_source_ids)
    if isinstance(value, ClaimTypeInputV1):
        source_ids.update(value.anticipated_source_ids)
    # Flow-A binding derives this exact deterministic contract and carries it in
    # the governed Claim candidate. The dormant direct-self-asserted constant has
    # no production producer or acceptor, so it is intentionally not resolvable.
    resolvable_contracts: dict[str, str] = {}
    for source_id in sorted(source_ids, key=lambda item: item.encode("utf-8")):
        contract = foreign_source_capture_contract(source_id)
        resolvable_contracts[capture_contract_digest(contract).tagged] = contract.identity.qualified
    with instance.bind_accepted_projection(coordinate) as projection:
        accepted_contracts.update(
            (row.artifact_digest, row.identity)
            for row in projection.typed.envelopes(kind="capture-contract")
        )
    resolvable_contracts.update(accepted_contracts)

    policy = (
        value.evidence_admission_policy
        if isinstance(value, ClaimTypeInputV1)
        else value.evidence_admission_policy.model_dump(mode="json")
    )
    raw_rules = policy.get("rules", [])
    rules = raw_rules if isinstance(raw_rules, list | tuple) else []
    warnings: list[ClaimTypeLintWarningV1] = []
    admitted: set[str] = set()
    admitted_identities: set[str] = set()
    resolvable_identities = set(resolvable_contracts.values())
    for index, raw_rule in enumerate(rules):
        if not isinstance(raw_rule, dict):
            continue
        named: list[str] = []
        for item in raw_rule.get("capture_contracts", []) or []:
            try:
                named.append(_contract_ref(item).target.qualified)
            except (PlaybillFormatError, ValueError):
                continue
        admitted_identities.update(named)
        for identity in named:
            if identity not in resolvable_identities:
                warnings.append(
                    ClaimTypeLintWarningV1(
                        code="playbill.claim_type.evidence_policy_admits_no_accepted_contract",
                        field_path=f"$.evidence_admission_policy.rules[{index}].capture_contracts",
                        contract_identity=identity,
                        replacement_rule_fragment={
                            "capture_contracts": sorted(
                                resolvable_identities, key=lambda item: item.encode("utf-8")
                            )
                        },
                    )
                )
        raw_digests = raw_rule.get("capture_contract_digests", [])
        digests = tuple(item for item in raw_digests if isinstance(item, str))
        admitted.update(digests)
        if digests and not set(digests).intersection(resolvable_contracts):
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
                            "capture_contract_digests": sorted(resolvable_contracts)
                        },
                    )
                )
    if (
        not admitted.intersection(resolvable_contracts)
        and not admitted_identities.intersection(resolvable_identities)
        and accepted_contracts
        and not warnings
    ):
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
    for source_id in sorted(source_ids, key=lambda item: item.encode("utf-8")):
        contract = foreign_source_capture_contract(source_id)
        contract_digest = capture_contract_digest(contract).tagged
        if contract_digest in admitted or contract.identity.qualified in admitted_identities:
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
    consequence = (
        value.attestation_consequence_policy
        if isinstance(value, ClaimTypeInputV1)
        else value.attestation_consequence_policy
    )
    for index, rule in enumerate(() if consequence is None else consequence.rules):
        # A threshold of zero escalates nothing: `next` treats the rule as disabled.
        if rule.minimum_independent_control_components == 0:
            warnings.append(
                ClaimTypeLintWarningV1(
                    code="playbill.claim_type.attestation_threshold_disabled",
                    field_path=(
                        f"$.attestation_consequence_policy.rules[{index}]"
                        ".minimum_independent_control_components"
                    ),
                    replacement_rule_fragment={"minimum_independent_control_components": 1},
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
    "claim_type_input_template",
    "identity_rules_supported",
    "lint_claim_type_input",
    "lower_claim_type_input",
]
