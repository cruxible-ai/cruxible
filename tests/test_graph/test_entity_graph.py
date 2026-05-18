"""Tests for EntityGraph operations."""

import pytest

from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
    make_node_id,
    split_node_id,
)


@pytest.fixture
def graph() -> EntityGraph:
    return EntityGraph()


@pytest.fixture
def populated_graph(graph: EntityGraph) -> EntityGraph:
    """Graph with 2 vehicles, 2 parts, and fitment edges."""
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-CIVIC",
            properties={"make": "Honda", "model": "Civic", "year": 2024},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-ACCORD",
            properties={"make": "Honda", "model": "Accord", "year": 2024},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1234",
            properties={"name": "Ceramic Brake Pad", "category": "brakes"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-5678",
            properties={"name": "Performance Rotor", "category": "brakes"},
        )
    )
    # BP-1234 fits both vehicles
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1234",
            to_type="Vehicle",
            to_id="V-CIVIC",
            properties={"verified": True, "confidence": 0.95},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1234",
            to_type="Vehicle",
            to_id="V-ACCORD",
            properties={"verified": True, "confidence": 0.9},
        )
    )
    # BP-5678 fits only Civic
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-5678",
            to_type="Vehicle",
            to_id="V-CIVIC",
            properties={"verified": False, "confidence": 0.7},
        )
    )
    # BP-5678 replaces BP-1234
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="replaces",
            from_type="Part",
            from_id="BP-5678",
            to_type="Part",
            to_id="BP-1234",
            properties={"direction": "upgrade"},
        )
    )
    return graph


class TestEntityOperations:
    def test_add_and_get(self, graph: EntityGraph):
        entity = EntityInstance(entity_type="Part", entity_id="P-1", properties={"name": "Widget"})
        graph.add_entity(entity)

        result = graph.get_entity("Part", "P-1")
        assert result is not None
        assert result.entity_id == "P-1"
        assert result.properties["name"] == "Widget"

    def test_get_missing(self, graph: EntityGraph):
        assert graph.get_entity("Part", "MISSING") is None

    def test_has_entity(self, graph: EntityGraph):
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1"))
        assert graph.has_entity("Part", "P-1") is True
        assert graph.has_entity("Part", "P-2") is False

    def test_list_entities(self, populated_graph: EntityGraph):
        vehicles = populated_graph.list_entities("Vehicle")
        assert len(vehicles) == 2
        ids = {v.entity_id for v in vehicles}
        assert ids == {"V-CIVIC", "V-ACCORD"}

    def test_list_entities_with_property_filter(self, populated_graph: EntityGraph):
        result = populated_graph.list_entities("Vehicle", property_filter={"make": "Honda"})
        assert len(result) == 2

    def test_list_entities_filter_single_match(self, populated_graph: EntityGraph):
        result = populated_graph.list_entities("Vehicle", property_filter={"model": "Civic"})
        assert len(result) == 1
        assert result[0].entity_id == "V-CIVIC"

    def test_list_entities_filter_no_match(self, populated_graph: EntityGraph):
        result = populated_graph.list_entities("Vehicle", property_filter={"make": "Toyota"})
        assert len(result) == 0

    def test_list_entities_filter_multiple_properties(self, populated_graph: EntityGraph):
        result = populated_graph.list_entities(
            "Vehicle", property_filter={"make": "Honda", "model": "Civic"}
        )
        assert len(result) == 1

    def test_list_entities_no_filter_same_as_default(self, populated_graph: EntityGraph):
        all_entities = populated_graph.list_entities("Vehicle")
        no_filter = populated_graph.list_entities("Vehicle", property_filter=None)
        assert len(all_entities) == len(no_filter)

    def test_list_entities_empty_type(self, graph: EntityGraph):
        assert graph.list_entities("NonExistent") == []

    def test_remove_entity(self, populated_graph: EntityGraph):
        populated_graph.remove_entity("Vehicle", "V-CIVIC")
        assert populated_graph.has_entity("Vehicle", "V-CIVIC") is False
        assert populated_graph.entity_count("Vehicle") == 1

    def test_update_entity(self, graph: EntityGraph):
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={"v": 1}))
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={"v": 2}))
        result = graph.get_entity("Part", "P-1")
        assert result is not None
        assert result.properties["v"] == 2

    def test_entity_count(self, populated_graph: EntityGraph):
        assert populated_graph.entity_count() == 4
        assert populated_graph.entity_count("Vehicle") == 2
        assert populated_graph.entity_count("Part") == 2


