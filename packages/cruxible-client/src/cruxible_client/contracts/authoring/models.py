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
    model_serializer,
    model_validator,
)

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_IDENTITY,
    ApprovalPolicyV1,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.candidates import validate_candidate_timestamp
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import (
    ClaimRetireDependentV1,
    ClaimRetirementReason,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_path,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBackingV1,
    ProjectionBlockStampV1,
    ProjectionMarkerSummaryV1,
)
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_IDENTITY,
    ProcedureRuntimePolicyV1,
)
from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContractV1
from cruxible_client.contracts.procedures.models import ProcedureHardCapsV3
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.proposal_models import (
    CHANGE_SET_RATIONALE_MAX_LENGTH,
    AuthenticatedActor,
    ProposalReceiveLimits,
    validate_change_set_rationale,
)
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_client.contracts.repairs import ServedRepairV1, served_repair_for_refusal
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    PlaybillWorkspaceAdvertisement,
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
    "sha256:75432134b8c36e8ee1c55291a9222b65b82396bb222b8950c5dca64cec0fd6b8"
)
INSERTION_EXPECTATION_ID_DOMAIN = "playbill-insertion-expectation-id-v1"
INSERTION_RESULT_KEY_DOMAIN = "playbill-insertion-result-key-v1"
INSERTION_TARGET_V2_DIGEST_DOMAIN = "playbill-insertion-target-v2"
INSERTION_EXPECTATION_V2_DIGEST_DOMAIN = "playbill-insertion-expectation-v2"
INSERTION_PREPARATION_V2_DIGEST_DOMAIN = "playbill-publication-preparation-v2"
INSERTION_SOURCE_OBSERVATION_V2_DIGEST_DOMAIN = "playbill-publication-source-observation-v2"
INSERTION_CONFIRMATION_OBSERVATION_V2_DIGEST_DOMAIN = (
    "playbill-insertion-confirmation-observation-v2"
)
INSERTION_TERMINAL_TOMBSTONE_V2_DIGEST_DOMAIN = "playbill-insertion-terminal-tombstone-v2"
INSERTION_PREPARE_OPERATION_V2_DOMAIN = "playbill-insertion-prepare-operation-v2"
_INSERTION_PREPARE_TERMINAL_OPERATION_V2_DOMAIN = "playbill-insertion-prepare-terminal-operation-v2"
INSERTION_CONFIRM_OPERATION_V2_DOMAIN = "playbill-insertion-confirm-operation-v2"
PUBLICATION_BLOCK_ID_DOMAIN = "playbill-publication-block-id-v1"
MAX_PUBLICATION_SOURCE_BYTES = 4 * 1024 * 1024

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
    selected_occurrence: int | None = Field(default=None, ge=1)

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
        if (
            self.selected_occurrence is not None
            and self.selected_occurrence > self.observed_occurrence_count
        ):
            raise ValueError("selected occurrence exceeds the observed occurrence count")
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
    def _internal_correspondence(self) -> "WorkingSelectionObservationV1":
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
            if isinstance(self.coordinate, WorkingDigestCoordinateV1) and (
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


def _insertion_source_id(value: str) -> str:
    if not _CANONICAL_NAME_RE.fullmatch(value):
        raise ValueError("insertion source_id must be stable, locator-free, and canonical")
    return value


class InsertionTargetV2(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-target-v2"] = "playbill-insertion-target-v2"
    source_id: str
    coordinate: WorkingSelectionCoordinateV1
    initial_preimage_digest: str
    initial_preimage_byte_length: int = Field(ge=0, le=MAX_PUBLICATION_SOURCE_BYTES)
    selector: InsertionAnchorWindowV1
    operation: InsertionOperation

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        return _insertion_source_id(value)

    @field_validator("initial_preimage_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="insertion initial whole-source digest")

    @model_validator(mode="after")
    def _target_shape(self) -> "InsertionTargetV2":
        if self.coordinate.source_byte_length != self.initial_preimage_byte_length:
            raise ValueError("insertion initial preimage length differs from its coordinate")
        if isinstance(self.coordinate, WorkingDigestCoordinateV1) and (
            self.coordinate.source_content_digest != self.initial_preimage_digest
        ):
            raise ValueError("insertion initial preimage differs from its coordinate")
        # Reuse v1's offset/occurrence laws without its obsolete postimage arithmetic.
        source_length = self.coordinate.source_byte_length
        selector = self.selector
        if selector.end_byte > source_length or selector.insertion_offset > source_length:
            raise ValueError("insertion target exceeds the proposer-observed source")
        if selector.observed_occurrence_count != 1:
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


def insertion_target_v2_digest(target: InsertionTargetV2) -> str:
    payload = target.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, INSERTION_TARGET_V2_DIGEST_DOMAIN, payload).tagged


class PublicationSourceObservationV2(_StrictAuthoringModel):
    tag: Literal["playbill-publication-source-observation-v2"] = (
        "playbill-publication-source-observation-v2"
    )
    source_id: str
    content_base64: str
    content_digest: str
    byte_length: int = Field(ge=0, le=MAX_PUBLICATION_SOURCE_BYTES)

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        return _insertion_source_id(value)

    @field_validator("content_base64")
    @classmethod
    def _content(cls, value: str) -> str:
        content = _canonical_base64(value, label="publication source content")
        if len(content) > MAX_PUBLICATION_SOURCE_BYTES:
            raise ValueError("publication source exceeds its 4 MiB byte ceiling")
        return value

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="publication source digest")

    @model_validator(mode="after")
    def _correspondence(self) -> "PublicationSourceObservationV2":
        content = self.content
        if len(content) != self.byte_length:
            raise ValueError("publication source length does not reproduce")
        if "sha256:" + hashlib.sha256(content).hexdigest() != self.content_digest:
            raise ValueError("publication source digest does not reproduce")
        return self

    @property
    def content(self) -> bytes:
        return _canonical_base64(self.content_base64, label="publication source content")


def publication_source_observation_v2_digest(value: PublicationSourceObservationV2) -> str:
    payload = value.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        INSERTION_SOURCE_OBSERVATION_V2_DIGEST_DOMAIN,
        payload,
    ).tagged


