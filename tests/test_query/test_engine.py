"""Tests for the query engine."""

from datetime import datetime, timedelta, timezone

import pytest

import cruxible_core.query.engine as query_engine
from cruxible_core.config.schema import (
    ConstraintSchema,
    CoreConfig,
    DecisionPolicySchema,
    EntityTypeSchema,
    EnumSchema,
    NamedQuerySchema,
    PropertySchema,
    RelationshipSchema,
    TraversalStep,
)
from cruxible_core.errors import EntityNotFoundError, QueryExecutionError, QueryNotFoundError
from cruxible_core.feedback.applier import apply_feedback
from cruxible_core.feedback.types import FeedbackRecord
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, RelationshipMetadata
from cruxible_core.query.engine import (
    QueryResult,
    _evaluate_constraint,
    _matches_filter,
    execute_query,
)
from cruxible_core.query.evaluate import evaluate_graph
from cruxible_core.query.predicates import build_predicate_context
from cruxible_core.query.types import (
    ProjectedQueryRow,
    QueryPathRow,
    QueryPathSegment,
    QueryRelationshipRow,
    dump_query_row,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _metadata(
    *,
    review_status: str = "unreviewed",
    lifecycle_status: str = "active",
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> RelationshipMetadata:
    return RelationshipMetadata(
        assertion=RelationshipAssertion(
            review=RelationshipReviewState(status=review_status),
            lifecycle=RelationshipLifecycleState(
                status=lifecycle_status,
                effective_from=effective_from,
                effective_until=effective_until,
            ),
        )
    )


def _terminal_ids(rows: list[object]) -> list[str]:
    ids: list[str] = []
    for row in rows:
        if isinstance(row, QueryPathRow):
            ids.append(row.result.entity_id)
        elif isinstance(row, EntityInstance):
            ids.append(row.entity_id)
        elif isinstance(row, QueryRelationshipRow):
            ids.append(row.to_id)
    return ids


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
                mode="traversal",
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
                result_shape="entity",
            ),
            "vehicles_for_part": NamedQuerySchema(
                mode="traversal",
                description="Find vehicles a part fits",
                entry_point="Part",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="outgoing",
                    )
                ],
                returns="list[Vehicle]",
                result_shape="entity",
            ),
            "fitted_parts_for_vehicle": NamedQuerySchema(
                mode="traversal",
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
                result_shape="entity",
            ),
            "replacements_for_vehicle": NamedQuerySchema(
                mode="traversal",
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
                result_shape="entity",
            ),
            "parts_for_vehicle_without_suppressed": NamedQuerySchema(
                mode="traversal",
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
                result_shape="entity",
            ),
            "parts_for_vehicle_without_vehicle_blocks": NamedQuerySchema(
                mode="traversal",
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
                result_shape="entity",
            ),
            "replacements_excluding_blocked": NamedQuerySchema(
                mode="traversal",
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
                result_shape="entity",
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
    def test_entryless_entity_collection_returns_entities_in_deterministic_order(
        self, config: CoreConfig, graph: EntityGraph
    ):
        config.named_queries["all_parts"] = NamedQuerySchema(
            mode="collection",
            result_shape="entity",
            returns="Part",
        )

        result = execute_query(config, graph, "all_parts", {})

        assert [row.entity_id for row in result.results if isinstance(row, EntityInstance)] == [
            "BP-1234",
            "BP-5678",
            "BP-9999",
        ]
        assert result.steps_executed == 0
        assert result.total_results == 3

    def test_entryless_entity_collection_filters_result_entity(
        self, config: CoreConfig, graph: EntityGraph
    ):
        config.named_queries["stoptech_parts"] = NamedQuerySchema(
            mode="collection",
            result_shape="entity",
            returns="Part",
            where={"result.properties.brand": {"eq": "StopTech"}},
        )

        result = execute_query(config, graph, "stoptech_parts", {})

        assert _terminal_ids(result.results) == ["BP-1234"]

    def test_entryless_entity_collection_uses_entity_id_lookup_before_scan(
        self, config: CoreConfig, graph: EntityGraph
    ):
        class RecordingGraph(EntityGraph):
            def __init__(self, source: EntityGraph) -> None:
                super().__init__()
                self._graph = source._graph
                self._entities_by_type = source._entities_by_type
                self._edge_counter = source._edge_counter
                self.list_entity_calls: list[tuple[str, dict[str, object] | None]] = []

            def list_entities(
                self,
                entity_type: str,
                property_filter: dict[str, object] | None = None,
            ) -> list[EntityInstance]:
                self.list_entity_calls.append((entity_type, property_filter))
                return super().list_entities(entity_type, property_filter=property_filter)

        recording_graph = RecordingGraph(graph)
        config.named_queries["one_part"] = NamedQuerySchema(
            mode="collection",
            result_shape="entity",
            returns="Part",
            where={"result.entity_id": {"eq": "$input.part_id"}},
        )

        result = execute_query(
            config,
            recording_graph,
            "one_part",
            {"part_id": "BP-5678"},
        )

        assert _terminal_ids(result.results) == ["BP-5678"]
        assert recording_graph.list_entity_calls == []

    def test_entryless_entity_collection_pushes_simple_property_filter(
        self, config: CoreConfig, graph: EntityGraph
    ):
        class RecordingGraph(EntityGraph):
            def __init__(self, source: EntityGraph) -> None:
                super().__init__()
                self._graph = source._graph
                self._entities_by_type = source._entities_by_type
                self._edge_counter = source._edge_counter
                self.list_entity_calls: list[tuple[str, dict[str, object] | None]] = []

            def list_entities(
                self,
                entity_type: str,
                property_filter: dict[str, object] | None = None,
            ) -> list[EntityInstance]:
                self.list_entity_calls.append((entity_type, property_filter))
                return super().list_entities(entity_type, property_filter=property_filter)

        recording_graph = RecordingGraph(graph)
        config.named_queries["stoptech_parts"] = NamedQuerySchema(
            mode="collection",
            result_shape="entity",
            returns="Part",
            where={"result.properties.brand": {"eq": "$input.brand"}},
        )

        result = execute_query(
            config,
            recording_graph,
            "stoptech_parts",
            {"brand": "StopTech"},
        )

        assert _terminal_ids(result.results) == ["BP-1234"]
        assert recording_graph.list_entity_calls == [
            ("Part", {"brand": "StopTech"})
        ]

    def test_entryless_relationship_collection_returns_relationships_in_order(
        self, config: CoreConfig, graph: EntityGraph
    ):
        config.named_queries["all_fitments"] = NamedQuerySchema(
            mode="collection",
            result_shape="relationship",
            returns="fits",
        )

        result = execute_query(config, graph, "all_fitments", {})

        assert [
            (row.from_id, row.to_id)
            for row in result.results
            if isinstance(row, QueryRelationshipRow)
        ] == [
            ("BP-1234", "V-ACCORD"),
            ("BP-1234", "V-CIVIC"),
            ("BP-5678", "V-CIVIC"),
            ("BP-9999", "V-CAMRY"),
        ]

    def test_entryless_relationship_collection_filters_edge_and_endpoints(
        self, config: CoreConfig, graph: EntityGraph
    ):
        config.named_queries["honda_verified_fitments"] = NamedQuerySchema(
            mode="collection",
            result_shape="relationship",
            returns="fits",
            where={
                "edge.properties.verified": {"eq": True},
                "target.properties.make": {"eq": "Honda"},
            },
        )

        result = execute_query(config, graph, "honda_verified_fitments", {})

        assert [
            (row.from_id, row.to_id)
            for row in result.results
            if isinstance(row, QueryRelationshipRow)
        ] == [("BP-1234", "V-CIVIC"), ("BP-5678", "V-CIVIC")]

    def test_entryless_relationship_collection_respects_relationship_state(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.update_relationship_state(
            "Part",
            "BP-1234",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=_metadata(review_status="approved"),
        )
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=_metadata(review_status="pending"),
        )
        config.named_queries["accepted_fitments"] = NamedQuerySchema(
            mode="collection",
            result_shape="relationship",
            returns="fits",
            relationship_state="accepted",
        )
        config.named_queries["pending_fitments"] = NamedQuerySchema(
            mode="collection",
            result_shape="relationship",
            returns="fits",
            relationship_state="pending",
        )
        config.named_queries["reviewable_fitments"] = NamedQuerySchema(
            mode="collection",
            result_shape="relationship",
            returns="fits",
            relationship_state="reviewable",
        )

        accepted = execute_query(config, graph, "accepted_fitments", {})
        pending = execute_query(config, graph, "pending_fitments", {})
        reviewable = execute_query(config, graph, "reviewable_fitments", {})

        assert [(row.from_id, row.to_id) for row in accepted.results] == [
            ("BP-1234", "V-CIVIC")
        ]
        assert [(row.from_id, row.to_id) for row in pending.results] == [
            ("BP-5678", "V-CIVIC")
        ]
        assert ("BP-5678", "V-CIVIC") in [
            (row.from_id, row.to_id) for row in reviewable.results
        ]

    def test_entryless_relationship_collection_reports_suppression_policy_summary(
        self, config: CoreConfig, graph: EntityGraph
    ):
        config.named_queries["all_fitments_without_stoptech"] = NamedQuerySchema(
            mode="collection",
            result_shape="relationship",
            returns="fits",
        )
        config.decision_policies.append(
            DecisionPolicySchema(
                name="suppress_stoptech_fitments",
                applies_to="query",
                query_name="all_fitments_without_stoptech",
                relationship_type="fits",
                effect="suppress",
                match={"from": {"brand": "StopTech"}},
            )
        )

        result = execute_query(config, graph, "all_fitments_without_stoptech", {})

        assert [
            (row.from_id, row.to_id)
            for row in result.results
            if isinstance(row, QueryRelationshipRow)
        ] == [
            ("BP-5678", "V-CIVIC"),
            ("BP-9999", "V-CAMRY"),
        ]
        assert result.policy_summary == {"suppress_stoptech_fitments": 2}
        assert result.receipt is not None
        assert any(
            node.detail.get("policy_summary") == {"suppress_stoptech_fitments": 2}
            for node in result.receipt.nodes
        )

    def test_entryless_path_shape_and_traversal_are_rejected(self):
        with pytest.raises(ValueError, match="mode 'collection' queries do not support"):
            NamedQuerySchema(mode="collection", result_shape="path", returns="Part")
        with pytest.raises(ValueError, match="mode 'collection' queries must not define"):
            NamedQuerySchema(
                mode="collection",
                result_shape="entity",
                returns="Part",
                traversal=[TraversalStep(relationship="fits")],
            )

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
            mode="traversal",
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

        assert _terminal_ids(result.results) == ["BP-1234"]

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
            mode="traversal",
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

    def test_entity_query_enforces_matching_returns(self, config, graph):
        config.named_queries["parts_as_part"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="Part",
            result_shape="entity",
        )

        result = execute_query(config, graph, "parts_as_part", {"vehicle_id": "V-CIVIC"})

        assert {row.entity_type for row in result.results} == {"Part"}

    def test_path_query_enforces_matching_returns(self, config, graph):
        config.named_queries["parts_path_as_part"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
        )

        result = execute_query(
            config,
            graph,
            "parts_path_as_part",
            {"vehicle_id": "V-CIVIC"},
        )

        result_types = {
            row.result.entity_type
            for row in result.results
            if isinstance(row, QueryPathRow)
        }
        assert result_types == {"Part"}

    def test_entity_query_rejects_wrong_declared_returns(self, config, graph):
        config.named_queries["parts_declared_as_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="Vehicle",
            result_shape="entity",
        )

        with pytest.raises(
            QueryExecutionError,
            match=(
                "Named query 'parts_declared_as_vehicle' returned "
                "Part:BP-1234 but declares result entity type 'Vehicle'"
            ),
        ):
            execute_query(
                config,
                graph,
                "parts_declared_as_vehicle",
                {"vehicle_id": "V-CIVIC"},
            )

    def test_path_query_rejects_wrong_declared_returns(self, config, graph):
        config.named_queries["parts_path_declared_as_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="Vehicle",
            result_shape="path",
        )

        with pytest.raises(
            QueryExecutionError,
            match="parts_path_declared_as_vehicle.*Part:BP-1234.*Vehicle",
        ):
            execute_query(
                config,
                graph,
                "parts_path_declared_as_vehicle",
                {"vehicle_id": "V-CIVIC"},
            )

    def test_optional_continuation_rejects_different_result_type(self, config, graph):
        config.named_queries["optional_fit_continues_to_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                    alias="fit",
                ),
                TraversalStep(
                    relationship="fits",
                    direction="outgoing",
                    required=False,
                    alias="other_vehicle",
                ),
            ],
            returns="list[Part]",
            result_shape="path",
        )

        with pytest.raises(
            QueryExecutionError,
            match="optional_fit_continues_to_vehicle.*Vehicle:V-ACCORD.*Part",
        ):
            execute_query(
                config,
                graph,
                "optional_fit_continues_to_vehicle",
                {"vehicle_id": "V-CIVIC"},
            )

    def test_unknown_entity_returns_type_fails_before_traversal(self, config, graph):
        config.named_queries["unknown_returns_even_with_no_rows"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": "never"},
                )
            ],
            returns="list[MissingEntity]",
            result_shape="entity",
        )

        with pytest.raises(
            QueryExecutionError,
            match="unknown_returns_even_with_no_rows.*list\\[MissingEntity\\]",
        ):
            execute_query(
                config,
                graph,
                "unknown_returns_even_with_no_rows",
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

    def test_traversal_where_can_reference_prior_path_alias(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        config.named_queries["path_ref_in_traversal_where"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Part",
            traversal=[
                TraversalStep(
                    relationship="replaces",
                    direction="incoming",
                    filter={"direction": ["equivalent", "upgrade"]},
                    alias="replacement",
                ),
                TraversalStep(
                    relationship="fits",
                    direction="outgoing",
                    where={
                        "current.entity_id": {
                            "eq": "$path.replacement.source.entity_id"
                        }
                    },
                ),
            ],
            returns="list[Vehicle]",
            result_shape="path",
            dedupe="path",
        )

        result = execute_query(
            config,
            graph,
            "path_ref_in_traversal_where",
            {"part_number": "BP-1234"},
        )

        assert [row.result.entity_id for row in result.results] == ["V-CIVIC"]

    def test_traversal_where_rejects_unknown_path_alias(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        config.named_queries["unknown_path_ref_in_traversal_where"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Part",
            traversal=[
                TraversalStep(
                    relationship="replaces",
                    direction="incoming",
                    alias="replacement",
                ),
                TraversalStep(
                    relationship="fits",
                    direction="outgoing",
                    where={
                        "current.entity_id": {
                            "eq": "$path.missing.source.entity_id"
                        }
                    },
                ),
            ],
            returns="list[Vehicle]",
            result_shape="path",
            dedupe="path",
        )

        with pytest.raises(QueryExecutionError, match="Unknown path alias 'missing'"):
            execute_query(
                config,
                graph,
                "unknown_path_ref_in_traversal_where",
                {"part_number": "BP-1234"},
            )

    def test_absent_optional_path_alias_ref_makes_predicate_fail(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        config.named_queries["missing_optional_path_ref_in_traversal_where"] = (
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(
                        relationship="blocked",
                        direction="outgoing",
                        required=False,
                        alias="optional_block",
                    ),
                    TraversalStep(
                        relationship="fits",
                        direction="incoming",
                        where={
                            "candidate.entity_id": {
                                "eq": "$path.optional_block.target.entity_id"
                            }
                        },
                    ),
                ],
                returns="list[Part]",
                result_shape="path",
                dedupe="path",
            )
        )

        result = execute_query(
            config,
            graph,
            "missing_optional_path_ref_in_traversal_where",
            {"vehicle_id": "V-CIVIC"},
        )

        assert result.results == []

    def test_traversal_related_predicate_can_reference_prior_path_alias(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="vehicle_blocks_part",
                from_type="Vehicle",
                from_id="V-CIVIC",
                to_type="Part",
                to_id="BP-5678",
            )
        )
        config.named_queries["path_ref_in_traversal_related"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Part",
            traversal=[
                TraversalStep(
                    relationship="replaces",
                    direction="incoming",
                    filter={"direction": ["equivalent", "upgrade"]},
                    alias="replacement",
                ),
                TraversalStep(
                    relationship="fits",
                    direction="outgoing",
                    where_related=[
                        {
                            "relationship": "vehicle_blocks_part",
                            "direction": "outgoing",
                            "target": {
                                "entity_id": {
                                    "eq": "$path.replacement.source.entity_id"
                                }
                            },
                        }
                    ],
                ),
            ],
            returns="list[Vehicle]",
            result_shape="path",
            dedupe="path",
        )

        result = execute_query(
            config,
            graph,
            "path_ref_in_traversal_related",
            {"part_number": "BP-1234"},
        )

        assert [row.result.entity_id for row in result.results] == ["V-CIVIC"]


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

        assert set(_terminal_ids(result.results)) == {"BP-1234", "BP-5678"}

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

        assert set(_terminal_ids(result.results)) == {"BP-1234", "BP-5678"}

    def test_related_exclusion_uses_accepted_query_state(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.update_relationship_state(
            "Part",
            "BP-1234",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=_metadata(review_status="approved"),
        )
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=_metadata(review_status="approved"),
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
            )
        )
        config.named_queries["accepted_without_suppressed"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    exclude_if_related=[
                        {"relationship": "suppressed_fit", "direction": "incoming"}
                    ],
                )
            ],
            returns="list[Part]",
            relationship_state="accepted",
        )

        result = execute_query(
            config,
            graph,
            "accepted_without_suppressed",
            {"vehicle_id": "V-CIVIC"},
        )

        assert set(_terminal_ids(result.results)) == {"BP-1234", "BP-5678"}

    def test_related_exclusion_uses_pending_query_state(
        self, config: CoreConfig, graph: EntityGraph
    ):
        graph.update_relationship_state(
            "Part",
            "BP-1234",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=_metadata(review_status="pending"),
        )
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=_metadata(review_status="pending"),
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
                metadata=_metadata(review_status="pending"),
            )
        )
        config.named_queries["pending_without_suppressed"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    exclude_if_related=[
                        {"relationship": "suppressed_fit", "direction": "incoming"}
                    ],
                )
            ],
            returns="list[Part]",
            relationship_state="pending",
        )

        result = execute_query(
            config,
            graph,
            "pending_without_suppressed",
            {"vehicle_id": "V-CIVIC"},
        )

        assert _terminal_ids(result.results) == ["BP-5678"]

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
                from_entity="Person",
                to_entity="Org",
                properties={"stake": PropertySchema(type="float", optional=True)},
            ),
        ],
        named_queries={
            "screen_org": NamedQuerySchema(
                mode="traversal",
                entry_point="Org",
                traversal=[
                    TraversalStep(
                        relationship=["owns", "owns_org"],
                        direction="incoming",
                    )
                ],
                returns="list[Person]",
                result_shape="entity",
            ),
            "screen_org_filtered": NamedQuerySchema(
                mode="traversal",
                entry_point="Org",
                traversal=[
                    TraversalStep(
                        relationship=["owns", "owns_org"],
                        direction="incoming",
                        filter={"stake": 0.5},
                    )
                ],
                returns="list[Person]",
                result_shape="entity",
            ),
            "screen_org_constrained": NamedQuerySchema(
                mode="traversal",
                entry_point="Org",
                traversal=[
                    TraversalStep(
                        relationship=["owns", "owns_org"],
                        direction="incoming",
                        constraint="target.person_id != $exclude_id",
                    )
                ],
                returns="list[Person]",
                result_shape="entity",
            ),
            "screen_org_single": NamedQuerySchema(
                mode="traversal",
                entry_point="Org",
                traversal=[
                    TraversalStep(
                        relationship="owns",
                        direction="incoming",
                    )
                ],
                returns="list[Person]",
                result_shape="entity",
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
            entity_type="Person",
            entity_id="PARENT-1",
            properties={"person_id": "PARENT-1"},
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
            from_type="Person",
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
        ids = set(_terminal_ids(result.results))
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
        ids = set(_terminal_ids(result.results))
        # Only stake=0.5 edges pass: P-1 (owns, 0.5) and PARENT-1 (owns_org, 0.5)
        assert ids == {"P-1", "PARENT-1"}

    def test_fan_out_with_constraint(self):
        """Constraint applies to neighbors from all relationship types."""
        config = _fan_out_config()
        graph = _fan_out_graph()
        result = execute_query(
            config, graph, "screen_org_constrained", {"org_id": "ORG-1", "exclude_id": "P-1"}
        )
        ids = set(_terminal_ids(result.results))
        # P-1 excluded by constraint; P-2 and PARENT-1 pass
        assert "P-2" in ids
        assert "P-1" not in ids

    def test_single_relationship_backward_compatible(self):
        """Single string relationship still works as before."""
        config = _fan_out_config()
        graph = _fan_out_graph()
        result = execute_query(config, graph, "screen_org_single", {"org_id": "ORG-1"})
        ids = set(_terminal_ids(result.results))
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
                mode="traversal",
                entry_point="Node",
                traversal=[TraversalStep(relationship="links", direction="outgoing", max_depth=1)],
                returns="list[Node]",
                result_shape="entity",
            ),
            "depth_2": NamedQuerySchema(
                mode="traversal",
                entry_point="Node",
                traversal=[TraversalStep(relationship="links", direction="outgoing", max_depth=2)],
                returns="list[Node]",
                result_shape="entity",
            ),
            "depth_3": NamedQuerySchema(
                mode="traversal",
                entry_point="Node",
                traversal=[TraversalStep(relationship="links", direction="outgoing", max_depth=3)],
                returns="list[Node]",
                result_shape="entity",
            ),
            "depth_2_filtered": NamedQuerySchema(
                mode="traversal",
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
                result_shape="entity",
            ),
            "fan_out_depth_2": NamedQuerySchema(
                mode="traversal",
                entry_point="Node",
                traversal=[
                    TraversalStep(
                        relationship=["links", "alt_links"],
                        direction="outgoing",
                        max_depth=2,
                    )
                ],
                returns="list[Node]",
                result_shape="entity",
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
            mode="traversal",
            entry_point="Node",
            traversal=[
                TraversalStep(
                    relationship="links",
                    direction="outgoing",
                    max_depth=4,
                )
            ],
            returns="list[Node]",
            result_shape="entity",
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


class TestPathResults:
    def test_existing_entity_query_output_remains_entity_rows(self, config, graph):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-CIVIC"})

        assert result.result_shape == "entity"
        assert result.dedupe == "entity"
        assert [type(row) for row in result.results] == [EntityInstance, EntityInstance]
        assert {row.entity_id for row in result.results} == {"BP-1234", "BP-5678"}

    def test_default_query_output_is_path_rows(self, config, graph):
        config.named_queries["default_parts_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "default_parts_for_vehicle",
            {"vehicle_id": "V-CIVIC"},
        )

        assert result.result_shape == "path"
        assert result.dedupe == "path"
        assert all(isinstance(row, QueryPathRow) for row in result.results)
        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert row.entry.entity_id == "V-CIVIC"
        assert row.result.entity_type == "Part"
        assert [entity.entity_type for entity in row.entities] == ["Vehicle", "Part"]
        assert len(row.path) == 1

    def test_entity_query_rejects_path_retaining_dedupe_at_runtime(self, config, graph):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="list[Part]",
            result_shape="entity",
        )
        query.dedupe = "none"
        config.named_queries["bad_entity_dedupe"] = query

        with pytest.raises(QueryExecutionError, match="requires dedupe 'entity'"):
            execute_query(config, graph, "bad_entity_dedupe", {"vehicle_id": "V-CIVIC"})

    def test_path_shape_includes_entry_result_entities_segments_and_aliases(
        self,
        config,
        graph,
    ):
        config.named_queries["parts_for_vehicle_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
        )

        result = execute_query(
            config,
            graph,
            "parts_for_vehicle_paths",
            {"vehicle_id": "V-CIVIC"},
        )

        rows = sorted(result.results, key=lambda row: row.result.entity_id)
        assert all(isinstance(row, QueryPathRow) for row in rows)
        first = rows[0]
        assert first.entry.entity_type == "Vehicle"
        assert first.entry.entity_id == "V-CIVIC"
        assert first.result.entity_type == "Part"
        assert first.result.entity_id == "BP-1234"
        assert [entity.entity_id for entity in first.entities] == ["V-CIVIC", "BP-1234"]
        assert len(first.path) == 1
        assert first.path[0].alias == "fit"
        assert first.path[0].relationship_type == "fits"
        assert first.path[0].from_type == "Part"
        assert first.path[0].from_id == "BP-1234"
        assert first.path[0].to_type == "Vehicle"
        assert first.path[0].to_id == "V-CIVIC"
        assert first.path[0].metadata.assertion.lifecycle.status == "active"

    def test_path_dedupe_can_collapse_or_preserve_distinct_paths(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"verified": True, "confidence": 0.99},
            )
        )
        config.named_queries["parts_path_entity_dedupe"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="entity",
        )
        config.named_queries["parts_path_path_dedupe"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
        )

        entity_deduped = execute_query(
            config,
            graph,
            "parts_path_entity_dedupe",
            {"vehicle_id": "V-CIVIC"},
        )
        path_deduped = execute_query(
            config,
            graph,
            "parts_path_path_dedupe",
            {"vehicle_id": "V-CIVIC"},
        )

        assert [
            row.result.entity_id
            for row in entity_deduped.results
            if isinstance(row, QueryPathRow)
        ].count("BP-1234") == 1
        bp1234_rows = [
            row
            for row in path_deduped.results
            if isinstance(row, QueryPathRow) and row.result.entity_id == "BP-1234"
        ]
        assert len(bp1234_rows) == 2
        assert len({row.path[0].edge_key for row in bp1234_rows}) == 2

    def test_multi_hop_path_query_includes_full_intermediate_entities(self):
        config = _kev_path_config()
        graph = _kev_path_graph()

        result = execute_query(
            config,
            graph,
            "business_services_for_vulnerability",
            {"vuln_id": "VULN-1"},
        )

        assert len(result.results) == 1
        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert row.entry.entity_type == "Vulnerability"
        assert row.entry.properties["title"] == "Remote Code Execution"
        assert row.result.entity_type == "BusinessService"
        assert row.result.properties["name"] == "Checkout"
        assert [
            (entity.entity_type, entity.entity_id, entity.properties["name"])
            for entity in row.entities
        ] == [
            ("Vulnerability", "VULN-1", "CVE-2026-0001"),
            ("Product", "PROD-1", "Payments API"),
            ("Asset", "ASSET-1", "payments-prod-1"),
            ("BusinessService", "SVC-1", "Checkout"),
        ]
        assert [segment.alias for segment in row.path] == [
            "affected_product",
            "deployed_asset",
            "supported_service",
        ]
        assert [segment.relationship_type for segment in row.path] == [
            "affects",
            "deployed_on",
            "supports",
        ]


