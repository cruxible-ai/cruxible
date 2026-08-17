"""Packing-independent identities and records for Playbill operational journals."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    CanonicalValue,
    CasDigest,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.errors import PlaybillJournalError
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.temporal import ensure_utc, format_datetime

PROCEDURE_EXHAUST_JOURNAL_FAMILY = "procedure-exhaust-v1"
REGISTERED_JOURNAL_FAMILIES: tuple[str, ...] = (PROCEDURE_EXHAUST_JOURNAL_FAMILY,)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PARTITION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HEX_SIGNATURE_RE = re.compile(r"^[0-9a-f]{128}$")

JournalEventKindV1 = Literal[
    "occurrence_materialized",
    "attempt_started",
    "admission_bound",
    "node_fired",
    "branch_evaluated",
    "source_acquisition",
    "produced_capture",
    "item_dependencies",
    "effect_intent",
    "effect_result",
    "terminal_egress",
    "attempt_finalized",
    "resolution_activation",
    "resolution",
    "resolution_disposition",
    "procedure_reading",
]


class _StrictJournalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _identifier(value: str, *, label: str, pattern: re.Pattern[str] = _IDENTIFIER_RE) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"{label} must be a stable canonical identifier")
    return value


def _digest(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be tagged lowercase SHA-256") from exc
    return value


class JournalStreamIdentityV1(_StrictJournalModel):
    """Backend-independent identity for one logical operational stream."""

    tag: Literal["playbill-journal-stream-identity-v1"] = "playbill-journal-stream-identity-v1"
    instance_id: str
    journal_family: str
    stream_id: str

    @field_validator("instance_id", "stream_id")
    @classmethod
    def _stable_identifier(cls, value: str, info: object) -> str:
        return _identifier(value, label=str(getattr(info, "field_name", "journal identity")))

    @field_validator("journal_family")
    @classmethod
    def _journal_family(cls, value: str) -> str:
        if value not in REGISTERED_JOURNAL_FAMILIES:
            supported = ", ".join(REGISTERED_JOURNAL_FAMILIES)
            raise ValueError(f"unknown journal family {value!r}; supported: {supported}")
        return value

    @property
    def identity_digest(self) -> str:
        return typed_digest(
            ArtifactDigest,
            "playbill-journal-stream-identity-v1",
            {"stream": self.model_dump(mode="json")},
        ).tagged


def journal_genesis_digest(stream: JournalStreamIdentityV1, partition_id: str) -> str:
    """Return the domain-separated predecessor for sequence one."""

    _identifier(partition_id, label="journal partition_id", pattern=_PARTITION_RE)
    return typed_digest(
        ArtifactDigest,
        "playbill-journal-partition-genesis-v1",
        {
            "stream": stream.model_dump(mode="json"),
            "partition_id": partition_id,
        },
    ).tagged


class JournalPartitionHeadV1(_StrictJournalModel):
    """One record digest that transitively commits the complete partition prefix."""

    tag: Literal["playbill-journal-partition-head-v1"] = "playbill-journal-partition-head-v1"
    stream: JournalStreamIdentityV1
    partition_id: str
    sequence: int = Field(ge=0)
    record_digest: str

    @field_validator("partition_id")
    @classmethod
    def _partition_id(cls, value: str) -> str:
        return _identifier(value, label="journal partition_id", pattern=_PARTITION_RE)

    @field_validator("record_digest")
    @classmethod
    def _record_digest(cls, value: str) -> str:
        return _digest(value, label="journal head record_digest")

    @model_validator(mode="after")
    def _genesis_head(self) -> "JournalPartitionHeadV1":
        if self.sequence == 0 and self.record_digest != journal_genesis_digest(
            self.stream, self.partition_id
        ):
            raise ValueError("sequence-zero journal head must carry the partition genesis digest")
        return self


def journal_head_key(
    head: JournalPartitionHeadV1,
) -> tuple[bytes, bytes, bytes, bytes]:
    return (
        head.stream.instance_id.encode("utf-8"),
        head.stream.journal_family.encode("utf-8"),
        head.stream.stream_id.encode("utf-8"),
        head.partition_id.encode("utf-8"),
    )


class JournalHeadVectorV1(_StrictJournalModel):
    """A sorted vector of independent partition-prefix commitments."""

    tag: Literal["playbill-journal-head-vector-v1"] = "playbill-journal-head-vector-v1"
    partitions: tuple[JournalPartitionHeadV1, ...]

    @field_validator("partitions")
    @classmethod
    def _partitions(
        cls, value: tuple[JournalPartitionHeadV1, ...]
    ) -> tuple[JournalPartitionHeadV1, ...]:
        keys = tuple(journal_head_key(item) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("journal head-vector partitions must be sorted and unique")
        return value

    @property
    def vector_digest(self) -> str:
        return typed_digest(
            ArtifactDigest,
            "playbill-journal-head-vector-v1",
            {"head_vector": self.model_dump(mode="json")},
        ).tagged


class JournalHeadStatementV1(_StrictJournalModel):
    """Authenticated head assertion; this is not an external witness receipt."""

    tag: Literal["playbill-journal-head-statement-v1"] = "playbill-journal-head-statement-v1"
    head_vector: JournalHeadVectorV1
    signer_id: str
    signing_key_id: str
    signing_role: Literal["journal_head"] = "journal_head"
    asserted_at: datetime

    @field_validator("signer_id", "signing_key_id")
    @classmethod
    def _signer_identifier(cls, value: str, info: object) -> str:
        return _identifier(value, label=str(getattr(info, "field_name", "head signer")))

    @field_validator("asserted_at")
    @classmethod
    def _asserted_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("asserted_at", when_used="json")
    def _serialize_asserted_at(self, value: datetime) -> str | None:
        return format_datetime(value)


def journal_head_statement_bytes(statement: JournalHeadStatementV1) -> bytes:
    return canonical_bytes(
        {
            "tag": "playbill-journal-head-signature-v1",
            "statement": statement.model_dump(mode="json"),
        }
    )


class JournalHeadManifestV1(_StrictJournalModel):
    """Portable signed head material verifiable without a backend database."""

    tag: Literal["playbill-journal-head-manifest-v1"] = "playbill-journal-head-manifest-v1"
    statement: JournalHeadStatementV1
    signature: str

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        if not _HEX_SIGNATURE_RE.fullmatch(value):
            raise ValueError("journal-head signature must be 64 bytes of lowercase hex")
        return value


class JournalHeadSignerProtocol(Protocol):
    """Client/daemon-held signer seam; private material never enters a wire model."""

    @property
    def signer_id(self) -> str: ...

    @property
    def signing_key_id(self) -> str: ...

    def sign_journal_head(self, message: bytes) -> str: ...


def build_journal_head_manifest(
    head_vector: JournalHeadVectorV1,
    *,
    asserted_at: datetime,
    signer: JournalHeadSignerProtocol,
) -> JournalHeadManifestV1:
    statement = JournalHeadStatementV1(
        head_vector=head_vector,
        signer_id=signer.signer_id,
        signing_key_id=signer.signing_key_id,
        asserted_at=asserted_at,
    )
    return JournalHeadManifestV1(
        statement=statement,
        signature=signer.sign_journal_head(journal_head_statement_bytes(statement)),
    )


def verify_journal_head_manifest(
    manifest: JournalHeadManifestV1,
    *,
    expected_public_key: str,
) -> None:
    """Verify one journal-writer assertion without granting witness semantics."""

    if not re.fullmatch(r"[0-9a-f]{64}", expected_public_key):
        raise PlaybillJournalError("journal-head public key must be 32 bytes of lowercase hex")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(expected_public_key)).verify(
            bytes.fromhex(manifest.signature),
            journal_head_statement_bytes(manifest.statement),
        )
    except (InvalidSignature, ValueError) as exc:
        raise PlaybillJournalError("journal-head signature verification failed") from exc


class ProcedureJournalRecordDraftV1(_StrictJournalModel):
    """Packing-free Procedure exhaust content before chain coordinates are assigned."""

    tag: Literal["playbill-procedure-journal-record-draft-v1"] = (
        "playbill-procedure-journal-record-draft-v1"
    )
    stream: JournalStreamIdentityV1
    partition_id: str
    event_kind: JournalEventKindV1
    accepted_coordinate: AcceptedCoordinate
    procedure_artifact_digest: str
    definition_digest: str
    run_id: str
    line_spec_digest: str | None = None
    occurrence_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    admission_binding_digest: str | None = None
    payload_digest: str
    actor_context: GovernedActorContext
    recorded_at: datetime

    @field_validator("partition_id")
    @classmethod
    def _partition_id(cls, value: str) -> str:
        return _identifier(value, label="journal partition_id", pattern=_PARTITION_RE)

    @field_validator("run_id", "occurrence_id")
    @classmethod
    def _event_identifier(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _identifier(value, label=str(getattr(info, "field_name", "event identifier")))

    @field_validator(
        "procedure_artifact_digest",
        "definition_digest",
        "line_spec_digest",
        "admission_binding_digest",
        "payload_digest",
    )
    @classmethod
    def _digests(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _digest(value, label=str(getattr(info, "field_name", "journal digest")))

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("recorded_at", when_used="json")
    def _serialize_recorded_at(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _family(self) -> "ProcedureJournalRecordDraftV1":
        if self.stream.journal_family != PROCEDURE_EXHAUST_JOURNAL_FAMILY:
            raise ValueError("Procedure journal records require the Procedure exhaust family")
        return self


class ProcedureJournalRecordV1(_StrictJournalModel):
    """One immutable record whose digest commits the exact partition prefix."""

    tag: Literal["playbill-procedure-journal-record-v1"] = "playbill-procedure-journal-record-v1"
    stream: JournalStreamIdentityV1
    partition_id: str
    sequence: int = Field(ge=1)
    previous_record_digest: str
    event_kind: JournalEventKindV1
    accepted_coordinate: AcceptedCoordinate
    procedure_artifact_digest: str
    definition_digest: str
    run_id: str
    line_spec_digest: str | None = None
    occurrence_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    admission_binding_digest: str | None = None
    payload_digest: str
    actor_context: GovernedActorContext
    recorded_at: datetime

    @field_validator("partition_id")
    @classmethod
    def _partition_id(cls, value: str) -> str:
        return _identifier(value, label="journal partition_id", pattern=_PARTITION_RE)

    @field_validator("run_id", "occurrence_id")
    @classmethod
    def _event_identifier(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _identifier(value, label=str(getattr(info, "field_name", "event identifier")))

    @field_validator(
        "previous_record_digest",
        "procedure_artifact_digest",
        "definition_digest",
        "line_spec_digest",
        "admission_binding_digest",
        "payload_digest",
    )
    @classmethod
    def _digests(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _digest(value, label=str(getattr(info, "field_name", "journal digest")))

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("recorded_at", when_used="json")
    def _serialize_recorded_at(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _family(self) -> "ProcedureJournalRecordV1":
        if self.stream.journal_family != PROCEDURE_EXHAUST_JOURNAL_FAMILY:
            raise ValueError("Procedure journal records require the Procedure exhaust family")
        if self.sequence == 1 and self.previous_record_digest != journal_genesis_digest(
            self.stream, self.partition_id
        ):
            raise ValueError("first journal record must commit the partition genesis digest")
        return self

    @classmethod
    def bind(
        cls,
        draft: ProcedureJournalRecordDraftV1,
        *,
        sequence: int,
        previous_record_digest: str,
    ) -> "ProcedureJournalRecordV1":
        return cls(
            **draft.model_dump(mode="python", exclude={"tag"}),
            sequence=sequence,
            previous_record_digest=previous_record_digest,
        )


def procedure_journal_record_digest(record: ProcedureJournalRecordV1) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-journal-record-v1",
        {"record": record.model_dump(mode="json")},
    ).tagged


class StoredProcedureJournalRecordV1(_StrictJournalModel):
    tag: Literal["playbill-stored-procedure-journal-record-v1"] = (
        "playbill-stored-procedure-journal-record-v1"
    )
    record: ProcedureJournalRecordV1
    record_digest: str

    @field_validator("record_digest")
    @classmethod
    def _record_digest(cls, value: str) -> str:
        return _digest(value, label="stored journal record_digest")

    @model_validator(mode="after")
    def _reproduce(self) -> "StoredProcedureJournalRecordV1":
        if self.record_digest != procedure_journal_record_digest(self.record):
            raise ValueError("stored journal record digest does not reproduce")
        return self


class JournalRangeV1(_StrictJournalModel):
    tag: Literal["playbill-journal-range-v1"] = "playbill-journal-range-v1"
    stream: JournalStreamIdentityV1
    partition_id: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    expected_previous_digest: str
    expected_head_digest: str

    @field_validator("partition_id")
    @classmethod
    def _partition_id(cls, value: str) -> str:
        return _identifier(value, label="journal partition_id", pattern=_PARTITION_RE)

    @field_validator("expected_previous_digest", "expected_head_digest")
    @classmethod
    def _range_digest(cls, value: str, info: object) -> str:
        return _digest(value, label=str(getattr(info, "field_name", "range digest")))

    @model_validator(mode="after")
    def _sequence_range(self) -> "JournalRangeV1":
        if self.first_sequence > self.last_sequence:
            raise ValueError("journal range first_sequence must be <= last_sequence")
        return self


def verify_journal_range(
    journal_range: JournalRangeV1,
    records: tuple[StoredProcedureJournalRecordV1, ...],
) -> JournalPartitionHeadV1:
    """Verify exact identity, continuity, and both authenticated range boundaries."""

    expected_count = journal_range.last_sequence - journal_range.first_sequence + 1
    if len(records) != expected_count:
        raise PlaybillJournalError("journal range record count is incomplete")
    previous = journal_range.expected_previous_digest
    for offset, stored in enumerate(records):
        record = stored.record
        sequence = journal_range.first_sequence + offset
        if (
            record.stream != journal_range.stream
            or record.partition_id != journal_range.partition_id
            or record.sequence != sequence
        ):
            raise PlaybillJournalError("journal range contains a substituted record coordinate")
        if record.previous_record_digest != previous:
            raise PlaybillJournalError("journal range chain continuity failed")
        previous = stored.record_digest
    if previous != journal_range.expected_head_digest:
        raise PlaybillJournalError("journal range does not reach its expected head digest")
    return JournalPartitionHeadV1(
        stream=journal_range.stream,
        partition_id=journal_range.partition_id,
        sequence=journal_range.last_sequence,
        record_digest=previous,
    )


def payload_digest(payload: object) -> str:
    """Address one canonical payload that may be stored in CAS separately."""

    return CasDigest(hashlib.sha256(journal_payload_bytes(payload)).hexdigest()).tagged


def journal_payload_bytes(payload: object) -> bytes:
    """Return the exact CAS bytes whose address is stored in a journal record."""

    return canonical_bytes(
        {
            "tag": "playbill-procedure-journal-payload-v1",
            "payload": payload,
        }
    )


def parse_journal_payload(content: bytes) -> CanonicalValue:
    """Verify and return one exact CAS-backed journal payload."""

    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlaybillJournalError("journal payload is malformed") from exc
    if not isinstance(raw, dict) or raw.get("tag") != "playbill-procedure-journal-payload-v1":
        raise PlaybillJournalError("journal payload has an unknown domain tag")
    if set(raw) != {"tag", "payload"} or canonical_bytes(raw) != content:
        raise PlaybillJournalError("journal payload is not in exact canonical form")
    return normalize_canonical(raw["payload"])


__all__ = [
    "JournalEventKindV1",
    "JournalHeadManifestV1",
    "JournalHeadSignerProtocol",
    "JournalHeadStatementV1",
    "JournalHeadVectorV1",
    "JournalPartitionHeadV1",
    "JournalRangeV1",
    "JournalStreamIdentityV1",
    "PROCEDURE_EXHAUST_JOURNAL_FAMILY",
    "ProcedureJournalRecordDraftV1",
    "ProcedureJournalRecordV1",
    "REGISTERED_JOURNAL_FAMILIES",
    "StoredProcedureJournalRecordV1",
    "build_journal_head_manifest",
    "journal_genesis_digest",
    "journal_head_key",
    "journal_head_statement_bytes",
    "journal_payload_bytes",
    "payload_digest",
    "parse_journal_payload",
    "procedure_journal_record_digest",
    "verify_journal_head_manifest",
    "verify_journal_range",
]