class TestRelationshipOperations:
    def test_has_relationship(self, populated_graph: EntityGraph):
        assert populated_graph.has_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        assert not populated_graph.has_relationship(
            "Part", "BP-5678", "Vehicle", "V-ACCORD", "fits"
        )

    def test_get_relationship(self, populated_graph: EntityGraph):
        rel = populated_graph.get_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
        assert rel is not None
        assert rel.properties["verified"] is True
        assert rel.properties["confidence"] == 0.95

    def test_get_relationship_missing(self, populated_graph: EntityGraph):
        assert (
            populated_graph.get_relationship("Part", "BP-5678", "Vehicle", "V-ACCORD", "fits")
            is None
        )

    def test_remove_relationship(self, populated_graph: EntityGraph):
        removed = populated_graph.remove_relationship(
            "Part", "BP-1234", "Vehicle", "V-CIVIC", "fits"
        )
        assert removed is True
        assert not populated_graph.has_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")

    def test_remove_relationship_missing(self, populated_graph: EntityGraph):
        assert not populated_graph.remove_relationship(
            "Part", "MISSING", "Vehicle", "V-CIVIC", "fits"
        )

    def test_stub_entity_creation(self, graph: EntityGraph):
        """Adding a relationship creates stub entities if they don't exist."""
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="NEW-PART",
                to_type="Vehicle",
                to_id="NEW-VEHICLE",
            )
        )
        assert graph.has_entity("Part", "NEW-PART")
        assert graph.has_entity("Vehicle", "NEW-VEHICLE")

    def test_edge_count(self, populated_graph: EntityGraph):
        assert populated_graph.edge_count() == 4
        assert populated_graph.edge_count("fits") == 3
        assert populated_graph.edge_count("replaces") == 1

    def test_relationship_count_between(self, graph: EntityGraph):
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"source": "A"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"source": "B"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={},
            )
        )

        assert graph.relationship_count_between("Part", "P-1", "Vehicle", "V-1", "fits") == 2
        assert graph.relationship_count_between("Part", "P-1", "Vehicle", "V-1", "replaces") == 1
        assert graph.relationship_count_between("Part", "P-1", "Vehicle", "V-1", "unknown") == 0
        assert graph.relationship_count_between("Part", "P-1", "Vehicle", "V-2", "fits") == 0

    def test_get_neighbors_with_relationship_refs(self, populated_graph: EntityGraph):
        refs = populated_graph.get_neighbors_with_relationship_refs(
            "Part",
            "BP-1234",
            relationship_type="fits",
            direction="outgoing",
        )
        assert len(refs) == 2
        assert all(isinstance(edge_key, int) for _, _, _, edge_key in refs)

    def test_update_specific_edge_by_key(self, graph: EntityGraph):
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"source": "A"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"source": "B"},
            )
        )
        refs = graph.get_neighbors_with_relationship_refs(
            "Part",
            "P-1",
            relationship_type="fits",
            direction="outgoing",
        )
        key_for_b = next(
            edge_key for _, props, _metadata, edge_key in refs if props.get("source") == "B"
        )
        updated = graph.update_relationship_state(
            "Part",
            "P-1",
            "Vehicle",
            "V-1",
            "fits",
            property_updates={"reviewed": True},
            edge_key=key_for_b,
        )
        assert updated is True
        refs_after = graph.get_neighbors_with_relationship_refs(
            "Part",
            "P-1",
            relationship_type="fits",
            direction="outgoing",
        )
        reviewed = {
            props.get("source"): props.get("reviewed")
            for _, props, _metadata, _edge_key in refs_after
        }
        assert reviewed["A"] is None
        assert reviewed["B"] is True


