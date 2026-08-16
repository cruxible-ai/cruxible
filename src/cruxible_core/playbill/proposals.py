"""Authenticated proposal admission and deterministic PB-C candidate evaluation."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_core.playbill.actor_context import TransportCapability
from cruxible_core.playbill.attestations import (
    ApprovalSubmission,
    approval_digest,
    approval_statement_bytes,
)
from cruxible_core.playbill.candidates import (
    CandidateMemberEvidence,
    CandidateRecord,
    SemanticCandidate,
    candidate_digest,
    render_candidate_record,
    validate_candidate_timestamp,
)
from cruxible_core.playbill.canonical import (
    CandidateDigest,
    ProposalDigest,
    Sha256Value,
    canonical_bytes,
    canonical_digest,
    file_digest,
    manifest_root,
    normalize_manifest_paths,
    semantic_diff,
    semantic_projection,
)
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
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
from cruxible_core.playbill.governance import ApprovalRequirement, MutationDisposition
from cruxible_core.playbill.laws import PLAYBILL_ACCEPTANCE_LAWS
from cruxible_core.playbill.principal_lifecycle import evaluate_principal_lifecycle
from cruxible_core.playbill.principals import principal_registry_from_tree
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_catalog import SourceCompilationManifest
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
_LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
_EvidenceModelT = TypeVar("_EvidenceModelT", bound=BaseModel)


class _StrictProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class ProposalAdmissionRecord(_StrictProposalModel):
    tag: Literal["playbill-proposal-admission-v1"] = "playbill-proposal-admission-v1"
    proposal_id: str
    actor_id: str
    target_ref: str
    proposed_base_oid: str
    candidate_commit_oid: str
    candidate_tree_oid: str
    source_compilation_digest: str | None
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
    candidate: CandidateRecord | None = None

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

    def write_candidate(self, record: CandidateRecord) -> Path:
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

    def read_candidate(self, candidate_digest_value: str) -> CandidateRecord:
        """Read one canonical validated candidate by its frozen C_s digest."""

        CandidateDigest.from_tagged(candidate_digest_value)
        path = self.candidates / f"{candidate_digest_value.removeprefix('sha256:')}.json"
        return self._read_model(path, CandidateRecord, label="validated candidate")

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
        )
        if not authorable and path not in result:
            raise ProposalAdmissionError(
                f"proposal removed a daemon-controlled or unregistered path: {path}"
            )
    return result


@dataclass(frozen=True)
class CandidateEvaluation:
    tree: dict[str, bytes]
    candidate: CandidateRecord | None
    diagnostics: tuple[CompilerDiagnostic, ...]
    rebased: bool


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
) -> CandidateEvaluation:
    candidate_tree = dict(proposed_tree)
    if rebased:
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
    ) -> None:
        self.transport = transport
        self.accepted = accepted
        self.bodies = bodies
        self.evidence = evidence
        self.receive_limits = receive_limits
        self._current_coordinate = current_coordinate or (lambda: accepted)

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
    "deterministic_rebase",
    "evaluate_proposal_tree",
    "validate_proposal_tree",
]
