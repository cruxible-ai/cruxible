"""Bounded neighborhood expansion: BFS determinism, budgets, state parity.

Covers the graph-layer ``expand_neighborhood`` BFS and the
``read_surface.inspect_neighborhood`` wrapper: diamond-graph correctness,
cycle termination, budget truncation with deterministic partial results,
filter composition (filtered neighbors consume NO budget), projection
validation, and bit-parity of edge visibility against the query engine's
``relationship_matches_query_state``.
"""

from __future__ import annotations

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
)
from cruxible_core.query.read_surface import (
    inspect_neighborhood,
    neighborhood_requested,
    validate_neighborhood_projection,
)
from cruxible_core.query.relationship_state import relationship_matches_query_state


def _metadata(review: str = "unreviewed", lifecycle: str = "active") -> RelationshipMetadata:
    return RelationshipMetadata(
        assertion=RelationshipAssertion(
            review=RelationshipReviewState(status=review),  # type: ignore[arg-type]
            lifecycle=RelationshipLifecycleState(status=lifecycle),  # type: ignore[arg-type]
        )
    )


def _graph(edges: list[tuple[str, str]], *, entity_type: str = "Node") -> EntityGraph:
    graph = EntityGraph()
    ids = sorted({eid for pair in edges for eid in pair})
    for eid in ids:
        graph.add_entity(
            EntityInstance(entity_type=entity_type, entity_id=eid, properties={"name": eid})
        )
    for from_id, to_id in edges:
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="linked",
                from_type=entity_type,
                from_id=from_id,
                to_type=entity_type,
                to_id=to_id,
            )
        )
    return graph


def _node_ids(result) -> list[tuple[str, int]]:
    return [(node.entity.entity_id, node.depth) for node in result.nodes]


def _edge_ids(result) -> list[tuple[str, str, int | None]]:
    return [(edge.from_id, edge.to_id, edge.edge_key) for edge in result.edges]


class TestDiamondBfs:
    """(a) depth-2 BFS on A->B->D, A->C->D: D once, at min depth, deterministic."""

    def test_diamond_nodes_edges_and_min_depth(self) -> None:
        graph = _graph([("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")])
        result = inspect_neighborhood(graph, "Node", "A", depth=2)
        assert result.found
        assert _node_ids(result) == [("B", 1), ("C", 1), ("D", 2)]
        assert _edge_ids(result) == [("A", "B", 0), ("A", "C", 1), ("B", "D", 2), ("C", "D", 3)]
        assert result.nodes_returned == 3
        assert result.edges_returned == 4
        assert result.truncated is False
        assert result.truncation_reasons == []

    def test_two_runs_are_identical(self) -> None:
        graph = _graph([("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")])
        first = inspect_neighborhood(graph, "Node", "A", depth=2)
        second = inspect_neighborhood(graph, "Node", "A", depth=2)
        assert _node_ids(first) == _node_ids(second)
        assert _edge_ids(first) == _edge_ids(second)

    def test_insertion_order_does_not_change_result_order(self) -> None:
        forward = _graph([("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")])
        reversed_seed = _graph([("C", "D"), ("B", "D"), ("A", "C"), ("A", "B")])
        first = inspect_neighborhood(forward, "Node", "A", depth=2)
        second = inspect_neighborhood(reversed_seed, "Node", "A", depth=2)
        assert _node_ids(first) == _node_ids(second)
        assert [(e.from_id, e.to_id) for e in first.edges] == [
            (e.from_id, e.to_id) for e in second.edges
        ]


class TestCycles:
    """(i) mutual edges terminate; each entity returned once."""

    def test_mutual_edges_terminate_each_entity_once(self) -> None:
        graph = _graph([("A", "B"), ("B", "A")])
        result = inspect_neighborhood(graph, "Node", "A", depth=4)
        assert _node_ids(result) == [("B", 1)]
        # Both directions of the cycle are returned as edges, exactly once.
        assert sorted((e.from_id, e.to_id) for e in result.edges) == [("A", "B"), ("B", "A")]
        assert result.truncated is False

    def test_self_loop_returns_edge_but_not_a_node(self) -> None:
        graph = _graph([("A", "A"), ("A", "B")])
        result = inspect_neighborhood(graph, "Node", "A", depth=2)
        assert _node_ids(result) == [("B", 1)]
        assert sorted((e.from_id, e.to_id) for e in result.edges) == [("A", "A"), ("A", "B")]


