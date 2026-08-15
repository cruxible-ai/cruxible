"""Group-approval content drift: detection and the marker's current-state rule.

A group approval accepts an edge's PROPERTIES, not merely its existence. When a
later legitimate write changes those properties the edge stays live and stays
approved — the world does change — but every ordinary read of it must carry the
divergence, or a reviewer still believes the group signed off on what the edge
now says.

RULING (Robert, 2026-07-25): the marker reflects CURRENT divergence, not
accumulated history. Every write recomputes it against the approved content and
DROPS it when the content fully matches the approval again. History lives in
receipts — each write that moved the edge is on the record there — so live state
does not have to carry a growing ledger of what the edge used to say.

Lives under ``graph/`` rather than ``service/`` because BOTH write paths need it:
the direct-write service entries and the canonical ``workflow_apply`` step. The
workflow path could not import from ``service`` (``service`` imports
``workflow``), and a marker only one of the two paths stamps is a marker a
reviewer cannot trust.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cruxible_core.graph.assertion_state import GroupApprovalDrift
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.property_diffs import property_delta
from cruxible_core.graph.provenance import provenance_group_id
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.temporal import utc_now

_MISSING = object()
"""Sentinel distinguishing "approved absent" from "approved as None"."""


@dataclass(frozen=True)
class GroupContentDrift:
    """One group-approved edge whose content an in-flight write changes.

    Computed BEFORE the write lands (the pre-write values are the baseline) and
    consumed after it, so the stamping pass does not have to re-diff an edge
    that has already been overwritten.
    """

    group_id: str
    changed_properties: list[str]
    approved_values: dict[str, Any]


def content_change(
    incoming: RelationshipInstance,
    existing: RelationshipInstance,
) -> list[str]:
    """Return the property names this write changes on ``existing``.

    ``incoming`` must be the VALIDATED relationship, not the raw payload: the
    update branch of ``apply_relationship`` replaces properties wholesale with
    the validated (merged onto the existing edge, coerced, defaulted) values, so
    that is what actually lands. Diffing the raw payload instead would report a
    property-less touch as "removed everything" and a ``"5"``-for-``5`` restate
    as a change — an over-eager marker, which is worse than no marker.
    """
    delta = property_delta(dict(incoming.properties), dict(existing.properties))
    return sorted({*delta.changed, *delta.added, *delta.removed})


def group_content_drift(
    incoming: RelationshipInstance,
    existing: RelationshipInstance,
) -> GroupContentDrift | None:
    """Return the drift this write introduces on a group-approved edge, if any.

    ``None`` when the edge is not group-backed or when the write changes nothing:
    a write that changes nothing is not drift and must leave the edge alone
    entirely, including any marker already on it.
    """
    if existing.metadata.provenance is None:
        return None
    group_id = provenance_group_id(existing.metadata.provenance)
    if group_id is None:
        return None
    changed = content_change(incoming, existing)
    if not changed:
        return None
    return GroupContentDrift(
        group_id=group_id,
        changed_properties=changed,
        approved_values={
            name: existing.properties[name] for name in changed if name in existing.properties
        },
    )


def stamp_group_approval_drift(
    graph: EntityGraph,
    relationship: RelationshipInstance,
    drift: GroupContentDrift | None,
    *,
    receipt_id: str | None,
    actor_context: Any | None,
) -> None:
    """Recompute the drift marker on an edge this write just changed.

    Called AFTER the write lands. The marker rides on the assertion axis (not
    review, not lifecycle): the edge stays live and stays approved.

    The recompute is the ruling. The previous implementation accumulated —
    property names and approved values only ever grew — so an edge that had been
    edited and then fully restored still read as drifted forever, and a partial
    revert still listed the properties that now matched. The approved baseline
    is carried forward across writes (the earlier marker holds the group's
    values; this write's "before" values are only the previous drift's), but
    what is REPORTED is only what still diverges from that baseline right now.
    """
    if drift is None:
        return
    persisted = graph.get_relationship(
        relationship.from_type,
        relationship.from_id,
        relationship.to_type,
        relationship.to_id,
        relationship.relationship_type,
    )
    if persisted is None:
        return

    previous = persisted.metadata.assertion.group_approval_drift
    carried = previous is not None and previous.group_id == drift.group_id

    approved_values = dict(drift.approved_values)
    candidates = set(drift.changed_properties)
    if carried:
        assert previous is not None
        # The earlier approved_values are the GROUP's; this write's "before"
        # values are only the previous drift's, so the earlier ones win.
        approved_values = {**approved_values, **previous.approved_values}
        candidates |= set(previous.changed_properties)

    divergent = sorted(
        name
        for name in candidates
        if persisted.properties.get(name, _MISSING) != approved_values.get(name, _MISSING)
    )

    if not divergent:
        if previous is None:
            return
        marker: GroupApprovalDrift | None = None
    else:
        detected_at = utc_now()
        first_detected_at = detected_at
        if carried:
            assert previous is not None
            first_detected_at = previous.first_detected_at or previous.detected_at or detected_at
        marker = GroupApprovalDrift(
            group_id=drift.group_id,
            changed_properties=divergent,
            approved_values={
                name: value for name, value in approved_values.items() if name in divergent
            },
            first_detected_at=first_detected_at,
            detected_at=detected_at,
            receipt_id=receipt_id,
            actor_context=actor_context,
        )

    metadata = persisted.metadata.model_copy(
        update={
            "assertion": persisted.metadata.assertion.model_copy(
                update={"group_approval_drift": marker}
            )
        }
    )
    graph.replace_relationship_state(
        relationship.from_type,
        relationship.from_id,
        relationship.to_type,
        relationship.to_id,
        relationship.relationship_type,
        properties=persisted.properties,
        metadata=metadata,
        edge_key=persisted.edge_key,
    )


__all__ = [
    "GroupContentDrift",
    "content_change",
    "group_content_drift",
    "stamp_group_approval_drift",
]