class TestProjectionOrderingAndLimit:
    def test_projected_entity_query_rows(self, config, graph):
        config.named_queries["projected_parts"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="list[Part]",
            result_shape="entity",
            select={
                "vehicle_id": "$entry.entity_id",
                "part_id": "$result.entity_id",
                "brand": "$result.properties.brand",
                "missing": "$result.properties.unknown",
                "input_vehicle": "$input.vehicle_id",
            },
        )

        result = execute_query(config, graph, "projected_parts", {"vehicle_id": "V-CIVIC"})

        assert all(isinstance(row, ProjectedQueryRow) for row in result.results)
        row = result.results[0]
        assert isinstance(row, ProjectedQueryRow)
        assert row.values["vehicle_id"] == "V-CIVIC"
        assert row.values["part_id"] == "BP-1234"
        assert row.values["brand"] == "StopTech"
        assert row.values["missing"] is None
        assert row.values["input_vehicle"] == "V-CIVIC"
        assert isinstance(row.source, EntityInstance)
        assert dump_query_row(row) == {"values": row.values}
        assert "source" in dump_query_row(row, include_source=True)

    def test_projected_path_query_uses_alias_edge_source_and_target(self, config, graph):
        config.named_queries["projected_fit_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
            select={
                "edge_key": "$path.fit.edge.edge_key",
                "confidence": "$path.fit.edge.properties.confidence",
                "review_status": "$path.fit.edge.metadata.assertion.review.status",
                "part_id": "$path.fit.source.entity_id",
                "vehicle_id": "$path.fit.target.entity_id",
            },
        )

        result = execute_query(config, graph, "projected_fit_paths", {"vehicle_id": "V-CIVIC"})

        row = result.results[0]
        assert isinstance(row, ProjectedQueryRow)
        assert row.values["part_id"] == "BP-1234"
        assert row.values["vehicle_id"] == "V-CIVIC"
        assert row.values["confidence"] == 0.95
        assert row.values["review_status"] == "unreviewed"
        assert isinstance(row.source, QueryPathRow)
        assert result.receipt is not None
        receipt_row = result.receipt.results[0]
        assert "source" in receipt_row
        assert "path" in receipt_row["source"]

    def test_projected_relationship_query_uses_relationship_and_endpoints(
        self, config, graph
    ):
        config.named_queries["projected_fit_edges"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="fits",
            result_shape="relationship",
            select={
                "edge_key": "$relationship.edge_key",
                "relationship_type": "$relationship.relationship_type",
                "from_id": "$from_entity.entity_id",
                "to_id": "$to_entity.entity_id",
                "result_id": "$result.entity_id",
            },
        )

        result = execute_query(config, graph, "projected_fit_edges", {"vehicle_id": "V-CIVIC"})

        row = result.results[0]
        assert isinstance(row, ProjectedQueryRow)
        assert row.values["relationship_type"] == "fits"
        assert row.values["from_id"] == "BP-1234"
        assert row.values["to_id"] == "V-CIVIC"
        assert row.values["result_id"] == "BP-1234"
        assert isinstance(row.source, QueryRelationshipRow)

    def test_missing_input_projection_ref_fails(self, config, graph):
        config.named_queries["missing_input_ref"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
            result_shape="entity",
            select={"missing": "$input.not_provided"},
        )

        with pytest.raises(QueryExecutionError, match="Missing query input reference"):
            execute_query(config, graph, "missing_input_ref", {"vehicle_id": "V-CIVIC"})

    def test_default_stable_ordering_without_order_by(self, config, graph):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-CIVIC"})

        assert [row.entity_id for row in result.results] == ["BP-1234", "BP-5678"]

    def test_explicit_order_by_number_and_stable_tie_breaker(self, config, graph):
        graph.update_relationship_state(
            "Part",
            "BP-1234",
            "Vehicle",
            "V-CIVIC",
            "fits",
            property_updates={"confidence": 0.9},
        )
        config.named_queries["ordered_fit_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            order_by=[
                {"by": "$path.fit.edge.properties.confidence", "direction": "desc"},
            ],
        )

        result = execute_query(config, graph, "ordered_fit_paths", {"vehicle_id": "V-CIVIC"})

        assert _terminal_ids(result.results) == ["BP-1234", "BP-5678"]

    def test_explicit_order_by_date_and_datetime_values(self, config, graph):
        config.relationships[0].properties["due_by"] = PropertySchema(type="date")
        config.relationships[0].properties["observed_at"] = PropertySchema(type="datetime")
        graph.update_relationship_state(
            "Part",
            "BP-1234",
            "Vehicle",
            "V-CIVIC",
            "fits",
            property_updates={
                "due_by": "2026-05-18T12:00:00Z",
                "observed_at": "2026-05-17T12:00:00Z",
            },
        )
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            property_updates={
                "due_by": "2026-05-17",
                "observed_at": "2026-05-17T13:00:00+00:00",
            },
        )
        config.named_queries["date_ordered_fit_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            order_by=[
                {
                    "by": "$path.fit.edge.properties.due_by",
                    "direction": "asc",
                    "value_type": "date",
                },
                {
                    "by": "$path.fit.edge.properties.observed_at",
                    "direction": "desc",
                    "value_type": "datetime",
                },
            ],
        )

        result = execute_query(
            config,
            graph,
            "date_ordered_fit_paths",
            {"vehicle_id": "V-CIVIC"},
        )

        assert _terminal_ids(result.results) == ["BP-5678", "BP-1234"]

    def test_order_by_missing_values_sort_last(self, config, graph):
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            property_updates={"rank": 1},
        )
        config.named_queries["missing_last_fit_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            order_by=[{"by": "$path.fit.edge.properties.rank", "direction": "asc"}],
        )

        result = execute_query(
            config,
            graph,
            "missing_last_fit_paths",
            {"vehicle_id": "V-CIVIC"},
        )

        assert _terminal_ids(result.results) == ["BP-5678", "BP-1234"]

    def test_order_by_ordered_enum_asc_and_desc(self, config, graph):
        config.enums["priority"] = EnumSchema(
            values=["low", "medium", "high", "critical"],
            ordered="low_to_high",
        )
        config.entity_types["Part"].properties["priority"] = PropertySchema(
            enum_ref="priority"
        )
        graph.update_entity_properties("Part", "BP-1234", {"priority": "low"})
        graph.update_entity_properties("Part", "BP-5678", {"priority": "critical"})

        config.named_queries["enum_ordered_fit_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            order_by=[
                {
                    "by": "$result.properties.priority",
                    "direction": "asc",
                    "enum_ref": "priority",
                },
            ],
        )

        asc = execute_query(
            config,
            graph,
            "enum_ordered_fit_paths",
            {"vehicle_id": "V-CIVIC"},
        )
        config.named_queries["enum_ordered_fit_paths"].order_by[0].direction = "desc"
        desc = execute_query(
            config,
            graph,
            "enum_ordered_fit_paths",
            {"vehicle_id": "V-CIVIC"},
        )

        assert _terminal_ids(asc.results) == ["BP-1234", "BP-5678"]
        assert _terminal_ids(desc.results) == ["BP-5678", "BP-1234"]

    def test_order_by_ordered_enum_rejects_unknown_runtime_value(self, config, graph):
        config.enums["priority"] = EnumSchema(
            values=["low", "medium", "high", "critical"],
            ordered="low_to_high",
        )
        graph.update_entity_properties("Part", "BP-1234", {"priority": "urgent"})
        graph.update_entity_properties("Part", "BP-5678", {"priority": "low"})
        config.named_queries["bad_enum_order"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            order_by=[
                {
                    "by": "$result.properties.priority",
                    "enum_ref": "priority",
                },
            ],
        )

        with pytest.raises(QueryExecutionError, match="Invalid ordered enum value"):
            execute_query(config, graph, "bad_enum_order", {"vehicle_id": "V-CIVIC"})

    def test_invalid_typed_order_value_fails(self, config, graph):
        graph.update_relationship_state(
            "Part",
            "BP-1234",
            "Vehicle",
            "V-CIVIC",
            "fits",
            property_updates={"due_by": "not-a-date"},
        )
        config.named_queries["bad_date_order"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            order_by=[
                {
                    "by": "$path.fit.edge.properties.due_by",
                    "value_type": "date",
                }
            ],
        )

        with pytest.raises(QueryExecutionError, match="Invalid date order_by value"):
            execute_query(config, graph, "bad_date_order", {"vehicle_id": "V-CIVIC"})

    def test_query_limit_records_total_and_truncation(self, config, graph):
        config.named_queries["limited_parts"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="list[Part]",
            result_shape="entity",
            limit=1,
        )

        result = execute_query(config, graph, "limited_parts", {"vehicle_id": "V-CIVIC"})

        assert len(result.results) == 1
        assert result.total_results == 2
        assert result.limit == 1
        assert result.truncated is True
        assert result.receipt is not None
        assert len(result.receipt.results) == 1
        assert result.receipt.execution_options["result_shape"] == "entity"
        result_node = next(node for node in result.receipt.nodes if node.node_type == "result")
        assert result_node.detail["total_results"] == 2
        assert result_node.detail["limit"] == 1
        assert result_node.detail["truncated"] is True


