"""Stable landing-trigger occurrence identities, independent of retry attempts."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.capture_journal import CaptureLandingEventV1

_LINE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_RAW_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _StrictOccurrenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class OccurrenceAttemptV1(_StrictOccurrenceModel):
    tag: Literal["playbill-occurrence-attempt-v1"] = "playbill-occurrence-attempt-v1"
    occurrence_digest: str
    attempt: int = Field(ge=1)

    @field_validator("occurrence_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


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


__all__ = [
    "CaptureLandingOccurrenceV1",
    "OccurrenceAttemptV1",
    "capture_landing_occurrence",
    "occurrence_digest",
]
