"""Tests for the query engine."""

import pytest

from cruxible_core.config.schema import (
    ConstraintSchema,
    CoreConfig,
    EntityTypeSchema,
    NamedQuerySchema,
    PropertySchema,
    RelationshipSchema,
    TraversalStep,
)
from cruxible_core.errors import EntityNotFoundError, QueryExecutionError, QueryNotFoundError
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, RelationshipMetadata
from cruxible_core.query.engine import (
    QueryResult,
    _evaluate_constraint,
    _matches_filter,
    execute_query,
)
from cruxible_core.query.evaluate import evaluate_graph

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> CoreConfig:
    return CoreConfig(
        name="test",
        entity_types={
            "Vehicle": EntityTypeSchema(
                properties={
                    "vehicle_id": PropertySchema(type="string", primary_key=True),
                    "year": PropertySchema(type="int"),
                    "make": PropertySchema(type="string"),
                    "model": PropertySchema(type="string"),
                }
            ),
            "Part": EntityTypeSchema(
                properties={
                    "part_number": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                    "category": PropertySchema(type="string"),
                    "brand": PropertySchema(type="string", optional=True),
                }
            ),
        },
        relationships=[
            RelationshipSchema(
                name="fits",
                from_entity="Part",
                to_entity="Vehicle",
                reverse_name="fitted_parts",
                properties={
                    "verified": PropertySchema(type="bool"),
                    "confidence": PropertySchema(type="float", optional=True),
                },
            ),
            RelationshipSchema(
                name="replaces",
                from_entity="Part",
                to_entity="Part",
                properties={
                    "direction": PropertySchema(type="string"),
                    "confidence": PropertySchema(type="float"),
                },
            ),
            RelationshipSchema(
                name="suppressed_fit",
                from_entity="Part",
                to_entity="Vehicle",
            ),
            RelationshipSchema(
                name="vehicle_blocks_part",
                from_entity="Vehicle",
                to_entity="Part",
            ),
            RelationshipSchema(
                name="blocked",
                from_entity="Part",
                to_entity="Part",
            ),
        ],
        named_queries={
            "parts_for_vehicle": NamedQuerySchema(
                description="Find parts that fit a vehicle",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="incoming",
                        filter={"verified": True},
                    )
                ],
                returns="list[Part]",
            ),
            "vehicles_for_part": NamedQuerySchema(
                description="Find vehicles a part fits",
                entry_point="Part",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="outgoing",
                    )
                ],
                returns="list[Vehicle]",
            ),
            "fitted_parts_for_vehicle": NamedQuerySchema(
                description="Find parts for a vehicle using the reverse relationship alias",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(
                        relationship="fitted_parts",
                        direction="outgoing",
                        filter={"verified": True},
                    )
                ],
                returns="list[Part]",
            ),
            "replacements_for_vehicle": NamedQuerySchema(
                description="Find replacements that fit a specific vehicle",
                entry_point="Part",
                traversal=[
                    TraversalStep(
                        relationship="replaces",
                        direction="incoming",
                        filter={"direction": ["equivalent", "upgrade"]},
                    ),
                    TraversalStep(
                        relationship="fits",
                        direction="outgoing",
                        constraint="target.vehicle_id == $vehicle_id",
                    ),
                ],
                returns="list[Vehicle]",
            ),
            "parts_for_vehicle_without_suppressed": NamedQuerySchema(
                description="Find verified parts excluding suppressed fitments",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="incoming",
                        filter={"verified": True},
                        exclude_if_related=[
                            {
                                "relationship": "suppressed_fit",
                                "direction": "incoming",
                            }
                        ],
                    )
                ],
                returns="list[Part]",
            ),
            "parts_for_vehicle_without_vehicle_blocks": NamedQuerySchema(
                description="Find verified parts excluding vehicle-side blocks",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="incoming",
                        filter={"verified": True},
                        exclude_if_related=[
                            {
                                "relationship": "vehicle_blocks_part",
                                "direction": "outgoing",
                            }
                        ],
                    )
                ],
                returns="list[Part]",
            ),
            "replacements_excluding_blocked": NamedQuerySchema(
                description="Find replacements excluding blocked part pairs",
                entry_point="Part",
                traversal=[
                    TraversalStep(
                        relationship="replaces",
                        direction="incoming",
                        exclude_if_related=[
                            {"relationship": "blocked", "direction": "both"}
                        ],
                    )
                ],
                returns="list[Part]",
            ),
        },
    )