class TestBudgets:
    """(b) budget truncation: deterministic partial results, correct reasons."""

    def test_node_budget_truncates_deterministically(self) -> None:
        graph = _graph([("A", "B"), ("A", "C"), ("A", "D"), ("A", "E")])
        first = inspect_neighborhood(graph, "Node", "A", depth=1, max_nodes=2)
        second = inspect_neighborhood(graph, "Node", "A", depth=1, max_nodes=2)
        assert first.truncated is True
        assert first.truncation_reasons == ["node_budget"]
        # Candidate order is (relationship_type, to_type, to_id, ...): B then C.
        assert _node_ids(first) == [("B", 1), ("C", 1)]
        assert first.nodes_returned == 2
        assert first.edges_returned == 2
        assert _node_ids(first) == _node_ids(second)
        assert _edge_ids(first) == _edge_ids(second)

    def test_edge_budget_truncates_and_stops(self) -> None:
        graph = _graph([("A", "B"), ("A", "C"), ("A", "D")])
        result = inspect_neighborhood(graph, "Node", "A", depth=1, max_edges=1)
        assert result.truncated is True
        assert result.truncation_reasons == ["edge_budget"]
        assert _node_ids(result) == [("B", 1)]
        assert result.edges_returned == 1

    def test_depth_horizon_is_reported(self) -> None:
        graph = _graph([("A", "B"), ("B", "C")])
        result = inspect_neighborhood(graph, "Node", "A", depth=1)
        assert _node_ids(result) == [("B", 1)]
        assert result.truncated is True
        assert result.truncation_reasons == ["depth"]

    def test_no_depth_reason_when_horizon_is_exhausted(self) -> None:
        graph = _graph([("A", "B"), ("B", "C")])
        result = inspect_neighborhood(graph, "Node", "A", depth=2)
        assert result.truncated is False
        assert result.truncation_reasons == []

    def test_legacy_limit_maps_to_node_budget(self) -> None:
        """The previously silent single-hop `[:limit]` cap is now visible."""
        graph = _graph([("A", "B"), ("A", "C"), ("A", "D")])
        result = inspect_neighborhood(graph, "Node", "A", depth=1, limit=1)
        assert _node_ids(result) == [("B", 1)]
        assert result.truncated is True
        assert "node_budget" in result.truncation_reasons

    def test_explicit_max_nodes_wins_over_limit(self) -> None:
        graph = _graph([("A", "B"), ("A", "C"), ("A", "D")])
        result = inspect_neighborhood(graph, "Node", "A", depth=1, limit=1, max_nodes=3)
        assert result.nodes_returned == 3
        assert result.truncated is False


