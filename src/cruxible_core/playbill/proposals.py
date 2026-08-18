"""Authenticated proposal admission and deterministic PB-C candidate evaluation."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from cruxible_core.playbill.acquisition_policies import (
    AcceptedSourceAcquisitionPolicyV1,
    SourceAcquisitionPolicyError,
    acquisition_policy_digest,
    evaluate_acquisition_policy_law,
    parse_acquisition_policy,
)
from cruxible_core.playbill.actor_context import TransportCapability
from cruxible_core.playbill.attestations import (
    ApprovalSubmission,
    approval_digest,
    approval_statement_bytes,
)
from cruxible_core.playbill.authoring_profiles import (
    AuthoringProfileError,
    ClaimTypeExpansionEvidenceV1,
    verify_claim_type_expansion_evidence,
)
from cruxible_core.playbill.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    CandidateRecord,
    CandidateRecordV2,
    ClosureProofV2,
    LawEvaluationCoordinateV1,
    MemberLawEvaluationV2,
    SemanticCandidate,
    candidate_digest,
    member_law_evidence_digest,
    render_candidate_record,
    validate_candidate_timestamp,
)
from cruxible_core.playbill.canonical import (
    CandidateDigest,
    ProposalDigest,
    SemanticDiffDigest,
    Sha256Value,
    canonical_bytes,
    canonical_digest,
    file_digest,
    manifest_root,
    normalize_manifest_paths,
    semantic_diff,
    semantic_projection,
    typed_digest,
)
from cruxible_core.playbill.captures import (
    AcceptedCaptureContract,
    CaptureFormatError,
    CaptureObjectStoreProtocol,
    capture_contract_digest,
    evaluate_capture_contract_law,
    parse_capture_contract,
)
from cruxible_core.playbill.claim_attestations import (
    accepted_referent_coordinates_from_tree,
)
from cruxible_core.playbill.claim_types import (
    AcceptedClaimType,
    ClaimType,
    ClaimTypeFormatError,
    claim_type_digest,
    evaluate_claim_type_law,
    parse_claim_type,
)
from cruxible_core.playbill.claims import (
    AcceptedClaim,
    ClaimFormatError,
    ExactContentClaimObject,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_statement_address,
    claim_statement_digest,
    evaluate_claim_law,
    parse_claim,
)
from cruxible_core.playbill.closure import (
    dependency_artifacts,
    evaluate_dependency_closure,
)
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.discovery import (
    DiscoveryHintsV1,
    DistinctRelationMemberV1,
    ProposedSemanticInterfaceV1,
    ReuseDispositionV1,
    SemanticReuseInterfaceV1,
    VocabularyReuseRequestV1,
    evaluate_vocabulary_reuse,
)
from cruxible_core.playbill.documents import (
    AcceptedDocument,
    BodyVerifierProtocol,
    document_digest,
    evaluate_document_law,
    parse_document,
)
from cruxible_core.playbill.errors import (
    DocumentFormatError,
    ProposalAdmissionError,
    ProposalIntegrityError,
    SubjectFormatError,
)
from cruxible_core.playbill.exhaust.promotions import (
    AcceptedExhaustPromotionV1,
    ExhaustPromotionError,
    ExhaustPromotionLawResultV1,
    ExhaustPromotionV1,
    evaluate_exhaust_promotion_acceptance,
    exhaust_promotion_digest,
    parse_exhaust_promotion,
)
from cruxible_core.playbill.governance import (
    ActivationPolicy,
    ApprovalRequirement,
    MutationDisposition,
    PermissionTier,
)
from cruxible_core.playbill.laws import PLAYBILL_ACCEPTANCE_LAWS
from cruxible_core.playbill.policies import (
    AdmissionActorV1,
    ClaimAdmissionCandidateContextV1,
    ClaimAdmissionPolicyV1,
    evaluate_claim_admission_candidate,
)
from cruxible_core.playbill.principal_lifecycle import evaluate_principal_lifecycle
from cruxible_core.playbill.principals import principal_registry_from_tree
from cruxible_core.playbill.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureFormatError,
    evaluate_procedure_law,
    parse_procedure,
    procedure_artifact_digest,
)
from cruxible_core.playbill.procedures.line_specs import (
    AcceptedLineSpecV1,
    LineSpecFormatError,
    evaluate_line_spec_law,
    line_spec_digest,
    parse_line_spec,
)
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.providers import (
    AcceptedProviderV1,
    ProviderFormatError,
    ProviderV1,
    evaluate_provider_law,
    parse_provider,
    provider_digest,
)
from cruxible_core.playbill.query.definitions import (
    AcceptedQueryDefinitionV1,
    QueryDefinitionFormatError,
    evaluate_query_definition_law,
    parse_query_definition,
    query_definition_digest,
)
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_catalog import SourceCompilationManifest
from cruxible_core.playbill.standing_mandates import (
    AcceptedStandingMandateV1,
    StandingMandateError,
    evaluate_standing_mandate_law,
    parse_standing_mandate,
    standing_mandate_digest,
)
from cruxible_core.playbill.subjects import (
    AcceptedSubject,
    evaluate_subject_law,
    parse_subject,
    subject_digest,
)
from cruxible_core.playbill.types import GitObjectFormat

_ACTOR_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_PROPOSAL_REF_RE = re.compile(r"^refs/proposals/[a-z][a-z0-9_.-]{0,127}/[a-z][a-z0-9_.-]{0,127}$")
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DOCUMENT_PATH_RE = re.compile(r"^documents/[a-z][a-z0-9_.-]{0,255}\.yaml$")
_PRINCIPAL_PATH_RE = re.compile(r"^principals/[a-z][a-z0-9_.-]{0,127}\.yaml$")
_SUBJECT_PATH_RE = re.compile(
    r"^subjects/[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*/"
    r"[a-z][a-z0-9_.-]{0,255}\.yaml$"
)
_CLAIM_TYPE_PATH_RE = re.compile(
    r"^claim-types/[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*/"
    r"[a-z][a-z0-9_]{0,63}\.yaml$"
)
_CAPTURE_CONTRACT_PATH_RE = re.compile(r"^capture-contracts/[a-z][a-z0-9_.-]{0,255}\.yaml$")
_PROVIDER_PATH_RE = re.compile(r"^providers/[a-z][a-z0-9_.-]{0,255}\.yaml$")
_SOURCE_ACQUISITION_POLICY_PATH_RE = re.compile(
    r"^source-acquisition-policies/[a-z][a-z0-9_.-]{0,255}\.yaml$"
)
_STANDING_MANDATE_PATH_RE = re.compile(r"^standing-mandates/[a-z][a-z0-9_.-]{0,255}\.yaml$")
_CLAIM_PATH_RE = re.compile(r"^claims/[0-9a-f]{2}/CLM-[0-9a-f]{32}\.yaml$")
_PROCEDURE_PATH_RE = re.compile(r"^procedures/[a-z][a-z0-9_.-]{0,255}\.yaml$")
_LINE_PATH_RE = re.compile(r"^lines/[a-z][a-z0-9_.-]{0,255}\.yaml$")
_QUERY_DEFINITION_PATH_RE = re.compile(r"^query-definitions/[a-z][a-z0-9_.-]{0,255}\.yaml$")
_EXHAUST_PROMOTION_PATH_RE = re.compile(r"^exhaust-promotions/[a-z][a-z0-9_.-]{0,255}\.yaml$")
_LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
_EvidenceModelT = TypeVar("_EvidenceModelT", bound=BaseModel)


class _StrictProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExhaustPromotionVerifierProtocol(Protocol):
    """Operational verification seam shared by proposal, settlement, and recovery."""

    def verify_promotion(
        self,
        promotion: ExhaustPromotionV1,
    ) -> ExhaustPromotionLawResultV1: ...


class AuthenticatedActor(_StrictProposalModel):
    """Identity established by the daemon's authentication boundary."""

    tag: Literal["playbill-authenticated-actor-v1"] = "playbill-authenticated-actor-v1"
    actor_id: str
    capabilities: tuple[TransportCapability, ...] = ("propose",)

    @field_validator("actor_id")
    @classmethod
    def _actor_id(cls, value: str) -> str:
        if not _ACTOR_RE.fullmatch(value):
            raise ValueError("authenticated actor_id is not canonical")
        return value

    @field_validator("capabilities")
    @classmethod
    def _capabilities(
        cls,
        value: tuple[TransportCapability, ...],
    ) -> tuple[TransportCapability, ...]:
        if tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))) != value:
            raise ValueError("transport capabilities must be sorted and unique")
        return value


class ProposalReceiveLimits(_StrictProposalModel):
    max_files: int = Field(default=10_000, ge=1, le=1_000_000)
    max_file_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=2**40)
    max_total_bytes: int = Field(default=64 * 1024 * 1024, ge=1, le=2**44)


class ProposalAdmissionRequest(_StrictProposalModel):
    tag: Literal["playbill-proposal-request-v1"] = "playbill-proposal-request-v1"
    target_ref: str
    proposed_base_oid: str
    source_compilation_digest: str | None = None
    claim_type_expansions: tuple[ClaimTypeExpansionEvidenceV1, ...] = ()

    @field_validator("target_ref")
    @classmethod
    def _target_ref(cls, value: str) -> str:
        if not _PROPOSAL_REF_RE.fullmatch(value):
            raise ValueError("target_ref must be a canonical namespaced proposal ref")
        return value

    @field_validator("proposed_base_oid")
    @classmethod
    def _proposed_base_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("proposed_base_oid is malformed")
        return value

    @field_validator("source_compilation_digest")
    @classmethod
    def _source_compilation_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("claim_type_expansions")
    @classmethod
    def _claim_type_expansions(
        cls,
        value: tuple[ClaimTypeExpansionEvidenceV1, ...],
    ) -> tuple[ClaimTypeExpansionEvidenceV1, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("ClaimType expansion evidence must be sorted and unique")
        digests = tuple(item.expanded_artifact_digest for item in value)
        if len(digests) != len(set(digests)):
            raise ValueError("ClaimType expansion evidence must be unique by expanded artifact")
        return value


class ProposalAdmissionRecord(_StrictProposalModel):
    tag: Literal["playbill-proposal-admission-v1"] = "playbill-proposal-admission-v1"
    proposal_id: str
    actor_id: str
    target_ref: str
    proposed_base_oid: str
    candidate_commit_oid: str
    candidate_tree_oid: str
    source_compilation_digest: str | None
    claim_type_expansions: tuple[ClaimTypeExpansionEvidenceV1, ...] = ()
    limits: ProposalReceiveLimits
    admitted_at: str

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id(cls, value: str) -> str:
        ProposalDigest.from_tagged(value)
        return value

    @field_validator("actor_id")
    @classmethod
    def _actor_id(cls, value: str) -> str:
        return AuthenticatedActor(actor_id=value).actor_id

    @field_validator("target_ref")
    @classmethod
    def _target_ref(cls, value: str) -> str:
        if not _PROPOSAL_REF_RE.fullmatch(value):
            raise ValueError("proposal admission target_ref is malformed")
        return value

    @field_validator("proposed_base_oid", "candidate_commit_oid", "candidate_tree_oid")
    @classmethod
    def _oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("proposal admission Git OID is malformed")
        return value

    @field_validator("source_compilation_digest")
    @classmethod
    def _compilation_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("admitted_at")
    @classmethod
    def _admitted_at(cls, value: str) -> str:
        return validate_candidate_timestamp(value)

    @model_validator(mode="after")
    def _namespace_binding(self) -> "ProposalAdmissionRecord":
        if self.target_ref.split("/")[2] != self.actor_id:
            raise ValueError("admission target namespace differs from authenticated actor")
        if (
            len(
                {
                    len(self.proposed_base_oid),
                    len(self.candidate_commit_oid),
                    len(self.candidate_tree_oid),
                }
            )
            != 1
        ):
            raise ValueError("proposal admission mixes Git object formats")
        return self


class ProposalEvaluationRecord(_StrictProposalModel):
    tag: Literal["playbill-proposal-evaluation-v1"] = "playbill-proposal-evaluation-v1"
    proposal_id: str
    verdict: Literal["candidate", "refused"]
    evaluated_base_oid: str
    evaluated_tree_oid: str | None
    rebased: bool
    candidate_digest: str | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()
    evaluated_at: str

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id(cls, value: str) -> str:
        ProposalDigest.from_tagged(value)
        return value

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str | None) -> str | None:
        if value is not None:
            CandidateDigest.from_tagged(value)
        return value

    @field_validator("evaluated_base_oid", "evaluated_tree_oid")
    @classmethod
    def _oid(cls, value: str | None) -> str | None:
        if value is not None and not _OID_RE.fullmatch(value):
            raise ValueError("proposal evaluation Git OID is malformed")
        return value

    @field_validator("evaluated_at")
    @classmethod
    def _evaluated_at(cls, value: str) -> str:
        return validate_candidate_timestamp(value)

    @model_validator(mode="after")
    def _verdict_shape(self) -> "ProposalEvaluationRecord":
        if self.verdict == "candidate":
            if self.candidate_digest is None or self.evaluated_tree_oid is None or self.diagnostics:
                raise ValueError("candidate evaluation record is incomplete")
        elif self.candidate_digest is not None:
            raise ValueError("refused evaluation cannot carry a candidate digest")
        if self.evaluated_tree_oid is not None and len(self.evaluated_tree_oid) != len(
            self.evaluated_base_oid
        ):
            raise ValueError("proposal evaluation mixes Git object formats")
        return self


