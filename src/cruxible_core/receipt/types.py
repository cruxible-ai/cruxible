"""Receipt types: a DAG of evidence showing how a query result was derived.

A receipt is a structured proof — not a log, not a trace. It records which
entities were consulted, which edges were traversed, which filters/constraints
passed or failed, and what produced the final result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from cruxible_core.primitives import new_id

OperationType = Literal[
    "query",
    "workflow",
    "add_entity",
    "add_relationship",
    "feedback",
    "feedback_batch",
    "group_propose",
    "group_rewrite",
    "group_clear",
    "group_resolve",
]
"""Coarse-grained category of operation that produced a receipt."""

WorkflowReceiptMode = Literal["preview", "apply", "proposal"]
"""Workflow execution mode recorded on workflow receipts."""

NodeType = Literal[
    "query",
    "workflow",
    "entity_lookup",
    "edge_traversal",
    "filter_applied",
    "constraint_check",
    "result",
    "plan_step",
    "mutation",
    "validation",
    "entity_write",
    "relationship_write",
    "feedback_applied",
]
"""Fine-grained kind of node within the receipt DAG."""

EdgeType = Literal[
    "consulted",
    "traversed",
    "filtered",
    "evaluated",
    "produced",
    "validated",
    "mutated",
    "applied",
]
"""Relation between two nodes in the receipt DAG."""


class ReceiptNode(BaseModel):
    """A single node in the receipt DAG."""

    node_id: str
    node_type: NodeType
    entity_type: str | None = None
    entity_id: str | None = None
    relationship: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceEdge(BaseModel):
    """A directed edge in the receipt DAG connecting two nodes."""

    from_node: str
    to_node: str
    edge_type: EdgeType


class Receipt(BaseModel):
    """A complete receipt for a Cruxible operation."""

    receipt_id: str = Field(default_factory=lambda: new_id("RCP"))
    query_name: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    nodes: list[ReceiptNode]
    edges: list[EvidenceEdge]
    results: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = 0.0
    operation_type: OperationType = "query"
    head_snapshot_id: str | None = Field(
        default=None,
        description="Instance head snapshot observed when the operation began, if available.",
    )
    workflow_mode: WorkflowReceiptMode | None = Field(
        default=None,
        description="Workflow receipt mode; unset for non-workflow receipts.",
    )
    committed: bool = Field(
        default=False,
        description=(
            "Whether the operation reached its Cruxible durability boundary. "
            "For read-only operations this is normally false and does not indicate failure."
        ),
    )