def insertion_expectation_id(
    *,
    instance_id: str,
    intent_id: str,
    intent_revision: int,
    member_identity: str | None = None,
) -> str:
    """Name one publication expectation inside one intent revision.

    A change set may publish several Claims at once, so the ID takes the member
    that owns it. A singular Claim intent owns exactly one, and its preimage
    stays the three-field preimage it has always been so its already-minted
    expectation IDs still reproduce.
    """

    preimage: dict[str, object] = {
        "instance_id": instance_id,
        "intent_id": intent_id,
        "intent_revision": intent_revision,
    }
    if member_identity is not None:
        preimage["member_identity"] = member_identity
    return typed_digest(
        Sha256Value,
        INSERTION_EXPECTATION_ID_DOMAIN,
        preimage,
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


class ExistingCaptureCitationSourceV1(_StrictAuthoringModel):
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
    WorkingSelectionObservationV1 | SelfSourceBodyV1,
    Field(discriminator="tag"),
]

ClaimAuthoringSourceV3 = Annotated[
    WorkingSelectionObservationV1 | SelfSourceBodyV1 | ExistingCaptureCitationSourceV1,
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
    insertion_target: InsertionTargetV2 | None = None

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
        if isinstance(
            self.source,
            WorkingSelectionObservationV1 | ExistingCaptureCitationSourceV1,
        ):
            if self.citation_role is None:
                raise ValueError("Flow A and existing Captures require an explicit citation_role")
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


class ClaimAuthoringPayloadV3(ClaimAuthoringPayloadV1):
    tag: Literal["playbill-claim-authoring-payload-v3"] = "playbill-claim-authoring-payload-v3"  # type: ignore[assignment]
    source: ClaimAuthoringSourceV3  # type: ignore[assignment]
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


class AuthoringCandidateReferenceV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-candidate-reference-v1"] = (
        "playbill-authoring-candidate-reference-v1"
    )
    role: str
    target: ArtifactIdentity
    resolution: Literal["candidate_in_change_set"] = "candidate_in_change_set"


class SubjectAuthoringPayloadV1(_StrictAuthoringModel):
    tag: Literal["playbill-subject-authoring-payload-v1"] = "playbill-subject-authoring-payload-v1"
    subject: SubjectShell


class QueryDefinitionAuthoringPayloadV1(_StrictAuthoringModel):
    tag: Literal["playbill-query-definition-authoring-payload-v1"] = (
        "playbill-query-definition-authoring-payload-v1"
    )
    query_definition: QueryDefinitionV1


class ApprovalPolicyAuthoringPayloadV1(_StrictAuthoringModel):
    tag: Literal["playbill-approval-policy-authoring-payload-v1"] = (
        "playbill-approval-policy-authoring-payload-v1"
    )
    approval_policy: ApprovalPolicyV1


class ProcedureRuntimePolicyAuthoringPayloadV1(_StrictAuthoringModel):
    tag: Literal["playbill-procedure-runtime-policy-authoring-payload-v1"] = (
        "playbill-procedure-runtime-policy-authoring-payload-v1"
    )
    procedure_runtime_policy: ProcedureRuntimePolicyV1


class ProcedureMandateAuthoringPayloadV1(_StrictAuthoringModel):
    """Decision-only grant input; lowering owns exact Procedure/predecessor digests."""

    tag: Literal["playbill-procedure-mandate-authoring-payload-v1"] = (
        "playbill-procedure-mandate-authoring-payload-v1"
    )
    name: str
    procedure_name: str
    rung: Literal[2, 3]
    authority_ceiling: ProcedureHardCapsV3
    namespace: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    retire: bool = False


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


class ProcedureAuthoringPayloadV2(_StrictAuthoringModel):
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
    owned_contracts: tuple[ProcedureOwnedContractV1, ...]
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


class ClaimTypeAuthoringPayloadV1(_StrictAuthoringModel):
    """One whole ClaimType definition authored inside an ordinary change set.

    A succession -- a ClaimType that names a predecessor, and the migration its
    whole reverse-pin closure then owes -- is `ClaimTypeSuccessionMemberV1`, a
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


class ClaimTypeSuccessionDependentV1(_StrictAuthoringModel):
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
    `playbill.claim_type.invalidation_deprecated` warning; change-set lowering
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
        # `ClaimTypeDependentDispositionV3`, which refuses a naive value, and a
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
    def _disposition_shape(self) -> "ClaimTypeSuccessionDependentV1":
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


class ClaimTypeSuccessionMemberV1(_StrictAuthoringModel):
    """One ClaimType succession, its whole closure disposed, as one member.

    Evolving a committed vocabulary is one epistemic move -- "I need this
    distinction, and here is everything it changes" -- so it is one member of
    one change set, and it admits or refuses with the Claims that speak the new
    vocabulary rather than days after them.

    `successor` is a whole ClaimType that names its predecessor by identity and
    pins its exact digest, which is what makes it a succession rather than the
    definition `ClaimTypeAuthoringPayloadV1` carries. `dependents` is the exact
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
    dependents: tuple[ClaimTypeSuccessionDependentV1, ...] = ()

    @field_validator("successor")
    @classmethod
    def _successor(cls, value: ClaimType) -> ClaimType:
        if value.lifecycle.state != "live":
            # The standalone migration route accepts a byte-identical retiring
            # successor without comment, so an author who knows one road did not
            # know the other: the refusal now says which road takes it.
            raise ValueError(
                "a ClaimType succession installs a live successor; retire a ClaimType "
                "through `cruxible playbill claim-type migrate`, which is the road that "
                "takes a retiring successor"
            )
        if value.lifecycle.predecessor_digest is None:
            raise ValueError(
                "a ClaimType succession member names the predecessor it succeeds; "
                "author a new ClaimType as a claim_type member"
            )
        return value

    @model_validator(mode="after")
    def _ordered_dependents(self) -> "ClaimTypeSuccessionMemberV1":
        identities = tuple(item.identity.qualified for item in self.dependents)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("succession dependents must be UTF-8 byte-sorted and unique")
        return self

    @property
    def predicate(self) -> str:
        return self.successor.predicate


class ClaimRetirementMemberV1(_StrictAuthoringModel):
    """One attributed Claim retirement, closure and all, as a change-set member.

    `mode` is `submit` alone: a change-set member is authored inside an intent
    whose own preflight already reports the closure this member still owes, so
    the second, member-local preflight mode of the standalone retirement route
    would only name the same inventory twice.

    `claim_ref` is the bare Claim ID, spelled exactly as
    `ClaimAuthoringPayloadV1.claim_ref` spells it. Tolerating a `Claim:` prefix
    here would give two spellings of one retirement the same member identity but
    different payload digests, so create-dedup would miss and two live intents
    could carry one semantic identity.
    """

    tag: Literal["playbill-claim-retirement-authoring-payload-v1"] = (
        "playbill-claim-retirement-authoring-payload-v1"
    )
    mode: Literal["submit"] = "submit"
    claim_ref: str
    reason: ClaimRetirementReason
    effective_until: datetime | None = None
    dependents: tuple[ClaimRetireDependentV1, ...] = ()

    @field_validator("claim_ref")
    @classmethod
    def _claim_ref(cls, value: str) -> str:
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
    def _ordered_dependents(self) -> "ClaimRetirementMemberV1":
        identities = tuple(item.artifact_identity.qualified for item in self.dependents)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("retirement dependents must be UTF-8 byte-sorted and unique")
        return self

    @property
    def claim_id(self) -> str:
        return self.claim_ref