class ProposalResult(_StrictProposalModel):
    admission: ProposalAdmissionRecord
    evaluation: ProposalEvaluationRecord
    candidate: CandidateRecord | CandidateRecordV2 | None = None

    @model_validator(mode="after")
    def _result_shape(self) -> "ProposalResult":
        if (self.evaluation.verdict == "candidate") != (self.candidate is not None):
            raise ValueError("proposal result candidate shape differs from evaluation verdict")
        return self


class ProposalTransportProtocol(Protocol):
    def object_format(self) -> GitObjectFormat: ...
    def read_main(self) -> str: ...
    def read_tree(self, oid: str) -> dict[str, bytes]: ...
    def read_proposal_ref(self, target_ref: str) -> str | None: ...
    def create_proposal_commit(
        self,
        tree: Mapping[str, bytes],
        *,
        base_oid: str,
        target_ref: str,
        actor_id: str,
        timestamp: str,
        expected_ref_oid: str | None,
    ) -> tuple[str, str]: ...


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_canonical_write(path: Path, payload: bytes) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensive OS contract
                raise ProposalIntegrityError("proposal evidence write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ProposalIntegrityError("immutable proposal evidence path is occupied")
        return
    except OSError as exc:
        raise ProposalIntegrityError("proposal evidence could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


class ProposalEvidenceStore:
    """Immutable out-of-band proposal/candidate evidence; never accepted authority."""

    def __init__(self, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise ProposalIntegrityError("proposal evidence root must be an existing directory")
        self.root = root.resolve(strict=True)
        self.proposals = self._directory("proposals")
        self.evaluations = self._directory("evaluations")
        self.candidates = self._directory("candidates")
        self.approvals = self._directory("approvals")
        self.source_compilations = self._directory("source-compilations")

    def _directory(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ProposalIntegrityError("proposal evidence directory is not trustworthy")
        os.chmod(path, 0o700)
        return path.resolve(strict=True)

    def write_admission(self, record: ProposalAdmissionRecord) -> Path:
        path = self.proposals / f"{record.proposal_id.removeprefix('sha256:')}.json"
        _exclusive_canonical_write(path, canonical_bytes(record.model_dump(mode="json")) + b"\n")
        return path

    def write_evaluation(self, record: ProposalEvaluationRecord) -> Path:
        digest = canonical_digest(
            "playbill-proposal-evaluation-v1",
            {key: value for key, value in record.model_dump(mode="json").items() if key != "tag"},
        )
        path = self.evaluations / f"{digest}.json"
        _exclusive_canonical_write(path, canonical_bytes(record.model_dump(mode="json")) + b"\n")
        return path

    def write_candidate(self, record: CandidateRecord | CandidateRecordV2) -> Path:
        path = self.candidates / f"{record.candidate_digest.removeprefix('sha256:')}.json"
        _exclusive_canonical_write(path, render_candidate_record(record))
        return path

    def write_source_compilation(self, manifest: SourceCompilationManifest) -> Path:
        """Persist a path-free immutable compile receipt beside proposal exhaust."""

        path = self.source_compilations / (
            f"{manifest.compilation_digest.removeprefix('sha256:')}.json"
        )
        _exclusive_canonical_write(
            path,
            canonical_bytes(manifest.model_dump(mode="json")) + b"\n",
        )
        return path

    def read_source_compilation(self, compilation_digest: str) -> SourceCompilationManifest:
        Sha256Value.from_tagged(compilation_digest)
        path = self.source_compilations / f"{compilation_digest.removeprefix('sha256:')}.json"
        return self._read_model(
            path,
            SourceCompilationManifest,
            label="source compilation",
        )

    def read_admission(self, proposal_id: str) -> ProposalAdmissionRecord:
        """Read one canonical immutable admission by its public proposal ID."""

        ProposalDigest.from_tagged(proposal_id)
        path = self.proposals / f"{proposal_id.removeprefix('sha256:')}.json"
        return self._read_model(path, ProposalAdmissionRecord, label="proposal admission")

    def read_evaluation(self, proposal_id: str) -> ProposalEvaluationRecord:
        """Resolve the sole canonical evaluation recorded for one admission."""

        ProposalDigest.from_tagged(proposal_id)
        matches: list[ProposalEvaluationRecord] = []
        for path in sorted(self.evaluations.glob("*.json"), key=lambda item: item.name):
            record = self._read_model(path, ProposalEvaluationRecord, label="proposal evaluation")
            if record.proposal_id == proposal_id:
                matches.append(record)
        if len(matches) != 1:
            raise ProposalIntegrityError(
                "proposal evidence must contain exactly one evaluation for the admission"
            )
        return matches[0]

    def list_evaluations(self) -> tuple[ProposalEvaluationRecord, ...]:
        """List canonical evaluations in stable evidence-filename order."""

        return tuple(
            self._read_model(path, ProposalEvaluationRecord, label="proposal evaluation")
            for path in sorted(self.evaluations.glob("*.json"), key=lambda item: item.name)
        )

    def read_candidate(self, candidate_digest_value: str) -> CandidateRecord | CandidateRecordV2:
        """Read one canonical validated candidate by its frozen C_s digest."""

        CandidateDigest.from_tagged(candidate_digest_value)
        path = self.candidates / f"{candidate_digest_value.removeprefix('sha256:')}.json"
        if path.is_symlink() or not path.is_file():
            raise ProposalIntegrityError(
                "validated candidate evidence is missing or not a regular file"
            )
        try:
            raw = path.read_bytes()
            adapter: TypeAdapter[CandidateRecord | CandidateRecordV2] = TypeAdapter(
                CandidateRecord | CandidateRecordV2
            )
            value: CandidateRecord | CandidateRecordV2 = adapter.validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise ProposalIntegrityError("validated candidate evidence is malformed") from exc
        if render_candidate_record(value) != raw:
            raise ProposalIntegrityError("validated candidate evidence is not canonical")
        return value

    def write_approval(
        self,
        candidate_digest_value: str,
        submission: ApprovalSubmission,
    ) -> Path:
        """Persist one public approval per candidate/signer; never private material."""

        CandidateDigest.from_tagged(candidate_digest_value)
        if submission.attestation.payload_digest != candidate_digest_value:
            raise ProposalIntegrityError("approval payload differs from evidence candidate")
        candidate_directory = self.approvals / candidate_digest_value.removeprefix("sha256:")
        candidate_directory.mkdir(mode=0o700, exist_ok=True)
        if candidate_directory.is_symlink() or not candidate_directory.is_dir():
            raise ProposalIntegrityError("approval evidence directory is not trustworthy")
        os.chmod(candidate_directory, 0o700)
        path = candidate_directory / f"{submission.attestation.signer_id}.json"
        _exclusive_canonical_write(
            path,
            canonical_bytes(submission.model_dump(mode="json")) + b"\n",
        )
        return path

    def read_approvals(self, candidate_digest_value: str) -> tuple[ApprovalSubmission, ...]:
        """Return canonical public approvals in the verifier's required signer order."""

        CandidateDigest.from_tagged(candidate_digest_value)
        candidate_directory = self.approvals / candidate_digest_value.removeprefix("sha256:")
        if not candidate_directory.exists():
            return ()
        if candidate_directory.is_symlink() or not candidate_directory.is_dir():
            raise ProposalIntegrityError("approval evidence directory is not trustworthy")
        submissions = tuple(
            self._read_model(path, ApprovalSubmission, label="approval submission")
            for path in sorted(candidate_directory.glob("*.json"), key=lambda item: item.name)
        )
        signer_ids = tuple(item.attestation.signer_id for item in submissions)
        if signer_ids != tuple(sorted(set(signer_ids), key=lambda value: value.encode("utf-8"))):
            raise ProposalIntegrityError("approval evidence is not uniquely signer-ordered")
        for path, submission in zip(
            sorted(candidate_directory.glob("*.json"), key=lambda item: item.name),
            submissions,
        ):
            if path.name != f"{submission.attestation.signer_id}.json":
                raise ProposalIntegrityError("approval evidence filename differs from signer")
            if submission.attestation.payload_digest != candidate_digest_value:
                raise ProposalIntegrityError("approval evidence names another candidate")
            # Exercise the frozen preimage parser at the storage boundary; this
            # is deliberately verification-free because the historical key is
            # selected by the service/replay layer.
            approval_statement_bytes(submission.attestation)
            approval_digest(submission.attestation)
        return submissions

    @staticmethod
    def _read_model(
        path: Path,
        model: type[_EvidenceModelT],
        *,
        label: str,
    ) -> _EvidenceModelT:
        if path.is_symlink() or not path.is_file():
            raise ProposalIntegrityError(f"{label} evidence is missing or not a regular file")
        try:
            raw = path.read_bytes()
            value = model.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise ProposalIntegrityError(f"{label} evidence is malformed") from exc
        if canonical_bytes(value.model_dump(mode="json")) + b"\n" != raw:
            raise ProposalIntegrityError(f"{label} evidence is not canonical")
        return value


def validate_proposal_tree(
    tree: Mapping[str, bytes],
    *,
    limits: ProposalReceiveLimits,
    base_tree: Mapping[str, bytes] | None = None,
) -> dict[str, bytes]:
    if len(tree) > limits.max_files:
        raise ProposalAdmissionError("proposal exceeds its file-count limit")
    normalized = normalize_manifest_paths(list(tree))
    if set(normalized) != set(tree):
        raise ProposalAdmissionError("proposal paths must already be canonical")
    total = 0
    result: dict[str, bytes] = {}
    base = base_tree or {}
    for path in normalized:
        content = tree[path]
        if not isinstance(content, bytes):
            raise ProposalAdmissionError("proposal tree values must be exact bytes")
        authorable = (
            _DOCUMENT_PATH_RE.fullmatch(path)
            or _PRINCIPAL_PATH_RE.fullmatch(path)
            or _SUBJECT_PATH_RE.fullmatch(path)
            or _CLAIM_TYPE_PATH_RE.fullmatch(path)
            or _CAPTURE_CONTRACT_PATH_RE.fullmatch(path)
            or _PROVIDER_PATH_RE.fullmatch(path)
            or _SOURCE_ACQUISITION_POLICY_PATH_RE.fullmatch(path)
            or _STANDING_MANDATE_PATH_RE.fullmatch(path)
            or _CLAIM_PATH_RE.fullmatch(path)
            or _PROCEDURE_PATH_RE.fullmatch(path)
            or _LINE_PATH_RE.fullmatch(path)
            or _QUERY_DEFINITION_PATH_RE.fullmatch(path)
            or _EXHAUST_PROMOTION_PATH_RE.fullmatch(path)
        )
        if not authorable and base.get(path) != content:
            raise ProposalAdmissionError(
                f"proposal changed a daemon-controlled or unregistered path: {path}"
            )
        if len(content) > limits.max_file_bytes:
            raise ProposalAdmissionError(f"proposal blob exceeds its byte limit: {path}")
        if content.startswith(_LFS_PREFIX):
            raise ProposalAdmissionError(f"proposal refuses Git LFS pointer: {path}")
        total += len(content)
        if total > limits.max_total_bytes:
            raise ProposalAdmissionError("proposal exceeds its total-byte limit")
        result[path] = content
    for path in normalize_manifest_paths(list(base)):
        authorable = (
            _DOCUMENT_PATH_RE.fullmatch(path)
            or _PRINCIPAL_PATH_RE.fullmatch(path)
            or _SUBJECT_PATH_RE.fullmatch(path)
            or _CLAIM_TYPE_PATH_RE.fullmatch(path)
            or _CAPTURE_CONTRACT_PATH_RE.fullmatch(path)
            or _PROVIDER_PATH_RE.fullmatch(path)
            or _SOURCE_ACQUISITION_POLICY_PATH_RE.fullmatch(path)
            or _STANDING_MANDATE_PATH_RE.fullmatch(path)
            or _CLAIM_PATH_RE.fullmatch(path)
            or _PROCEDURE_PATH_RE.fullmatch(path)
            or _LINE_PATH_RE.fullmatch(path)
            or _QUERY_DEFINITION_PATH_RE.fullmatch(path)
            or _EXHAUST_PROMOTION_PATH_RE.fullmatch(path)
        )
        if not authorable and path not in result:
            raise ProposalAdmissionError(
                f"proposal removed a daemon-controlled or unregistered path: {path}"
            )
    return result


@dataclass(frozen=True)
class CandidateEvaluation:
    tree: dict[str, bytes]
    candidate: CandidateRecord | CandidateRecordV2 | None
    diagnostics: tuple[CompilerDiagnostic, ...]
    rebased: bool


def claim_type_expansions_from_candidate(
    candidate: CandidateRecord | CandidateRecordV2,
) -> tuple[ClaimTypeExpansionEvidenceV1, ...]:
    """Recover and revalidate authoring-only evidence committed by v2 law output."""

    if not isinstance(candidate, CandidateRecordV2):
        return ()
    expansions: list[ClaimTypeExpansionEvidenceV1] = []
    for evidence in candidate.law_evidence:
        raw = evidence.result.get("authoring_expansion")
        if raw is None:
            continue
        try:
            expansions.append(ClaimTypeExpansionEvidenceV1.model_validate(raw))
        except ValidationError as exc:
            raise ProposalIntegrityError(
                "candidate contains invalid ClaimType authoring expansion evidence"
            ) from exc
    return tuple(
        sorted(
            expansions,
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )


def _diagnostic(code: str, message: str, path: str | None = None) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        severity="error",
        message=message,
        subject=SemanticAddress.whole_artifact(path) if path is not None else None,
    )


def deterministic_rebase(
    *,
    base_tree: Mapping[str, bytes],
    current_tree: Mapping[str, bytes],
    proposed_tree: Mapping[str, bytes],
) -> tuple[dict[str, bytes], tuple[str, ...]]:
    """Apply the exact base→proposal delta over current or return conflicting paths."""

    paths = normalize_manifest_paths(
        list({*base_tree.keys(), *current_tree.keys(), *proposed_tree.keys()})
    )
    rebased = dict(current_tree)
    conflicts: list[str] = []
    for path in paths:
        base = base_tree.get(path)
        proposed = proposed_tree.get(path)
        if base == proposed:
            continue
        current = current_tree.get(path)
        if current != base and current != proposed:
            conflicts.append(path)
            continue
        if proposed is None:
            rebased.pop(path, None)
        else:
            rebased[path] = proposed
    return rebased, tuple(conflicts)


class RebaseMemberConflictV2(_StrictProposalModel):
    tag: Literal["playbill-rebase-member-conflict-v2"] = "playbill-rebase-member-conflict-v2"
    code: Literal["playbill.rebase.member_conflict"] = "playbill.rebase.member_conflict"
    path: str
    old_parent_digest: str | None
    proposed_digest: str | None
    new_parent_digest: str | None

    @field_validator("old_parent_digest", "proposed_digest", "new_parent_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value


class RebaseResultV2(_StrictProposalModel):
    tag: Literal["playbill-rebase-result-v2"] = "playbill-rebase-result-v2"
    tree: dict[str, bytes]
    conflicts: tuple[RebaseMemberConflictV2, ...]
    approvals_invalidated: Literal[True] = True


def _optional_file_digest(content: bytes | None) -> str | None:
    return None if content is None else file_digest(content).tagged


def deterministic_rebase_v2(
    *,
    old_parent_tree: Mapping[str, bytes],
    new_parent_tree: Mapping[str, bytes],
    proposed_tree: Mapping[str, bytes],
) -> RebaseResultV2:
    """Three-way member rebase with exact, canonically ordered conflicts."""

    paths = normalize_manifest_paths(
        list({*old_parent_tree.keys(), *new_parent_tree.keys(), *proposed_tree.keys()})
    )
    rebased = dict(new_parent_tree)
    conflicts: list[RebaseMemberConflictV2] = []
    for path in paths:
        old = old_parent_tree.get(path)
        proposed = proposed_tree.get(path)
        if old == proposed:
            continue
        new = new_parent_tree.get(path)
        if new == proposed:
            continue
        if new != old:
            conflicts.append(
                RebaseMemberConflictV2(
                    path=path,
                    old_parent_digest=_optional_file_digest(old),
                    proposed_digest=_optional_file_digest(proposed),
                    new_parent_digest=_optional_file_digest(new),
                )
            )
            continue
        if proposed is None:
            rebased.pop(path, None)
        else:
            rebased[path] = proposed
    return RebaseResultV2(tree=rebased, conflicts=tuple(conflicts))


def _canonical_model_digest(domain: str, model: BaseModel) -> str:
    payload = model.model_dump(mode="json")
    payload.pop("tag", None)
    return typed_digest(Sha256Value, domain, payload).tagged


def _reuse_interfaces(tree: Mapping[str, bytes]) -> tuple[SemanticReuseInterfaceV1, ...]:
    descriptor_terms: dict[bytes, dict[str, set[str]]] = {}

    def terms_for(address: SemanticAddress) -> dict[str, set[str]]:
        key = canonical_bytes(address.model_dump(mode="json"))
        return descriptor_terms.setdefault(
            key,
            {"aliases": set(), "tags": set(), "relations": set()},
        )

    for descriptor_path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not _CLAIM_PATH_RE.fullmatch(descriptor_path):
            continue
        descriptor = parse_claim(tree[descriptor_path], path=descriptor_path)
        if descriptor.lifecycle.state != "live":
            continue
        predicate = descriptor.statement.predicate
        if predicate in {"semantic.alias", "semantic.tag"} and isinstance(
            descriptor.statement.object, LiteralClaimObject
        ):
            value = descriptor.statement.object.value
            if not isinstance(value, str):
                continue
            field = "aliases" if predicate == "semantic.alias" else "tags"
            terms_for(descriptor.statement.subject)[field].add(value)
        elif predicate in {"semantic.related_to", "semantic.distinct_from"} and isinstance(
            descriptor.statement.object, SubjectClaimObject
        ):
            relation_label = descriptor.statement.object.address.artifact_path
            terms_for(descriptor.statement.subject)["relations"].add(relation_label)
            terms_for(descriptor.statement.object.address)["relations"].add(
                descriptor.statement.subject.artifact_path
            )

    interfaces: list[SemanticReuseInterfaceV1] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        content = tree[path]
        if _CLAIM_TYPE_PATH_RE.fullmatch(path):
            claim_type = parse_claim_type(content, path=path)
            if claim_type.lifecycle.state != "live":
                continue
            signature = typed_digest(
                Sha256Value,
                "playbill-claim-type-structural-signature-v1",
                claim_type.structure.model_dump(mode="json"),
            ).tagged
            tokens = tuple(
                sorted(
                    {
                        claim_type.predicate,
                        claim_type.predicate.rpartition(".")[2],
                    },
                    key=lambda item: item.encode("utf-8"),
                )
            )
            descriptors = terms_for(SemanticAddress.whole_artifact(path))
            interfaces.append(
                SemanticReuseInterfaceV1(
                    address=SemanticAddress.whole_artifact(path),
                    identity=claim_type.identity,
                    kind="claim-type",
                    label=claim_type.predicate,
                    canonical_tokens=tokens,
                    structural_signature_digest=signature,
                    aliases=tuple(
                        sorted(descriptors["aliases"], key=lambda item: item.encode("utf-8"))
                    ),
                    tags=tuple(sorted(descriptors["tags"], key=lambda item: item.encode("utf-8"))),
                    relation_labels=tuple(
                        sorted(descriptors["relations"], key=lambda item: item.encode("utf-8"))
                    ),
                )
            )
        elif _SUBJECT_PATH_RE.fullmatch(path):
            subject = parse_subject(content, path=path)
            if subject.lifecycle.state != "live":
                continue
            # Subject-kind similarity is not evidence that two instances are
            # duplicates. Include the stable identity in the signature.
            signature = typed_digest(
                Sha256Value,
                "playbill-subject-reuse-signature-v1",
                {"identity": subject.identity.qualified},
            ).tagged
            descriptors = terms_for(SemanticAddress.whole_artifact(path))
            interfaces.append(
                SemanticReuseInterfaceV1(
                    address=SemanticAddress.whole_artifact(path),
                    identity=subject.identity,
                    kind="subject",
                    label=subject.identity.qualified,
                    canonical_tokens=(subject.subject_id,),
                    structural_signature_digest=signature,
                    aliases=tuple(
                        sorted(descriptors["aliases"], key=lambda item: item.encode("utf-8"))
                    ),
                    tags=tuple(sorted(descriptors["tags"], key=lambda item: item.encode("utf-8"))),
                    relation_labels=tuple(
                        sorted(descriptors["relations"], key=lambda item: item.encode("utf-8"))
                    ),
                )
            )
    return tuple(interfaces)


def _claim_type_reuse_evidence(
    *,
    claim_type: ClaimType,
    path: str,
    lookup_tree: Mapping[str, bytes],
    candidate_scope: tuple[str, ...],
    current: AcceptedProjectionCoordinate,
) -> dict[str, object]:
    structure = claim_type.structure
    signature = typed_digest(
        Sha256Value,
        "playbill-claim-type-structural-signature-v1",
        structure.model_dump(mode="json"),
    ).tagged
    predicate = claim_type.predicate
    proposal = ProposedSemanticInterfaceV1(
        address=SemanticAddress.whole_artifact(path),
        identity=claim_type.identity,
        kind="claim-type",
        label=predicate,
        canonical_tokens=tuple(
            sorted(
                {predicate, predicate.rpartition(".")[2]},
                key=lambda item: item.encode("utf-8"),
            )
        ),
        structural_signature_digest=signature,
    )
    relations: list[DistinctRelationMemberV1] = []
    for relation_path in candidate_scope:
        if not _CLAIM_PATH_RE.fullmatch(relation_path):
            continue
        relation = parse_claim(lookup_tree[relation_path], path=relation_path)
        if relation.statement.predicate != "semantic.distinct_from" or not isinstance(
            relation.statement.object, SubjectClaimObject
        ):
            continue
        relations.append(
            DistinctRelationMemberV1(
                claim_address=claim_statement_address(relation_path),
                claim_artifact_digest=claim_artifact_digest(relation).tagged,
                subject=relation.statement.subject,
                object=relation.statement.object.address,
            )
        )
    evidence = evaluate_vocabulary_reuse(
        VocabularyReuseRequestV1(
            proposal=proposal,
            hints=DiscoveryHintsV1(),
            disposition=ReuseDispositionV1(kind="new_distinct"),
        ),
        accepted_interfaces=tuple(
            item for item in _reuse_interfaces(lookup_tree) if item.address.artifact_path != path
        ),
        coordinate=AcceptedCoordinate.from_internal(current),
        implementation_digest=current.compiler.rule_digest,
        distinct_relation_members=tuple(
            sorted(
                relations,
                key=lambda item: canonical_bytes(item.model_dump(mode="json")),
            )
        ),
        descriptor_claims_available=any(
            parse_claim(lookup_tree[item], path=item).statement.predicate
            in {
                "semantic.alias",
                "semantic.distinct_from",
                "semantic.related_to",
                "semantic.tag",
            }
            for item in lookup_tree
            if _CLAIM_PATH_RE.fullmatch(item)
        ),
    )
    return evidence.model_dump(mode="json")


def _member_disposition(
    *,
    predecessor_digest: str | None,
    candidate_digest_value: str | None,
    retired: bool,
) -> Literal["create", "replace", "retire", "delete"]:
    if predecessor_digest is None:
        return "create"
    if candidate_digest_value is None:
        return "delete"
    return "retire" if retired else "replace"


def _aggregate_tier(values: list[PermissionTier]) -> PermissionTier:
    order: dict[PermissionTier, int] = {
        "governed_write": 0,
        "graph_write": 1,
        "admin": 2,
    }
    return max(values, key=order.__getitem__)


def _aggregate_activation(values: list[ActivationPolicy]) -> ActivationPolicy:
    order: dict[ActivationPolicy, int] = {
        "snapshot": 0,
        "drain": 1,
        "epoch-check": 2,
        "abort": 3,
    }
    return max(values, key=order.__getitem__)


def _claim_policy_value(
    value: LiteralClaimObject | SubjectClaimObject | ExactContentClaimObject,
) -> object:
    if isinstance(value, LiteralClaimObject):
        return value.value
    return value.model_dump(mode="json")


def _effective_claim_values(
    tree: Mapping[str, bytes],
    *,
    evaluation_time: str,
) -> dict[str, dict[str, tuple[object, ...]]]:
    """Project live Claim objects by exact Subject and predicate for policy law."""

    at = datetime.strptime(evaluation_time, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    collected: dict[str, dict[str, dict[bytes, object]]] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not _CLAIM_PATH_RE.fullmatch(path):
            continue
        claim = parse_claim(tree[path], path=path)
        if claim.lifecycle.state != "live":
            continue
        statement = claim.statement
        if statement.effective_from is not None and statement.effective_from > at:
            continue
        if statement.effective_until is not None and statement.effective_until <= at:
            continue
        value = _claim_policy_value(statement.object)
        predicate_values = collected.setdefault(statement.subject.artifact_path, {}).setdefault(
            statement.predicate,
            {},
        )
        predicate_values[canonical_bytes(value)] = value
    return {
        subject_path: {
            predicate: tuple(values[key] for key in sorted(values))
            for predicate, values in sorted(
                predicates.items(),
                key=lambda item: item[0].encode("utf-8"),
            )
        }
        for subject_path, predicates in sorted(
            collected.items(),
            key=lambda item: item[0].encode("utf-8"),
        )
    }


def _policy_has_requirements(policy: ClaimAdmissionPolicyV1) -> bool:
    return any(
        (
            policy.transition_requirements,
            policy.actor_requirements,
            policy.evidence_requirements,
            policy.freeze_requirements,
        )
    )


def _lineage_creation_actor(
    current_tree: Mapping[str, bytes],
    *,
    subject_path: str,
    claim_paths: tuple[str, ...],
    candidate_creates_lineage: bool,
    actor_id: str,
) -> str | None:
    """Recover immutable creation attribution from accepted change-set history."""

    targets = {subject_path, *claim_paths}
    for path in sorted(current_tree, key=lambda item: item.encode("utf-8")):
        if not re.fullmatch(r"changesets/cs-[0-9]{20}\.json", path):
            continue
        try:
            payload = json.loads(current_tree[path])
            members = payload["members"]
            recorded_actor = payload["actor_binding"]["actor_id"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProposalIntegrityError(
                f"accepted change-set creation attribution is invalid: {path}"
            ) from exc
        if not isinstance(members, list) or not isinstance(recorded_actor, str):
            raise ProposalIntegrityError(
                f"accepted change-set creation attribution is invalid: {path}"
            )
        if any(isinstance(member, dict) and member.get("path") in targets for member in members):
            return recorded_actor
    return actor_id if candidate_creates_lineage else None


def _claim_admission_evaluations(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
    timestamp: str,
    actor_id: str,
    actor_roles: tuple[str, ...],
    subjects: Mapping[str, AcceptedSubject],
    claim_types: Mapping[str, AcceptedClaimType],
) -> tuple[
    dict[str, tuple[dict[str, object], ...]],
    dict[str, tuple[str, ...]],
    tuple[CompilerDiagnostic, ...],
]:
    """Evaluate every policy governing each Subject changed by Claim members."""

    changed_by_subject: dict[str, list[str]] = {}
    for path in scope:
        if not _CLAIM_PATH_RE.fullmatch(path) or path not in candidate_tree:
            continue
        claim = parse_claim(candidate_tree[path], path=path)
        changed_by_subject.setdefault(claim.statement.subject.artifact_path, []).append(path)
    if not changed_by_subject:
        return {}, {}, ()

    parent_values = _effective_claim_values(current_tree, evaluation_time=timestamp)
    candidate_values = _effective_claim_values(candidate_tree, evaluation_time=timestamp)
    accepted_claim_paths: dict[str, list[str]] = {}
    for path in sorted(current_tree, key=lambda item: item.encode("utf-8")):
        if not _CLAIM_PATH_RE.fullmatch(path):
            continue
        claim = parse_claim(current_tree[path], path=path)
        accepted_claim_paths.setdefault(claim.statement.subject.artifact_path, []).append(path)

    entries_by_path: dict[str, tuple[dict[str, object], ...]] = {}
    digests_by_path: dict[str, tuple[str, ...]] = {}
    diagnostics: list[CompilerDiagnostic] = []
    for subject_path, changed_paths in sorted(
        changed_by_subject.items(),
        key=lambda item: item[0].encode("utf-8"),
    ):
        subject = subjects.get(subject_path)
        if subject is None:
            continue  # Claim law emits the exact unresolved-subject diagnostic.
        applicable = tuple(
            sorted(
                (
                    item
                    for item in claim_types.values()
                    if subject.shell.subject_kind in item.claim_type.allowed_subject_kinds
                    and _policy_has_requirements(item.claim_type.admission_policy)
                ),
                key=lambda item: item.claim_type.identity.qualified.encode("utf-8"),
            )
        )
        if not applicable:
            continue
        declared_predicates = tuple(
            sorted(
                {
                    item.claim_type.predicate
                    for item in claim_types.values()
                    if subject.shell.subject_kind in item.claim_type.allowed_subject_kinds
                },
                key=lambda item: item.encode("utf-8"),
            )
        )
        lineage_actor = _lineage_creation_actor(
            current_tree,
            subject_path=subject_path,
            claim_paths=tuple(accepted_claim_paths.get(subject_path, ())),
            candidate_creates_lineage=(
                subject_path not in current_tree and subject_path in candidate_tree
            )
            or not accepted_claim_paths.get(subject_path),
            actor_id=actor_id,
        )
        context = ClaimAdmissionCandidateContextV1(
            evaluation_time=timestamp,
            declared_predicates=declared_predicates,
            parent_values=parent_values.get(subject_path, {}),
            candidate_values=candidate_values.get(subject_path, {}),
            admission_actor=AdmissionActorV1(
                actor_id=actor_id,
                roles=actor_roles,
            ),
            lineage_creation_actor_id=lineage_actor,
            # QueryDefinition execution is introduced in PC-F. Until then an
            # evidence-gated transition refuses as missing rather than trusting
            # caller-authored query output.
            query_results=(),
        )
        entries: list[dict[str, object]] = []
        policy_digests: set[str] = set()
        for accepted_type in applicable:
            policy = accepted_type.claim_type.admission_policy
            policy_digest = _canonical_model_digest(
                "playbill-claim-admission-policy-v1",
                policy,
            )
            evaluated = evaluate_claim_admission_candidate(policy, context)
            entries.append(
                {
                    "claim_type_digest": accepted_type.artifact_digest,
                    "claim_type_identity": accepted_type.claim_type.identity.qualified,
                    "policy_digest": policy_digest,
                    "candidate_result": evaluated.model_dump(mode="json"),
                }
            )
            policy_digests.add(policy_digest)
            for code in evaluated.refusal_codes:
                for changed_path in changed_paths:
                    diagnostics.append(
                        _diagnostic(
                            code,
                            "The Subject-level Claim admission policy refused "
                            "this closed change set.",
                            changed_path,
                        )
                    )
        ordered_entries = tuple(sorted(entries, key=lambda item: canonical_bytes(item)))
        ordered_digests = tuple(sorted(policy_digests))
        for changed_path in changed_paths:
            entries_by_path[changed_path] = ordered_entries
            digests_by_path[changed_path] = ordered_digests
    ordered_diagnostics = tuple(
        sorted(
            diagnostics,
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )
    return entries_by_path, digests_by_path, ordered_diagnostics


def _evaluate_v2_proposal_tree(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: dict[str, bytes],
    current: AcceptedProjectionCoordinate,
    bodies: BodyVerifierProtocol,
    timestamp: str,
    scope: tuple[str, ...],
    diff_digest: SemanticDiffDigest,
    actor_id: str | None,
    rebased: bool,
    claim_type_expansions: tuple[ClaimTypeExpansionEvidenceV1, ...],
    promotion_verifier: ExhaustPromotionVerifierProtocol | None,
) -> CandidateEvaluation:
    for path in scope:
        proposed_bytes = candidate_tree.get(path)
        if proposed_bytes is None:
            continue
        try:
            if any(
                pattern.fullmatch(path)
                for pattern in (
                    _DOCUMENT_PATH_RE,
                    _SUBJECT_PATH_RE,
                    _CLAIM_TYPE_PATH_RE,
                    _CAPTURE_CONTRACT_PATH_RE,
                    _PROVIDER_PATH_RE,
                    _SOURCE_ACQUISITION_POLICY_PATH_RE,
                    _STANDING_MANDATE_PATH_RE,
                    _CLAIM_PATH_RE,
                    _PROCEDURE_PATH_RE,
                    _LINE_PATH_RE,
                    _QUERY_DEFINITION_PATH_RE,
                    _EXHAUST_PROMOTION_PATH_RE,
                )
            ):
                dependency_artifacts({path: proposed_bytes})
        except (
            CaptureFormatError,
            ClaimFormatError,
            DocumentFormatError,
            ProviderFormatError,
            SourceAcquisitionPolicyError,
            StandingMandateError,
            SubjectFormatError,
            ClaimTypeFormatError,
            ProcedureFormatError,
            LineSpecFormatError,
            QueryDefinitionFormatError,
            ExhaustPromotionError,
        ) as exc:
            return CandidateEvaluation(
                candidate_tree,
                None,
                (
                    _diagnostic(
                        "playbill.proposal.member_format_invalid",
                        str(exc),
                        path,
                    ),
                ),
                rebased,
            )
    closure = evaluate_dependency_closure(
        parent_tree=current_tree,
        candidate_tree=candidate_tree,
        scope=scope,
    )
    if closure.verdict == "refused":
        closure_diagnostics: list[CompilerDiagnostic] = []
        if closure.missing_dependents:
            message = canonical_bytes(
                {"missing": [item.model_dump(mode="json") for item in closure.missing_dependents]}
            ).decode("utf-8")
            closure_diagnostics.append(
                _diagnostic(
                    "playbill.change_set.incomplete_closure",
                    message,
                )
            )
        if closure.unresolved_pins:
            closure_diagnostics.append(
                _diagnostic(
                    "playbill.change_set.unresolved_pin",
                    canonical_bytes(
                        {"pins": [item.model_dump(mode="json") for item in closure.unresolved_pins]}
                    ).decode("utf-8"),
                )
            )
        return CandidateEvaluation(
            candidate_tree,
            None,
            tuple(closure_diagnostics),
            rebased,
        )

    principals = principal_registry_from_tree(
        current_tree,
        semantic_root=current.semantic_root,
    )
    accepted_referent_coordinates = accepted_referent_coordinates_from_tree(
        current_tree,
        current=AcceptedCoordinate.from_internal(current),
    )
    candidate_states = {item.path: item for item in dependency_artifacts(candidate_tree)}
    parent_states = {item.path: item for item in dependency_artifacts(current_tree)}
    candidate_identities = {
        item.identity.qualified: (item.identity, item.artifact_digest)
        for item in candidate_states.values()
    }
    resolved_subjects: dict[str, AcceptedSubject] = {}
    resolved_claim_types: dict[str, AcceptedClaimType] = {}
    resolved_capture_contracts: dict[str, AcceptedCaptureContract] = {}
    resolved_providers: dict[str, ProviderV1] = {}
    resolved_procedures: dict[str, AcceptedProcedureV1] = {}
    for state in candidate_states.values():
        content = candidate_tree[state.path]
        if state.artifact_kind == "subject":
            shell = parse_subject(content, path=state.path)
            resolved_subjects[state.path] = AcceptedSubject(
                path=state.path,
                shell=shell,
                artifact_digest=state.artifact_digest,
            )
        elif state.artifact_kind == "claim-type":
            claim_type_artifact = parse_claim_type(content, path=state.path)
            resolved_claim_types[claim_type_artifact.identity.qualified] = AcceptedClaimType(
                path=state.path,
                claim_type=claim_type_artifact,
                artifact_digest=state.artifact_digest,
            )
        elif state.artifact_kind == "capture-contract":
            capture_contract = parse_capture_contract(content, path=state.path)
            resolved_capture_contracts[capture_contract.identity.qualified] = (
                AcceptedCaptureContract(
                    path=state.path,
                    contract=capture_contract,
                    artifact_digest=state.artifact_digest,
                )
            )
        elif state.artifact_kind == "provider":
            provider = parse_provider(content, path=state.path)
            resolved_providers[provider.identity.qualified] = provider
        elif state.artifact_kind == "procedure":
            procedure = parse_procedure(content, path=state.path)
            resolved_procedures[procedure.identity.qualified] = AcceptedProcedureV1(
                path=state.path,
                procedure=procedure,
                artifact_digest=state.artifact_digest,
            )
    actor_roles: tuple[str, ...] = ()
    if actor_id is not None:
        try:
            actor_roles = tuple(
                str(role)
                for role in principals.require_active(actor_id).authority_roles
                if role != "daemon"
            )
        except Exception:
            actor_roles = ()
    claim_admission_by_path: dict[str, tuple[dict[str, object], ...]] = {}
    claim_admission_digests_by_path: dict[str, tuple[str, ...]] = {}
    claim_admission_diagnostics: tuple[CompilerDiagnostic, ...] = ()
    if actor_id is not None:
        (
            claim_admission_by_path,
            claim_admission_digests_by_path,
            claim_admission_diagnostics,
        ) = _claim_admission_evaluations(
            current_tree=current_tree,
            candidate_tree=candidate_tree,
            scope=scope,
            timestamp=timestamp,
            actor_id=actor_id,
            actor_roles=actor_roles,
            subjects=resolved_subjects,
            claim_types=resolved_claim_types,
        )
    member_inputs: list[
        tuple[
            str,
            str,
            str | None,
            str,
            PermissionTier,
            tuple[str, ...],
            ActivationPolicy,
            str,
            str,
            dict[str, object],
            tuple[str, ...],
            bool,
        ]
    ] = []
    diagnostics: list[CompilerDiagnostic] = list(claim_admission_diagnostics)
    used_expansions: set[str] = set()
    for path in scope:
        proposed_bytes = candidate_tree.get(path)
        if proposed_bytes is None:
            diagnostics.append(
                _diagnostic(
                    "playbill.change_set.delete_unsupported",
                    "PC-A2 does not activate artifact deletion semantics.",
                    path,
                )
            )
            continue
        parent_state = parent_states.get(path)
        candidate_state = candidate_states.get(path)
        if candidate_state is None:
            diagnostics.append(
                _diagnostic(
                    "playbill.proposal.unregistered_semantic_kind",
                    "No PC-A2 acceptance law is registered for this changed path.",
                    path,
                )
            )
            continue
        if _PROCEDURE_PATH_RE.fullmatch(path):
            procedure = parse_procedure(proposed_bytes, path=path)
            predecessor_procedure: AcceptedProcedureV1 | None = None
            if parent_state is not None:
                previous_procedure = parse_procedure(current_tree[path], path=path)
                predecessor_procedure = AcceptedProcedureV1(
                    path=path,
                    procedure=previous_procedure,
                    artifact_digest=procedure_artifact_digest(previous_procedure).tagged,
                )
            procedure_law = evaluate_procedure_law(
                procedure,
                path=path,
                actor_roles=actor_roles,
                predecessor=predecessor_procedure,
            )
            if procedure_law.verdict == "refused":
                diagnostics.extend(procedure_law.diagnostics)
                continue
            if procedure_law.artifact_digest is None or procedure_law.required_tier is None:
                raise ProposalIntegrityError("accepted Procedure law result is incomplete")
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
                artifact_tag=procedure.artifact_format
            )
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    (
                        None
                        if predecessor_procedure is None
                        else predecessor_procedure.artifact_digest
                    ),
                    procedure_law.artifact_digest,
                    procedure_law.required_tier,
                    procedure_law.approval_scope,
                    procedure.activation_policy,
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": procedure_law.artifact_digest,
                        "authoring_expansion": (
                            procedure.definition.annotations
                            if isinstance(procedure.definition.annotations, dict)
                            and "builder_kind" in procedure.definition.annotations
                            else None
                        ),
                        "definition_digest": procedure.definition_digest,
                        "directly_runnable": procedure.directly_runnable,
                        "verdict": "accepted",
                    },
                    (),
                    procedure.lifecycle.state == "retired",
                )
            )
            continue
        if _EXHAUST_PROMOTION_PATH_RE.fullmatch(path):
            promotion = parse_exhaust_promotion(proposed_bytes, path=path)
            predecessor_promotion: AcceptedExhaustPromotionV1 | None = None
            if parent_state is not None:
                previous_promotion = parse_exhaust_promotion(current_tree[path], path=path)
                predecessor_promotion = AcceptedExhaustPromotionV1(
                    path=path,
                    promotion=previous_promotion,
                    artifact_digest=exhaust_promotion_digest(previous_promotion),
                    accepted_coordinate=AcceptedCoordinate.from_internal(current),
                )
            if promotion_verifier is None:
                diagnostics.append(
                    _diagnostic(
                        "playbill.promotion.verifier_unavailable",
                        "ExhaustPromotion evaluation requires the exact journal/reducer verifier.",
                        path,
                    )
                )
                continue
            promotion_law = evaluate_exhaust_promotion_acceptance(
                promotion,
                path=path,
                actor_roles=actor_roles,
                predecessor=predecessor_promotion,
                operational_result=promotion_verifier.verify_promotion(promotion),
            )
            if promotion_law.verdict == "refused":
                diagnostics.append(
                    _diagnostic(
                        promotion_law.refusal_code or "playbill.promotion.refused",
                        promotion_law.message or "ExhaustPromotion law refused.",
                        path,
                    )
                )
                continue
            if (
                promotion_law.artifact_digest is None
                or promotion_law.required_tier is None
                or promotion_law.activation_policy is None
            ):
                raise ProposalIntegrityError("accepted ExhaustPromotion law result is incomplete")
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
                artifact_tag=promotion.artifact_format
            )
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    (
                        None
                        if predecessor_promotion is None
                        else predecessor_promotion.artifact_digest
                    ),
                    promotion_law.artifact_digest,
                    promotion_law.required_tier,
                    promotion_law.approval_scope,
                    promotion_law.activation_policy,
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    promotion_law.model_dump(mode="json"),
                    (),
                    promotion.lifecycle.state == "retired",
                )
            )
            continue
        if _LINE_PATH_RE.fullmatch(path):
            line = parse_line_spec(proposed_bytes, path=path)
            accepted_procedure = resolved_procedures.get(line.procedure.target.qualified)
            if accepted_procedure is None:
                diagnostics.append(
                    _diagnostic(
                        "playbill.line.procedure_unavailable",
                        "LineSpec's exact Procedure is unavailable in candidate state.",
                        path,
                    )
                )
                continue
            predecessor_line: AcceptedLineSpecV1 | None = None
            if parent_state is not None:
                previous_line = parse_line_spec(current_tree[path], path=path)
                predecessor_line = AcceptedLineSpecV1(
                    path=path,
                    line=previous_line,
                    artifact_digest=line_spec_digest(previous_line).tagged,
                )
            interface_digests: dict[str, str] = {}
            for state in candidate_states.values():
                interface_digests[state.artifact_digest] = state.artifact_digest
                interface_pin = next(
                    (pin for pin in state.pins if pin.role == "interface"),
                    None,
                )
                if interface_pin is not None:
                    interface_digests[state.artifact_digest] = interface_pin.artifact_digest
            line_law = evaluate_line_spec_law(
                line,
                path=path,
                actor_roles=actor_roles,
                procedure=accepted_procedure,
                interface_digests=interface_digests,
                predecessor=predecessor_line,
            )
            if line_law.verdict == "refused":
                diagnostics.extend(line_law.diagnostics)
                continue
            if line_law.artifact_digest is None or line_law.required_tier is None:
                raise ProposalIntegrityError("accepted LineSpec law result is incomplete")
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag=line.artifact_format)
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    None if predecessor_line is None else predecessor_line.artifact_digest,
                    line_law.artifact_digest,
                    line_law.required_tier,
                    line_law.approval_scope,
                    "snapshot",
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": line_law.artifact_digest,
                        "occurrence_epoch": line.occurrence_epoch,
                        "procedure_digest": line.procedure.artifact_digest,
                        "verdict": "accepted",
                    },
                    (),
                    line.lifecycle.state == "retired",
                )
            )
            continue
        if _QUERY_DEFINITION_PATH_RE.fullmatch(path):
            query_definition = parse_query_definition(proposed_bytes, path=path)
            predecessor_query: AcceptedQueryDefinitionV1 | None = None
            if parent_state is not None:
                previous_query = parse_query_definition(current_tree[path], path=path)
                predecessor_query = AcceptedQueryDefinitionV1(
                    path=path,
                    query=previous_query,
                    artifact_digest=query_definition_digest(previous_query).tagged,
                )
            query_law = evaluate_query_definition_law(
                query_definition,
                path=path,
                actor_roles=actor_roles,
                predecessor=predecessor_query,
                accepted_artifacts=candidate_identities,
            )
            if query_law.verdict == "refused":
                diagnostics.extend(query_law.diagnostics)
                continue
            if query_law.artifact_digest is None or query_law.required_tier is None:
                raise ProposalIntegrityError("accepted QueryDefinition law result is incomplete")
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
                artifact_tag=query_definition.artifact_format
            )
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    None if predecessor_query is None else predecessor_query.artifact_digest,
                    query_law.artifact_digest,
                    query_law.required_tier,
                    query_law.approval_scope,
                    "snapshot",
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": query_law.artifact_digest,
                        "result_cardinality": query_definition.result_cardinality,
                        "verdict": "accepted",
                    },
                    (),
                    query_definition.lifecycle.state == "retired",
                )
            )
            continue
        if _PROVIDER_PATH_RE.fullmatch(path):
            provider = parse_provider(proposed_bytes, path=path)
            predecessor_provider: AcceptedProviderV1 | None = None
            if parent_state is not None:
                previous_provider = parse_provider(current_tree[path], path=path)
                predecessor_provider = AcceptedProviderV1(
                    path=path,
                    provider=previous_provider,
                    artifact_digest=provider_digest(previous_provider).tagged,
                )
            provider_law = evaluate_provider_law(
                provider,
                path=path,
                actor_roles=actor_roles,
                predecessor=predecessor_provider,
            )
            if provider_law.verdict == "refused":
                diagnostics.extend(provider_law.diagnostics)
                continue
            if provider_law.artifact_digest is None or provider_law.required_tier is None:
                raise ProposalIntegrityError("accepted Provider law result is incomplete")
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
                artifact_tag=provider.artifact_format
            )
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    (
                        None
                        if predecessor_provider is None
                        else predecessor_provider.artifact_digest
                    ),
                    provider_law.artifact_digest,
                    provider_law.required_tier,
                    provider_law.approval_scope,
                    "snapshot",
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": provider_law.artifact_digest,
                        "verdict": "accepted",
                    },
                    (),
                    provider.lifecycle.state == "retired",
                )
            )
            continue
        if _SOURCE_ACQUISITION_POLICY_PATH_RE.fullmatch(path):
            policy = parse_acquisition_policy(proposed_bytes, path=path)
            predecessor_policy: AcceptedSourceAcquisitionPolicyV1 | None = None
            if parent_state is not None:
                previous_policy = parse_acquisition_policy(current_tree[path], path=path)
                predecessor_policy = AcceptedSourceAcquisitionPolicyV1(
                    path=path,
                    policy=previous_policy,
                    artifact_digest=acquisition_policy_digest(previous_policy).tagged,
                )
            policy_law = evaluate_acquisition_policy_law(
                policy,
                path=path,
                actor_roles=actor_roles,
                predecessor=predecessor_policy,
            )
            if policy_law.verdict == "refused":
                diagnostics.extend(policy_law.diagnostics)
                continue
            if policy_law.artifact_digest is None or policy_law.required_tier is None:
                raise ProposalIntegrityError(
                    "accepted SourceAcquisitionPolicy law result is incomplete"
                )
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag=policy.artifact_format)
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    None if predecessor_policy is None else predecessor_policy.artifact_digest,
                    policy_law.artifact_digest,
                    policy_law.required_tier,
                    policy_law.approval_scope,
                    "snapshot",
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": policy_law.artifact_digest,
                        "verdict": "accepted",
                    },
                    (),
                    policy.lifecycle.state == "retired",
                )
            )
            continue
        if _STANDING_MANDATE_PATH_RE.fullmatch(path):
            mandate = parse_standing_mandate(proposed_bytes, path=path)
            predecessor_mandate: AcceptedStandingMandateV1 | None = None
            if parent_state is not None:
                previous_mandate = parse_standing_mandate(current_tree[path], path=path)
                predecessor_mandate = AcceptedStandingMandateV1(
                    path=path,
                    mandate=previous_mandate,
                    artifact_digest=standing_mandate_digest(previous_mandate).tagged,
                )
            mandate_law = evaluate_standing_mandate_law(
                mandate,
                path=path,
                actor_roles=actor_roles,
                predecessor=predecessor_mandate,
            )
            if mandate_law.verdict == "refused":
                diagnostics.extend(mandate_law.diagnostics)
                continue
            if mandate_law.artifact_digest is None or mandate_law.required_tier is None:
                raise ProposalIntegrityError("accepted StandingMandate law result is incomplete")
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
                artifact_tag=mandate.artifact_format
            )
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    (None if predecessor_mandate is None else predecessor_mandate.artifact_digest),
                    mandate_law.artifact_digest,
                    mandate_law.required_tier,
                    mandate_law.approval_scope,
                    "snapshot",
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": mandate_law.artifact_digest,
                        "verdict": "accepted",
                    },
                    (),
                    mandate.lifecycle.state == "retired",
                )
            )
            continue
        if _CAPTURE_CONTRACT_PATH_RE.fullmatch(path):
            capture_contract = parse_capture_contract(proposed_bytes, path=path)
            predecessor_contract: AcceptedCaptureContract | None = None
            if parent_state is not None:
                previous_contract = parse_capture_contract(current_tree[path], path=path)
                predecessor_contract = AcceptedCaptureContract(
                    path=path,
                    contract=previous_contract,
                    artifact_digest=capture_contract_digest(previous_contract).tagged,
                )
            capture_contract_law = evaluate_capture_contract_law(
                capture_contract,
                path=path,
                actor_roles=actor_roles,
                predecessor=predecessor_contract,
            )
            if capture_contract_law.verdict == "refused":
                diagnostics.extend(capture_contract_law.diagnostics)
                continue
            if (
                capture_contract_law.artifact_digest is None
                or capture_contract_law.required_tier is None
            ):
                raise ProposalIntegrityError("accepted CaptureContract law result is incomplete")
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
                artifact_tag=capture_contract.artifact_format
            )
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    None if predecessor_contract is None else predecessor_contract.artifact_digest,
                    capture_contract_law.artifact_digest,
                    capture_contract_law.required_tier,
                    capture_contract_law.approval_scope,
                    "snapshot",
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": capture_contract_law.artifact_digest,
                        "verdict": "accepted",
                    },
                    (),
                    capture_contract.lifecycle.state == "retired",
                )
            )
            continue
        if _CLAIM_PATH_RE.fullmatch(path):
            claim = parse_claim(proposed_bytes, path=path)
            predecessor_claim: AcceptedClaim | None = None
            if parent_state is not None:
                previous_claim = parse_claim(current_tree[path], path=path)
                predecessor_claim = AcceptedClaim(
                    path=path,
                    claim=previous_claim,
                    statement_digest=claim_statement_digest(previous_claim.statement).tagged,
                    artifact_digest=claim_artifact_digest(previous_claim).tagged,
                )
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag=claim.artifact_format)
            if not isinstance(bodies, CaptureObjectStoreProtocol):
                diagnostics.append(
                    _diagnostic(
                        "playbill.claim.capture_store_unavailable",
                        "Claim evaluation requires the managed evidence CAS.",
                        path,
                    )
                )
                continue
            claim_law = evaluate_claim_law(
                claim,
                path=path,
                principals=principals,
                actor_id=actor_id,
                predecessor=predecessor_claim,
                subjects=resolved_subjects,
                claim_types=resolved_claim_types,
                capture_contracts=resolved_capture_contracts,
                capture_store=bodies,
                providers=resolved_providers,
                law_digest=installed.coordinate.digest,
                instance_id=current.instance_id,
                accepted_coordinate=AcceptedCoordinate.from_internal(current),
                accepted_referent_coordinates=accepted_referent_coordinates,
                evaluation_time=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
            )
            if claim_law.verdict == "refused":
                diagnostics.extend(claim_law.diagnostics)
                continue
            if claim_law.artifact_digest is None or claim_law.required_tier is None:
                raise ProposalIntegrityError("accepted Claim law result is incomplete")
            governing_policy_digests = {
                *claim_admission_digests_by_path.get(path, ()),
                _canonical_model_digest(
                    "playbill-claim-admission-policy-v1",
                    resolved_claim_types[
                        claim.statement.claim_type.qualified
                    ].claim_type.admission_policy,
                ),
                _canonical_model_digest(
                    "playbill-claim-evidence-admission-policy-v1",
                    resolved_claim_types[
                        claim.statement.claim_type.qualified
                    ].claim_type.evidence_admission_policy,
                ),
                _canonical_model_digest(
                    "playbill-claim-resolution-policy-v1",
                    resolved_claim_types[
                        claim.statement.claim_type.qualified
                    ].claim_type.resolution_policy,
                ),
            }
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    None if predecessor_claim is None else predecessor_claim.artifact_digest,
                    claim_law.artifact_digest,
                    claim_law.required_tier,
                    claim_law.approval_scope,
                    "snapshot",
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": claim_law.artifact_digest,
                        "claim_evidence": (
                            None
                            if claim_law.evidence is None
                            else claim_law.evidence.model_dump(mode="json")
                        ),
                        "claim_admission": list(claim_admission_by_path.get(path, ())),
                        "statement_digest": claim_law.statement_digest,
                        "verdict": "accepted",
                    },
                    tuple(sorted(governing_policy_digests)),
                    claim.lifecycle.state == "retired",
                )
            )
            continue
        if _CLAIM_TYPE_PATH_RE.fullmatch(path):
            try:
                claim_type = parse_claim_type(proposed_bytes, path=path)
            except ClaimTypeFormatError as exc:
                diagnostics.append(
                    _diagnostic("playbill.claim_type.format_invalid", str(exc), path)
                )
                continue
            predecessor: AcceptedClaimType | None = None
            if parent_state is not None:
                previous = parse_claim_type(current_tree[path], path=path)
                predecessor = AcceptedClaimType(
                    path=path,
                    claim_type=previous,
                    artifact_digest=claim_type_digest(previous).tagged,
                )
            law = evaluate_claim_type_law(
                claim_type,
                path=path,
                principals=principals,
                actor_id=actor_id,
                predecessor=predecessor,
                accepted_artifacts=candidate_identities,
            )
            if law.verdict == "refused":
                diagnostics.extend(law.diagnostics)
                continue
            if law.artifact_digest is None or law.required_tier is None:
                raise ProposalIntegrityError("accepted ClaimType law result is incomplete")
            reuse: dict[str, object] | None = None
            if predecessor is None:
                reuse = _claim_type_reuse_evidence(
                    claim_type=claim_type,
                    path=path,
                    lookup_tree=candidate_tree,
                    candidate_scope=scope,
                    current=current,
                )
                if reuse["verdict"] == "refused":
                    diagnostics.append(
                        _diagnostic(
                            str(reuse["refusal_code"]),
                            "ClaimType vocabulary reuse disposition was refused.",
                            path,
                        )
                    )
                    continue
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
                artifact_tag=claim_type.artifact_format
            )
            policy_digests = tuple(
                sorted(
                    (
                        _canonical_model_digest(
                            "playbill-claim-admission-policy-v1",
                            claim_type.admission_policy,
                        ),
                        _canonical_model_digest(
                            "playbill-claim-evidence-admission-policy-v1",
                            claim_type.evidence_admission_policy,
                        ),
                        _canonical_model_digest(
                            "playbill-claim-resolution-policy-v1",
                            claim_type.resolution_policy,
                        ),
                    )
                )
            )
            expansion = next(
                (
                    item
                    for item in claim_type_expansions
                    if item.expanded_artifact_digest == law.artifact_digest
                ),
                None,
            )
            if expansion is not None:
                try:
                    verify_claim_type_expansion_evidence(
                        expansion,
                        claim_type=claim_type,
                        compiler_digest=current.compiler.rule_digest,
                    )
                except AuthoringProfileError as exc:
                    diagnostics.append(
                        _diagnostic(
                            "playbill.claim_type.profile_evidence_invalid",
                            str(exc),
                            path,
                        )
                    )
                    continue
                used_expansions.add(expansion.expanded_artifact_digest)
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    None if predecessor is None else predecessor.artifact_digest,
                    law.artifact_digest,
                    law.required_tier,
                    law.approval_scope,
                    "snapshot",
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": law.artifact_digest,
                        "authoring_expansion": (
                            None if expansion is None else expansion.model_dump(mode="json")
                        ),
                        "expanded_claim_type": claim_type.model_dump(mode="json"),
                        "reuse": reuse,
                        "verdict": "accepted",
                    },
                    policy_digests,
                    claim_type.lifecycle.state == "retired",
                )
            )
            continue
        if _SUBJECT_PATH_RE.fullmatch(path):
            subject = parse_subject(proposed_bytes, path=path)
            predecessor_subject: AcceptedSubject | None = None
            if parent_state is not None:
                previous_subject = parse_subject(current_tree[path], path=path)
                predecessor_subject = AcceptedSubject(
                    path=path,
                    shell=previous_subject,
                    artifact_digest=subject_digest(previous_subject).tagged,
                )
            subject_law = evaluate_subject_law(
                subject,
                path=path,
                principals=principals,
                actor_id=actor_id,
                predecessor=predecessor_subject,
            )
            if subject_law.verdict == "refused":
                diagnostics.extend(subject_law.diagnostics)
                continue
            if subject_law.artifact_digest is None or subject_law.required_tier is None:
                raise ProposalIntegrityError("accepted Subject law result is incomplete")
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
                artifact_tag=subject.artifact_format
            )
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    None if predecessor_subject is None else predecessor_subject.artifact_digest,
                    subject_law.artifact_digest,
                    subject_law.required_tier,
                    subject_law.approval_scope,
                    "snapshot",
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": subject_law.artifact_digest,
                        "verdict": "accepted",
                    },
                    (),
                    subject.lifecycle.state == "retired",
                )
            )
            continue
        if _DOCUMENT_PATH_RE.fullmatch(path):
            document_shell = parse_document(proposed_bytes, path=path)
            predecessor_document: AcceptedDocument | None = None
            if parent_state is not None:
                previous_document = parse_document(current_tree[path], path=path)
                predecessor_document = AcceptedDocument(
                    path=path,
                    shell=previous_document,
                    envelope_digest=document_digest(previous_document).tagged,
                )
            document_law = evaluate_document_law(
                document_shell,
                path=path,
                bodies=bodies,
                predecessor=predecessor_document,
            )
            if document_law.verdict == "refused":
                diagnostics.extend(document_law.diagnostics)
                continue
            if (
                document_law.envelope_digest is None
                or document_law.required_tier is None
                or document_law.activation_policy is None
            ):
                raise ProposalIntegrityError("accepted Document law result is incomplete")
            installed = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag=document_shell.tag)
            member_inputs.append(
                (
                    path,
                    installed.artifact_kind,
                    None if predecessor_document is None else predecessor_document.envelope_digest,
                    document_law.envelope_digest,
                    document_law.required_tier,
                    document_law.approval_scope,
                    document_law.activation_policy,
                    installed.coordinate.identifier,
                    installed.coordinate.digest,
                    {
                        "artifact_digest": document_law.envelope_digest,
                        "verdict": "accepted",
                    },
                    (),
                    False,
                )
            )
            continue
        diagnostics.append(
            _diagnostic(
                "playbill.proposal.unregistered_semantic_kind",
                "No PC-A2 acceptance law is registered for this changed path.",
                path,
            )
        )
    unused_expansions = tuple(
        item
        for item in claim_type_expansions
        if item.expanded_artifact_digest not in used_expansions
    )
    if unused_expansions:
        diagnostics.append(
            _diagnostic(
                "playbill.claim_type.profile_output_mismatch",
                "ClaimType profile evidence does not bind any expanded candidate artifact.",
            )
        )
    if diagnostics:
        return CandidateEvaluation(candidate_tree, None, tuple(diagnostics), rebased)
    if tuple(item[0] for item in member_inputs) != scope:
        raise ProposalIntegrityError("v2 evaluator did not cover every scoped member")
    semantic_tree = semantic_projection(candidate_tree)
    semantic_candidate = SemanticCandidate(
        parent_semantic_root=current.semantic_root,
        candidate_manifest_root=manifest_root(semantic_tree).tagged,
        semantic_diff_digest=diff_digest.tagged,
        scope=scope,
        timestamp=timestamp,
    )
    law_evidence: list[MemberLawEvaluationV2] = []
    members: list[CandidateMemberLawEvidenceV2] = []
    for (
        path,
        artifact_kind,
        predecessor_digest,
        proposed_digest,
        _tier,
        _roles,
        _activation,
        law_identifier,
        law_digest,
        result,
        policy_digests,
        retired,
    ) in member_inputs:
        proofs = closure.proofs_for(path)
        evidence = MemberLawEvaluationV2(
            path=path,
            law_identifier=law_identifier,
            law_digest=law_digest,
            evaluation_time=timestamp,
            evaluation_coordinate=LawEvaluationCoordinateV1(
                git_oid=current.git_oid,
                semantic_root=current.semantic_root,
                generation_root=current.generation_root,
                compiler_digest=current.compiler.rule_digest,
            ),
            dependency_proof_refs=proofs,
            policy_digests=policy_digests,
            result=result,
        )
        law_evidence.append(evidence)
        disposition = _member_disposition(
            predecessor_digest=predecessor_digest,
            candidate_digest_value=proposed_digest,
            retired=retired,
        )
        members.append(
            CandidateMemberLawEvidenceV2(
                path=path,
                artifact_kind=artifact_kind,
                disposition=disposition,
                predecessor_artifact_digest=predecessor_digest,
                candidate_artifact_digest=proposed_digest,
                law_identifier=law_identifier,
                law_digest=law_digest,
                law_evidence_digest=member_law_evidence_digest(evidence),
                closure_role="invalidation" if disposition == "retire" else "authored",
                dependency_proof_refs=proofs,
            )
        )
    member_evidence_digest = typed_digest(
        Sha256Value,
        "playbill-candidate-member-evidence-v2",
        {"members": [item.model_dump(mode="json") for item in members]},
    ).tagged
    requirements = tuple(
        ApprovalRequirement(role=role)
        for role in sorted(
            {role for item in member_inputs for role in item[5]},
            key=lambda item: item.encode("utf-8"),
        )
    )
    law_digests = {
        identifier: digest
        for identifier, digest in sorted(
            {(item[7], item[8]) for item in member_inputs},
            key=lambda item: item[0].encode("utf-8"),
        )
    }
    record = CandidateRecordV2(
        candidate=semantic_candidate,
        candidate_digest=candidate_digest(semantic_candidate).tagged,
        required_tier=_aggregate_tier([item[4] for item in member_inputs]),
        approval_requirements=requirements,
        activation_policy=_aggregate_activation([item[6] for item in member_inputs]),
        closure_proof=ClosureProofV2(
            paths=scope,
            dependency_graph_digest=closure.dependency_graph_digest,
            member_evidence_digest=member_evidence_digest,
        ),
        members=tuple(members),
        law_evidence=tuple(law_evidence),
        law_digests=law_digests,
        compiler_digest=current.compiler.rule_digest,
    )
    return CandidateEvaluation(candidate_tree, record, (), rebased)


