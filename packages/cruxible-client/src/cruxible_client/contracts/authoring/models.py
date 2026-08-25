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
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import ArtifactAuthority, ArtifactIdentity
from cruxible_client.contracts.candidates import validate_candidate_timestamp
from cruxible_client.contracts.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import LiteralClaimObject, SubjectClaimObject, claim_path
from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContractV1
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.proposal_models import AuthenticatedActor, ProposalReceiveLimits
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_client.contracts.types import CompilerCoordinate

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
AUTHORING_PROGRAM_DIGEST_DOMAIN = "playbill-sdk-authoring-program-v1"
AUTHORING_PROGRAM_STAMP_OPERATION_DOMAIN = "playbill-authoring-program-stamp-operation-v1"
# Before this lineage's first public release, a version's digest may be re-pinned
# only with its audited snapshot, SDK handshake, and digest guardrail in the same
# commit. After first public release, every contract change must succeed the version.
AUTHORING_SDK_VERSION = "0.4.0"
AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST = (
    "sha256:036e3af304bf20dfcdafdc0b31c764d1e909084b3c70df73a62d1feb6a7079b2"
)
INSERTION_TARGET_DIGEST_DOMAIN = "playbill-insertion-target-v1"
INSERTION_EXPECTATION_ID_DOMAIN = "playbill-insertion-expectation-id-v1"
INSERTION_EXPECTATION_DIGEST_DOMAIN = "playbill-insertion-expectation-v1"
INSERTION_PATCH_ENVELOPE_DIGEST_DOMAIN = "playbill-insertion-patch-envelope-v1"
INSERTION_CONFIRMATION_OBSERVATION_DIGEST_DOMAIN = "playbill-insertion-confirmation-observation-v1"
INSERTION_CONFIRM_OPERATION_DOMAIN = "playbill-insertion-confirm-operation-v1"
INSERTION_RESULT_KEY_DOMAIN = "playbill-insertion-result-key-v1"
INSERTION_TERMINAL_TOMBSTONE_DIGEST_DOMAIN = "playbill-insertion-terminal-tombstone-v1"

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


class AuthoringReferenceExpectationV1(_StrictAuthoringModel):
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


class AuthoringReferenceSuccessorV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-reference-successor-v1"] = (
        "playbill-authoring-reference-successor-v1"
    )
    payload_path: str
    artifact_kind: AuthoringReferenceKind
    address: str
    coordinate: AcceptedCoordinate


class AuthoringProgramOperationV1(_StrictAuthoringModel):
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


class AuthoringProgramStampV1(_StrictAuthoringModel):
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
    operations: tuple[AuthoringProgramOperationV1, ...],
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
    program_stamp: AuthoringProgramStampV1,
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
    values: tuple[AuthoringReferenceExpectationV1, ...],
) -> tuple[AuthoringReferenceExpectationV1, ...]:
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
    values: tuple[AuthoringReferenceExpectationV1, ...],
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


