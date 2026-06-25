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
)
from cruxible_core.graph.types import EntityMetadata


def entity_metadata_with_lifecycle(
    metadata: dict[str, Any] | None,
    lifecycle: contracts.EntityLifecycleInput | None,
) -> dict[str, Any]:
    """Build the typed entity-metadata envelope for a direct write.

    Author-supplied ``metadata`` is treated as wholly free-form: it is carried in
    the envelope's ``extra`` slot, NOT interpreted for owned slices. So a
    hand-authored ``metadata={"lifecycle": ...}`` lands at ``extra["lifecycle"]`` --
    inert free-form data -- and can never become the typed lifecycle state. The
    typed ``lifecycle`` field is set ONLY from the ``lifecycle`` contract input
    (``EntityInput.lifecycle``), which is the single channel for entity lifecycle.
    The result is re-encoded to the flat storable dict; ``None`` lifecycle leaves an
    undecorated entity at its default ``live`` state.
    """
    extra = dict(metadata or {})
    typed_lifecycle = (
        _EntityLifecycleState(status=lifecycle.status, reason=lifecycle.reason)
        if lifecycle is not None
        else None
    )
    return EntityMetadata(lifecycle=typed_lifecycle, extra=extra).to_metadata_dict()


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