class TestRelationshipStateWrites:
    def test_update_relationship_state_merges_properties_and_preserves_metadata(
        self, graph: EntityGraph
    ):
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"confidence": 0.9},
                metadata=RelationshipMetadata(
                    provenance=RelationshipProvenance(source="ingest")
                ),
            )
        )
        updated = graph.update_relationship_state(
            "Part",
            "P-1",
            "Vehicle",
            "V-1",
            "fits",
            property_updates={"reviewed": True},
        )
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert updated is True
        assert rel.properties == {"confidence": 0.9, "reviewed": True}
        assert rel.metadata.provenance is not None
        assert rel.metadata.provenance.source == "ingest"

    def test_replace_relationship_state_replaces_properties_and_metadata(
        self, graph: EntityGraph
    ):
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"confidence": 0.9},
                metadata=RelationshipMetadata(
                    assertion=RelationshipAssertion(
                        review=RelationshipReviewState(
                            status="approved",
                            source="human",
                        )
                    )
                ),
            )
        )

        updated = graph.replace_relationship_state(
            "Part",
            "P-1",
            "Vehicle",
            "V-1",
            "fits",
            properties={"confidence": 0.95},
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(
                        status="rejected",
                        source="agent",
                    )
                )
            ),
        )

        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert updated is True
        assert rel.properties["confidence"] == 0.95
        assert rel.metadata.assertion.review.status == "rejected"
        assert rel.metadata.assertion.review.source == "agent"

    def test_update_relationship_state_replaces_metadata(self, graph: EntityGraph):
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"confidence": 0.9},
                metadata=RelationshipMetadata(
                    provenance=RelationshipProvenance(source="ingest")
                ),
            )
        )
        updated = graph.update_relationship_state(
            "Part",
            "P-1",
            "Vehicle",
            "V-1",
            "fits",
            metadata=RelationshipMetadata(provenance=RelationshipProvenance(source="mcp_add")),
        )
        rel = graph.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert updated is True
        assert rel.properties == {"confidence": 0.9}
        assert rel.metadata.provenance is not None
        assert rel.metadata.provenance.source == "mcp_add"