class InsertionAnchorWindowV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-anchor-window-v1"] = "playbill-insertion-anchor-window-v1"
    anchor_content_base64: str
    anchor_bytes_digest: str
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    insertion_offset: int = Field(ge=0)
    observed_occurrence_count: int = Field(ge=0)

    @field_validator("anchor_content_base64")
    @classmethod
    def _content(cls, value: str) -> str:
        content = _canonical_base64(value, label="insertion anchor content")
        if len(content) > 4 * 1024:
            raise ValueError("insertion anchor exceeds its 4 KiB byte limit")
        return value

    @field_validator("anchor_bytes_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="insertion anchor digest")

    @model_validator(mode="after")
    def _correspondence(self) -> "InsertionAnchorWindowV1":
        content = _canonical_base64(
            self.anchor_content_base64,
            label="insertion anchor content",
        )
        if self.end_byte < self.start_byte:
            raise ValueError("insertion anchor window is decreasing")
        if len(content) != self.end_byte - self.start_byte:
            raise ValueError("insertion anchor bytes differ from the declared window")
        expected = "sha256:" + hashlib.sha256(content).hexdigest()
        if self.anchor_bytes_digest != expected:
            raise ValueError("insertion anchor digest differs from its exact bytes")
        return self

    @property
    def content(self) -> bytes:
        return _canonical_base64(
            self.anchor_content_base64,
            label="insertion anchor content",
        )


InsertionOperation: TypeAlias = Literal[
    "insert_before",
    "insert_after",
    "replace_window",
    "append",
]


class InsertionTargetV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-target-v1"] = "playbill-insertion-target-v1"
    source_id: str
    coordinate: WorkingSelectionCoordinateV1
    preimage_digest: str
    selector: InsertionAnchorWindowV1
    operation: InsertionOperation
    postimage_digest: str
    postimage_byte_length: int = Field(ge=0)

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        if not _CANONICAL_NAME_RE.fullmatch(value):
            raise ValueError("insertion source_id must be stable, locator-free, and canonical")
        return value

    @field_validator("preimage_digest", "postimage_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="insertion whole-source digest")

    @model_validator(mode="after")
    def _target_shape(self) -> "InsertionTargetV1":
        source_length = self.coordinate.source_byte_length
        selector = self.selector
        if selector.end_byte > source_length or selector.insertion_offset > source_length:
            raise ValueError("insertion target exceeds the proposer-observed source")
        if isinstance(self.coordinate, WorkingDigestCoordinateV1) and (
            self.coordinate.source_content_digest != self.preimage_digest
        ):
            raise ValueError("insertion preimage differs from its observed-digest coordinate")
        empty_append = (
            self.operation == "append"
            and source_length == 0
            and selector.start_byte == selector.end_byte == selector.insertion_offset == 0
            and selector.content == b""
        )
        if selector.observed_occurrence_count != 1 and not (
            empty_append and selector.observed_occurrence_count == 1
        ):
            raise ValueError("insertion anchor must have exactly one observed occurrence")
        if self.operation == "insert_before" and selector.insertion_offset != selector.start_byte:
            raise ValueError("insert_before offset must equal the anchor start")
        if self.operation == "insert_after" and selector.insertion_offset != selector.end_byte:
            raise ValueError("insert_after offset must equal the anchor end")
        if self.operation == "replace_window" and selector.insertion_offset != selector.start_byte:
            raise ValueError("replace_window offset must equal the window start")
        if self.operation == "append" and selector.insertion_offset != source_length:
            raise ValueError("append offset must equal the observed source length")
        return self


def insertion_target_digest(target: InsertionTargetV1) -> str:
    payload = target.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, INSERTION_TARGET_DIGEST_DOMAIN, payload).tagged


def insertion_expectation_id(
    *,
    instance_id: str,
    intent_id: str,
    intent_revision: int,
) -> str:
    return typed_digest(
        Sha256Value,
        INSERTION_EXPECTATION_ID_DOMAIN,
        {
            "instance_id": instance_id,
            "intent_id": intent_id,
            "intent_revision": intent_revision,
        },
    ).tagged


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
    insertion_target: InsertionTargetV1 | None = None

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


class ClaimDependencyDraftsV1(_StrictAuthoringModel):
    tag: Literal["playbill-claim-dependency-drafts-v1"] = "playbill-claim-dependency-drafts-v1"
    subject: SubjectShell | None = None
    claim_type: ClaimType | None = None


class ClaimAuthoringPayloadV2(ClaimAuthoringPayloadV1):
    tag: Literal["playbill-claim-authoring-payload-v2"] = "playbill-claim-authoring-payload-v2"  # type: ignore[assignment]
    dependency_drafts: ClaimDependencyDraftsV1


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


class ProcedureAuthoringPayloadV2(_StrictAuthoringModel):
    tag: Literal["playbill-procedure-authoring-payload-v2"] = (
        "playbill-procedure-authoring-payload-v2"
    )
    definition: dict[str, object]
    authority: ArtifactAuthority
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    owned_contracts: tuple[ProcedureOwnedContractV1, ...]
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
    ClaimAuthoringPayloadV1
    | ClaimAuthoringPayloadV2
    | ProcedureAuthoringPayloadV1
    | ProcedureAuthoringPayloadV2,
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


InsertionExpectationState: TypeAlias = Literal[
    "awaiting_claim_acceptance",
    "pending",
    "confirming",
    "bound",
    "expired",
    "abandoned",
    "claim_currency_changed",
]
InsertionConfirmOutcome: TypeAlias = Literal[
    "bound",
    "already_bound",
    "backing_candidate_pending",
    "backing_candidate_refused",
    "ambiguous",
    "stale_target",
    "expired",
    "claim_currency_changed",
]


class InsertionPatchEnvelopeV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-patch-envelope-v1"] = "playbill-insertion-patch-envelope-v1"
    source_id: str
    preimage_digest: str
    preimage_byte_length: int = Field(ge=0)
    selector: InsertionAnchorWindowV1
    operation: InsertionOperation
    body_digest: str
    body_byte_length: int = Field(ge=0)
    postimage_digest: str
    postimage_byte_length: int = Field(ge=0)
    target_digest: str
    expires_at: datetime
    envelope_digest: str

    @field_validator(
        "preimage_digest",
        "body_digest",
        "postimage_digest",
        "target_digest",
        "envelope_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="insertion patch digest")

    @field_validator("expires_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("expires_at", when_used="json")
    def _serialize_time(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @model_validator(mode="after")
    def _correspondence(self) -> "InsertionPatchEnvelopeV1":
        removed = (
            self.selector.end_byte - self.selector.start_byte
            if self.operation == "replace_window"
            else 0
        )
        expected_length = self.preimage_byte_length - removed + self.body_byte_length
        if expected_length != self.postimage_byte_length:
            raise ValueError("insertion patch byte-length arithmetic does not reproduce")
        if self.envelope_digest != insertion_patch_envelope_digest(self):
            raise ValueError("insertion patch envelope digest does not reproduce")
        return self


def insertion_patch_envelope_digest(envelope: InsertionPatchEnvelopeV1) -> str:
    payload = envelope.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("envelope_digest")
    return typed_digest(
        Sha256Value,
        INSERTION_PATCH_ENVELOPE_DIGEST_DOMAIN,
        payload,
    ).tagged


def build_insertion_patch_envelope(**values: object) -> InsertionPatchEnvelopeV1:
    provisional = InsertionPatchEnvelopeV1.model_construct(
        **cast(dict[str, Any], values),
        envelope_digest="sha256:" + "0" * 64,
    )
    return InsertionPatchEnvelopeV1.model_validate(
        {
            **values,
            "envelope_digest": insertion_patch_envelope_digest(provisional),
        }
    )


class InsertionConfirmationObservationV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-confirmation-observation-v1"] = (
        "playbill-insertion-confirmation-observation-v1"
    )
    expectation_id: str
    source_id: str
    coordinate: WorkingSelectionCoordinateV1
    observed_content_digest: str
    selected_start_byte: int = Field(ge=0)
    selected_end_byte: int = Field(ge=0)
    selected_bytes_digest: str
    observed_occurrence_count: int = Field(ge=0)

    @field_validator("expectation_id", "observed_content_digest", "selected_bytes_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="insertion confirmation digest")

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        if not _CANONICAL_NAME_RE.fullmatch(value):
            raise ValueError("confirmation source_id must be stable and locator-free")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "InsertionConfirmationObservationV1":
        if self.selected_end_byte < self.selected_start_byte:
            raise ValueError("confirmation selected span is decreasing")
        if self.selected_end_byte > self.coordinate.source_byte_length:
            raise ValueError("confirmation selected span exceeds its observed source")
        if isinstance(self.coordinate, WorkingDigestCoordinateV1) and (
            self.coordinate.source_content_digest != self.observed_content_digest
        ):
            raise ValueError("confirmation digest differs from its coordinate")
        return self


def insertion_confirmation_observation_digest(
    observation: InsertionConfirmationObservationV1,
) -> str:
    payload = observation.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        INSERTION_CONFIRMATION_OBSERVATION_DIGEST_DOMAIN,
        payload,
    ).tagged


class InsertionTerminalTombstoneV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-terminal-tombstone-v1"] = (
        "playbill-insertion-terminal-tombstone-v1"
    )
    result_key: str
    intent_id: str
    expectation_id: str
    final_state: Literal["bound", "expired", "abandoned", "claim_currency_changed"]
    final_result: Literal[
        "bound",
        "expired",
        "abandoned",
        "claim_currency_changed",
    ]
    citation_id: str | None = None
    successor_candidate_ref: str | None = None
    finalized_at: datetime
    retain_until: datetime
    patch_envelope_digest: str
    tombstone_digest: str

    @field_validator(
        "result_key",
        "expectation_id",
        "patch_envelope_digest",
        "tombstone_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="insertion tombstone digest")

    @field_validator("finalized_at", "retain_until")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("finalized_at", "retain_until", when_used="json")
    def _serialize_times(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @model_validator(mode="after")
    def _shape(self) -> "InsertionTerminalTombstoneV1":
        if self.retain_until < self.finalized_at:
            raise ValueError("insertion tombstone retention precedes finalization")
        if (self.final_state == "bound") != (self.citation_id is not None):
            raise ValueError("only a bound insertion tombstone carries citation_id")
        if self.tombstone_digest != insertion_terminal_tombstone_digest(self):
            raise ValueError("insertion tombstone digest does not reproduce")
        return self


def insertion_terminal_tombstone_digest(tombstone: InsertionTerminalTombstoneV1) -> str:
    payload = tombstone.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("tombstone_digest")
    return typed_digest(
        Sha256Value,
        INSERTION_TERMINAL_TOMBSTONE_DIGEST_DOMAIN,
        payload,
    ).tagged


def insertion_result_key(
    *,
    instance_id: str,
    actor_id: str,
    intent_id: str,
    expectation_id: str,
) -> str:
    return typed_digest(
        Sha256Value,
        INSERTION_RESULT_KEY_DOMAIN,
        {
            "instance_id": instance_id,
            "actor_id": actor_id,
            "intent_id": intent_id,
            "expectation_id": expectation_id,
        },
    ).tagged


def build_insertion_terminal_tombstone(**values: object) -> InsertionTerminalTombstoneV1:
    provisional = InsertionTerminalTombstoneV1.model_construct(
        **cast(dict[str, Any], values),
        tombstone_digest="sha256:" + "0" * 64,
    )
    return InsertionTerminalTombstoneV1.model_validate(
        {
            **values,
            "tombstone_digest": insertion_terminal_tombstone_digest(provisional),
        }
    )


class InsertionExpectationV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-expectation-v1"] = "playbill-insertion-expectation-v1"
    expectation_id: str
    state: InsertionExpectationState
    claim_identity: str
    original_claim_artifact_digest: str
    claim_statement_digest: str
    patch: InsertionPatchEnvelopeV1
    confirmation_observation: InsertionConfirmationObservationV1 | None = None
    citation_id: str | None = None
    successor_proposal_id: str | None = None
    successor_candidate_ref: str | None = None
    successor_candidate_digest: str | None = None
    terminal_tombstone: InsertionTerminalTombstoneV1 | None = None
    expectation_digest: str

    @field_validator(
        "expectation_id",
        "original_claim_artifact_digest",
        "claim_statement_digest",
        "citation_id",
        "successor_candidate_digest",
        "expectation_digest",
    )
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            _sha256(value, label="insertion expectation digest")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "InsertionExpectationV1":
        if self.state in {"confirming", "bound"} and self.confirmation_observation is None:
            raise ValueError("confirmed insertion state requires its exact observation")
        if self.state in {"awaiting_claim_acceptance", "pending", "expired", "abandoned"} and (
            self.confirmation_observation is not None
        ):
            raise ValueError("unconfirmed insertion state cannot carry an observation")
        successor_values = (
            self.successor_proposal_id,
            self.successor_candidate_ref,
        )
        if any(value is not None for value in successor_values) != all(
            value is not None for value in successor_values
        ):
            raise ValueError("insertion successor proposal handles are all-or-none")
        if self.successor_candidate_digest is not None and any(
            value is None for value in successor_values
        ):
            raise ValueError("insertion candidate digest requires its proposal handles")
        terminal = self.state in {
            "bound",
            "expired",
            "abandoned",
            "claim_currency_changed",
        }
        if terminal != (self.terminal_tombstone is not None):
            raise ValueError("terminal insertion state requires exactly one tombstone")
        if self.state == "bound" and self.citation_id is None:
            raise ValueError("bound insertion expectation requires citation_id")
        if self.expectation_digest != insertion_expectation_digest(self):
            raise ValueError("insertion expectation digest does not reproduce")
        return self


def insertion_expectation_digest(expectation: InsertionExpectationV1) -> str:
    payload = expectation.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("expectation_digest")
    return typed_digest(
        Sha256Value,
        INSERTION_EXPECTATION_DIGEST_DOMAIN,
        payload,
    ).tagged


def build_insertion_expectation(**values: object) -> InsertionExpectationV1:
    provisional = InsertionExpectationV1.model_construct(
        **cast(dict[str, Any], values),
        expectation_digest="sha256:" + "0" * 64,
    )
    return InsertionExpectationV1.model_validate(
        {
            **values,
            "expectation_digest": insertion_expectation_digest(provisional),
        }
    )


def update_insertion_expectation(
    expectation: InsertionExpectationV1,
    **changes: object,
) -> InsertionExpectationV1:
    values = {
        name: getattr(expectation, name)
        for name in type(expectation).model_fields
        if name not in {"tag", "expectation_digest"}
    }
    values.update(changes)
    return build_insertion_expectation(**values)


def insertion_confirmation_operation_key(
    expectation_id: str,
    observation: InsertionConfirmationObservationV1,
) -> str:
    return typed_digest(
        Sha256Value,
        INSERTION_CONFIRM_OPERATION_DOMAIN,
        {
            "expectation_id": expectation_id,
            "observation_digest": insertion_confirmation_observation_digest(observation),
        },
    ).tagged


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


def build_preflight_certificate(**values: object) -> PreflightCertificateV1:
    """Build the self-digesting frozen certificate without weakening validation."""

    typed_values = cast(dict[str, Any], values)
    provisional = PreflightCertificateV1.model_construct(
        **typed_values,
        certificate_digest="sha256:" + "0" * 64,
    )
    return PreflightCertificateV1.model_validate(
        {
            **values,
            "certificate_digest": preflight_certificate_digest(provisional),
        }
    )


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
    base_coordinate: AcceptedCoordinate
    semantic_identity: str
    payload: AuthoringPayloadV1
    payload_digest: str
    create_fingerprint: str
    intent_revision: int = Field(default=0, ge=0)
    last_preflight: PreflightResultV1 | None = None
    candidate_status: CandidateStatusV1
    insertion_expectation: InsertionExpectationV1 | None = None

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
            if self.insertion_expectation is not None:
                if self.payload.insertion_target is None:
                    raise ValueError("insertion expectation requires an insertion target")
                if self.insertion_expectation.claim_identity != self.semantic_identity:
                    raise ValueError("insertion expectation names another Claim identity")
                expected_id = insertion_expectation_id(
                    instance_id=self.instance_id,
                    intent_id=self.intent_id,
                    intent_revision=self.intent_revision,
                )
                if self.insertion_expectation.expectation_id != expected_id:
                    raise ValueError("insertion expectation ID does not reproduce")
        elif self.semantic_identity != f"Procedure:{self.payload.definition['name']}":
            raise ValueError("Procedure AuthoringIntent identity differs from its definition")
        elif self.insertion_expectation is not None:
            raise ValueError("Procedure AuthoringIntent cannot own an insertion expectation")
        return self


class AuthoringIntentV2(AuthoringIntentV1):
    """V1 intent state plus coordinate assertions that never enter authoring identity."""

    tag: Literal["playbill-authoring-intent-v2"] = "playbill-authoring-intent-v2"  # type: ignore[assignment]
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]

    @field_validator("reference_expectations")
    @classmethod
    def _reference_expectations(
        cls,
        value: tuple[AuthoringReferenceExpectationV1, ...],
    ) -> tuple[AuthoringReferenceExpectationV1, ...]:
        return canonical_reference_expectations(value)


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


class AuthoringIntentCreateRequestV2(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-create-request-v2"] = (
        "playbill-authoring-intent-create-request-v2"
    )
    payload: AuthoringPayloadV1
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]

    @field_validator("reference_expectations")
    @classmethod
    def _reference_expectations(
        cls,
        value: tuple[AuthoringReferenceExpectationV1, ...],
    ) -> tuple[AuthoringReferenceExpectationV1, ...]:
        return canonical_reference_expectations(value)


class AuthoringIntentCompileRequestV2(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-compile-request-v2"] = (
        "playbill-authoring-intent-compile-request-v2"
    )
    payload: AuthoringPayloadV1
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]
    intent_id: str | None = None

    @field_validator("reference_expectations")
    @classmethod
    def _reference_expectations(
        cls,
        value: tuple[AuthoringReferenceExpectationV1, ...],
    ) -> tuple[AuthoringReferenceExpectationV1, ...]:
        return canonical_reference_expectations(value)


class AuthoringIntentCreateRequestV3(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-create-request-v3"] = (
        "playbill-authoring-intent-create-request-v3"
    )
    payload: AuthoringPayloadV1
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]
    program_stamp: AuthoringProgramStampV1

    @field_validator("reference_expectations")
    @classmethod
    def _reference_expectations(
        cls,
        value: tuple[AuthoringReferenceExpectationV1, ...],
    ) -> tuple[AuthoringReferenceExpectationV1, ...]:
        return canonical_reference_expectations(value)


