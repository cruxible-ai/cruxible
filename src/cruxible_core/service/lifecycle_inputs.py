"""Convert typed lifecycle contract inputs into typed service state.

The typed lifecycle write channel (``EntityInput.lifecycle`` /
``RelationshipInput.lifecycle``) is the ONLY way a direct write sets lifecycle
state. These helpers translate the contract inputs into the typed core models so
every contract->service mapping (HTTP route, MCP, CLI local path) builds the same
validated lifecycle and never hand-authors a metadata dict.

Review safety: ``RelationshipLifecycleInput`` carries only ``status``/``reason``;
mapping it to ``RelationshipLifecycleState`` cannot produce review/group_override
state, and ``apply_relationship`` writes only ``assertion.lifecycle`` from it.
"""

from __future__ import annotations

from typing import Any

from cruxible_client import contracts
from cruxible_core.graph.assertion_state import (
    EntityLifecycleState as _EntityLifecycleState,
)
from cruxible_core.graph.assertion_state import (
    RelationshipLifecycleState,
    entity_lifecycle_into_metadata,
)


def entity_metadata_with_lifecycle(
    metadata: dict[str, Any] | None,
    lifecycle: contracts.EntityLifecycleInput | None,
) -> dict[str, Any]:
    """Merge a typed entity lifecycle input into an entity-metadata dict.

    Returns a copy of ``metadata`` (the free-form, non-lifecycle metadata) with the
    typed lifecycle serialized under the reserved lifecycle key. When ``lifecycle``
    is ``None`` the metadata is returned unchanged (an undecorated entity stays at
    its default ``live`` state).
    """
    base = dict(metadata or {})
    if lifecycle is None:
        return base
    typed = _EntityLifecycleState(status=lifecycle.status, reason=lifecycle.reason)
    return entity_lifecycle_into_metadata(typed, base=base)


def relationship_lifecycle_state(
    lifecycle: contracts.RelationshipLifecycleInput | None,
) -> RelationshipLifecycleState | None:
    """Map a typed relationship lifecycle input to the core lifecycle state.

    Returns ``None`` when no lifecycle write was requested, so the edge keeps its
    add/update default lifecycle. The result sets ONLY ``assertion.lifecycle``.
    """
    if lifecycle is None:
        return None
    return RelationshipLifecycleState(status=lifecycle.status, reason=lifecycle.reason)


__all__ = [
    "entity_metadata_with_lifecycle",
    "relationship_lifecycle_state",
]
