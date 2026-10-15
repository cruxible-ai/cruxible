"""Storage-neutral journal protocol and durable local append-only backend."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.content_memo import ParseMemo, ValueMemo
from cruxible_client.contracts.errors import (
    JournalConflictError,
    JournalError,
    JournalIntegrityError,
)
from cruxible_core.exhaust.journal_index import JournalIndex, journal_locked
from cruxible_core.exhaust.records import (
    JournalPartitionHeadV1,
    JournalRangeV1,
    JournalStreamIdentityV1,
    ProcedureJournalRecordDraftV1,
    ProcedureJournalRecordV1,
    StoredProcedureJournalRecordV1,
    journal_genesis_digest,
    journal_stream_directory_key,
    journal_stream_strings,
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
                raise JournalError("journal metadata write made no progress")
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
    return journal_stream_directory_key(stream)


@dataclass(frozen=True)
class _PartitionKeys:
    """One partition's validated names and identity bytes, derived once per process."""

    stream_directory: str
    partition_directory: str
    identity: bytes


#: By the stream identity's strings and the partition id; an invalid coordinate
#: refuses before anything is kept. One entry per run partition a process touches.
_PARTITION_KEYS: ValueMemo[_PartitionKeys] = ValueMemo(capacity=4096)


def _partition_keys(stream: JournalStreamIdentityV1, partition_id: str) -> _PartitionKeys:
    def derive() -> _PartitionKeys:
        # Validation is shared with the frozen head grammar.
        JournalPartitionHeadV1(
            stream=stream,
            partition_id=partition_id,
            sequence=0,
            record_digest=journal_genesis_digest(stream, partition_id),
        )
        return _PartitionKeys(
            stream_directory=_identity_key(stream),
            partition_directory=hashlib.sha256(partition_id.encode("utf-8")).hexdigest(),
            identity=canonical_bytes(
                {
                    "tag": "playbill-local-journal-partition-v1",
                    "stream": stream.model_dump(mode="json"),
                    "partition_id": partition_id,
                }
            ),
        )

    return _PARTITION_KEYS.derive((*journal_stream_strings(stream), partition_id), derive)


def _parse_journal_frame(body: bytes) -> StoredProcedureJournalRecordV1:
    try:
        stored = StoredProcedureJournalRecordV1.model_validate_json(body)
    except ValueError as exc:
        raise JournalIntegrityError("journal record frame is malformed") from exc
    if canonical_bytes(stored.model_dump(mode="json")) != body:
        raise JournalIntegrityError("journal record frame is not canonical")
    return stored


#: Verified frames by their exact bytes, shared sealed: an appended record is
#: read back by the index catch-up and then by every selection that names it.
_FRAME_PARSES: ParseMemo[StoredProcedureJournalRecordV1] = ParseMemo(
    capacity=8192, max_bytes=32 * 1024 * 1024
)


def parse_journal_frame(body: bytes) -> StoredProcedureJournalRecordV1:
    """Verify one frame body (strict model, reproduced digest, exact canonical bytes) once.

    Only the frame's own content is remembered; where it sits in its partition
    (coordinate, sequence and chain) is checked by every read.
    """

    return _FRAME_PARSES.parse(body, "journal-frame", None, lambda: _parse_journal_frame(body))


def journal_log_path(root: Path, stream: JournalStreamIdentityV1, partition_id: str) -> Path:
    """Where one partition's append-only log lives under a journal root, touching nothing.

    The log is the journal's authority: a reader that must notice an append
    without opening the journal stats this file.
    """

    keys = _partition_keys(stream, partition_id)
    return root / "streams" / keys.stream_directory / keys.partition_directory / "records.log"


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


def _parse_writer_state(content: bytes) -> JournalWriterStateV1:
    try:
        state = JournalWriterStateV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise JournalIntegrityError("journal writer state is malformed") from exc
    if canonical_bytes(state.model_dump(mode="json")) != content:
        raise JournalIntegrityError("journal writer state is not canonical")
    return state