class TestGetDescendants:
    """Thorough tests for BFS get_descendants."""

    def test_single_hop(self, populated_graph: EntityGraph):
        """Direct children at depth 1."""
        desc = populated_graph.get_descendants("Part", "BP-1234", "fits")
        assert len(desc) == 2
        ids = {d[0].entity_id for d in desc}
        assert ids == {"V-CIVIC", "V-ACCORD"}
        assert all(d[1] == 1 for d in desc)

    def test_multi_hop_depths(self, populated_graph: EntityGraph):
        """BP-5678 -> replaces -> BP-1234 -> fits -> vehicles. Check depths."""
        desc = populated_graph.get_descendants("Part", "BP-5678")
        by_id = {d[0].entity_id: d[1] for d in desc}
        # Direct neighbors: V-CIVIC (fits, depth 1) and BP-1234 (replaces, depth 1)
        assert by_id["BP-1234"] == 1
        assert by_id["V-CIVIC"] == 1
        # V-ACCORD reachable via BP-1234 at depth 2
        assert by_id["V-ACCORD"] == 2

    def test_relationship_type_filter(self, populated_graph: EntityGraph):
        """Only traverse 'replaces' edges, not 'fits'."""
        desc = populated_graph.get_descendants("Part", "BP-5678", "replaces")
        assert len(desc) == 1
        assert desc[0][0].entity_id == "BP-1234"

    def test_relationship_type_filter_no_match(self, populated_graph: EntityGraph):
        """Filter by a relationship type that doesn't exist from this node."""
        desc = populated_graph.get_descendants("Part", "BP-1234", "replaces")
        # BP-1234 has no outgoing 'replaces' edges
        assert desc == []

    def test_max_depth_zero(self, populated_graph: EntityGraph):
        """max_depth=0 means don't traverse at all."""
        desc = populated_graph.get_descendants("Part", "BP-1234", max_depth=0)
        assert desc == []

    def test_max_depth_one(self, populated_graph: EntityGraph):
        """max_depth=1 returns only direct neighbors."""
        desc = populated_graph.get_descendants("Part", "BP-5678", max_depth=1)
        assert all(d[1] == 1 for d in desc)

    def test_max_depth_limits_multi_hop(self, populated_graph: EntityGraph):
        """max_depth=1 from BP-5678 should NOT reach V-ACCORD (depth 2)."""
        desc = populated_graph.get_descendants("Part", "BP-5678", max_depth=1)
        ids = {d[0].entity_id for d in desc}
        assert "V-ACCORD" not in ids

    def test_max_depth_two_reaches_second_hop(self, populated_graph: EntityGraph):
        """max_depth=2 from BP-5678 SHOULD reach V-ACCORD."""
        desc = populated_graph.get_descendants("Part", "BP-5678", max_depth=2)
        ids = {d[0].entity_id for d in desc}
        assert "V-ACCORD" in ids

    def test_edge_filter_accepts(self, populated_graph: EntityGraph):
        """Edge filter that accepts all verified edges."""
        desc = populated_graph.get_descendants(
            "Part",
            "BP-1234",
            "fits",
            edge_filter=lambda props: props.get("verified") is True,
        )
        assert len(desc) == 2

    def test_edge_filter_rejects_some(self, populated_graph: EntityGraph):
        """Edge filter that requires confidence > 0.92."""
        desc = populated_graph.get_descendants(
            "Part",
            "BP-1234",
            "fits",
            edge_filter=lambda props: props.get("confidence", 0) > 0.92,
        )
        # Only V-CIVIC edge has confidence 0.95; V-ACCORD is 0.9
        assert len(desc) == 1
        assert desc[0][0].entity_id == "V-CIVIC"

    def test_edge_filter_rejects_all(self, populated_graph: EntityGraph):
        """Edge filter that rejects everything."""
        desc = populated_graph.get_descendants(
            "Part",
            "BP-1234",
            "fits",
            edge_filter=lambda props: False,
        )
        assert desc == []

    def test_bidirectional(self, populated_graph: EntityGraph):
        """Bidirectional from a vehicle finds parts via incoming fits edges."""
        desc = populated_graph.get_descendants(
            "Vehicle",
            "V-CIVIC",
            "fits",
            bidirectional=True,
        )
        # V-CIVIC has no outgoing fits, but incoming from BP-1234 and BP-5678
        ids = {d[0].entity_id for d in desc}
        assert "BP-1234" in ids
        assert "BP-5678" in ids

    def test_bidirectional_does_not_revisit(self, populated_graph: EntityGraph):
        """Bidirectional BFS visits each node only once."""
        desc = populated_graph.get_descendants(
            "Part",
            "BP-1234",
            bidirectional=True,
        )
        ids = [d[0].entity_id for d in desc]
        assert len(ids) == len(set(ids))

    def test_cycle_does_not_loop(self, graph: EntityGraph):
        """Cycle: A -> B -> C -> A. BFS should terminate."""
        for nid in ["1", "2", "3"]:
            graph.add_entity(EntityInstance(entity_type="N", entity_id=nid))
        for src, dst in [("1", "2"), ("2", "3"), ("3", "1")]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="next",
                    from_type="N",
                    from_id=src,
                    to_type="N",
                    to_id=dst,
                )
            )
        desc = graph.get_descendants("N", "1", "next")
        ids = {d[0].entity_id for d in desc}
        assert ids == {"2", "3"}

    def test_self_loop_ignored(self, graph: EntityGraph):
        """An edge from a node to itself should not cause infinite loop."""
        graph.add_entity(EntityInstance(entity_type="N", entity_id="1"))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="self_ref",
                from_type="N",
                from_id="1",
                to_type="N",
                to_id="1",
            )
        )
        desc = graph.get_descendants("N", "1", "self_ref")
        assert desc == []

    def test_missing_entity(self, graph: EntityGraph):
        assert graph.get_descendants("Part", "MISSING", "fits") == []

    def test_no_outgoing_edges(self, populated_graph: EntityGraph):
        """Leaf node with no outgoing edges returns empty."""
        desc = populated_graph.get_descendants("Vehicle", "V-ACCORD", "fits")
        assert desc == []

    def test_diamond_graph(self, graph: EntityGraph):
        """Diamond: A -> B, A -> C, B -> D, C -> D. D visited once at depth 2."""
        for nid in ["A", "B", "C", "D"]:
            graph.add_entity(EntityInstance(entity_type="N", entity_id=nid))
        for src, dst in [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="link",
                    from_type="N",
                    from_id=src,
                    to_type="N",
                    to_id=dst,
                )
            )
        desc = graph.get_descendants("N", "A", "link")
        ids = [d[0].entity_id for d in desc]
        # No duplicates
        assert len(ids) == len(set(ids))
        assert set(ids) == {"B", "C", "D"}
        # D is at depth 2 (via whichever of B/C BFS finds first)
        by_id = {d[0].entity_id: d[1] for d in desc}
        assert by_id["B"] == 1
        assert by_id["C"] == 1
        assert by_id["D"] == 2

    def test_long_chain(self, graph: EntityGraph):
        """Linear chain: 0 -> 1 -> 2 -> 3 -> 4. Verify all depths."""
        for i in range(5):
            graph.add_entity(EntityInstance(entity_type="N", entity_id=str(i)))
        for i in range(4):
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="next",
                    from_type="N",
                    from_id=str(i),
                    to_type="N",
                    to_id=str(i + 1),
                )
            )
        desc = graph.get_descendants("N", "0", "next")
        by_id = {d[0].entity_id: d[1] for d in desc}
        assert by_id == {"1": 1, "2": 2, "3": 3, "4": 4}

    def test_long_chain_max_depth(self, graph: EntityGraph):
        """Linear chain with max_depth=2 stops at node 2."""
        for i in range(5):
            graph.add_entity(EntityInstance(entity_type="N", entity_id=str(i)))
        for i in range(4):
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="next",
                    from_type="N",
                    from_id=str(i),
                    to_type="N",
                    to_id=str(i + 1),
                )
            )
        desc = graph.get_descendants("N", "0", "next", max_depth=2)
        ids = {d[0].entity_id for d in desc}
        assert ids == {"1", "2"}


