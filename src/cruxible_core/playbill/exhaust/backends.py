"""Storage-neutral journal protocol and durable local append-only backend."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.errors import PlaybillJournalError
from cruxible_core.playbill.exhaust.records import (
    JournalHeadVectorV1,
    JournalPartitionHeadV1,
    JournalRangeV1,
    JournalStreamIdentityV1,
    ProcedureJournalRecordDraftV1,
    ProcedureJournalRecordV1,
    StoredProcedureJournalRecordV1,
    journal_genesis_digest,
    journal_head_key,
    procedure_journal_record_digest,
    verify_journal_range,
)

_FRAME_HEADER_BYTES = 8
_MAX_RECORD_BYTES = 16 * 1024 * 1024


class JournalBackendProtocol(Protocol):
    """Minimum backend seam shared by local, mirror, and future hosted stores."""

    def read_head(
        self, stream: JournalStreamIdentityV1, partition_id: str
    ) -> JournalPartitionHeadV1: ...

    def append(
        self,
        draft: ProcedureJournalRecordDraftV1,
        *,
        expected_head: JournalPartitionHeadV1,
        fencing_token: str,
    ) -> StoredProcedureJournalRecordV1: ...

    def read_exact_range(
        self,
        journal_range: JournalRangeV1,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]: ...

    def import_verified_range(
        self,
        records: tuple[StoredProcedureJournalRecordV1, ...],
        *,
        expected_head: JournalPartitionHeadV1,
    ) -> JournalPartitionHeadV1: ...


class _StrictBackendModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JournalWriterStateV1(_StrictBackendModel):
    tag: Literal["playbill-journal-writer-state-v1"] = "playbill-journal-writer-state-v1"
    generation: int
    fencing_token: str
    active: bool

    @field_validator("generation")
    @classmethod
    def _generation(cls, value: int) -> int:
        if value < 1:
            raise ValueError("journal writer generation must be positive")
        return value

    @field_validator("fencing_token")
    @classmethod
    def _fencing_token(cls, value: str) -> str:
        if not value or len(value) > 256 or value != value.strip():
            raise ValueError("journal fencing token must be nonblank canonical text")
        return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensive OS contract
                raise PlaybillJournalError("journal metadata write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_path, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()
        raise


def _identity_key(stream: JournalStreamIdentityV1) -> str:
    return hashlib.sha256(canonical_bytes(stream.model_dump(mode="json"))).hexdigest()


def _partition_key(partition_id: str) -> str:
    return hashlib.sha256(partition_id.encode("utf-8")).hexdigest()


def _record_matches_draft(
    record: ProcedureJournalRecordV1,
    draft: ProcedureJournalRecordDraftV1,
) -> bool:
    record_payload = record.model_dump(
        mode="json",
        exclude={"tag", "sequence", "previous_record_digest"},
    )
    draft_payload = draft.model_dump(mode="json", exclude={"tag"})
    return record_payload == draft_payload


class LocalJournalBackend:
    """Length-framed canonical records; indexes are rebuilt by scanning the log."""

    def __init__(self, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise PlaybillJournalError("journal root must be an existing regular directory")
        self.root = root.resolve(strict=True)
        streams = self.root / "streams"
        streams.mkdir(mode=0o700, exist_ok=True)
        if streams.is_symlink() or not streams.is_dir():
            raise PlaybillJournalError("journal streams directory is not trustworthy")
        os.chmod(streams, 0o700)
        self._streams_root = streams.resolve(strict=True)

    def _partition_directory(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        *,
        create: bool,
    ) -> Path:
        # Validation is shared with the frozen head grammar.
        JournalPartitionHeadV1(
            stream=stream,
            partition_id=partition_id,
            sequence=0,
            record_digest=journal_genesis_digest(stream, partition_id),
        )
        stream_directory = self._streams_root / _identity_key(stream)
        partition_directory = stream_directory / _partition_key(partition_id)
        if create:
            stream_directory.mkdir(mode=0o700, exist_ok=True)
            partition_directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(stream_directory, 0o700)
            os.chmod(partition_directory, 0o700)
        if partition_directory.exists():
            if partition_directory.is_symlink() or not partition_directory.is_dir():
                raise PlaybillJournalError("journal partition directory is not trustworthy")
            if partition_directory.resolve(strict=True).parent != stream_directory.resolve(
                strict=True
            ):
                raise PlaybillJournalError("journal partition directory escapes its stream")
            self._verify_or_initialize_identity(
                partition_directory,
                stream=stream,
                partition_id=partition_id,
                create=create,
            )
        return partition_directory

    @staticmethod
    def _verify_or_initialize_identity(
        directory: Path,
        *,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        create: bool,
    ) -> None:
        identity_path = directory / "identity.json"
        expected = canonical_bytes(
            {
                "tag": "playbill-local-journal-partition-v1",
                "stream": stream.model_dump(mode="json"),
                "partition_id": partition_id,
            }
        )
        if not identity_path.exists():
            if not create:
                raise PlaybillJournalError("journal partition identity is missing")
            _atomic_write(identity_path, expected)
            return
        if identity_path.is_symlink() or not identity_path.is_file():
            raise PlaybillJournalError("journal partition identity is not a regular file")
        if identity_path.read_bytes() != expected:
            raise PlaybillJournalError("journal partition identity substitution detected")

    @staticmethod
    def _read_records_from_directory(
        directory: Path,
        *,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        recover_tail: bool,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        path = directory / "records.log"
        if not path.exists():
            return ()
        if path.is_symlink() or not path.is_file():
            raise PlaybillJournalError("journal record log is not a regular file")
        mode = os.O_RDWR if recover_tail else os.O_RDONLY
        descriptor = os.open(path, mode | getattr(os, "O_NOFOLLOW", 0))
        records: list[StoredProcedureJournalRecordV1] = []
        valid_end = 0
        try:
            size = os.fstat(descriptor).st_size
            offset = 0
            previous = journal_genesis_digest(stream, partition_id)
            while offset < size:
                header = os.pread(descriptor, _FRAME_HEADER_BYTES, offset)
                if len(header) < _FRAME_HEADER_BYTES:
                    break
                length = int.from_bytes(header, "big")
                if length <= 0 or length > _MAX_RECORD_BYTES:
                    raise PlaybillJournalError("journal frame length is invalid")
                body = os.pread(descriptor, length, offset + _FRAME_HEADER_BYTES)
                if len(body) < length:
                    break
                try:
                    raw = json.loads(body)
                    stored = StoredProcedureJournalRecordV1.model_validate(raw)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise PlaybillJournalError("journal record frame is malformed") from exc
                if canonical_bytes(stored.model_dump(mode="json")) != body:
                    raise PlaybillJournalError("journal record frame is not canonical")
                record = stored.record
                if (
                    record.stream != stream
                    or record.partition_id != partition_id
                    or record.sequence != len(records) + 1
                    or record.previous_record_digest != previous
                ):
                    raise PlaybillJournalError("journal record chain or coordinate is corrupt")
                records.append(stored)
                previous = stored.record_digest
                offset += _FRAME_HEADER_BYTES + length
                valid_end = offset
            if valid_end != size:
                if not recover_tail:
                    raise PlaybillJournalError("journal has an incomplete crash tail")
                os.ftruncate(descriptor, valid_end)
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if recover_tail and valid_end != size:
            _fsync_directory(directory)
        return tuple(records)

    def _records(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        *,
        create: bool = False,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        directory = self._partition_directory(
            stream,
            partition_id,
            create=create,
        )
        if not directory.exists():
            return ()
        return self._read_records_from_directory(
            directory,
            stream=stream,
            partition_id=partition_id,
            recover_tail=True,
        )

    def read_head(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
    ) -> JournalPartitionHeadV1:
        records = self._records(stream, partition_id)
        digest = (
            records[-1].record_digest if records else journal_genesis_digest(stream, partition_id)
        )
        return JournalPartitionHeadV1(
            stream=stream,
            partition_id=partition_id,
            sequence=len(records),
            record_digest=digest,
        )

    def read_head_vector(
        self,
        partitions: tuple[tuple[JournalStreamIdentityV1, str], ...],
    ) -> JournalHeadVectorV1:
        heads = tuple(self.read_head(stream, partition) for stream, partition in partitions)
        return JournalHeadVectorV1(partitions=tuple(sorted(heads, key=journal_head_key)))

    def _writer_state(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
    ) -> JournalWriterStateV1 | None:
        directory = self._partition_directory(stream, partition_id, create=False)
        if not directory.exists():
            return None
        path = directory / "writer.json"
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise PlaybillJournalError("journal writer state is not a regular file")
        try:
            state = JournalWriterStateV1.model_validate(json.loads(path.read_bytes()))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PlaybillJournalError("journal writer state is malformed") from exc
        if canonical_bytes(state.model_dump(mode="json")) != path.read_bytes():
            raise PlaybillJournalError("journal writer state is not canonical")
        return state

    def activate_writer(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        *,
        fencing_token: str,
        expected_head: JournalPartitionHeadV1,
    ) -> JournalWriterStateV1:
        current_head = self.read_head(stream, partition_id)
        if expected_head != current_head:
            raise PlaybillJournalError("writer activation expected head is stale or substituted")
        directory = self._partition_directory(stream, partition_id, create=True)
        previous = self._writer_state(stream, partition_id)
        if previous is not None and previous.active:
            if previous.fencing_token == fencing_token:
                return previous
            raise PlaybillJournalError("journal partition already has an active fenced writer")
        state = JournalWriterStateV1(
            generation=1 if previous is None else previous.generation + 1,
            fencing_token=fencing_token,
            active=True,
        )
        _atomic_write(directory / "writer.json", canonical_bytes(state.model_dump(mode="json")))
        return state

    def fence_writer(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        *,
        expected_fencing_token: str,
    ) -> JournalWriterStateV1:
        directory = self._partition_directory(stream, partition_id, create=False)
        state = self._writer_state(stream, partition_id)
        if state is None or state.fencing_token != expected_fencing_token:
            raise PlaybillJournalError("journal writer fencing token does not match")
        if not state.active:
            return state
        fenced = JournalWriterStateV1(
            generation=state.generation,
            fencing_token=state.fencing_token,
            active=False,
        )
        _atomic_write(directory / "writer.json", canonical_bytes(fenced.model_dump(mode="json")))
        return fenced

    def append(
        self,
        draft: ProcedureJournalRecordDraftV1,
        *,
        expected_head: JournalPartitionHeadV1,
        fencing_token: str,
    ) -> StoredProcedureJournalRecordV1:
        if expected_head.stream != draft.stream or expected_head.partition_id != draft.partition_id:
            raise PlaybillJournalError("append expected head names another partition")
        directory = self._partition_directory(
            draft.stream,
            draft.partition_id,
            create=True,
        )
        records = self._records(draft.stream, draft.partition_id, create=True)
        current = self.read_head(draft.stream, draft.partition_id)
        writer = self._writer_state(draft.stream, draft.partition_id)
        if writer is None or not writer.active or writer.fencing_token != fencing_token:
            raise PlaybillJournalError("append requires the current active fencing token")

        # A retried append against its old expected head reproduces the prior result.
        if (
            records
            and current.sequence == expected_head.sequence + 1
            and records[-1].record.previous_record_digest == expected_head.record_digest
            and _record_matches_draft(records[-1].record, draft)
        ):
            return records[-1]
        if current != expected_head:
            raise PlaybillJournalError("append expected head is stale or forked")

        record = ProcedureJournalRecordV1.bind(
            draft,
            sequence=current.sequence + 1,
            previous_record_digest=current.record_digest,
        )
        stored = StoredProcedureJournalRecordV1(
            record=record,
            record_digest=procedure_journal_record_digest(record),
        )
        body = canonical_bytes(stored.model_dump(mode="json"))
        if len(body) > _MAX_RECORD_BYTES:
            raise PlaybillJournalError("journal record exceeds the frozen local frame limit")
        frame = len(body).to_bytes(_FRAME_HEADER_BYTES, "big") + body
        path = directory / "records.log"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            view = memoryview(frame)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:  # pragma: no cover - defensive OS contract
                    raise PlaybillJournalError("journal append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
        _fsync_directory(directory)
        return stored

    def read_exact_range(
        self,
        journal_range: JournalRangeV1,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        records = self._records(journal_range.stream, journal_range.partition_id)
        selected = records[journal_range.first_sequence - 1 : journal_range.last_sequence]
        result = tuple(selected)
        verify_journal_range(journal_range, result)
        return result

    def range_from_sequences(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        *,
        first_sequence: int,
        last_sequence: int,
    ) -> JournalRangeV1:
        records = self._records(stream, partition_id)
        if first_sequence < 1 or last_sequence < first_sequence or last_sequence > len(records):
            raise PlaybillJournalError("requested journal sequence range is unavailable")
        previous = (
            journal_genesis_digest(stream, partition_id)
            if first_sequence == 1
            else records[first_sequence - 2].record_digest
        )
        return JournalRangeV1(
            stream=stream,
            partition_id=partition_id,
            first_sequence=first_sequence,
            last_sequence=last_sequence,
            expected_previous_digest=previous,
            expected_head_digest=records[last_sequence - 1].record_digest,
        )

    def import_verified_range(
        self,
        records: tuple[StoredProcedureJournalRecordV1, ...],
        *,
        expected_head: JournalPartitionHeadV1,
    ) -> JournalPartitionHeadV1:
        if not records:
            return expected_head
        first = records[0].record
        if (
            first.stream != expected_head.stream
            or first.partition_id != expected_head.partition_id
            or first.sequence != expected_head.sequence + 1
            or first.previous_record_digest != expected_head.record_digest
        ):
            raise PlaybillJournalError("journal import does not extend the exact local head")
        journal_range = JournalRangeV1(
            stream=first.stream,
            partition_id=first.partition_id,
            first_sequence=first.sequence,
            last_sequence=records[-1].record.sequence,
            expected_previous_digest=expected_head.record_digest,
            expected_head_digest=records[-1].record_digest,
        )
        imported_head = verify_journal_range(journal_range, records)
        current = self.read_head(first.stream, first.partition_id)
        if current != expected_head:
            raise PlaybillJournalError("journal import local head changed or names a fork")
        directory = self._partition_directory(first.stream, first.partition_id, create=True)
        path = directory / "records.log"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            for stored in records:
                body = canonical_bytes(stored.model_dump(mode="json"))
                if len(body) > _MAX_RECORD_BYTES:
                    raise PlaybillJournalError("imported record exceeds local frame limit")
                frame = len(body).to_bytes(_FRAME_HEADER_BYTES, "big") + body
                view = memoryview(frame)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:  # pragma: no cover
                        raise PlaybillJournalError("journal import made no progress")
                    view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
        _fsync_directory(directory)
        return imported_head

    def recover_partition(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
    ) -> JournalPartitionHeadV1:
        """Discard only an incomplete final frame and rebuild the derived index."""

        self._records(stream, partition_id)
        return self.read_head(stream, partition_id)

    def all_records(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        """Return the verified complete local prefix for index rebuilds and export planning."""

        return self._records(stream, partition_id)

    def _record_log_path_for_testing(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
    ) -> Path:
        """Return the private log path for deterministic crash-boundary fixtures."""

        return self._partition_directory(stream, partition_id, create=True) / "records.log"


__all__ = [
    "JournalBackendProtocol",
    "JournalWriterStateV1",
    "LocalJournalBackend",
]
