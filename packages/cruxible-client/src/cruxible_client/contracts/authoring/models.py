"""Frozen PC-G1b authoring wires and deterministic digest preimages."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from cruxible_client.contracts.accepted_attestations import attestation_identity
from cruxible_client.contracts.acquisition_policies import SourceAcquisitionPolicy
from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_IDENTITY,
    ApprovalPolicy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.candidates import validate_candidate_timestamp
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import CaptureContract
from cruxible_client.contracts.change_control import StateCoordinate
from cruxible_client.contracts.claim_attestations import ClaimAttestation
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import (
    ClaimRetireDependent,
    ClaimRetirementReason,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_path,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBacking,
    ProjectionBlockStampAny,
)
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_IDENTITY,
    ProcedureRuntimePolicy,
)
from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContract
from cruxible_client.contracts.procedures.models import ProcedureHardCaps
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.proposal_models import (
    CHANGE_SET_RATIONALE_MAX_LENGTH,
    AuthenticatedActor,
    ProposalReceiveLimits,
    validate_change_set_rationale,
)
from cruxible_client.contracts.query.definitions import QueryDefinition, QueryDefinitionSpec
from cruxible_client.contracts.repairs import ServedRepair, served_repair_for_refusal
from cruxible_client.contracts.resolution_contracts import ResolutionContract
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_client.contracts.triggers import InternalActionName, TriggerSchedule
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    WorkspaceAdvertisement,
)

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
AUTHORING_REFERENCE_EXPECTATIONS_DIGEST_DOMAIN = "playbill-authoring-reference-expectations-v1"
AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN = "playbill-authoring-change-set-membership-v1"
AUTHORING_CLAIM_MEMBER_IDENTITY_DIGEST_DOMAIN = "playbill-authoring-claim-member-identity-v1"
AUTHORING_PROGRAM_DIGEST_DOMAIN = "playbill-sdk-authoring-program-v1"
AUTHORING_PROGRAM_STAMP_OPERATION_DOMAIN = "playbill-authoring-program-stamp-operation-v1"
# Before this lineage's first public release, a version's digest may be re-pinned
# only with its audited snapshot, SDK handshake, and digest guardrail in the same
# commit. After first public release, every contract change must succeed the version.
AUTHORING_SDK_VERSION = "0.5.0"
AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST = (
    "sha256:bfbac94baca8f3c7b57ce1af749b24710f15a7030c60dbfb826ca95e70a9df93"
)

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
AuthoringReferenceKind: TypeAlias = Literal[
    "Subject",
    "ClaimType",
    "Claim",
    "Procedure",
    "QueryDefinition",
    "Source",
]


class _StrictAuthoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthoringReferenceExpectation(_StrictAuthoringModel):
    """One coordinate assertion emitted by an SDK ``TypedRef``."""

    tag: Literal["playbill-authoring-reference-expectation-v1"] = (
        "playbill-authoring-reference-expectation-v1"
    )
    payload_path: str
    artifact_kind: AuthoringReferenceKind
    address: str
    minted_coordinate: AcceptedCoordinate

    @field_validator("payload_path")
    @classmethod
    def _payload_path(cls, value: str) -> str:
        if not value or value != value.strip() or any(char.isspace() for char in value):
            raise ValueError("reference expectation payload_path must be canonical")
        return value

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("reference expectation address must be canonical")
        return value


class AuthoringSlotExpectation(_StrictAuthoringModel):
    """The exact live membership of one slot a change set depends on.

    A typed write chooses what to revise, retire and disposition from the live
    Claims of a slot (one Subject, predicate and qualifier). Admission checks
    this membership at the head the candidate is evaluated at, and refuses when
    a Claim joined or left the slot since ``minted_coordinate``: an evaluated
    candidate is only ever settled at that same head, so a candidate that
    passed can never activate over a slot it did not see.
    """

    tag: Literal["playbill-authoring-slot-expectation-v1"] = (
        "playbill-authoring-slot-expectation-v1"
    )
    payload_path: str
    artifact_kind: Literal["Slot"] = "Slot"
    subject_path: str
    predicate: str
    qualifier: str | None = None
    live_claims: tuple[str, ...]
    minted_coordinate: AcceptedCoordinate

    @field_validator("payload_path", "subject_path", "predicate")
    @classmethod
    def _canonical(cls, value: str) -> str:
        if not value or value != value.strip() or any(char.isspace() for char in value):
            raise ValueError("slot expectation names must be canonical")
        return value

    @field_validator("live_claims")
    @classmethod
    def _live_claims(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("slot expectation live_claims must be sorted and unique")
        return value

    @property
    def address(self) -> str:
        """The slot, spelled for ordering beside reference expectations."""

        qualifier = "" if self.qualifier is None else f"@{self.qualifier}"
        return f"{self.subject_path}#{self.predicate}{qualifier}"


AuthoringExpectation: TypeAlias = Annotated[
    AuthoringReferenceExpectation | AuthoringSlotExpectation,
    Field(discriminator="tag"),
]


class AuthoringReferenceSuccessor(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-reference-successor-v1"] = (
        "playbill-authoring-reference-successor-v1"
    )
    payload_path: str
    artifact_kind: AuthoringReferenceKind
    address: str
    coordinate: AcceptedCoordinate


class AuthoringProgramOperation(_StrictAuthoringModel):
    operation: str
    decisions: dict[str, object]

    @field_validator("operation")
    @classmethod
    def _operation(cls, value: str) -> str:
        if not _CANONICAL_NAME_RE.fullmatch(value):
            raise ValueError("program operation must be a canonical name")
        return value

    @field_validator("decisions", mode="before")
    @classmethod
    def _decisions(cls, value: object) -> dict[str, object]:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):
            raise ValueError("program operation decisions must be a canonical object")
        return cast(dict[str, object], normalized)


class AuthoringProgramStamp(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-program-stamp-v1"] = "playbill-authoring-program-stamp-v1"
    program_digest: str
    sdk_version: str
    sdk_contract_snapshot_digest: str

    @field_validator("program_digest", "sdk_contract_snapshot_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _sha256(value, label="authoring program-stamp digest")

    @field_validator("sdk_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not value or value != value.strip() or any(char.isspace() for char in value):
            raise ValueError("authoring program-stamp version must be canonical")
        return value


def authoring_program_digest(
    *,
    sdk_contract_snapshot_digest: str,
    operations: tuple[AuthoringProgramOperation, ...],
) -> str:
    _sha256(sdk_contract_snapshot_digest, label="SDK contract-snapshot digest")
    return typed_digest(
        Sha256Value,
        AUTHORING_PROGRAM_DIGEST_DOMAIN,
        {
            "sdk_contract_snapshot_digest": sdk_contract_snapshot_digest,
            "operations": [item.model_dump(mode="json") for item in operations],
        },
    ).tagged


def authoring_program_stamp_operation_key(
    *,
    intent_id: str,
    intent_revision: int,
    program_stamp: AuthoringProgramStamp,
) -> str:
    return typed_digest(
        Sha256Value,
        AUTHORING_PROGRAM_STAMP_OPERATION_DOMAIN,
        {
            "intent_id": intent_id,
            "intent_revision": intent_revision,
            "program_stamp": program_stamp.model_dump(mode="json"),
        },
    ).tagged


def canonical_reference_expectations(
    values: tuple[AuthoringExpectation, ...],
) -> tuple[AuthoringExpectation, ...]:
    keys = tuple(
        (
            item.payload_path.encode("utf-8"),
            item.artifact_kind.encode("ascii"),
            item.address.encode("utf-8"),
        )
        for item in values
    )
    if keys != tuple(sorted(set(keys))):
        raise ValueError("reference expectations must be canonically sorted and unique")
    paths = tuple(item.payload_path for item in values)
    if len(paths) != len(set(paths)):
        raise ValueError("reference expectation payload paths must be unique")
    return values


def reference_expectations_digest(
    values: tuple[AuthoringExpectation, ...],
) -> str:
    canonical_reference_expectations(values)
    return typed_digest(
        Sha256Value,
        AUTHORING_REFERENCE_EXPECTATIONS_DIGEST_DOMAIN,
        {"reference_expectations": [item.model_dump(mode="json") for item in values]},
    ).tagged


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


class AuthoringExactContentObject(_StrictAuthoringModel):
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


AuthoringClaimObject = Annotated[
    LiteralClaimObject | SubjectClaimObject | AuthoringExactContentObject,
    Field(discriminator="kind"),
]


class AuthoringClaimStatement(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-claim-statement-v1"] = "playbill-authoring-claim-statement-v1"
    subject: SemanticAddress
    predicate: str
    qualifier: str | None = None
    object: AuthoringClaimObject
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
    def _interval(self) -> "AuthoringClaimStatement":
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("Claim effective interval must be increasing")
        return self


class AuthoringExistingClaimDisposition(_StrictAuthoringModel):
    claim_id: str
    disposition: Literal["not_tested", "support", "contradict", "unsure"]

    @field_validator("claim_id")
    @classmethod
    def _claim_id(cls, value: str) -> str:
        claim_path(value)
        return value


class WorkingGitBlobCoordinate(_StrictAuthoringModel):
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


class WorkingDigestCoordinate(_StrictAuthoringModel):
    kind: Literal["observed_digest"] = "observed_digest"
    source_content_digest: str
    source_byte_length: int = Field(ge=0)

    @field_validator("source_content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="working source content digest")


WorkingSelectionCoordinate = Annotated[
    WorkingGitBlobCoordinate | WorkingDigestCoordinate,
    Field(discriminator="kind"),
]


class WorkingAnchorWindow(_StrictAuthoringModel):
    tag: Literal["playbill-working-anchor-window-v1"] = "playbill-working-anchor-window-v1"
    anchor: str
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=1)
    observed_occurrence_count: int = Field(ge=0)
    selected_occurrence: int | None = Field(default=None, ge=1)

    @field_validator("anchor")
    @classmethod
    def _anchor(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("working selection anchor must be nonblank and normalized")
        return value

    @model_validator(mode="after")
    def _window(self) -> "WorkingAnchorWindow":
        if self.end_byte <= self.start_byte:
            raise ValueError("working selection window must cover at least one byte")
        if (
            self.selected_occurrence is not None
            and self.selected_occurrence > self.observed_occurrence_count
        ):
            raise ValueError("selected occurrence exceeds the observed occurrence count")
        return self


class WorkingSelectionObservation(_StrictAuthoringModel):
    tag: Literal["playbill-working-selection-observation-v1"] = (
        "playbill-working-selection-observation-v1"
    )
    source_id: str
    coordinate: WorkingSelectionCoordinate
    selected_content_base64: str
    selected_bytes_digest: str
    selector: WorkingAnchorWindow
    # The whole observed source, present when it declares a projection block.
    # A citation into such a page has to be proved outside every block window
    # by the daemon, which holds only the selected bytes; the page is the
    # manifest of its own windows, so the client hands it over. Optional and
    # additive: a page with no stamped block sends nothing, and an intent
    # stored before the field existed reads back unchanged.
    source_content_base64: str | None = None

    @model_serializer(mode="wrap")
    def _preserve_source_content_presence(self, handler: Any) -> dict[str, object]:
        payload = cast(dict[str, object], handler(self))
        # Historical payloads predate this field. Adding a default null changes
        # their payload, fingerprint and journal-event digest preimages. An
        # explicit null may itself have been committed by a newer writer, so
        # preserve presence as well as value rather than excluding every null.
        if "source_content_base64" not in self.model_fields_set:
            payload.pop("source_content_base64", None)
        return payload

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

    @field_validator("source_content_base64")
    @classmethod
    def _source_content(cls, value: str | None) -> str | None:
        if value is not None:
            _canonical_base64(value, label="working source content")
        return value

    @property
    def source_content(self) -> bytes | None:
        """The whole observed source when the observation carries it."""

        if self.source_content_base64 is None:
            return None
        return _canonical_base64(self.source_content_base64, label="working source content")

    @field_validator("selected_bytes_digest")
    @classmethod
    def _selected_digest(cls, value: str) -> str:
        return _sha256(value, label="working selected-bytes digest")

    @model_validator(mode="after")
    def _internal_correspondence(self) -> "WorkingSelectionObservation":
        selected = self.selected_content
        if self.selector.end_byte > self.coordinate.source_byte_length:
            raise ValueError("working selection exceeds the observed whole-source length")
        if len(selected) != self.selector.end_byte - self.selector.start_byte:
            raise ValueError("working selected bytes differ from the declared window length")
        digest = "sha256:" + hashlib.sha256(selected).hexdigest()
        if digest != self.selected_bytes_digest:
            raise ValueError("working selected-bytes digest does not reproduce")
        whole = self.source_content
        if whole is not None:
            if len(whole) != self.coordinate.source_byte_length:
                raise ValueError("working source content length differs from its coordinate")
            if isinstance(self.coordinate, WorkingDigestCoordinate) and (
                "sha256:" + hashlib.sha256(whole).hexdigest()
                != self.coordinate.source_content_digest
            ):
                raise ValueError("working source content digest differs from its coordinate")
            if whole[self.selector.start_byte : self.selector.end_byte] != selected:
                raise ValueError("working selected bytes are not at their window in the source")
        return self

    @property
    def selected_content(self) -> bytes:
        return _canonical_base64(
            self.selected_content_base64,
            label="working selected content",
        )


class SelfSourceBody(_StrictAuthoringModel):
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


class ExistingCaptureCitationSource(_StrictAuthoringModel):
    """Reference one already-materialized Capture without re-authoring its bytes."""

    tag: Literal["playbill-existing-capture-citation-source-v1"] = (
        "playbill-existing-capture-citation-source-v1"
    )
    capture_digest: str

    @field_validator("capture_digest")
    @classmethod
    def _capture_digest(cls, value: str) -> str:
        return _sha256(value, label="existing Capture digest")


ClaimAuthoringSourceV1 = Annotated[
    WorkingSelectionObservation | SelfSourceBody,
    Field(discriminator="tag"),
]

ClaimAuthoringSource = Annotated[
    WorkingSelectionObservation | SelfSourceBody | ExistingCaptureCitationSource,
    Field(discriminator="tag"),
]


class ClaimAuthoringPayloadV1(_StrictAuthoringModel):
    tag: Literal["playbill-claim-authoring-payload-v1"] = "playbill-claim-authoring-payload-v1"
    statement: AuthoringClaimStatement
    rationale: str
    source: ClaimAuthoringSourceV1
    citation_role: Literal["evidence", "copy"] | None = None
    revises: str | None = None
    existing_claim_dispositions: tuple[AuthoringExistingClaimDisposition, ...] = ()

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Claim authoring rationale must not be empty")
        return value

    @field_validator("revises")
    @classmethod
    def _revises(cls, value: str | None) -> str | None:
        if value is not None:
            claim_path(value)
        return value

    @field_validator("existing_claim_dispositions")
    @classmethod
    def _dispositions(
        cls,
        value: tuple[AuthoringExistingClaimDisposition, ...],
    ) -> tuple[AuthoringExistingClaimDisposition, ...]:
        ids = tuple(item.claim_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("ascii"))):
            raise ValueError("existing Claim dispositions must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _source_role(self) -> "ClaimAuthoringPayloadV1":
        if isinstance(
            self.source,
            WorkingSelectionObservation | ExistingCaptureCitationSource,
        ):
            if self.citation_role is None:
                raise ValueError("Flow A and existing Captures require an explicit citation_role")
        elif self.citation_role is not None:
            raise ValueError("Flow B self-source fixes its citation role server-side")
        return self


class ClaimDependencyDrafts(_StrictAuthoringModel):
    tag: Literal["playbill-claim-dependency-drafts-v1"] = "playbill-claim-dependency-drafts-v1"
    subject: SubjectShell | None = None
    claim_type: ClaimType | None = None


class ClaimAuthoringPayloadV2(ClaimAuthoringPayloadV1):
    tag: Literal["playbill-claim-authoring-payload-v2"] = "playbill-claim-authoring-payload-v2"  # type: ignore[assignment]
    dependency_drafts: ClaimDependencyDrafts


class ClaimDerivationBinding(_StrictAuthoringModel):
    """Backend-bound reducer and exact admitted inputs, never author-supplied hashes."""

    procedure: ArtifactPin
    inputs: tuple[ArtifactPin, ...]

    @model_validator(mode="after")
    def _kinds(self) -> "ClaimDerivationBinding":
        if self.procedure.target.kind != "Procedure" or not self.inputs:
            raise ValueError("a derivation needs its Procedure and nonempty Claim inputs")
        if any(pin.target.kind != "Claim" for pin in self.inputs):
            raise ValueError("derivation inputs must name Claims")
        if len({p.target.qualified for p in self.inputs}) != len(self.inputs):
            raise ValueError("derivation inputs must be unique by identity")
        return self


class ClaimAuthoringPayload(ClaimAuthoringPayloadV1):
    tag: Literal["playbill-claim-authoring-payload-v3"] = "playbill-claim-authoring-payload-v3"  # type: ignore[assignment]
    source: ClaimAuthoringSource  # type: ignore[assignment]
    dependency_drafts: ClaimDependencyDrafts
    derivation: ClaimDerivationBinding | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class AuthoringArtifactReference(_StrictAuthoringModel):
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


class AuthoringCandidateReference(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-candidate-reference-v1"] = (
        "playbill-authoring-candidate-reference-v1"
    )
    role: str
    target: ArtifactIdentity
    resolution: Literal["candidate_in_change_set"] = "candidate_in_change_set"


class ResolutionContractAuthoringPayload(_StrictAuthoringModel):
    tag: Literal["playbill-resolution-contract-authoring-payload-v1"] = (
        "playbill-resolution-contract-authoring-payload-v1"
    )
    resolution_contract: ResolutionContract


class AttestationAuthoringPayload(_StrictAuthoringModel):
    """One immutable signed statement proposed through ordinary acceptance."""

    tag: Literal["cruxible-attestation-authoring-payload-v1"] = (
        "cruxible-attestation-authoring-payload-v1"
    )
    attestation: ClaimAttestation


class SubjectAuthoringPayload(_StrictAuthoringModel):
    tag: Literal["playbill-subject-authoring-payload-v1"] = "playbill-subject-authoring-payload-v1"
    subject: SubjectShell


class QueryDefinitionAuthoringPayload(_StrictAuthoringModel):
    tag: Literal["playbill-query-definition-authoring-payload-v1"] = (
        "playbill-query-definition-authoring-payload-v1"
    )
    query_definition: QueryDefinitionSpec | QueryDefinition


class ApprovalPolicyAuthoringPayload(_StrictAuthoringModel):
    tag: Literal["playbill-approval-policy-authoring-payload-v1"] = (
        "playbill-approval-policy-authoring-payload-v1"
    )
    approval_policy: ApprovalPolicy


class ProcedureRuntimePolicyAuthoringPayload(_StrictAuthoringModel):
    tag: Literal["playbill-procedure-runtime-policy-authoring-payload-v1"] = (
        "playbill-procedure-runtime-policy-authoring-payload-v1"
    )
    procedure_runtime_policy: ProcedureRuntimePolicy


class MandateScopeAuthoring(_StrictAuthoringModel):
    """One ClaimType a settle grant covers, named by predicate; lowering pins its digest."""

    tag: Literal["playbill-mandate-scope-authoring-v1"] = "playbill-mandate-scope-authoring-v1"
    claim_type: str
    change_kinds: tuple[Literal["create", "revise", "retire"], ...] = Field(min_length=1)
    binding_subject_role: Literal["subject", "object"] = "subject"


class MandateConditionAuthoring(_StrictAuthoringModel):
    """The settle predicate, named by query; lowering pins the exact accepted query."""

    tag: Literal["playbill-mandate-condition-authoring-v1"] = (
        "playbill-mandate-condition-authoring-v1"
    )
    query_name: str
    binding_parameter: str
    fixed_parameters: dict[str, object] = Field(default_factory=dict)
    required_fields: tuple[str, ...] = Field(min_length=1)
    fallback: Literal["refuse", "propose"]


class ProcedureMandateAuthoringPayload(_StrictAuthoringModel):
    """Decision-only grant input; lowering owns every exact digest it pins."""

    tag: Literal["playbill-procedure-mandate-authoring-payload-v1"] = (
        "playbill-procedure-mandate-authoring-payload-v1"
    )
    name: str
    procedure_name: str
    grants: Literal["propose", "settle"]
    resource_ceiling: ProcedureHardCaps
    namespace: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    scope: tuple[MandateScopeAuthoring, ...] = ()
    subject_scope: tuple[SemanticAddress, ...] | None = None
    condition: MandateConditionAuthoring | None = None
    suspended: bool = False
    retire: bool = False


class CaptureContractAuthoringPayload(_StrictAuthoringModel):
    """One whole CaptureContract authored as a change-set definition member."""

    tag: Literal["playbill-capture-contract-authoring-payload-v1"] = (
        "playbill-capture-contract-authoring-payload-v1"
    )
    capture_contract: CaptureContract


class SourceAcquisitionPolicyAuthoringPayload(_StrictAuthoringModel):
    """One whole SourceAcquisitionPolicy authored as a change-set definition member."""

    tag: Literal["playbill-source-acquisition-policy-authoring-payload-v1"] = (
        "playbill-source-acquisition-policy-authoring-payload-v1"
    )
    acquisition_policy: SourceAcquisitionPolicy


class LineAuthoringPayload(_StrictAuthoringModel):
    """Decision-only Line input; lowering owns the exact Procedure and policy pins.

    An author names the accepted or same-set Procedure and acquisition policy
    by name, the way a ProcedureMandate member names its Procedure, and
    lowering resolves both into the exact digest pins the LineSpec carries. A
    budget left unset lowers to the Procedure's own hard caps. The acquisition
    policy is required only when the Procedure acquires (has Source or exhaust
    nodes); a pure-compute Line pins none. ``parameters`` is the Procedure's
    input record, checked against its input Contract when the Line lowers.
    When the Line runs is not its own: Triggers aim at it. ``trigger_input``
    binds the triggering Capture to one Source input, and lowering declares the
    exact event that input accepts from its CaptureContract.
    """

    tag: Literal["playbill-line-authoring-payload-v1"] = "playbill-line-authoring-payload-v1"
    name: str
    procedure_name: str
    acquisition_policy_name: str | None = None
    # Caps this Line below its Procedure's capability; omitted, it is that capability.
    max_authority: Literal["observe", "propose", "settle"] | None = None
    trigger_input: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    parameters: object = Field(default_factory=dict)
    budgets: dict[str, int] | None = None
    epsilon: object = Field(default_factory=lambda: {"$decimal": "0.1"})
    occurrence_epoch: int = Field(default=1, ge=1)
    retire: bool = False

    @field_validator("name", "procedure_name", "acquisition_policy_name")
    @classmethod
    def _names(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("Line authoring names must be nonblank and normalized")
        return value

    @field_validator("parameters", "epsilon", mode="before")
    @classmethod
    def _canonical(cls, value: object) -> object:
        return normalize_canonical(value)


class TriggerAuthoringPayload(_StrictAuthoringModel):
    """Decision-only Trigger input: a schedule and exactly one target.

    ``line_name`` names an accepted or same-set Line, which lowering refers to by
    identity; ``action`` names an internal action instead.
    """

    tag: Literal["playbill-trigger-authoring-payload-v1"] = "playbill-trigger-authoring-payload-v1"
    name: str
    schedule: TriggerSchedule
    line_name: str | None = None
    action: InternalActionName | None = None
    retire: bool = False

    @field_validator("name", "line_name")
    @classmethod
    def _names(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("Trigger authoring names must be nonblank and normalized")
        return value

    @model_validator(mode="after")
    def _one_target(self) -> "TriggerAuthoringPayload":
        if (self.line_name is None) == (self.action is None):
            raise ValueError("a Trigger names exactly one target: line_name or action")
        return self


class ProcedureAuthoringPayloadV1(_StrictAuthoringModel):
    tag: Literal["playbill-procedure-authoring-payload-v1"] = (
        "playbill-procedure-authoring-payload-v1"
    )
    definition: dict[str, object]
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


class ProcedureAuthoringPayload(_StrictAuthoringModel):
    """A Procedure envelope input, plus the acquisition policy that envelope pins.

    `acquisition_policy` is the SEMANTIC NAME of an accepted (or same-change-set
    candidate) `SourceAcquisitionPolicy`, never a digest: lowering resolves it
    the way every other Procedure reference resolves, and declares the resolved
    exact pin under role `acquisition-policy` on the artifact envelope. The
    definition never mentions it, so the definition digest is untouched -- this
    is a Procedure-level binding the way a Line's policy pin is a Line-level
    one, and the closure evaluator holds it to the same standard as any other
    non-deferred pin.
    """

    tag: Literal["playbill-procedure-authoring-payload-v2"] = (
        "playbill-procedure-authoring-payload-v2"
    )
    definition: dict[str, object]
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    owned_contracts: tuple[ProcedureOwnedContract, ...]
    acquisition_policy: str | None = None
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

    @field_validator("acquisition_policy")
    @classmethod
    def _acquisition_policy(cls, value: str | None) -> str | None:
        if value is not None and not _CANONICAL_NAME_RE.fullmatch(value):
            raise ValueError("acquisition policy name is not canonical")
        return value


class ClaimTypeAuthoringPayload(_StrictAuthoringModel):
    """One whole ClaimType definition authored inside an ordinary change set.

    A succession -- a ClaimType that names a predecessor, and the migration its
    whole reverse-pin closure then owes -- is `ClaimTypeSuccessionMember`, a
    member of its own, because the closure is the decision. This member defines
    a ClaimType nothing yet depends on.
    """

    tag: Literal["playbill-claim-type-authoring-payload-v1"] = (
        "playbill-claim-type-authoring-payload-v1"
    )
    claim_type: ClaimType

    @field_validator("claim_type")
    @classmethod
    def _claim_type(cls, value: ClaimType) -> ClaimType:
        if value.lifecycle.state != "live" or value.lifecycle.predecessor_digest is not None:
            raise ValueError(
                "a ClaimType definition member cannot carry a succession; "
                "author it as a claim_type_succession member"
            )
        return value


ClaimTypeSuccessionDisposition: TypeAlias = Literal[
    "successor",
    "retire",
    "invalidation",
    "re_author",
]


class ClaimTypeSuccessionDependent(_StrictAuthoringModel):
    """What one member of a succession's closure becomes in the same generation.

    The vocabulary is the standalone migration route's own, so an author who
    knows one road knows the other: `successor` carries the dependent to the
    successor type by re-pinning it, `retire` tombstones it -- with
    `claim_retirement_reason` `was-rescinded` that is a rescission, with any
    other reason and an optional `claim_effective_until` it is an ordinary
    attributed retirement.

    `re_author` is the disposition only a change set can offer: the dependent's
    successor is a sibling Claim member of the same intent, named by
    `successor_claim_id` -- the Claim ID that member revises, which is this
    dependent's own. That sibling is lowered under the successor type, so it may
    say under the new vocabulary what the predecessor could not say -- and it
    keeps the dependent's identity, its subject and its predicate, and its exact
    predecessor digest, which is what makes it a re-authoring of that Claim
    rather than a new one. There is no second spelling by member index: an index
    could only ever name the member this Claim ID already names.

    `invalidation` parses and always refuses. It is the standalone route's
    deprecated spelling of `retire`, answered there with a
    `cruxible.claim_type.invalidation_deprecated` warning; change-set lowering
    has no warning channel, so admitting it would coerce a deprecated word
    silently. The word is carried here only so an author who knows the
    standalone vocabulary gets a typed refusal naming the operator route
    instead of an untyped parse failure. It emits no deprecation notice: this
    surface never accepted it, so there is nothing to schedule for removal.
    """

    tag: Literal["playbill-claim-type-succession-dependent-v1"] = (
        "playbill-claim-type-succession-dependent-v1"
    )
    identity: ArtifactIdentity
    disposition: ClaimTypeSuccessionDisposition
    successor_claim_id: str | None = None
    claim_retirement_reason: ClaimRetirementReason | None = None
    claim_effective_until: datetime | None = None

    @field_validator("successor_claim_id")
    @classmethod
    def _successor_claim_id(cls, value: str | None) -> str | None:
        if value is not None:
            claim_path(value)
        return value

    @field_validator("claim_effective_until")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        # Refused, not reinterpreted: this instant is handed straight to
        # `ClaimTypeDependentDisposition`, which refuses a naive value, and a
        # member that silently called it UTC would retire a Claim at an instant
        # the author never wrote. The sibling retirement member's `ensure_utc`
        # is the older idiom; this field mirrors the migration vocabulary it
        # lowers into.
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("claim_effective_until must be timezone-aware")
        return value

    @field_serializer("claim_effective_until", when_used="json")
    def _serialize_time(self, value: datetime | None) -> str | None:
        return None if value is None else format_datetime(value)

    @model_validator(mode="after")
    def _disposition_shape(self) -> "ClaimTypeSuccessionDependent":
        if self.disposition == "re_author":
            if self.successor_claim_id is None:
                raise ValueError(
                    "a re_author dependent names the sibling Claim member that says it "
                    "again, as successor_claim_id"
                )
        elif self.successor_claim_id is not None:
            raise ValueError("only a re_author dependent names a sibling member")
        if self.disposition not in ("retire", "invalidation") and (
            self.claim_retirement_reason is not None or self.claim_effective_until is not None
        ):
            raise ValueError("retirement attribution belongs to a retire disposition")
        return self


class ClaimTypeSuccessionMember(_StrictAuthoringModel):
    """One ClaimType succession, its whole closure disposed, as one member.

    Evolving a committed vocabulary is one epistemic move -- "I need this
    distinction, and here is everything it changes" -- so it is one member of
    one change set, and it admits or refuses with the Claims that speak the new
    vocabulary rather than days after them.

    `successor` is a whole ClaimType that names its predecessor by identity and
    pins its exact digest, which is what makes it a succession rather than the
    definition `ClaimTypeAuthoringPayload` carries. `dependents` is the exact
    reverse-pin closure of the predecessor over the staged tree -- the accepted
    tree as this set's definition members left it -- and a closure that is not
    exact refuses. Sibling Claims are not in it: members lower in dependency
    order, so a Claim of the succeeded predicate authored in this same set is
    lowered under the successor and lands as an ordinary member.
    """

    tag: Literal["playbill-claim-type-succession-authoring-payload-v1"] = (
        "playbill-claim-type-succession-authoring-payload-v1"
    )
    successor: ClaimType
    dependents: tuple[ClaimTypeSuccessionDependent, ...] = ()
    #: Carry every closure member `dependents` does not name to the successor,
    #: computed by the daemon from the staged tree (retired Claims included).
    #: `dependents` then names only the exceptions: a `retire` or `re_author`.
    #: Absent from the wire when false, so an exact member's bytes never move.
    carry_all: bool = Field(default=False, exclude_if=lambda value: not value)

    @field_validator("successor")
    @classmethod
    def _successor(cls, value: ClaimType) -> ClaimType:
        if value.lifecycle.state != "live":
            # The standalone migration route accepts a byte-identical retiring
            # successor without comment, so an author who knows one road did not
            # know the other: the refusal now says which road takes it.
            raise ValueError(
                "a ClaimType succession installs a live successor; retire a ClaimType "
                "through `cruxible claim-type migrate`, which is the road that "
                "takes a retiring successor"
            )
        if value.lifecycle.predecessor_digest is None:
            raise ValueError(
                "a ClaimType succession member names the predecessor it succeeds; "
                "author a new ClaimType as a claim_type member"
            )
        return value

    @model_validator(mode="after")
    def _ordered_dependents(self) -> "ClaimTypeSuccessionMember":
        identities = tuple(item.identity.qualified for item in self.dependents)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("succession dependents must be UTF-8 byte-sorted and unique")
        return self

    @property
    def predicate(self) -> str:
        return self.successor.predicate


class ClaimRetirementMember(_StrictAuthoringModel):
    """One attributed Claim retirement, closure and all, as a change-set member.

    `mode` is `submit` alone: a change-set member is authored inside an intent
    whose own preflight already reports the closure this member still owes, so
    the second, member-local preflight mode of the standalone retirement route
    would only name the same inventory twice.

    `retires` is the bare Claim ID, named and spelled exactly as
    `ClaimAuthoringPayloadV1.revises` is. Tolerating a `Claim:` prefix here would
    give two spellings of one retirement the same member identity but different
    payload digests, so create-dedup would miss and two live intents could carry
    one semantic identity.
    """

    tag: Literal["playbill-claim-retirement-authoring-payload-v1"] = (
        "playbill-claim-retirement-authoring-payload-v1"
    )
    mode: Literal["submit"] = "submit"
    retires: str
    reason: ClaimRetirementReason
    effective_until: datetime | None = None
    dependents: tuple[ClaimRetireDependent, ...] = ()

    @field_validator("retires")
    @classmethod
    def _retires(cls, value: str) -> str:
        claim_path(value)
        return value

    @field_validator("effective_until")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_serializer("effective_until", when_used="json")
    def _serialize_time(self, value: datetime | None) -> str | None:
        return None if value is None else format_datetime(value)

    @model_validator(mode="after")
    def _ordered_dependents(self) -> "ClaimRetirementMember":
        identities = tuple(item.artifact_identity.qualified for item in self.dependents)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("retirement dependents must be UTF-8 byte-sorted and unique")
        return self

    @property
    def claim_id(self) -> str:
        return self.retires


AuthoringChangeSetMember: TypeAlias = Annotated[
    ClaimAuthoringPayloadV1
    | ClaimAuthoringPayloadV2
    | ClaimAuthoringPayload
    | ClaimTypeAuthoringPayload
    | ClaimTypeSuccessionMember
    | ClaimRetirementMember
    | ResolutionContractAuthoringPayload
    | AttestationAuthoringPayload
    | SubjectAuthoringPayload
    | QueryDefinitionAuthoringPayload
    | ApprovalPolicyAuthoringPayload
    | ProcedureRuntimePolicyAuthoringPayload
    | ProcedureMandateAuthoringPayload
    | CaptureContractAuthoringPayload
    | SourceAcquisitionPolicyAuthoringPayload
    | LineAuthoringPayload
    | TriggerAuthoringPayload
    | ProcedureAuthoringPayloadV1
    | ProcedureAuthoringPayload,
    Field(discriminator="tag"),
]


def authoring_claim_member_identity(payload: ClaimAuthoringPayloadV1) -> str:
    """Name one authored Claim member before the daemon has minted its Claim ID.

    A revision names its lineage. A new Claim has no ID until create mints one,
    so its member identity is its own authored statement: two members that would
    write the same statement are one member twice, and two members that merely
    contend for one slot stay distinct so the slot law -- not a membership
    collision -- is what refuses them.
    """

    if payload.revises is not None:
        return f"Claim:{payload.revises}"
    statement = payload.statement.model_dump(mode="json")
    statement.pop("tag")
    digest = typed_digest(
        Sha256Value,
        AUTHORING_CLAIM_MEMBER_IDENTITY_DIGEST_DOMAIN,
        statement,
    ).tagged.removeprefix("sha256:")
    return f"Claim:@{digest}"


def authoring_member_identity(payload: AuthoringChangeSetMember) -> str:
    if isinstance(payload, ResolutionContractAuthoringPayload):
        return payload.resolution_contract.identity.qualified
    if isinstance(payload, AttestationAuthoringPayload):
        return attestation_identity(payload.attestation).qualified
    if isinstance(payload, ClaimAuthoringPayloadV1):
        return authoring_claim_member_identity(payload)
    if isinstance(payload, ClaimTypeAuthoringPayload):
        return f"ClaimType:{payload.claim_type.predicate}"
    if isinstance(payload, ClaimTypeSuccessionMember):
        return f"ClaimTypeSuccession:{payload.predicate}"
    if isinstance(payload, ClaimRetirementMember):
        return f"ClaimRetirement:{payload.claim_id}"
    if isinstance(payload, SubjectAuthoringPayload):
        return f"Subject:{payload.subject.subject_kind}/{payload.subject.subject_id}"
    if isinstance(payload, QueryDefinitionAuthoringPayload):
        return payload.query_definition.identity.qualified
    if isinstance(payload, ApprovalPolicyAuthoringPayload):
        return APPROVAL_POLICY_IDENTITY
    if isinstance(payload, ProcedureRuntimePolicyAuthoringPayload):
        return PROCEDURE_RUNTIME_POLICY_IDENTITY
    if isinstance(payload, ProcedureMandateAuthoringPayload):
        return f"ProcedureMandate:{payload.name}"
    if isinstance(payload, CaptureContractAuthoringPayload):
        return f"CaptureContract:{payload.capture_contract.identity.name}"
    if isinstance(payload, SourceAcquisitionPolicyAuthoringPayload):
        return f"SourceAcquisitionPolicy:{payload.acquisition_policy.identity.name}"
    if isinstance(payload, LineAuthoringPayload):
        return f"Line:{payload.name}"
    if isinstance(payload, TriggerAuthoringPayload):
        return f"Trigger:{payload.name}"
    return f"Procedure:{payload.definition['name']}"


def authoring_change_set_membership(
    members: tuple[AuthoringChangeSetMember, ...],
) -> tuple[tuple[str, str], ...]:
    identities = tuple(authoring_member_identity(member) for member in members)
    return tuple((identity.partition(":")[0], identity) for identity in identities)


class _AuthoringIntentDecodeContext:
    """Output slots for one private event decode, never retained on a model.

    Payload validation and intent binding finish before the event validator
    consumes these results. Identity guards bind reuse to those very objects,
    within this validation call only; they are not a mutable-model cache.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.members: tuple[AuthoringChangeSetMember, ...] | None = None
        self.member_identities: tuple[str, ...] = ()
        self.payload: AuthoringPayload | None = None
        self.normalized_payload: dict[str, CanonicalValue] | None = None


