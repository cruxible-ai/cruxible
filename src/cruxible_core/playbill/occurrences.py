"""Stable trigger occurrence identities, independent of retry attempts."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.capture_journal import CaptureCursorV1, CaptureLandingEventV1

_LINE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_RAW_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class _StrictOccurrenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _line_id(value: str) -> str:
    if not _LINE_ID_RE.fullmatch(value):
        raise ValueError("occurrence line_id must be canonical")
    return value


def _tagged_digest(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


def _cursor_vector(value: tuple[CaptureCursorV1, ...]) -> tuple[CaptureCursorV1, ...]:
    partitions = tuple(cursor.partition_id.encode("ascii") for cursor in value)
    if partitions != tuple(sorted(set(partitions))):
        raise ValueError("occurrence cursor vector must be sorted and unique by partition")
    return value


class CaptureLandingOccurrenceV1(_StrictOccurrenceModel):
    tag: Literal["playbill-capture-landing-occurrence-v1"] = (
        "playbill-capture-landing-occurrence-v1"
    )
    line_id: str
    occurrence_epoch: int = Field(ge=0)
    trigger_kind: Literal["capture_landing"] = "capture_landing"
    partition_id: str
    sequence: int = Field(ge=0, le=(2**64) - 1)
    event_id: str

    @field_validator("line_id")
    @classmethod
    def _line_id(cls, value: str) -> str:
        if not _LINE_ID_RE.fullmatch(value):
            raise ValueError("landing occurrence line_id must be canonical")
        return value

    @field_validator("partition_id", "event_id")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _RAW_SHA256_RE.fullmatch(value):
            raise ValueError("landing occurrence journal identifiers must be raw SHA-256")
        return value


class CadenceOccurrenceV1(_StrictOccurrenceModel):
    """One scheduled tick; the tick index, never a wall clock, carries identity."""

    tag: Literal["playbill-cadence-occurrence-v1"] = "playbill-cadence-occurrence-v1"
    line_id: str
    occurrence_epoch: int = Field(ge=0)
    trigger_kind: Literal["cadence"] = "cadence"
    cadence_policy_digest: str
    tick_index: int = Field(ge=0, le=(2**63) - 1)

    _line = field_validator("line_id")(_line_id)
    _policy = field_validator("cadence_policy_digest")(_tagged_digest)


class WindowCloseOccurrenceV1(_StrictOccurrenceModel):
    """One closed window identified by the journal cursors it covers, not by time."""

    tag: Literal["playbill-window-close-occurrence-v1"] = "playbill-window-close-occurrence-v1"
    line_id: str
    occurrence_epoch: int = Field(ge=0)
    trigger_kind: Literal["window_close"] = "window_close"
    window_policy_digest: str
    from_cursors: tuple[CaptureCursorV1, ...]
    to_cursors: tuple[CaptureCursorV1, ...]

    _line = field_validator("line_id")(_line_id)
    _policy = field_validator("window_policy_digest")(_tagged_digest)
    _cursors = field_validator("from_cursors", "to_cursors")(_cursor_vector)

    @model_validator(mode="after")
    def _covers_a_real_advance(self) -> "WindowCloseOccurrenceV1":
        if window_advance_count(self.from_cursors, self.to_cursors) < 1:
            raise ValueError("window-close occurrence must cover at least one new landing")
        return self


class ManualOccurrenceV1(_StrictOccurrenceModel):
    """One explicitly requested occurrence; the request handle carries identity."""

    tag: Literal["playbill-manual-occurrence-v1"] = "playbill-manual-occurrence-v1"
    line_id: str
    occurrence_epoch: int = Field(ge=0)
    trigger_kind: Literal["manual"] = "manual"
    request_id: str

    _line = field_validator("line_id")(_line_id)

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        if not _REQUEST_ID_RE.fullmatch(value):
            raise ValueError("manual occurrence request_id must be a canonical identifier")
        return value


LineOccurrenceV1 = Annotated[
    CadenceOccurrenceV1 | CaptureLandingOccurrenceV1 | ManualOccurrenceV1 | WindowCloseOccurrenceV1,
    Field(discriminator="trigger_kind"),
]


class OccurrenceAttemptV1(_StrictOccurrenceModel):
    tag: Literal["playbill-occurrence-attempt-v1"] = "playbill-occurrence-attempt-v1"
    occurrence_digest: str
    attempt: int = Field(ge=1)

    @field_validator("occurrence_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


def window_advance_count(
    from_cursors: tuple[CaptureCursorV1, ...],
    to_cursors: tuple[CaptureCursorV1, ...],
) -> int:
    """Count the landings a window covers; refuse a rewritten or regressed cursor."""

    lower = {cursor.partition_id: cursor for cursor in from_cursors}
    covered = 0
    for cursor in to_cursors:
        previous = lower.pop(cursor.partition_id, None)
        if previous is None:
            covered += cursor.sequence + 1
            continue
        if cursor.sequence < previous.sequence:
            raise ValueError("window cursor vector regressed on a partition")
        if cursor.sequence == previous.sequence and cursor.event_id != previous.event_id:
            raise ValueError("window cursor vector rewrote a partition event")
        covered += cursor.sequence - previous.sequence
    if lower:
        raise ValueError("window cursor vector dropped a previously covered partition")
    return covered


def capture_landing_occurrence(
    *,
    line_id: str,
    occurrence_epoch: int,
    anchor: CaptureLandingEventV1,
) -> CaptureLandingOccurrenceV1:
    return CaptureLandingOccurrenceV1(
        line_id=line_id,
        occurrence_epoch=occurrence_epoch,
        partition_id=anchor.partition_id,
        sequence=anchor.sequence,
        event_id=anchor.event_id,
    )


def occurrence_digest(occurrence: CaptureLandingOccurrenceV1) -> str:
    payload = occurrence.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-capture-landing-occurrence-v1",
        payload,
    ).tagged


_OCCURRENCE_DIGEST_DOMAINS: dict[str, str] = {
    "cadence": "playbill-cadence-occurrence-v1",
    "manual": "playbill-manual-occurrence-v1",
    "window_close": "playbill-window-close-occurrence-v1",
}


def line_occurrence_digest(occurrence: LineOccurrenceV1) -> str:
    """Address any trigger occurrence without disturbing the frozen landing preimage."""

    if isinstance(occurrence, CaptureLandingOccurrenceV1):
        return occurrence_digest(occurrence)
    payload = occurrence.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        _OCCURRENCE_DIGEST_DOMAINS[occurrence.trigger_kind],
        payload,
    ).tagged


__all__ = [
    "CadenceOccurrenceV1",
    "CaptureLandingOccurrenceV1",
    "LineOccurrenceV1",
    "ManualOccurrenceV1",
    "OccurrenceAttemptV1",
    "WindowCloseOccurrenceV1",
    "capture_landing_occurrence",
    "line_occurrence_digest",
    "occurrence_digest",
    "window_advance_count",
]
