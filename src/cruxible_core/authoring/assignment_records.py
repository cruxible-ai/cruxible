"""The core records a Subject's recorded assignment names (identity D1, review F-001).

A minted Subject, and a further holder of a ``hint`` key, record the operation
and member that created them (``SubjectShell.assignment``), and their id is a
digest of the two. What proves core assigned that id is the record core keeps
of the operation's actual creations (``subject-creations/<operation>.json``):
for each creation member, its kind, canonical key encoding, rule
(``first_holder``, ``further_holder`` or ``minted``), outcome (``created`` or
``reused``) and the id it assigned or reused (``RecordedCreation``).

Core writes it when it lowers a stored authoring intent (its preflight) and
when a Procedure terminal delivers, before anything is proposed; a preview
writes nothing. The candidate boundary authorizes a minted or further-holder
creation only when the record of the operation it names holds an equal
``created`` entry -- the same member, kind, key, rule and id -- so a binding
copied to another key, kind or operation, or taken from a member core only
reused, refuses. The record is found by the operation id alone (one path) and
only grows, so a rebase or readmission of the proposal leaves its answer as it
was. It is replaced whole (temporary file, flush, rename), so a reader sees
the record before or after an update, never part of one.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.indexes.subject_identity import RecordedCreation
from cruxible_core.storage.preview_fence import is_previewing

#: Where the creation records live, under the exhaust root.
SUBJECT_CREATION_RECORDS = "subject-creations"
PROCEDURE_OPERATION_ID_DOMAIN = "playbill-procedure-proposal-intent-id-v1"
_OPERATION = re.compile(r"AIT-[A-Za-z0-9]{1,128}")


def procedure_operation_id(*, admission_binding_digest: str, node_id: str) -> str:
    """The intent id one Procedure terminal lowers under: its admission and node alone."""

    digest = typed_digest(
        Sha256Value,
        PROCEDURE_OPERATION_ID_DOMAIN,
        {"admission_binding_digest": admission_binding_digest, "node_id": node_id},
    ).tagged.removeprefix("sha256:")
    return f"AIT-{digest[:32]}"


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    member: str
    kind: str
    key: str | None
    rule: Literal["first_holder", "further_holder", "minted"]
    outcome: Literal["created", "reused"]
    subject_id: str


class SubjectCreationRecordV1(BaseModel):
    """Every creation core lowered for one operation, with its actual assignment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-subject-creation-record-v1"] = "cruxible-subject-creation-record-v1"
    operation: str
    creations: tuple[_Entry, ...]

    def recorded(self) -> frozenset[RecordedCreation]:
        return frozenset(RecordedCreation(**entry.model_dump()) for entry in self.creations)


class SubjectAssignmentRecords:
    """The boundary's ``SubjectAssignmentAuthority``, and where core writes the records."""

    def __init__(self, exhaust_root: Path) -> None:
        self.exhaust_root = exhaust_root
        self.directory = exhaust_root / SUBJECT_CREATION_RECORDS

    def __call__(self, operation: str, creation: RecordedCreation) -> bool:
        if creation.outcome != "created":
            return False
        record = self.record(operation)
        return record is not None and creation in record.recorded()

    def _path(self, operation: str) -> Path:
        return self.directory / f"{operation}.json"

    def record(self, operation: str) -> SubjectCreationRecordV1 | None:
        """The creation record of one operation, or None when absent or invalid."""

        if not _OPERATION.fullmatch(operation):
            return None
        path = self._path(operation)
        if path.is_symlink() or not path.is_file():
            return None
        try:
            record = SubjectCreationRecordV1.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            return None
        return record if record.operation == operation else None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def record_creations(self, operation: str, creations: Iterable[RecordedCreation]) -> None:
        """Add one lowering's actual creations to its operation's record (never in a preview)."""

        found = tuple(creations)
        if not found or is_previewing() or not _OPERATION.fullmatch(operation):
            return
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with self._locked():
            held = self.record(operation)
            merged = {*found, *(() if held is None else held.recorded())}
            if held is not None and merged == held.recorded():
                return
            record = SubjectCreationRecordV1(
                operation=operation,
                creations=tuple(
                    _Entry(**vars(item))
                    for item in sorted(
                        merged,
                        key=lambda item: (item.member, item.rule, item.outcome, item.subject_id),
                    )
                ),
            )
            temporary = self.directory / f".{operation}.{secrets.token_hex(8)}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                view = memoryview(canonical_bytes(record.model_dump(mode="json")))
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self._path(operation))
            directory = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)


__all__ = [
    "PROCEDURE_OPERATION_ID_DOMAIN",
    "SUBJECT_CREATION_RECORDS",
    "SubjectAssignmentRecords",
    "SubjectCreationRecordV1",
    "procedure_operation_id",
]