class TestQueryIncludes:
    def test_include_from_result_attaches_many_side_context_with_limit(
        self, config, graph
    ):
        config.named_queries["part_with_replacements"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "replacements": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                    "limit": 1,
                    "order_by": [
                        {"by": "$edge.properties.confidence", "direction": "desc"}
                    ],
                }
            },
        )

        result = execute_query(
            config,
            graph,
            "part_with_replacements",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        include = row.includes["replacements"]
        assert include.exists is True
        assert include.count == 2
        assert include.truncated is True
        assert include.limit == 1
        assert [item.source.entity_id for item in include.items] == ["BP-5678"]
        assert result.receipt is not None
        assert any(
            "include_summary" in node.detail
            for node in result.receipt.nodes
        )

    def test_include_where_can_reference_path_alias(self, config, graph):
        config.named_queries["include_where_path_ref"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "replacements": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                    "where": {
                        "target.entity_id": {"eq": "$path.fit.source.entity_id"}
                    },
                }
            },
        )

        result = execute_query(
            config,
            graph,
            "include_where_path_ref",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert [item.source.entity_id for item in row.includes["replacements"].items] == [
            "BP-5678",
            "BP-9999",
        ]

    def test_include_related_predicate_can_reference_path_alias(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="blocked",
                from_type="Part",
                from_id="BP-5678",
                to_type="Part",
                to_id="BP-1234",
            )
        )
        config.named_queries["include_related_path_ref"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "has_block_to_fit_source": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                    "where_related": [
                        {
                            "relationship": "blocked",
                            "direction": "outgoing",
                            "target": {
                                "entity_id": {
                                    "eq": "$path.fit.source.entity_id"
                                }
                            },
                        }
                    ],
                }
            },
        )

        result = execute_query(
            config,
            graph,
            "include_related_path_ref",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert [
            item.source.entity_id
            for item in row.includes["has_block_to_fit_source"].items
        ] == ["BP-5678"]

    def test_include_order_by_ordered_enum(self, config, graph):
        config.enums["priority"] = EnumSchema(
            values=["low", "medium", "high", "critical"],
            ordered="low_to_high",
        )
        config.entity_types["Part"].properties["priority"] = PropertySchema(
            enum_ref="priority"
        )
        graph.update_entity_properties("Part", "BP-5678", {"priority": "low"})
        graph.update_entity_properties("Part", "BP-9999", {"priority": "critical"})
        config.named_queries["part_with_ordered_replacements"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "replacements": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                    "order_by": [
                        {
                            "by": "$source.properties.priority",
                            "direction": "desc",
                            "enum_ref": "priority",
                        }
                    ],
                }
            },
        )

        result = execute_query(
            config,
            graph,
            "part_with_ordered_replacements",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert [item.source.entity_id for item in row.includes["replacements"].items] == [
            "BP-9999",
            "BP-5678",
        ]

    def test_optional_include_without_match_retains_row(self, config, graph):
        config.named_queries["part_with_optional_blocks"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "blocks": {
                    "from": "$result",
                    "relationship": "blocked",
                    "direction": "outgoing",
                    "many": True,
                }
            },
        )

        result = execute_query(
            config,
            graph,
            "part_with_optional_blocks",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        include = row.includes["blocks"]
        assert include.exists is False
        assert include.count == 0
        assert include.items == []

    def test_required_include_filters_row_without_match(self, config, graph):
        config.named_queries["part_requiring_blocks"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "blocks": {
                    "from": "$result",
                    "relationship": "blocked",
                    "direction": "outgoing",
                    "required": True,
                }
            },
        )

        result = execute_query(
            config,
            graph,
            "part_requiring_blocks",
            {"vehicle_id": "V-CIVIC"},
        )

        assert result.results == []

    def test_singular_include_errors_on_multiple_matches(self, config, graph):
        config.named_queries["ambiguous_replacement"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "replacement": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                }
            },
        )

        with pytest.raises(QueryExecutionError, match="set many: true"):
            execute_query(
                config,
                graph,
                "ambiguous_replacement",
                {"vehicle_id": "V-CIVIC"},
            )

    def test_singular_include_where_and_projection_refs(self, config, graph):
        config.named_queries["projected_replacement"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "replacement": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "where": {"edge.properties.confidence": {"gte": 0.8}},
                },
                "no_match": {
                    "from": "$result",
                    "relationship": "blocked",
                    "direction": "outgoing",
                },
            },
            select={
                "has_replacement": "$include.replacement.exists",
                "replacement_count": "$include.replacement.count",
                "replacement_part": "$include.replacement.source.entity_id",
                "replacement_confidence": "$include.replacement.edge.properties.confidence",
                "missing_block_target": "$include.no_match.target.entity_id",
            },
        )

        result = execute_query(
            config,
            graph,
            "projected_replacement",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, ProjectedQueryRow)
        assert row.values == {
            "has_replacement": True,
            "replacement_count": 1,
            "replacement_part": "BP-5678",
            "replacement_confidence": 0.85,
            "missing_block_target": None,
        }
        assert isinstance(row.source, QueryPathRow)
        assert "replacement" in row.source.includes

    def test_include_from_entry_and_path_alias_anchors(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="vehicle_blocks_part",
                from_type="Vehicle",
                from_id="V-CIVIC",
                to_type="Part",
                to_id="BP-9999",
            )
        )
        config.named_queries["path_anchor_includes"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "entry_block": {
                    "from": "$entry",
                    "relationship": "vehicle_blocks_part",
                    "direction": "outgoing",
                },
                "source_replacements": {
                    "from": "$path.fit.source",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                },
                "target_blocks": {
                    "from": "$path.fit.target",
                    "relationship": "vehicle_blocks_part",
                    "direction": "outgoing",
                },
            },
        )

        result = execute_query(
            config,
            graph,
            "path_anchor_includes",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert row.includes["entry_block"].items[0].target.entity_id == "BP-9999"
        assert row.includes["source_replacements"].count == 2
        assert row.includes["target_blocks"].items[0].target.entity_id == "BP-9999"

    def test_many_include_items_projection_is_allowed(self, config, graph):
        config.named_queries["many_include_items"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "replacements": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                }
            },
            select={
                "count": "$include.replacements.count",
                "items": "$include.replacements.items",
            },
        )

        result = execute_query(
            config,
            graph,
            "many_include_items",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, ProjectedQueryRow)
        assert row.values["count"] == 2
        assert len(row.values["items"]) == 2

    def test_include_related_predicates_match_from_candidate_anchor(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="blocked",
                from_type="Part",
                from_id="BP-5678",
                to_type="Part",
                to_id="BP-9999",
            )
        )
        config.named_queries["related_include_filters"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "has_block": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                    "where_related": [
                        {
                            "relationship": "blocked",
                            "direction": "outgoing",
                            "target": {"entity_id": {"eq": "BP-9999"}},
                        }
                    ],
                },
                "no_block": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                    "where_not_related": [
                        {
                            "relationship": "blocked",
                            "direction": "outgoing",
                            "target": {"entity_id": {"eq": "BP-9999"}},
                        }
                    ],
                },
            },
        )

        result = execute_query(
            config,
            graph,
            "related_include_filters",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert [item.source.entity_id for item in row.includes["has_block"].items] == [
            "BP-5678"
        ]
        assert [item.source.entity_id for item in row.includes["no_block"].items] == [
            "BP-9999"
        ]

    def test_include_relationship_state_uses_parent_query_visibility(self, config, graph):
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Part",
            "BP-1234",
            "replaces",
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="pending")
                )
            ),
        )
        config.named_queries["live_include_replacements"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "replacements": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                }
            },
        )

        result = execute_query(
            config,
            graph,
            "live_include_replacements",
            {"vehicle_id": "V-CIVIC"},
        )

        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert [item.source.entity_id for item in row.includes["replacements"].items] == [
            "BP-9999"
        ]


