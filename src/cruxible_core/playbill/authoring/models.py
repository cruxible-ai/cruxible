"""Frozen PC-G1b authoring wires and deterministic digest preimages."""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity
from cruxible_core.playbill.candidates import validate_candidate_timestamp
from cruxible_core.playbill.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.claim_type_structure import ClaimRole
from cruxible_core.playbill.claims import LiteralClaimObject, SubjectClaimObject, claim_path
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalReceiveLimits
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.types import CompilerCoordinate
from cruxible_core.temporal import ensure_utc, format_datetime

AUTHORING_INTENT_ID_RE = re.compile(r"^AIT-[0-9a-f]{32}$")
_CANONICAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_GIT_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")

AUTHORING_PAYLOAD_DIGEST_DOMAIN = "playbill-authoring-payload-v1"
AUTHORING_CREATE_FINGERPRINT_DOMAIN = "playbill-authoring-create-fingerprint-v1"
AUTHORING_RESOLVED_DIGEST_DOMAIN = "playbill-authoring-resolved-v1"
AUTHORING_CANDIDATE_TREE_DIGEST_DOMAIN = "playbill-authoring-candidate-tree-v1"
AUTHORING_FRONTIER_DIGEST_DOMAIN = "playbill-authoring-frontier-v1"
AUTHORING_INSTANCE_DESCRIPTOR_DIGEST_DOMAIN = "playbill-instance-descriptor-v1"
AUTHORING_PREFLIGHT_CERTIFICATE_DIGEST_DOMAIN = "playbill-authoring-preflight-certificate-v1"

MAX_DIAGNOSTICS = 128
MAX_BLOCKED_CHECKS = 128
MAX_REPAIR_ALTERNATIVES = 4
MAX_REPAIR_BYTES = 16 * 1024
MAX_FRONTIER_BYTES = 1024 * 1024

CandidateStatusState: TypeAlias = Literal[
    "draft",
    "preflight_refused",
    "ready_to_submit",
    "awaiting_external_approval",
    "approval_invalid",
    "ready_to_activate",
    "conflicted_after_rebase",
    "superseded",
    "accepted",
    "terminal",
]
DiagnosticOwner: TypeAlias = Literal["writer", "approver", "daemon", "external_state"]
DiagnosticDisposition: TypeAlias = Literal["edit_and_retry", "wait", "superseded", "terminal"]


class _StrictAuthoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_base64(value: str, *, label: str) -> bytes:
    try:
        content = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} must be canonical base64") from exc
    if base64.b64encode(content).decode("ascii") != value:
        raise ValueError(f"{label} must use canonical base64 spelling")
    return content


