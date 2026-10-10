"""Filesystem persistence for immutable proposal and candidate evidence."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from cruxible_core.indexes.proposals.proposal_index import ProposalIndex
    from cruxible_core.ledger.git import GitLedger

from pydantic import BaseModel, ValidationError

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
from cruxible_client.contracts.errors import (
    ProposalIntegrityError,
    ProposalNotFoundError,
    ProposalRejectedError,
    ProposalWithdrawnError,
)
from cruxible_client.contracts.proposal_models import ProposalItemReason
from cruxible_client.contracts.source_catalog import SourceCompilationManifest
from cruxible_core.authoring.id_prefixes import resolve_id_prefix
from cruxible_core.proposals.candidate_review_summary import (
    CandidateReviewSummary,
    parse_candidate_evidence,
    read_candidate_bytes,
    read_candidate_review_summary,
)
from cruxible_core.proposals.proposal_notes import (
    admission_bytes,
    evaluation_bytes,
    proposal_admission_id,
)
from cruxible_core.proposals.proposals import (
    ProposalAdmissionRecord,
    ProposalAdmissionRequest,
    ProposalEvaluationRecord,
    ProposalWithdrawalRecord,
)
from cruxible_core.storage.preview_fence import is_previewing

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
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ProposalIntegrityError("immutable proposal evidence path is occupied")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                if stream.read() != payload:
                    raise ProposalIntegrityError("immutable proposal evidence path is occupied")
            # An interrupted writer may have written every byte without reaching
            # its fsync. Retry proves and flushes the same opened inode before
            # derived publication, then flushes the directory below as usual.
            os.fsync(descriptor)
        except OSError as exc:
            raise ProposalIntegrityError(
                "existing proposal evidence could not be persisted"
            ) from exc
    except OSError as exc:
        raise ProposalIntegrityError("proposal evidence could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


class ProposalEvidenceStore:
    """Immutable out-of-band proposal/candidate evidence; never accepted authority."""

    def __init__(
        self, root: Path, *, index: ProposalIndex | None = None, transport: GitLedger | None = None
    ) -> None:
        if root.is_symlink() or not root.is_dir():
            raise ProposalIntegrityError("proposal evidence root must be an existing directory")
        self.root = root.resolve(strict=True)
        self.index = index
        self.transport = transport
        self._pending_records: dict[str, dict[str, Path]] | None = None
        self.proposals = self._directory("proposals")
        self.evaluations = self._directory("evaluations")
        self.candidates = self._directory("candidates")
        self.approvals = self._directory("approvals")
        self.source_compilations = self._directory("source-compilations")
        self.withdrawals = self._directory("withdrawals")
        self.item_reasons = self._directory("item-reasons")

    def _directory(self, name: str) -> Path:
        path = self.root / name
        if is_previewing():
            # A preview reads, and writes nothing (R12): a directory no record
            # has been written to yet holds nothing to read, so it is not made.
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ProposalIntegrityError("proposal evidence directory is not trustworthy")
            return path.resolve(strict=False)
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ProposalIntegrityError("proposal evidence directory is not trustworthy")
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            os.chmod(path, 0o700)
        return path.resolve(strict=True)

    @contextmanager
    def publication(self) -> Iterator[None]:
        """Publish existing source writes as one admission-last locator update."""
        if self.index is None or self._pending_records is not None:
            yield
        else:
            with self.index.publication(self):
                yield

    def _write_record(self, kind: str, identity: str, path: Path, payload: bytes) -> None:
        with self.publication():
            if self.index is not None and kind == "evaluation":
                # A second digest-named evaluation is a duplicate, even if the
                # caller has not yet reached its admission write.
                connection = self.index._connection
                assert connection is not None
                old = connection.execute(
                    "SELECT evaluation_path FROM proposals WHERE proposal_id=?", (identity,)
                ).fetchone()
                located = self.root / old[0] if old and old[0] else None
                if located is not None and located != path:
                    raise ProposalIntegrityError("proposal evidence contains multiple evaluations")
            _exclusive_canonical_write(path, payload)
            if self._pending_records is not None:
                self._pending_records.setdefault(kind, {})[identity] = path

    def write_admission(self, record: ProposalAdmissionRecord) -> Path:
        path = self.proposals / f"{record.proposal_id.removeprefix('sha256:')}.json"
        self._write_record("admission", record.proposal_id, path, admission_bytes(record))
        return path

    def write_evaluation(self, record: ProposalEvaluationRecord) -> Path:
        digest = canonical_digest(
            "playbill-proposal-evaluation-v1",
            {key: value for key, value in record.model_dump(mode="json").items() if key != "tag"},
        )
        path = self.evaluations / f"{digest}.json"
        self._write_record("evaluation", record.proposal_id, path, evaluation_bytes(record))
        return path

    def write_candidate(self, record: CandidateRecordAnyVersion) -> Path:
        path = self.candidates / f"{record.candidate_digest.removeprefix('sha256:')}.json"
        self._write_record(
            "candidate", record.candidate_digest, path, render_candidate_record(record)
        )
        return path

    def write_withdrawal(self, record: ProposalWithdrawalRecord) -> Path:
        """Persist one terminal withdrawal beside the admission it retires.

        Immutable like every other record here: the exclusive write makes a
        second withdrawal of the same proposal either a no-op, when the bytes
        are identical, or a refusal, so a withdrawal's reason cannot be rewritten
        after the fact.
        """

        path = self.withdrawals / f"{record.proposal_id.removeprefix('sha256:')}.json"
        self._write_record(
            "withdrawal",
            record.proposal_id,
            path,
            canonical_bytes(record.model_dump(mode="json")) + b"\n",
        )
        return path

    def read_withdrawal(self, proposal_id: str) -> ProposalWithdrawalRecord | None:
        """Return this proposal's withdrawal, or None when it has not been withdrawn."""

        ProposalDigest.from_tagged(proposal_id)
        if self.index is not None:
            row = self.index.locate(self, proposal_id)
            if row["withdrawal_path"] is None:
                return None
            return self._read_located(row, "withdrawal", ProposalWithdrawalRecord)
        path = self.withdrawals / f"{proposal_id.removeprefix('sha256:')}.json"
        if not path.exists():
            return None
        record = self._read_model(path, ProposalWithdrawalRecord, label="proposal withdrawal")
        if record.proposal_id != proposal_id:
            raise ProposalIntegrityError("withdrawal evidence names another proposal")
        return record

    def refuse_closed(self, proposal_id: str) -> None:
        """Refuse a settlement door asked to settle a withdrawn or rejected proposal.

        Every door that would settle a proposal calls this: approval,
        activation, and readmission. Without it a withdrawal or rejection is a
        note in the inventory rather than a terminal transition -- the proposal
        could be approved and activated, and the list would then report it
        `accepted`, which is the outcome the record says will never happen.
        """

        record = self.read_withdrawal(proposal_id)
        if record is None:
            return
        if record.kind == "rejected":
            raise ProposalRejectedError(
                record.proposal_id,
                actor_id=record.actor_id,
                reason=record.reason,
                rejected_at=record.withdrawn_at,
            )
        raise ProposalWithdrawnError(
            record.proposal_id,
            actor_id=record.actor_id,
            reason=record.reason,
            withdrawn_at=record.withdrawn_at,
        )

    # -- Procedure item reasons ---------------------------------------------------

    def _item_reason_directory(self, target_ref: str) -> Path:
        return self.item_reasons / hashlib.sha256(target_ref.encode("utf-8")).hexdigest()

    @staticmethod
    def _item_reason_name(path: str) -> str:
        return hashlib.sha256(path.encode("utf-8")).hexdigest() + ".json"

    def write_item_reasons(self, records: tuple[ProposalItemReason, ...]) -> None:
        """Persist each item's reason under its operation's proposal ref, one file per member.

        Immutable like every record here and idempotent: a retry of the same
        operation writes the same bytes (an existing identical file is a
        no-op, a different one refuses). Written before the proposal is
        submitted, so an admitted proposal never lacks its items' reasons.
        """

        directories: set[Path] = set()
        for record in records:
            directory = self._item_reason_directory(record.target_ref)
            if directory not in directories:
                directory.mkdir(mode=0o700, exist_ok=True)
                if directory.is_symlink() or not directory.is_dir():
                    raise ProposalIntegrityError(
                        "proposal item reason directory is not trustworthy"
                    )
                directories.add(directory)
            _exclusive_canonical_write(
                directory / self._item_reason_name(record.path),
                canonical_bytes(record.model_dump(mode="json")) + b"\n",
            )
        for directory in sorted(directories):
            _fsync_directory(directory)
        if directories:
            _fsync_directory(self.item_reasons)

    def read_item_reason(self, target_ref: str, path: str) -> ProposalItemReason | None:
        """The reason recorded for one member of one operation's proposal: one file read."""

        file = self._item_reason_directory(target_ref) / self._item_reason_name(path)
        if not file.exists():
            return None
        record = self._read_model(file, ProposalItemReason, label="proposal item reason")
        if record.target_ref != target_ref or record.path != path:
            raise ProposalIntegrityError("proposal item reason names another member")
        return record

    def item_reason_target(self, admission: ProposalAdmissionRecord) -> str:
        """The proposal ref an admission's item reasons are kept under.

        A readmission re-admits its source under a new ref; its items' reasons
        stay under the ref of the operation that first proposed them.
        """

        current = admission
        seen = {current.proposal_id}
        while current.readmits is not None:
            source = current.readmits.source_proposal_id
            if source in seen:
                raise ProposalIntegrityError("proposal readmission links form a cycle")
            seen.add(source)
            current = self.read_admission(source)
        return current.target_ref

    def item_reason_page(
        self, target_ref: str, *, after: str | None, limit: int
    ) -> tuple[tuple[ProposalItemReason, ...], int, str | None]:
        """One page of an operation's item reasons; the total; the next cursor.

        Pages follow the records' stable file order (the digest of each
        member's path), and the cursor is the last record's file stem. Only
        the page's records are read beyond the directory listing.
        """

        directory = self._item_reason_directory(target_ref)
        if not directory.is_dir():
            return (), 0, None
        names = sorted(item.name for item in directory.iterdir() if item.name.endswith(".json"))
        remaining = names if after is None else [name for name in names if name[:-5] > after]
        page_names = remaining[:limit]
        page = tuple(
            self._read_model(directory / name, ProposalItemReason, label="proposal item reason")
            for name in page_names
        )
        cursor = page_names[-1][:-5] if len(remaining) > limit else None
        return page, len(names), cursor

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

        if self.index is not None:
            if len(proposal_id) == 71:
                # A malformed full-length id is returned unchanged, so the
                # caller's own typed refusal names it rather than a ValueError.
                return proposal_id
            ids = tuple(
                row["proposal_id"]
                for row in self.index.rows(
                    self, "proposal_id>=? AND proposal_id<?", (proposal_id, proposal_id + "\uffff")
                )
            )
        else:
            ids = tuple(f"sha256:{path.stem}" for path in self.proposals.glob("*.json"))
        return resolve_id_prefix(
            proposal_id,
            ids,
            marker="sha256:",
            label="proposal",
        )

    def read_admission(self, proposal_id: str) -> ProposalAdmissionRecord:
        """Read one canonical immutable admission by its public proposal ID."""

        selector = proposal_id
        proposal_id = self.resolve_proposal_id(proposal_id)
        try:
            ProposalDigest.from_tagged(proposal_id)
        except ValueError as exc:
            raise ProposalNotFoundError(selector) from exc
        if self.index is not None:
            rows = self.index.rows(self, "proposal_id=?", (proposal_id,))
            if not rows:
                raise ProposalNotFoundError(selector)
            return self._read_located(
                rows[0],
                "admission",
                ProposalAdmissionRecord,
                render=admission_bytes,
            )
        path = self.proposals / f"{proposal_id.removeprefix('sha256:')}.json"
        if not path.exists():
            raise ProposalNotFoundError(selector)
        record = self._read_model(
            path,
            ProposalAdmissionRecord,
            label="proposal admission",
            render=admission_bytes,
        )
        if record.proposal_id != proposal_id:
            raise ProposalIntegrityError("proposal admission lookup names another admission")
        return record

    def admissions_at_target(self, target_ref: str) -> tuple[ProposalAdmissionRecord, ...]:
        """The admissions submitted under one exact target ref, through its locator.

        A dedupe by target ref reads these, never every admission retained.
        """

        if self.index is not None:
            return tuple(
                self._read_located(
                    row, "admission", ProposalAdmissionRecord, render=admission_bytes
                )
                for row in self.index.rows(
                    self, "target_ref=? AND admission_path IS NOT NULL", (target_ref,)
                )
            )
        return tuple(item for item in self.list_admissions() if item.target_ref == target_ref)

    def list_admissions(self) -> tuple[ProposalAdmissionRecord, ...]:
        """List canonical admissions in stable evidence-filename order."""

        if self.index is not None:
            return tuple(
                self._read_located(
                    row, "admission", ProposalAdmissionRecord, render=admission_bytes
                )
                for row in self.index.rows(self, "admission_path IS NOT NULL")
            )
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
        if self.index is not None:
            row = self.index.locate(self, proposal_id)
            if row["evaluation_path"] is None:
                raise ProposalIntegrityError(
                    "proposal evidence must contain exactly one evaluation for the admission"
                )
            return self._read_located(row, "evaluation", ProposalEvaluationRecord)
        # Explicit cold/recovery operation for an unbound source store.
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

        if self.index is not None:
            return tuple(
                self._read_located(row, "evaluation", ProposalEvaluationRecord)
                for row in self.index.rows(self, "evaluation_path IS NOT NULL")
            )
        return tuple(
            self._read_model(path, ProposalEvaluationRecord, label="proposal evaluation")
            for path in sorted(self.evaluations.glob("*.json"), key=lambda item: item.name)
        )

    def read_candidate(self, candidate_digest_value: str) -> CandidateRecordAnyVersion:
        """Read one canonical validated candidate by its frozen C_s digest."""

        raw = read_candidate_bytes(self.candidates, candidate_digest_value)
        assert raw is not None
        return parse_candidate_evidence(raw, expected_digest=candidate_digest_value)

    def read_candidate_review_summary_if_present(
        self, candidate_digest_value: str
    ) -> CandidateReviewSummary | None:
        """Read immutable prose/parent metadata; absent interrupted writes stay absent."""

        return read_candidate_review_summary(self.candidates, candidate_digest_value)

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

    def _read_located(
        self,
        row: dict[str, Any],
        kind: str,
        model: type[_EvidenceModelT],
        *,
        render: Callable[[Any], bytes] | None = None,
    ) -> _EvidenceModelT:
        from cruxible_core.indexes.proposals.proposal_index import file_digest

        if row[kind + "_path"] is None:
            raise ProposalIntegrityError(f"proposal {kind} evidence is missing")
        relative = Path(row[kind + "_path"])
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2:
            raise ProposalIntegrityError("proposal locator path escapes source directory")
        raw = self.read_record_bytes(self.root / relative)
        if file_digest(raw) != row[kind + "_digest"]:
            raise ProposalIntegrityError("proposal located evidence digest differs")
        record = self.parse_model_bytes(raw, model, label="proposal " + kind, render=render)
        if getattr(record, "proposal_id", None) != row["proposal_id"]:
            raise ProposalIntegrityError("proposal located evidence names another admission")
        return record

    @staticmethod
    def read_record_bytes(path: Path) -> bytes:
        directory_fd = file_fd = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            file_fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ProposalIntegrityError("proposal evidence is not a regular file")
            with os.fdopen(file_fd, "rb") as stream:
                file_fd = None
                return stream.read()
        except OSError as exc:
            raise ProposalIntegrityError("proposal evidence is missing or unavailable") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)

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

        raw = ProposalEvidenceStore.read_record_bytes(path)
        return ProposalEvidenceStore.parse_model_bytes(raw, model, label=label, render=render)

    @staticmethod
    def parse_model_bytes(
        raw: bytes,
        model: type[_EvidenceModelT],
        *,
        label: str,
        render: Callable[[Any], bytes] | None = None,
    ) -> _EvidenceModelT:
        """Apply the ordinary evidence validation to already read bytes."""
        try:
            value = model.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise ProposalIntegrityError(f"{label} evidence is malformed") from exc
        expected = (
            canonical_bytes(value.model_dump(mode="json")) + b"\n"
            if render is None
            else render(value)
        )
        if expected != raw:
            raise ProposalIntegrityError(f"{label} evidence is not canonical")
        if isinstance(value, ProposalAdmissionRecord):
            expected_id = proposal_admission_id(
                actor_id=value.actor_id,
                request=ProposalAdmissionRequest(
                    target_ref=value.target_ref,
                    proposed_base_oid=value.proposed_base_oid,
                    source_compilation_digest=value.source_compilation_digest,
                    claim_type_expansions=value.claim_type_expansions,
                    rationale=value.rationale,
                ),
                candidate_commit_oid=value.candidate_commit_oid,
                candidate_tree_oid=value.candidate_tree_oid,
                admitted_at=value.admitted_at,
                limits=value.limits,
            )
            if value.proposal_id != expected_id:
                raise ProposalIntegrityError("proposal admission does not reproduce its identity")
        return value


__all__ = ["ProposalEvidenceStore"]
