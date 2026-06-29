"""Tests for edge_key in RelationshipInstance and get_relationship()."""

from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance


class TestRelationshipEdgeKey:
    def test_get_relationship_returns_edge_key(self):
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                properties={"source": "catalog"},
            )
        )
        rel = graph.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
        assert rel is not None
        assert rel.edge_key is not None
        assert isinstance(rel.edge_key, int)

    def test_get_relationship_with_edge_key_filter(self):
        """Selecting specific edge among multiple same-type edges."""
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V1", properties={}))
        # Add two fits edges between same endpoints
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                properties={"source": "catalog"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                properties={"source": "user_report"},
            )
        )
        # Get both edges to find their keys
        rel1 = graph.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
        assert rel1 is not None
        key1 = rel1.edge_key

        # Get specific edge by key
        rel_specific = graph.get_relationship("Part", "P1", "Vehicle", "V1", "fits", edge_key=key1)
        assert rel_specific is not None
        assert rel_specific.edge_key == key1
        assert rel_specific.properties["source"] == rel1.properties["source"]
