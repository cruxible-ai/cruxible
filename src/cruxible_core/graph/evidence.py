"""Structured evidence metadata for governed graph facts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from cruxible_core.primitives import canonical_json


class EvidenceRef(BaseModel):
    """Durable pointer to source evidence behind a graph fact or signal."""

    source: str
    source_record_id: str
    artifact_id: str | None = None
    table: str | None = None
    row_index: int | None = None
    label: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _collect_extra_metadata(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        known = set(cls.model_fields)
        payload = dict(value)
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ValueError("EvidenceRef metadata must be an object")
        extra = {
            str(key): payload.pop(key)
            for key in list(payload)
            if key not in known
        }
        payload["metadata"] = {**dict(metadata), **extra}
        return payload

    @field_validator("source", "source_record_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("EvidenceRef source and source_record_id must be non-empty")
        return value

    @model_serializer(mode="plain")
    def _serialize_compact(self) -> dict[str, Any]:
        return self._compact_payload()

    def to_payload(self) -> dict[str, Any]:
        """Return a compact JSON-compatible payload."""
        return self._compact_payload()

    def _compact_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "source_record_id": self.source_record_id,
        }
        if self.artifact_id is not None:
            payload["artifact_id"] = self.artifact_id
        if self.table is not None:
            payload["table"] = self.table
        if self.row_index is not None:
            payload["row_index"] = self.row_index
        if self.label is not None:
            payload["label"] = self.label
        if not self.metadata:
            return payload
        payload["metadata"] = self.metadata
        return payload


class RelationshipEvidence(BaseModel):
    """Cruxible-owned evidence metadata attached to an accepted relationship."""

    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    rationale: str | None = None
    source_group_id: str | None = None
    source_receipt_ids: list[str] = Field(default_factory=list)
    source_trace_ids: list[str] = Field(default_factory=list)
    source_step_ids: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


def normalize_evidence_ref(value: EvidenceRef | Mapping[str, Any]) -> EvidenceRef:
    """Validate one evidence reference payload."""
    if isinstance(value, EvidenceRef):
        return value
    return EvidenceRef.model_validate(value)


def evidence_ref_payload(value: EvidenceRef | Mapping[str, Any]) -> dict[str, Any]:
    """Validate and dump one evidence reference."""
    return normalize_evidence_ref(value).to_payload()


def merge_evidence_ref_objects(
    *groups: Iterable[EvidenceRef | Mapping[str, Any]],
) -> list[EvidenceRef]:
    """Merge evidence reference groups as validated objects with first-seen dedupe."""
    merged: list[EvidenceRef] = []
    seen: set[str] = set()
    for group in groups:
        for ref in group:
            evidence = normalize_evidence_ref(ref)
            key = _evidence_ref_key(evidence)
            if key in seen:
                continue
            seen.add(key)
            merged.append(evidence)
    return merged


def merge_evidence_refs(
    *groups: Iterable[EvidenceRef | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge evidence reference groups with deterministic first-seen dedupe."""
    return [evidence.to_payload() for evidence in merge_evidence_ref_objects(*groups)]


def _evidence_ref_key(ref: EvidenceRef) -> str:
    stable_identity = {
        "source": ref.source,
        "source_record_id": ref.source_record_id,
        "artifact_id": ref.artifact_id,
        "table": ref.table,
        "row_index": ref.row_index,
        "criteria": ref.metadata.get("criteria"),
        "match_criteria_id": ref.metadata.get("match_criteria_id"),
    }
    return canonical_json(stable_identity)


__all__ = [
    "EvidenceRef",
    "RelationshipEvidence",
    "evidence_ref_payload",
    "merge_evidence_ref_objects",
    "merge_evidence_refs",
    "normalize_evidence_ref",
]
