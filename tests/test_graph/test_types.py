"""Tests for graph runtime model helpers."""

from __future__ import annotations

from cruxible_core.graph.assertion_state import RelationshipAssertion
from cruxible_core.graph.types import RelationshipInstance, RelationshipMetadata


def test_relationship_instance_identity_projections_ignore_non_identity_fields() -> None:
    relationship = RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
        edge_key=7,
        properties={"verified": True},
        metadata=RelationshipMetadata(assertion=RelationshipAssertion(group_override=True)),
    )
    changed_non_identity = relationship.model_copy(
        update={
            "edge_key": 8,
            "properties": {"verified": False},
            "metadata": RelationshipMetadata(),
        }
    )

    assert relationship.identity_tuple() == (
        "Part",
        "BP-1",
        "Vehicle",
        "V-1",
        "fits",
    )
    assert changed_non_identity.identity_tuple() == relationship.identity_tuple()
    assert relationship.identity_payload() == {
        "from_type": "Part",
        "from_id": "BP-1",
        "to_type": "Vehicle",
        "to_id": "V-1",
        "relationship_type": "fits",
    }
    assert relationship.endpoint_label() == "Part:BP-1->Vehicle:V-1"
    assert relationship.relationship_label() == "Part:BP-1 -[fits]-> Vehicle:V-1"
