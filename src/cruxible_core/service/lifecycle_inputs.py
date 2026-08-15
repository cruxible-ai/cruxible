"""Convert typed lifecycle contract inputs into typed service state.

The typed lifecycle write channel (``EntityInput.lifecycle`` /
``RelationshipInput.lifecycle``) is the ONLY way a direct write sets lifecycle
state. These helpers translate the contract inputs into the typed core models so
every contract->service mapping (HTTP route, MCP, CLI local path) builds the same
validated lifecycle and never hand-authors a metadata dict.

Review safety: ``RelationshipLifecycleInput`` carries only ``status``/``reason``;
mapping it to ``RelationshipLifecycleState`` cannot produce review/group_override
state, and ``apply_relationship`` writes only ``assertion.lifecycle`` from it.

Terminal statuses are NOT writable through this channel — see
:data:`TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES` and
:data:`TERMINAL_ENTITY_LIFECYCLE_STATUSES`.

These mapper refusals are the EARLY, friendly ones: they fire on the contract
payload, before any graph work, so an HTTP/MCP caller gets the teaching message
attached to the field it supplied. They are NOT the guarantee. The guarantee
lives at the graph write chokepoint (``graph/operations.py``:
``apply_entity`` / ``apply_relationship``), which every free-write path shares
including the exported service functions and the local CLI. Both refusals raise
the same :class:`TerminalLifecycleWriteRefusedError`.
"""

from __future__ import annotations

from typing import Any, Protocol

from cruxible_core.errors import TerminalLifecycleWriteRefusedError
from cruxible_core.graph.assertion_state import (
    EntityLifecycleStatus,
    RelationshipLifecycleStatus,
    TERMINAL_ENTITY_LIFECYCLE_STATUSES,
    TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES,
    WRITABLE_ENTITY_LIFECYCLE_STATUSES,
    WRITABLE_RELATIONSHIP_LIFECYCLE_STATUSES,
    RelationshipLifecycleState,
)
from cruxible_core.graph.assertion_state import (
    EntityLifecycleState as _EntityLifecycleState,
)
from cruxible_core.graph.types import EntityMetadata


class EntityLifecycleInput(Protocol):
    status: EntityLifecycleStatus
    reason: str | None


class RelationshipLifecycleInput(Protocol):
    status: RelationshipLifecycleStatus
    reason: str | None


def _refuse_terminal_lifecycle(status: str, *, kind: str, writable: str) -> None:
    """Refuse a terminal lifecycle status arriving on a contract payload.

    The early, friendly half of the refusal — see this module's docstring. The
    binding one is at the graph chokepoint; this one only saves an HTTP/MCP
    caller a round trip through validation to reach the same answer.
    """
    raise TerminalLifecycleWriteRefusedError(kind, status, writable)


def entity_metadata_with_lifecycle(
    metadata: dict[str, Any] | None,
    lifecycle: EntityLifecycleInput | None,
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
    if lifecycle is not None and lifecycle.status in TERMINAL_ENTITY_LIFECYCLE_STATUSES:
        _refuse_terminal_lifecycle(
            lifecycle.status,
            kind="entity",
            writable=WRITABLE_ENTITY_LIFECYCLE_STATUSES,
        )
    typed_lifecycle = (
        _EntityLifecycleState(status=lifecycle.status, reason=lifecycle.reason)
        if lifecycle is not None
        else None
    )
    return EntityMetadata(lifecycle=typed_lifecycle, extra=extra).to_metadata_dict()


def relationship_lifecycle_state(
    lifecycle: RelationshipLifecycleInput | None,
) -> RelationshipLifecycleState | None:
    """Map a typed relationship lifecycle input to the core lifecycle state.

    Returns ``None`` when no lifecycle write was requested, so the edge keeps its
    add/update default lifecycle. The result sets ONLY ``assertion.lifecycle``.
    """
    if lifecycle is None:
        return None
    if lifecycle.status in TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES:
        _refuse_terminal_lifecycle(
            lifecycle.status,
            kind="relationship",
            writable=WRITABLE_RELATIONSHIP_LIFECYCLE_STATUSES,
        )
    return RelationshipLifecycleState(status=lifecycle.status, reason=lifecycle.reason)


__all__ = [
    "TERMINAL_ENTITY_LIFECYCLE_STATUSES",
    "TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES",
    "entity_metadata_with_lifecycle",
    "relationship_lifecycle_state",
]
