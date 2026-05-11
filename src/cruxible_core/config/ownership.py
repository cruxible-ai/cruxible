"""Shared ownership guards for upstream-backed overlays."""

from __future__ import annotations

from collections.abc import Iterable

from cruxible_core.errors import OwnershipError
from cruxible_core.snapshot.types import UpstreamMetadata


def check_upstream_type_ownership(
    upstream: UpstreamMetadata | None,
    *,
    entity_types: Iterable[str] = (),
    relationship_types: Iterable[str] = (),
) -> None:
    """Reject writes that target types owned by an upstream release."""
    if upstream is None:
        return

    blocked_entities = sorted(set(entity_types) & set(upstream.owned_entity_types))
    if blocked_entities:
        names = ", ".join(blocked_entities)
        raise OwnershipError(
            f"Overlay instances cannot mutate upstream-owned entity types: {names}",
            blocked_types=blocked_entities,
        )

    blocked_relationships = sorted(
        set(relationship_types) & set(upstream.owned_relationship_types)
    )
    if blocked_relationships:
        names = ", ".join(blocked_relationships)
        raise OwnershipError(
            f"Overlay instances cannot mutate upstream-owned relationship types: {names}",
            blocked_types=blocked_relationships,
        )
