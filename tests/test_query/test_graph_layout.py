"""Tests for the normalized graph transport (`layout="graph"`) of query output.

Losslessness is asserted structurally: rows are RECONSTRUCTED from the graph
sections and compared for equality against the serialized rows the layout
replaced — for diamond (convergence), cycle, parallel-edge, include, entity,
relationship, and projected shapes, at both standard and compact profiles.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cruxible_core.config.schema import (
    CoreConfig,
    EntityTypeSchema,
    NamedQuerySchema,
    PropertySchema,
    RelationshipSchema,
    TraversalStep,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.query.engine import execute_query
from cruxible_core.query.graph_layout import normalize_query_items
from cruxible_core.query.profiles import ReadProfile, profile_query_items
from cruxible_core.query.types import dump_query_row

# ---------------------------------------------------------------------------
# Reconstruction: rebuild the rows layout from the graph sections.
# ---------------------------------------------------------------------------


def _node_key(entity: dict[str, Any]) -> tuple[Any, Any]:
    return (entity.get("entity_type"), entity.get("entity_id"))


def _segment_from_ref(ref: dict[str, Any], edges: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild a rows-layout edge payload from a physical card plus a ref alias."""
    return {**edges[ref["edge"]], "alias": ref["alias"]}


def _reconstruct_includes(
    include_refs: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        alias: {
            "alias": include["alias"],
            "many": include["many"],
            "exists": include["exists"],
            "count": include["count"],
            "limit": include["limit"],
            "truncated": include["truncated"],
            "items": [
                {
                    "edge": _segment_from_ref(item, edges),
                    "source": nodes[item["source"]],
                    "target": nodes[item["target"]],
                }
                for item in include["items"]
            ],
        }
        for alias, include in include_refs.items()
    }


def _reconstruct_base_row(ref: dict[str, Any], sections: dict[str, Any]) -> dict[str, Any]:
    nodes = sections["nodes"]
    edges = sections["edges"]
    node_by_key = {_node_key(node): node for node in nodes}
    if "edge" in ref:
        edge = edges[ref["edge"]]
        row = dict(edge)
        row["entry"] = nodes[ref["entry"]]
        row["from_entity"] = nodes[ref["from_entity"]] if ref["from_entity"] is not None else None
        row["to_entity"] = nodes[ref["to_entity"]] if ref["to_entity"] is not None else None
        row["includes"] = _reconstruct_includes(ref["includes"], nodes, edges)
        return row
    if "entry" in ref:
        (path_index,) = ref["paths"]
        path_edges = [_segment_from_ref(step, edges) for step in sections["paths"][path_index]]
        # Walk the path from the entry: each segment connects the current node
        # to its other endpoint — this recovers the per-row `entities` array
        # that the graph layout deliberately does not materialize.
        current = _node_key(nodes[ref["entry"]])
        entities = [node_by_key[current]]
        for edge in path_edges:
            source_key = (edge["from_type"], edge["from_id"])
            target_key = (edge["to_type"], edge["to_id"])
            current = target_key if source_key == current else source_key
            entities.append(node_by_key[current])
        return {
            "entry": nodes[ref["entry"]],
            "result": nodes[ref["result"]],
            "entities": entities,
            "path": path_edges,
            "includes": _reconstruct_includes(ref["includes"], nodes, edges),
        }
    return nodes[ref["result"]]