class TestOperationalQueryControls:
    def test_non_required_step_without_match_preserves_path_row(self, config, graph):
        config.named_queries["fit_with_optional_replacement"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.95},
                    alias="fit",
                ),
                TraversalStep(
                    relationship="replaces",
                    direction="outgoing",
                    required=False,
                    alias="replacement",
                ),
            ],
            returns="list[Part]",
            result_shape="path",
            select={
                "part_id": "$result.entity_id",
                "replacement_edge": "$path.replacement.edge.edge_key",
            },
        )

        result = execute_query(
            config,
            graph,
            "fit_with_optional_replacement",
            {"vehicle_id": "V-CIVIC"},
        )

        assert len(result.results) == 1
        row = result.results[0]
        assert isinstance(row, ProjectedQueryRow)
        assert row.values == {"part_id": "BP-1234", "replacement_edge": None}
        assert result.receipt is not None
        assert any(
            node.detail.get("optional_traversal_preserved") is True
            for node in result.receipt.nodes
        )

    def test_non_required_step_with_match_appends_segment(self, config, graph):
        config.named_queries["fit_with_replacement_match"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.9},
                    alias="fit",
                ),
                TraversalStep(
                    relationship="replaces",
                    direction="outgoing",
                    required=False,
                    alias="replacement",
                ),
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
        )

        result = execute_query(
            config,
            graph,
            "fit_with_replacement_match",
            {"vehicle_id": "V-CIVIC"},
        )

        assert len(result.results) == 1
        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert [segment.alias for segment in row.path] == ["fit", "replacement"]
        assert row.result.entity_id == "BP-1234"

    def test_non_required_step_with_multiple_matches_fans_out_deterministically(
        self, config, graph
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="BP-5678",
                to_type="Part",
                to_id="BP-9999",
                properties={"direction": "alternate", "confidence": 0.7},
            )
        )
        config.named_queries["fit_with_replacement_fanout"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.9},
                    alias="fit",
                ),
                TraversalStep(
                    relationship="replaces",
                    direction="outgoing",
                    required=False,
                    alias="replacement",
                ),
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
        )

        result = execute_query(
            config,
            graph,
            "fit_with_replacement_fanout",
            {"vehicle_id": "V-CIVIC"},
        )

        result_ids = [
            row.result.entity_id
            for row in result.results
            if isinstance(row, QueryPathRow)
        ]
        assert result_ids == ["BP-1234", "BP-9999"]

    def test_unknown_path_alias_still_fails(self, config, graph):
        config.named_queries["unknown_projection_alias"] = NamedQuerySchema.model_construct(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
            select={"bad": "$path.missing.edge.edge_key"},
        )

        with pytest.raises(QueryExecutionError, match="Unknown path alias"):
            execute_query(
                config,
                graph,
                "unknown_projection_alias",
                {"vehicle_id": "V-CIVIC"},
            )

    def test_max_paths_truncates_deterministically_and_records_metadata(
        self, config, graph
    ):
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id="AA-0001",
                properties={
                    "part_number": "AA-0001",
                    "name": "Sorted First Pad",
                    "category": "brakes",
                },
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="AA-0001",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"verified": True, "confidence": 0.97},
            )
        )
        config.named_queries["budgeted_fit_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            max_paths=1,
        )

        result = execute_query(config, graph, "budgeted_fit_paths", {"vehicle_id": "V-CIVIC"})

        assert _terminal_ids(result.results) == ["AA-0001"]
        assert result.total_results == 1
        assert result.total_path_count is None
        assert result.retained_path_count == 1
        assert result.path_truncated is True
        assert result.limit_truncated is False
        assert result.truncated is True
        assert result.truncation_reasons == ["max_paths"]
        assert result.receipt is not None
        traversal_nodes = [
            node for node in result.receipt.nodes if node.node_type == "edge_traversal"
        ]
        assert len(traversal_nodes) == 1
        assert traversal_nodes[0].entity_id == "AA-0001"
        result_node = next(node for node in result.receipt.nodes if node.node_type == "result")
        assert result_node.detail["path_truncated"] is True
        assert result_node.detail["truncation_reasons"] == ["max_paths"]
        assert result_node.detail["total_path_count"] is None
        assert result_node.detail["retained_path_count"] == 1
        assert result_node.detail["evaluated_path_candidate_count"] == 1

    def test_max_paths_does_not_gather_later_relationship_types(
        self, config, graph, monkeypatch
    ):
        calls: list[str] = []
        original_iter = query_engine.iter_step_relationships

        def spy_iter_step_relationships(*args, **kwargs):
            calls.append(kwargs["relationship_type"])
            return original_iter(*args, **kwargs)

        monkeypatch.setattr(
            query_engine,
            "iter_step_relationships",
            spy_iter_step_relationships,
        )
        config.named_queries["budgeted_multi_relationship"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship=["fits", "replaces"],
                    direction="incoming",
                    alias="hop",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            max_paths=1,
        )

        result = execute_query(
            config,
            graph,
            "budgeted_multi_relationship",
            {"vehicle_id": "V-CIVIC"},
        )

        assert _terminal_ids(result.results) == ["BP-1234"]
        assert calls == ["fits"]
        assert result.truncation_reasons == ["max_paths"]

    def test_max_paths_does_not_queue_capped_state_for_deeper_traversal(
        self, config, graph, monkeypatch
    ):
        original_deque = query_engine.deque
        deeper_appends: list[object] = []

        class TrackingDeque(original_deque):
            def append(self, item):
                if isinstance(item, tuple) and len(item) == 3 and item[2] > 0:
                    deeper_appends.append(item)
                super().append(item)

        monkeypatch.setattr(query_engine, "deque", TrackingDeque)
        config.named_queries["budgeted_deep_replacements"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Part",
            traversal=[
                TraversalStep(
                    relationship="replaces",
                    direction="outgoing",
                    max_depth=2,
                    alias="replacement",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            max_paths=1,
        )

        result = execute_query(
            config,
            graph,
            "budgeted_deep_replacements",
            {"part_number": "BP-5678"},
        )

        assert _terminal_ids(result.results) == ["BP-1234"]
        assert deeper_appends == []
        assert result.path_truncated is True
        assert result.truncation_reasons == ["max_paths"]

    def test_max_paths_uses_stable_identity_not_insertion_order(self, config, graph):
        for part_id in ("ZZ-9999", "AA-0001"):
            graph.add_entity(
                EntityInstance(
                    entity_type="Part",
                    entity_id=part_id,
                    properties={
                        "part_number": part_id,
                        "name": part_id,
                        "category": "brakes",
                    },
                )
            )
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="fits",
                    from_type="Part",
                    from_id=part_id,
                    to_type="Vehicle",
                    to_id="V-CIVIC",
                    properties={"verified": True, "confidence": 0.97},
                )
            )
        config.named_queries["stable_budgeted_fit_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            max_paths=2,
        )

        result = execute_query(
            config,
            graph,
            "stable_budgeted_fit_paths",
            {"vehicle_id": "V-CIVIC"},
        )

        assert _terminal_ids(result.results) == ["AA-0001", "BP-1234"]

    def test_max_paths_per_result_caps_each_result_entity(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"verified": True, "confidence": 0.96},
            )
        )
        config.named_queries["per_result_budgeted_fit_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
            max_paths_per_result=1,
        )

        result = execute_query(
            config,
            graph,
            "per_result_budgeted_fit_paths",
            {"vehicle_id": "V-CIVIC"},
        )

        assert _terminal_ids(result.results).count("BP-1234") == 1
        assert _terminal_ids(result.results).count("BP-5678") == 1
        assert result.total_path_count == 3
        assert result.retained_path_count == 2
        assert result.truncation_reasons == ["max_paths_per_result"]

    def test_optional_matches_respect_max_paths(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="BP-5678",
                to_type="Part",
                to_id="BP-9999",
                properties={"direction": "alternate", "confidence": 0.7},
            )
        )
        config.named_queries["optional_budgeted_replacements"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"confidence": 0.9},
                    alias="fit",
                ),
                TraversalStep(
                    relationship="replaces",
                    direction="outgoing",
                    required=False,
                    alias="replacement",
                ),
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
            max_paths=1,
        )

        result = execute_query(
            config,
            graph,
            "optional_budgeted_replacements",
            {"vehicle_id": "V-CIVIC"},
        )

        assert _terminal_ids(result.results) == ["BP-1234"]
        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert [segment.alias for segment in row.path] == ["fit", "replacement"]
        assert result.path_truncated is True
        assert result.truncation_reasons == ["max_paths"]

    def test_output_limit_is_distinct_from_path_budget_truncation(self, config, graph):
        config.named_queries["budget_and_limited_fit_paths"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            max_paths=2,
            limit=1,
        )

        result = execute_query(
            config,
            graph,
            "budget_and_limited_fit_paths",
            {"vehicle_id": "V-CIVIC"},
        )

        assert len(result.results) == 1
        assert result.total_results == 2
        assert result.total_path_count == 2
        assert result.retained_path_count == 2
        assert result.path_truncated is False
        assert result.limit_truncated is True
        assert result.truncation_reasons == ["limit"]


