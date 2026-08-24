"""Distinct optional witness-record contract for accepted Playbill generations."""

from __future__ import annotations

import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import GenerationRoot, SemanticRoot, canonical_bytes
from cruxible_client.contracts.types import GitObjectFormat

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class WitnessRecord(BaseModel):
    """One external monotonic statement about an already accepted `G_n`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-witness-v1"] = "playbill-witness-v1"
    instance_id: str
    object_format: GitObjectFormat
    head_oid: str
    semantic_root: str
    generation_root: str
    sequence: int = Field(ge=0)

    @field_validator("head_oid")
    @classmethod
    def _head_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("witness head OID is malformed")
        return value

    @field_validator("semantic_root")
    @classmethod
    def _semantic_root(cls, value: str) -> str:
        SemanticRoot.from_tagged(value)
        return value

    @field_validator("generation_root")
    @classmethod
    def _generation_root(cls, value: str) -> str:
        GenerationRoot.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _oid_format(self) -> "WitnessRecord":
        expected = 40 if self.object_format == "sha1" else 64
        if len(self.head_oid) != expected:
            raise ValueError("witness head OID differs from object format")
        return self


class WitnessSink(Protocol):
    """Optional external service seam; capture landing events use another contract."""

    def publish(self, record: WitnessRecord) -> None: ...

    def latest(self, instance_id: str) -> WitnessRecord | None: ...


def render_witness_record(record: WitnessRecord) -> bytes:
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


__all__ = ["WitnessRecord", "WitnessSink", "render_witness_record"]