def _reconstruct_rows(sections: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ref in sections["results"]:
        if "values" in ref:
            source = ref["source"]
            rows.append(
                {
                    "values": ref["values"],
                    "source": (
                        _reconstruct_base_row(source, sections) if source is not None else None
                    ),
                }
            )
        else:
            rows.append(_reconstruct_base_row(ref, sections))
    return rows


def _dump_rows(result: Any, profile: ReadProfile = "standard") -> list[dict[str, Any]]:
    return [
        dump_query_row(row, include_source=True, mode="json", profile=profile)
        for row in result.results
    ]


def _assert_lossless(result: Any, profile: ReadProfile) -> dict[str, Any]:
    """Normalize the serialized rows, reconstruct them, and assert equality."""
    items = _dump_rows(result, profile)
    sections = normalize_query_items(items)
    assert _reconstruct_rows(sections) == items
    return sections


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> CoreConfig:
    return CoreConfig(
        name="graph-layout",
        entity_types={
            "Vehicle": EntityTypeSchema(
                properties={
                    "vehicle_id": PropertySchema(type="string", primary_key=True),
                    "make": PropertySchema(type="string"),
                }
            ),
            "Part": EntityTypeSchema(
                properties={
                    "part_number": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                }
            ),
        },
        relationships=[
            RelationshipSchema(
                name="fits",
                from_entity="Part",
                to_entity="Vehicle",
                reverse_name="fitted_parts",
                properties={"verified": PropertySchema(type="bool", optional=True)},
            ),
            RelationshipSchema(
                name="replaces",
                from_entity="Part",
                to_entity="Part",
                properties={"direction": PropertySchema(type="string", optional=True)},
            ),
        ],
        named_queries={
            # Diamond: Part -> its Vehicles -> their Parts (converges on parts
            # fitting several shared vehicles via multiple distinct paths).
            "diamond_back_to_part": NamedQuerySchema(
                mode="traversal",
                entry_point="Part",
                traversal=[
                    TraversalStep(relationship="fits", direction="outgoing", alias="hop1"),
                    TraversalStep(relationship="fitted_parts", direction="outgoing", alias="hop2"),
                ],
                returns="list[Part]",
                result_shape="path",
                dedupe="path",
                include={
                    "replacements": {
                        "from": "$result",
                        "relationship": "replaces",
                        "direction": "incoming",
                        "many": True,
                    }
                },
            ),
            # Same physical edge under two step aliases: hop2 walks fits
            # edges in reverse (fitted_parts) and hop3 walks them forward,
            # so each P-DUAL fits edge is visited at hop2 in one path and
            # at hop3 in the other.
            "diamond_vehicle_loop": NamedQuerySchema(
                mode="traversal",
                entry_point="Part",
                traversal=[
                    TraversalStep(relationship="fits", direction="outgoing", alias="hop1"),
                    TraversalStep(relationship="fitted_parts", direction="outgoing", alias="hop2"),
                    TraversalStep(relationship="fits", direction="outgoing", alias="hop3"),
                ],
                returns="list[Vehicle]",
                result_shape="path",
                dedupe="path",
            ),
            "parts_paths": NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(relationship="fits", direction="incoming", alias="fit"),
                ],
                returns="list[Part]",
                result_shape="path",
                dedupe="path",
                allow_relationship_state_override=True,
            ),
            "parts_entities": NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(relationship="fits", direction="incoming"),
                ],
                returns="list[Part]",
                result_shape="entity",
            ),
            "replaces_edges": NamedQuerySchema(
                mode="collection",
                returns="replaces",
                result_shape="relationship",
            ),
            "part_names_projected": NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(relationship="fits", direction="incoming", alias="fit"),
                ],
                returns="list[Part]",
                result_shape="path",
                dedupe="path",
                select={
                    "part": "$result.properties.name",
                    "verified": "$path.fit.edge.properties.verified",
                },
            ),
        },
    )


def _entity(entity_type: str, entity_id: str, **properties: Any) -> EntityInstance:
    return EntityInstance(entity_type=entity_type, entity_id=entity_id, properties=properties)


def _fits(part: str, vehicle: str, **properties: Any) -> RelationshipInstance:
    return RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id=part,
        to_type="Vehicle",
        to_id=vehicle,
        properties=properties,
    )


@pytest.fixture
def diamond_graph() -> EntityGraph:
    """P-A fits V-1 and V-2; P-DUAL also fits both -> two paths P-A..P-DUAL."""
    g = EntityGraph()
    g.add_entity(_entity("Part", "P-A", part_number="P-A", name="Anchor"))
    g.add_entity(_entity("Part", "P-DUAL", part_number="P-DUAL", name="Dual"))
    g.add_entity(_entity("Part", "P-REP", part_number="P-REP", name="Replacement"))
    g.add_entity(_entity("Vehicle", "V-1", vehicle_id="V-1", make="Honda"))
    g.add_entity(_entity("Vehicle", "V-2", vehicle_id="V-2", make="Toyota"))
    g.add_relationship(_fits("P-A", "V-1", verified=True))
    g.add_relationship(_fits("P-A", "V-2", verified=True))
    g.add_relationship(_fits("P-DUAL", "V-1", verified=True))
    g.add_relationship(_fits("P-DUAL", "V-2", verified=False))
    g.add_relationship(
        RelationshipInstance(
            relationship_type="replaces",
            from_type="Part",
            from_id="P-REP",
            to_type="Part",
            to_id="P-DUAL",
            properties={"direction": "upgrade"},
        )
    )
    return g


# ---------------------------------------------------------------------------
# (a) Losslessness
# ---------------------------------------------------------------------------