def _sha256(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be tagged lowercase SHA-256") from exc
    return value


class AuthoringExactContentObjectV1(_StrictAuthoringModel):
    kind: Literal["exact_content_body"] = "exact_content_body"
    content_base64: str

    @field_validator("content_base64")
    @classmethod
    def _content(cls, value: str) -> str:
        _canonical_base64(value, label="exact-content body")
        return value

    @property
    def content(self) -> bytes:
        return _canonical_base64(self.content_base64, label="exact-content body")


AuthoringClaimObjectV1 = Annotated[
    LiteralClaimObject | SubjectClaimObject | AuthoringExactContentObjectV1,
    Field(discriminator="kind"),
]


class AuthoringClaimStatementV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-claim-statement-v1"] = "playbill-authoring-claim-statement-v1"
    subject: SemanticAddress
    predicate: str
    qualifier: str | None = None
    object: AuthoringClaimObjectV1
    role: ClaimRole
    effective_from: datetime | None = None
    effective_until: datetime | None = None

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("authoring predicate must be nonblank and normalized")
        return value

    @field_validator("effective_from", "effective_until")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_serializer("effective_from", "effective_until", when_used="json")
    def _serialize_times(self, value: datetime | None) -> str | None:
        return None if value is None else format_datetime(value)

    @model_validator(mode="after")
    def _interval(self) -> "AuthoringClaimStatementV1":
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("Claim effective interval must be increasing")
        return self


class AuthoringExistingClaimDispositionV1(_StrictAuthoringModel):
    claim_id: str
    disposition: Literal["not_tested", "support", "contradict", "unsure"]

    @field_validator("claim_id")
    @classmethod
    def _claim_id(cls, value: str) -> str:
        claim_path(value)
        return value


class WorkingGitBlobCoordinateV1(_StrictAuthoringModel):
    kind: Literal["git_blob"] = "git_blob"
    repository_id: str
    commit_oid: str
    blob_oid: str
    source_byte_length: int = Field(ge=0)

    @field_validator("repository_id")
    @classmethod
    def _repository_id(cls, value: str) -> str:
        if not _REPOSITORY_ID_RE.fullmatch(value):
            raise ValueError("repository_id must be locator-free and canonical")
        return value

    @field_validator("commit_oid", "blob_oid")
    @classmethod
    def _oid(cls, value: str) -> str:
        if not _GIT_OID_RE.fullmatch(value):
            raise ValueError("working Git coordinate OID is malformed")
        return value


class WorkingDigestCoordinateV1(_StrictAuthoringModel):
    kind: Literal["observed_digest"] = "observed_digest"
    source_content_digest: str
    source_byte_length: int = Field(ge=0)

    @field_validator("source_content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="working source content digest")


WorkingSelectionCoordinateV1 = Annotated[
    WorkingGitBlobCoordinateV1 | WorkingDigestCoordinateV1,
    Field(discriminator="kind"),
]


class WorkingAnchorWindowV1(_StrictAuthoringModel):
    tag: Literal["playbill-working-anchor-window-v1"] = "playbill-working-anchor-window-v1"
    anchor: str
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=1)
    observed_occurrence_count: int = Field(ge=0)

    @field_validator("anchor")
    @classmethod
    def _anchor(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("working selection anchor must be nonblank and normalized")
        return value

    @model_validator(mode="after")
    def _window(self) -> "WorkingAnchorWindowV1":
        if self.end_byte <= self.start_byte:
            raise ValueError("working selection window must cover at least one byte")
        return self


class WorkingSelectionObservationV1(_StrictAuthoringModel):
    tag: Literal["playbill-working-selection-observation-v1"] = (
        "playbill-working-selection-observation-v1"
    )
    source_id: str
    coordinate: WorkingSelectionCoordinateV1
    selected_content_base64: str
    selected_bytes_digest: str
    selector: WorkingAnchorWindowV1

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        if not _CANONICAL_NAME_RE.fullmatch(value):
            raise ValueError("working source_id must be stable, locator-free, and canonical")
        return value

    @field_validator("selected_content_base64")
    @classmethod
    def _selected_content(cls, value: str) -> str:
        _canonical_base64(value, label="working selected content")
        return value

    @field_validator("selected_bytes_digest")
    @classmethod
    def _selected_digest(cls, value: str) -> str:
        return _sha256(value, label="working selected-bytes digest")

    @model_validator(mode="after")
    def _internal_correspondence(self) -> "WorkingSelectionObservationV1":
        import hashlib

        selected = self.selected_content
        if self.selector.end_byte > self.coordinate.source_byte_length:
            raise ValueError("working selection exceeds the observed whole-source length")
        if len(selected) != self.selector.end_byte - self.selector.start_byte:
            raise ValueError("working selected bytes differ from the declared window length")
        digest = "sha256:" + hashlib.sha256(selected).hexdigest()
        if digest != self.selected_bytes_digest:
            raise ValueError("working selected-bytes digest does not reproduce")
        return self

    @property
    def selected_content(self) -> bytes:
        return _canonical_base64(
            self.selected_content_base64,
            label="working selected content",
        )


class SelfSourceBodyV1(_StrictAuthoringModel):
    tag: Literal["playbill-self-source-body-v1"] = "playbill-self-source-body-v1"
    content_base64: str

    @field_validator("content_base64")
    @classmethod
    def _content(cls, value: str) -> str:
        _canonical_base64(value, label="self-source body")
        return value

    @property
    def content(self) -> bytes:
        return _canonical_base64(self.content_base64, label="self-source body")


ClaimAuthoringSourceV1 = Annotated[
    WorkingSelectionObservationV1 | SelfSourceBodyV1,
    Field(discriminator="tag"),
]


class ClaimAuthoringPayloadV1(_StrictAuthoringModel):
    tag: Literal["playbill-claim-authoring-payload-v1"] = "playbill-claim-authoring-payload-v1"
    statement: AuthoringClaimStatementV1
    rationale: str
    source: ClaimAuthoringSourceV1
    citation_role: Literal["evidence", "copy"] | None = None
    claim_ref: str | None = None
    existing_claim_dispositions: tuple[AuthoringExistingClaimDispositionV1, ...] = ()
    insertion_target: object | None = None

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Claim authoring rationale must not be empty")
        return value

    @field_validator("claim_ref")
    @classmethod
    def _claim_ref(cls, value: str | None) -> str | None:
        if value is not None:
            claim_path(value)
        return value

    @field_validator("insertion_target", mode="before")
    @classmethod
    def _target(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @field_validator("existing_claim_dispositions")
    @classmethod
    def _dispositions(
        cls,
        value: tuple[AuthoringExistingClaimDispositionV1, ...],
    ) -> tuple[AuthoringExistingClaimDispositionV1, ...]:
        ids = tuple(item.claim_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("ascii"))):
            raise ValueError("existing Claim dispositions must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _source_role(self) -> "ClaimAuthoringPayloadV1":
        if isinstance(self.source, WorkingSelectionObservationV1):
            if self.citation_role is None:
                raise ValueError("Flow A requires an explicit citation_role")
        elif self.citation_role is not None:
            raise ValueError("Flow B self-source fixes its citation role server-side")
        return self


class AuthoringArtifactReferenceV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-artifact-reference-v1"] = (
        "playbill-authoring-artifact-reference-v1"
    )
    role: str
    target: ArtifactIdentity
    resolution: Literal["accepted_at_intent_base"] = "accepted_at_intent_base"

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        if not _CANONICAL_NAME_RE.fullmatch(value):
            raise ValueError("authoring artifact-reference role is not canonical")
        return value


class ProcedureAuthoringPayloadV1(_StrictAuthoringModel):
    tag: Literal["playbill-procedure-authoring-payload-v1"] = (
        "playbill-procedure-authoring-payload-v1"
    )
    definition: dict[str, object]
    authority: ArtifactAuthority
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    retire: bool = False

    @field_validator("definition", mode="before")
    @classmethod
    def _definition(cls, value: object) -> dict[str, object]:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):
            raise ValueError("Procedure authoring definition must be a canonical object")
        if "name" not in normalized:
            raise ValueError("Procedure authoring definition requires a semantic name")
        return cast(dict[str, object], normalized)


AuthoringPayloadV1 = Annotated[
    ClaimAuthoringPayloadV1 | ProcedureAuthoringPayloadV1,
    Field(discriminator="tag"),
]


def authoring_payload_digest(payload: AuthoringPayloadV1) -> str:
    preimage = payload.model_dump(mode="json")
    preimage.pop("tag")
    return typed_digest(
        Sha256Value,
        AUTHORING_PAYLOAD_DIGEST_DOMAIN,
        preimage,
    ).tagged


def authoring_create_fingerprint(
    *,
    instance_id: str,
    actor_id: str,
    payload: AuthoringPayloadV1,
) -> str:
    return typed_digest(
        Sha256Value,
        AUTHORING_CREATE_FINGERPRINT_DOMAIN,
        {
            "instance_id": instance_id,
            "actor_id": actor_id,
            "payload": payload.model_dump(mode="json"),
        },
    ).tagged


class RepairAlternativeV1(_StrictAuthoringModel):
    kind: str
    description: str
    replacement: object | None = None

    @field_validator("replacement", mode="before")
    @classmethod
    def _replacement(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @model_validator(mode="after")
    def _bounded(self) -> "RepairAlternativeV1":
        if len(canonical_bytes(self.model_dump(mode="json"))) > MAX_REPAIR_BYTES:
            raise ValueError("authoring repair exceeds the frozen repair-byte limit")
        return self


class AuthoringDiagnosticV1(_StrictAuthoringModel):
    code: str
    stage: str
    offending_element: str
    message: str
    owner: DiagnosticOwner
    disposition: DiagnosticDisposition
    repairs: tuple[RepairAlternativeV1, ...] = ()

    @field_validator("repairs")
    @classmethod
    def _repairs(
        cls,
        value: tuple[RepairAlternativeV1, ...],
    ) -> tuple[RepairAlternativeV1, ...]:
        if len(value) > MAX_REPAIR_ALTERNATIVES:
            raise ValueError("authoring diagnostic exceeds the repair-alternative limit")
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("authoring repairs must be canonically sorted and unique")
        return value

    @model_validator(mode="after")
    def _writer_has_repair(self) -> "AuthoringDiagnosticV1":
        if self.owner == "writer" and self.disposition == "edit_and_retry" and not self.repairs:
            raise ValueError("writer-repairable diagnostic must carry its repair")
        return self


class BlockedCheckV1(_StrictAuthoringModel):
    check: str
    blocked_by: tuple[str, ...]
    reason: str

    @field_validator("blocked_by")
    @classmethod
    def _blocked_by(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value), key=lambda item: item.encode())):
            raise ValueError("blocked check dependencies must be nonempty, sorted, and unique")
        return value