class TestFilters:
    """(d) target_types + relationship_types compose; budgets count RETURNED."""

    def _mixed_graph(self) -> EntityGraph:
        graph = EntityGraph()
        for entity_type, entity_id in [
            ("Hub", "H"),
            ("Widget", "W1"),
            ("Widget", "W2"),
            ("Gadget", "G1"),
            ("Gadget", "G2"),
        ]:
            graph.add_entity(EntityInstance(entity_type=entity_type, entity_id=entity_id))
        for rel, to_type, to_id in [
            ("makes", "Widget", "W1"),
            ("makes", "Widget", "W2"),
            ("owns", "Gadget", "G1"),
            ("owns", "Gadget", "G2"),
        ]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type=rel,
                    from_type="Hub",
                    from_id="H",
                    to_type=to_type,
                    to_id=to_id,
                )
            )
        return graph

    def test_target_types_only_returns_matching_entities(self) -> None:
        graph = self._mixed_graph()
        result = inspect_neighborhood(graph, "Hub", "H", depth=1, target_types=["Widget"])
        assert [(n.entity.entity_type, n.entity.entity_id) for n in result.nodes] == [
            ("Widget", "W1"),
            ("Widget", "W2"),
        ]
        assert all(e.to_type == "Widget" for e in result.edges)

    def test_relationship_types_filter(self) -> None:
        graph = self._mixed_graph()
        result = inspect_neighborhood(graph, "Hub", "H", depth=1, relationship_types=["owns"])
        assert [(n.entity.entity_type, n.entity.entity_id) for n in result.nodes] == [
            ("Gadget", "G1"),
            ("Gadget", "G2"),
        ]

    def test_single_relationship_type_unions_with_repeatable(self) -> None:
        graph = self._mixed_graph()
        result = inspect_neighborhood(
            graph,
            "Hub",
            "H",
            depth=1,
            relationship_type="makes",
            relationship_types=["owns"],
        )
        assert result.nodes_returned == 4

    def test_filtered_out_neighbors_do_not_consume_node_budget(self) -> None:
        """Budgets count returned nodes, not visited candidates.

        Candidate order is (relationship_type, ...), so the two `makes`
        Widget edges are scanned BEFORE the `owns` Gadget edges. With
        target_types=[Gadget] and max_nodes=2, the Widget candidates are
        dropped by the filter and must not eat the budget: both Gadgets
        fit and the read is NOT truncated.
        """
        graph = self._mixed_graph()
        result = inspect_neighborhood(
            graph, "Hub", "H", depth=1, target_types=["Gadget"], max_nodes=2
        )
        assert [(n.entity.entity_type, n.entity.entity_id) for n in result.nodes] == [
            ("Gadget", "G1"),
            ("Gadget", "G2"),
        ]
        assert result.truncated is False
        assert result.truncation_reasons == []


class TestStateParity:
    """(c) edge visibility is bit-identical to relationship_matches_query_state."""

    STATES = ("live", "accepted", "all", "not-live", "pending", "reviewable")

    def _governed_graph(self) -> tuple[EntityGraph, dict[str, RelationshipMetadata]]:
        graph = EntityGraph()
        metadata_by_target = {
            "LIVE": _metadata(review="unreviewed", lifecycle="active"),
            "APPROVED": _metadata(review="approved", lifecycle="active"),
            "PENDING": _metadata(review="pending", lifecycle="active"),
            "REJECTED": _metadata(review="rejected", lifecycle="active"),
            "SUPERSEDED": _metadata(review="approved", lifecycle="superseded"),
            "RETRACTED": _metadata(review="unreviewed", lifecycle="retracted"),
        }
        graph.add_entity(EntityInstance(entity_type="Node", entity_id="ROOT"))
        for target, metadata in metadata_by_target.items():
            graph.add_entity(EntityInstance(entity_type="Node", entity_id=target))
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="linked",
                    from_type="Node",
                    from_id="ROOT",
                    to_type="Node",
                    to_id=target,
                    metadata=metadata,
                )
            )
        return graph, metadata_by_target

    @pytest.mark.parametrize("state", STATES)
    def test_expansion_matches_predicate_for_every_edge(self, state: str) -> None:
        graph, metadata_by_target = self._governed_graph()
        result = inspect_neighborhood(graph, "Node", "ROOT", depth=1, state=state)  # type: ignore[arg-type]
        returned_targets = {edge.to_id for edge in result.edges}
        for target, metadata in metadata_by_target.items():
            expected = relationship_matches_query_state(metadata, state)  # type: ignore[arg-type]
            assert (target in returned_targets) is expected, (
                f"state={state} target={target}: expansion visibility diverged "
                "from relationship_matches_query_state"
            )

    def test_default_state_is_all_per_the_inspection_contract(self) -> None:
        """No explicit state returns EVERY stored edge, like single-hop/list edges."""
        graph, metadata_by_target = self._governed_graph()
        default = inspect_neighborhood(graph, "Node", "ROOT", depth=1)
        explicit_all = inspect_neighborhood(graph, "Node", "ROOT", depth=1, state="all")
        assert default.state == "all"
        assert _edge_ids(default) == _edge_ids(explicit_all)
        assert {e.to_id for e in default.edges} == set(metadata_by_target)
        assert default.edges_hidden_by_state == 0
        # Governance markers survive on the default read: pending is marked.
        by_target = {edge.to_id: edge.metadata for edge in default.edges}
        assert by_target["PENDING"]["assertion"]["review"]["status"] == "pending"

    def test_pending_edge_hidden_from_live_visible_to_pending_states(self) -> None:
        graph, _ = self._governed_graph()
        live = inspect_neighborhood(graph, "Node", "ROOT", depth=1, state="live")
        assert "PENDING" not in {e.to_id for e in live.edges}
        for state in ("pending", "reviewable", "all"):
            result = inspect_neighborhood(graph, "Node", "ROOT", depth=1, state=state)  # type: ignore[arg-type]
            assert "PENDING" in {e.to_id for e in result.edges}

    def test_markers_survive_on_returned_edges(self) -> None:
        """Pending/superseded edges carry their review/lifecycle markers."""
        graph, _ = self._governed_graph()
        result = inspect_neighborhood(graph, "Node", "ROOT", depth=1, state="all")
        by_target = {edge.to_id: edge.metadata for edge in result.edges}
        assert by_target["PENDING"]["assertion"]["review"]["status"] == "pending"
        assert by_target["REJECTED"]["assertion"]["review"]["status"] == "rejected"
        assert by_target["SUPERSEDED"]["assertion"]["lifecycle"]["status"] == "superseded"
        assert by_target["LIVE"]["assertion"]["review"]["status"] == "unreviewed"


