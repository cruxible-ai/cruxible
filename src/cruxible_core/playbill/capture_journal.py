"""Partitioned operational landing journal for inert Capture envelopes."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.captures import (
    CaptureEnvelopeV1,
    CaptureRunCoordinateV1,
    capture_digest,
)
from cruxible_core.playbill.errors import PlaybillFormatError

_RAW_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CaptureJournalError(PlaybillFormatError):
    """A landing append violates partition, idempotency, or chain law."""


class _StrictJournalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptureLandingEventV1(_StrictJournalModel):
    tag: Literal["playbill-capture-landing-v1"] = "playbill-capture-landing-v1"
    instance_id: str
    partition_id: str
    sequence: int = Field(ge=0, le=(2**64) - 1)
    event_id: str
    idempotency_key: str
    capture_digest: str
    capture_contract_digest: str
    run_coordinate: CaptureRunCoordinateV1
    run_receipt_digest: str
    producer_binding_digest: str
    previous_event_digest: str | None
    landed_at: datetime

    @field_validator("partition_id", "event_id", "idempotency_key", "previous_event_digest")
    @classmethod
    def _raw_digest(cls, value: str | None) -> str | None:
        if value is not None and not _RAW_SHA256_RE.fullmatch(value):
            raise ValueError("landing journal identifiers must be raw lowercase SHA-256")
        return value

    @field_validator(
        "capture_digest",
        "capture_contract_digest",
        "run_receipt_digest",
        "producer_binding_digest",
    )
    @classmethod
    def _tagged_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("landed_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("landing time must be timezone-aware")
        return value

    @property
    def cursor(self) -> str:
        return (
            f"playbill-capture-cursor-v1:{self.partition_id}:{self.sequence:020d}:{self.event_id}"
        )


class CaptureCursorV1(_StrictJournalModel):
    tag: Literal["playbill-capture-cursor-v1"] = "playbill-capture-cursor-v1"
    partition_id: str
    sequence: int = Field(ge=0, le=(2**64) - 1)
    event_id: str

    @field_validator("partition_id", "event_id")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _RAW_SHA256_RE.fullmatch(value):
            raise ValueError("Capture cursor identifiers must be raw lowercase SHA-256")
        return value

    @classmethod
    def parse(cls, value: str) -> "CaptureCursorV1":
        prefix = "playbill-capture-cursor-v1:"
        if not value.startswith(prefix):
            raise CaptureJournalError("Capture cursor has an unsupported version")
        parts = value[len(prefix) :].split(":")
        if len(parts) != 3 or len(parts[1]) != 20 or not parts[1].isdigit():
            raise CaptureJournalError("Capture cursor is malformed")
        return cls(partition_id=parts[0], sequence=int(parts[1]), event_id=parts[2])

    def render(self) -> str:
        return f"{self.tag}:{self.partition_id}:{self.sequence:020d}:{self.event_id}"


def capture_partition_id(*, instance_id: str, envelope: CaptureEnvelopeV1) -> str:
    return typed_digest(
        Sha256Value,
        "playbill-capture-partition-v1",
        {
            "instance_id": instance_id,
            "capture_contract_digest": envelope.capture_contract_digest,
            "producer_binding_digest": envelope.producer_binding_digest,
        },
    ).value


def capture_landing_idempotency_key(
    *,
    instance_id: str,
    envelope: CaptureEnvelopeV1,
) -> str:
    return typed_digest(
        Sha256Value,
        "playbill-capture-landing-idempotency-v1",
        {
            "instance_id": instance_id,
            "capture_contract_digest": envelope.capture_contract_digest,
            "run_coordinate": envelope.run_coordinate.model_dump(mode="json"),
            "run_receipt_digest": envelope.run_receipt_digest,
            "producer_binding_digest": envelope.producer_binding_digest,
            "capture_digest": capture_digest(envelope).tagged,
        },
    ).value


def capture_landing_event_id(event: CaptureLandingEventV1) -> str:
    payload = event.model_dump(mode="json")
    payload.pop("event_id")
    payload.pop("tag")
    return typed_digest(Sha256Value, "playbill-capture-landing-v1", payload).value


@runtime_checkable
class CaptureLandingJournalProtocol(Protocol):
    def append(
        self,
        *,
        instance_id: str,
        envelope: CaptureEnvelopeV1,
        landed_at: datetime,
        idempotency_key: str,
    ) -> CaptureLandingEventV1: ...

    def events_after(self, cursor: str | None = None) -> tuple[CaptureLandingEventV1, ...]: ...


class InMemoryCaptureLandingJournal:
    """Reference semantics for an append-only per-partition journal."""

    def __init__(self) -> None:
        self._partitions: dict[str, list[CaptureLandingEventV1]] = {}
        self._idempotency: dict[str, CaptureLandingEventV1] = {}

    def append(
        self,
        *,
        instance_id: str,
        envelope: CaptureEnvelopeV1,
        landed_at: datetime,
        idempotency_key: str,
    ) -> CaptureLandingEventV1:
        expected_key = capture_landing_idempotency_key(
            instance_id=instance_id,
            envelope=envelope,
        )
        if idempotency_key != expected_key:
            raise CaptureJournalError("landing idempotency key does not match its exact preimage")
        existing = self._idempotency.get(idempotency_key)
        if existing is not None:
            if (
                existing.capture_digest != capture_digest(envelope).tagged
                or existing.run_coordinate != envelope.run_coordinate
                or existing.run_receipt_digest != envelope.run_receipt_digest
                or existing.producer_binding_digest != envelope.producer_binding_digest
            ):
                raise CaptureJournalError("landing retry reuses a key with a different payload")
            return existing
        partition_id = capture_partition_id(instance_id=instance_id, envelope=envelope)
        partition = self._partitions.setdefault(partition_id, [])
        sequence = len(partition)
        previous = partition[-1].event_id if partition else None
        provisional = CaptureLandingEventV1(
            instance_id=instance_id,
            partition_id=partition_id,
            sequence=sequence,
            event_id="0" * 64,
            idempotency_key=idempotency_key,
            capture_digest=capture_digest(envelope).tagged,
            capture_contract_digest=envelope.capture_contract_digest,
            run_coordinate=envelope.run_coordinate,
            run_receipt_digest=envelope.run_receipt_digest,
            producer_binding_digest=envelope.producer_binding_digest,
            previous_event_digest=previous,
            landed_at=landed_at,
        )
        event = provisional.model_copy(update={"event_id": capture_landing_event_id(provisional)})
        partition.append(event)
        self._idempotency[idempotency_key] = event
        return event

    def events_after(self, cursor: str | None = None) -> tuple[CaptureLandingEventV1, ...]:
        if cursor is None:
            return tuple(
                event
                for partition_id in sorted(self._partitions)
                for event in self._partitions[partition_id]
            )
        parsed = CaptureCursorV1.parse(cursor)
        partition = self._partitions.get(parsed.partition_id)
        if partition is None or parsed.sequence >= len(partition):
            raise CaptureJournalError("Capture cursor does not resolve in this journal")
        anchor = partition[parsed.sequence]
        if anchor.event_id != parsed.event_id:
            raise CaptureJournalError("Capture cursor event does not match its partition chain")
        return tuple(partition[parsed.sequence + 1 :])

    def vector_cursor(self) -> tuple[CaptureCursorV1, ...]:
        return tuple(
            CaptureCursorV1(
                partition_id=partition_id,
                sequence=events[-1].sequence,
                event_id=events[-1].event_id,
            )
            for partition_id, events in sorted(self._partitions.items())
            if events
        )

    def verify(self) -> None:
        for partition_id, partition in self._partitions.items():
            previous: str | None = None
            for sequence, event in enumerate(partition):
                if (
                    event.partition_id != partition_id
                    or event.sequence != sequence
                    or event.previous_event_digest != previous
                    or capture_landing_event_id(event.model_copy(update={"event_id": "0" * 64}))
                    != event.event_id
                ):
                    raise CaptureJournalError("Capture landing partition chain is corrupt")
                previous = event.event_id


__all__ = [
    "CaptureCursorV1",
    "CaptureJournalError",
    "CaptureLandingEventV1",
    "CaptureLandingJournalProtocol",
    "InMemoryCaptureLandingJournal",
    "capture_landing_event_id",
    "capture_landing_idempotency_key",
    "capture_partition_id",
]
