"""Kind-correct exact lifecycle-status filters for query readers."""

from __future__ import annotations

from typing import Any, Literal

from cruxible_core.graph.assertion_state import relationship_assertion_from_metadata
from cruxible_core.query.enums import LifecycleStatus

ENTITY_LIFECYCLE_STATUSES = frozenset({"live", "retired", "superseded"})
RELATIONSHIP_LIFECYCLE_STATUSES = frozenset({"active", "inactive", "superseded", "retracted"})


def validate_lifecycle_status(
    status: LifecycleStatus,
    *,
    kind: Literal["entity", "relationship"],
) -> None:
    """Refuse vocabulary from the other artifact kind."""
    allowed = ENTITY_LIFECYCLE_STATUSES if kind == "entity" else RELATIONSHIP_LIFECYCLE_STATUSES
    if status not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Unsupported {kind} lifecycle_status '{status}'. Use: {choices}")


def entity_matches_lifecycle_status(metadata: Any, status: LifecycleStatus) -> bool:
    """Return whether typed entity metadata has exactly *status*."""
    validate_lifecycle_status(status, kind="entity")
    lifecycle = getattr(metadata, "lifecycle", None)
    return ("live" if lifecycle is None else lifecycle.status) == status


def relationship_matches_lifecycle_status(metadata: Any, status: LifecycleStatus) -> bool:
    """Return whether relationship assertion metadata has exactly *status*."""
    validate_lifecycle_status(status, kind="relationship")
    return relationship_assertion_from_metadata(metadata).lifecycle.status == status


__all__ = [
    "ENTITY_LIFECYCLE_STATUSES",
    "RELATIONSHIP_LIFECYCLE_STATUSES",
    "entity_matches_lifecycle_status",
    "relationship_matches_lifecycle_status",
    "validate_lifecycle_status",
]
