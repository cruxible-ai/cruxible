"""Resolving a caller-supplied claim target against live graph state.

One tuple can carry several edges, so a caller that means a SPECIFIC claim has
to disambiguate. Two disambiguators exist and they are not equals:

* ``claim_id`` -- the minted, immutable identity. It WINS whenever it is
  supplied, because it names one claim for the life of that claim.
* ``edge_key`` -- the per-load networkx key. It stays the legacy disambiguator
  (records written before identity carry only this, and it remains the wire
  disambiguator and the ordering token), but it is not stable across loads.

When BOTH are supplied and they disagree, this REFUSES rather than silently
preferring either: a caller holding two contradictory references to a claim does
not know which claim it means, and picking one for it would attach an
observation or a correction to a claim the caller never chose.
"""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import RelationshipInstance


class ClaimTargetConflictError(ValueError):
    """Raised when ``claim_id`` and ``edge_key`` name different claims."""


@dataclass(frozen=True)
class ClaimTarget:
    """One resolved (or unresolvable) claim reference."""

    relationship: RelationshipInstance | None
    #: Which disambiguator actually selected the claim, when one did.
    resolved_by: str | None = None


def resolve_claim_target(
    graph: EntityGraph,
    *,
    relationship_type: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    claim_id: str | None = None,
    edge_key: int | None = None,
) -> ClaimTarget:
    """Resolve one claim reference, with ``claim_id`` taking precedence.

    Raises :class:`ClaimTargetConflictError` when the supplied disambiguators
    contradict each other (different claims), or when a supplied ``claim_id``
    resolves to a claim whose identity tuple is not the one asked for.
    """
    tuple_match = graph.get_relationship(
        from_type,
        from_id,
        to_type,
        to_id,
        relationship_type,
        edge_key=edge_key,
    )
    if claim_id is None:
        return ClaimTarget(tuple_match, "edge_key" if edge_key is not None else None)

    by_id = graph.find_relationship_by_claim_id(claim_id)
    if by_id is None:
        # An unknown id is an unresolved target, not a conflict: the claim may
        # have been removed, or the reference may come from another instance.
        # Callers treat "no relationship" as they always have.
        return ClaimTarget(None, "claim_id")
    if by_id.identity_tuple() != (from_type, from_id, to_type, to_id, relationship_type):
        raise ClaimTargetConflictError(
            f"claim_id '{claim_id}' resolves to {by_id.relationship_label()}, which is not "
            f"the requested claim {from_type}:{from_id} -[{relationship_type}]-> "
            f"{to_type}:{to_id}"
        )
    if edge_key is not None and by_id.edge_key != edge_key:
        raise ClaimTargetConflictError(
            f"target disambiguators disagree: claim_id '{claim_id}' is edge_key "
            f"{by_id.edge_key}, but edge_key {edge_key} was also supplied. Supply one, "
            "or supply matching values -- neither is silently preferred."
        )
    return ClaimTarget(by_id, "claim_id")


__all__ = ["ClaimTarget", "ClaimTargetConflictError", "resolve_claim_target"]
