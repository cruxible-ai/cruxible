"""Seed terminal lifecycle state through the TRUSTED chokepoint capability.

Terminal lifecycle statuses (entity ``retired``/``superseded``, relationship
``retracted``/``superseded``) are refused on every free-write path — the refusal
lives at the graph chokepoint (``graph/operations.py``), so the exported service
functions and the local CLI are covered, not just the contract mappers. Those
refusals are asserted directly in ``tests/test_service/
test_relationship_lifecycle_write.py`` and ``test_entity_lifecycle_gating.py``.

Read-gating tests still need retired/retracted state to exist. They seed it here,
through ``trusted_lifecycle_transition=True`` — the internal capability the
dedicated receipted verbs of ``wi-lifecycle-verbs`` will carry. The flag is a
Python keyword argument on the graph operation; no contract payload field maps to
it, so nothing a caller can send reaches this path.
"""

from __future__ import annotations

from cruxible_core.graph.assertion_state import (
    EntityLifecycleState,
    RelationshipLifecycleState,
)
from cruxible_core.graph.operations import (
    apply_entity,
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.types import EntityMetadata
from cruxible_core.instance_protocol import InstanceProtocol

TRUSTED_LIFECYCLE_SOURCE = "lifecycle_verb"
"""Stand-in source for the future dedicated receipted lifecycle verbs."""


def seed_entity_lifecycle(
    instance: InstanceProtocol,
    entity_type: str,
    entity_id: str,
    status: str,
    *,
    reason: str | None = None,
) -> None:
    """Set an entity's typed lifecycle status via the trusted capability."""
    config = instance.load_config()
    graph = instance.load_graph()
    validated = validate_entity(
        config,
        graph,
        entity_type,
        entity_id,
        {},
        metadata=EntityMetadata(
            lifecycle=EntityLifecycleState(status=status, reason=reason)  # type: ignore[arg-type]
        ),
    )
    apply_entity(
        graph,
        validated,
        config=config,
        source=TRUSTED_LIFECYCLE_SOURCE,
        trusted_lifecycle_transition=True,
    )
    instance.save_graph(graph)


def seed_relationship_lifecycle(
    instance: InstanceProtocol,
    *,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    status: str,
    reason: str | None = None,
    properties: dict | None = None,
) -> None:
    """Set an edge's typed lifecycle status via the trusted capability."""
    config = instance.load_config()
    graph = instance.load_graph()
    validated = validate_relationship(
        config,
        graph,
        from_type,
        from_id,
        relationship_type,
        to_type,
        to_id,
        properties or {},
    )
    apply_relationship(
        graph,
        validated,
        TRUSTED_LIFECYCLE_SOURCE,
        "terminal_lifecycle_seed",
        config=config,
        lifecycle=RelationshipLifecycleState(status=status, reason=reason),  # type: ignore[arg-type]
        trusted_lifecycle_transition=True,
    )
    instance.save_graph(graph)


__all__ = [
    "TRUSTED_LIFECYCLE_SOURCE",
    "seed_entity_lifecycle",
    "seed_relationship_lifecycle",
]