class ChangeSetAuthoringPayload(_StrictAuthoringModel):
    tag: Literal["playbill-change-set-authoring-payload-v1"] = (
        "playbill-change-set-authoring-payload-v1"
    )
    # One authoring intent is one changeset, so the builder that carries eighty
    # members must also carry one: a two-member floor made the SDK's uniform
    # `cx.changes(...)` path refuse exactly the smallest set an author writes
    # first, and pushed them back onto a second, singular surface to say it.
    members: tuple[AuthoringChangeSetMember, ...] = Field(min_length=1)
    # Why this set exists, in the author's own words. It was already an argument
    # to `cx.changes(rationale=...)` and it was already hashed into the program
    # digest -- which meant the daemon could prove the author wrote SOMETHING and
    # could never read it, so the candidate commit fell back to a mechanical
    # subject. Absent from the canonical bytes when unset, so a payload written
    # before this field digests exactly as it did.
    rationale: str | None = Field(
        default=None,
        max_length=CHANGE_SET_RATIONALE_MAX_LENGTH,
        exclude_if=lambda value: value is None,
    )

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str | None) -> str | None:
        return validate_change_set_rationale(value)

    @field_validator("members")
    @classmethod
    def _members(
        cls,
        value: tuple[AuthoringChangeSetMember, ...],
        info: ValidationInfo,
    ) -> tuple[AuthoringChangeSetMember, ...]:
        identities = tuple(authoring_member_identity(member) for member in value)
        if len(set(identities)) != len(identities):
            raise ValueError("change-set member identities must be unique")
        if identities != tuple(sorted(identities, key=lambda item: item.encode("utf-8"))):
            raise ValueError("change-set members must be sorted by semantic identity")
        if isinstance(info.context, _AuthoringIntentDecodeContext):
            info.context.members = value
            info.context.member_identities = identities
        return value