def _kev_path_config() -> CoreConfig:
    return CoreConfig(
        name="kev-path",
        entity_types={
            "Vulnerability": EntityTypeSchema(
                properties={
                    "vuln_id": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                    "title": PropertySchema(type="string"),
                }
            ),
            "Product": EntityTypeSchema(
                properties={
                    "product_id": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                }
            ),
            "Asset": EntityTypeSchema(
                properties={
                    "asset_id": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                }
            ),
            "BusinessService": EntityTypeSchema(
                properties={
                    "service_id": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                }
            ),
        },
        relationships=[
            RelationshipSchema(name="affects", from_entity="Vulnerability", to_entity="Product"),
            RelationshipSchema(name="deployed_on", from_entity="Product", to_entity="Asset"),
            RelationshipSchema(name="supports", from_entity="Asset", to_entity="BusinessService"),
        ],
        named_queries={
            "business_services_for_vulnerability": NamedQuerySchema(
                mode="traversal",
                entry_point="Vulnerability",
                traversal=[
                    TraversalStep(
                        relationship="affects",
                        direction="outgoing",
                        alias="affected_product",
                    ),
                    TraversalStep(
                        relationship="deployed_on",
                        direction="outgoing",
                        alias="deployed_asset",
                    ),
                    TraversalStep(
                        relationship="supports",
                        direction="outgoing",
                        alias="supported_service",
                    ),
                ],
                returns="list[BusinessService]",
                result_shape="path",
                dedupe="path",
            )
        },
    )


