"""The core records a Subject's recorded assignment names (identity D1, review F-001).

A minted Subject, and a further holder of a ``hint`` key, record the operation
and member that created them (``SubjectShell.assignment``), and their id is a
digest of the two. The digest proves only that the id matches what is
recorded; what proves core assigned it is a durable core record of that
operation which creates exactly that member of that kind:

- an authoring intent in this instance's intent store (the operation is its
  intent id; the record is the intent's own payload), or
- the record a Procedure terminal writes when it delivers a change creating
  Subjects (``subject-creations/<operation>.json``): the operation is the
  terminal's lowering intent id, recomputed from the journaled admission binding
  and node it names, and the record lists the creation members it lowered.

Both are found by the operation id alone (one path), written only by core, and
never changed by a rebase or readmission of the proposal, so the boundary's
answer at admission is its answer at settlement. A raw proposal names no such
record, so it creates only keyed first holders, whose ids are recomputed from
their keys.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_client.contracts.authoring.models import (
    AuthoringIntentV1,
    ChangeSetAuthoringPayload,
    SubjectCreationAuthoringPayload,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_core.authoring.store import AuthoringIntentStore

#: Where a Procedure terminal's creation records live, under the exhaust root.
PROCEDURE_SUBJECT_CREATIONS = "subject-creations"
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


class ProcedureSubjectCreationsV1(BaseModel):
    """What one Procedure terminal operation creates: (kind, member) pairs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-procedure-subject-creations-v1"] = (
        "cruxible-procedure-subject-creations-v1"
    )
    operation: str
    admission_binding_digest: str
    node_id: str
    creations: tuple[tuple[str, str], ...]


def intent_creates(intent: AuthoringIntentV1, member: str, kind: str) -> bool:
    """Whether ``intent``'s own payload creates exactly ``member`` of ``kind``."""

    payload = intent.payload
    members = payload.members if isinstance(payload, ChangeSetAuthoringPayload) else (payload,)
    return any(
        isinstance(item, SubjectCreationAuthoringPayload)
        and item.member_identity == member
        and item.subject_kind == kind
        for item in members
    )


class SubjectAssignmentRecords:
    """The candidate boundary's lookup of a recorded assignment (``SubjectAssignmentAuthority``)."""

    def __init__(self, exhaust_root: Path) -> None:
        self.exhaust_root = exhaust_root
        self._intents: AuthoringIntentStore | None = None

    def __call__(self, operation: str, member: str, kind: str) -> bool:
        if not _OPERATION.fullmatch(operation):
            return False
        intent = self._intent(operation)
        if intent is not None:
            return intent_creates(intent, member, kind)
        record = self.procedure_record(operation)
        return record is not None and (kind, member) in record.creations

    def _intent(self, operation: str) -> AuthoringIntentV1 | None:
        if self._intents is None:
            if not (self.exhaust_root / "authoring-intents").is_dir():
                return None
            self._intents = AuthoringIntentStore(self.exhaust_root, read_only=True)
        return self._intents.intent_record(operation)

    def _procedure_path(self, operation: str) -> Path:
        return self.exhaust_root / PROCEDURE_SUBJECT_CREATIONS / f"{operation}.json"

    def procedure_record(self, operation: str) -> ProcedureSubjectCreationsV1 | None:
        """The creation record of one Procedure operation, or None when absent or invalid."""

        path = self._procedure_path(operation)
        if path.is_symlink() or not path.is_file():
            return None
        try:
            record = ProcedureSubjectCreationsV1.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            return None
        expected = procedure_operation_id(
            admission_binding_digest=record.admission_binding_digest, node_id=record.node_id
        )
        if record.operation != operation or expected != operation:
            return None
        return record

    def record_procedure_operation(
        self,
        *,
        admission_binding_digest: str,
        node_id: str,
        creations: tuple[tuple[str, str], ...],
    ) -> str:
        """Record what one Procedure terminal operation creates, before it is submitted.

        Idempotent: a re-delivery of the same operation records the union (an
        operation's members are fixed by its admitted request).
        """

        operation = procedure_operation_id(
            admission_binding_digest=admission_binding_digest, node_id=node_id
        )
        held = self.procedure_record(operation)
        merged = tuple(sorted({*creations, *(() if held is None else held.creations)}))
        if held is not None and held.creations == merged:
            return operation
        record = ProcedureSubjectCreationsV1(
            operation=operation,
            admission_binding_digest=admission_binding_digest,
            node_id=node_id,
            creations=merged,
        )
        directory = self.exhaust_root / PROCEDURE_SUBJECT_CREATIONS
        directory.mkdir(mode=0o700, exist_ok=True)
        temporary = directory / f".{operation}-{secrets.token_hex(8)}"
        temporary.write_bytes(
            json.dumps(record.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        )
        os.replace(temporary, self._procedure_path(operation))
        return operation


__all__ = [
    "PROCEDURE_OPERATION_ID_DOMAIN",
    "PROCEDURE_SUBJECT_CREATIONS",
    "ProcedureSubjectCreationsV1",
    "SubjectAssignmentRecords",
    "intent_creates",
    "procedure_operation_id",
]