@pytest.fixture
def graph() -> EntityGraph:
    g = EntityGraph()

    # Vehicles
    g.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-CIVIC",
            properties={"vehicle_id": "V-CIVIC", "year": 2024, "make": "Honda", "model": "Civic"},
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-ACCORD",
            properties={"vehicle_id": "V-ACCORD", "year": 2024, "make": "Honda", "model": "Accord"},
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-CAMRY",
            properties={"vehicle_id": "V-CAMRY", "year": 2023, "make": "Toyota", "model": "Camry"},
        )
    )

    # Parts
    g.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1234",
            properties={
                "part_number": "BP-1234",
                "name": "Ceramic Brake Pad",
                "category": "brakes",
                "brand": "StopTech",
            },
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-5678",
            properties={
                "part_number": "BP-5678",
                "name": "Performance Rotor",
                "category": "brakes",
                "brand": "Brembo",
            },
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-9999",
            properties={
                "part_number": "BP-9999",
                "name": "Budget Brake Pad",
                "category": "brakes",
                "brand": "Generic",
            },
        )
    )

    # Fitments: BP-1234 fits CIVIC (verified) and ACCORD (unverified)
    g.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1234",
            to_type="Vehicle",
            to_id="V-CIVIC",
            properties={"verified": True, "confidence": 0.95},
        )
    )
    g.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1234",
            to_type="Vehicle",
            to_id="V-ACCORD",
            properties={"verified": False, "confidence": 0.7},
        )
    )

    # BP-5678 fits CIVIC (verified)
    g.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-5678",
            to_type="Vehicle",
            to_id="V-CIVIC",
            properties={"verified": True, "confidence": 0.9},
        )
    )

    # BP-9999 fits CAMRY (verified)
    g.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-9999",
            to_type="Vehicle",
            to_id="V-CAMRY",
            properties={"verified": True, "confidence": 0.8},
        )
    )

    # Replacements: BP-5678 replaces BP-1234 (upgrade), BP-9999 replaces BP-1234 (downgrade)
    g.add_relationship(
        RelationshipInstance(
            relationship_type="replaces",
            from_type="Part",
            from_id="BP-5678",
            to_type="Part",
            to_id="BP-1234",
            properties={"direction": "upgrade", "confidence": 0.85},
        )
    )
    g.add_relationship(
        RelationshipInstance(
            relationship_type="replaces",
            from_type="Part",
            from_id="BP-9999",
            to_type="Part",
            to_id="BP-1234",
            properties={"direction": "downgrade", "confidence": 0.6},
        )
    )

    return g


# ---------------------------------------------------------------------------
# execute_query: basic
# ---------------------------------------------------------------------------


class TestExecuteQuery:
    def test_parts_for_vehicle(self, config: CoreConfig, graph: EntityGraph):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-CIVIC"})
        assert isinstance(result, QueryResult)
        assert result.query_name == "parts_for_vehicle"
        assert result.steps_executed == 1
        part_ids = {r.entity_id for r in result.results}
        # Only verified fitments
        assert "BP-1234" in part_ids
        assert "BP-5678" in part_ids

    def test_parts_for_vehicle_filter_excludes_unverified(
        self, config: CoreConfig, graph: EntityGraph
    ):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-ACCORD"})
        part_ids = {r.entity_id for r in result.results}
        # BP-1234 fits ACCORD but is unverified
        assert len(part_ids) == 0

    def test_pending_review_relationship_is_not_traversed(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="pending")
                )
            ),
        )

        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-CIVIC"})

        assert {item.entity_id for item in result.results} == {"BP-1234"}

    def test_typed_datetime_constraint_uses_explicit_value_type(
        self, config: CoreConfig, graph: EntityGraph
    ) -> None:
        config.named_queries["published_parts_for_vehicle"] = NamedQuerySchema(
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    constraint="target.published_at on_or_before $as_of",
                    constraint_value_type="datetime",
                )
            ],
            returns="list[Part]",
        )
        graph.update_entity_properties(
            "Part",
            "BP-1234",
            {"published_at": "2026-05-17T13:00:00+01:00"},
        )
        graph.update_entity_properties(
            "Part",
            "BP-5678",
            {"published_at": "2026-05-17T13:00:01+00:00"},
        )

        result = execute_query(
            config,
            graph,
            "published_parts_for_vehicle",
            {"vehicle_id": "V-CIVIC", "as_of": "2026-05-17T12:00:00Z"},
        )

        assert [entity.entity_id for entity in result.results] == ["BP-1234"]

    def test_top_level_temporal_constraint_alias_is_evaluated(
        self, config: CoreConfig, graph: EntityGraph
    ) -> None:
        config.constraints.append(
            ConstraintSchema(
                name="published_before_until",
                rule="fits.FROM.published_at before fits.TO.available_until",
                value_type="datetime",
                severity="error",
            )
        )
        graph.update_entity_properties(
            "Part",
            "BP-1234",
            {"published_at": "2026-05-17T13:00:00Z"},
        )
        graph.update_entity_properties(
            "Part",
            "BP-5678",
            {"published_at": "2026-05-17T11:00:00Z"},
        )
        graph.update_entity_properties(
            "Vehicle",
            "V-CIVIC",
            {"available_until": "2026-05-17T12:00:00+00:00"},
        )

        report = evaluate_graph(config, graph)

        assert report.constraint_summary["published_before_until"] == 1
        assert any(
            finding.category == "constraint_violation"
            and finding.detail["constraint"] == "published_before_until"
            for finding in report.findings
        )

    def test_parts_for_vehicle_via_reverse_name(
        self, config: CoreConfig, graph: EntityGraph
    ):
        result = execute_query(
            config,
            graph,
            "fitted_parts_for_vehicle",
            {"vehicle_id": "V-CIVIC"},
        )
        part_ids = {r.entity_id for r in result.results}
        assert part_ids == {"BP-1234", "BP-5678"}

    def test_vehicles_for_part(self, config: CoreConfig, graph: EntityGraph):
        result = execute_query(config, graph, "vehicles_for_part", {"part_number": "BP-1234"})
        vehicle_ids = {r.entity_id for r in result.results}
        assert "V-CIVIC" in vehicle_ids
        assert "V-ACCORD" in vehicle_ids

    def test_no_results(self, config: CoreConfig, graph: EntityGraph):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-CAMRY"})
        # BP-9999 fits CAMRY and is verified
        assert len(result.results) == 1
        assert result.results[0].entity_id == "BP-9999"

    def test_total_results(self, config: CoreConfig, graph: EntityGraph):
        result = execute_query(config, graph, "vehicles_for_part", {"part_number": "BP-1234"})
        assert result.total_results == 2

    def test_parameters_stored(self, config: CoreConfig, graph: EntityGraph):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-CIVIC"})
        assert result.parameters == {"vehicle_id": "V-CIVIC"}

    def test_source_side_constraint_fails_query(
        self, config: CoreConfig, graph: EntityGraph
    ):
        config.named_queries["bad_source_constraint"] = NamedQuerySchema(
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    constraint="source.vehicle_id == $vehicle_id",
                )
            ],
            returns="list[Part]",
        )

        with pytest.raises(QueryExecutionError, match="source-side traversal constraints"):
            execute_query(
                config,
                graph,
                "bad_source_constraint",
                {"vehicle_id": "V-CIVIC"},
            )