def _kev_path_graph() -> EntityGraph:
    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Vulnerability",
            entity_id="VULN-1",
            properties={
                "vuln_id": "VULN-1",
                "name": "CVE-2026-0001",
                "title": "Remote Code Execution",
            },
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Product",
            entity_id="PROD-1",
            properties={"product_id": "PROD-1", "name": "Payments API"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Asset",
            entity_id="ASSET-1",
            properties={"asset_id": "ASSET-1", "name": "payments-prod-1"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="BusinessService",
            entity_id="SVC-1",
            properties={"service_id": "SVC-1", "name": "Checkout"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="affects",
            from_type="Vulnerability",
            from_id="VULN-1",
            to_type="Product",
            to_id="PROD-1",
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="deployed_on",
            from_type="Product",
            from_id="PROD-1",
            to_type="Asset",
            to_id="ASSET-1",
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="supports",
            from_type="Asset",
            from_id="ASSET-1",
            to_type="BusinessService",
            to_id="SVC-1",
        )
    )
    return graph


class TestStructuredPredicates:
    def test_predicate_context_rejects_mismatched_segment(self, graph):
        current = graph.get_entity("Part", "BP-1234")
        candidate = graph.get_entity("Vehicle", "V-CIVIC")
        assert current is not None
        assert candidate is not None
        segment = QueryPathSegment(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-9999",
            to_type="Vehicle",
            to_id="V-CIVIC",
        )

        with pytest.raises(QueryExecutionError, match="endpoints do not match"):
            build_predicate_context(
                entry=current,
                current=current,
                candidate=candidate,
                segment=segment,
            )

    def test_where_filters_edge_source_target_and_candidate_values(self, config, graph):
        config.named_queries["production_brake_fitments"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Part",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "outgoing",
                        "where": {
                            "edge.properties.confidence": {"gte": 0.9},
                            "edge.properties.deprecated": {"exists": False},
                            "source.properties.category": {"eq": "brakes"},
                            "target.properties.make": {"eq": "Honda"},
                            "target.properties.model": {"not_in": ["Accord"]},
                            "candidate.entity_id": {"in": ["V-CIVIC"]},
                        },
                    }
                )
            ],
            returns="list[Vehicle]",
        )

        result = execute_query(
            config,
            graph,
            "production_brake_fitments",
            {"part_number": "BP-1234"},
        )

        assert _terminal_ids(result.results) == ["V-CIVIC"]

    def test_where_does_not_treat_runtime_as_temporal_field(self, config, graph):
        config.relationships[0].properties["runtime"] = PropertySchema(type="string")
        graph.update_entity_properties("Part", "BP-1234", {"uptime": 42})
        graph.update_relationship_state(
            "Part",
            "BP-1234",
            "Vehicle",
            "V-CIVIC",
            "fits",
            property_updates={"runtime": "2026-05-17-build"},
        )
        config.named_queries["python_runtime_fitments"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Part",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "outgoing",
                        "where": {
                            "edge.properties.runtime": {"eq": "2026-05-17-build"},
                            "source.properties.uptime": {"gt": 10},
                        },
                    }
                )
            ],
            returns="list[Vehicle]",
        )

        result = execute_query(
            config,
            graph,
            "python_runtime_fitments",
            {"part_number": "BP-1234"},
        )

        assert _terminal_ids(result.results) == ["V-CIVIC"]

    def test_where_does_not_treat_date_like_entity_id_as_temporal(self, config, graph):
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id="2026-05-17-build",
                properties={
                    "vehicle_id": "2026-05-17-build",
                    "year": 2026,
                    "make": "Build",
                    "model": "Candidate",
                },
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="2026-05-17-build",
                properties={"verified": True},
            )
        )
        config.named_queries["date_like_vehicle_id_fitments"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Part",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "outgoing",
                        "where": {
                            "candidate.entity_id": {"eq": "2026-05-17-build"},
                        },
                    }
                )
            ],
            returns="list[Vehicle]",
        )

        result = execute_query(
            config,
            graph,
            "date_like_vehicle_id_fitments",
            {"part_number": "BP-1234"},
        )

        assert _terminal_ids(result.results) == ["2026-05-17-build"]

    def test_where_filters_relationship_metadata_review_and_lifecycle(self, config):
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-1",
                properties={"vehicle_id": "V-1", "make": "Honda", "model": "Civic", "year": 2026},
            )
        )
        for part_id, review_status, lifecycle_status in (
            ("P-APPROVED", "approved", "active"),
            ("P-REJECTED", "rejected", "active"),
            ("P-INACTIVE", "approved", "inactive"),
        ):
            graph.add_entity(
                EntityInstance(
                    entity_type="Part",
                    entity_id=part_id,
                    properties={
                        "part_number": part_id,
                        "name": part_id,
                        "category": "brakes",
                        "brand": "Acme",
                    },
                )
            )
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="fits",
                    from_type="Part",
                    from_id=part_id,
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": True},
                    metadata=RelationshipMetadata(
                        assertion=RelationshipAssertion(
                            review=RelationshipReviewState(
                                status=review_status,
                                source="human",
                            ),
                            lifecycle=RelationshipLifecycleState(status=lifecycle_status),
                        )
                    ),
                )
            )
        config.named_queries["approved_active_parts"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where": {
                            "edge.metadata.assertion.lifecycle.status": {"eq": "active"},
                            "edge.metadata.assertion.review.status": {"eq": "approved"},
                        },
                    }
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "approved_active_parts",
            {"vehicle_id": "V-1"},
        )

        assert _terminal_ids(result.results) == ["P-APPROVED"]

    def test_where_compares_date_input_refs(self, config, graph):
        config.relationships[0].properties["due_by"] = PropertySchema(type="date")
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CAMRY",
                properties={"verified": True, "due_by": "2026-05-21"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-5678",
                to_type="Vehicle",
                to_id="V-CAMRY",
                properties={"verified": True, "due_by": "2026-05-28"},
            )
        )
        config.named_queries["parts_due_before_cutoff"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where": {
                            "edge.properties.due_by": {"lte": "$input.cutoff_date"},
                        },
                    }
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "parts_due_before_cutoff",
            {"vehicle_id": "V-CAMRY", "cutoff_date": "2026-05-22T00:00:00Z"},
        )

        assert _terminal_ids(result.results) == ["BP-1234"]

    def test_where_compares_datetime_values(self, config, graph):
        config.relationships[0].properties["checked_at"] = PropertySchema(type="datetime")
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CAMRY",
                properties={"verified": True, "checked_at": "2026-05-17T12:00:00Z"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-5678",
                to_type="Vehicle",
                to_id="V-CAMRY",
                properties={"verified": True, "checked_at": "2026-05-18T12:00:00+00:00"},
            )
        )
        config.named_queries["parts_checked_before"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where": {
                            "edge.properties.checked_at": {"lt": "$input.as_of"},
                        },
                    }
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "parts_checked_before",
            {"vehicle_id": "V-CAMRY", "as_of": "2026-05-18T00:00:00+00:00"},
        )

        assert _terminal_ids(result.results) == ["BP-1234"]

    def test_invalid_temporal_predicate_value_raises(self, config, graph):
        config.relationships[0].properties["checked_at"] = PropertySchema(type="datetime")
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CAMRY",
                properties={"verified": True, "checked_at": "2026-05-17T12:00:00Z"},
            )
        )
        config.named_queries["bad_checked_at"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where": {
                            "edge.properties.checked_at": {"lte": "$input.as_of"},
                        },
                    }
                )
            ],
            returns="list[Part]",
        )

        with pytest.raises(QueryExecutionError, match="Invalid datetime predicate value"):
            execute_query(
                config,
                graph,
                "bad_checked_at",
                {"vehicle_id": "V-CAMRY", "as_of": "not-a-datetime"},
            )

    def test_where_compares_metadata_datetime_from_runtime_value(self, config):
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-1",
                properties={"vehicle_id": "V-1", "make": "Honda", "model": "Civic", "year": 2026},
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id="P-1",
                properties={
                    "part_number": "P-1",
                    "name": "Metadata Part",
                    "category": "brakes",
                },
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": True},
                metadata=RelationshipMetadata(
                    assertion=RelationshipAssertion(
                        lifecycle=RelationshipLifecycleState(
                            effective_until=datetime(2027, 5, 20, tzinfo=timezone.utc)
                        )
                    )
                ),
            )
        )
        config.named_queries["effective_fitments"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where": {
                            "edge.metadata.assertion.lifecycle.effective_until": {
                                "gt": "$input.as_of"
                            },
                        },
                    }
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "effective_fitments",
            {"vehicle_id": "V-1", "as_of": "2026-05-19T00:00:00Z"},
        )

        assert _terminal_ids(result.results) == ["P-1"]

    def test_missing_input_ref_raises_clear_error(self, config, graph):
        config.named_queries["missing_input_ref"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where": {
                            "edge.properties.confidence": {"gte": "$input.min_confidence"},
                        },
                    }
                )
            ],
            returns="list[Part]",
        )

        with pytest.raises(QueryExecutionError, match="Missing query input reference"):
            execute_query(
                config,
                graph,
                "missing_input_ref",
                {"vehicle_id": "V-CIVIC"},
            )

    def test_where_related_from_candidate_matches_edge_source_and_target_predicates(
        self,
        config,
        graph,
    ):
        config.entity_types["Owner"] = EntityTypeSchema(
            properties={
                "owner_id": PropertySchema(type="string", primary_key=True),
                "name": PropertySchema(type="string"),
            }
        )
        config.relationships.append(
            RelationshipSchema(name="owned_by", from_entity="Part", to_entity="Owner")
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Owner",
                entity_id="OWNER-1",
                properties={"owner_id": "OWNER-1", "name": "Team One"},
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Owner",
                entity_id="OWNER-2",
                properties={"owner_id": "OWNER-2", "name": "Team Two"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="owned_by",
                from_type="Part",
                from_id="BP-1234",
                to_type="Owner",
                to_id="OWNER-1",
                properties={"verification_status": "verified"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="owned_by",
                from_type="Part",
                from_id="BP-5678",
                to_type="Owner",
                to_id="OWNER-2",
                properties={"verification_status": "pending"},
            )
        )
        config.named_queries["owned_parts_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where_related": [
                            {
                                "relationship": "owned_by",
                                "direction": "outgoing",
                                # Related checks are anchored from the traversal candidate:
                                # here, the Part reached by the incoming fits edge.
                                "edge": {
                                    "properties.verification_status": {"eq": "verified"}
                                },
                                "source": {"properties.brand": {"eq": "StopTech"}},
                                "target": {
                                    "properties.owner_id": {"eq": "$input.owner_id"}
                                },
                            }
                        ],
                    }
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "owned_parts_for_vehicle",
            {"vehicle_id": "V-CIVIC", "owner_id": "OWNER-1"},
        )

        assert _terminal_ids(result.results) == ["BP-1234"]

    def test_where_not_related_excludes_matching_related_edge(self, config, graph):
        config.relationships.append(
            RelationshipSchema(name="suppressed_fit", from_entity="Part", to_entity="Vehicle")
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-5678",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"reason": "retired"},
            )
        )
        config.named_queries["unsuppressed_parts_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where_not_related": [
                            {
                                "relationship": "suppressed_fit",
                                "direction": "outgoing",
                                # Related checks are anchored from the traversal candidate:
                                # here, the Part reached by the incoming fits edge.
                                "edge": {"properties.reason": {"eq": "retired"}},
                                "target": {"entity_id": {"eq": "$entry.entity_id"}},
                            }
                        ],
                    }
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "unsuppressed_parts_for_vehicle",
            {"vehicle_id": "V-CIVIC"},
        )

        assert _terminal_ids(result.results) == ["BP-1234"]