#: Writer fences by their exact bytes (a frozen model of scalars): every append
#: reads the fence, which changes only on activation and fencing.
_WRITER_STATES: ValueMemo[JournalWriterStateV1] = ValueMemo(capacity=1024)


class LocalJournalBackend:
    """Length-framed canonical records; indexes are rebuilt by scanning the log."""

    def __init__(self, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise JournalError("journal root must be an existing regular directory")
        self.root = root.resolve(strict=True)
        streams = self.root / "streams"
        streams.mkdir(mode=0o700, exist_ok=True)
        if streams.is_symlink() or not streams.is_dir():
            raise JournalError("journal streams directory is not trustworthy")
        os.chmod(streams, 0o700)
        self._streams_root = streams.resolve(strict=True)

    def _partition_directory(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        *,
        create: bool,
    ) -> Path:
        keys = _partition_keys(stream, partition_id)
        stream_directory = self._streams_root / keys.stream_directory
        partition_directory = stream_directory / keys.partition_directory
        if create:
            stream_directory.mkdir(mode=0o700, exist_ok=True)
            partition_directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(stream_directory, 0o700)
            os.chmod(partition_directory, 0o700)
        if partition_directory.exists():
            if partition_directory.is_symlink() or not partition_directory.is_dir():
                raise JournalError("journal partition directory is not trustworthy")
            if partition_directory.resolve(strict=True).parent != stream_directory.resolve(
                strict=True
            ):
                raise JournalError("journal partition directory escapes its stream")
            self._verify_or_initialize_identity(
                partition_directory,
                expected=keys.identity,
                create=create,
            )
        return partition_directory

    @staticmethod
    def _verify_or_initialize_identity(
        directory: Path,
        *,
        expected: bytes,
        create: bool,
    ) -> None:
        identity_path = directory / "identity.json"
        if not identity_path.exists():
            if not create:
                raise JournalIntegrityError("journal partition identity is missing")
            _atomic_write(identity_path, expected)
            return
        if identity_path.is_symlink() or not identity_path.is_file():
            raise JournalIntegrityError("journal partition identity is not a regular file")
        if identity_path.read_bytes() != expected:
            raise JournalIntegrityError("journal partition identity substitution detected")

    @staticmethod
    def _read_frames(
        directory: Path,
        *,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        recover_tail: bool,
        tolerate_tail: bool = False,
        offset: int = 0,
        sequence: int = 0,
        previous: str | None = None,
    ) -> Generator[tuple[int, int, StoredProcedureJournalRecordV1], None, None]:
        """One verifier for rebuilds, tail catch-up, and indexed exact reads.

        Only a writer holding the journal lock may ``recover_tail``: another
        process's reader cannot tell a crash tail from an append still being
        written, so a reader that must not refuse passes ``tolerate_tail`` and
        stops at the last complete frame without truncating anything.
        """
        path = directory / "records.log"
        if not path.exists():
            return
        if path.is_symlink() or not path.is_file():
            raise JournalError("journal record log is not a regular file")
        descriptor = os.open(
            path, (os.O_RDWR if recover_tail else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
        )
        valid_end = offset
        try:
            size = os.fstat(descriptor).st_size
            previous = previous or journal_genesis_digest(stream, partition_id)
            while offset < size:
                header = os.pread(descriptor, _FRAME_HEADER_BYTES, offset)
                if len(header) < _FRAME_HEADER_BYTES:
                    break
                length = int.from_bytes(header, "big")
                if length <= 0 or length > _MAX_RECORD_BYTES:
                    raise JournalIntegrityError("journal frame length is invalid")
                body = os.pread(descriptor, length, offset + _FRAME_HEADER_BYTES)
                if len(body) < length:
                    break
                stored = parse_journal_frame(body)
                record = stored.record
                if (
                    record.stream != stream
                    or record.partition_id != partition_id
                    or record.sequence != sequence + 1
                    or record.previous_record_digest != previous
                ):
                    raise JournalIntegrityError("journal record chain or coordinate is corrupt")
                yield offset, length + _FRAME_HEADER_BYTES, stored
                previous = stored.record_digest
                sequence += 1
                offset += _FRAME_HEADER_BYTES + length
                valid_end = offset
            if valid_end != size:
                if not recover_tail:
                    if tolerate_tail:
                        return
                    raise JournalIntegrityError("journal has an incomplete crash tail")
                os.ftruncate(descriptor, valid_end)
                os.fsync(descriptor)
                _fsync_directory(directory)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_records_from_directory(
        directory: Path, *, stream: JournalStreamIdentityV1, partition_id: str, recover_tail: bool
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        return tuple(
            stored
            for _, _, stored in LocalJournalBackend._read_frames(
                directory,
                stream=stream,
                partition_id=partition_id,
                recover_tail=recover_tail,
                tolerate_tail=not recover_tail,
            )
        )

    @property
    def index(self) -> JournalIndex:

        return JournalIndex(self)

    def select_records(
        self, stream: JournalStreamIdentityV1, **filters: Any
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        """Read only matching retained records; SQL is a rebuildable locator."""
        return self.index.select(stream, **filters)

    def _records(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        *,
        recover_tail: bool,
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
            recover_tail=recover_tail,
        )

    def read_head(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
    ) -> JournalPartitionHeadV1:
        return self.index.head(stream, partition_id)

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
            raise JournalError("journal writer state is not a regular file")
        content = path.read_bytes()
        return _WRITER_STATES.derive(content, lambda: _parse_writer_state(content))

    def writer_state(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
    ) -> JournalWriterStateV1 | None:
        """Return the durable fence for one partition; reading never appends."""

        return self._writer_state(stream, partition_id)

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
            raise JournalConflictError("writer activation expected head is stale or substituted")
        directory = self._partition_directory(stream, partition_id, create=True)
        previous = self._writer_state(stream, partition_id)
        if previous is not None and previous.active:
            if previous.fencing_token == fencing_token:
                return previous
            raise JournalConflictError("journal partition already has an active fenced writer")
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
            raise JournalConflictError("journal writer fencing token does not match")
        if not state.active:
            return state
        fenced = JournalWriterStateV1(
            generation=state.generation,
            fencing_token=state.fencing_token,
            active=False,
        )
        _atomic_write(directory / "writer.json", canonical_bytes(fenced.model_dump(mode="json")))
        return fenced

    @journal_locked
    def append(
        self,
        draft: ProcedureJournalRecordDraftV1,
        *,
        expected_head: JournalPartitionHeadV1,
        fencing_token: str,
    ) -> StoredProcedureJournalRecordV1:
        if expected_head.stream != draft.stream or expected_head.partition_id != draft.partition_id:
            raise JournalConflictError("append expected head names another partition")
        directory = self._partition_directory(
            draft.stream,
            draft.partition_id,
            create=True,
        )
        # Readers never truncate; the writer discards a crash tail before extending.
        self.index.sync(draft.stream, draft.partition_id, recover_tail=True)
        records = self.select_records(
            draft.stream, partition_id=draft.partition_id, descending=True, limit=1
        )
        current = self.read_head(draft.stream, draft.partition_id)
        writer = self._writer_state(draft.stream, draft.partition_id)
        if writer is None or not writer.active or writer.fencing_token != fencing_token:
            raise JournalConflictError("append requires the current active fencing token")

        # A retried append against its old expected head reproduces the prior result.
        if (
            records
            and current.sequence == expected_head.sequence + 1
            and records[-1].record.previous_record_digest == expected_head.record_digest
            and _record_matches_draft(records[-1].record, draft)
        ):
            return records[-1]
        if current != expected_head:
            raise JournalConflictError("append expected head is stale or forked")

        record = ProcedureJournalRecordV1.bind(
            draft,
            sequence=current.sequence + 1,
            previous_record_digest=current.record_digest,
        )
        stored = StoredProcedureJournalRecordV1.bind_stored(record)
        body = canonical_bytes(stored.model_dump(mode="json"))
        if len(body) > _MAX_RECORD_BYTES:
            raise JournalError("journal record exceeds the frozen local frame limit")
        frame = len(body).to_bytes(_FRAME_HEADER_BYTES, "big") + body
        self.index.mark_dirty(draft.stream, draft.partition_id)
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
                    raise JournalError("journal append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
        _fsync_directory(directory)
        self.index.sync(draft.stream, draft.partition_id)
        return stored

    def read_exact_range(
        self,
        journal_range: JournalRangeV1,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        result = self.select_records(
            journal_range.stream,
            partition_id=journal_range.partition_id,
            first_sequence=journal_range.first_sequence,
            last_sequence=journal_range.last_sequence,
        )
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
        records = self.select_records(
            stream,
            partition_id=partition_id,
            first_sequence=first_sequence,
            last_sequence=last_sequence,
        )
        if (
            first_sequence < 1
            or last_sequence < first_sequence
            or len(records) != last_sequence - first_sequence + 1
        ):
            raise JournalError("requested journal sequence range is unavailable")
        previous = records[0].record.previous_record_digest
        return JournalRangeV1(
            stream=stream,
            partition_id=partition_id,
            first_sequence=first_sequence,
            last_sequence=last_sequence,
            expected_previous_digest=previous,
            expected_head_digest=records[-1].record_digest,
        )

    @journal_locked
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
            raise JournalConflictError("journal import does not extend the exact local head")
        journal_range = JournalRangeV1(
            stream=first.stream,
            partition_id=first.partition_id,
            first_sequence=first.sequence,
            last_sequence=records[-1].record.sequence,
            expected_previous_digest=expected_head.record_digest,
            expected_head_digest=records[-1].record_digest,
        )
        imported_head = verify_journal_range(journal_range, records)
        self.index.sync(first.stream, first.partition_id, recover_tail=True)
        current = self.read_head(first.stream, first.partition_id)
        if current != expected_head:
            raise JournalConflictError("journal import local head changed or names a fork")
        directory = self._partition_directory(first.stream, first.partition_id, create=True)
        self.index.mark_dirty(first.stream, first.partition_id)
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
                    raise JournalError("imported record exceeds local frame limit")
                frame = len(body).to_bytes(_FRAME_HEADER_BYTES, "big") + body
                view = memoryview(frame)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:  # pragma: no cover
                        raise JournalError("journal import made no progress")
                    view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
        _fsync_directory(directory)
        self.index.sync(first.stream, first.partition_id)
        return imported_head

    @journal_locked
    def recover_partition(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
    ) -> JournalPartitionHeadV1:
        """Discard only an incomplete final frame and rebuild the derived index."""

        self._records(stream, partition_id, recover_tail=True)
        return self.read_head(stream, partition_id)

    def catch_up_index(self, stream: JournalStreamIdentityV1) -> None:
        """Bring the journal index up to every record appended since its last sync."""

        self.index.catch_up(stream, self.partition_ids(stream))

    def all_records(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        """Return the verified complete local prefix for index rebuilds and export planning."""

        return self._records(stream, partition_id, recover_tail=False)

    def partition_ids(self, stream: JournalStreamIdentityV1) -> tuple[str, ...]:
        """List authenticated local partition identities for rebuild-only scans."""

        stream_directory = self._streams_root / _identity_key(stream)
        if not stream_directory.exists():
            return ()
        if stream_directory.is_symlink() or not stream_directory.is_dir():
            raise JournalError("journal stream directory is not trustworthy")
        found: list[str] = []
        for directory in stream_directory.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                raise JournalError("journal partition directory is not trustworthy")
            identity_path = directory / "identity.json"
            if identity_path.is_symlink() or not identity_path.is_file():
                raise JournalIntegrityError("journal partition identity is not trustworthy")
            try:
                payload = json.loads(identity_path.read_bytes())
                partition_id = payload["partition_id"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise JournalIntegrityError("journal partition identity is invalid") from exc
            if not isinstance(partition_id, str):
                raise JournalIntegrityError("journal partition identity is invalid")
            expected = self._partition_directory(stream, partition_id, create=False)
            if expected != directory:
                raise JournalIntegrityError("journal partition directory identity mismatches")
            found.append(partition_id)
        return tuple(sorted(found, key=lambda value: value.encode("utf-8")))

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