# ---------------------------------------------------------------------------
# execute_query: multi-step with constraint
# ---------------------------------------------------------------------------


class TestMultiStepQuery:
    def test_replacements_for_vehicle(self, config: CoreConfig, graph: EntityGraph):
        """BP-1234 has replacements BP-5678 (upgrade) and BP-9999 (downgrade).
        Filter keeps only upgrade/equivalent. BP-5678 fits CIVIC, so it appears."""
        result = execute_query(
            config,
            graph,
            "replacements_for_vehicle",
            {"part_number": "BP-1234", "vehicle_id": "V-CIVIC"},
        )
        # Step 1: BP-1234 <- replaces incoming <- BP-5678 (upgrade), BP-9999 (downgrade filtered)
        # Step 2: BP-5678 -> fits outgoing -> V-CIVIC (constraint passes), V-ACCORD would fail
        vehicle_ids = {r.entity_id for r in result.results}
        assert "V-CIVIC" in vehicle_ids

    def test_replacement_no_match(self, config: CoreConfig, graph: EntityGraph):
        """BP-5678 has no incoming replaces edges (nobody replaces it)."""
        result = execute_query(
            config,
            graph,
            "replacements_for_vehicle",
            {"part_number": "BP-5678", "vehicle_id": "V-CIVIC"},
        )
        assert len(result.results) == 0

    def test_multi_step_result_lineage_points_to_final_step(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        result = execute_query(
            config,
            graph,
            "replacements_for_vehicle",
            {"part_number": "BP-1234", "vehicle_id": "V-CIVIC"},
        )
        assert result.receipt is not None
        receipt = result.receipt

        result_nodes = [n for n in receipt.nodes if n.node_type == "result"]
        assert len(result_nodes) == 1
        produced = [e for e in receipt.edges if e.to_node == result_nodes[0].node_id]
        assert len(produced) == 1

        parent = next(n for n in receipt.nodes if n.node_id == produced[0].from_node)
        assert parent.node_type == "edge_traversal"
        assert parent.relationship == "fits"
        assert parent.entity_type == "Vehicle"
        assert parent.entity_id == "V-CIVIC"
        assert parent.detail["from_entity_id"] == "BP-5678"


class TestRelatedEdgeExclusions:
    def test_candidate_kept_when_related_edge_does_not_exist(
        self, config: CoreConfig, graph: EntityGraph
    ):
        result = execute_query(
            config,
            graph,
            "parts_for_vehicle_without_suppressed",
            {"vehicle_id": "V-CIVIC"},
        )

        assert {item.entity_id for item in result.results} == {"BP-1234", "BP-5678"}

    def test_outgoing_related_edge_excludes_candidate(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="vehicle_blocks_part",
                from_type="Vehicle",
                from_id="V-CIVIC",
                to_type="Part",
                to_id="BP-5678",
                properties={},
            )
        )

        result = execute_query(
            config,
            graph,
            "parts_for_vehicle_without_vehicle_blocks",
            {"vehicle_id": "V-CIVIC"},
        )

        assert {item.entity_id for item in result.results} == {"BP-1234"}

    def test_incoming_related_edge_excludes_candidate(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={},
            )
        )

        result = execute_query(
            config,
            graph,
            "parts_for_vehicle_without_suppressed",
            {"vehicle_id": "V-CIVIC"},
        )

        assert {item.entity_id for item in result.results} == {"BP-5678"}

    def test_both_direction_excludes_when_outgoing_related_edge_exists(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="blocked",
                from_type="Part",
                from_id="BP-1234",
                to_type="Part",
                to_id="BP-5678",
                properties={},
            )
        )

        result = execute_query(
            config,
            graph,
            "replacements_excluding_blocked",
            {"part_number": "BP-1234"},
        )

        assert {item.entity_id for item in result.results} == {"BP-9999"}

    def test_both_direction_excludes_when_incoming_related_edge_exists(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="blocked",
                from_type="Part",
                from_id="BP-5678",
                to_type="Part",
                to_id="BP-1234",
                properties={},
            )
        )

        result = execute_query(
            config,
            graph,
            "replacements_excluding_blocked",
            {"part_number": "BP-1234"},
        )

        assert {item.entity_id for item in result.results} == {"BP-9999"}

    def test_related_edge_pending_review_does_not_exclude(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
                metadata=RelationshipMetadata(
                    assertion=RelationshipAssertion(
                        review=RelationshipReviewState(status="pending")
                    )
                ),
            )
        )

        result = execute_query(
            config,
            graph,
            "parts_for_vehicle_without_suppressed",
            {"vehicle_id": "V-CIVIC"},
        )

        assert {item.entity_id for item in result.results} == {"BP-1234", "BP-5678"}

    def test_related_edge_rejected_does_not_exclude(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
                metadata=RelationshipMetadata(
                    assertion=RelationshipAssertion(
                        review=RelationshipReviewState(status="rejected", source="human")
                    )
                ),
            )
        )

        result = execute_query(
            config,
            graph,
            "parts_for_vehicle_without_suppressed",
            {"vehicle_id": "V-CIVIC"},
        )

        assert {item.entity_id for item in result.results} == {"BP-1234", "BP-5678"}

    def test_related_edge_with_default_assertion_excludes(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-5678",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={},
            )
        )

        result = execute_query(
            config,
            graph,
            "parts_for_vehicle_without_suppressed",
            {"vehicle_id": "V-CIVIC"},
        )

        assert {item.entity_id for item in result.results} == {"BP-1234"}

    def test_related_exclusion_is_recorded_as_filter_event(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="vehicle_blocks_part",
                from_type="Vehicle",
                from_id="V-CIVIC",
                to_type="Part",
                to_id="BP-5678",
                properties={},
            )
        )

        result = execute_query(
            config,
            graph,
            "parts_for_vehicle_without_vehicle_blocks",
            {"vehicle_id": "V-CIVIC"},
        )

        assert result.receipt is not None
        filters = [
            node
            for node in result.receipt.nodes
            if node.node_type == "filter_applied"
            and "exclude_if_related" in node.detail.get("filter", {})
        ]
        assert filters
        assert {
            "filter": {
                "exclude_if_related": {
                    "relationship": "vehicle_blocks_part",
                    "direction": "outgoing",
                }
            },
            "passed": False,
        } in [node.detail for node in filters]


