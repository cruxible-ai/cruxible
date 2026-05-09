"""Query engine: execute named queries from config against an EntityGraph.

Traversal model:
- Start at an entry entity (resolved from params via primary key)
- Each TraversalStep follows one or more relationships (fan-out),
  applying edge filters and target entity constraints
- Steps chain: output entities of step N become input for step N+1
- max_depth controls how many hops a single step traverses (BFS)
- Final step output is the query result
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from cruxible_core.config.property_validation import (
    entity_properties_with_identity,
    entity_with_identity_properties,
)
from cruxible_core.errors import (
    EntityNotFoundError,
    QueryExecutionError,
    QueryNotFoundError,
    RelationshipNotFoundError,
)
from cruxible_core.graph.types import REJECTED_STATUSES, EntityInstance
from cruxible_core.predicate import COMPARISON_SYMBOL_PATTERN, evaluate_comparison
from cruxible_core.query.filters import matches_exact_filter
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.types import Receipt

if TYPE_CHECKING:
    from cruxible_core.config.schema import CoreConfig, TraversalStep
    from cruxible_core.graph.entity_graph import EntityGraph


class QueryResult(BaseModel):
    """Result of executing a named query."""

    query_name: str
    parameters: dict[str, Any]
    results: list[EntityInstance]
    steps_executed: int
    total_results: int | None = None
    receipt: Receipt | None = None
    policy_summary: dict[str, int] = Field(default_factory=dict)

    def model_post_init(self, _context: Any) -> None:
        if self.total_results is None:
            self.total_results = len(self.results)


def _matches_filter(entity_props: dict[str, Any], filter_spec: dict[str, Any]) -> bool:
    """Backward-compatible alias for the shared exact-match helper."""
    return matches_exact_filter(entity_props, filter_spec)


def execute_query(
    config: CoreConfig,
    graph: EntityGraph,
    query_name: str,
    params: dict[str, Any],
) -> QueryResult:
    """Execute a named query from the config against the graph.

    Resolves the entry entity from params using the entry_point type's
    primary key, then chains traversal steps. Builds a receipt DAG
    recording every lookup, traversal, filter, and constraint.

    Args:
        config: Config with named query definitions
        graph: Populated graph to query
        query_name: Name of the query in config.named_queries
        params: Query parameters (must include entry entity ID)

    Returns:
        QueryResult with matching entities and a Receipt
    """
    query_schema = config.named_queries.get(query_name)
    if query_schema is None:
        raise QueryNotFoundError(query_name)

    builder = ReceiptBuilder(query_name=query_name, parameters=params)

    entry_entity = _resolve_entry_entity(
        config,
        graph,
        query_schema.entry_point,
        params,
        builder=builder,
    )

    current_entities = [entry_entity]
    current_parent_ids: list[str] | None = None
    steps_executed = 0
    policy_summary: dict[str, int] = {}

    for step in query_schema.traversal:
        current_entities, current_parent_ids = _execute_step(
            config,
            graph,
            step,
            current_entities,
            params,
            query_name=query_name,
            policy_summary=policy_summary,
            builder=builder,
        )
        steps_executed += 1

    result_entities = [
        entity_with_identity_properties(config, entity) for entity in current_entities
    ]
    result_dicts = [e.model_dump() for e in result_entities]
    builder.record_results(result_dicts, parent_ids=current_parent_ids)
    receipt = builder.build(result_dicts)

    return QueryResult(
        query_name=query_name,
        parameters=params,
        results=result_entities,
        steps_executed=steps_executed,
        receipt=receipt,
        policy_summary=policy_summary,
    )


def _resolve_entry_entity(
    config: CoreConfig,
    graph: EntityGraph,
    entry_point: str,
    params: dict[str, Any],
    *,
    builder: ReceiptBuilder | None = None,
) -> EntityInstance:
    """Find the entry entity using the primary key from params."""
    entity_schema = config.get_entity_type(entry_point)
    if entity_schema is None:
        raise QueryExecutionError(f"Entry point entity type '{entry_point}' not in config")

    pk = entity_schema.get_primary_key()
    if pk is None:
        raise QueryExecutionError(f"Entity type '{entry_point}' has no primary key")

    entity_id = params.get(pk)
    if entity_id is None:
        raise QueryExecutionError(
            f"Parameter '{pk}' required for entry point '{entry_point}'. "
            f"Got params: {sorted(params.keys())}"
        )

    entity = graph.get_entity(entry_point, str(entity_id))
    if entity is None:
        raise EntityNotFoundError(entry_point, str(entity_id))

    if builder is not None:
        builder.record_entity_lookup(
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
        )

    return entity


def _execute_step(
    config: CoreConfig,
    graph: EntityGraph,
    step: TraversalStep,
    current_entities: list[EntityInstance],
    params: dict[str, Any],
    query_name: str,
    policy_summary: dict[str, int],
    *,
    builder: ReceiptBuilder | None = None,
) -> tuple[list[EntityInstance], list[str] | None]:
    """Execute one traversal step via BFS with multi-relationship fan-out.

    Supports multiple relationship types per step and multi-hop traversal
    via max_depth. Three dedup layers:
      1. Expansion dedup: never expand the same node twice
      2. Result dedup: each entity appears once in output (first path owns lineage)
      3. Evidence: all traversal edges recorded in receipt regardless of dedup
    """
    # Validate and resolve all relationship references up front.
    resolved_refs: list[tuple[str, str]] = []
    for rel_ref in step.relationship_types:
        resolved = config.resolve_relationship_reference(rel_ref)
        if resolved is None:
            raise RelationshipNotFoundError(rel_ref)
        rel_schema, is_reverse = resolved
        direction = _flip_relationship_direction(step.direction) if is_reverse else step.direction
        resolved_refs.append((rel_schema.name, direction))
    step_policies = {
        rel_name: _active_query_policies(config, query_name, rel_name)
        for rel_name, _ in resolved_refs
    }

    next_entities: list[EntityInstance] = []
    next_parent_ids: list[str] = []

    # BFS state
    # Queue entries: (entity, current_depth, parent_traversal_id)
    queue: deque[tuple[EntityInstance, int, str | None]] = deque()
    seen_expanded: set[str] = set()  # nodes already expanded (neighbors queried)
    seen_results: set[str] = set()  # nodes already in result list

    # Seed queue with input entities (they are inputs, not results)
    for entity in current_entities:
        nid = entity.node_id()
        seen_expanded.add(nid)
        seen_results.add(nid)
        queue.append((entity, 0, None))

    while queue:
        entity, depth, parent_tid = queue.popleft()

        if depth >= step.max_depth:
            continue

        for rel_type, direction in resolved_refs:
            rel_policies = step_policies.get(rel_type, [])
            neighbors = graph.get_neighbors_with_edge_refs(
                entity.entity_type,
                entity.entity_id,
                relationship_type=rel_type,
                direction=direction,
            )

            for neighbor, edge_props, edge_key in neighbors:
                if edge_props.get("review_status") in REJECTED_STATUSES:
                    continue

                nid = neighbor.node_id()

                # Record evidence regardless of dedup
                traversal_id = None
                if builder is not None:
                    traversal_id = builder.record_traversal(
                        from_entity_type=entity.entity_type,
                        from_entity_id=entity.entity_id,
                        to_entity_type=neighbor.entity_type,
                        to_entity_id=neighbor.entity_id,
                        relationship=rel_type,
                        edge_props=edge_props,
                        edge_key=edge_key,
                        parent_id=parent_tid,
                    )

                # Apply edge filter (blocks subtree on failure)
                if step.filter:
                    passed = matches_exact_filter(edge_props, step.filter)
                    if builder is not None and traversal_id is not None:
                        builder.record_filter(
                            filter_spec=step.filter,
                            passed=passed,
                            parent_id=traversal_id,
                        )
                    if not passed:
                        continue

                # Apply target entity filter (blocks subtree on failure)
                if step.target_filter:
                    passed = matches_exact_filter(
                        entity_properties_with_identity(
                            config,
                            neighbor.entity_type,
                            neighbor.entity_id,
                            neighbor.properties,
                        ),
                        step.target_filter,
                    )
                    if builder is not None and traversal_id is not None:
                        builder.record_filter(
                            filter_spec=step.target_filter,
                            passed=passed,
                            parent_id=traversal_id,
                        )
                    if not passed:
                        continue

                # Apply constraint (blocks subtree on failure)
                if step.constraint:
                    passed = _evaluate_constraint(
                        config,
                        step.constraint,
                        neighbor,
                        params,
                    )
                    if builder is not None and traversal_id is not None:
                        builder.record_constraint(
                            constraint=step.constraint,
                            passed=passed,
                            entity_type=neighbor.entity_type,
                            entity_id=neighbor.entity_id,
                            parent_id=traversal_id,
                        )
                    if not passed:
                        continue

                if step.exclude_if_related:
                    excluded = False
                    for exclusion in step.exclude_if_related:
                        passed = not _related_edge_exists(
                            graph,
                            current_entity=entity,
                            candidate_entity=neighbor,
                            relationship_type=exclusion.relationship,
                            direction=exclusion.direction,
                        )
                        if builder is not None and traversal_id is not None:
                            builder.record_filter(
                                filter_spec={
                                    "exclude_if_related": {
                                        "relationship": exclusion.relationship,
                                        "direction": exclusion.direction,
                                    }
                                },
                                passed=passed,
                                parent_id=traversal_id,
                            )
                        if not passed:
                            excluded = True
                            break
                    if excluded:
                        continue

                if direction == "outgoing":
                    policy_from_entity = entity
                    policy_to_entity = neighbor
                else:
                    policy_from_entity = neighbor
                    policy_to_entity = entity

                if rel_policies and _policy_should_suppress(
                    config=config,
                    policies=rel_policies,
                    from_entity=policy_from_entity,
                    to_entity=policy_to_entity,
                    edge_props=edge_props,
                    context={
                        "query_name": query_name,
                        "relationship_type": rel_type,
                        "direction": direction,
                    },
                    policy_summary=policy_summary,
                    builder=builder,
                    parent_id=traversal_id,
                ):
                    continue

                # Result dedup: first path owns the lineage
                if nid not in seen_results:
                    seen_results.add(nid)
                    next_entities.append(neighbor)
                    if traversal_id is not None:
                        next_parent_ids.append(traversal_id)

                # Expansion dedup: enqueue for deeper hops if not yet expanded
                if nid not in seen_expanded:
                    seen_expanded.add(nid)
                    queue.append((neighbor, depth + 1, traversal_id))

    return next_entities, next_parent_ids or None


def _related_edge_exists(
    graph: EntityGraph,
    *,
    current_entity: EntityInstance,
    candidate_entity: EntityInstance,
    relationship_type: str,
    direction: str,
) -> bool:
    """Check whether a live related edge exists for a current/candidate pair."""
    if direction == "outgoing":
        return graph.has_live_relationship(
            current_entity.entity_type,
            current_entity.entity_id,
            candidate_entity.entity_type,
            candidate_entity.entity_id,
            relationship_type,
        )
    if direction == "incoming":
        return graph.has_live_relationship(
            candidate_entity.entity_type,
            candidate_entity.entity_id,
            current_entity.entity_type,
            current_entity.entity_id,
            relationship_type,
        )
    return graph.has_live_relationship(
        current_entity.entity_type,
        current_entity.entity_id,
        candidate_entity.entity_type,
        candidate_entity.entity_id,
        relationship_type,
    ) or graph.has_live_relationship(
        candidate_entity.entity_type,
        candidate_entity.entity_id,
        current_entity.entity_type,
        current_entity.entity_id,
        relationship_type,
    )


def _flip_relationship_direction(
    direction: str,
) -> str:
    if direction == "outgoing":
        return "incoming"
    if direction == "incoming":
        return "outgoing"
    return direction


def _active_query_policies(
    config: CoreConfig,
    query_name: str,
    relationship_type: str,
) -> list[Any]:
    """Return non-expired query policies applicable to one traversal relationship."""
    return [
        policy
        for policy in config.decision_policies
        if policy.applies_to == "query"
        and policy.query_name == query_name
        and policy.relationship_type == relationship_type
        and not _policy_expired(policy.expires_at)
    ]


def _policy_expired(expires_at: str | None) -> bool:
    """Return True when a policy should no longer apply."""
    if not expires_at:
        return False
    try:
        normalized = expires_at.replace("Z", "+00:00")
        expiry = datetime.fromisoformat(normalized)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry < datetime.now(timezone.utc)
    except ValueError:
        return False


def _policy_should_suppress(
    *,
    config: CoreConfig,
    policies: list[Any],
    from_entity: EntityInstance,
    to_entity: EntityInstance,
    edge_props: dict[str, Any],
    context: dict[str, Any],
    policy_summary: dict[str, int],
    builder: ReceiptBuilder | None,
    parent_id: str | None,
) -> bool:
    """Apply query-side suppress policies to one traversed edge."""
    for policy in policies:
        from_props = entity_properties_with_identity(
            config, from_entity.entity_type, from_entity.entity_id, from_entity.properties
        )
        to_props = entity_properties_with_identity(
            config, to_entity.entity_type, to_entity.entity_id, to_entity.properties
        )
        if not matches_exact_filter(from_props, policy.match.from_match):
            continue
        if not matches_exact_filter(to_props, policy.match.to):
            continue
        if not matches_exact_filter(edge_props, policy.match.edge):
            continue
        if not matches_exact_filter(context, policy.match.context):
            continue
        policy_summary[policy.name] = policy_summary.get(policy.name, 0) + 1
        if builder is not None and parent_id is not None:
            builder.record_validation(
                passed=True,
                detail={
                    "policy_name": policy.name,
                    "policy_effect": policy.effect,
                    "applies_to": "query",
                },
                parent_id=parent_id,
            )
        return True
    return False


_CONSTRAINT_RE = re.compile(
    rf"^(target|source)\.([\w-]+)\s*{COMPARISON_SYMBOL_PATTERN}\s*(.+)$"
)


def _evaluate_constraint(
    config: CoreConfig,
    constraint: str,
    target_entity: EntityInstance,
    params: dict[str, Any],
) -> bool:
    """Evaluate a simple constraint expression.

    Supported format: "target.<property> <op> $<param>" or literal.
    Source-side constraints are rejected because traversal source semantics
    depend on relationship direction and are not implemented.

    Examples:
        "target.vehicle_id == $vehicle_id"
        "target.category != brakes"
        "target.year >= 2024"
    """
    match = _CONSTRAINT_RE.match(constraint.strip())
    if match is None:
        return True  # Unknown constraint format — don't filter

    side, prop, operator, rhs = match.groups()
    rhs = rhs.strip()

    if side == "target":
        lhs_value = entity_properties_with_identity(
            config, target_entity.entity_type, target_entity.entity_id, target_entity.properties
        ).get(prop)
    else:
        raise QueryExecutionError(
            "source-side traversal constraints are not supported; "
            "use target.<property> constraints"
        )

    if rhs.startswith("$"):
        rhs_value = params.get(rhs[1:])
    else:
        rhs_value = _parse_literal(rhs)

    return evaluate_comparison(lhs_value, operator, rhs_value)


def _parse_literal(value: str) -> Any:
    """Parse a literal value from a constraint string."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    # Strip quotes if present
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value