class DiagnosticFrontierLimitsV1(_StrictAuthoringModel):
    max_diagnostics: Literal[128] = 128
    max_blocked_checks: Literal[128] = 128
    max_repair_alternatives: Literal[4] = 4
    max_repair_bytes: Literal[16384] = 16384
    max_frontier_bytes: Literal[1048576] = 1048576


class DiagnosticFrontierV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-diagnostic-frontier-v1"] = (
        "playbill-authoring-diagnostic-frontier-v1"
    )
    diagnostics: tuple[AuthoringDiagnosticV1, ...] = ()
    blocked_checks: tuple[BlockedCheckV1, ...] = ()
    frontier_complete: bool = True

    @model_validator(mode="after")
    def _bounded(self) -> "DiagnosticFrontierV1":
        if len(self.diagnostics) > MAX_DIAGNOSTICS:
            raise ValueError("authoring frontier exceeds the diagnostic limit")
        if len(self.blocked_checks) > MAX_BLOCKED_CHECKS:
            raise ValueError("authoring frontier exceeds the blocked-check limit")
        diagnostic_keys = tuple(
            (item.stage.encode(), item.code.encode(), item.offending_element.encode())
            for item in self.diagnostics
        )
        if diagnostic_keys != tuple(sorted(set(diagnostic_keys))):
            raise ValueError("authoring diagnostics must be canonically sorted and unique")
        blocked_keys = tuple(item.check.encode() for item in self.blocked_checks)
        if blocked_keys != tuple(sorted(set(blocked_keys))):
            raise ValueError("authoring blocked checks must be canonically sorted and unique")
        if len(canonical_bytes(self.model_dump(mode="json"))) > MAX_FRONTIER_BYTES:
            raise ValueError("authoring frontier exceeds its frozen byte limit")
        return self

    @property
    def digest(self) -> str:
        preimage = self.model_dump(mode="json")
        preimage.pop("tag")
        return typed_digest(
            Sha256Value,
            AUTHORING_FRONTIER_DIGEST_DOMAIN,
            preimage,
        ).tagged