# ---------------------------------------------------------------------------
# execute_query: error cases
# ---------------------------------------------------------------------------


class TestQueryErrors:
    def test_query_not_found(self, config: CoreConfig, graph: EntityGraph):
        with pytest.raises(QueryNotFoundError):
            execute_query(config, graph, "nonexistent", {})

    def test_missing_param(self, config: CoreConfig, graph: EntityGraph):
        with pytest.raises(QueryExecutionError, match="Parameter 'vehicle_id' required"):
            execute_query(config, graph, "parts_for_vehicle", {})

    def test_missing_param_shows_available_keys(self, config: CoreConfig, graph: EntityGraph):
        """Error message includes the param keys the caller actually provided."""
        with pytest.raises(QueryExecutionError, match="Got params:.*person_name") as exc_info:
            execute_query(config, graph, "parts_for_vehicle", {"person_name": "Bob"})
        assert "vehicle_id" in str(exc_info.value)

    def test_entity_not_in_graph(self, config: CoreConfig, graph: EntityGraph):
        with pytest.raises(EntityNotFoundError):
            execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-MISSING"})


# ---------------------------------------------------------------------------
# _matches_filter
# ---------------------------------------------------------------------------


class TestMatchesFilter:
    def test_scalar_match(self):
        assert _matches_filter({"verified": True}, {"verified": True})

    def test_scalar_mismatch(self):
        assert not _matches_filter({"verified": False}, {"verified": True})

    def test_list_match(self):
        assert _matches_filter(
            {"direction": "upgrade"},
            {"direction": ["upgrade", "equivalent"]},
        )

    def test_list_mismatch(self):
        assert not _matches_filter(
            {"direction": "downgrade"},
            {"direction": ["upgrade", "equivalent"]},
        )

    def test_missing_property(self):
        assert not _matches_filter({}, {"verified": True})

    def test_multiple_filters_all_pass(self):
        assert _matches_filter(
            {"verified": True, "confidence": 0.9},
            {"verified": True, "confidence": 0.9},
        )

    def test_multiple_filters_one_fails(self):
        assert not _matches_filter(
            {"verified": True, "confidence": 0.5},
            {"verified": True, "confidence": 0.9},
        )

    def test_empty_filter(self):
        assert _matches_filter({"verified": True}, {})