AuthoringPayload = Annotated[
    ClaimAuthoringPayloadV1
    | ClaimAuthoringPayloadV2
    | ClaimAuthoringPayload
    | ProcedureAuthoringPayloadV1
    | ProcedureAuthoringPayload
    | ResolutionContractAuthoringPayload
    | AttestationAuthoringPayload
    | SubjectAuthoringPayload
    | QueryDefinitionAuthoringPayload
    | ApprovalPolicyAuthoringPayload
    | ProcedureRuntimePolicyAuthoringPayload
    | ProcedureMandateAuthoringPayload
    | CaptureContractAuthoringPayload
    | SourceAcquisitionPolicyAuthoringPayload
    | LineAuthoringPayload
    | TriggerAuthoringPayload
    | ChangeSetAuthoringPayload,
    Field(discriminator="tag"),
]


def authoring_payload_digest(payload: AuthoringPayload) -> str:
    """Digest what the payload IS, which is never how its author described it.

    A CHANGE SET's rationale is dropped beside `tag`. A set's identity is a
    property of its members alone -- that is the law the three-surface parity
    test pins, and it is what lets a CLI file, an MCP dict and an SDK draft
    naming the same members be recognized as one authoring. A digest that moved
    when the prose moved would make describing a set a different set. The prose
    is still covered: `authoring_create_fingerprint` takes the whole payload, so
    a rationale edited after the fact does not reproduce its own intent.

    A CLAIM's rationale is NOT dropped, and the two are not the same field
    wearing one name. A Claim's rationale is part of what the author asserted;
    it travels into the capture and is evidence. Stripping it here would
    silently restate the identity of every Claim payload ever digested.
    """

    preimage = payload.model_dump(mode="json")
    preimage.pop("tag")
    if isinstance(payload, ChangeSetAuthoringPayload):
        preimage.pop("rationale", None)
    return typed_digest(
        Sha256Value,
        AUTHORING_PAYLOAD_DIGEST_DOMAIN,
        preimage,
    ).tagged


