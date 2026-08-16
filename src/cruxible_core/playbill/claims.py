"""First-class Claim artifacts, identity layers, and PC-B acceptance law."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    CasDigest,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.captures import (
    AcceptedCaptureContract,
    CaptureObjectStoreProtocol,
    verify_capture,
)
from cruxible_core.playbill.claim_type_structure import ClaimRole
from cruxible_core.playbill.claim_types import AcceptedClaimType, ClaimType
from cruxible_core.playbill.descriptor_claim_types import DescriptorPredicate
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.discovery import (
    DescriptorAuthorityContextV1,
    evaluate_descriptor_authority,
)
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.governance import PermissionTier
from cruxible_core.playbill.policies import (
    EvidenceAdmissionInputV1,
    evaluate_claim_evidence_admission,
)
from cruxible_core.playbill.principals import PrincipalRegistrySnapshot
from cruxible_core.playbill.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_core.playbill.source_references import EvidenceCommitmentV1
from cruxible_core.playbill.subjects import AcceptedSubject

_CLAIM_ID_RE = re.compile(r"^CLM-[0-9a-f]{32}$")


class ClaimFormatError(PlaybillFormatError):
    """A Claim envelope, path, or frozen semantic preimage is invalid."""


class _StrictClaimModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LiteralClaimObject(_StrictClaimModel):
    kind: Literal["literal"] = "literal"
    value: object

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)


class SubjectClaimObject(_StrictClaimModel):
    kind: Literal["subject"] = "subject"
    address: SemanticAddress


class ExactContentClaimObject(_StrictClaimModel):
    kind: Literal["exact_content"] = "exact_content"
    content_digest: str
    span: ContentSpan | None = None

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _span_binding(self) -> "ExactContentClaimObject":
        if self.span is not None and self.span.content_digest != self.content_digest:
            raise ValueError("exact-content span differs from its content digest")
        return self


ClaimObject = Annotated[
    LiteralClaimObject | SubjectClaimObject | ExactContentClaimObject,
    Field(discriminator="kind"),
]


class ClaimStatement(_StrictClaimModel):
    tag: Literal["playbill-claim-statement-v1"] = "playbill-claim-statement-v1"
    subject: SemanticAddress
    claim_type: ArtifactIdentity
    claim_type_digest: str
    predicate: str
    qualifier: str | None = None
    object: ClaimObject
    role: ClaimRole
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    shell_context_digest: str | None = None

    @field_validator("claim_type_digest")
    @classmethod
    def _claim_type_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @field_validator("shell_context_digest")
    @classmethod
    def _shell_context_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _statement_shape(self) -> "ClaimStatement":
        if self.claim_type.kind != "ClaimType":
            raise ValueError("Claim statement must name a ClaimType identity")
        if self.claim_type.name != self.predicate:
            raise ValueError("Claim statement predicate must equal its ClaimType identity")
        for value in (self.effective_from, self.effective_until):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("Claim effective times must be timezone-aware")
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("Claim effective interval must be increasing")
        return self


class ClaimReferentContext(_StrictClaimModel):
    tag: Literal["playbill-claim-referent-context-v1"] = "playbill-claim-referent-context-v1"
    subject_content_digest: str
    object_content_digest: str | None = None
    observed_at: datetime

    @field_validator("subject_content_digest", "object_content_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @field_validator("observed_at")
    @classmethod
    def _observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Claim referent observed_at must be timezone-aware")
        return value


class ClaimBacking(_StrictClaimModel):
    tag: Literal["playbill-claim-backing-v1"] = "playbill-claim-backing-v1"
    referent_context: ClaimReferentContext
    capture_digests: tuple[str, ...] = ()
    attestation_digests: tuple[str, ...] = ()
    input_claim_digests: tuple[str, ...] = ()
    reducer_digest: str | None = None
    source_mappings: tuple[SourceMapping, ...] = ()

    @field_validator("capture_digests", "attestation_digests")
    @classmethod
    def _cas_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("Claim CAS digest sets must be sorted and unique")
        for item in value:
            CasDigest.from_tagged(item)
        return value

    @field_validator("input_claim_digests")
    @classmethod
    def _claim_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("input Claim digest set must be sorted and unique")
        for item in value:
            ArtifactDigest.from_tagged(item)
        return value

    @field_validator("reducer_digest")
    @classmethod
    def _reducer_digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @field_validator("source_mappings")
    @classmethod
    def _source_mappings(cls, value: tuple[SourceMapping, ...]) -> tuple[SourceMapping, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("Claim source mappings must be canonically sorted and unique")
        return value

    @model_validator(mode="after")
    def _derivation_shape(self) -> "ClaimBacking":
        if (self.reducer_digest is None) != (not self.input_claim_digests):
            raise ValueError("Claim derivation requires reducer and input Claims together")
        return self


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes]:
    return pin.role.encode("utf-8"), pin.target.qualified.encode("utf-8")


class ClaimArtifact(_StrictClaimModel):
    artifact_format: Literal["playbill-claim-v1"] = "playbill-claim-v1"
    identity: ArtifactIdentity
    statement: ClaimStatement
    backing: ClaimBacking
    authority: ArtifactAuthority
    pins: tuple[ArtifactPin, ...]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("Claim pins must be canonically sorted")
        identities = tuple((item.role, item.target.qualified) for item in value)
        if len(identities) != len(set(identities)):
            raise ValueError("Claim pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _identity_shape(self) -> "ClaimArtifact":
        if self.identity.kind != "Claim" or not _CLAIM_ID_RE.fullmatch(self.identity.name):
            raise ValueError("Claim identity must be Claim:CLM- plus 128-bit lowercase hex")
        return self


def new_claim_id() -> str:
    """Allocate a proposal-side opaque lineage name using the operating-system CSPRNG."""

    return f"CLM-{secrets.token_hex(16)}"


def claim_path(claim_id: str) -> str:
    if not _CLAIM_ID_RE.fullmatch(claim_id):
        raise ClaimFormatError("Claim ID must be CLM- plus 32 lowercase hexadecimal digits")
    return f"claims/{claim_id[4:6]}/{claim_id}.yaml"


def validate_claim_path(claim: ClaimArtifact, path: str) -> str:
    expected = claim_path(claim.identity.name)
    if path != expected:
        raise ClaimFormatError(
            f"Claim identity/path disagreement: {claim.identity.qualified!r} requires {expected!r}"
        )
    return path


def render_claim(claim: ClaimArtifact) -> bytes:
    return canonical_bytes(claim.model_dump(mode="json")) + b"\n"


def parse_claim(content: bytes, *, path: str) -> ClaimArtifact:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ClaimFormatError("Claim is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("artifact_format") != "playbill-claim-v1":
        declared = payload.get("artifact_format") if isinstance(payload, dict) else None
        raise ClaimFormatError(f"unsupported Claim artifact format: {declared!r}")
    try:
        claim = ClaimArtifact.model_validate(payload)
    except ValidationError as exc:
        raise ClaimFormatError("Claim failed strict v1 validation") from exc
    if render_claim(claim) != content:
        raise ClaimFormatError("Claim is not in canonical wire form")
    validate_claim_path(claim, path)
    return claim


def claim_statement_digest(statement: ClaimStatement) -> Sha256Value:
    payload = statement.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, "playbill-claim-statement-v1", payload)


def claim_referent_context_digest(context: ClaimReferentContext) -> Sha256Value:
    """Digest only exact shells; acquisition time remains backing, not statement identity."""

    return typed_digest(
        Sha256Value,
        "playbill-claim-referent-context-v1",
        {
            "subject_content_digest": context.subject_content_digest,
            "object_content_digest": context.object_content_digest,
        },
    )


def claim_artifact_digest(claim: ClaimArtifact) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        {
            "artifact_format": claim.artifact_format,
            "identity": claim.identity.model_dump(mode="json"),
            "statement_digest": claim_statement_digest(claim.statement).tagged,
            "backing": claim.backing.model_dump(mode="json"),
            "authority": claim.authority.model_dump(mode="json"),
            "pins": [item.model_dump(mode="json") for item in claim.pins],
            "lifecycle": claim.lifecycle.model_dump(mode="json"),
        },
    )


def claim_statement_address(path: str) -> SemanticAddress:
    return SemanticAddress.claim_statement(path)


class AcceptedClaim(_StrictClaimModel):
    path: str
    claim: ClaimArtifact
    statement_digest: str
    artifact_digest: str

    @model_validator(mode="after")
    def _correspondence(self) -> "AcceptedClaim":
        validate_claim_path(self.claim, self.path)
        if self.statement_digest != claim_statement_digest(self.claim.statement).tagged:
            raise ValueError("accepted Claim statement digest does not reproduce")
        if self.artifact_digest != claim_artifact_digest(self.claim).tagged:
            raise ValueError("accepted Claim artifact digest does not reproduce")
        return self


class ClaimLawEvidenceV1(_StrictClaimModel):
    tag: Literal["playbill-claim-law-evidence-v1"] = "playbill-claim-law-evidence-v1"
    law_digest: str
    adjudication_rule_digest: str
    statement_digest: str
    artifact_digest: str
    initial_verdict: Literal["supported", "uncovered", "stale", "contradicted", "unresolved"]
    evidence_basis: tuple[Literal["origin_only", "direct", "derivational"], ...]

    @field_validator(
        "law_digest", "adjudication_rule_digest", "statement_digest", "artifact_digest"
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ClaimLawResult(_StrictClaimModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    statement_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    evidence: ClaimLawEvidenceV1 | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "ClaimLawResult":
        complete = all(
            item is not None
            for item in (
                self.artifact_digest,
                self.statement_digest,
                self.required_tier,
                self.evidence,
            )
        ) and bool(self.approval_scope)
        if self.verdict == "accepted" and (not complete or self.diagnostics):
            raise ValueError("accepted Claim law result is incomplete")
        if self.verdict == "refused" and complete:
            raise ValueError("refused Claim law result cannot be complete")
        return self


def _diagnostic(code: str, message: str, *, path: str) -> ClaimLawResult:
    return ClaimLawResult(
        verdict="refused",
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity="error",
                message=message,
                subject=SemanticAddress.whole_artifact(path),
            ),
        ),
    )


def _validate_literal_schema(value: object, schema: Mapping[str, object]) -> bool:
    """Deterministic closed subset sufficient for frozen ClaimType v1 schemas."""

    if "const" in schema and canonical_bytes(value) != canonical_bytes(schema["const"]):
        return False
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or canonical_bytes(value) not in {
            canonical_bytes(item) for item in enum
        }:
            return False
    kind = schema.get("type")
    matches = {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }
    if kind is not None and not matches.get(str(kind), False):
        return False
    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        pattern = schema.get("pattern")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            return False
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            return False
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            return False
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list) and not set(required).issubset(value):
            return False
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name, member in value.items():
                member_schema = properties.get(name)
                if isinstance(member_schema, dict) and not _validate_literal_schema(
                    member, member_schema
                ):
                    return False
                if member_schema is None and schema.get("additionalProperties") is False:
                    return False
    item_schema = schema.get("items")
    if isinstance(value, list) and isinstance(item_schema, dict):
        if any(not _validate_literal_schema(item, item_schema) for item in value):
            return False
    return True


@dataclass(frozen=True)
class _ResolvedReferent:
    identity: ArtifactIdentity
    artifact_digest: str
    semantic_kind: str
    authority: ArtifactAuthority


def _resolved_referent(
    address: SemanticAddress,
    subjects: Mapping[str, AcceptedSubject],
    claim_types: Mapping[str, AcceptedClaimType],
    *,
    descriptor: bool,
) -> _ResolvedReferent | None:
    if address.selector.scheme != "artifact-v1":
        return None
    subject = subjects.get(address.artifact_path)
    if subject is not None:
        return _ResolvedReferent(
            identity=subject.shell.identity,
            artifact_digest=subject.artifact_digest,
            semantic_kind=("semantic.subject" if descriptor else subject.shell.subject_kind),
            authority=subject.shell.authority,
        )
    claim_type = next(
        (item for item in claim_types.values() if item.path == address.artifact_path),
        None,
    )
    if descriptor and claim_type is not None:
        return _ResolvedReferent(
            identity=claim_type.claim_type.identity,
            artifact_digest=claim_type.artifact_digest,
            semantic_kind="semantic.claim_type",
            authority=claim_type.claim_type.authority,
        )
    return None


def _required_pin(
    claim: ClaimArtifact,
    *,
    role: str,
    identity: ArtifactIdentity,
    digest: str,
) -> bool:
    return any(
        pin.role == role and pin.target == identity and pin.artifact_digest == digest
        for pin in claim.pins
    )


def evaluate_claim_law(
    claim: ClaimArtifact,
    *,
    path: str,
    principals: PrincipalRegistrySnapshot,
    actor_id: str | None,
    predecessor: AcceptedClaim | None,
    subjects: Mapping[str, AcceptedSubject],
    claim_types: Mapping[str, AcceptedClaimType],
    capture_contracts: Mapping[str, AcceptedCaptureContract],
    capture_store: CaptureObjectStoreProtocol,
    law_digest: str,
) -> ClaimLawResult:
    """Evaluate one Claim against exact resolved dependencies and immutable Captures."""

    try:
        validate_claim_path(claim, path)
    except ClaimFormatError as exc:
        return _diagnostic("playbill.claim.path_mismatch", str(exc), path=path)
    statement = claim.statement
    claim_type = claim_types.get(statement.claim_type.qualified)
    if claim_type is None or claim_type.artifact_digest != statement.claim_type_digest:
        return _diagnostic(
            "playbill.claim.claim_type_unresolved",
            "The exact ClaimType dependency does not resolve.",
            path=path,
        )
    contract: ClaimType = claim_type.claim_type
    descriptor = statement.predicate in {
        "semantic.alias",
        "semantic.distinct_from",
        "semantic.related_to",
        "semantic.tag",
    }
    if not _required_pin(
        claim,
        role="claim-type",
        identity=contract.identity,
        digest=claim_type.artifact_digest,
    ):
        return _diagnostic(
            "playbill.claim.claim_type_pin_missing",
            "The Claim does not pin its exact ClaimType artifact.",
            path=path,
        )
    subject = _resolved_referent(
        statement.subject,
        subjects,
        claim_types,
        descriptor=descriptor,
    )
    if subject is None:
        return _diagnostic(
            "playbill.claim.subject_unresolved",
            "The Claim subject does not resolve to an exact Subject shell.",
            path=path,
        )
    if subject.semantic_kind not in contract.allowed_subject_kinds:
        return _diagnostic(
            "playbill.claim.subject_kind_forbidden",
            "The Claim subject kind is not admitted by its ClaimType.",
            path=path,
        )
    if claim.backing.referent_context.subject_content_digest != subject.artifact_digest:
        return _diagnostic(
            "playbill.claim.subject_context_mismatch",
            "The proposer-observed Subject shell digest does not resolve.",
            path=path,
        )
    if not _required_pin(
        claim,
        role="subject",
        identity=subject.identity,
        digest=subject.artifact_digest,
    ):
        return _diagnostic(
            "playbill.claim.subject_pin_missing",
            "The Claim does not pin its exact Subject shell.",
            path=path,
        )

    object_subject: _ResolvedReferent | None = None
    if statement.object.kind != contract.object_kind:
        return _diagnostic(
            "playbill.claim.object_kind_mismatch",
            "The Claim object kind differs from its ClaimType.",
            path=path,
        )
    if isinstance(statement.object, LiteralClaimObject):
        assert contract.literal_schema is not None
        if not _validate_literal_schema(statement.object.value, contract.literal_schema):
            return _diagnostic(
                "playbill.claim.literal_schema_invalid",
                "The Claim literal fails its exact ClaimType schema.",
                path=path,
            )
        if claim.backing.referent_context.object_content_digest is not None:
            return _diagnostic(
                "playbill.claim.object_context_unexpected",
                "Literal Claims cannot name an object Subject shell digest.",
                path=path,
            )
    elif isinstance(statement.object, SubjectClaimObject):
        object_subject = _resolved_referent(
            statement.object.address,
            subjects,
            claim_types,
            descriptor=descriptor,
        )
        if object_subject is None:
            return _diagnostic(
                "playbill.claim.object_subject_unresolved",
                "The Claim object Subject does not resolve.",
                path=path,
            )
        if object_subject.semantic_kind not in contract.allowed_object_subject_kinds:
            return _diagnostic(
                "playbill.claim.object_subject_kind_forbidden",
                "The object Subject kind is not admitted by its ClaimType.",
                path=path,
            )
        if claim.backing.referent_context.object_content_digest != object_subject.artifact_digest:
            return _diagnostic(
                "playbill.claim.object_context_mismatch",
                "The proposer-observed object Subject shell digest does not resolve.",
                path=path,
            )
        if not _required_pin(
            claim,
            role="object-subject",
            identity=object_subject.identity,
            digest=object_subject.artifact_digest,
        ):
            return _diagnostic(
                "playbill.claim.object_subject_pin_missing",
                "The Claim does not pin its exact object Subject shell.",
                path=path,
            )
    elif claim.backing.referent_context.object_content_digest is not None:
        return _diagnostic(
            "playbill.claim.object_context_unexpected",
            "Exact-content Claims cannot name an object Subject shell digest.",
            path=path,
        )

    if statement.predicate != contract.predicate or statement.role not in contract.permitted_roles:
        return _diagnostic(
            "playbill.claim.statement_contract_mismatch",
            "The Claim predicate or role differs from its ClaimType contract.",
            path=path,
        )
    expected_shell_digest = claim_referent_context_digest(claim.backing.referent_context).tagged
    if contract.referent_sensitivity == "shell":
        if statement.shell_context_digest != expected_shell_digest:
            return _diagnostic(
                "playbill.claim.shell_context_mismatch",
                "A shell-sensitive Claim must bind the exact referent-context digest.",
                path=path,
            )
    elif statement.shell_context_digest is not None:
        return _diagnostic(
            "playbill.claim.shell_context_forbidden",
            "An identity-sensitive Claim cannot add shell bytes to statement identity.",
            path=path,
        )
    if claim.backing.attestation_digests:
        return _diagnostic(
            "playbill.claim.attestations_deferred",
            "Signed ClaimAttestations activate in PC-C.",
            path=path,
        )
    roles: set[str] = set()
    if actor_id is not None:
        try:
            roles = {
                str(role)
                for role in principals.require_active(actor_id).authority_roles
                if role != "daemon"
            }
        except Exception:
            roles = set()
    if claim.authority != contract.authority:
        return _diagnostic(
            "playbill.claim.authority_mismatch",
            "Claim authority must be derived byte-exactly from its accepted ClaimType.",
            path=path,
        )
    if descriptor:
        descriptor_authority = evaluate_descriptor_authority(
            cast(DescriptorPredicate, statement.predicate),
            DescriptorAuthorityContextV1(
                actor_roles=tuple(sorted(roles, key=lambda item: item.encode("utf-8"))),
                target_namespace_roles=subject.authority.propose_roles,
                recall_descriptor_roles=contract.authority.propose_roles,
                new_item_namespace_roles=subject.authority.propose_roles,
                blocking_cross_namespace_roles=(
                    () if object_subject is None else object_subject.authority.propose_roles
                ),
            ),
        )
        if descriptor_authority.verdict == "refused":
            return _diagnostic(
                descriptor_authority.refusal_code or "playbill.descriptor.authority_refused",
                "The authenticated actor does not satisfy the descriptor authority floor.",
                path=path,
            )
    if not descriptor and not roles.intersection(claim.authority.propose_roles):
        return _diagnostic(
            "playbill.claim.actor_unauthorized",
            "The authenticated actor lacks ClaimType-derived proposal authority.",
            path=path,
        )
    digest = claim_artifact_digest(claim).tagged
    if predecessor is None:
        if claim.lifecycle != ArtifactLifecycle():
            return _diagnostic(
                "playbill.claim.unexpected_predecessor",
                "A new Claim must begin live without a predecessor.",
                path=path,
            )
    else:
        if predecessor.path != path or predecessor.claim.identity != claim.identity:
            return _diagnostic(
                "playbill.claim.predecessor_identity_mismatch",
                "The live predecessor belongs to a different Claim lineage.",
                path=path,
            )
        if claim.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _diagnostic(
                "playbill.claim.stale_predecessor",
                "The Claim successor does not name the exact live predecessor.",
                path=path,
            )
        if predecessor.claim.lifecycle.state == "retired":
            return _diagnostic(
                "playbill.claim.lifecycle_invalid",
                "A retired Claim lineage cannot be revived.",
                path=path,
            )
        if claim.authority != predecessor.claim.authority:
            return _diagnostic(
                "playbill.claim.authority_change_unsupported",
                "Claim succession cannot weaken or rewrite accepted authority in v1.",
                path=path,
            )
        old = predecessor.claim.backing
        if not (
            set(old.capture_digests).issubset(claim.backing.capture_digests)
            and set(old.attestation_digests).issubset(claim.backing.attestation_digests)
            and set(old.input_claim_digests).issubset(claim.backing.input_claim_digests)
        ):
            return _diagnostic(
                "playbill.claim.required_backing_dropped",
                "Claim succession cannot silently drop accepted backing.",
                path=path,
            )
        if digest == predecessor.artifact_digest:
            return _diagnostic(
                "playbill.claim.no_semantic_change",
                "Claim succession must produce a new artifact digest.",
                path=path,
            )

    statement_address = claim_statement_address(path)
    if any(mapping.subject != statement_address for mapping in claim.backing.source_mappings):
        return _diagnostic(
            "playbill.claim.source_mapping_subject_mismatch",
            "Every Claim source mapping must target its exact statement address.",
            path=path,
        )
    evidence_basis: set[Literal["origin_only", "direct", "derivational"]] = set()
    verified_commitments: dict[str, EvidenceCommitmentV1] = {}
    capture_contract_pin_digests = {
        pin.artifact_digest for pin in claim.pins if pin.role == "capture-contract"
    }
    for capture_digest_value in claim.backing.capture_digests:
        envelope = None
        resolved_contract = None
        for contract_candidate in capture_contracts.values():
            try:
                candidate_envelope = verify_capture(
                    capture_digest_value,
                    store=capture_store,
                    contract=contract_candidate.contract,
                )
            except (PlaybillFormatError, ValueError):
                continue
            envelope = candidate_envelope
            resolved_contract = contract_candidate
            break
        if envelope is None or resolved_contract is None:
            return _diagnostic(
                "playbill.claim.capture_unverified",
                "A backing Capture or its exact CaptureContract cannot be verified.",
                path=path,
            )
        if resolved_contract.artifact_digest not in capture_contract_pin_digests:
            return _diagnostic(
                "playbill.claim.capture_contract_pin_missing",
                "A backing CaptureContract is not pinned by the Claim.",
                path=path,
            )
        verified_commitments[envelope.commitment.digest] = envelope.commitment
        relevant_spans = tuple(
            span
            for mapping in claim.backing.source_mappings
            for span in mapping.spans
            if span.content_digest == envelope.commitment.digest
        )
        source_bound = bool(relevant_spans) or (
            getattr(envelope.source, "selector_type", None)
            in {"direct-claim-source-v1", "direct-claim-external-selector-v1"}
            and isinstance(getattr(envelope.source, "selector", None), dict)
            and getattr(envelope.source, "selector", {}).get("claim_id") == claim.identity.name
        )
        for kind in resolved_contract.contract.evidence_kinds:
            admission = evaluate_claim_evidence_admission(
                contract.evidence_admission_policy,
                EvidenceAdmissionInputV1(
                    claim_role=statement.role,
                    capture_contract_digest=resolved_contract.artifact_digest,
                    evidence_kind=kind,
                    reducer_digest=claim.backing.reducer_digest,
                    input_claim_artifact_digests=claim.backing.input_claim_digests,
                    source_subject_bound=source_bound,
                ),
            )
            if admission.verdict == "eligible" and admission.admission is not None:
                evidence_basis.add(admission.admission)
    for mapping in claim.backing.source_mappings:
        for span in mapping.spans:
            commitment = verified_commitments.get(span.content_digest)
            if (
                commitment is None
                or getattr(commitment, "digest_kind", None) != "exact_bytes"
                or getattr(commitment, "byte_length", None) is None
                or span.end_byte > getattr(commitment, "byte_length")
            ):
                return _diagnostic(
                    "playbill.claim.source_mapping_unverified",
                    "A source span is not bounded by an exact verified Capture commitment.",
                    path=path,
                )
    if isinstance(statement.object, ExactContentClaimObject):
        exact_commitment = verified_commitments.get(statement.object.content_digest)
        if exact_commitment is None:
            return _diagnostic(
                "playbill.claim.exact_content_unverified",
                "The exact-content object has no verified backing Capture commitment.",
                path=path,
            )
        if statement.object.span is not None:
            mapped_spans = {
                canonical_bytes(span.model_dump(mode="json"))
                for mapping in claim.backing.source_mappings
                for span in mapping.spans
            }
            if canonical_bytes(statement.object.span.model_dump(mode="json")) not in mapped_spans:
                return _diagnostic(
                    "playbill.claim.exact_content_span_unmapped",
                    "The exact-content object span is not mapped to the Claim statement.",
                    path=path,
                )
    if statement.role == "environment_binding" and not isinstance(
        statement.object, ExactContentClaimObject
    ):
        return _diagnostic(
            "playbill.claim.environment_binding_not_exact",
            "An environment-binding Claim must identify exact content bytes.",
            path=path,
        )
    if not claim.backing.capture_digests:
        return _diagnostic(
            "playbill.claim.origin_capture_missing",
            "Every accepted Claim requires inspectable origin Capture backing.",
            path=path,
        )
    if statement.role == "derivation" and (
        not claim.backing.input_claim_digests or claim.backing.reducer_digest is None
    ):
        return _diagnostic(
            "playbill.claim.derivation_incomplete",
            "A derivation Claim requires exact inputs and reducer backing.",
            path=path,
        )
    if statement.role != "derivation" and (
        claim.backing.input_claim_digests or claim.backing.reducer_digest is not None
    ):
        return _diagnostic(
            "playbill.claim.derivation_forbidden",
            "Only derivation Claims may name reducers and input Claims.",
            path=path,
        )
    if not evidence_basis:
        evidence_basis.add("origin_only")
    statement_digest = claim_statement_digest(statement).tagged
    adjudication_rule_digest = typed_digest(
        Sha256Value,
        "playbill-claim-adjudication-rule-v1",
        {
            "claim_type_digest": claim_type.artifact_digest,
            "evidence_policy": contract.evidence_admission_policy.model_dump(mode="json"),
            "resolution_policy": contract.resolution_policy.model_dump(mode="json"),
        },
    ).tagged
    basis = tuple(sorted(evidence_basis, key=lambda item: item.encode("utf-8")))
    initial_verdict: Literal["supported", "uncovered"] = (
        "supported" if any(item != "origin_only" for item in basis) else "uncovered"
    )
    return ClaimLawResult(
        verdict="accepted",
        artifact_digest=digest,
        statement_digest=statement_digest,
        required_tier="governed_write",
        approval_scope=claim.authority.approve_roles,
        evidence=ClaimLawEvidenceV1(
            law_digest=law_digest,
            adjudication_rule_digest=adjudication_rule_digest,
            statement_digest=statement_digest,
            artifact_digest=digest,
            initial_verdict=initial_verdict,
            evidence_basis=basis,
        ),
    )


__all__ = [
    "AcceptedClaim",
    "ClaimArtifact",
    "ClaimBacking",
    "ClaimFormatError",
    "ClaimLawEvidenceV1",
    "ClaimLawResult",
    "ClaimObject",
    "ClaimReferentContext",
    "ClaimStatement",
    "ExactContentClaimObject",
    "LiteralClaimObject",
    "SubjectClaimObject",
    "claim_artifact_digest",
    "claim_path",
    "claim_referent_context_digest",
    "claim_statement_address",
    "claim_statement_digest",
    "evaluate_claim_law",
    "new_claim_id",
    "parse_claim",
    "render_claim",
    "validate_claim_path",
]