# ---------------------------------------------------------------------------
# _evaluate_constraint
# ---------------------------------------------------------------------------


class TestEvaluateConstraint:
    def test_target_property_equals_param(self, config: CoreConfig):
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-CIVIC"},
        )
        assert _evaluate_constraint(
            config,
            "target.vehicle_id == $vehicle_id",
            entity,
            {"vehicle_id": "V-CIVIC"},
        )

    def test_target_property_not_equals_param(self, config: CoreConfig):
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-ACCORD"},
        )
        assert not _evaluate_constraint(
            config,
            "target.vehicle_id == $vehicle_id",
            entity,
            {"vehicle_id": "V-CIVIC"},
        )

    def test_not_equals_operator(self, config: CoreConfig):
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-ACCORD"},
        )
        assert _evaluate_constraint(
            config,
            "target.vehicle_id != $vehicle_id",
            entity,
            {"vehicle_id": "V-CIVIC"},
        )

    def test_literal_comparison(self, config: CoreConfig):
        entity = EntityInstance(
            entity_type="Part",
            entity_id="P-1",
            properties={"category": "brakes"},
        )
        assert _evaluate_constraint(
            config,
            "target.category == brakes",
            entity,
            {},
        )

    def test_numeric_literal(self, config: CoreConfig):
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"year": 2024},
        )
        assert _evaluate_constraint(
            config,
            "target.year == 2024",
            entity,
            {},
        )

    def test_ordered_numeric_literal(self, config: CoreConfig):
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"year": 2024},
        )
        assert _evaluate_constraint(
            config,
            "target.year >= 2024",
            entity,
            {},
        )

    def test_ordered_param_comparison(self, config: CoreConfig):
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"year": 2024},
        )
        assert _evaluate_constraint(
            config,
            "target.year > $min_year",
            entity,
            {"min_year": 2023},
        )

    def test_date_constraint_uses_explicit_value_type(self, config: CoreConfig) -> None:
        entity = EntityInstance(
            entity_type="Part",
            entity_id="P-1",
            properties={"available_on": "2026-05-17T23:00:00-02:00"},
        )

        assert _evaluate_constraint(
            config,
            "target.available_on == $as_of",
            entity,
            {"as_of": "2026-05-18"},
            value_type="date",
        )

    def test_missing_property_returns_false(self, config: CoreConfig):
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={},
        )
        assert not _evaluate_constraint(
            config,
            "target.vehicle_id == $vehicle_id",
            entity,
            {"vehicle_id": "V-CIVIC"},
        )

    def test_missing_param_returns_false(self, config: CoreConfig):
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-CIVIC"},
        )
        assert not _evaluate_constraint(
            config,
            "target.vehicle_id == $missing_param",
            entity,
            {},
        )

    def test_unknown_format_passes(self, config: CoreConfig):
        """Unknown constraint formats are permissive (don't filter)."""
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={},
        )
        assert _evaluate_constraint(config, "some_weird_expression", entity, {})

    def test_source_side_constraint_raises(self, config: CoreConfig):
        """source.X constraints fail closed instead of being recorded as passed."""
        entity = EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-CIVIC"},
        )
        with pytest.raises(QueryExecutionError, match="source-side traversal constraints"):
            _evaluate_constraint(
                config,
                "source.vehicle_id == $vehicle_id",
                entity,
                {"vehicle_id": "V-CIVIC"},
            )