def authoring_create_fingerprint(
    *,
    instance_id: str,
    actor_id: str,
    payload: AuthoringPayload,
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


def _normalized_authoring_digest(domain: str, preimage: dict[str, CanonicalValue]) -> str:
    """Hash an internal normalized snapshot with a frozen ASCII domain tag.

    Every runtime value must already have passed ``normalize_canonical``.
    This skips only its repeated traversal, retaining the same canonical JSON
    encoder and domain-separated bytes as ``typed_digest``. Never use this
    helper with raw values or retain its input across validation calls.
    """

    assert "tag" not in preimage
    return (
        "sha256:"
        + hashlib.sha256(canonical_json({"tag": domain, **preimage}).encode("utf-8")).hexdigest()
    )


class RepairAlternative(_StrictAuthoringModel):
    kind: str
    description: str
    replacement: object | None = None

    @field_validator("replacement", mode="before")
    @classmethod
    def _replacement(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @model_validator(mode="after")
    def _bounded(self) -> "RepairAlternative":
        if len(canonical_bytes(self.model_dump(mode="json"))) > MAX_REPAIR_BYTES:
            raise ValueError("authoring repair exceeds the frozen repair-byte limit")
        return self


class AuthoringDiagnostic(_StrictAuthoringModel):
    code: str
    stage: str
    offending_element: str
    message: str
    owner: DiagnosticOwner
    disposition: DiagnosticDisposition
    repairs: tuple[RepairAlternative, ...] = ()

    @field_validator("repairs")
    @classmethod
    def _repairs(
        cls,
        value: tuple[RepairAlternative, ...],
    ) -> tuple[RepairAlternative, ...]:
        if len(value) > MAX_REPAIR_ALTERNATIVES:
            raise ValueError("authoring diagnostic exceeds the repair-alternative limit")
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("authoring repairs must be canonically sorted and unique")
        return value

    @model_validator(mode="after")
    def _writer_has_repair(self) -> "AuthoringDiagnostic":
        if self.owner == "writer" and self.disposition == "edit_and_retry" and not self.repairs:
            raise ValueError("writer-repairable diagnostic must carry its repair")
        return self


class BlockedCheck(_StrictAuthoringModel):
    check: str
    blocked_by: tuple[str, ...]
    reason: str

    @field_validator("blocked_by")
    @classmethod
    def _blocked_by(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value), key=lambda item: item.encode())):
            raise ValueError("blocked check dependencies must be nonempty, sorted, and unique")
        return value