class TestEdgesHiddenByState:
    """`edges_hidden_by_state`: edges excluded SOLELY by an explicit state.

    The count covers the frontier the BFS actually explored — hidden edges
    consume no budget and are never walked, so regions reachable only through
    hidden edges are never speculatively counted.
    """

    def _pending_star(self, count: int = 6) -> EntityGraph:
        """ROOT with `count` pending-only dependency edges (the op-1 shape)."""
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Node", entity_id="ROOT"))
        for index in range(count):
            target = f"DEP-{index}"
            graph.add_entity(EntityInstance(entity_type="Node", entity_id=target))
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="depends_on",
                    from_type="Node",
                    from_id="ROOT",
                    to_type="Node",
                    to_id=target,
                    metadata=_metadata(review="pending"),
                )
            )
        return graph

    def test_op1_regression_default_shows_pending_explicit_live_reports_hidden(self) -> None:
        """Pending-only neighborhoods must never read as confident empties."""
        graph = self._pending_star(6)
        default = inspect_neighborhood(graph, "Node", "ROOT", depth=1)
        assert default.edges_returned == 6
        assert default.edges_hidden_by_state == 0
        assert all(
            edge.metadata["assertion"]["review"]["status"] == "pending" for edge in default.edges
        )
        live = inspect_neighborhood(graph, "Node", "ROOT", depth=1, state="live")
        assert live.edges == []
        assert live.nodes == []
        assert live.edges_hidden_by_state == 6

    def test_count_is_per_state_not_just_live(self) -> None:
        graph = self._pending_star(2)
        # `pending` shows the pending edges and hides nothing here.
        pending = inspect_neighborhood(graph, "Node", "ROOT", depth=1, state="pending")
        assert pending.edges_returned == 2
        assert pending.edges_hidden_by_state == 0
        accepted = inspect_neighborhood(graph, "Node", "ROOT", depth=1, state="accepted")
        assert accepted.edges_returned == 0
        assert accepted.edges_hidden_by_state == 2

    def test_edges_failing_other_filters_are_not_counted(self) -> None:
        """Only edges excluded SOLELY by state count as hidden."""
        graph = EntityGraph()
        for entity_type, entity_id in [("Hub", "H"), ("Widget", "W"), ("Gadget", "G")]:
            graph.add_entity(EntityInstance(entity_type=entity_type, entity_id=entity_id))
        for rel, to_type, to_id in [("makes", "Widget", "W"), ("owns", "Gadget", "G")]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type=rel,
                    from_type="Hub",
                    from_id="H",
                    to_type=to_type,
                    to_id=to_id,
                    metadata=_metadata(review="pending"),
                )
            )
        # Relationship filter drops `owns` before state is consulted: only the
        # `makes` edge is hidden by state.
        by_rel = inspect_neighborhood(
            graph, "Hub", "H", depth=1, relationship_types=["makes"], state="live"
        )
        assert by_rel.edges_hidden_by_state == 1
        # Target filter drops the Widget endpoint first: only the Gadget edge
        # is hidden by state.
        by_target = inspect_neighborhood(
            graph, "Hub", "H", depth=1, target_types=["Gadget"], state="live"
        )
        assert by_target.edges_hidden_by_state == 1

    def test_count_covers_only_the_explored_frontier(self) -> None:
        """Hidden regions are not speculatively traversed: a pending edge
        behind another pending edge is invisible to the count."""
        graph = EntityGraph()
        for entity_id in ("ROOT", "A", "B"):
            graph.add_entity(EntityInstance(entity_type="Node", entity_id=entity_id))
        for from_id, to_id in (("ROOT", "A"), ("A", "B")):
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="linked",
                    from_type="Node",
                    from_id=from_id,
                    to_type="Node",
                    to_id=to_id,
                    metadata=_metadata(review="pending"),
                )
            )
        result = inspect_neighborhood(graph, "Node", "ROOT", depth=2, state="live")
        # ROOT->A is hidden at the root; A is never visited, so A->B is never
        # scanned and never counted.
        assert result.edges_hidden_by_state == 1

    def test_hidden_edges_consume_no_budget(self) -> None:
        graph = self._pending_star(3)
        graph.add_entity(EntityInstance(entity_type="Node", entity_id="LIVE-1"))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="depends_on",
                from_type="Node",
                from_id="ROOT",
                to_type="Node",
                to_id="LIVE-1",
            )
        )
        result = inspect_neighborhood(
            graph, "Node", "ROOT", depth=1, state="live", max_edges=1, max_nodes=1
        )
        assert result.edges_returned == 1
        assert result.truncated is False
        assert result.edges_hidden_by_state == 3