# ---------------------------------------------------------------------------
# Multi-relationship fan-out
# ---------------------------------------------------------------------------


def _fan_out_config() -> CoreConfig:
    """Config with two relationship types from the same entity pair."""
    return CoreConfig(
        name="fan_out_test",
        entity_types={
            "Org": EntityTypeSchema(
                properties={"org_id": PropertySchema(type="string", primary_key=True)}
            ),
            "Person": EntityTypeSchema(
                properties={"person_id": PropertySchema(type="string", primary_key=True)}
            ),
            "ParentOrg": EntityTypeSchema(
                properties={"org_id": PropertySchema(type="string", primary_key=True)}
            ),
        },
        relationships=[
            RelationshipSchema(
                name="owns",
                from_entity="Person",
                to_entity="Org",
                properties={"stake": PropertySchema(type="float", optional=True)},
            ),
            RelationshipSchema(
                name="owns_org",
                from_entity="ParentOrg",
                to_entity="Org",
                properties={"stake": PropertySchema(type="float", optional=True)},
            ),
        ],
        named_queries={
            "screen_org": NamedQuerySchema(
                entry_point="Org",
                traversal=[
                    TraversalStep(
                        relationship=["owns", "owns_org"],
                        direction="incoming",
                    )
                ],
                returns="list",
            ),
            "screen_org_filtered": NamedQuerySchema(
                entry_point="Org",
                traversal=[
                    TraversalStep(
                        relationship=["owns", "owns_org"],
                        direction="incoming",
                        filter={"stake": 0.5},
                    )
                ],
                returns="list",
            ),
            "screen_org_constrained": NamedQuerySchema(
                entry_point="Org",
                traversal=[
                    TraversalStep(
                        relationship=["owns", "owns_org"],
                        direction="incoming",
                        constraint="target.person_id != $exclude_id",
                    )
                ],
                returns="list",
            ),
            "screen_org_single": NamedQuerySchema(
                entry_point="Org",
                traversal=[
                    TraversalStep(
                        relationship="owns",
                        direction="incoming",
                    )
                ],
                returns="list",
            ),
        },
    )


def _fan_out_graph() -> EntityGraph:
    g = EntityGraph()
    g.add_entity(
        EntityInstance(entity_type="Org", entity_id="ORG-1", properties={"org_id": "ORG-1"})
    )
    g.add_entity(
        EntityInstance(entity_type="Person", entity_id="P-1", properties={"person_id": "P-1"})
    )
    g.add_entity(
        EntityInstance(entity_type="Person", entity_id="P-2", properties={"person_id": "P-2"})
    )
    g.add_entity(
        EntityInstance(
            entity_type="ParentOrg", entity_id="PARENT-1", properties={"org_id": "PARENT-1"}
        )
    )

    # P-1 owns ORG-1
    g.add_relationship(
        RelationshipInstance(
            relationship_type="owns",
            from_type="Person",
            from_id="P-1",
            to_type="Org",
            to_id="ORG-1",
            properties={"stake": 0.5},
        )
    )
    # P-2 owns ORG-1
    g.add_relationship(
        RelationshipInstance(
            relationship_type="owns",
            from_type="Person",
            from_id="P-2",
            to_type="Org",
            to_id="ORG-1",
            properties={"stake": 0.3},
        )
    )
    # PARENT-1 owns_org ORG-1
    g.add_relationship(
        RelationshipInstance(
            relationship_type="owns_org",
            from_type="ParentOrg",
            from_id="PARENT-1",
            to_type="Org",
            to_id="ORG-1",
            properties={"stake": 0.5},
        )
    )
    return g


