"""Final policy-bearing ClaimType v1 artifact and acceptance law."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Callable, Final, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_for_codec,
    artifact_path_matches,
    canonical_bytes,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.claim_type_structure import ClaimRole, ClaimTypeStructure
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier, governance_identifier
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV2,
    ClaimResolutionPolicy,
)

_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})+$")


class ClaimTypeFormatError(PlaybillFormatError):
    """The ClaimType envelope or its canonical path is invalid."""


class ClaimTypeFreshnessHorizonInvalid(ClaimTypeFormatError):
    """A ClaimType v3 evidence-freshness horizon is malformed or non-positive."""


class _StrictClaimTypeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class ClaimFreshnessDuration(_StrictClaimTypeModel):
    tag: Literal["playbill-duration-v1"] = "playbill-duration-v1"
    microseconds: int = Field(ge=0)


class ClaimEvidenceFreshness(_StrictClaimTypeModel):
    tag: Literal["playbill-claim-evidence-freshness-v1"] = "playbill-claim-evidence-freshness-v1"
    stale_after: ClaimFreshnessDuration

    @model_validator(mode="after")
    def _positive_horizon(self) -> "ClaimEvidenceFreshness":
        if self.stale_after.microseconds <= 0:
            raise ValueError("evidence freshness stale_after must be positive")
        return self


class ClaimAttestationConsequenceRule(_StrictClaimTypeModel):
    tag: Literal["playbill-claim-attestation-consequence-rule-v1"] = (
        "playbill-claim-attestation-consequence-rule-v1"
    )
    rule_id: str
    stance: Literal["unsure", "contradict"]
    minimum_independent_control_components: int = Field(ge=0)
    consequence: Literal["next_claim_attestation_threshold"] = "next_claim_attestation_threshold"
    require_current: Literal[True] = True

    @field_validator("rule_id")
    @classmethod
    def _rule_id(cls, value: str) -> str:
        return governance_identifier(value, label="attestation consequence rule_id")


class ClaimAttestationConsequencePolicy(_StrictClaimTypeModel):
    tag: Literal["playbill-claim-attestation-consequence-policy-v1"] = (
        "playbill-claim-attestation-consequence-policy-v1"
    )
    rules: tuple[ClaimAttestationConsequenceRule, ...] = Field(min_length=1)

    @field_validator("rules")
    @classmethod
    def _rules(
        cls, value: tuple[ClaimAttestationConsequenceRule, ...]
    ) -> tuple[ClaimAttestationConsequenceRule, ...]:
        rule_ids = tuple(rule.rule_id for rule in value)
        if rule_ids != tuple(sorted(set(rule_ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("attestation consequence rules must be sorted and unique by rule_id")
        return value


CURRENT_CLAIM_TYPE_FORMAT: Final = "playbill-claim-type-v7"
_CURRENT_POLICY_FORMATS: Final = frozenset(
    {"playbill-claim-type-v5", "playbill-claim-type-v6", "playbill-claim-type-v7"}
)
_IDENTITY_RULE_FORMATS: Final = frozenset({"playbill-claim-type-v6", "playbill-claim-type-v7"})
CLAIM_TYPE_FORMATS: Final = (
    "playbill-claim-type-v1",
    "playbill-claim-type-v3",
    "playbill-claim-type-v4",
    "playbill-claim-type-v5",
    "playbill-claim-type-v6",
    "playbill-claim-type-v7",
)

#: What a Claim of this type must be backed by. ``none``: the Claim's own origin
#: supports it; ``self``: the evidence rules decide (the meaning every ClaimType
#: before v7 has); ``captured``: at least one Capture under a declared contract.
EvidenceRequirement = Literal["none", "self", "captured"]
#: What a revision that changes its statement keeps. ``replace``: exactly the
#: evidence it cites; ``accumulate``: everything its predecessors cited too (the
#: meaning every ClaimType before v7 has).
RevisionEvidence = Literal["replace", "accumulate"]
V7_FIELDS: Final = (
    "description",
    "member_descriptions",
    "default_role",
    "evidence_requirement",
    "revision_evidence",
)
_DESCRIPTION_MAX: Final = 1024
_MEMBER_DESCRIPTION_MAX: Final = 256


def _canonical_text(value: str, *, label: str, maximum: int) -> str:
    """Refuse text that is not already NFC, trimmed and within bounds."""

    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must be NFC-normalized")
    if value.strip() != value:
        raise ValueError(f"{label} must not start or end with whitespace")
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{label} must be 1..{maximum} characters")
    return value


def canonical_description_text(value: str) -> str:
    """Normalize authored text to the one spelling a ClaimType v7 accepts."""

    return unicodedata.normalize("NFC", value).strip()


class ClaimTypeMemberDescription(_StrictClaimTypeModel):
    """What one literal enum member means, beside the ClaimType that admits it."""

    member: str | int | bool | None
    description: str

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return _canonical_text(value, label="member description", maximum=_MEMBER_DESCRIPTION_MAX)


def _member_key(member: object) -> bytes:
    return canonical_bytes(member)


class ClaimType(_StrictClaimTypeModel):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # The versioned serializer omits fields; it does not turn a ClaimType
        # into an arbitrary dictionary. Keep the declared grammar in OpenAPI.
        def declared(node: CoreSchema) -> CoreSchema:
            result = dict(node)
            if result.get("type") == "model":
                result.pop("serialization", None)
            elif isinstance(result.get("schema"), dict):
                result["schema"] = declared(result["schema"])
            return cast(CoreSchema, result)

        return handler(declared(schema))

    artifact_format: Literal[
        "playbill-claim-type-v1",
        "playbill-claim-type-v3",
        "playbill-claim-type-v4",
        "playbill-claim-type-v5",
        "playbill-claim-type-v6",
        "playbill-claim-type-v7",
    ] = "playbill-claim-type-v1"
    identity: ArtifactIdentity
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
    evidence_admission_policy: (
        ClaimEvidenceAdmissionPolicyV1
        | ClaimEvidenceAdmissionPolicyV2
        | ClaimEvidenceAdmissionPolicy
    )
    admission_policy: ClaimAdmissionPolicy
    resolution_policy: ClaimResolutionPolicy
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()
    # Existing v3 envelopes committed these null placeholders. They remain
    # null-only compatibility bytes, never supported authoring capabilities.
    subject_scope: None = None
    slot_policy: None = None
    evidence_freshness: ClaimEvidenceFreshness | None = None
    attestation_consequence_policy: ClaimAttestationConsequencePolicy | None = None
    #: How long an ``unsure`` examined attestation holds a standing ``next`` row
    #: (stale or uncovered evidence) when it names no ``valid_until``. Absent,
    #: the engine default applies.
    unsure_hold_for: ClaimFreshnessDuration | None = None
    # ClaimType v7. Every earlier format holds these at null or empty and never
    # writes them, so its bytes and digests are exactly what they were.
    #: What the predicate means, for the people and agents who read and write it.
    description: str | None = None
    #: What each literal enum member means, sorted by the member's canonical bytes.
    member_descriptions: tuple[ClaimTypeMemberDescription, ...] = ()
    #: The role a write takes when it names none. Never ``derivation``.
    default_role: ClaimRole | None = None
    #: Read through ``effective_evidence_requirement``; null before v7.
    evidence_requirement: EvidenceRequirement | None = None
    #: Read through ``effective_revision_evidence``; null before v7.
    revision_evidence: RevisionEvidence | None = None

    @model_serializer(mode="wrap")
    def _versioned_wire(self, handler: Any) -> dict[str, object]:
        payload = cast(dict[str, object], handler(self))
        if self.artifact_format != "playbill-claim-type-v7":
            for field in V7_FIELDS:
                payload.pop(field, None)
        if self.unsure_hold_for is None:
            payload.pop("unsure_hold_for", None)
        if self.artifact_format in {
            "playbill-claim-type-v1",
            "playbill-claim-type-v3",
        }:
            payload.pop("attestation_consequence_policy", None)
        if self.artifact_format == "playbill-claim-type-v1":
            payload.pop("subject_scope", None)
            payload.pop("slot_policy", None)
            payload.pop("evidence_freshness", None)
        return payload

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        if not _PREDICATE_RE.fullmatch(value):
            raise ValueError("ClaimType predicate must be a canonical qualified identifier")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        def key(pin: ArtifactPin) -> tuple[bytes, bytes]:
            return pin.role.encode("utf-8"), pin.target.qualified.encode("utf-8")

        if value != tuple(sorted(value, key=key)):
            raise ValueError("ClaimType pins must be canonically sorted")
        identities = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(identities) != len(set(identities)):
            raise ValueError("ClaimType pins must be unique by role and target")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_text(value, label="ClaimType description", maximum=_DESCRIPTION_MAX)

    @field_validator("member_descriptions")
    @classmethod
    def _member_descriptions(
        cls, value: tuple[ClaimTypeMemberDescription, ...]
    ) -> tuple[ClaimTypeMemberDescription, ...]:
        keys = tuple(_member_key(item.member) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError(
                "member descriptions must be sorted and unique by the member's canonical bytes"
            )
        return value

    def _validate_v7_fields(self) -> None:
        if self.artifact_format != "playbill-claim-type-v7":
            if (
                self.description is not None
                or self.member_descriptions
                or self.default_role is not None
                or self.evidence_requirement is not None
                or self.revision_evidence is not None
            ):
                raise ValueError(
                    "only ClaimType v7 can carry descriptions, a default role, an evidence "
                    "requirement or a revision-evidence rule"
                )
            return
        if self.evidence_requirement is None or self.revision_evidence is None:
            raise ValueError(
                "ClaimType v7 states its evidence_requirement and revision_evidence explicitly"
            )
        if self.default_role is not None:
            if self.default_role == "derivation":
                raise ValueError(
                    "default_role cannot be derivation: derivation Claims come only from Procedures"
                )
            if self.default_role not in self.permitted_roles:
                raise ValueError("default_role must be one of the ClaimType's permitted_roles")
        if self.member_descriptions:
            members = None if self.literal_schema is None else self.literal_schema.get("enum")
            if not isinstance(members, list):
                raise ValueError("member descriptions need a literal_schema with a top-level enum")
            admitted = {_member_key(item) for item in members}
            for item in self.member_descriptions:
                if _member_key(item.member) not in admitted:
                    raise ValueError(
                        f"member description names {item.member!r}, which is not an enum member"
                    )

    @model_validator(mode="after")
    def _complete_contract(self) -> "ClaimType":
        expected = ArtifactIdentity(kind="ClaimType", name=self.predicate)
        if self.identity != expected:
            raise ValueError("ClaimType identity must equal ClaimType:<predicate>")
        # Reuse the deliberately policy-free PC-A1 validator so the final wire
        # cannot drift from the reviewed structural surface.
        if self.artifact_format == "playbill-claim-type-v1":
            if self.evidence_freshness is not None:
                raise ValueError("ClaimType v1 cannot carry v3 evidence freshness")
            if self.attestation_consequence_policy is not None:
                raise ValueError("ClaimType v1 cannot carry v4 attestation consequences")
        elif self.artifact_format == "playbill-claim-type-v3":
            if self.evidence_freshness is None:
                raise ValueError("ClaimType v3 requires evidence freshness")
            if self.attestation_consequence_policy is not None:
                raise ValueError("ClaimType v3 cannot carry v4 attestation consequences")
        elif (
            self.artifact_format == "playbill-claim-type-v4"
            and self.attestation_consequence_policy is None
        ):
            raise ValueError("ClaimType v4 requires an attestation consequence policy")
        if self.unsure_hold_for is not None:
            if self.artifact_format not in _CURRENT_POLICY_FORMATS:
                raise ValueError("only ClaimType v5 and later can declare unsure_hold_for")
            if self.unsure_hold_for.microseconds <= 0:
                raise ValueError("ClaimType unsure_hold_for must be positive")
        self._validate_v7_fields()
        if self.artifact_format in _IDENTITY_RULE_FORMATS:
            version = self.artifact_format.removeprefix("playbill-claim-type-")
            if not isinstance(self.evidence_admission_policy, ClaimEvidenceAdmissionPolicy):
                raise ValueError(
                    f"ClaimType {version} requires evidence policy v3 naming CaptureContracts "
                    "by identity"
                )
            if any(pin.target.kind == "Procedure" for pin in self.pins):
                raise ValueError("ClaimTypes cannot depend on producing Procedures")
            if any(pin.target.kind == "CaptureContract" for pin in self.pins):
                raise ValueError(
                    f"ClaimType {version} names CaptureContracts by identity in its evidence "
                    "rules, never by an exact pin"
                )
        elif self.artifact_format == "playbill-claim-type-v5":
            if not isinstance(self.evidence_admission_policy, ClaimEvidenceAdmissionPolicyV2):
                raise ValueError(
                    "ClaimType v5 requires evidence policy v2 without producer authorization"
                )
            if any(pin.target.kind == "Procedure" for pin in self.pins):
                raise ValueError("ClaimTypes cannot depend on producing Procedures")
        elif not isinstance(self.evidence_admission_policy, ClaimEvidenceAdmissionPolicyV1):
            raise ValueError("Historical ClaimTypes require their original evidence policy")
        ClaimTypeStructure(
            predicate=self.predicate,
            allowed_subject_kinds=self.allowed_subject_kinds,
            object_kind=self.object_kind,
            literal_schema=self.literal_schema,
            allowed_object_subject_kinds=self.allowed_object_subject_kinds,
            cardinality=self.cardinality,
            permitted_roles=self.permitted_roles,
            referent_sensitivity=self.referent_sensitivity,
        )
        if self.resolution_policy.cardinality != self.cardinality:
            raise ValueError("ClaimType and resolution-policy cardinality must agree")
        return self

    @property
    def structure(self) -> ClaimTypeStructure:
        return ClaimTypeStructure(
            predicate=self.predicate,
            allowed_subject_kinds=self.allowed_subject_kinds,
            object_kind=self.object_kind,
            literal_schema=self.literal_schema,
            allowed_object_subject_kinds=self.allowed_object_subject_kinds,
            cardinality=self.cardinality,
            permitted_roles=self.permitted_roles,
            referent_sensitivity=self.referent_sensitivity,
        )


def claim_type_path(predicate: str) -> str:
    if not _PREDICATE_RE.fullmatch(predicate):
        raise ClaimTypeFormatError("ClaimType predicate is not path-addressable")
    namespace, _separator, name = predicate.rpartition(".")
    return f"claim-types/{namespace}/{name}.json"


def validate_claim_type_path(
    claim_type: ClaimType,
    path: str,
    *,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> str:
    expected = claim_type_path(claim_type.predicate)
    if not artifact_path_matches(expected, path, codec=codec):
        raise ClaimTypeFormatError(
            f"ClaimType identity/path disagreement: {claim_type.identity.qualified!r} "
            f"requires {artifact_path_for_codec(expected, codec)!r}"
        )
    return path


def render_claim_type(claim_type: ClaimType) -> bytes:
    payload = claim_type.model_dump(mode="json")
    if claim_type.artifact_format in {
        "playbill-claim-type-v1",
        "playbill-claim-type-v3",
    }:
        payload.pop("attestation_consequence_policy", None)
    if claim_type.artifact_format == "playbill-claim-type-v1":
        payload.pop("subject_scope", None)
        payload.pop("slot_policy", None)
        payload.pop("evidence_freshness", None)
    return pretty_canonical_bytes(payload)


def parse_claim_type(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ClaimType:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ClaimTypeFormatError("ClaimType is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("artifact_format") not in CLAIM_TYPE_FORMATS:
        declared = payload.get("artifact_format") if isinstance(payload, dict) else None
        raise ClaimTypeFormatError(f"unsupported ClaimType artifact format: {declared!r}")
    try:
        claim_type = ClaimType.model_validate(payload)
    except ValidationError as exc:
        if payload.get("artifact_format") == "playbill-claim-type-v3" and any(
            tuple(error["loc"])[0:1] == ("evidence_freshness",) for error in exc.errors()
        ):
            raise ClaimTypeFreshnessHorizonInvalid(
                "ClaimType v3 evidence freshness horizon is malformed or non-positive"
            ) from exc
        raise ClaimTypeFormatError("ClaimType failed strict versioned validation") from exc
    validate_claim_type_path(claim_type, path, codec=codec)
    if artifact_bytes_for_path(render_claim_type(claim_type), path, codec=codec) != content:
        raise ClaimTypeFormatError("ClaimType is not in canonical wire form")
    return claim_type


def _claim_type_digest_v1(claim_type: ClaimType) -> ArtifactDigest:
    payload = claim_type.model_dump(mode="json")
    payload.pop("subject_scope", None)
    payload.pop("slot_policy", None)
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        payload,
    )


def _claim_type_digest_v3(claim_type: ClaimType) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        claim_type.model_dump(mode="json"),
    )


def _claim_type_digest_v4(claim_type: ClaimType) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        claim_type.model_dump(mode="json"),
    )


def _claim_type_digest_v5(claim_type: ClaimType) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        claim_type.model_dump(mode="json"),
    )


def _claim_type_digest_v6(claim_type: ClaimType) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        claim_type.model_dump(mode="json"),
    )


def _claim_type_digest_v7(claim_type: ClaimType) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        claim_type.model_dump(mode="json"),
    )


CLAIM_TYPE_DIGEST_FUNCTIONS: dict[str, Callable[[ClaimType], ArtifactDigest]] = {
    "playbill-claim-type-v1": _claim_type_digest_v1,
    "playbill-claim-type-v3": _claim_type_digest_v3,
    "playbill-claim-type-v4": _claim_type_digest_v4,
    "playbill-claim-type-v5": _claim_type_digest_v5,
    "playbill-claim-type-v6": _claim_type_digest_v6,
    "playbill-claim-type-v7": _claim_type_digest_v7,
}


def claim_type_digest(claim_type: ClaimType) -> ArtifactDigest:
    return CLAIM_TYPE_DIGEST_FUNCTIONS[claim_type.artifact_format](claim_type)


def effective_revision_evidence(claim_type: ClaimType) -> RevisionEvidence:
    """What a statement-changing revision keeps; every format before v7 accumulates."""

    return claim_type.revision_evidence or "accumulate"


def effective_evidence_requirement(claim_type: ClaimType) -> EvidenceRequirement:
    """What backs a Claim of this type; every format before v7 means ``self``."""

    return claim_type.evidence_requirement or "self"


def claim_type_accepts_subject(claim_type: ClaimType, subject_kind: str) -> bool:
    return subject_kind in claim_type.allowed_subject_kinds


def claim_type_projection_structure(claim_type: ClaimType) -> dict[str, object]:
    """Project the policy-free finite-subject structure shared by v1 and v3."""

    return claim_type.structure.model_dump(mode="json")


class AcceptedClaimType(_StrictClaimTypeModel):
    path: str
    claim_type: ClaimType
    artifact_digest: str

    @model_validator(mode="after")
    def _correspondence(self) -> "AcceptedClaimType":
        validate_claim_type_path(self.claim_type, self.path)
        if self.artifact_digest != claim_type_digest(self.claim_type).tagged:
            raise ValueError("accepted ClaimType digest differs from its exact envelope")
        return self


class ClaimTypeLawResult(_StrictClaimTypeModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "ClaimTypeLawResult":
        if self.verdict == "accepted":
            if self.artifact_digest is None or self.required_tier is None:
                raise ValueError("accepted ClaimType law result is incomplete")
            if self.diagnostics:
                raise ValueError("accepted ClaimType law result cannot carry diagnostics")
        elif self.artifact_digest is not None or self.required_tier is not None:
            raise ValueError("refused ClaimType law result cannot carry acceptance fields")
        return self


def _diagnostic(code: str, message: str, *, path: str) -> CompilerDiagnostic:
    from cruxible_client.contracts.semantic import SemanticAddress

    return CompilerDiagnostic(
        code=code,
        severity="error",
        message=message,
        subject=SemanticAddress.whole_artifact(path),
    )


def _v7_law_refusal(claim_type: ClaimType, *, path: str) -> CompilerDiagnostic | None:
    """The ClaimType v7 law: a declared requirement must be satisfiable by its rules."""

    from cruxible_client.contracts.captures import (
        COORDINATOR_SELF_SOURCE_CONTRACT_ID,
        DIRECT_SELF_ASSERTED_CONTRACT_ID,
    )

    role = claim_type.default_role
    if role is not None and (role == "derivation" or role not in claim_type.permitted_roles):
        return _diagnostic(
            "playbill.claim_type.default_role_not_permitted",
            f"default_role {role!r} must be one of the permitted roles "
            f"({', '.join(claim_type.permitted_roles)}) and cannot be derivation.",
            path=path,
        )
    requirement = effective_evidence_requirement(claim_type)
    if requirement == "captured":
        own = {DIRECT_SELF_ASSERTED_CONTRACT_ID, COORDINATOR_SELF_SOURCE_CONTRACT_ID}
        declared = any(
            item.target.name not in own
            for rule in claim_type.evidence_admission_policy.rules
            for item in getattr(rule, "capture_contracts", ())
        )
        if not declared:
            return _diagnostic(
                "playbill.claim_type.evidence_requirement_unsatisfiable",
                "evidence_requirement 'captured' needs an evidence rule naming a declared "
                "CaptureContract; every rule names only the Claim's own words.",
                path=path,
            )
    if requirement == "none" and not set(
        claim_type.resolution_policy.required_basis_kinds
    ).issubset({"origin_only"}):
        return _diagnostic(
            "playbill.claim_type.evidence_requirement_unsatisfiable",
            "evidence_requirement 'none' supports a Claim on its origin alone, so "
            "resolution_policy.required_basis_kinds may name only origin_only.",
            path=path,
        )
    return None


def evaluate_claim_type_law(
    claim_type: ClaimType,
    *,
    path: str,
    predecessor: AcceptedClaimType | None,
    accepted_artifacts: Mapping[str, tuple[ArtifactIdentity, str]] | None = None,
) -> ClaimTypeLawResult:
    """Evaluate exact path, lifecycle, and digest-pinned dependencies."""

    try:
        validate_claim_type_path(claim_type, path)
    except ClaimTypeFormatError as exc:
        return ClaimTypeLawResult(
            verdict="refused",
            diagnostics=(_diagnostic("playbill.claim_type.path_mismatch", str(exc), path=path),),
        )
    if claim_type.artifact_format == "playbill-claim-type-v7":
        refusal = _v7_law_refusal(claim_type, path=path)
        if refusal is not None:
            return ClaimTypeLawResult(verdict="refused", diagnostics=(refusal,))
    if accepted_artifacts is not None:
        for pin in claim_type.pins:
            accepted = accepted_artifacts.get(pin.target.qualified)
            if accepted is None or accepted[1] != pin.artifact_digest:
                return ClaimTypeLawResult(
                    verdict="refused",
                    diagnostics=(
                        _diagnostic(
                            "playbill.claim_type.pin_unresolved",
                            "A ClaimType pin does not resolve at the accepted parent coordinate.",
                            path=path,
                        ),
                    ),
                )
    digest = claim_type_digest(claim_type).tagged
    if predecessor is None:
        if claim_type.lifecycle.state != "live" or claim_type.lifecycle.predecessor_digest:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.unexpected_predecessor",
                        "A new ClaimType must begin live without a predecessor.",
                        path=path,
                    ),
                ),
            )
    else:
        previous = predecessor.claim_type
        if previous.identity != claim_type.identity or predecessor.path != path:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.predecessor_identity_mismatch",
                        "The live predecessor has a different ClaimType identity.",
                        path=path,
                    ),
                ),
            )
        if claim_type.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.stale_predecessor",
                        "The ClaimType does not name the exact live predecessor digest.",
                        path=path,
                    ),
                ),
            )
        if previous.lifecycle.state == "retired":
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.lifecycle_invalid",
                        "A retired ClaimType cannot be revived or revised.",
                        path=path,
                    ),
                ),
            )
        if digest == predecessor.artifact_digest:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.no_semantic_change",
                        "ClaimType succession must produce a new artifact digest.",
                        path=path,
                    ),
                ),
            )
    return ClaimTypeLawResult(
        verdict="accepted",
        artifact_digest=digest,
        required_tier="governed_write",
        approval_scope=(),
    )


__all__ = [
    "AcceptedClaimType",
    "CLAIM_TYPE_DIGEST_FUNCTIONS",
    "CLAIM_TYPE_FORMATS",
    "ClaimAttestationConsequencePolicy",
    "ClaimAttestationConsequenceRule",
    "ClaimType",
    "ClaimTypeMemberDescription",
    "EvidenceRequirement",
    "RevisionEvidence",
    "V7_FIELDS",
    "canonical_description_text",
    "effective_evidence_requirement",
    "effective_revision_evidence",
    "ClaimEvidenceFreshness",
    "ClaimFreshnessDuration",
    "ClaimTypeFreshnessHorizonInvalid",
    "ClaimTypeFormatError",
    "ClaimTypeLawResult",
    "claim_type_digest",
    "claim_type_accepts_subject",
    "claim_type_path",
    "claim_type_projection_structure",
    "evaluate_claim_type_law",
    "parse_claim_type",
    "render_claim_type",
    "validate_claim_type_path",
]