class TestLosslessness:
    @pytest.mark.parametrize("profile", ["standard", "compact"])
    def test_diamond_with_includes_round_trips(self, config, diamond_graph, profile) -> None:
        result = execute_query(
            config, diamond_graph, "diamond_back_to_part", {"part_number": "P-A"}
        )
        sections = _assert_lossless(result, profile)
        # Convergence: P-DUAL is reached via two distinct paths but serialized once.
        dual_nodes = [node for node in sections["nodes"] if node["entity_id"] == "P-DUAL"]
        assert len(dual_nodes) == 1
        # Include neighbors dedupe into the shared arrays too.
        assert any(node["entity_id"] == "P-REP" for node in sections["nodes"])

    @pytest.mark.parametrize("profile", ["standard", "compact"])
    def test_parallel_edges_round_trip(self, config, profile) -> None:
        g = EntityGraph()
        g.add_entity(_entity("Vehicle", "V-1", vehicle_id="V-1", make="Honda"))
        g.add_entity(_entity("Part", "P-1", part_number="P-1", name="Pads"))
        g.add_relationship(_fits("P-1", "V-1", verified=True))
        g.add_relationship(_fits("P-1", "V-1", verified=False))
        result = execute_query(config, g, "parts_paths", {"vehicle_id": "V-1"})
        assert result.total_results == 2
        sections = _assert_lossless(result, profile)
        # Parallel edges stay distinct by edge_key; endpoints serialize once.
        assert len(sections["edges"]) == 2
        edge_keys = {edge["edge_key"] for edge in sections["edges"]}
        assert len(edge_keys) == 2
        assert len(sections["nodes"]) == 2

    @pytest.mark.parametrize("profile", ["standard", "compact"])
    def test_cycle_relationship_rows_round_trip(self, config, profile) -> None:
        g = EntityGraph()
        for part in ("P-1", "P-2", "P-3"):
            g.add_entity(_entity("Part", part, part_number=part, name=f"Part {part}"))
        for source, target in (("P-1", "P-2"), ("P-2", "P-3"), ("P-3", "P-1")):
            g.add_relationship(
                RelationshipInstance(
                    relationship_type="replaces",
                    from_type="Part",
                    from_id=source,
                    to_type="Part",
                    to_id=target,
                    properties={"direction": "cycle"},
                )
            )
        result = execute_query(config, g, "replaces_edges", {})
        assert result.total_results == 3
        sections = _assert_lossless(result, profile)
        # The cycle's three entities serialize once each despite appearing as
        # entry/from/to across all three rows; each edge serializes once.
        assert len(sections["nodes"]) == 3
        assert len(sections["edges"]) == 3
        assert sections["paths"] == []

    @pytest.mark.parametrize("profile", ["standard", "compact"])
    def test_entity_rows_round_trip(self, config, diamond_graph, profile) -> None:
        result = execute_query(config, diamond_graph, "parts_entities", {"vehicle_id": "V-1"})
        assert result.total_results == 2
        sections = _assert_lossless(result, profile)
        assert sections["edges"] == []
        assert sections["paths"] == []
        assert [ref["result"] for ref in sections["results"]] == [0, 1]

    @pytest.mark.parametrize("profile", ["standard", "compact"])
    def test_projected_rows_round_trip(self, config, diamond_graph, profile) -> None:
        result = execute_query(config, diamond_graph, "part_names_projected", {"vehicle_id": "V-1"})
        assert result.total_results == 2
        sections = _assert_lossless(result, profile)
        assert all("values" in ref for ref in sections["results"])
        assert all(ref["source"] is not None for ref in sections["results"])

    @pytest.mark.parametrize("profile", ["standard", "compact"])
    def test_same_edge_under_two_aliases_is_one_card(self, config, diamond_graph, profile) -> None:
        """Physical edge identity: one card, per-occurrence aliases on the refs."""
        result = execute_query(
            config, diamond_graph, "diamond_vehicle_loop", {"part_number": "P-A"}
        )
        assert result.total_results == 2, "fixture must retain both three-hop paths"
        sections = _assert_lossless(result, profile)
        # Cards never carry an alias key.
        assert all("alias" not in edge for edge in sections["edges"])
        # Four physical edges despite six traversal-step occurrences.
        assert len(sections["edges"]) == 4
        # Each P-DUAL fits edge is ONE card referenced under BOTH aliases.
        for vehicle in ("V-1", "V-2"):
            (card_index,) = [
                index
                for index, edge in enumerate(sections["edges"])
                if edge["from_id"] == "P-DUAL" and edge["to_id"] == vehicle
            ]
            aliases = sorted(
                step["alias"]
                for path in sections["paths"]
                for step in path
                if step["edge"] == card_index
            )
            assert aliases == ["hop2", "hop3"]

    def test_row_order_is_preserved(self, config, diamond_graph) -> None:
        result = execute_query(
            config, diamond_graph, "diamond_back_to_part", {"part_number": "P-A"}
        )
        items = _dump_rows(result)
        sections = normalize_query_items(items)
        nodes = sections["nodes"]
        expected = [(row["entry"]["entity_id"], row["result"]["entity_id"]) for row in items]
        actual = [
            (nodes[ref["entry"]]["entity_id"], nodes[ref["result"]]["entity_id"])
            for ref in sections["results"]
        ]
        assert actual == expected