class TestRelationshipState:
    def test_where_related_ignores_pending_related_edge_under_live_state(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-5678",
                to_type="Vehicle",
                to_id="V-CIVIC",
                metadata=_metadata(review_status="pending"),
            )
        )
        config.named_queries["parts_with_live_suppression"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where_related": [
                            {
                                "relationship": "suppressed_fit",
                                "direction": "outgoing",
                                "target": {"entity_id": {"eq": "$entry.entity_id"}},
                            }
                        ],
                    }
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "parts_with_live_suppression",
            {"vehicle_id": "V-CIVIC"},
        )

        assert result.results == []

    def test_where_not_related_ignores_pending_related_edge_under_live_state(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-5678",
                to_type="Vehicle",
                to_id="V-CIVIC",
                metadata=_metadata(review_status="pending"),
            )
        )
        config.named_queries["parts_without_live_suppression"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where_not_related": [
                            {
                                "relationship": "suppressed_fit",
                                "direction": "outgoing",
                                "target": {"entity_id": {"eq": "$entry.entity_id"}},
                            }
                        ],
                    }
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "parts_without_live_suppression",
            {"vehicle_id": "V-CIVIC"},
        )

        assert set(_terminal_ids(result.results)) == {"BP-1234", "BP-5678"}

    def test_inactive_related_edge_does_not_affect_related_predicates(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_fit",
                from_type="Part",
                from_id="BP-5678",
                to_type="Vehicle",
                to_id="V-CIVIC",
                metadata=_metadata(lifecycle_status="inactive"),
            )
        )
        config.named_queries["parts_with_inactive_suppression"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where_related": [
                            {
                                "relationship": "suppressed_fit",
                                "direction": "outgoing",
                                "target": {"entity_id": {"eq": "$entry.entity_id"}},
                            }
                        ],
                    }
                )
            ],
            returns="list[Part]",
        )
        config.named_queries["parts_without_inactive_suppression"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where_not_related": [
                            {
                                "relationship": "suppressed_fit",
                                "direction": "outgoing",
                                "target": {"entity_id": {"eq": "$entry.entity_id"}},
                            }
                        ],
                    }
                )
            ],
            returns="list[Part]",
        )

        related = execute_query(
            config,
            graph,
            "parts_with_inactive_suppression",
            {"vehicle_id": "V-CIVIC"},
        )
        not_related = execute_query(
            config,
            graph,
            "parts_without_inactive_suppression",
            {"vehicle_id": "V-CIVIC"},
        )

        assert related.results == []
        assert set(_terminal_ids(not_related.results)) == {"BP-1234", "BP-5678"}

    def test_relationship_state_modes_filter_traversal(self, config):
        graph = EntityGraph()
        now = datetime.now(timezone.utc)
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-1",
                properties={"vehicle_id": "V-1", "year": 2026, "make": "Honda", "model": "Civic"},
            )
        )
        states = [
            ("P-APPROVED", "approved", "active"),
            ("P-PENDING", "pending", "active"),
            ("P-UNREVIEWED", "unreviewed", "active"),
            ("P-REJECTED", "rejected", "active"),
            ("P-INACTIVE", "approved", "inactive"),
            ("P-SUPERSEDED", "approved", "superseded"),
            ("P-RETRACTED", "approved", "retracted"),
            ("P-FUTURE", "pending", "active"),
            ("P-EXPIRED", "pending", "active"),
        ]
        for part_id, review_status, lifecycle_status in states:
            graph.add_entity(
                EntityInstance(
                    entity_type="Part",
                    entity_id=part_id,
                    properties={
                        "part_number": part_id,
                        "name": part_id,
                        "category": "brakes",
                        "brand": "Acme",
                    },
                )
            )
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="fits",
                    from_type="Part",
                    from_id=part_id,
                    to_type="Vehicle",
                    to_id="V-1",
                    properties={"verified": True},
                    metadata=_metadata(
                        review_status=review_status,
                        lifecycle_status=lifecycle_status,
                        effective_from=(
                            now + timedelta(days=1) if part_id == "P-FUTURE" else None
                        ),
                        effective_until=(
                            now - timedelta(days=1) if part_id == "P-EXPIRED" else None
                        ),
                    ),
                )
            )
        traversal = [TraversalStep(relationship="fits", direction="incoming")]
        config.named_queries["live_parts"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=traversal,
            returns="list[Part]",
            result_shape="entity",
        )
        config.named_queries["accepted_parts"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=traversal,
            returns="list[Part]",
            relationship_state="accepted",
            result_shape="entity",
        )
        config.named_queries["pending_parts"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=traversal,
            returns="list[Part]",
            relationship_state="pending",
        )
        config.named_queries["reviewable_parts"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=traversal,
            returns="list[Part]",
            relationship_state="reviewable",
        )

        live = execute_query(config, graph, "live_parts", {"vehicle_id": "V-1"})
        accepted = execute_query(config, graph, "accepted_parts", {"vehicle_id": "V-1"})
        pending = execute_query(config, graph, "pending_parts", {"vehicle_id": "V-1"})
        reviewable = execute_query(config, graph, "reviewable_parts", {"vehicle_id": "V-1"})

        assert [row.entity_id for row in live.results] == ["P-APPROVED", "P-UNREVIEWED"]
        assert [row.entity_id for row in accepted.results] == ["P-APPROVED"]
        assert _terminal_ids(pending.results) == ["P-PENDING"]
        assert _terminal_ids(reviewable.results) == [
            "P-APPROVED",
            "P-PENDING",
            "P-UNREVIEWED",
        ]

    def test_runtime_relationship_state_override_requires_opt_in(self, config, graph):
        config.named_queries["pending_override_blocked"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
        )

        with pytest.raises(QueryExecutionError, match="override is not allowed"):
            execute_query(
                config,
                graph,
                "pending_override_blocked",
                {"vehicle_id": "V-CIVIC"},
                relationship_state="pending",
            )

    def test_runtime_relationship_state_override_filters_when_allowed(self, config, graph):
        rel = graph.get_relationship("Part", "BP-5678", "Vehicle", "V-CIVIC", "fits")
        assert rel is not None
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=_metadata(review_status="pending"),
            edge_key=rel.edge_key,
        )
        config.named_queries["parts_override_allowed"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
            allow_relationship_state_override=True,
        )

        result = execute_query(
            config,
            graph,
            "parts_override_allowed",
            {"vehicle_id": "V-CIVIC"},
            relationship_state="pending",
        )

        assert result.relationship_state == "pending"
        assert _terminal_ids(result.results) == ["BP-5678"]

    @pytest.mark.parametrize("state", ["pending", "reviewable"])
    def test_runtime_relationship_state_override_rejects_compact_entity_query(
        self,
        config,
        graph,
        state,
    ):
        config.named_queries["compact_override_allowed"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
            result_shape="entity",
            allow_relationship_state_override=True,
        )

        with pytest.raises(QueryExecutionError, match=f"relationship_state '{state}'"):
            execute_query(
                config,
                graph,
                "compact_override_allowed",
                {"vehicle_id": "V-CIVIC"},
                relationship_state=state,
            )

    def test_runtime_relationship_state_override_rejects_reviewable_relationship_query(
        self,
        config,
        graph,
    ):
        config.named_queries["relationship_override_allowed"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="fits",
            result_shape="relationship",
            allow_relationship_state_override=True,
        )

        with pytest.raises(
            QueryExecutionError,
            match="relationship_state 'reviewable' requires result_shape 'path'",
        ):
            execute_query(
                config,
                graph,
                "relationship_override_allowed",
                {"vehicle_id": "V-CIVIC"},
                relationship_state="reviewable",
            )

    @pytest.mark.parametrize("state", ["pending", "reviewable"])
    def test_runtime_relationship_state_override_allows_path_query(
        self,
        config,
        graph,
        state,
    ):
        config.named_queries["path_override_allowed"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
            result_shape="path",
            allow_relationship_state_override=True,
        )

        result = execute_query(
            config,
            graph,
            "path_override_allowed",
            {"vehicle_id": "V-CIVIC"},
            relationship_state=state,
        )

        assert result.relationship_state == state
        assert result.result_shape == "path"

    def test_receipt_records_config_relationship_state_and_shape_options(self, config, graph):
        config.named_queries["pending_path_receipt"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    alias="fitment",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
            relationship_state="pending",
        )

        result = execute_query(
            config,
            graph,
            "pending_path_receipt",
            {"vehicle_id": "V-CIVIC"},
        )

        assert result.receipt is not None
        assert result.receipt.execution_options == {
            "relationship_state": "pending",
            "relationship_state_source": "query_config",
            "result_shape": "path",
            "dedupe": "path",
        }
        root = result.receipt.nodes[0]
        assert root.node_type == "query"
        assert root.detail["execution_options"] == result.receipt.execution_options

    def test_receipt_records_runtime_relationship_state_override_source(self, config, graph):
        config.named_queries["override_receipt"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
            allow_relationship_state_override=True,
        )

        result = execute_query(
            config,
            graph,
            "override_receipt",
            {"vehicle_id": "V-CIVIC"},
            relationship_state="accepted",
        )

        assert result.receipt is not None
        assert result.receipt.execution_options["relationship_state"] == "accepted"
        assert result.receipt.execution_options["relationship_state_source"] == "runtime_override"
        assert (
            result.receipt.nodes[0].detail["execution_options"]
            == result.receipt.execution_options
        )

    def test_reviewable_path_rows_expose_segment_review_state_and_receipt_options(
        self,
        config,
        graph,
    ):
        rel = graph.get_relationship("Part", "BP-5678", "Vehicle", "V-CIVIC", "fits")
        assert rel is not None
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=_metadata(review_status="pending"),
            edge_key=rel.edge_key,
        )
        config.named_queries["reviewable_path_receipt"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    alias="fitment",
                )
            ],
            returns="list[Part]",
            relationship_state="reviewable",
        )

        result = execute_query(
            config,
            graph,
            "reviewable_path_receipt",
            {"vehicle_id": "V-CIVIC"},
        )

        assert result.result_shape == "path"
        assert result.dedupe == "path"
        assert result.receipt is not None
        assert result.receipt.execution_options == {
            "relationship_state": "reviewable",
            "relationship_state_source": "query_config",
            "result_shape": "path",
            "dedupe": "path",
        }
        statuses = {
            row.result.entity_id: row.path[0].metadata.assertion.review.status
            for row in result.results
            if isinstance(row, QueryPathRow)
        }
        assert statuses == {"BP-1234": "unreviewed", "BP-5678": "pending"}

    def test_receipt_records_structured_predicate_summary(self, config, graph):
        config.named_queries["predicate_summary_receipt"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep.model_validate(
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                        "where": {
                            "edge.properties.verified": {"eq": True},
                        },
                        "where_related": [
                            {
                                "relationship": "suppressed_fit",
                                "direction": "outgoing",
                                "target": {"entity_id": {"eq": "$entry.entity_id"}},
                            }
                        ],
                        "where_not_related": [
                            {
                                "relationship": "vehicle_blocks_part",
                                "direction": "incoming",
                                "source": {"entity_id": {"eq": "$entry.entity_id"}},
                            }
                        ],
                    }
                )
            ],
            returns="list[Part]",
        )

        result = execute_query(
            config,
            graph,
            "predicate_summary_receipt",
            {"vehicle_id": "V-CIVIC"},
        )

        assert result.receipt is not None
        filter_summary = result.receipt.nodes[0].detail["filter_summary"]
        assert filter_summary == [
            {
                "step": 0,
                "relationship": "fits",
                "direction": "incoming",
                "required": True,
                "where": {"edge.properties.verified": {"eq": True}},
                "where_related": [
                    {
                        "relationship": "suppressed_fit",
                        "direction": "outgoing",
                        "target": {"entity_id": {"eq": "$entry.entity_id"}},
                    }
                ],
                "where_not_related": [
                    {
                        "relationship": "vehicle_blocks_part",
                        "direction": "incoming",
                        "source": {"entity_id": {"eq": "$entry.entity_id"}},
                    }
                ],
            }
        ]

    def test_pending_relationship_query_can_be_approved_into_live_state(self, config, graph):
        rel = graph.get_relationship("Part", "BP-5678", "Vehicle", "V-CIVIC", "fits")
        assert rel is not None
        graph.update_relationship_state(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            metadata=_metadata(review_status="pending"),
            edge_key=rel.edge_key,
        )
        config.named_queries["pending_fit_edges"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="fits",
            result_shape="relationship",
            relationship_state="pending",
        )
        config.named_queries["accepted_fit_edges"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="fits",
            result_shape="relationship",
            relationship_state="accepted",
        )
        config.named_queries["live_fit_edges"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="fits",
            result_shape="relationship",
        )

        pending = execute_query(
            config,
            graph,
            "pending_fit_edges",
            {"vehicle_id": "V-CIVIC"},
        )

        assert len(pending.results) == 1
        row = pending.results[0]
        assert isinstance(row, QueryRelationshipRow)
        assert row.edge_key is not None
        assert row.metadata.assertion.review.status == "pending"
        assert row.from_entity is not None
        assert row.to_entity is not None

        applied = apply_feedback(
            graph,
            FeedbackRecord(
                receipt_id=pending.receipt.receipt_id,
                action="approve",
                target=row,
            ),
        )
        assert applied is True

        approved_rel = graph.get_relationship(
            "Part",
            "BP-5678",
            "Vehicle",
            "V-CIVIC",
            "fits",
            edge_key=row.edge_key,
        )
        assert approved_rel is not None
        assert approved_rel.metadata.assertion.review.status == "approved"

        accepted = execute_query(
            config,
            graph,
            "accepted_fit_edges",
            {"vehicle_id": "V-CIVIC"},
        )
        live = execute_query(
            config,
            graph,
            "live_fit_edges",
            {"vehicle_id": "V-CIVIC"},
        )
        pending_after = execute_query(
            config,
            graph,
            "pending_fit_edges",
            {"vehicle_id": "V-CIVIC"},
        )

        assert [row.edge_key for row in accepted.results] == [approved_rel.edge_key]
        assert approved_rel.edge_key in [row.edge_key for row in live.results]
        assert pending_after.results == []