class TestGetAncestors:
    """Thorough tests for BFS get_ancestors."""

    def test_single_hop(self, populated_graph: EntityGraph):
        """BP-1234 has one incoming 'replaces' from BP-5678."""
        anc = populated_graph.get_ancestors("Part", "BP-1234", "replaces")
        assert len(anc) == 1
        assert anc[0][0].entity_id == "BP-5678"
        assert anc[0][1] == 1

    def test_ignores_wrong_relationship_type(self, populated_graph: EntityGraph):
        """V-CIVIC has incoming 'fits' edges but no 'replaces'."""
        anc = populated_graph.get_ancestors("Vehicle", "V-CIVIC", "replaces")
        assert anc == []

    def test_multi_hop_chain(self, graph: EntityGraph):
        """Chain: A -> B -> C. Ancestors of C via 'parent' = [B, A]."""
        for nid in ["A", "B", "C"]:
            graph.add_entity(EntityInstance(entity_type="N", entity_id=nid))
        for src, dst in [("A", "B"), ("B", "C")]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="parent",
                    from_type="N",
                    from_id=src,
                    to_type="N",
                    to_id=dst,
                )
            )
        anc = graph.get_ancestors("N", "C", "parent")
        by_id = {a[0].entity_id: a[1] for a in anc}
        assert by_id == {"B": 1, "A": 2}

    def test_max_depth_zero(self, populated_graph: EntityGraph):
        """max_depth=0 returns nothing."""
        anc = populated_graph.get_ancestors("Part", "BP-1234", "replaces", max_depth=0)
        assert anc == []

    def test_max_depth_limits(self, graph: EntityGraph):
        """Chain A -> B -> C -> D. Ancestors of D with max_depth=1 = [C]."""
        for nid in ["A", "B", "C", "D"]:
            graph.add_entity(EntityInstance(entity_type="N", entity_id=nid))
        for src, dst in [("A", "B"), ("B", "C"), ("C", "D")]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="parent",
                    from_type="N",
                    from_id=src,
                    to_type="N",
                    to_id=dst,
                )
            )
        anc = graph.get_ancestors("N", "D", "parent", max_depth=1)
        assert len(anc) == 1
        assert anc[0][0].entity_id == "C"

    def test_max_depth_two(self, graph: EntityGraph):
        """Chain A -> B -> C -> D. Ancestors of D with max_depth=2 = [C, B]."""
        for nid in ["A", "B", "C", "D"]:
            graph.add_entity(EntityInstance(entity_type="N", entity_id=nid))
        for src, dst in [("A", "B"), ("B", "C"), ("C", "D")]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="parent",
                    from_type="N",
                    from_id=src,
                    to_type="N",
                    to_id=dst,
                )
            )
        anc = graph.get_ancestors("N", "D", "parent", max_depth=2)
        ids = {a[0].entity_id for a in anc}
        assert ids == {"B", "C"}

    def test_cycle_does_not_loop(self, graph: EntityGraph):
        """Cycle: A -> B -> C -> A. Ancestors of A should terminate."""
        for nid in ["A", "B", "C"]:
            graph.add_entity(EntityInstance(entity_type="N", entity_id=nid))
        for src, dst in [("A", "B"), ("B", "C"), ("C", "A")]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="next",
                    from_type="N",
                    from_id=src,
                    to_type="N",
                    to_id=dst,
                )
            )
        anc = graph.get_ancestors("N", "A", "next")
        ids = {a[0].entity_id for a in anc}
        assert ids == {"C", "B"}

    def test_self_loop_ignored(self, graph: EntityGraph):
        """Self-loop should not cause infinite recursion."""
        graph.add_entity(EntityInstance(entity_type="N", entity_id="1"))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="self_ref",
                from_type="N",
                from_id="1",
                to_type="N",
                to_id="1",
            )
        )
        anc = graph.get_ancestors("N", "1", "self_ref")
        assert anc == []

    def test_missing_entity(self, graph: EntityGraph):
        assert graph.get_ancestors("Part", "MISSING", "replaces") == []

    def test_no_incoming_edges(self, populated_graph: EntityGraph):
        """Root node with no incoming edges of that type."""
        anc = populated_graph.get_ancestors("Part", "BP-5678", "replaces")
        assert anc == []

    def test_multiple_parents(self, graph: EntityGraph):
        """Diamond: A -> C, B -> C. Ancestors of C = [A, B]."""
        for nid in ["A", "B", "C"]:
            graph.add_entity(EntityInstance(entity_type="N", entity_id=nid))
        for src in ["A", "B"]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="parent",
                    from_type="N",
                    from_id=src,
                    to_type="N",
                    to_id="C",
                )
            )
        anc = graph.get_ancestors("N", "C", "parent")
        ids = {a[0].entity_id for a in anc}
        assert ids == {"A", "B"}
        assert all(a[1] == 1 for a in anc)

    def test_diamond_no_duplicates(self, graph: EntityGraph):
        """Diamond: R -> A, R -> B, A -> D, B -> D. Ancestors of D has no dupes."""
        for nid in ["R", "A", "B", "D"]:
            graph.add_entity(EntityInstance(entity_type="N", entity_id=nid))
        for src, dst in [("R", "A"), ("R", "B"), ("A", "D"), ("B", "D")]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="parent",
                    from_type="N",
                    from_id=src,
                    to_type="N",
                    to_id=dst,
                )
            )
        anc = graph.get_ancestors("N", "D", "parent")
        ids = [a[0].entity_id for a in anc]
        assert len(ids) == len(set(ids))
        assert set(ids) == {"A", "B", "R"}