class DiagnosticFrontierLimits(_StrictAuthoringModel):
    max_diagnostics: Literal[128] = 128
    max_blocked_checks: Literal[128] = 128
    max_repair_alternatives: Literal[4] = 4
    max_repair_bytes: Literal[16384] = 16384
    max_frontier_bytes: Literal[1048576] = 1048576


class DiagnosticFrontier(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-diagnostic-frontier-v1"] = (
        "playbill-authoring-diagnostic-frontier-v1"
    )
    diagnostics: tuple[AuthoringDiagnostic, ...] = ()
    blocked_checks: tuple[BlockedCheck, ...] = ()
    frontier_complete: bool = True

    @model_validator(mode="after")
    def _bounded(self) -> "DiagnosticFrontier":
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


class AcceptanceCondition(_StrictAuthoringModel):
    condition: str
    owner: DiagnosticOwner
    action: str
    satisfied: bool


class CandidateStatus(_StrictAuthoringModel):
    tag: Literal["playbill-candidate-status-v1"] = "playbill-candidate-status-v1"
    state: CandidateStatusState
    proposal_id: str | None = None
    candidate_digest: str | None = None
    current_accepted_coordinate: AcceptedCoordinate
    path_to_acceptance: tuple[AcceptanceCondition, ...] = ()
    accepted_generation: AcceptedCoordinate | None = None

    @field_validator("proposal_id", "candidate_digest")
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value, label="CandidateStatus digest")

    @model_validator(mode="after")
    def _accepted_shape(self) -> "CandidateStatus":
        if (self.state == "accepted") != (self.accepted_generation is not None):
            raise ValueError("accepted CandidateStatus alone carries an accepted generation")
        return self