AuthoringChangeSetMemberV1: TypeAlias = Annotated[
    ClaimAuthoringPayloadV1
    | ClaimAuthoringPayloadV2
    | ClaimAuthoringPayloadV3
    | ClaimTypeAuthoringPayloadV1
    | ClaimTypeSuccessionMemberV1
    | ClaimRetirementMemberV1
    | SubjectAuthoringPayloadV1
    | QueryDefinitionAuthoringPayloadV1
    | ApprovalPolicyAuthoringPayloadV1
    | ProcedureRuntimePolicyAuthoringPayloadV1
    | ProcedureMandateAuthoringPayloadV1
    | ProcedureAuthoringPayloadV1
    | ProcedureAuthoringPayloadV2,
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

    if payload.claim_ref is not None:
        return f"Claim:{payload.claim_ref}"
    statement = payload.statement.model_dump(mode="json")
    statement.pop("tag")
    digest = typed_digest(
        Sha256Value,
        AUTHORING_CLAIM_MEMBER_IDENTITY_DIGEST_DOMAIN,
        statement,
    ).tagged.removeprefix("sha256:")
    return f"Claim:@{digest}"


def authoring_member_identity(payload: AuthoringChangeSetMemberV1) -> str:
    if isinstance(payload, ClaimAuthoringPayloadV1):
        return authoring_claim_member_identity(payload)
    if isinstance(payload, ClaimTypeAuthoringPayloadV1):
        return f"ClaimType:{payload.claim_type.predicate}"
    if isinstance(payload, ClaimTypeSuccessionMemberV1):
        return f"ClaimTypeSuccession:{payload.predicate}"
    if isinstance(payload, ClaimRetirementMemberV1):
        return f"ClaimRetirement:{payload.claim_id}"
    if isinstance(payload, SubjectAuthoringPayloadV1):
        return f"Subject:{payload.subject.subject_kind}/{payload.subject.subject_id}"
    if isinstance(payload, QueryDefinitionAuthoringPayloadV1):
        return payload.query_definition.identity.qualified
    if isinstance(payload, ApprovalPolicyAuthoringPayloadV1):
        return APPROVAL_POLICY_IDENTITY
    if isinstance(payload, ProcedureRuntimePolicyAuthoringPayloadV1):
        return PROCEDURE_RUNTIME_POLICY_IDENTITY
    if isinstance(payload, ProcedureMandateAuthoringPayloadV1):
        return f"ProcedureMandate:{payload.name}"
    return f"Procedure:{payload.definition['name']}"


def authoring_change_set_membership(
    members: tuple[AuthoringChangeSetMemberV1, ...],
) -> tuple[tuple[str, str], ...]:
    identities = tuple(authoring_member_identity(member) for member in members)
    return tuple((identity.partition(":")[0], identity) for identity in identities)


class ChangeSetAuthoringPayloadV1(_StrictAuthoringModel):
    tag: Literal["playbill-change-set-authoring-payload-v1"] = (
        "playbill-change-set-authoring-payload-v1"
    )
    # One authoring intent is one changeset, so the builder that carries eighty
    # members must also carry one: a two-member floor made the SDK's uniform
    # `pb.changes(...)` path refuse exactly the smallest set an author writes
    # first, and pushed them back onto a second, singular surface to say it.
    members: tuple[AuthoringChangeSetMemberV1, ...] = Field(min_length=1)
    # Why this set exists, in the author's own words. It was already an argument
    # to `pb.changes(rationale=...)` and it was already hashed into the program
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
        value: tuple[AuthoringChangeSetMemberV1, ...],
    ) -> tuple[AuthoringChangeSetMemberV1, ...]:
        identities = tuple(authoring_member_identity(member) for member in value)
        if len(set(identities)) != len(identities):
            raise ValueError("change-set member identities must be unique")
        if identities != tuple(sorted(identities, key=lambda item: item.encode("utf-8"))):
            raise ValueError("change-set members must be sorted by semantic identity")
        return value


AuthoringPayloadV1 = Annotated[
    ClaimAuthoringPayloadV1
    | ClaimAuthoringPayloadV2
    | ClaimAuthoringPayloadV3
    | ProcedureAuthoringPayloadV1
    | ProcedureAuthoringPayloadV2
    | SubjectAuthoringPayloadV1
    | QueryDefinitionAuthoringPayloadV1
    | ApprovalPolicyAuthoringPayloadV1
    | ProcedureRuntimePolicyAuthoringPayloadV1
    | ProcedureMandateAuthoringPayloadV1
    | ChangeSetAuthoringPayloadV1,
    Field(discriminator="tag"),
]


def authoring_payload_digest(payload: AuthoringPayloadV1) -> str:
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
    if isinstance(payload, ChangeSetAuthoringPayloadV1):
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


InsertionExpectationStateV2: TypeAlias = Literal[
    "awaiting_claim_acceptance",
    "pending",
    "prepared",
    "bound",
    "expired",
    "abandoned",
    "claim_currency_changed",
]


def publication_block_id(expectation_id: str) -> str:
    _sha256(expectation_id, label="publication expectation ID")
    digest = typed_digest(
        Sha256Value,
        PUBLICATION_BLOCK_ID_DOMAIN,
        {"expectation_id": expectation_id},
    ).tagged.removeprefix("sha256:")
    return "pub-" + digest[:32]