# ---------------------------------------------------------------------------
# (b) dedupe=path multi-path semantics
# ---------------------------------------------------------------------------


class TestPathDedupe:
    def test_multiple_paths_to_one_result_stay_distinct(self, config, diamond_graph) -> None:
        result = execute_query(
            config, diamond_graph, "diamond_back_to_part", {"part_number": "P-A"}
        )
        items = _dump_rows(result)
        dual_rows = [row for row in items if row["result"]["entity_id"] == "P-DUAL"]
        assert len(dual_rows) == 2, "fixture must retain two distinct paths to P-DUAL"

        sections = normalize_query_items(items)
        nodes = sections["nodes"]
        dual_refs = [
            ref for ref in sections["results"] if nodes[ref["result"]]["entity_id"] == "P-DUAL"
        ]
        # Two result entries referencing two distinct path entries, one node set.
        assert len(dual_refs) == 2
        dual_path_indexes = [index for ref in dual_refs for index in ref["paths"]]
        assert len(set(dual_path_indexes)) == 2
        assert sections["paths"][dual_path_indexes[0]] != sections["paths"][dual_path_indexes[1]]
        assert len([node for node in nodes if node["entity_id"] == "P-DUAL"]) == 1


# ---------------------------------------------------------------------------
# (d) Profile composition
# ---------------------------------------------------------------------------


class TestProfileComposition:
    def test_compact_governance_markers_survive_normalization(self, config) -> None:
        g = EntityGraph()
        g.add_entity(_entity("Vehicle", "V-1", vehicle_id="V-1", make="Honda"))
        g.add_entity(_entity("Part", "P-1", part_number="P-1", name="Pads"))
        g.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": True},
                metadata={
                    "assertion": {
                        "review": {"status": "pending"},
                        "lifecycle": {"status": "active"},
                    }
                },
            )
        )
        result = execute_query(
            config, g, "parts_paths", {"vehicle_id": "V-1"}, relationship_state="all"
        )
        assert result.total_results == 1

        compact_sections = _assert_lossless(result, "compact")
        (edge,) = compact_sections["edges"]
        assert edge["metadata"]["assertion"]["review"]["status"] == "pending"
        assert edge["metadata"]["assertion"]["lifecycle"]["status"] == "active"
        # Compact node cards stay bounded identity cards.
        for node in compact_sections["nodes"]:
            assert set(node) == {"entity_type", "entity_id", "properties", "metadata"}

        standard_sections = _assert_lossless(result, "standard")
        (standard_edge,) = standard_sections["edges"]
        assert standard_edge["metadata"]["assertion"]["review"]["status"] == "pending"

    def test_graph_cards_are_exactly_the_profiled_row_payloads(self, config, diamond_graph) -> None:
        """Node cards ARE the row payloads; edge cards are them minus `alias`."""
        result = execute_query(
            config, diamond_graph, "diamond_back_to_part", {"part_number": "P-A"}
        )
        items = profile_query_items(_dump_rows(result), "compact")
        sections = normalize_query_items(items)
        first_row = items[0]
        entry_index = sections["results"][0]["entry"]
        assert sections["nodes"][entry_index] is first_row["entry"]
        first_path_index = sections["results"][0]["paths"][0]
        first_step = sections["paths"][first_path_index][0]
        source_segment = first_row["path"][0]
        # The per-occurrence alias moves to the step reference; the card is
        # the physical remainder of the same payload, not a reshaping.
        assert first_step["alias"] == source_segment["alias"]
        assert sections["edges"][first_step["edge"]] == {
            key: value for key, value in source_segment.items() if key != "alias"
        }