class TestFindPath:
    def test_direct_path(self, populated_graph: EntityGraph):
        path = populated_graph.find_path("Part", "BP-5678", "Vehicle", "V-CIVIC")
        assert path is not None
        assert path[0].entity_id == "BP-5678"
        assert path[-1].entity_id == "V-CIVIC"

    def test_no_path(self, graph: EntityGraph):
        graph.add_entity(EntityInstance(entity_type="A", entity_id="1"))
        graph.add_entity(EntityInstance(entity_type="B", entity_id="2"))
        assert graph.find_path("A", "1", "B", "2") is None

    def test_missing_entity(self, graph: EntityGraph):
        assert graph.find_path("A", "1", "B", "2") is None


class TestIterEdges:
    def test_iter_edges_yields_all_keys(self, populated_graph: EntityGraph):
        """iter_edges() yields dicts with edge identity, properties, and metadata."""
        expected_keys = {
            "from_type",
            "from_id",
            "to_type",
            "to_id",
            "relationship_type",
            "edge_key",
            "properties",
            "metadata",
        }
        for edge in populated_graph.iter_edges():
            assert set(edge.keys()) == expected_keys

    def test_iter_edges_filter(self, populated_graph: EntityGraph):
        """iter_edges(relationship_type=...) filters correctly."""
        fits = list(populated_graph.iter_edges(relationship_type="fits"))
        assert len(fits) == 3
        assert all(e["relationship_type"] == "fits" for e in fits)

        replaces = list(populated_graph.iter_edges(relationship_type="replaces"))
        assert len(replaces) == 1
        assert replaces[0]["relationship_type"] == "replaces"

    def test_iter_edges_no_match(self, populated_graph: EntityGraph):
        assert list(populated_graph.iter_edges(relationship_type="nonexistent")) == []

    def test_list_edges_equals_materialized_iter_edges(self, populated_graph: EntityGraph):
        """list_edges() == list(iter_edges()) — parity assertion."""
        assert populated_graph.list_edges() == list(populated_graph.iter_edges())
        assert populated_graph.list_edges(relationship_type="fits") == list(
            populated_graph.iter_edges(relationship_type="fits")
        )

    def test_iter_relationships_matches_iter_edges(self, populated_graph: EntityGraph):
        """iter_relationships() yields typed rows matching iter_edges() data."""
        for edge_dict, relationship in zip(
            populated_graph.iter_edges(),
            populated_graph.iter_relationships(),
            strict=True,
        ):
            assert relationship.from_type == edge_dict["from_type"]
            assert relationship.from_id == edge_dict["from_id"]
            assert relationship.to_type == edge_dict["to_type"]
            assert relationship.to_id == edge_dict["to_id"]
            assert relationship.properties == edge_dict["properties"]
            assert relationship.metadata.model_dump(mode="json", exclude_none=True) == edge_dict[
                "metadata"
            ]

    def test_iter_edges_empty_graph(self, graph: EntityGraph):
        assert list(graph.iter_edges()) == []

    def test_iter_edges_rejects_missing_relationship_type(
        self, populated_graph: EntityGraph
    ):
        data = populated_graph.to_dict()
        del data["edges"][0]["relationship_type"]
        restored = EntityGraph.from_dict(data)

        with pytest.raises(ValueError, match="missing relationship_type"):
            list(restored.iter_edges())

        with pytest.raises(ValueError, match="missing relationship_type"):
            list(restored.iter_edges(relationship_type="replaces"))