def evaluate_proposal_tree(
    *,
    base_tree: Mapping[str, bytes],
    current_tree: Mapping[str, bytes],
    proposed_tree: Mapping[str, bytes],
    current: AcceptedProjectionCoordinate,
    bodies: BodyVerifierProtocol,
    timestamp: str,
    rebased: bool,
    actor_id: str | None = None,
    claim_type_expansions: tuple[ClaimTypeExpansionEvidenceV1, ...] = (),
    promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
) -> CandidateEvaluation:
    candidate_tree = dict(proposed_tree)
    if rebased:
        _original_diff, original_scope = semantic_diff(base_tree, proposed_tree)
        v2_rebase = len(original_scope) > 1 or any(
            any(
                pattern.fullmatch(path)
                for pattern in (
                    _CLAIM_TYPE_PATH_RE,
                    _CAPTURE_CONTRACT_PATH_RE,
                    _PROVIDER_PATH_RE,
                    _SOURCE_ACQUISITION_POLICY_PATH_RE,
                    _STANDING_MANDATE_PATH_RE,
                    _CLAIM_PATH_RE,
                    _PROCEDURE_PATH_RE,
                    _LINE_PATH_RE,
                    _QUERY_DEFINITION_PATH_RE,
                    _EXHAUST_PROMOTION_PATH_RE,
                )
            )
            for path in original_scope
        )
        if v2_rebase:
            rebased_result = deterministic_rebase_v2(
                old_parent_tree=base_tree,
                new_parent_tree=current_tree,
                proposed_tree=proposed_tree,
            )
            candidate_tree = rebased_result.tree
            if rebased_result.conflicts:
                diagnostics = tuple(
                    _diagnostic(
                        conflict.code,
                        canonical_bytes(conflict.model_dump(mode="json")).decode("utf-8"),
                        conflict.path,
                    )
                    for conflict in rebased_result.conflicts
                )
                return CandidateEvaluation(candidate_tree, None, diagnostics, True)
        else:
            candidate_tree, conflicts = deterministic_rebase(
                base_tree=base_tree,
                current_tree=current_tree,
                proposed_tree=proposed_tree,
            )
            if conflicts:
                diagnostics = tuple(
                    _diagnostic(
                        "playbill.proposal.rebase_conflict",
                        "The accepted artifact changed incompatibly after the proposed base.",
                        path,
                    )
                    for path in conflicts
                )
                return CandidateEvaluation(candidate_tree, None, diagnostics, True)

    diff_digest, scope = semantic_diff(current_tree, candidate_tree)
    if len(scope) > 1 or any(
        any(
            pattern.fullmatch(path)
            for pattern in (
                _CLAIM_TYPE_PATH_RE,
                _CAPTURE_CONTRACT_PATH_RE,
                _PROVIDER_PATH_RE,
                _SOURCE_ACQUISITION_POLICY_PATH_RE,
                _STANDING_MANDATE_PATH_RE,
                _CLAIM_PATH_RE,
                _PROCEDURE_PATH_RE,
                _LINE_PATH_RE,
                _QUERY_DEFINITION_PATH_RE,
                _EXHAUST_PROMOTION_PATH_RE,
            )
        )
        for path in scope
    ):
        return _evaluate_v2_proposal_tree(
            current_tree=current_tree,
            candidate_tree=candidate_tree,
            current=current,
            bodies=bodies,
            timestamp=timestamp,
            scope=scope,
            diff_digest=diff_digest,
            actor_id=actor_id,
            rebased=rebased,
            claim_type_expansions=claim_type_expansions,
            promotion_verifier=promotion_verifier,
        )
    if len(scope) != 1:
        return CandidateEvaluation(
            candidate_tree,
            None,
            (
                _diagnostic(
                    "playbill.proposal.non_singleton_scope",
                    "PB-D proposals must change exactly one registered semantic member.",
                ),
            ),
            rebased,
        )
    path = scope[0]
    if _PRINCIPAL_PATH_RE.fullmatch(path):
        lifecycle = evaluate_principal_lifecycle(
            current_tree=current_tree,
            proposed_tree=candidate_tree,
            current=current,
            path=path,
            actor_id=actor_id,
            timestamp=timestamp,
        )
        if lifecycle.candidate is None:
            return CandidateEvaluation(
                candidate_tree,
                None,
                (
                    _diagnostic(
                        lifecycle.error_code or "playbill.principal.transition_refused",
                        lifecycle.error_message or "Principal transition was refused.",
                        path,
                    ),
                ),
                rebased,
            )
        return CandidateEvaluation(candidate_tree, lifecycle.candidate, (), rebased)
    if _SUBJECT_PATH_RE.fullmatch(path):
        proposed_bytes = candidate_tree.get(path)
        if proposed_bytes is None:
            return CandidateEvaluation(
                candidate_tree,
                None,
                (
                    _diagnostic(
                        "playbill.subject.removal_unsupported",
                        "Subjects are retired by successor, never removed from accepted state.",
                        path,
                    ),
                ),
                rebased,
            )
        try:
            subject_shell = parse_subject(proposed_bytes, path=path)
        except SubjectFormatError as exc:
            return CandidateEvaluation(
                candidate_tree,
                None,
                (_diagnostic("playbill.subject.format_invalid", str(exc), path),),
                rebased,
            )
        subject_predecessor: AcceptedSubject | None = None
        current_bytes = current_tree.get(path)
        if current_bytes is not None:
            try:
                current_subject_shell = parse_subject(current_bytes, path=path)
            except SubjectFormatError as exc:
                raise ProposalIntegrityError("current accepted Subject cannot be parsed") from exc
            subject_predecessor = AcceptedSubject(
                path=path,
                shell=current_subject_shell,
                artifact_digest=subject_digest(current_subject_shell).tagged,
            )
        principals = principal_registry_from_tree(
            current_tree,
            semantic_root=current.semantic_root,
        )
        subject_law = evaluate_subject_law(
            subject_shell,
            path=path,
            principals=principals,
            actor_id=actor_id,
            predecessor=subject_predecessor,
        )
        if subject_law.verdict == "refused":
            return CandidateEvaluation(candidate_tree, None, subject_law.diagnostics, rebased)
        if subject_law.required_tier is None or subject_law.artifact_digest is None:
            raise ProposalIntegrityError("accepted Subject law result is incomplete")
        semantic_tree = semantic_projection(candidate_tree)
        semantic_candidate = SemanticCandidate(
            parent_semantic_root=current.semantic_root,
            candidate_manifest_root=manifest_root(semantic_tree).tagged,
            semantic_diff_digest=diff_digest.tagged,
            scope=scope,
            timestamp=timestamp,
        )
        registered_law = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
            artifact_tag=subject_shell.artifact_format
        )
        record = CandidateRecord(
            candidate=semantic_candidate,
            candidate_digest=candidate_digest(semantic_candidate).tagged,
            required_tier=subject_law.required_tier,
            approval_requirements=tuple(
                ApprovalRequirement(role=role) for role in subject_law.approval_scope
            ),
            activation_policy="snapshot",
            closure_paths=scope,
            members=(
                CandidateMemberEvidence(
                    path=path,
                    artifact_kind=registered_law.artifact_kind,
                    artifact_digest=file_digest(proposed_bytes).tagged,
                    disposition=(
                        "replacement" if subject_predecessor is None else "hand-authored-successor"
                    ),
                    law_identifier=registered_law.coordinate.identifier,
                ),
            ),
            law_digests={
                registered_law.coordinate.identifier: registered_law.coordinate.digest,
            },
            compiler_digest=current.compiler.rule_digest,
        )
        return CandidateEvaluation(candidate_tree, record, (), rebased)
    if not _DOCUMENT_PATH_RE.fullmatch(path):
        return CandidateEvaluation(
            candidate_tree,
            None,
            (
                _diagnostic(
                    "playbill.proposal.unregistered_semantic_kind",
                    "No accepted proposal law is registered for the changed path.",
                    path,
                ),
            ),
            rebased,
        )
    proposed_bytes = candidate_tree.get(path)
    if proposed_bytes is None:
        return CandidateEvaluation(
            candidate_tree,
            None,
            (
                _diagnostic(
                    "playbill.document.removal_unsupported",
                    "PB-C does not activate Document removal semantics.",
                    path,
                ),
            ),
            rebased,
        )
    try:
        shell = parse_document(proposed_bytes, path=path)
    except DocumentFormatError as exc:
        return CandidateEvaluation(
            candidate_tree,
            None,
            (_diagnostic("playbill.document.format_invalid", str(exc), path),),
            rebased,
        )
    predecessor: AcceptedDocument | None = None
    current_bytes = current_tree.get(path)
    if current_bytes is not None:
        try:
            current_shell = parse_document(current_bytes, path=path)
        except DocumentFormatError as exc:  # accepted-state corruption, not author refusal
            raise ProposalIntegrityError("current accepted Document cannot be parsed") from exc
        predecessor = AcceptedDocument(
            path=path,
            shell=current_shell,
            envelope_digest=document_digest(current_shell).tagged,
        )
    law = evaluate_document_law(shell, path=path, bodies=bodies, predecessor=predecessor)
    if law.verdict == "refused":
        return CandidateEvaluation(candidate_tree, None, law.diagnostics, rebased)
    if (
        law.envelope_digest is None or law.required_tier is None or law.activation_policy is None
    ):  # pragma: no cover - guarded by DocumentLawResult
        raise ProposalIntegrityError("accepted Document law result is incomplete")

    semantic_tree = semantic_projection(candidate_tree)
    semantic_candidate = SemanticCandidate(
        parent_semantic_root=current.semantic_root,
        candidate_manifest_root=manifest_root(semantic_tree).tagged,
        semantic_diff_digest=diff_digest.tagged,
        scope=scope,
        timestamp=timestamp,
    )
    registered_law = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag=shell.tag)
    disposition: MutationDisposition = (
        "replacement" if predecessor is None else "hand-authored-successor"
    )
    record = CandidateRecord(
        candidate=semantic_candidate,
        candidate_digest=candidate_digest(semantic_candidate).tagged,
        required_tier=law.required_tier,
        approval_requirements=tuple(ApprovalRequirement(role=role) for role in law.approval_scope),
        activation_policy=law.activation_policy,
        closure_paths=scope,
        members=(
            CandidateMemberEvidence(
                path=path,
                artifact_kind=registered_law.artifact_kind,
                artifact_digest=file_digest(proposed_bytes).tagged,
                disposition=disposition,
                law_identifier=registered_law.coordinate.identifier,
            ),
        ),
        law_digests={
            registered_law.coordinate.identifier: registered_law.coordinate.digest,
        },
        compiler_digest=current.compiler.rule_digest,
    )
    return CandidateEvaluation(candidate_tree, record, (), rebased)