class TestValidationAndOptIn:
    def test_depth_beyond_hard_cap_raises_typed_error(self) -> None:
        graph = _graph([("A", "B")])
        with pytest.raises(ConfigError, match="depth must be between 1 and 4"):
            inspect_neighborhood(graph, "Node", "A", depth=5)

    def test_max_nodes_beyond_hard_cap_raises_typed_error(self) -> None:
        graph = _graph([("A", "B")])
        with pytest.raises(ConfigError, match="max_nodes must be between 1 and 500"):
            inspect_neighborhood(graph, "Node", "A", max_nodes=501)

    def test_max_edges_beyond_hard_cap_raises_typed_error(self) -> None:
        graph = _graph([("A", "B")])
        with pytest.raises(ConfigError, match="max_edges must be between 1 and 1000"):
            inspect_neighborhood(graph, "Node", "A", max_edges=1001)

    def test_unknown_state_raises_typed_error(self) -> None:
        graph = _graph([("A", "B")])
        with pytest.raises(ConfigError, match="state must be one of"):
            inspect_neighborhood(graph, "Node", "A", state="bogus")  # type: ignore[arg-type]

    def test_missing_root_returns_found_false(self) -> None:
        graph = _graph([("A", "B")])
        result = inspect_neighborhood(graph, "Node", "MISSING", depth=2)
        assert result.found is False
        assert result.nodes == []
        assert result.edges == []

    def test_neighborhood_requested_predicate(self) -> None:
        assert neighborhood_requested() is False
        assert neighborhood_requested(depth=1) is True
        assert neighborhood_requested(state="live") is True
        assert neighborhood_requested(max_nodes=10) is True
        assert neighborhood_requested(max_edges=10) is True
        assert neighborhood_requested(relationship_types=["fits"]) is True
        assert neighborhood_requested(target_types=["Vehicle"]) is True
        assert neighborhood_requested(projection=["name"]) is True
        assert neighborhood_requested(relationship_types=[], projection=[]) is False

    def test_projection_validation_without_config_is_a_no_op(self) -> None:
        validate_neighborhood_projection(None, ["anything"])