class PreflightCertificate(_StrictAuthoringModel):
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
    frontier_limits: DiagnosticFrontierLimits = DiagnosticFrontierLimits()
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

    @field_serializer("receive_limits", when_used="always")
    def _serialize_receive_limits(self, value: ProposalReceiveLimits) -> dict[str, object]:
        """Render the RECEIVE bounds alone, in every mode.

        A certificate re-derives its own digest on every read, and it nests
        inside a stored authoring intent whose event digest covers it in turn.
        Both preimages are the model's own dump, so a certificate that carried
        the whole limits model would stop reproducing -- and every intent
        holding one would stop being readable -- the moment a limit key with a
        default was added to `ProposalReceiveLimits`. That is what advertising
        the change-set record ceiling did.

        What a certificate is a statement about is what receive would enforce on
        this submission, and that is exactly this subset; the advertised
        ceilings are a published number of the build reading it, recovered from
        the model's own defaults. Same reasoning as `_PROPOSAL_ID_LIMIT_KEYS`
        for the proposal id, applied to the other stored identity.
        """

        return value.receive_bound_payload()

    @model_validator(mode="after")
    def _reproduces(self) -> "PreflightCertificate":
        if self.certificate_digest != preflight_certificate_digest(self):
            raise ValueError("preflight certificate digest does not reproduce")
        return self


def preflight_certificate_digest(certificate: PreflightCertificate) -> str:
    payload = certificate.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("certificate_digest")
    return typed_digest(
        Sha256Value,
        AUTHORING_PREFLIGHT_CERTIFICATE_DIGEST_DOMAIN,
        payload,
    ).tagged


def build_preflight_certificate(**values: object) -> PreflightCertificate:
    """Build the self-digesting frozen certificate without weakening validation."""

    typed_values = cast(dict[str, Any], values)
    provisional = PreflightCertificate.model_construct(
        **typed_values,
        certificate_digest="sha256:" + "0" * 64,
    )
    return PreflightCertificate.model_validate(
        {
            **values,
            "certificate_digest": preflight_certificate_digest(provisional),
        }
    )


class PreflightResult(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-preflight-result-v1"] = (
        "playbill-authoring-preflight-result-v1"
    )
    verdict: Literal["passed", "refused"]
    certificate: PreflightCertificate
    frontier: DiagnosticFrontier

    @model_validator(mode="after")
    def _verdict(self) -> "PreflightResult":
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


class ChangeSetClaimIdentity(_StrictAuthoringModel):
    """One change-set Claim member's minted Claim ID, frozen at create."""

    tag: Literal["playbill-change-set-claim-identity-v1"] = "playbill-change-set-claim-identity-v1"
    member_identity: str
    claim_id: str

    @field_validator("claim_id")
    @classmethod
    def _claim_id(cls, value: str) -> str:
        claim_path(value)
        return value


class AuthoringIntentV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-v1"] = "playbill-authoring-intent-v1"
    intent_id: str
    instance_id: str
    actor_id: str
    canonical_timestamp: str
    base_coordinate: AcceptedCoordinate
    semantic_identity: str
    payload: AuthoringPayload
    payload_digest: str
    create_fingerprint: str
    intent_revision: int = Field(default=0, ge=0)
    last_preflight: PreflightResult | None = None
    candidate_status: CandidateStatus
    change_set_claim_identities: tuple[ChangeSetClaimIdentity, ...] = ()

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
    def _validated_binding(self, info: ValidationInfo) -> "AuthoringIntentV1":
        # Pydantic detects context parameters by required argument count.
        # Keep the directly callable binding helper's no-context form too.
        return self._binding(info)

    def _binding(self, info: ValidationInfo | None = None) -> "AuthoringIntentV1":
        context = None if info is None else info.context
        # One ephemeral normalized snapshot feeds both frozen preimages. Never
        # cache it on the model: frozen payloads contain mutable nested values.
        payload_dump = self.payload.model_dump(mode="json")
        payload_tag = payload_dump.pop("tag")
        normalized_payload = normalize_canonical(payload_dump)
        assert isinstance(normalized_payload, dict)
        # One snapshot, two preimages that differ for exactly one payload: a
        # change set's rationale is dropped beside `tag`, exactly as
        # `authoring_payload_digest` drops it, because a set's identity is its
        # members. Withheld here rather than re-derived, and put straight back
        # into the fingerprint preimage below, where
        # `authoring_create_fingerprint` still digests the whole payload. Unset,
        # the field is absent from the dump, so nothing is withheld and nothing
        # is restored -- which is what the standalone pop's default says. A
        # Claim's rationale stays in both; the law is at
        # `authoring_payload_digest`.
        withheld: dict[str, CanonicalValue] = {}
        if isinstance(self.payload, ChangeSetAuthoringPayload):
            if "rationale" in normalized_payload:
                withheld["rationale"] = normalized_payload.pop("rationale")
        if self.payload_digest != _normalized_authoring_digest(
            AUTHORING_PAYLOAD_DIGEST_DOMAIN, normalized_payload
        ):
            raise ValueError("AuthoringIntent payload digest does not reproduce")
        # Preserve fingerprint traversal/refusal order after the payload check.
        instance_id = normalize_canonical(self.instance_id, location="$.instance_id")
        actor_id = normalize_canonical(self.actor_id, location="$.actor_id")
        tagged_payload = {
            "tag": normalize_canonical(payload_tag, location="$.payload.tag"),
            **normalized_payload,
            **withheld,
        }
        expected_fingerprint = _normalized_authoring_digest(
            AUTHORING_CREATE_FINGERPRINT_DOMAIN,
            {"instance_id": instance_id, "actor_id": actor_id, "payload": tagged_payload},
        )
        if self.create_fingerprint != expected_fingerprint:
            raise ValueError("AuthoringIntent create fingerprint does not reproduce")
        if isinstance(self.payload, ClaimAuthoringPayloadV1):
            claim_path(self.semantic_identity)
            if self.change_set_claim_identities:
                raise ValueError("a singular Claim intent owns no change-set Claim identities")
        else:
            if isinstance(self.payload, ChangeSetAuthoringPayload):
                if (
                    isinstance(context, _AuthoringIntentDecodeContext)
                    and context.members is self.payload.members
                ):
                    membership = tuple(
                        (identity.partition(":")[0], identity)
                        for identity in context.member_identities
                    )
                else:
                    membership = authoring_change_set_membership(self.payload.members)
                expected_identity = "ChangeSet:" + typed_digest(
                    Sha256Value,
                    AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
                    {
                        "members": [
                            {"kind": kind, "identity": identity} for kind, identity in membership
                        ]
                    },
                ).tagged.removeprefix("sha256:")
            else:
                expected_identity = authoring_member_identity(self.payload)
            if self.semantic_identity != expected_identity:
                raise ValueError("AuthoringIntent identity differs from its payload")
            if not isinstance(self.payload, ChangeSetAuthoringPayload):
                if self.change_set_claim_identities:
                    raise ValueError("only a change set owns per-member Claim identities")
            else:
                self._bind_change_set_members(
                    self.payload,
                    member_identities=tuple(identity for _kind, identity in membership),
                )
        if isinstance(context, _AuthoringIntentDecodeContext):
            context.payload = self.payload
            context.normalized_payload = tagged_payload
        return self

    def _bind_change_set_members(
        self,
        payload: "ChangeSetAuthoringPayload",
        *,
        member_identities: tuple[str, ...],
    ) -> None:
        claim_members = {
            identity: member
            for identity, member in zip(member_identities, payload.members, strict=True)
            if isinstance(member, ClaimAuthoringPayloadV1)
        }
        minted = self.change_set_claim_identities
        identities = tuple(item.member_identity for item in minted)
        if identities != tuple(sorted(claim_members, key=lambda item: item.encode("utf-8"))):
            raise ValueError("change-set Claim identities must name every Claim member once")
        by_member = {item.member_identity: item.claim_id for item in minted}
        for member_identity, member in claim_members.items():
            claim_id = by_member[member_identity]
            if member.revises is not None and member.revises != claim_id:
                raise ValueError("a revising Claim member keeps the lineage it names")