class TestMultiRelationshipStep:
    def test_fan_out_single_step(self):
        """Two relationship types traversed, results merged."""
        config = _fan_out_config()
        graph = _fan_out_graph()
        result = execute_query(config, graph, "screen_org", {"org_id": "ORG-1"})
        ids = {r.entity_id for r in result.results}
        assert ids == {"P-1", "P-2", "PARENT-1"}

    def test_fan_out_deduplication(self):
        """Same entity reachable via both rels appears once in results.

        Uses the depth config where links and alt_links both connect Node->Node,
        so the same node can be reached via two different relationship types.
        """
        config = _depth_config()
        graph = EntityGraph()
        for nid in ["A", "B"]:
            graph.add_entity(
                EntityInstance(entity_type="Node", entity_id=nid, properties={"node_id": nid})
            )
        # A -> B via links
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="links",
                from_type="Node",
                from_id="A",
                to_type="Node",
                to_id="B",
                properties={"weight": 1.0},
            )
        )
        # A -> B via alt_links
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="alt_links",
                from_type="Node",
                from_id="A",
                to_type="Node",
                to_id="B",
                properties={"weight": 1.0},
            )
        )
        result = execute_query(config, graph, "fan_out_depth_2", {"node_id": "A"})
        all_ids = [r.entity_id for r in result.results]
        assert all_ids.count("B") == 1

    def test_fan_out_receipt_records_both(self):
        """Receipt has traversal edges from both relationship types."""
        config = _fan_out_config()
        graph = _fan_out_graph()
        result = execute_query(config, graph, "screen_org", {"org_id": "ORG-1"})
        assert result.receipt is not None
        traversal_nodes = [n for n in result.receipt.nodes if n.node_type == "edge_traversal"]
        rel_types = {n.relationship for n in traversal_nodes}
        assert "owns" in rel_types
        assert "owns_org" in rel_types

    def test_fan_out_with_filter(self):
        """Filter applies to edges from all relationship types."""
        config = _fan_out_config()
        graph = _fan_out_graph()
        result = execute_query(config, graph, "screen_org_filtered", {"org_id": "ORG-1"})
        ids = {r.entity_id for r in result.results}
        # Only stake=0.5 edges pass: P-1 (owns, 0.5) and PARENT-1 (owns_org, 0.5)
        assert ids == {"P-1", "PARENT-1"}

    def test_fan_out_with_constraint(self):
        """Constraint applies to neighbors from all relationship types."""
        config = _fan_out_config()
        graph = _fan_out_graph()
        result = execute_query(
            config, graph, "screen_org_constrained", {"org_id": "ORG-1", "exclude_id": "P-1"}
        )
        ids = {r.entity_id for r in result.results}
        # P-1 excluded by constraint; P-2 and PARENT-1 pass
        # Note: PARENT-1 doesn't have person_id so constraint target.person_id != $exclude_id
        # evaluates to None != "P-1" which is True
        assert "P-2" in ids
        assert "P-1" not in ids

    def test_single_relationship_backward_compatible(self):
        """Single string relationship still works as before."""
        config = _fan_out_config()
        graph = _fan_out_graph()
        result = execute_query(config, graph, "screen_org_single", {"org_id": "ORG-1"})
        ids = {r.entity_id for r in result.results}
        assert ids == {"P-1", "P-2"}


# ---------------------------------------------------------------------------
# max_depth BFS
# ---------------------------------------------------------------------------


def _depth_config() -> CoreConfig:
    """Config with a chain of 'links' relationships for depth testing."""
    return CoreConfig(
        name="depth_test",
        entity_types={
            "Node": EntityTypeSchema(
                properties={"node_id": PropertySchema(type="string", primary_key=True)}
            ),
        },
        relationships=[
            RelationshipSchema(
                name="links",
                from_entity="Node",
                to_entity="Node",
                properties={"weight": PropertySchema(type="float", optional=True)},
            ),
            RelationshipSchema(
                name="alt_links",
                from_entity="Node",
                to_entity="Node",
                properties={"weight": PropertySchema(type="float", optional=True)},
            ),
        ],
        named_queries={
            "depth_1": NamedQuerySchema(
                entry_point="Node",
                traversal=[TraversalStep(relationship="links", direction="outgoing", max_depth=1)],
                returns="list[Node]",
            ),
            "depth_2": NamedQuerySchema(
                entry_point="Node",
                traversal=[TraversalStep(relationship="links", direction="outgoing", max_depth=2)],
                returns="list[Node]",
            ),
            "depth_3": NamedQuerySchema(
                entry_point="Node",
                traversal=[TraversalStep(relationship="links", direction="outgoing", max_depth=3)],
                returns="list[Node]",
            ),
            "depth_2_filtered": NamedQuerySchema(
                entry_point="Node",
                traversal=[
                    TraversalStep(
                        relationship="links",
                        direction="outgoing",
                        max_depth=2,
                        filter={"weight": 1.0},
                    )
                ],
                returns="list[Node]",
            ),
            "fan_out_depth_2": NamedQuerySchema(
                entry_point="Node",
                traversal=[
                    TraversalStep(
                        relationship=["links", "alt_links"],
                        direction="outgoing",
                        max_depth=2,
                    )
                ],
                returns="list[Node]",
            ),
        },
    )


def _chain_graph() -> EntityGraph:
    """A -> B -> C -> D linear chain via 'links'."""
    g = EntityGraph()
    for nid in ["A", "B", "C", "D"]:
        g.add_entity(EntityInstance(entity_type="Node", entity_id=nid, properties={"node_id": nid}))
    for src, dst in [("A", "B"), ("B", "C"), ("C", "D")]:
        g.add_relationship(
            RelationshipInstance(
                relationship_type="links",
                from_type="Node",
                from_id=src,
                to_type="Node",
                to_id=dst,
                properties={"weight": 1.0},
            )
        )
    return g