class TestRelationshipResults:
    def test_outgoing_relationship_rows_include_metadata_and_endpoint_payloads(
        self,
        config,
        graph,
    ):
        config.named_queries["fit_edges_for_part"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Part",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="outgoing",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="fits",
            result_shape="relationship",
            dedupe="path",
        )

        result = execute_query(
            config,
            graph,
            "fit_edges_for_part",
            {"part_number": "BP-1234"},
        )

        assert result.result_shape == "relationship"
        rows = sorted(result.results, key=lambda row: row.to_id)
        assert all(isinstance(row, QueryRelationshipRow) for row in rows)
        row = next(row for row in rows if row.to_id == "V-CIVIC")
        assert row.relationship_type == "fits"
        assert row.from_type == "Part"
        assert row.from_id == "BP-1234"
        assert row.to_type == "Vehicle"
        assert row.to_id == "V-CIVIC"
        assert row.edge_key is not None
        assert row.properties["verified"] is True
        assert row.metadata.assertion.review.status == "unreviewed"
        assert row.entry.entity_type == "Part"
        assert row.entry.properties["part_number"] == "BP-1234"
        assert row.from_entity is not None
        assert row.from_entity.properties["name"] == "Ceramic Brake Pad"
        assert row.to_entity is not None
        assert row.to_entity.properties["make"] == "Honda"

    def test_incoming_relationship_rows(self, config, graph):
        config.named_queries["fit_edges_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="fits",
            result_shape="relationship",
            dedupe="path",
        )

        result = execute_query(
            config,
            graph,
            "fit_edges_for_vehicle",
            {"vehicle_id": "V-CIVIC"},
        )

        rows = sorted(result.results, key=lambda row: row.from_id)
        assert [row.from_id for row in rows] == ["BP-1234", "BP-5678"]
        assert all(isinstance(row, QueryRelationshipRow) for row in rows)
        assert all(row.to_id == "V-CIVIC" for row in rows)
        assert all(row.entry.entity_id == "V-CIVIC" for row in rows)

    def test_parallel_relationship_rows_are_distinguished_by_edge_key(
        self,
        config,
        graph,
    ):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"verified": True, "confidence": 0.99},
            )
        )
        config.named_queries["fit_edges_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="fits",
            result_shape="relationship",
            dedupe="path",
        )

        result = execute_query(
            config,
            graph,
            "fit_edges_for_vehicle",
            {"vehicle_id": "V-CIVIC"},
        )

        bp1234_rows = [
            row
            for row in result.results
            if isinstance(row, QueryRelationshipRow) and row.from_id == "BP-1234"
        ]
        assert len(bp1234_rows) == 2
        assert len({row.edge_key for row in bp1234_rows}) == 2

    def test_relationship_query_defaults_to_path_dedupe(self, config, graph):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1234",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"verified": True, "confidence": 0.99},
            )
        )
        config.named_queries["fit_edges_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="fits",
            result_shape="relationship",
        )

        result = execute_query(
            config,
            graph,
            "fit_edges_for_vehicle",
            {"vehicle_id": "V-CIVIC"},
        )

        assert result.dedupe == "path"
        bp1234_rows = [
            row
            for row in result.results
            if isinstance(row, QueryRelationshipRow) and row.from_id == "BP-1234"
        ]
        assert len(bp1234_rows) == 2

    def test_relationship_query_rejects_returns_mismatch_at_runtime(
        self,
        config,
        graph,
    ):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="fits",
            result_shape="relationship",
        )
        query.returns = "not_fits"
        config.named_queries["bad_fit_edges"] = query

        with pytest.raises(
            QueryExecutionError,
            match="must set returns to its final relationship type",
        ):
            execute_query(
                config,
                graph,
                "bad_fit_edges",
                {"vehicle_id": "V-CIVIC"},
            )