class AuthoringIntent(AuthoringIntentV1):
    """V1 intent state plus coordinate assertions that never enter authoring identity."""

    tag: Literal["playbill-authoring-intent-v2"] = "playbill-authoring-intent-v2"  # type: ignore[assignment]
    reference_expectations: tuple[AuthoringExpectation, ...]

    @field_validator("reference_expectations")
    @classmethod
    def _reference_expectations(
        cls,
        value: tuple[AuthoringExpectation, ...],
    ) -> tuple[AuthoringExpectation, ...]:
        return canonical_reference_expectations(value)


# Response wrappers must retain the fields selected by the nested intent tag.
_AuthoringIntentResponse: TypeAlias = Annotated[
    AuthoringIntentV1 | AuthoringIntent, Field(discriminator="tag")
]


class AuthoringIntentView(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-view-v1"] = "playbill-authoring-intent-view-v1"
    intent: _AuthoringIntentResponse


class AuthoringIntentList(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-list-v1"] = "playbill-authoring-intent-list-v1"
    intents: tuple[_AuthoringIntentResponse, ...]


class AuthoringIntentCompileRequestV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-compile-request-v1"] = (
        "playbill-authoring-intent-compile-request-v1"
    )
    payload: AuthoringPayload
    intent_id: str | None = None


class AuthoringIntentCompileRequest(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-compile-request-v3"] = (
        "playbill-authoring-intent-compile-request-v3"
    )
    payload: AuthoringPayload
    reference_expectations: tuple[AuthoringExpectation, ...]
    program_stamp: AuthoringProgramStamp
    intent_id: str | None = None

    @field_validator("reference_expectations")
    @classmethod
    def _reference_expectations(
        cls,
        value: tuple[AuthoringExpectation, ...],
    ) -> tuple[AuthoringExpectation, ...]:
        return canonical_reference_expectations(value)


class AuthoringIntentPreflightRequest(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-preflight-request-v1"] = (
        "playbill-authoring-intent-preflight-request-v1"
    )


class AuthoringIntentSubmitRequest(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-submit-request-v1"] = (
        "playbill-authoring-intent-submit-request-v1"
    )


class AuthoringSubmitMember(_StrictAuthoringModel):
    """What one submitted member became, so a set says it once per member."""

    tag: Literal["playbill-authoring-submit-member-v1"] = "playbill-authoring-submit-member-v1"
    identity: str
    artifact_digest: str
    predecessor_digest: str | None = None
    identity_stable: bool = False
    claim_revision: int | None = None


class AuthoringSubmitResult(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-submit-result-v1"] = "playbill-authoring-submit-result-v1"
    intent: _AuthoringIntentResponse
    status: CandidateStatus
    workspace_advertisement: WorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT
    # A `revises` submit amends one Claim identity in place rather than adding a
    # second Claim, and nothing in the result said so: the caller saw an ordinary
    # submit and had to re-read the artifact to learn the identity was reused.
    # `claim_revision` is the revision this candidate becomes once accepted.
    identity_stable: bool = False
    claim_revision: int | None = None
    # One intent is one changeset, so the same two facts are reported per member.
    # The singular pair above stays the singular Claim intent's answer.
    members: tuple[AuthoringSubmitMember, ...] = ()

    @field_validator("members")
    @classmethod
    def _members(
        cls,
        value: tuple[AuthoringSubmitMember, ...],
    ) -> tuple[AuthoringSubmitMember, ...]:
        identities = tuple(item.identity for item in value)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("submit result members must be identity-sorted and unique")
        return value


BlockSyncReadStatus: TypeAlias = Literal[
    "current",
    "successor",
    "refused",
    "unsyncable",
    "unchecked",
]
BlockSyncReadReason: TypeAlias = Literal[
    "block_workspace_instance_mismatch",
    "block_backing_missing",
    "block_backing_changed",
    "block_backing_overturned",
    "block_backing_retired",
    "block_successor_ambiguous",
    "block_query_unchecked",
]


class BlockSyncSuccessorCandidate(_StrictAuthoringModel):
    tag: Literal["playbill-block-sync-successor-candidate-v1"] = (
        "playbill-block-sync-successor-candidate-v1"
    )
    identity: ArtifactIdentity
    artifact_digest: str
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)

    _artifact_digest = field_validator("artifact_digest")(
        lambda value: _sha256(value, label="block sync successor artifact digest")
    )


def _projection_evaluation_time(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("projection checks require an absolute evaluation time")
    return None if value is None else ensure_utc(value)


class BlockSyncReadRequest(_StrictAuthoringModel):
    tag: Literal["playbill-block-sync-read-request-v1"] = "playbill-block-sync-read-request-v1"
    stamp: ProjectionBlockStampAny
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime | None = None
    preferred_successor_digest: str | None = None

    @field_validator("evaluation_time")
    @classmethod
    def _absolute_time(cls, value: datetime | None) -> datetime | None:
        return _projection_evaluation_time(value)

    @field_validator("preferred_successor_digest")
    @classmethod
    def _preferred_digest(cls, value: str | None) -> str | None:
        if value is not None:
            _sha256(value, label="preferred block sync successor digest")
        return value


class ProjectionDependencyIssue(_StrictAuthoringModel):
    identity: ArtifactIdentity
    status: Literal["stale", "unchecked", "invalid"]
    reason: BlockSyncReadReason
    detail: str


class BlockSyncReadResult(_StrictAuthoringModel):
    """A currency assessment; body rendering is owned by the author."""

    tag: Literal["playbill-block-sync-read-result-v1"] = "playbill-block-sync-read-result-v1"
    status: BlockSyncReadStatus
    original_artifact_digest: str | None = None
    artifact_digest: str | None = None
    coordinate: AcceptedCoordinate | None = None
    generation: int | None = Field(default=None, ge=0)
    backing: ProjectionBacking | None = None
    # The current spelling of every held backing that moved under the stamp.
    # A block holds a LIST, so naming one is not enough to repair it.
    moved_backings: tuple[ProjectionBacking, ...] = ()
    issues: tuple[ProjectionDependencyIssue, ...] = ()
    current_backings: tuple[ProjectionBacking, ...] = ()
    successor_candidates: tuple[BlockSyncSuccessorCandidate, ...] = ()
    reason: BlockSyncReadReason | None = None
    detail: str | None = None

    @field_validator("original_artifact_digest", "artifact_digest")
    @classmethod
    def _optional_digests(cls, value: str | None) -> str | None:
        if value is not None:
            _sha256(value, label="block sync digest")
        return value

    @model_validator(mode="after")
    def _result_shape(self) -> "BlockSyncReadResult":
        success = self.status in {"current", "successor"}
        if success != (self.coordinate is not None and self.generation is not None):
            raise ValueError("a block currency verdict names the coordinate it was read at")
        if success and (self.reason is not None or self.successor_candidates):
            raise ValueError("successful block sync reads cannot carry a refusal")
        if not success and self.reason is None:
            raise ValueError("refused block sync reads require a typed reason")
        if self.status == "successor" and not self.moved_backings:
            raise ValueError("a successor verdict names exactly the backings that moved")
        if self.reason == "block_successor_ambiguous":
            if len(self.successor_candidates) < 2:
                raise ValueError("ambiguous block sync reads require successor candidates")
        elif self.successor_candidates:
            raise ValueError("only ambiguous block sync reads carry successor candidates")
        if self.successor_candidates != tuple(
            sorted(
                self.successor_candidates,
                key=lambda item: item.artifact_digest.encode("ascii"),
            )
        ):
            raise ValueError("block sync successor candidates must be digest-sorted")
        return self


class ProjectionCheckRequest(_StrictAuthoringModel):
    tag: Literal["playbill-projection-check-request-v1"] = "playbill-projection-check-request-v1"
    stamps: tuple[ProjectionBlockStampAny, ...] = Field(max_length=4096)
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime | None = None

    @field_validator("evaluation_time")
    @classmethod
    def _absolute_time(cls, value: datetime | None) -> datetime | None:
        return _projection_evaluation_time(value)


class ProjectionCheckResult(_StrictAuthoringModel):
    tag: Literal["playbill-projection-check-result-v1"] = "playbill-projection-check-result-v1"
    coordinate: AcceptedCoordinate
    evaluation_time: datetime
    results: tuple[BlockSyncReadResult, ...]


BlockSyncOutcome: TypeAlias = Literal[
    "unchanged",
    "stale",
    "dirty",
    "detached",
    "would_detach",
    "skipped",
    "refused",
    "unchecked",
]
BlockSyncReason: TypeAlias = Literal[
    "workspace_not_attached",
    "workspace_binding_invalid",
    "workspace_instance_mismatch",
    "workspace_source_catalog_invalid",
    "source_path_invalid",
    "source_not_projection_target",
    "block_marker_malformed",
    "block_unstamped",
    "block_locally_modified",
    "block_backing_missing",
    "block_backing_changed",
    "block_backing_overturned",
    "block_backing_retired",
    "block_successor_ambiguous",
    "block_concurrent_edit",
    "block_frame_invalid",
    "block_sync_failed",
    "projection_processing_incomplete",
    "block_query_unchecked",
]


class BlockSyncItem(_StrictAuthoringModel):
    tag: Literal["playbill-block-sync-item-v1"] = "playbill-block-sync-item-v1"
    path: str
    source_id: str | None = None
    block_id: str | None = None
    currency_policy: Literal["warn", "require_current"] = "warn"
    outcome: BlockSyncOutcome
    reason: BlockSyncReason | None = None
    # The prose ``repair_commands`` this replaced were free strings a caller had
    # to parse; the structured carrier names the served operation and its
    # arguments, and a producer that carries none projects the declared repair
    # its typed reason resolves to.
    repair: ServedRepair | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _declared_repair(cls, value: object) -> object:
        if not isinstance(value, dict) or value.get("repair") is not None:
            return value
        reason = value.get("reason")
        if not isinstance(reason, str):
            return value
        return {**value, "repair": served_repair_for_refusal(reason).model_dump(mode="python")}

    @model_validator(mode="after")
    def _item_shape(self) -> "BlockSyncItem":
        # `stale` and `dirty` are findings, not refusals, but they are just as
        # reasoned: a block whose held list moved names which reason moved it,
        # and a block whose prose moved names that. Every one of them carries a
        # repair, because a finding with no named change is a row nobody acts on.
        reasoned = self.outcome in {"skipped", "refused", "unchecked", "stale", "dirty"}
        if reasoned != (self.reason is not None):
            raise ValueError("block sync skipped/refusal outcomes require exactly one typed reason")
        if reasoned != (self.repair is not None):
            raise ValueError("block sync refusal outcomes carry exactly one structured repair")
        return self

    @property
    def blocking(self) -> bool:
        return self.outcome == "refused" or (
            self.currency_policy == "require_current"
            and self.outcome in {"unchecked", "stale", "dirty"}
        )


class BlockSyncResult(_StrictAuthoringModel):
    tag: Literal["playbill-block-sync-result-v1"] = "playbill-block-sync-result-v1"
    items: tuple[BlockSyncItem, ...]
    changed_file_count: int = Field(ge=0)
    would_change: bool
    has_refusals: bool

    @model_validator(mode="after")
    def _summary_shape(self) -> "BlockSyncResult":
        changed = {item.path for item in self.items if item.outcome == "detached"}
        prospective = any(item.outcome in {"detached", "would_detach"} for item in self.items)
        # Currency policy gates drift; integrity refusals always fail the check.
        refused = any(item.blocking for item in self.items)
        if self.changed_file_count != len(changed):
            raise ValueError("block sync changed-file count does not reproduce")
        if self.would_change != prospective or self.has_refusals != refused:
            raise ValueError("block sync summary flags do not reproduce")
        return self


class BlockDetachResult(_StrictAuthoringModel):
    """Retired blocks' markers removed from pages, bodies kept; or (preview) which would be.

    ``coordinate`` digests the named pages' bytes as this call read them; a
    commit carrying ``at`` refuses ``cruxible.preview.state_moved`` if any page
    changed since its preview.
    """

    tag: Literal["playbill-block-detach-result-v1"] = "playbill-block-detach-result-v1"
    status: Literal["detached", "would_detach"]
    sync: BlockSyncResult
    coordinate: StateCoordinate


__all__ = [
    "BlockDetachResult",
    "AUTHORING_INTENT_ID_RE",
    "AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST",
    "AUTHORING_SDK_VERSION",
    "AcceptanceCondition",
    "AuthoringArtifactReference",
    "AuthoringCandidateReference",
    "AuthoringChangeSetMember",
    "AuthoringClaimStatement",
    "AuthoringDiagnostic",
    "AuthoringExactContentObject",
    "AuthoringIntentCompileRequest",
    "AuthoringIntentCompileRequestV1",
    "AuthoringIntentList",
    "AuthoringIntentPreflightRequest",
    "AuthoringIntentSubmitRequest",
    "AuthoringIntentV1",
    "AuthoringIntent",
    "AuthoringIntentView",
    "AuthoringPayload",
    "AuthoringProgramOperation",
    "AuthoringProgramStamp",
    "AuthoringExpectation",
    "AuthoringReferenceExpectation",
    "AuthoringReferenceKind",
    "AuthoringSlotExpectation",
    "AuthoringReferenceSuccessor",
    "AuthoringSubmitMember",
    "AuthoringSubmitResult",
    "BlockedCheck",
    "CandidateStatusState",
    "CandidateStatus",
    "ClaimAuthoringPayloadV1",
    "ClaimAuthoringPayloadV2",
    "ClaimAuthoringPayload",
    "ChangeSetAuthoringPayload",
    "ChangeSetClaimIdentity",
    "ClaimRetirementMember",
    "ClaimTypeAuthoringPayload",
    "ClaimTypeSuccessionDependent",
    "ClaimTypeSuccessionDisposition",
    "ClaimTypeSuccessionMember",
    "ClaimAuthoringSource",
    "ClaimDependencyDrafts",
    "DiagnosticFrontierLimits",
    "DiagnosticFrontier",
    "BlockSyncItem",
    "BlockSyncOutcome",
    "BlockSyncReadReason",
    "BlockSyncReadRequest",
    "ProjectionCheckRequest",
    "ProjectionCheckResult",
    "ProjectionDependencyIssue",
    "BlockSyncReadResult",
    "BlockSyncReadStatus",
    "BlockSyncReason",
    "BlockSyncResult",
    "BlockSyncSuccessorCandidate",
    "PreflightCertificate",
    "PreflightResult",
    "ProcedureAuthoringPayloadV1",
    "ProcedureAuthoringPayload",
    "ApprovalPolicyAuthoringPayload",
    "ProcedureRuntimePolicyAuthoringPayload",
    "MandateConditionAuthoring",
    "MandateScopeAuthoring",
    "ProcedureMandateAuthoringPayload",
    "TriggerAuthoringPayload",
    "QueryDefinitionAuthoringPayload",
    "AttestationAuthoringPayload",
    "ResolutionContractAuthoringPayload",
    "SubjectAuthoringPayload",
    "RepairAlternative",
    "ExistingCaptureCitationSource",
    "SelfSourceBody",
    "WorkingAnchorWindow",
    "WorkingDigestCoordinate",
    "WorkingGitBlobCoordinate",
    "WorkingSelectionObservation",
    "authoring_create_fingerprint",
    "authoring_change_set_membership",
    "authoring_claim_member_identity",
    "authoring_member_identity",
    "authoring_payload_digest",
    "authoring_program_digest",
    "authoring_program_stamp_operation_key",
    "canonical_reference_expectations",
    "build_preflight_certificate",
    "preflight_certificate_digest",
    "reference_expectations_digest",
]