class TestMaxDepth:
    def test_max_depth_2(self):
        """Depth 2 from A reaches B and C."""
        config = _depth_config()
        graph = _chain_graph()
        result = execute_query(config, graph, "depth_2", {"node_id": "A"})
        ids = {r.entity_id for r in result.results}
        assert ids == {"B", "C"}

    def test_max_depth_1_default(self):
        """Depth 1 (default) only gets direct neighbors."""
        config = _depth_config()
        graph = _chain_graph()
        result = execute_query(config, graph, "depth_1", {"node_id": "A"})
        ids = {r.entity_id for r in result.results}
        assert ids == {"B"}

    def test_max_depth_with_fan_out(self):
        """Multi-relationship + max_depth 2 — BFS across both rel types."""
        config = _depth_config()
        graph = _chain_graph()
        # Add alt_links: A -> C (shortcut)
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="alt_links",
                from_type="Node",
                from_id="A",
                to_type="Node",
                to_id="C",
                properties={"weight": 1.0},
            )
        )
        result = execute_query(config, graph, "fan_out_depth_2", {"node_id": "A"})
        ids = {r.entity_id for r in result.results}
        # links d1: B, links d2: C, alt_links d1: C (dedup), alt_links d2 from C: D
        assert ids == {"B", "C", "D"}

    def test_max_depth_cycle_detection(self):
        """Circular relationships don't infinite loop."""
        config = _depth_config()
        graph = _chain_graph()
        # Add cycle: D -> A
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="links",
                from_type="Node",
                from_id="D",
                to_type="Node",
                to_id="A",
                properties={"weight": 1.0},
            )
        )
        result = execute_query(config, graph, "depth_3", {"node_id": "A"})
        ids = {r.entity_id for r in result.results}
        # A is the entry point (seen_expanded), so the cycle back to A won't add it to results
        assert ids == {"B", "C", "D"}

    def test_cycle_excludes_entry_entity(self):
        """Entry entity must not appear in results even with max_depth >= cycle length."""
        config = _depth_config()
        graph = _chain_graph()
        # Add cycle: D -> A
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="links",
                from_type="Node",
                from_id="D",
                to_type="Node",
                to_id="A",
                properties={"weight": 1.0},
            )
        )
        # max_depth=4 exceeds cycle length — A must still not appear
        config.named_queries["depth_4"] = NamedQuerySchema(
            entry_point="Node",
            traversal=[
                TraversalStep(
                    relationship="links",
                    direction="outgoing",
                    max_depth=4,
                )
            ],
            returns="list[Node]",
        )
        result = execute_query(config, graph, "depth_4", {"node_id": "A"})
        ids = {r.entity_id for r in result.results}
        assert "A" not in ids
        assert ids == {"B", "C", "D"}

    def test_max_depth_receipt_chain(self):
        """Each hop's receipt node has previous hop as parent (not root)."""
        config = _depth_config()
        graph = _chain_graph()
        result = execute_query(config, graph, "depth_2", {"node_id": "A"})
        assert result.receipt is not None
        receipt = result.receipt

        traversal_nodes = [n for n in receipt.nodes if n.node_type == "edge_traversal"]
        assert len(traversal_nodes) == 2  # A->B and B->C

        # Find the B->C traversal (to_id=C)
        hop2 = next(n for n in traversal_nodes if n.entity_id == "C")
        # Its parent edge should point to the A->B traversal, not root
        parent_edges = [e for e in receipt.edges if e.to_node == hop2.node_id]
        assert len(parent_edges) == 1
        parent_node_id = parent_edges[0].from_node
        parent_node = next(n for n in receipt.nodes if n.node_id == parent_node_id)
        assert parent_node.node_type == "edge_traversal"
        assert parent_node.entity_id == "B"

    def test_max_depth_filter_blocks_subtree(self):
        """Rejected edge at depth 1 prevents depth 2+ traversal."""
        config = _depth_config()
        # Build custom graph: A->B (weight=1.0), B->C (weight=0.0), C->D (weight=1.0)
        graph = EntityGraph()
        for nid in ["A", "B", "C", "D"]:
            graph.add_entity(
                EntityInstance(entity_type="Node", entity_id=nid, properties={"node_id": nid})
            )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="links",
                from_type="Node",
                from_id="A",
                to_type="Node",
                to_id="B",
                properties={"weight": 1.0},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="links",
                from_type="Node",
                from_id="B",
                to_type="Node",
                to_id="C",
                properties={"weight": 0.0},  # won't match filter={weight: 1.0}
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="links",
                from_type="Node",
                from_id="C",
                to_type="Node",
                to_id="D",
                properties={"weight": 1.0},
            )
        )
        result = execute_query(config, graph, "depth_2_filtered", {"node_id": "A"})
        ids = {r.entity_id for r in result.results}
        # A->B passes (weight=1.0), B->C blocked (weight=0.0), so only B
        assert ids == {"B"}