class AcceptanceConditionV1(_StrictAuthoringModel):
    condition: str
    owner: DiagnosticOwner
    action: str
    satisfied: bool


class CandidateStatusV1(_StrictAuthoringModel):
    tag: Literal["playbill-candidate-status-v1"] = "playbill-candidate-status-v1"
    state: CandidateStatusState
    proposal_id: str | None = None
    candidate_digest: str | None = None
    current_accepted_coordinate: AcceptedCoordinate
    path_to_acceptance: tuple[AcceptanceConditionV1, ...] = ()
    accepted_generation: AcceptedCoordinate | None = None

    @field_validator("proposal_id", "candidate_digest")
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value, label="CandidateStatus digest")

    @model_validator(mode="after")
    def _accepted_shape(self) -> "CandidateStatusV1":
        if (self.state == "accepted") != (self.accepted_generation is not None):
            raise ValueError("accepted CandidateStatus alone carries an accepted generation")
        return self


class PreflightCertificateV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-preflight-certificate-v1"] = (
        "playbill-authoring-preflight-certificate-v1"
    )
    instance_id: str
    intent_id: str
    intent_revision: int = Field(ge=0)
    actor: AuthenticatedActor
    payload_digest: str
    resolved_authoring_digest: str
    accepted_coordinate: AcceptedCoordinate
    compiler_coordinate: CompilerCoordinate
    instance_descriptor_digest: str
    receive_limits: ProposalReceiveLimits
    canonical_timestamp: str
    proposal_ref: str
    proposal_ref_oid: str | None
    candidate_tree_digest: str
    frontier_digest: str
    frontier_limits: DiagnosticFrontierLimitsV1 = DiagnosticFrontierLimitsV1()
    certificate_digest: str

    @field_validator(
        "payload_digest",
        "resolved_authoring_digest",
        "instance_descriptor_digest",
        "candidate_tree_digest",
        "frontier_digest",
        "certificate_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="preflight certificate digest")

    @field_validator("intent_id")
    @classmethod
    def _intent_id(cls, value: str) -> str:
        if not AUTHORING_INTENT_ID_RE.fullmatch(value):
            raise ValueError("preflight intent ID is malformed")
        return value

    @field_validator("proposal_ref_oid")
    @classmethod
    def _proposal_oid(cls, value: str | None) -> str | None:
        if value is not None and not _GIT_OID_RE.fullmatch(value):
            raise ValueError("preflight proposal-ref OID is malformed")
        return value

    @model_validator(mode="after")
    def _reproduces(self) -> "PreflightCertificateV1":
        if self.certificate_digest != preflight_certificate_digest(self):
            raise ValueError("preflight certificate digest does not reproduce")
        return self


def preflight_certificate_digest(certificate: PreflightCertificateV1) -> str:
    payload = certificate.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("certificate_digest")
    return typed_digest(
        Sha256Value,
        AUTHORING_PREFLIGHT_CERTIFICATE_DIGEST_DOMAIN,
        payload,
    ).tagged


class PreflightResultV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-preflight-result-v1"] = (
        "playbill-authoring-preflight-result-v1"
    )
    verdict: Literal["passed", "refused"]
    certificate: PreflightCertificateV1
    frontier: DiagnosticFrontierV1

    @model_validator(mode="after")
    def _verdict(self) -> "PreflightResultV1":
        passed = (
            self.frontier.frontier_complete
            and not self.frontier.diagnostics
            and not self.frontier.blocked_checks
        )
        if (self.verdict == "passed") != passed:
            raise ValueError("preflight verdict disagrees with its complete frontier")
        if self.certificate.frontier_digest != self.frontier.digest:
            raise ValueError("preflight certificate names another diagnostic frontier")
        return self


