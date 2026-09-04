"""Filesystem persistence for immutable proposal and candidate evidence."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from cruxible_client.contracts.attestations import (
    ApprovalSubmission,
    approval_digest,
    approval_statement_bytes,
)
from cruxible_client.contracts.candidates import (
    CandidateRecordAnyVersion,
    render_candidate_record,
)
from cruxible_client.contracts.canonical import (
    CandidateDigest,
    ProposalDigest,
    Sha256Value,
    canonical_bytes,
    canonical_digest,
)
from cruxible_client.contracts.errors import ProposalIntegrityError, ProposalWithdrawnError
from cruxible_client.contracts.source_catalog import SourceCompilationManifest
from cruxible_core.playbill.id_prefixes import resolve_id_prefix
from cruxible_core.playbill.proposal_notes import (
    admission_bytes,
    evaluation_bytes,
    proposal_evaluation_note,
)
from cruxible_core.playbill.proposals import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
    ProposalWithdrawalRecordV1,
)

_EvidenceModelT = TypeVar("_EvidenceModelT", bound=BaseModel)


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
        self.withdrawals = self._directory("withdrawals")

    def _directory(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ProposalIntegrityError("proposal evidence directory is not trustworthy")
        os.chmod(path, 0o700)
        return path.resolve(strict=True)

    def write_admission(self, record: ProposalAdmissionRecord) -> Path:
        path = self.proposals / f"{record.proposal_id.removeprefix('sha256:')}.json"
        _exclusive_canonical_write(path, admission_bytes(record))
        return path

    def write_evaluation(self, record: ProposalEvaluationRecord) -> Path:
        digest = canonical_digest(
            "playbill-proposal-evaluation-v1",
            {key: value for key, value in record.model_dump(mode="json").items() if key != "tag"},
        )
        path = self.evaluations / f"{digest}.json"
        _exclusive_canonical_write(path, evaluation_bytes(record))
        return path

    def write_candidate(self, record: CandidateRecordAnyVersion) -> Path:
        path = self.candidates / f"{record.candidate_digest.removeprefix('sha256:')}.json"
        _exclusive_canonical_write(path, render_candidate_record(record))
        return path

    def write_withdrawal(self, record: ProposalWithdrawalRecordV1) -> Path:
        """Persist one terminal withdrawal beside the admission it retires.

        Immutable like every other record here: the exclusive write makes a
        second withdrawal of the same proposal either a no-op, when the bytes
        are identical, or a refusal, so a withdrawal's reason cannot be rewritten
        after the fact.
        """

        path = self.withdrawals / f"{record.proposal_id.removeprefix('sha256:')}.json"
        _exclusive_canonical_write(path, canonical_bytes(record.model_dump(mode="json")) + b"\n")
        return path

    def read_withdrawal(self, proposal_id: str) -> ProposalWithdrawalRecordV1 | None:
        """Return this proposal's withdrawal, or None when it has not been withdrawn."""

        ProposalDigest.from_tagged(proposal_id)
        path = self.withdrawals / f"{proposal_id.removeprefix('sha256:')}.json"
        if not path.exists():
            return None
        record = self._read_model(path, ProposalWithdrawalRecordV1, label="proposal withdrawal")
        if record.proposal_id != proposal_id:
            raise ProposalIntegrityError("withdrawal evidence names another proposal")
        return record

    def refuse_withdrawn(self, proposal_id: str) -> None:
        """Refuse a settlement door asked to settle a withdrawn proposal.

        Every door that would settle a proposal calls this: approval,
        activation, and readmission. Without it a withdrawal is a note in the
        inventory rather than a terminal transition -- a withdrawn proposal
        could be approved and activated, and the list would then report it
        `accepted`, which is the outcome the record says will never happen.
        """

        record = self.read_withdrawal(proposal_id)
        if record is not None:
            raise ProposalWithdrawnError(
                record.proposal_id,
                actor_id=record.actor_id,
                reason=record.reason,
                withdrawn_at=record.withdrawn_at,
            )

    def withdrawn_proposal_ids(self) -> frozenset[str]:
        """Return every withdrawn proposal id, read from its own evidence."""

        return frozenset(
            self._read_model(
                path,
                ProposalWithdrawalRecordV1,
                label="proposal withdrawal",
            ).proposal_id
            for path in sorted(self.withdrawals.glob("*.json"), key=lambda item: item.name)
        )

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

    def resolve_proposal_id(self, proposal_id: str) -> str:
        """Accept a unique sha256: prefix where a full proposal id is expected."""

        return resolve_id_prefix(
            proposal_id,
            tuple(f"sha256:{path.stem}" for path in self.proposals.glob("*.json")),
            marker="sha256:",
            label="proposal",
        )

    def read_admission(self, proposal_id: str) -> ProposalAdmissionRecord:
        """Read one canonical immutable admission by its public proposal ID."""

        proposal_id = self.resolve_proposal_id(proposal_id)
        ProposalDigest.from_tagged(proposal_id)
        path = self.proposals / f"{proposal_id.removeprefix('sha256:')}.json"
        return self._read_model(
            path,
            ProposalAdmissionRecord,
            label="proposal admission",
            render=admission_bytes,
        )

    def list_admissions(self) -> tuple[ProposalAdmissionRecord, ...]:
        """List canonical admissions in stable evidence-filename order."""

        return tuple(
            self._read_model(
                path,
                ProposalAdmissionRecord,
                label="proposal admission",
                render=admission_bytes,
            )
            for path in sorted(self.proposals.glob("*.json"), key=lambda item: item.name)
        )

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

    def evaluation_note(self, proposal_id: str) -> bytes:
        """Re-render one proposal's evaluation note from the source of record.

        The store, not the note, is authority. Rendering the expected bytes here
        is what lets a settlement door compare Git's copy against the daemon's
        own files and refuse a note that has been edited underneath it.
        """

        proposal_id = self.resolve_proposal_id(proposal_id)
        return proposal_evaluation_note(
            admission=self.read_admission(proposal_id),
            evaluation=self.read_evaluation(proposal_id),
        )

    def read_candidate(self, candidate_digest_value: str) -> CandidateRecordAnyVersion:
        """Read one canonical validated candidate by its frozen C_s digest."""

        CandidateDigest.from_tagged(candidate_digest_value)
        path = self.candidates / f"{candidate_digest_value.removeprefix('sha256:')}.json"
        if path.is_symlink() or not path.is_file():
            raise ProposalIntegrityError(
                "validated candidate evidence is missing or not a regular file"
            )
        try:
            raw = path.read_bytes()
            adapter: TypeAdapter[CandidateRecordAnyVersion] = TypeAdapter(CandidateRecordAnyVersion)
            value: CandidateRecordAnyVersion = adapter.validate_json(raw)
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
            approval_statement_bytes(submission.attestation)
            approval_digest(submission.attestation)
        return submissions

    @staticmethod
    def _read_model(
        path: Path,
        model: type[_EvidenceModelT],
        *,
        label: str,
        render: Callable[[Any], bytes] | None = None,
    ) -> _EvidenceModelT:
        """Read one stored record and prove its bytes are the canonical ones.

        `render` is the writer this record was persisted through, for the one
        model whose persisted shape is narrower than its own dump. Verifying
        against the plain dump there would refuse every admission the store
        holds the moment a field with a default is added to its limits.
        """

        if path.is_symlink() or not path.is_file():
            raise ProposalIntegrityError(f"{label} evidence is missing or not a regular file")
        try:
            raw = path.read_bytes()
            value = model.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise ProposalIntegrityError(f"{label} evidence is malformed") from exc
        expected = (
            canonical_bytes(value.model_dump(mode="json")) + b"\n"
            if render is None
            else render(value)
        )
        if expected != raw:
            raise ProposalIntegrityError(f"{label} evidence is not canonical")
        return value


__all__ = ["ProposalEvidenceStore"]
