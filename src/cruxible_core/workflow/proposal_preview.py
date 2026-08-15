"""Pure proposal-preview comparison retained for the Procedure donor."""

from __future__ import annotations

import hashlib
from typing import Any

from cruxible_core.graph.diff import (
    GraphDiffSelector,
    GraphDiffSide,
    OwnershipBasis,
    diff_edges,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.group.types import CandidateMember
from cruxible_core.primitives import canonical_json


def compare_pending_relationships(
    graph: EntityGraph,
    relationship_type: str,
    members: list[CandidateMember],
) -> dict[str, Any]:
    """Preview candidate relationships through the shared edge comparator."""
    proposed = EntityGraph.from_dict(graph.to_dict())
    for index, member in enumerate(members):
        relationship = member.as_relationship()
        identity = {
            **relationship.identity_payload(),
            "properties": relationship.properties,
            "index": index,
        }
        claim_digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
        proposed.add_relationship(
            RelationshipInstance(
                **relationship.model_dump(mode="python", exclude={"claim_id", "edge_key"}),
                claim_id=f"preview-{claim_digest[:24]}",
            )
        )
    basis = OwnershipBasis.unknown_basis("pending relationship preview has no ownership pin")
    section = diff_edges(
        GraphDiffSide(graph=graph, ownership=basis),
        GraphDiffSide(graph=proposed, ownership=basis),
        GraphDiffSelector(relationship_types=frozenset({relationship_type})),
    )
    return {"basis": "pending_group", "sections": {"edges": section.payload()}}


__all__ = ["compare_pending_relationships"]
