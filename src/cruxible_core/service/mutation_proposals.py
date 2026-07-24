"""Canonical proposal bodies, attached to a mutation receipt before guards run.

A mutation receipt opens with SUMMARY parameters (``{"count": 3}``) and only
records the nodes carrying actual proposed properties AFTER the guards pass. So
the receipt of a REFUSED write — the highest-information row the instance
produces — named a count and nothing else, and a refused write leaves no state
to reconstruct the proposal from.

These helpers build one JSON-safe representation of the submitted proposal
(properties, metadata, evidence, endpoints, every batch member) plus the flat
list of subject coordinates it touches. Call sites hand both to
:meth:`cruxible_core.receipt.builder.ReceiptBuilder.record_proposal` as the
first act inside the receipt boundary, so the body is on the receipt no matter
which layer refuses — guard, validator, or the guard evaluator itself erroring.

The representation is derived from the SUBMITTED inputs, not from validated or
post-write objects: what a refusal must preserve is what the caller asked for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.service.types import (
    BatchDirectWriteInput,
    EntityWriteInput,
    RelationshipWriteInput,
    SharedEvidenceInput,
)


def json_safe(value: Any) -> Any:
    """Convert a submitted payload fragment into canonical-JSON-safe data.

    Proposal bodies are canonically encoded (digest + byte count) and persisted
    as receipt JSON, so every leaf has to be a JSON scalar. Unknown objects
    degrade to ``repr`` rather than raising: a proposal body must never be able
    to fail the mutation it is describing.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            str(key): json_safe(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return repr(value)


def entity_subject(entity_type: str, entity_id: str) -> dict[str, Any]:
    """Subject coordinates for one proposed entity."""
    return {"entity_type": entity_type, "entity_id": entity_id}


def relationship_subject(
    *,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    relationship: str,
) -> dict[str, Any]:
    """Subject coordinates for one proposed edge, in ``relationship_write`` keys."""
    return {
        "from_type": from_type,
        "from_id": from_id,
        "to_type": to_type,
        "to_id": to_id,
        "relationship": relationship,
    }


def entity_input_member(entity: EntityWriteInput) -> dict[str, Any]:
    """Proposal member for one submitted entity write input."""
    return {
        "entity_type": entity.entity_type,
        "entity_id": entity.entity_id,
        "properties": json_safe(entity.properties),
        "metadata": json_safe(entity.metadata),
    }


def entity_instance_member(entity: EntityInstance) -> dict[str, Any]:
    """Proposal member for one submitted entity instance."""
    return {
        "entity_type": entity.entity_type,
        "entity_id": entity.entity_id,
        "properties": json_safe(entity.properties),
        "metadata": json_safe(entity.metadata),
    }


def relationship_input_member(relationship: RelationshipWriteInput) -> dict[str, Any]:
    """Proposal member for one submitted relationship write input."""
    member: dict[str, Any] = {
        "from_type": relationship.from_type,
        "from_id": relationship.from_id,
        "to_type": relationship.to_type,
        "to_id": relationship.to_id,
        "relationship": relationship.relationship_type,
        "properties": json_safe(relationship.properties),
        "pending": relationship.pending,
        "evidence_refs": json_safe(relationship.evidence_refs),
        "source_evidence": json_safe(relationship.source_evidence),
        "evidence_rationale": relationship.evidence_rationale,
        "lifecycle": json_safe(relationship.lifecycle),
    }
    shared_keys = getattr(relationship, "shared_evidence_keys", None)
    if shared_keys:
        member["shared_evidence_keys"] = json_safe(shared_keys)
    return member


def relationship_instance_member(
    relationship: RelationshipInstance,
    *,
    pending: bool | None = None,
    lifecycle: Any = None,
) -> dict[str, Any]:
    """Proposal member for one submitted relationship instance."""
    member: dict[str, Any] = {
        "from_type": relationship.from_type,
        "from_id": relationship.from_id,
        "to_type": relationship.to_type,
        "to_id": relationship.to_id,
        "relationship": relationship.relationship_type,
        "properties": json_safe(relationship.properties),
        "metadata": json_safe(relationship.metadata),
    }
    if pending is not None:
        member["pending"] = pending
    if lifecycle is not None:
        member["lifecycle"] = json_safe(lifecycle)
    return member


def build_proposal(
    *,
    operation: str,
    entities: Sequence[Mapping[str, Any]] = (),
    relationships: Sequence[Mapping[str, Any]] = (),
    extra: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(proposal_body, subjects)`` for one submitted mutation.

    ``subjects`` are derived from the members themselves so the join keys and
    the retained body can never disagree about what was proposed.
    """
    body: dict[str, Any] = {"operation": operation}
    if entities:
        body["entities"] = [dict(member) for member in entities]
    if relationships:
        body["relationships"] = [dict(member) for member in relationships]
    if extra:
        body.update(json_safe(extra))

    subjects: list[dict[str, Any]] = []
    for member in entities:
        subjects.append(
            entity_subject(str(member["entity_type"]), str(member["entity_id"])),
        )
    for member in relationships:
        subjects.append(
            relationship_subject(
                from_type=str(member["from_type"]),
                from_id=str(member["from_id"]),
                to_type=str(member["to_type"]),
                to_id=str(member["to_id"]),
                relationship=str(member["relationship"]),
            )
        )
    return body, subjects


def batch_direct_write_proposal(
    payload: BatchDirectWriteInput,
    *,
    source: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Proposal for the batch direct-write path, including shared evidence."""
    return build_proposal(
        operation="batch_direct_write",
        entities=[entity_input_member(entity) for entity in payload.entities],
        relationships=[
            relationship_input_member(relationship) for relationship in payload.relationships
        ],
        extra={
            "source": source,
            "shared_evidence": _shared_evidence(payload.shared_evidence),
        },
    )


def _shared_evidence(
    shared_evidence: Mapping[str, SharedEvidenceInput],
) -> dict[str, Any]:
    return {key: json_safe(value) for key, value in shared_evidence.items()}


__all__ = [
    "batch_direct_write_proposal",
    "build_proposal",
    "entity_input_member",
    "entity_instance_member",
    "entity_subject",
    "json_safe",
    "relationship_input_member",
    "relationship_instance_member",
    "relationship_subject",
]
