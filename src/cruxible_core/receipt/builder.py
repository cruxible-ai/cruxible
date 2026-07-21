"""Receipt builder: records evidence during query execution.

The builder is created before a query runs, collects nodes and edges
as the engine traverses the graph, and produces a Receipt at the end.
"""

from __future__ import annotations

import time
from typing import Any

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.primitives import new_id
from cruxible_core.receipt.mutation_payloads import (
    MutationPayloadRetention,
    retain_mutation_payload,
)
from cruxible_core.receipt.types import (
    EdgeType,
    EvidenceEdge,
    OperationType,
    Receipt,
    ReceiptNode,
    WorkflowReceiptMode,
)


class ReceiptBuilder:
    """Collects evidence nodes and edges during query execution."""

    def __init__(
        self,
        query_name: str = "",
        parameters: dict[str, Any] | None = None,
        operation_type: OperationType = "query",
        head_snapshot_id: str | None = None,
        workflow_mode: WorkflowReceiptMode | None = None,
        execution_options: dict[str, Any] | None = None,
        root_detail: dict[str, Any] | None = None,
        actor_context: GovernedActorContext | None = None,
    ) -> None:
        # Pre-allocated so write paths can stamp the creating receipt id into
        # relationship provenance within the same transaction that persists
        # the receipt.
        self._receipt_id = new_id("RCP")
        self._query_name = query_name
        self._parameters = parameters or {}
        self._execution_options = execution_options or {}
        self._root_detail = root_detail or {}
        self._operation_type = operation_type
        self._head_snapshot_id = head_snapshot_id
        self._workflow_mode = workflow_mode
        # Token-derived actor identity for this operation, preserved onto the
        # built receipt where available. None on auth-off/local paths -- we do
        # not fabricate an actor context.
        self._actor_context = actor_context
        self._nodes: list[ReceiptNode] = []
        self._edges: list[EvidenceEdge] = []
        self._counter = 0
        self._start_ns = time.monotonic_ns()
        # "Committed" is intentionally conservative: it flips only after the
        # caller reaches the operation-specific durability boundary.
        self._committed = False

        if operation_type == "query":
            self._root_id = self._add_node(
                node_type="query",
                detail={
                    "query_name": query_name,
                    "parameters": self._parameters,
                    "execution_options": self._execution_options,
                    **self._root_detail,
                },
            )
        elif operation_type == "gate_evaluation":
            self._root_id = self._add_node(
                node_type="gate_evaluation",
                detail={
                    "gate_name": query_name,
                    "parameters": self._parameters,
                    **self._root_detail,
                },
            )
        elif operation_type == "workflow":
            self._root_id = self._add_node(
                node_type="workflow",
                detail={"workflow_name": query_name, "parameters": self._parameters},
            )
        else:
            self._root_id = self._add_node(
                node_type="mutation",
                detail={"operation_type": operation_type, "parameters": self._parameters},
            )

    def _next_id(self) -> str:
        self._counter += 1
        return f"n{self._counter}"

    def _add_node(self, **kwargs: Any) -> str:
        node_id = self._next_id()
        self._nodes.append(ReceiptNode(node_id=node_id, **kwargs))
        return node_id

    def _add_edge(self, from_node: str, to_node: str, edge_type: EdgeType) -> None:
        self._edges.append(EvidenceEdge(from_node=from_node, to_node=to_node, edge_type=edge_type))

    @property
    def root_id(self) -> str:
        return self._root_id

    @property
    def receipt_id(self) -> str:
        """The id the built receipt will carry, available before build()."""
        return self._receipt_id

    def record_entity_lookup(
        self,
        entity_type: str,
        entity_id: str,
        parent_id: str | None = None,
    ) -> str:
        """Record that an entity was looked up from the graph."""
        node_id = self._add_node(
            node_type="entity_lookup",
            entity_type=entity_type,
            entity_id=entity_id,
        )
        self._add_edge(parent_id or self._root_id, node_id, "consulted")
        return node_id

    def record_traversal(
        self,
        from_entity_type: str,
        from_entity_id: str,
        to_entity_type: str,
        to_entity_id: str,
        relationship: str,
        edge_props: dict[str, Any],
        edge_key: int | None = None,
        parent_id: str | None = None,
    ) -> str:
        """Record that an edge was traversed from one entity to another."""
        detail: dict[str, Any] = {
            "from_entity_type": from_entity_type,
            "from_entity_id": from_entity_id,
            "edge_properties": edge_props,
        }
        if edge_key is not None:
            detail["edge_key"] = edge_key

        node_id = self._add_node(
            node_type="edge_traversal",
            entity_type=to_entity_type,
            entity_id=to_entity_id,
            relationship=relationship,
            detail=detail,
        )
        self._add_edge(parent_id or self._root_id, node_id, "traversed")
        return node_id

    def record_filter(
        self,
        filter_spec: dict[str, Any],
        passed: bool,
        parent_id: str,
    ) -> str:
        """Record that a filter was applied to an edge."""
        node_id = self._add_node(
            node_type="filter_applied",
            detail={"filter": filter_spec, "passed": passed},
        )
        self._add_edge(parent_id, node_id, "filtered")
        return node_id

    def record_constraint(
        self,
        constraint: str,
        passed: bool,
        entity_type: str,
        entity_id: str,
        parent_id: str,
    ) -> str:
        """Record that a constraint was evaluated against an entity."""
        node_id = self._add_node(
            node_type="constraint_check",
            entity_type=entity_type,
            entity_id=entity_id,
            detail={"constraint": constraint, "passed": passed},
        )
        self._add_edge(parent_id, node_id, "evaluated")
        return node_id

    def record_results(
        self,
        results: list[dict[str, Any]],
        parent_ids: list[str] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        """Record the final query results."""
        node_id = self._add_node(
            node_type="result",
            detail={"count": len(results), **(detail or {})},
        )
        for pid in parent_ids or [self._root_id]:
            self._add_edge(pid, node_id, "produced")
        return node_id

    def record_plan_step(
        self,
        step_id: str,
        kind: str,
        detail: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> str:
        """Record a workflow plan step with step-specific detail."""
        node_id = self._add_node(
            node_type="plan_step",
            detail={"step_id": step_id, "kind": kind, **(detail or {})},
        )
        self._add_edge(parent_id or self._root_id, node_id, "produced")
        return node_id

    def mark_committed(self) -> None:
        """Mark the operation as having reached its durable state boundary."""
        self._committed = True

    def apply_mutation_payload_retention(
        self,
        *,
        retention: MutationPayloadRetention = "metadata",
    ) -> None:
        """Reduce the mutation payload to the mode's canonical representation.

        The mutation payload is the receipt ``parameters`` dict. Every retention
        mode stamps the content-addressed ``payload_digest`` and ``byte_count``
        (computed over the ORIGINAL full payload) onto the root mutation node's
        ``payload_metadata``. No-op for non-mutation receipts -- query/workflow
        receipts keep their full ``parameters`` untouched, which keeps the
        workflow preview->apply path safe.

        The reduced representation becomes the SINGLE canonical payload value
        carried by EVERY persisted copy of the receipt:

        * It replaces top-level ``self._parameters``, so ``build()`` propagates it
          to both the ``receipts.parameters`` column and the ``receipt_json``
          top-level ``.parameters``.
        * It replaces the root mutation node's ``detail["parameters"]``.

        Per mode (option "b", regardless of payload size):

        * ``metadata`` -- digest + byte_count only; never any body.
        * ``preview``  -- digest + byte_count + bounded structural preview; never
          the raw full body.
        * ``full``     -- the complete body, retained inline verbatim.
        """
        if self._operation_type in ("query", "gate_evaluation", "workflow"):
            return
        root = self._nodes[0]
        if root.node_type != "mutation":
            return
        retained_payload, metadata = retain_mutation_payload(
            self._parameters,
            retention=retention,
        )
        root.payload_metadata = metadata
        # The reduced representation is canonical: carry it on the root node AND
        # on the top-level parameters (picked up by build()), so no persisted
        # copy retains a body the mode is supposed to shed.
        self._parameters = retained_payload
        root.detail = {
            "operation_type": self._operation_type,
            "parameters": retained_payload,
        }

    def record_validation(
        self,
        passed: bool,
        detail: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> str:
        """Record a validation check result."""
        node_id = self._add_node(
            node_type="validation",
            detail={"passed": passed, **(detail or {})},
        )
        self._add_edge(parent_id or self._root_id, node_id, "validated")
        return node_id

    def record_entity_write(
        self,
        entity_type: str,
        entity_id: str,
        is_update: bool,
        detail: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> str:
        """Record that an entity was written to the graph."""
        node_id = self._add_node(
            node_type="entity_write",
            entity_type=entity_type,
            entity_id=entity_id,
            detail={"is_update": is_update, **(detail or {})},
        )
        self._add_edge(parent_id or self._root_id, node_id, "mutated")
        return node_id

    def record_relationship_write(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship: str,
        is_update: bool,
        detail: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> str:
        """Record that a relationship was written to the graph."""
        node_detail: dict[str, Any] = {
            "from_type": from_type,
            "from_id": from_id,
            "to_type": to_type,
            "to_id": to_id,
            "relationship": relationship,
            "is_update": is_update,
        }
        if detail:
            node_detail.update(detail)
        node_id = self._add_node(
            node_type="relationship_write",
            detail=node_detail,
        )
        self._add_edge(parent_id or self._root_id, node_id, "mutated")
        return node_id

    def record_feedback_applied(
        self,
        target_str: str,
        action: str,
        applied: bool,
        parent_id: str | None = None,
    ) -> str:
        """Record that feedback was applied (or not) to an edge."""
        node_id = self._add_node(
            node_type="feedback_applied",
            detail={"target": target_str, "action": action, "applied": applied},
        )
        self._add_edge(parent_id or self._root_id, node_id, "applied")
        return node_id

    def build(self, results: list[dict[str, Any]] | None = None) -> Receipt:
        """Finalize and return the receipt."""
        elapsed_ms = (time.monotonic_ns() - self._start_ns) / 1_000_000
        return Receipt(
            receipt_id=self._receipt_id,
            query_name=self._query_name,
            parameters=self._parameters,
            execution_options=self._execution_options,
            nodes=list(self._nodes),
            edges=list(self._edges),
            results=results or [],
            duration_ms=round(elapsed_ms, 3),
            operation_type=self._operation_type,
            head_snapshot_id=self._head_snapshot_id,
            workflow_mode=self._workflow_mode,
            committed=self._committed,
            actor_context=self._actor_context,
        )