def _proposal_id_payload(
    *,
    actor_id: str,
    request: ProposalAdmissionRequest,
    candidate_commit_oid: str,
    candidate_tree_oid: str,
    admitted_at: str,
    limits: ProposalReceiveLimits,
) -> str:
    return ProposalDigest(
        canonical_digest(
            "playbill-proposal-admission-v1",
            {
                "actor_id": actor_id,
                "target_ref": request.target_ref,
                "proposed_base_oid": request.proposed_base_oid,
                "candidate_commit_oid": candidate_commit_oid,
                "candidate_tree_oid": candidate_tree_oid,
                "source_compilation_digest": request.source_compilation_digest,
                "claim_type_expansions": [
                    item.model_dump(mode="json") for item in request.claim_type_expansions
                ],
                "limits": limits.model_dump(mode="json"),
                "admitted_at": admitted_at,
            },
        )
    ).tagged


class ProposalService:
    """Daemon-only PB-C proposal service; it never updates main or accepted state."""

    def __init__(
        self,
        transport: ProposalTransportProtocol,
        *,
        accepted: AcceptedProjectionCoordinate,
        bodies: BodyVerifierProtocol,
        evidence: ProposalEvidenceStore,
        receive_limits: ProposalReceiveLimits = ProposalReceiveLimits(),
        current_coordinate: Callable[[], AcceptedProjectionCoordinate] | None = None,
        promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
    ) -> None:
        self.transport = transport
        self.accepted = accepted
        self.bodies = bodies
        self.evidence = evidence
        self.receive_limits = receive_limits
        self._current_coordinate = current_coordinate or (lambda: accepted)
        self.promotion_verifier = promotion_verifier

    def submit(
        self,
        *,
        actor: AuthenticatedActor,
        request: ProposalAdmissionRequest,
        candidate_tree: Mapping[str, bytes],
        timestamp: str,
    ) -> ProposalResult:
        validate_candidate_timestamp(timestamp)
        if "propose" not in actor.capabilities:
            raise ProposalAdmissionError("authenticated actor lacks the propose capability")
        current = self._current_coordinate()
        namespace = request.target_ref.split("/")[2]
        if namespace != actor.actor_id:
            raise ProposalAdmissionError(
                "proposal ref namespace differs from the authenticated actor"
            )
        expected_oid_length = 40 if self.transport.object_format() == "sha1" else 64
        if len(request.proposed_base_oid) != expected_oid_length:
            raise ProposalAdmissionError("proposed base OID length differs from object format")
        if (
            current.instance_id != self.accepted.instance_id
            or current.repository_path != self.accepted.repository_path
            or current.git_object_format != self.accepted.git_object_format
        ):
            raise ProposalAdmissionError("current coordinate names a different instance ledger")
        if current.git_oid == self.accepted.git_oid and current != self.accepted:
            raise ProposalAdmissionError("current coordinate contradicts the verified base")
        if self.transport.read_main() != current.git_oid:
            raise ProposalAdmissionError("current coordinate is not the accepted main ref")

        base_tree = self.transport.read_tree(request.proposed_base_oid)
        validated_tree = validate_proposal_tree(
            candidate_tree,
            limits=self.receive_limits,
            base_tree=base_tree,
        )
        existing = self.transport.read_proposal_ref(request.target_ref)
        commit_oid, tree_oid = self.transport.create_proposal_commit(
            validated_tree,
            base_oid=request.proposed_base_oid,
            target_ref=request.target_ref,
            actor_id=actor.actor_id,
            timestamp=timestamp,
            expected_ref_oid=existing,
        )
        proposal_id = _proposal_id_payload(
            actor_id=actor.actor_id,
            request=request,
            candidate_commit_oid=commit_oid,
            candidate_tree_oid=tree_oid,
            admitted_at=timestamp,
            limits=self.receive_limits,
        )
        admission = ProposalAdmissionRecord(
            proposal_id=proposal_id,
            actor_id=actor.actor_id,
            target_ref=request.target_ref,
            proposed_base_oid=request.proposed_base_oid,
            candidate_commit_oid=commit_oid,
            candidate_tree_oid=tree_oid,
            source_compilation_digest=request.source_compilation_digest,
            claim_type_expansions=request.claim_type_expansions,
            limits=self.receive_limits,
            admitted_at=timestamp,
        )
        self.evidence.write_admission(admission)

        current_tree = self.transport.read_tree(current.git_oid)
        is_rebase = current.git_oid != request.proposed_base_oid
        outcome = evaluate_proposal_tree(
            base_tree=base_tree,
            current_tree=current_tree,
            proposed_tree=validated_tree,
            current=current,
            bodies=self.bodies,
            timestamp=timestamp,
            rebased=is_rebase,
            actor_id=actor.actor_id,
            claim_type_expansions=request.claim_type_expansions,
            promotion_verifier=self.promotion_verifier,
        )

        evaluated_tree_oid: str | None = tree_oid
        if outcome.candidate is not None and is_rebase:
            commit_oid, evaluated_tree_oid = self.transport.create_proposal_commit(
                outcome.tree,
                base_oid=current.git_oid,
                target_ref=request.target_ref,
                actor_id=actor.actor_id,
                timestamp=timestamp,
                expected_ref_oid=commit_oid,
            )
        candidate_value = outcome.candidate.candidate_digest if outcome.candidate else None
        evaluation = ProposalEvaluationRecord(
            proposal_id=proposal_id,
            verdict="candidate" if outcome.candidate is not None else "refused",
            evaluated_base_oid=current.git_oid,
            evaluated_tree_oid=evaluated_tree_oid if outcome.candidate is not None else None,
            rebased=is_rebase,
            candidate_digest=candidate_value,
            diagnostics=outcome.diagnostics,
            evaluated_at=timestamp,
        )
        self.evidence.write_evaluation(evaluation)
        if outcome.candidate is not None:
            self.evidence.write_candidate(outcome.candidate)
        if self.transport.read_main() != current.git_oid:
            raise ProposalIntegrityError("proposal evaluation changed or raced accepted main")
        return ProposalResult(
            admission=admission,
            evaluation=evaluation,
            candidate=outcome.candidate,
        )


__all__ = [
    "AuthenticatedActor",
    "CandidateEvaluation",
    "ProposalAdmissionRecord",
    "ProposalAdmissionRequest",
    "ProposalEvaluationRecord",
    "ProposalEvidenceStore",
    "ProposalReceiveLimits",
    "ProposalResult",
    "ProposalService",
    "ProposalTransportProtocol",
    "ExhaustPromotionVerifierProtocol",
    "claim_type_expansions_from_candidate",
    "deterministic_rebase",
    "deterministic_rebase_v2",
    "evaluate_proposal_tree",
    "validate_proposal_tree",
]