class PublicationPreparationV2(_StrictAuthoringModel):
    tag: Literal["playbill-publication-preparation-v2"] = "playbill-publication-preparation-v2"
    expectation_id: str
    revision: int = Field(ge=1)
    accepted_coordinate: AcceptedCoordinate
    accepted_generation: int = Field(ge=0)
    source_id: str
    rebased_selector: InsertionAnchorWindowV1
    operation: InsertionOperation
    body_digest: str
    body_byte_length: int = Field(ge=0, le=MAX_PUBLICATION_SOURCE_BYTES)
    block_id: str
    stamp: ProjectionBlockStampV1
    inserted_block_digest: str
    inserted_block_byte_length: int = Field(ge=0, le=MAX_PUBLICATION_SOURCE_BYTES)
    block_start_byte: int = Field(ge=0)
    block_end_byte: int = Field(ge=0)
    body_start_byte: int = Field(ge=0)
    body_end_byte: int = Field(ge=0)
    target_digest: str
    expires_at: datetime
    preparation_digest: str

    @field_validator(
        "expectation_id",
        "body_digest",
        "inserted_block_digest",
        "target_digest",
        "preparation_digest",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        return _sha256(value, label="publication preparation digest")

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        return _insertion_source_id(value)

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
    def _shape(self) -> "PublicationPreparationV2":
        if self.block_id != publication_block_id(self.expectation_id):
            raise ValueError("publication block ID does not reproduce")
        if self.stamp.source_id != self.source_id or self.stamp.block_id != self.block_id:
            raise ValueError("publication stamp differs from its source or block")
        if not (
            self.block_start_byte
            <= self.body_start_byte
            <= self.body_end_byte
            <= self.block_end_byte
        ):
            raise ValueError("publication block/body spans are malformed")
        if self.body_end_byte - self.body_start_byte != self.body_byte_length:
            raise ValueError("publication body span length does not reproduce")
        if self.block_end_byte - self.block_start_byte != self.inserted_block_byte_length:
            raise ValueError("publication block span length does not reproduce")
        if self.preparation_digest != publication_preparation_v2_digest(self):
            raise ValueError("publication preparation digest does not reproduce")
        return self


def publication_preparation_v2_digest(value: PublicationPreparationV2) -> str:
    payload = value.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("preparation_digest")
    return typed_digest(Sha256Value, INSERTION_PREPARATION_V2_DIGEST_DOMAIN, payload).tagged


def build_publication_preparation_v2(**values: object) -> PublicationPreparationV2:
    provisional = PublicationPreparationV2.model_construct(
        **cast(dict[str, Any], values),
        preparation_digest="sha256:" + "0" * 64,
    )
    return PublicationPreparationV2.model_validate(
        {
            **values,
            "preparation_digest": publication_preparation_v2_digest(provisional),
        }
    )


class InsertionConfirmationObservationV2(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-confirmation-observation-v2"] = (
        "playbill-insertion-confirmation-observation-v2"
    )
    intent_id: str
    expectation_id: str
    preparation_digest: str
    source_id: str
    marker_summary: ProjectionMarkerSummaryV1
    observed_occurrence_count: int = Field(ge=0)

    @field_validator("expectation_id", "preparation_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _sha256(value, label="publication confirmation digest")

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        return _insertion_source_id(value)


def insertion_confirmation_observation_v2_digest(
    value: InsertionConfirmationObservationV2,
) -> str:
    payload = value.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        INSERTION_CONFIRMATION_OBSERVATION_V2_DIGEST_DOMAIN,
        payload,
    ).tagged


class InsertionTerminalTombstoneV2(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-terminal-tombstone-v2"] = (
        "playbill-insertion-terminal-tombstone-v2"
    )
    result_key: str
    intent_id: str
    expectation_id: str
    final_state: Literal["bound", "expired", "abandoned", "claim_currency_changed"]
    preparation_digest: str | None = None
    source_id: str | None = None
    block_id: str | None = None
    accepted_claim_identity: str
    accepted_claim_artifact_digest: str
    accepted_claim_coordinate: AcceptedCoordinate | None = None
    finalized_at: datetime
    retain_until: datetime
    tombstone_digest: str

    @field_validator(
        "result_key",
        "expectation_id",
        "preparation_digest",
        "accepted_claim_artifact_digest",
        "tombstone_digest",
    )
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is not None:
            _sha256(value, label="publication tombstone digest")
        return value

    @field_validator("finalized_at", "retain_until")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("finalized_at", "retain_until", when_used="json")
    def _serialize_times(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @model_validator(mode="after")
    def _shape(self) -> "InsertionTerminalTombstoneV2":
        if self.retain_until < self.finalized_at:
            raise ValueError("publication tombstone retention precedes finalization")
        commitments = (
            self.preparation_digest,
            self.source_id,
            self.block_id,
        )
        if self.final_state == "bound" and not all(item is not None for item in commitments):
            raise ValueError("bound publication tombstone requires exact source commitments")
        if self.final_state != "bound" and any(item is not None for item in commitments):
            raise ValueError("unbound publication tombstone cannot claim applied source bytes")
        if self.final_state == "bound" and self.accepted_claim_coordinate is None:
            raise ValueError("bound publication tombstone requires its accepted Claim coordinate")
        if self.tombstone_digest != insertion_terminal_tombstone_v2_digest(self):
            raise ValueError("publication tombstone digest does not reproduce")
        return self


def insertion_terminal_tombstone_v2_digest(value: InsertionTerminalTombstoneV2) -> str:
    payload = value.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("tombstone_digest")
    return typed_digest(
        Sha256Value,
        INSERTION_TERMINAL_TOMBSTONE_V2_DIGEST_DOMAIN,
        payload,
    ).tagged


def build_insertion_terminal_tombstone_v2(**values: object) -> InsertionTerminalTombstoneV2:
    provisional = InsertionTerminalTombstoneV2.model_construct(
        **cast(dict[str, Any], values),
        tombstone_digest="sha256:" + "0" * 64,
    )
    return InsertionTerminalTombstoneV2.model_validate(
        {
            **values,
            "tombstone_digest": insertion_terminal_tombstone_v2_digest(provisional),
        }
    )


class InsertionExpectationV2(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-expectation-v2"] = "playbill-insertion-expectation-v2"
    expectation_id: str
    state: InsertionExpectationStateV2
    claim_identity: str
    original_claim_artifact_digest: str
    claim_statement_digest: str
    accepted_claim_coordinate: AcceptedCoordinate | None = None
    target: InsertionTargetV2
    preparation: PublicationPreparationV2 | None = None
    expires_at: datetime
    terminal_tombstone: InsertionTerminalTombstoneV2 | None = None
    expectation_digest: str

    @field_validator(
        "expectation_id",
        "original_claim_artifact_digest",
        "claim_statement_digest",
        "expectation_digest",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        return _sha256(value, label="publication expectation digest")

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
    def _shape(self) -> "InsertionExpectationV2":
        if self.state == "awaiting_claim_acceptance" and self.accepted_claim_coordinate is not None:
            raise ValueError("awaiting publication cannot claim an accepted Claim coordinate")
        if self.state in {"pending", "prepared", "bound"} and (
            self.accepted_claim_coordinate is None
        ):
            raise ValueError("accepted publication state requires its Claim coordinate")
        if self.state == "prepared" and self.preparation is None:
            raise ValueError("prepared publication state requires exact preparation")
        terminal = self.state in {
            "bound",
            "expired",
            "abandoned",
            "claim_currency_changed",
        }
        if terminal != (self.terminal_tombstone is not None):
            raise ValueError("terminal publication state requires exactly one tombstone")
        if self.state == "bound" and self.preparation is None:
            raise ValueError("bound publication state requires its preparation")
        if self.preparation is not None:
            if self.accepted_claim_coordinate != self.preparation.accepted_coordinate:
                raise ValueError("publication preparation names another accepted Claim coordinate")
            if self.preparation.expires_at != self.expires_at:
                raise ValueError("publication preparation changes the expectation expiry")
        if self.terminal_tombstone is not None:
            if self.terminal_tombstone.expectation_id != self.expectation_id:
                raise ValueError("publication tombstone names another expectation")
            if self.terminal_tombstone.final_state != self.state:
                raise ValueError("publication tombstone disagrees with its terminal state")
            if self.terminal_tombstone.accepted_claim_coordinate != self.accepted_claim_coordinate:
                raise ValueError("publication tombstone changes the accepted Claim coordinate")
        if self.expectation_digest != insertion_expectation_v2_digest(self):
            raise ValueError("publication expectation digest does not reproduce")
        return self


def insertion_expectation_v2_digest(value: InsertionExpectationV2) -> str:
    payload = value.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("expectation_digest")
    return typed_digest(Sha256Value, INSERTION_EXPECTATION_V2_DIGEST_DOMAIN, payload).tagged


def build_insertion_expectation_v2(**values: object) -> InsertionExpectationV2:
    provisional = InsertionExpectationV2.model_construct(
        **cast(dict[str, Any], values),
        expectation_digest="sha256:" + "0" * 64,
    )
    return InsertionExpectationV2.model_validate(
        {**values, "expectation_digest": insertion_expectation_v2_digest(provisional)}
    )


def update_insertion_expectation_v2(
    expectation: InsertionExpectationV2,
    **changes: object,
) -> InsertionExpectationV2:
    values = {
        name: getattr(expectation, name)
        for name in type(expectation).model_fields
        if name not in {"tag", "expectation_digest"}
    }
    values.update(changes)
    return build_insertion_expectation_v2(**values)


def insertion_prepare_operation_v2_key(
    expectation_id: str,
    observation: PublicationSourceObservationV2,
    *,
    live_expectation_digest: str,
) -> str:
    _sha256(live_expectation_digest, label="live publication expectation digest")
    return typed_digest(
        Sha256Value,
        INSERTION_PREPARE_OPERATION_V2_DOMAIN,
        {
            "expectation_id": expectation_id,
            "live_expectation_digest": live_expectation_digest,
            "observation_digest": publication_source_observation_v2_digest(observation),
        },
    ).tagged


def insertion_prepare_terminal_operation_v2_key(
    expectation_id: str,
    observation: PublicationSourceObservationV2,
) -> str:
    """Key one terminal prepare attempt independently of the state it terminalizes."""

    return typed_digest(
        Sha256Value,
        _INSERTION_PREPARE_TERMINAL_OPERATION_V2_DOMAIN,
        {
            "expectation_id": expectation_id,
            "observation_digest": publication_source_observation_v2_digest(observation),
        },
    ).tagged


def insertion_confirm_operation_v2_key(
    expectation_id: str,
    observation: InsertionConfirmationObservationV2,
) -> str:
    return typed_digest(
        Sha256Value,
        INSERTION_CONFIRM_OPERATION_V2_DOMAIN,
        {
            "expectation_id": expectation_id,
            "observation_digest": insertion_confirmation_observation_v2_digest(observation),
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


class ChangeSetClaimIdentityV1(_StrictAuthoringModel):
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
    payload: AuthoringPayloadV1
    payload_digest: str
    create_fingerprint: str
    intent_revision: int = Field(default=0, ge=0)
    last_preflight: PreflightResultV1 | None = None
    candidate_status: CandidateStatusV1
    # A singular Claim intent carries its one expectation in both fields; a
    # change set carries one per publishing Claim member in the plural field and
    # nothing in the singular one, because no single expectation is "the" one.
    insertion_expectation: InsertionExpectationV2 | None = None
    insertion_expectations: tuple[InsertionExpectationV2, ...] = ()
    change_set_claim_identities: tuple[ChangeSetClaimIdentityV1, ...] = ()

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
        if isinstance(self.payload, ChangeSetAuthoringPayloadV1):
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
                if self.insertion_expectation.target != self.payload.insertion_target:
                    raise ValueError("publication expectation changes its frozen target")
            expected_plural = (
                () if self.insertion_expectation is None else (self.insertion_expectation,)
            )
            if self.insertion_expectations != expected_plural:
                raise ValueError("a singular Claim intent carries its one expectation in both")
        else:
            if isinstance(self.payload, ChangeSetAuthoringPayloadV1):
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
            if self.insertion_expectation is not None:
                raise ValueError("non-Claim AuthoringIntent cannot own an insertion expectation")
            if not isinstance(self.payload, ChangeSetAuthoringPayloadV1):
                if self.insertion_expectations:
                    raise ValueError("only a Claim member can own a publication expectation")
                if self.change_set_claim_identities:
                    raise ValueError("only a change set owns per-member Claim identities")
            else:
                self._bind_change_set_members(
                    self.payload,
                    member_identities=tuple(identity for _kind, identity in membership),
                )
        return self

    def _bind_change_set_members(
        self,
        payload: "ChangeSetAuthoringPayloadV1",
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
            if member.claim_ref is not None and member.claim_ref != claim_id:
                raise ValueError("a revising Claim member keeps the lineage it names")
        expectations = self.insertion_expectations
        expectation_ids = tuple(item.expectation_id for item in expectations)
        if expectation_ids != tuple(sorted(set(expectation_ids), key=lambda item: item.encode())):
            raise ValueError("publication expectations must be ID-sorted and unique")
        published = {
            by_member[identity]: (identity, member)
            for identity, member in claim_members.items()
            if member.insertion_target is not None
        }
        for expectation in expectations:
            named = published.get(expectation.claim_identity)
            if named is None:
                raise ValueError("publication expectation names no publishing Claim member")
            member_identity, member = named
            expected_id = insertion_expectation_id(
                instance_id=self.instance_id,
                intent_id=self.intent_id,
                intent_revision=self.intent_revision,
                member_identity=member_identity,
            )
            if expectation.expectation_id != expected_id:
                raise ValueError("insertion expectation ID does not reproduce")
            if expectation.target != member.insertion_target:
                raise ValueError("publication expectation changes its frozen target")


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


# Response wrappers must retain the fields selected by the nested intent tag.
_AuthoringIntentResponse: TypeAlias = Annotated[
    AuthoringIntentV1 | AuthoringIntentV2, Field(discriminator="tag")
]


class AuthoringIntentViewV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-view-v1"] = "playbill-authoring-intent-view-v1"
    intent: _AuthoringIntentResponse


class AuthoringIntentListV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-intent-list-v1"] = "playbill-authoring-intent-list-v1"
    intents: tuple[_AuthoringIntentResponse, ...]


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


class AuthoringSubmitMemberV1(_StrictAuthoringModel):
    """What one submitted member became, so a set says it once per member."""

    tag: Literal["playbill-authoring-submit-member-v1"] = "playbill-authoring-submit-member-v1"
    identity: str
    artifact_digest: str
    predecessor_digest: str | None = None
    identity_stable: bool = False
    claim_revision: int | None = None


class AuthoringSubmitResultV1(_StrictAuthoringModel):
    tag: Literal["playbill-authoring-submit-result-v1"] = "playbill-authoring-submit-result-v1"
    intent: _AuthoringIntentResponse
    status: CandidateStatusV1
    workspace_advertisement: PlaybillWorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT
    # A `revises` submit amends one Claim identity in place rather than adding a
    # second Claim, and nothing in the result said so: the caller saw an ordinary
    # submit and had to re-read the artifact to learn the identity was reused.
    # `claim_revision` is the revision this candidate becomes once accepted.
    identity_stable: bool = False
    claim_revision: int | None = None
    # One intent is one changeset, so the same two facts are reported per member.
    # The singular pair above stays the singular Claim intent's answer.
    members: tuple[AuthoringSubmitMemberV1, ...] = ()

    @field_validator("members")
    @classmethod
    def _members(
        cls,
        value: tuple[AuthoringSubmitMemberV1, ...],
    ) -> tuple[AuthoringSubmitMemberV1, ...]:
        identities = tuple(item.identity for item in value)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("submit result members must be identity-sorted and unique")
        return value


class InsertionPrepareRequestV2(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-prepare-request-v2"] = "playbill-insertion-prepare-request-v2"
    observation: PublicationSourceObservationV2
    # Omitted, the intent's sole expectation is meant; a change set that
    # publishes several Claims has no sole expectation and must name one.
    expectation_id: str | None = None


class PublicationPrepareWarningV1(_StrictAuthoringModel):
    tag: Literal["playbill-publication-prepare-warning-v1"] = (
        "playbill-publication-prepare-warning-v1"
    )
    code: Literal["playbill.authoring.publication_citation_anchor_collision"] = (
        "playbill.authoring.publication_citation_anchor_collision"
    )
    source_id: str
    citation_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        return _insertion_source_id(value)

    @field_validator("citation_ids")
    @classmethod
    def _citation_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("publication warning citation IDs must be sorted and unique")
        for item in value:
            Sha256Value.from_tagged(item)
        return value


class InsertionPrepareResultV2(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-prepare-result-v2"] = "playbill-insertion-prepare-result-v2"
    outcome: Literal[
        "prepared",
        "already_prepared",
        "bound",
        "expired",
        "claim_currency_changed",
    ]
    intent: _AuthoringIntentResponse
    expectation: InsertionExpectationV2
    preparation: PublicationPreparationV2 | None = None
    inserted_block_base64: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    warnings: tuple[PublicationPrepareWarningV1, ...] = ()

    @model_validator(mode="after")
    def _preparation_shape(self) -> "InsertionPrepareResultV2":
        if self.outcome in {"prepared", "already_prepared", "bound"} and (self.preparation is None):
            raise ValueError("successful publication preparation requires exact preparation")
        if self.preparation is None:
            if self.inserted_block_base64 is not None:
                raise ValueError("rendered publication bytes require an exact preparation")
            return self
        if self.inserted_block_base64 is None:
            raise ValueError("an exact preparation requires its rendered publication bytes")
        rendered = _canonical_base64(
            self.inserted_block_base64,
            label="rendered publication block",
        )
        if (
            len(rendered) != self.preparation.inserted_block_byte_length
            or "sha256:" + hashlib.sha256(rendered).hexdigest()
            != self.preparation.inserted_block_digest
        ):
            raise ValueError("rendered publication bytes differ from their preparation")
        return self

    @field_validator("warnings")
    @classmethod
    def _warnings(
        cls, value: tuple[PublicationPrepareWarningV1, ...]
    ) -> tuple[PublicationPrepareWarningV1, ...]:
        if value != tuple(
            sorted(
                set(value),
                key=lambda item: (
                    item.code.encode("ascii"),
                    item.source_id.encode("utf-8"),
                    item.citation_ids,
                ),
            )
        ):
            raise ValueError("publication prepare warnings must be sorted and unique")
        return value


class InsertionConfirmRequestV2(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-confirm-request-v2"] = "playbill-insertion-confirm-request-v2"
    observation: InsertionConfirmationObservationV2
    expectation_id: str | None = None


class InsertionConfirmResultV2(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-confirm-result-v2"] = "playbill-insertion-confirm-result-v2"
    outcome: Literal["bound", "already_bound", "expired", "claim_currency_changed"]
    intent: _AuthoringIntentResponse
    expectation: InsertionExpectationV2


class InsertionAbandonRequestV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-abandon-request-v1"] = "playbill-insertion-abandon-request-v1"


class InsertionAbandonResultV1(_StrictAuthoringModel):
    tag: Literal["playbill-insertion-abandon-result-v1"] = "playbill-insertion-abandon-result-v1"
    intent: _AuthoringIntentResponse
    expectation: InsertionExpectationV2


PlaybillBlockSyncReadStatus: TypeAlias = Literal[
    "current",
    "successor",
    "refused",
    "unsyncable",
]
PlaybillBlockSyncReadReason: TypeAlias = Literal[
    "block_workspace_instance_mismatch",
    "block_backing_missing",
    "block_backing_changed",
    "block_backing_retired",
    "block_successor_ambiguous",
]


class PlaybillBlockSyncSuccessorCandidateV1(_StrictAuthoringModel):
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


class PlaybillBlockSyncReadRequestV1(_StrictAuthoringModel):
    tag: Literal["playbill-block-sync-read-request-v1"] = "playbill-block-sync-read-request-v1"
    stamp: ProjectionBlockStampV1
    preferred_successor_digest: str | None = None

    @field_validator("preferred_successor_digest")
    @classmethod
    def _preferred_digest(cls, value: str | None) -> str | None:
        if value is not None:
            _sha256(value, label="preferred block sync successor digest")
        return value


class PlaybillBlockSyncReadResultV1(_StrictAuthoringModel):
    """What the daemon can say about one declared block, without rendering it.

    This read used to return the accepted BODY a single-Claim block would be
    rewritten to. Nothing renders any more: a projection block is prose the
    agent wrote, held to an explicit backing list, and the only question worth
    asking of accepted state is whether that list still reads as it did. So the
    body fields stay in the shape and are never populated -- an older client
    that asks for them gets nothing rather than a rewrite it did not expect --
    and the answer is a currency verdict over EVERY held backing instead of one.

    Unpopulated, not forbidden. Turning a field that was REQUIRED on success
    into one the model refuses would invert the shape rather than narrow it: a
    payload minted before this batch would stop parsing against the model that
    describes it. A body carried in is still bound to its digest, exactly as it
    always was; the daemon simply never sends one.
    """

    tag: Literal["playbill-block-sync-read-result-v1"] = "playbill-block-sync-read-result-v1"
    status: PlaybillBlockSyncReadStatus
    original_artifact_digest: str | None = None
    artifact_digest: str | None = None
    coordinate: AcceptedCoordinate | None = None
    generation: int | None = Field(default=None, ge=0)
    backing: ProjectionBackingV1 | None = None
    # The current spelling of every held backing that moved under the stamp.
    # A block holds a LIST, so naming one is not enough to repair it.
    moved_backings: tuple[ProjectionBackingV1, ...] = ()
    body_content_base64: str | None = None
    body_digest: str | None = None
    successor_candidates: tuple[PlaybillBlockSyncSuccessorCandidateV1, ...] = ()
    reason: PlaybillBlockSyncReadReason | None = None
    detail: str | None = None

    @field_validator("original_artifact_digest", "artifact_digest", "body_digest")
    @classmethod
    def _optional_digests(cls, value: str | None) -> str | None:
        if value is not None:
            _sha256(value, label="block sync digest")
        return value

    @field_validator("body_content_base64")
    @classmethod
    def _body_base64(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("block sync body is not canonical base64") from exc
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("block sync body base64 spelling is not canonical")
        return value

    @model_validator(mode="after")
    def _result_shape(self) -> "PlaybillBlockSyncReadResultV1":
        success = self.status in {"current", "successor"}
        if success != (self.coordinate is not None and self.generation is not None):
            raise ValueError("a block currency verdict names the coordinate it was read at")
        if (self.body_content_base64 is None) != (self.body_digest is None):
            raise ValueError("a block sync read body is named with its digest or not at all")
        if self.body_content_base64 is not None and self.body_digest is not None:
            body = base64.b64decode(self.body_content_base64, validate=True)
            if "sha256:" + hashlib.sha256(body).hexdigest() != self.body_digest:
                raise ValueError("block sync retained body does not reproduce its digest")
        if success and (self.reason is not None or self.successor_candidates):
            raise ValueError("successful block sync reads cannot carry a refusal")
        if not success and self.reason is None:
            raise ValueError("refused block sync reads require a typed reason")
        if (self.status == "successor") != bool(self.moved_backings):
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

    @property
    def body(self) -> bytes | None:
        if self.body_content_base64 is None:
            return None
        return base64.b64decode(self.body_content_base64, validate=True)


# `synced` and `would_sync` name the one thing this verb still writes towards
# agreement. Nothing renders a block body, so no body is ever rewritten; what
# `--accept-local` does instead is re-stamp the block on the prose already in the
# page, and `synced` says the two now agree because this call made them agree
# (`would_sync` is the same, previewed under `--check`). The members were kept
# through the convergence removal because narrowing a result enum is a wire
# removal; they carry the surviving write.
PlaybillBlockSyncOutcome: TypeAlias = Literal[
    "unchanged",
    "stale",
    "dirty",
    "synced",
    "would_sync",
    "detached",
    "would_detach",
    "skipped",
    "refused",
    "unsyncable",
]
# Three members below carry no producer any more, and stay for the same reason
# `synced` and `would_sync` do: narrowing a served vocabulary is a wire removal,
# and the deprecate-then-remove policy governs it. They are not consequences of
# the one sanctioned removal -- the held-list rules retired
# `block_multi_backing` and `block_query_backing`, and
# `workspace_source_catalog_missing` never had a producer at all -- so they are
# deprecated rather than silently dropped. A caller holding a result minted
# before this batch still parses it, and the repair each one names is the rule
# that was in force when it could still be produced.
#
# The removal is a COMMITMENT, not a sentiment: all three carry a
# `DEPRECATIONS.md` row in the read-only-member form, deprecated in 0.5.1 and
# removed in 0.6.0. No `DEPRECATION_REGISTRY` entry, and deliberately so -- the
# registry exists to emit a structured warning on a transport, and nothing can
# emit one for an enum member no producer ever writes. The schedule row is the
# whole of the commitment, exactly as it is for `GroupStatus 'auto_resolved'`
# and `OperationType 'group_clear'`.
PlaybillBlockSyncReason: TypeAlias = Literal[
    "workspace_not_attached",
    "workspace_binding_invalid",
    "workspace_instance_mismatch",
    "workspace_source_catalog_invalid",
    "workspace_source_catalog_missing",
    "source_path_invalid",
    "source_not_projection_target",
    "block_marker_malformed",
    "block_unstamped",
    "block_locally_modified",
    "block_multi_backing",
    "block_query_backing",
    "block_backing_missing",
    "block_backing_changed",
    "block_backing_retired",
    "block_successor_ambiguous",
    "block_concurrent_edit",
    "block_frame_invalid",
    "block_sync_failed",
]


class PlaybillBlockSyncItemV1(_StrictAuthoringModel):
    tag: Literal["playbill-block-sync-item-v1"] = "playbill-block-sync-item-v1"
    path: str
    source_id: str | None = None
    block_id: str | None = None
    outcome: PlaybillBlockSyncOutcome
    reason: PlaybillBlockSyncReason | None = None
    # The prose ``repair_commands`` this replaced were free strings a caller had
    # to parse; the structured carrier names the served operation and its
    # arguments, and a producer that carries none projects the declared repair
    # its typed reason resolves to.
    repair: ServedRepairV1 | None = None
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
    def _item_shape(self) -> "PlaybillBlockSyncItemV1":
        # `stale` and `dirty` are findings, not refusals, but they are just as
        # reasoned: a block whose held list moved names which reason moved it,
        # and a block whose prose moved names that. Every one of them carries a
        # repair, because a finding with no named change is a row nobody acts on.
        reasoned = self.outcome in {"skipped", "refused", "unsyncable", "stale", "dirty"}
        if reasoned != (self.reason is not None):
            raise ValueError("block sync skipped/refusal outcomes require exactly one typed reason")
        if reasoned != (self.repair is not None):
            raise ValueError("block sync refusal outcomes carry exactly one structured repair")
        return self


class PlaybillBlockSyncResultV1(_StrictAuthoringModel):
    tag: Literal["playbill-block-sync-result-v1"] = "playbill-block-sync-result-v1"
    items: tuple[PlaybillBlockSyncItemV1, ...]
    changed_file_count: int = Field(ge=0)
    would_change: bool
    has_refusals: bool

    @model_validator(mode="after")
    def _summary_shape(self) -> "PlaybillBlockSyncResultV1":
        changed = {item.path for item in self.items if item.outcome in {"synced", "detached"}}
        prospective = any(
            item.outcome in {"synced", "would_sync", "detached", "would_detach"}
            for item in self.items
        )
        # A stale held list and a hand-edited body are findings this verb
        # reports and cannot repair, so they count as refusals for the exit
        # code: `block sync` at the end of an activation must not answer clean
        # over a page that has drifted from the state it declares.
        refused = any(
            item.outcome in {"refused", "unsyncable", "stale", "dirty"} for item in self.items
        )
        if self.changed_file_count != len(changed):
            raise ValueError("block sync changed-file count does not reproduce")
        if self.would_change != prospective or self.has_refusals != refused:
            raise ValueError("block sync summary flags do not reproduce")
        return self


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
    "INSERTION_EXPECTATION_ID_DOMAIN",
    "INSERTION_RESULT_KEY_DOMAIN",
    "INSERTION_CONFIRMATION_OBSERVATION_V2_DIGEST_DOMAIN",
    "INSERTION_CONFIRM_OPERATION_V2_DOMAIN",
    "INSERTION_EXPECTATION_V2_DIGEST_DOMAIN",
    "INSERTION_PREPARATION_V2_DIGEST_DOMAIN",
    "INSERTION_PREPARE_OPERATION_V2_DOMAIN",
    "INSERTION_SOURCE_OBSERVATION_V2_DIGEST_DOMAIN",
    "INSERTION_TARGET_V2_DIGEST_DOMAIN",
    "INSERTION_TERMINAL_TOMBSTONE_V2_DIGEST_DOMAIN",
    "MAX_PUBLICATION_SOURCE_BYTES",
    "PUBLICATION_BLOCK_ID_DOMAIN",
    "AcceptanceConditionV1",
    "AuthoringArtifactReferenceV1",
    "AuthoringCandidateReferenceV1",
    "AuthoringChangeSetMemberV1",
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
    "AuthoringSubmitMemberV1",
    "AuthoringSubmitResultV1",
    "BlockedCheckV1",
    "CandidateStatusState",
    "CandidateStatusV1",
    "ClaimAuthoringPayloadV1",
    "ClaimAuthoringPayloadV2",
    "ClaimAuthoringPayloadV3",
    "ChangeSetAuthoringPayloadV1",
    "ChangeSetClaimIdentityV1",
    "ClaimRetirementMemberV1",
    "ClaimTypeAuthoringPayloadV1",
    "ClaimTypeSuccessionDependentV1",
    "ClaimTypeSuccessionDisposition",
    "ClaimTypeSuccessionMemberV1",
    "ClaimAuthoringSourceV3",
    "ClaimDependencyDraftsV1",
    "DiagnosticFrontierLimitsV1",
    "DiagnosticFrontierV1",
    "InsertionAbandonRequestV1",
    "InsertionAbandonResultV1",
    "InsertionAnchorWindowV1",
    "InsertionConfirmationObservationV2",
    "InsertionConfirmRequestV2",
    "InsertionConfirmResultV2",
    "InsertionExpectationStateV2",
    "InsertionExpectationV2",
    "InsertionOperation",
    "InsertionTargetV2",
    "InsertionTerminalTombstoneV2",
    "InsertionPrepareRequestV2",
    "InsertionPrepareResultV2",
    "PublicationPreparationV2",
    "PublicationPrepareWarningV1",
    "PublicationSourceObservationV2",
    "PlaybillBlockSyncItemV1",
    "PlaybillBlockSyncOutcome",
    "PlaybillBlockSyncReadReason",
    "PlaybillBlockSyncReadRequestV1",
    "PlaybillBlockSyncReadResultV1",
    "PlaybillBlockSyncReadStatus",
    "PlaybillBlockSyncReason",
    "PlaybillBlockSyncResultV1",
    "PlaybillBlockSyncSuccessorCandidateV1",
    "PreflightCertificateV1",
    "PreflightResultV1",
    "ProcedureAuthoringPayloadV1",
    "ProcedureAuthoringPayloadV2",
    "ApprovalPolicyAuthoringPayloadV1",
    "ProcedureRuntimePolicyAuthoringPayloadV1",
    "ProcedureMandateAuthoringPayloadV1",
    "QueryDefinitionAuthoringPayloadV1",
    "SubjectAuthoringPayloadV1",
    "RepairAlternativeV1",
    "ExistingCaptureCitationSourceV1",
    "SelfSourceBodyV1",
    "WorkingAnchorWindowV1",
    "WorkingDigestCoordinateV1",
    "WorkingGitBlobCoordinateV1",
    "WorkingSelectionObservationV1",
    "authoring_create_fingerprint",
    "authoring_change_set_membership",
    "authoring_claim_member_identity",
    "authoring_member_identity",
    "authoring_payload_digest",
    "authoring_program_digest",
    "authoring_program_stamp_operation_key",
    "canonical_reference_expectations",
    "build_insertion_expectation_v2",
    "build_insertion_terminal_tombstone_v2",
    "build_publication_preparation_v2",
    "build_preflight_certificate",
    "insertion_confirmation_observation_v2_digest",
    "insertion_confirm_operation_v2_key",
    "insertion_expectation_id",
    "insertion_expectation_v2_digest",
    "insertion_result_key",
    "insertion_target_v2_digest",
    "insertion_terminal_tombstone_v2_digest",
    "insertion_prepare_operation_v2_key",
    "insertion_prepare_terminal_operation_v2_key",
    "publication_block_id",
    "publication_preparation_v2_digest",
    "publication_source_observation_v2_digest",
    "preflight_certificate_digest",
    "reference_expectations_digest",
    "update_insertion_expectation_v2",
]