class TestEdgeIteration:
    def test_iter_relationships(self, populated_graph: EntityGraph):
        relationships = list(populated_graph.iter_relationships("fits"))
        assert len(relationships) == 3
        for relationship in relationships:
            assert relationship.from_type == "Part"
            assert relationship.to_type == "Vehicle"

    def test_iter_relationships_all(self, populated_graph: EntityGraph):
        all_relationships = list(populated_graph.iter_relationships())
        assert len(all_relationships) == 4

    def test_get_neighbors_outgoing(self, populated_graph: EntityGraph):
        neighbors = populated_graph.get_neighbors_with_relationship_refs(
            "Part", "BP-1234", "fits", direction="outgoing"
        )
        assert len(neighbors) == 2
        ids = {n[0].entity_id for n in neighbors}
        assert ids == {"V-CIVIC", "V-ACCORD"}

    def test_get_neighbors_incoming(self, populated_graph: EntityGraph):
        neighbors = populated_graph.get_neighbors_with_relationship_refs(
            "Vehicle", "V-CIVIC", "fits", direction="incoming"
        )
        assert len(neighbors) == 2
        ids = {n[0].entity_id for n in neighbors}
        assert ids == {"BP-1234", "BP-5678"}

    def test_get_neighbors_both(self, populated_graph: EntityGraph):
        neighbors = populated_graph.get_neighbors_with_relationship_refs(
            "Part", "BP-1234", direction="both"
        )
        # outgoing: 2 fits edges, incoming: 1 replaces edge
        assert len(neighbors) == 3

    def test_get_neighbors_missing(self, graph: EntityGraph):
        assert graph.get_neighbors_with_relationship_refs("Part", "MISSING") == []


class TestIntrospection:
    def test_list_entity_types_empty(self, graph: EntityGraph):
        assert graph.list_entity_types() == []

    def test_list_entity_types(self, populated_graph: EntityGraph):
        types = populated_graph.list_entity_types()
        assert set(types) == {"Vehicle", "Part"}

    def test_list_relationship_types_empty(self, graph: EntityGraph):
        assert graph.list_relationship_types() == []

    def test_list_relationship_types(self, populated_graph: EntityGraph):
        types = populated_graph.list_relationship_types()
        assert set(types) == {"fits", "replaces"}

    def test_list_relationship_types_sorted(self, populated_graph: EntityGraph):
        types = populated_graph.list_relationship_types()
        assert types == sorted(types)


class TestClear:
    def test_clear(self, populated_graph: EntityGraph):
        populated_graph.clear()
        assert populated_graph.entity_count() == 0
        assert populated_graph.edge_count() == 0

    def test_clear_resets_introspection(self, populated_graph: EntityGraph):
        populated_graph.clear()
        assert populated_graph.list_entity_types() == []
        assert populated_graph.list_relationship_types() == []


class TestSplitNodeId:
    def test_roundtrip(self):
        node_id = make_node_id("Vehicle", "V-123")
        entity_type, entity_id = split_node_id(node_id)
        assert entity_type == "Vehicle"
        assert entity_id == "V-123"

    def test_id_containing_colon(self):
        node_id = make_node_id("Part", "urn:part:123")
        entity_type, entity_id = split_node_id(node_id)
        assert entity_type == "Part"
        assert entity_id == "urn:part:123"

    def test_raises_on_missing_delimiter(self):
        with pytest.raises(ValueError, match="Invalid node_id"):
            split_node_id("nocolon")


class TestIterAllEntities:
    def test_returns_all(self, populated_graph: EntityGraph):
        entities = list(populated_graph.iter_all_entities())
        assert len(entities) == 4
        ids = {(e.entity_type, e.entity_id) for e in entities}
        assert ids == {
            ("Vehicle", "V-CIVIC"),
            ("Vehicle", "V-ACCORD"),
            ("Part", "BP-1234"),
            ("Part", "BP-5678"),
        }

    def test_empty_graph(self, graph: EntityGraph):
        assert list(graph.iter_all_entities()) == []


class TestIsIsolated:
    def test_isolated_entity(self, graph: EntityGraph):
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="LONE"))
        assert graph.is_isolated("Part", "LONE") is True

    def test_connected_entity(self, populated_graph: EntityGraph):
        assert populated_graph.is_isolated("Part", "BP-1234") is False

    def test_missing_entity(self, graph: EntityGraph):
        assert graph.is_isolated("Part", "MISSING") is True