# ---------------------------------------------------------------------------
# (h) rb-2-shaped byte measurement
# ---------------------------------------------------------------------------


def _scenery_fixture() -> tuple[CoreConfig, EntityGraph]:
    """A scenery_at_place-shaped dataset: one fat anchor, 38 one-hop results.

    Mirrors benchmarks/read_anchor rb-2 (scenery at lumbridge): a single Place
    entity with a large property card, 38 Scenery entities each reached by one
    `located_at` edge — the shape whose rows layout duplicated the anchor card
    into every row (57,263 bytes compact for a 492-byte identity answer).
    """
    config = CoreConfig(
        name="scenery",
        entity_types={
            "Place": EntityTypeSchema(
                properties={
                    "place_id": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                    "title": PropertySchema(type="string"),
                    "summary": PropertySchema(type="string"),
                    "status": PropertySchema(type="string"),
                }
            ),
            "Scenery": EntityTypeSchema(
                properties={
                    "scenery_id": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                }
            ),
        },
        relationships=[
            RelationshipSchema(
                name="located_at",
                from_entity="Scenery",
                to_entity="Place",
                properties={"plane": PropertySchema(type="int", optional=True)},
            ),
        ],
        named_queries={
            "scenery_at_place": NamedQuerySchema(
                mode="traversal",
                entry_point="Place",
                traversal=[
                    TraversalStep(relationship="located_at", direction="incoming", alias="loc"),
                ],
                returns="list[Scenery]",
                result_shape="path",
                dedupe="path",
            ),
        },
    )
    g = EntityGraph()
    g.add_entity(
        EntityInstance(
            entity_type="Place",
            entity_id="lumbridge",
            properties={
                "place_id": "lumbridge",
                "name": "Lumbridge",
                "title": "Lumbridge Castle and Township",
                "summary": (
                    "A starting township on the banks of the River Lum, featuring "
                    "a castle with a courtyard, a general store, a church, ranges "
                    "and furnaces, and the surrounding swamp and cow fields that "
                    "new adventurers explore first. The town spans several planes "
                    "of the castle keep and hosts numerous quest start points. "
                    "The castle's ground floor holds the bank of the duke and a "
                    "kitchen with a range used by new cooks, while the courtyard "
                    "connects to the general store, the graveyard, and the church "
                    "of Saradomin with its organ and altar. North of the castle "
                    "lie the cow field and chicken coop that supply hides, meat, "
                    "feathers, and eggs; to the south the swamp caves open beneath "
                    "the town with their own lighting hazards. The River Lum "
                    "splits the town from the eastern farms, crossed by a bridge "
                    "where fishing spots line the banks. Goblins roam the east "
                    "bank in numbers suitable for early combat training, and the "
                    "town's furnace and anvil serve early smiths working the "
                    "nearby copper and tin rocks."
                ),
                "status": "released",
            },
        )
    )
    for index in range(38):
        scenery_id = f"SC-{index:02d}"
        g.add_entity(
            EntityInstance(
                entity_type="Scenery",
                entity_id=scenery_id,
                properties={"scenery_id": scenery_id, "name": f"Scenery object {index}"},
            )
        )
        g.add_relationship(
            RelationshipInstance(
                relationship_type="located_at",
                from_type="Scenery",
                from_id=scenery_id,
                to_type="Place",
                to_id="lumbridge",
                properties={"plane": index % 4},
            )
        )
    return config, g


class TestSceneryByteMeasurement:
    def test_graph_layout_is_at_least_5x_smaller_at_compact(self) -> None:
        config, graph = _scenery_fixture()
        result = execute_query(config, graph, "scenery_at_place", {"place_id": "lumbridge"})
        assert result.total_results == 38

        rows = profile_query_items(_dump_rows(result), "compact")
        sections = normalize_query_items(rows)
        assert _reconstruct_rows(sections) == rows

        rows_bytes = len(json.dumps({"items": rows}).encode())
        graph_bytes = len(json.dumps({"layout": "graph", **sections}).encode())
        ratio = rows_bytes / graph_bytes
        print(
            f"\nscenery_at_place compact bytes: rows={rows_bytes} "
            f"graph={graph_bytes} ratio={ratio:.1f}x"
        )
        assert graph_bytes * 5 <= rows_bytes, (
            f"expected graph layout at least 5x smaller: rows={rows_bytes} "
            f"graph={graph_bytes} ratio={ratio:.1f}x"
        )