class AuthoringIntentV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-v1"] = "playbill-authoring-intent-v1"
    intent_id: str
    instance_id: str
    actor_id: str
    canonical_timestamp: str
    semantic_identity: str
    payload: AuthoringPayloadV1
    payload_digest: str
    create_fingerprint: str
    intent_revision: int = Field(default=0, ge=0)
    last_preflight: PreflightResultV1 | None = None
    candidate_status: CandidateStatusV1

    @field_validator("intent_id")
    @classmethod
    def _intent_id(cls, value: str) -> str:
        if not AUTHORING_INTENT_ID_RE.fullmatch(value):
            raise ValueError("AuthoringIntent ID must be AIT- plus 128-bit lowercase hex")
        return value

    @field_validator("payload_digest", "create_fingerprint")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="AuthoringIntent digest")

    @field_validator("canonical_timestamp")
    @classmethod
    def _canonical_time(cls, value: str) -> str:
        return validate_candidate_timestamp(value)

    @model_validator(mode="after")
    def _binding(self) -> "AuthoringIntentV1":
        if self.payload_digest != authoring_payload_digest(self.payload):
            raise ValueError("AuthoringIntent payload digest does not reproduce")
        expected_fingerprint = authoring_create_fingerprint(
            instance_id=self.instance_id,
            actor_id=self.actor_id,
            payload=self.payload,
        )
        if self.create_fingerprint != expected_fingerprint:
            raise ValueError("AuthoringIntent create fingerprint does not reproduce")
        if isinstance(self.payload, ClaimAuthoringPayloadV1):
            claim_path(self.semantic_identity)
        elif self.semantic_identity != f"Procedure:{self.payload.definition['name']}":
            raise ValueError("Procedure AuthoringIntent identity differs from its definition")
        return self