class AuthoringIntentCompileRequestV3(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-compile-request-v3"] = (
        "playbill-authoring-intent-compile-request-v3"
    )
    payload: AuthoringPayloadV1
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]
    program_stamp: AuthoringProgramStampV1
    intent_id: str | None = None

    @field_validator("reference_expectations")
    @classmethod
    def _reference_expectations(
        cls,
        value: tuple[AuthoringReferenceExpectationV1, ...],
    ) -> tuple[AuthoringReferenceExpectationV1, ...]:
        return canonical_reference_expectations(value)


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


class InsertionConfirmRequestV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-confirm-request-v1"] = "playbill-insertion-confirm-request-v1"
    observation: InsertionConfirmationObservationV1


class InsertionConfirmResultV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-confirm-result-v1"] = "playbill-insertion-confirm-result-v1"
    outcome: InsertionConfirmOutcome
    intent: AuthoringIntentV1
    expectation: InsertionExpectationV1
    successor_status: CandidateStatusV1 | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> "InsertionConfirmResultV1":
        needs_status = self.outcome in {
            "backing_candidate_pending",
            "backing_candidate_refused",
        }
        if needs_status != (self.successor_status is not None):
            raise ValueError("insertion confirm successor status disagrees with its outcome")
        return self


class InsertionAbandonRequestV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-abandon-request-v1"] = "playbill-insertion-abandon-request-v1"