class TestNeighborIds:
    def test_returns_neighbors(self, populated_graph: EntityGraph):
        neighbors = populated_graph.neighbor_ids("Part", "BP-1234")
        expected = {
            make_node_id("Vehicle", "V-CIVIC"),
            make_node_id("Vehicle", "V-ACCORD"),
            make_node_id("Part", "BP-5678"),
        }
        assert neighbors == expected

    def test_missing_entity(self, graph: EntityGraph):
        assert graph.neighbor_ids("Part", "MISSING") == set()


class TestToDictFromDict:
    def test_roundtrip(self, populated_graph: EntityGraph):
        data = populated_graph.to_dict()
        restored = EntityGraph.from_dict(data)

        assert restored.entity_count() == populated_graph.entity_count()
        assert restored.edge_count() == populated_graph.edge_count()

        entity = restored.get_entity("Vehicle", "V-CIVIC")
        assert entity is not None
        assert entity.properties["make"] == "Honda"

        assert restored.has_relationship("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")

    def test_edge_key_continuity(self, populated_graph: EntityGraph):
        """After from_dict, new edge keys should not collide with existing ones."""
        data = populated_graph.to_dict()
        restored = EntityGraph.from_dict(data)

        # BP-1234 -> V-CIVIC already has one "fits" edge
        assert (
            restored.relationship_count_between("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
            == 1
        )

        # Add a second "fits" edge on the same pair (different properties)
        restored.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"source": "new"},
            )
        )

        # Both edges must coexist — proves keys didn't collide
        assert (
            restored.relationship_count_between("Part", "BP-1234", "Vehicle", "V-CIVIC", "fits")
            == 2
        )
        assert restored.edge_count() == populated_graph.edge_count() + 1


class TestExtractAndMerge:
    def test_merge_does_not_let_empty_extracted_stub_overwrite_base_entity(
        self,
    ):
        current = EntityGraph()
        current.add_entity(
            EntityInstance(
                entity_type="ReferenceThing",
                entity_id="R-1",
                properties={"name": "old upstream value"},
            )
        )
        current.add_entity(
            EntityInstance(
                entity_type="LocalThing",
                entity_id="L-1",
                properties={"name": "local value"},
            )
        )
        current.add_relationship(
            RelationshipInstance(
                relationship_type="local_links_reference",
                from_type="LocalThing",
                from_id="L-1",
                to_type="ReferenceThing",
                to_id="R-1",
                properties={"reason": "watch"},
            )
        )

        overlay = current.extract_owned_subgraph(
            entity_types=["LocalThing"],
            relationship_types=["local_links_reference"],
        )
        stub = overlay.get_entity("ReferenceThing", "R-1")
        assert stub is not None
        assert stub.properties == {}

        next_upstream = EntityGraph()
        next_upstream.add_entity(
            EntityInstance(
                entity_type="ReferenceThing",
                entity_id="R-1",
                properties={"name": "new upstream value"},
            )
        )

        merged = EntityGraph.merge_graphs(next_upstream, overlay)

        reference = merged.get_entity("ReferenceThing", "R-1")
        assert reference is not None
        assert reference.properties == {"name": "new upstream value"}
        local = merged.get_entity("LocalThing", "L-1")
        assert local is not None
        assert local.properties == {"name": "local value"}
        assert merged.has_relationship(
            "LocalThing",
            "L-1",
            "ReferenceThing",
            "R-1",
            "local_links_reference",
        )


class TestCountEdges:
    def test_count_edges_incoming(self, populated_graph: EntityGraph):
        """Count incoming edges of a specific type."""
        # V-CIVIC has 2 incoming fits edges (from BP-1234 and BP-5678)
        assert populated_graph.count_edges("Vehicle", "V-CIVIC", "fits", "incoming") == 2
        # V-ACCORD has 1 incoming fits edge (from BP-1234)
        assert populated_graph.count_edges("Vehicle", "V-ACCORD", "fits", "incoming") == 1
        # BP-1234 has 1 incoming replaces edge (from BP-5678)
        assert populated_graph.count_edges("Part", "BP-1234", "replaces", "incoming") == 1
        # BP-1234 has 0 incoming fits edges
        assert populated_graph.count_edges("Part", "BP-1234", "fits", "incoming") == 0

    def test_count_edges_no_node(self, graph: EntityGraph):
        """Non-existent node returns 0."""
        assert graph.count_edges("Part", "MISSING", "fits", "incoming") == 0
        assert graph.count_edges("Part", "MISSING", None, "both") == 0