class AuthoringIntentViewV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-view-v1"] = "playbill-authoring-intent-view-v1"
    intent: AuthoringIntentV1


class AuthoringIntentListV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-list-v1"] = "playbill-authoring-intent-list-v1"
    intents: tuple[AuthoringIntentV1, ...]


class AuthoringIntentCreateRequestV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-create-request-v1"] = (
        "playbill-authoring-intent-create-request-v1"
    )
    payload: AuthoringPayloadV1


class AuthoringIntentCompileRequestV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-compile-request-v1"] = (
        "playbill-authoring-intent-compile-request-v1"
    )
    payload: AuthoringPayloadV1
    intent_id: str | None = None


class AuthoringIntentPreflightRequestV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-preflight-request-v1"] = (
        "playbill-authoring-intent-preflight-request-v1"
    )


class AuthoringIntentSubmitRequestV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-submit-request-v1"] = (
        "playbill-authoring-intent-submit-request-v1"
    )


class AuthoringSubmitResultV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-submit-result-v1"] = "playbill-authoring-submit-result-v1"
    intent: AuthoringIntentV1
    status: CandidateStatusV1


__all__ = [
    "AUTHORING_CANDIDATE_TREE_DIGEST_DOMAIN",
    "AUTHORING_CREATE_FINGERPRINT_DOMAIN",
    "AUTHORING_FRONTIER_DIGEST_DOMAIN",
    "AUTHORING_INSTANCE_DESCRIPTOR_DIGEST_DOMAIN",
    "AUTHORING_INTENT_ID_RE",
    "AUTHORING_PAYLOAD_DIGEST_DOMAIN",
    "AUTHORING_PREFLIGHT_CERTIFICATE_DIGEST_DOMAIN",
    "AUTHORING_RESOLVED_DIGEST_DOMAIN",
    "AcceptanceConditionV1",
    "AuthoringArtifactReferenceV1",
    "AuthoringClaimStatementV1",
    "AuthoringDiagnosticV1",
    "AuthoringExactContentObjectV1",
    "AuthoringIntentCompileRequestV1",
    "AuthoringIntentCreateRequestV1",
    "AuthoringIntentListV1",
    "AuthoringIntentPreflightRequestV1",
    "AuthoringIntentSubmitRequestV1",
    "AuthoringIntentV1",
    "AuthoringIntentViewV1",
    "AuthoringPayloadV1",
    "AuthoringSubmitResultV1",
    "BlockedCheckV1",
    "CandidateStatusState",
    "CandidateStatusV1",
    "ClaimAuthoringPayloadV1",
    "DiagnosticFrontierLimitsV1",
    "DiagnosticFrontierV1",
    "PreflightCertificateV1",
    "PreflightResultV1",
    "ProcedureAuthoringPayloadV1",
    "RepairAlternativeV1",
    "SelfSourceBodyV1",
    "WorkingAnchorWindowV1",
    "WorkingDigestCoordinateV1",
    "WorkingGitBlobCoordinateV1",
    "WorkingSelectionObservationV1",
    "authoring_create_fingerprint",
    "authoring_payload_digest",
    "preflight_certificate_digest",
]