class InsertionAbandonResultV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-abandon-result-v1"] = "playbill-insertion-abandon-result-v1"
    intent: AuthoringIntentV1
    expectation: InsertionExpectationV1


__all__ = [
    "AUTHORING_CANDIDATE_TREE_DIGEST_DOMAIN",
    "AUTHORING_CREATE_FINGERPRINT_DOMAIN",
    "AUTHORING_FRONTIER_DIGEST_DOMAIN",
    "AUTHORING_INSTANCE_DESCRIPTOR_DIGEST_DOMAIN",
    "AUTHORING_INTENT_ID_RE",
    "AUTHORING_PAYLOAD_DIGEST_DOMAIN",
    "AUTHORING_PREFLIGHT_CERTIFICATE_DIGEST_DOMAIN",
    "AUTHORING_PROGRAM_DIGEST_DOMAIN",
    "AUTHORING_PROGRAM_STAMP_OPERATION_DOMAIN",
    "AUTHORING_REFERENCE_EXPECTATIONS_DIGEST_DOMAIN",
    "AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST",
    "AUTHORING_SDK_VERSION",
    "AUTHORING_RESOLVED_DIGEST_DOMAIN",
    "INSERTION_CONFIRMATION_OBSERVATION_DIGEST_DOMAIN",
    "INSERTION_CONFIRM_OPERATION_DOMAIN",
    "INSERTION_EXPECTATION_DIGEST_DOMAIN",
    "INSERTION_EXPECTATION_ID_DOMAIN",
    "INSERTION_PATCH_ENVELOPE_DIGEST_DOMAIN",
    "INSERTION_RESULT_KEY_DOMAIN",
    "INSERTION_TARGET_DIGEST_DOMAIN",
    "INSERTION_TERMINAL_TOMBSTONE_DIGEST_DOMAIN",
    "AcceptanceConditionV1",
    "AuthoringArtifactReferenceV1",
    "AuthoringClaimStatementV1",
    "AuthoringDiagnosticV1",
    "AuthoringExactContentObjectV1",
    "AuthoringIntentCompileRequestV2",
    "AuthoringIntentCompileRequestV3",
    "AuthoringIntentCompileRequestV1",
    "AuthoringIntentCreateRequestV2",
    "AuthoringIntentCreateRequestV3",
    "AuthoringIntentCreateRequestV1",
    "AuthoringIntentListV1",
    "AuthoringIntentPreflightRequestV1",
    "AuthoringIntentSubmitRequestV1",
    "AuthoringIntentV1",
    "AuthoringIntentV2",
    "AuthoringIntentViewV1",
    "AuthoringPayloadV1",
    "AuthoringProgramOperationV1",
    "AuthoringProgramStampV1",
    "AuthoringReferenceExpectationV1",
    "AuthoringReferenceKind",
    "AuthoringReferenceSuccessorV1",
    "AuthoringSubmitResultV1",
    "BlockedCheckV1",
    "CandidateStatusState",
    "CandidateStatusV1",
    "ClaimAuthoringPayloadV1",
    "ClaimAuthoringPayloadV2",
    "ClaimDependencyDraftsV1",
    "DiagnosticFrontierLimitsV1",
    "DiagnosticFrontierV1",
    "InsertionAbandonRequestV1",
    "InsertionAbandonResultV1",
    "InsertionAnchorWindowV1",
    "InsertionConfirmationObservationV1",
    "InsertionConfirmOutcome",
    "InsertionConfirmRequestV1",
    "InsertionConfirmResultV1",
    "InsertionExpectationState",
    "InsertionExpectationV1",
    "InsertionOperation",
    "InsertionPatchEnvelopeV1",
    "InsertionTargetV1",
    "InsertionTerminalTombstoneV1",
    "PreflightCertificateV1",
    "PreflightResultV1",
    "ProcedureAuthoringPayloadV1",
    "ProcedureAuthoringPayloadV2",
    "RepairAlternativeV1",
    "SelfSourceBodyV1",
    "WorkingAnchorWindowV1",
    "WorkingDigestCoordinateV1",
    "WorkingGitBlobCoordinateV1",
    "WorkingSelectionObservationV1",
    "authoring_create_fingerprint",
    "authoring_payload_digest",
    "authoring_program_digest",
    "authoring_program_stamp_operation_key",
    "canonical_reference_expectations",
    "build_insertion_expectation",
    "build_insertion_patch_envelope",
    "build_insertion_terminal_tombstone",
    "build_preflight_certificate",
    "insertion_confirmation_observation_digest",
    "insertion_confirmation_operation_key",
    "insertion_expectation_digest",
    "insertion_expectation_id",
    "insertion_patch_envelope_digest",
    "insertion_result_key",
    "insertion_target_digest",
    "insertion_terminal_tombstone_digest",
    "preflight_certificate_digest",
    "reference_expectations_digest",
    "update_insertion_expectation",
]
