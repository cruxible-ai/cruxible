"""Authoring-only ClaimType profiles that expand before semantic law."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.claim_type_structure import ClaimTypeStructure
from cruxible_core.playbill.claim_types import ClaimType, claim_type_digest, render_claim_type
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.policies import (
    ActorRequirementV1,
    AttestationRequirement,
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
    EvidenceRequirementV1,
    TransitionRequirementV1,
)

ClaimTypeProfileId = Literal[
    "ordinary-project-fact-v1",
    "governed-single-valued-status-transition-v1",
    "append-only-source-observation-v1",
    "policy-owner-normative-claim-v1",
    "replay-verifiable-derivation-v1",
    "source-backed-scientific-result-v1",
]


class AuthoringProfileError(PlaybillFormatError):
    """A compact authoring profile cannot be expanded unambiguously."""


class _StrictProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimTypeProfileDefinitionV1(_StrictProfileModel):
    tag: Literal["playbill-claim-type-profile-definition-v1"] = (
        "playbill-claim-type-profile-definition-v1"
    )
    profile_id: ClaimTypeProfileId
    required_parameters: tuple[str, ...]
    optional_parameters: tuple[str, ...] = ()
    allowed_overrides: tuple[Literal["conflict_result", "require_current"], ...] = (
        "conflict_result",
        "require_current",
    )
    profile_digest: str

    @field_validator("required_parameters", "optional_parameters", "allowed_overrides")
    @classmethod
    def _sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("profile definition sets must be sorted and unique")
        return value

    @field_validator("profile_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _self_digest(self) -> "ClaimTypeProfileDefinitionV1":
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        payload.pop("profile_digest")
        expected = typed_digest(
            Sha256Value,
            "playbill-claim-type-profile-definition-v1",
            payload,
        ).tagged
        if expected != self.profile_digest:
            raise ValueError("authoring profile digest does not reproduce")
        if set(self.required_parameters).intersection(self.optional_parameters):
            raise ValueError("profile parameter cannot be both required and optional")
        return self


def _profile(
    profile_id: ClaimTypeProfileId,
    *,
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
) -> ClaimTypeProfileDefinitionV1:
    values: dict[str, object] = {
        "profile_id": profile_id,
        "required_parameters": list(required),
        "optional_parameters": list(optional),
        "allowed_overrides": ["conflict_result", "require_current"],
    }
    digest = typed_digest(
        Sha256Value,
        "playbill-claim-type-profile-definition-v1",
        values,
    ).tagged
    return ClaimTypeProfileDefinitionV1(
        profile_id=profile_id,
        required_parameters=required,
        optional_parameters=optional,
        allowed_overrides=("conflict_result", "require_current"),
        profile_digest=digest,
    )


CLAIM_TYPE_AUTHORING_PROFILES: tuple[ClaimTypeProfileDefinitionV1, ...] = (
    _profile(
        "append-only-source-observation-v1",
        required=("capture_contract_digest", "evidence_kind"),
        optional=("attestation_requirement",),
    ),
    _profile(
        "governed-single-valued-status-transition-v1",
        required=(
            "approval_query_digest",
            "from_values",
            "reviewer_role",
            "to_value",
        ),
    ),
    _profile("ordinary-project-fact-v1"),
    _profile("policy-owner-normative-claim-v1"),
    _profile(
        "replay-verifiable-derivation-v1",
        required=("capture_contract_digest", "evidence_kind", "reducer_digest"),
        optional=("attestation_requirement",),
    ),
    _profile(
        "source-backed-scientific-result-v1",
        required=("capture_contract_digest", "evidence_kind"),
        optional=("attestation_requirement",),
    ),
)


class AuthorityProfileParametersV1(_StrictProfileModel):
    propose_roles: tuple[str, ...]
    approve_roles: tuple[str, ...]

    def authority(self) -> ArtifactAuthority:
        return ArtifactAuthority(
            propose_roles=self.propose_roles,
            approve_roles=self.approve_roles,
        )


class ClaimTypeProfileInputV1(_StrictProfileModel):
    tag: Literal["playbill-claim-type-profile-input-v1"] = "playbill-claim-type-profile-input-v1"
    profile_id: str
    profile_digest: str
    authoring_source_digest: str
    compiler_digest: str
    structure: ClaimTypeStructure
    authority_parameters: AuthorityProfileParametersV1 | None
    pins: tuple[ArtifactPin, ...] = ()
    parameters: dict[str, object] = Field(default_factory=dict)
    overrides: dict[str, object] = Field(default_factory=dict)

    @field_validator("profile_digest", "authoring_source_digest", "compiler_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("parameters", "overrides")
    @classmethod
    def _canonical_object(cls, value: dict[str, object]) -> dict[str, object]:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):  # pragma: no cover - field type proves this
            raise ValueError("profile values must be canonical objects")
        return {str(key): item for key, item in normalized.items()}


class ClaimTypeExpansionEvidenceV1(_StrictProfileModel):
    tag: Literal["playbill-claim-type-expansion-evidence-v1"] = (
        "playbill-claim-type-expansion-evidence-v1"
    )
    profile_id: str
    profile_digest: str
    authoring_source_digest: str
    compiler_digest: str
    overrides: dict[str, object]
    overrides_digest: str
    expanded_output_digest: str
    expanded_artifact_digest: str

    @field_validator(
        "profile_digest",
        "authoring_source_digest",
        "compiler_digest",
        "overrides_digest",
        "expanded_output_digest",
        "expanded_artifact_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("overrides")
    @classmethod
    def _overrides(cls, value: dict[str, object]) -> dict[str, object]:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):  # pragma: no cover - field type proves this
            raise ValueError("profile overrides must be a canonical object")
        return {str(key): item for key, item in normalized.items()}

    @model_validator(mode="after")
    def _override_binding(self) -> "ClaimTypeExpansionEvidenceV1":
        expected = typed_digest(
            Sha256Value,
            "playbill-claim-type-profile-overrides-v1",
            self.overrides,
        ).tagged
        if self.overrides_digest != expected:
            raise ValueError("profile overrides digest does not reproduce")
        return self


class ClaimTypeExpansionResultV1(_StrictProfileModel):
    tag: Literal["playbill-claim-type-expansion-result-v1"] = (
        "playbill-claim-type-expansion-result-v1"
    )
    claim_type: ClaimType
    evidence: ClaimTypeExpansionEvidenceV1


def _definitions() -> dict[str, ClaimTypeProfileDefinitionV1]:
    return {item.profile_id: item for item in CLAIM_TYPE_AUTHORING_PROFILES}


def _require_digest(parameters: dict[str, object], key: str) -> str:
    value = parameters[key]
    if not isinstance(value, str):
        raise AuthoringProfileError(f"profile parameter {key!r} must be a digest")
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise AuthoringProfileError(f"profile parameter {key!r} must be a digest") from exc
    return value


def _attestation(parameters: dict[str, object]) -> AttestationRequirement:
    value = parameters.get("attestation_requirement", "none")
    if value not in {"none", "verified_provider", "verified_principal", "any_verified"}:
        raise AuthoringProfileError("attestation_requirement is unknown")
    return cast(AttestationRequirement, value)


def _profile_policies(
    profile_id: str,
    structure: ClaimTypeStructure,
    parameters: dict[str, object],
    overrides: dict[str, object],
) -> tuple[ClaimEvidenceAdmissionPolicyV1, ClaimAdmissionPolicyV1, ClaimResolutionPolicyV1]:
    evidence = ClaimEvidenceAdmissionPolicyV1()
    admission = ClaimAdmissionPolicyV1()
    if profile_id == "governed-single-valued-status-transition-v1":
        if structure.cardinality != "one":
            raise AuthoringProfileError("governed status profile requires cardinality='one'")
        reviewer_role = parameters["reviewer_role"]
        from_values = parameters["from_values"]
        to_value = parameters["to_value"]
        if not isinstance(reviewer_role, str) or not isinstance(from_values, list):
            raise AuthoringProfileError("status transition parameters are ambiguous")
        admission = ClaimAdmissionPolicyV1(
            transition_requirements=(
                TransitionRequirementV1(
                    requirement_id="governed-transition",
                    when_predicate=structure.predicate,
                    from_values=tuple(from_values),
                    to_value=to_value,
                    require=("accepted-evidence", "reviewer-role"),
                ),
            ),
            actor_requirements=(
                ActorRequirementV1(
                    requirement_id="reviewer-role",
                    signer_roles=(reviewer_role,),
                    signer_distinct_from_lineage_creation_actor=True,
                ),
            ),
            evidence_requirements=(
                EvidenceRequirementV1(
                    requirement_id="accepted-evidence",
                    query_definition_digest=_require_digest(parameters, "approval_query_digest"),
                    min_count=1,
                ),
            ),
        )
    elif profile_id in {
        "append-only-source-observation-v1",
        "source-backed-scientific-result-v1",
    }:
        if structure.cardinality != "many":
            raise AuthoringProfileError("observation profiles require cardinality='many'")
        evidence = ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="source-observation",
                    claim_roles=("observation",),
                    capture_contract_digests=(
                        _require_digest(parameters, "capture_contract_digest"),
                    ),
                    evidence_kinds=(str(parameters["evidence_kind"]),),
                    admission="direct",
                    subject_binding="contract_source_mapping",
                    attestation_requirement=_attestation(parameters),
                ),
            )
        )
    elif profile_id == "replay-verifiable-derivation-v1":
        evidence = ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="replay-verifiable-derivation",
                    claim_roles=("derivation",),
                    capture_contract_digests=(
                        _require_digest(parameters, "capture_contract_digest"),
                    ),
                    evidence_kinds=(str(parameters["evidence_kind"]),),
                    admission="derivational",
                    subject_binding="contract_source_mapping",
                    allowed_reducer_digests=(_require_digest(parameters, "reducer_digest"),),
                    attestation_requirement=_attestation(parameters),
                ),
            )
        )
    conflict_result = overrides.get("conflict_result", "unresolved")
    require_current = overrides.get("require_current", True)
    if conflict_result not in {"unresolved", "refuse"} or not isinstance(require_current, bool):
        raise AuthoringProfileError("profile override value is invalid")
    resolution = ClaimResolutionPolicyV1(
        cardinality=structure.cardinality,
        eligible_verdicts=("supported",),
        require_current=require_current,
        selector="all" if structure.cardinality == "many" else "only_contender",
        conflict_result=cast(Literal["unresolved", "refuse"], conflict_result),
    )
    return evidence, admission, resolution


def expand_claim_type_profile(request: ClaimTypeProfileInputV1) -> ClaimTypeExpansionResultV1:
    """Expand compact authoring input into the only bytes candidate law will see."""

    definition = _definitions().get(request.profile_id)
    if definition is None:
        raise AuthoringProfileError(f"unknown ClaimType authoring profile: {request.profile_id}")
    if request.profile_digest != definition.profile_digest:
        raise AuthoringProfileError("authoring profile digest is stale or forged")
    if request.authority_parameters is None:
        raise AuthoringProfileError("authoring profile requires explicit authority parameters")
    supplied = set(request.parameters)
    required = set(definition.required_parameters)
    allowed = required | set(definition.optional_parameters)
    if required - supplied:
        raise AuthoringProfileError("authoring profile is missing required parameters")
    if supplied - allowed:
        raise AuthoringProfileError("authoring profile parameter is outside its closed schema")
    if set(request.overrides) - set(definition.allowed_overrides):
        raise AuthoringProfileError("authoring profile override is outside its closed schema")
    evidence, admission, resolution = _profile_policies(
        request.profile_id,
        request.structure,
        request.parameters,
        request.overrides,
    )
    structure = request.structure
    claim_type = ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=structure.predicate),
        predicate=structure.predicate,
        allowed_subject_kinds=structure.allowed_subject_kinds,
        object_kind=structure.object_kind,
        literal_schema=structure.literal_schema,
        allowed_object_subject_kinds=structure.allowed_object_subject_kinds,
        cardinality=structure.cardinality,
        permitted_roles=structure.permitted_roles,
        referent_sensitivity=structure.referent_sensitivity,
        evidence_admission_policy=evidence,
        admission_policy=admission,
        resolution_policy=resolution,
        authority=request.authority_parameters.authority(),
        pins=request.pins,
        lifecycle=ArtifactLifecycle(),
    )
    rendered = canonical_bytes(claim_type.model_dump(mode="json")) + b"\n"
    return ClaimTypeExpansionResultV1(
        claim_type=claim_type,
        evidence=ClaimTypeExpansionEvidenceV1(
            profile_id=definition.profile_id,
            profile_digest=definition.profile_digest,
            authoring_source_digest=request.authoring_source_digest,
            compiler_digest=request.compiler_digest,
            overrides=request.overrides,
            overrides_digest=typed_digest(
                Sha256Value,
                "playbill-claim-type-profile-overrides-v1",
                request.overrides,
            ).tagged,
            expanded_output_digest=typed_digest(
                Sha256Value,
                "playbill-claim-type-profile-output-v1",
                {"canonical_bytes_hex": rendered.hex()},
            ).tagged,
            expanded_artifact_digest=claim_type_digest(claim_type).tagged,
        ),
    )


def verify_claim_type_expansion_evidence(
    evidence: ClaimTypeExpansionEvidenceV1,
    *,
    claim_type: ClaimType,
    compiler_digest: str,
) -> None:
    """Verify profile identity and every digest against the exact expanded bytes."""

    definition = _definitions().get(evidence.profile_id)
    if definition is None or definition.profile_digest != evidence.profile_digest:
        raise AuthoringProfileError("ClaimType expansion names an unknown or stale profile")
    if evidence.compiler_digest != compiler_digest:
        raise AuthoringProfileError("ClaimType expansion names a different compiler")
    rendered = render_claim_type(claim_type)
    expected_output = typed_digest(
        Sha256Value,
        "playbill-claim-type-profile-output-v1",
        {"canonical_bytes_hex": rendered.hex()},
    ).tagged
    if evidence.expanded_output_digest != expected_output:
        raise AuthoringProfileError("ClaimType expansion output digest does not reproduce")
    if evidence.expanded_artifact_digest != claim_type_digest(claim_type).tagged:
        raise AuthoringProfileError("ClaimType expansion artifact digest does not reproduce")


__all__ = [
    "AuthorityProfileParametersV1",
    "AuthoringProfileError",
    "CLAIM_TYPE_AUTHORING_PROFILES",
    "ClaimTypeProfileId",
    "ClaimTypeExpansionEvidenceV1",
    "ClaimTypeExpansionResultV1",
    "ClaimTypeProfileDefinitionV1",
    "ClaimTypeProfileInputV1",
    "expand_claim_type_profile",
    "verify_claim_type_expansion_evidence",
]
