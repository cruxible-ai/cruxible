"""Receipt types: a DAG of evidence showing how a query result was derived.

A receipt is a structured proof — not a log, not a trace. It records which
entities were consulted, which edges were traversed, which filters/constraints
passed or failed, and what produced the final result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.primitives import new_id
from cruxible_core.receipt.mutation_payloads import MutationPayloadMetadata
from cruxible_core.temporal import utc_now
from cruxible_core.workflow_execution_types import WorkflowResultMode

OperationType = Literal[
    "query",
    "gate_evaluation",
    "workflow",
    "procedure",
    "procedure_transition",
    "attestation",
    "attestation_disposition",
    "resolution_contract_open",
    "resolution_contract_resolve",
    "resolution_contract_disposition",
    "add_entity",
    "add_relationship",
    "batch_direct_write",
    "feedback",
    "feedback_batch",
    "group_propose",
    "group_rewrite",
    "group_withdraw",
    # Deprecated: read-only legacy. ``group_clear`` was renamed to
    # ``group_withdraw`` in 0.3 and is never written again, but 0.2.x instances
    # persisted receipts carrying it — dropping the literal made ``get_receipt``
    # raise on every one of them. Removed once no shipped 0.2.x receipt store
    # can still be read.
    "group_clear",
    "group_resolve",
    "group_trust_update",
    "state_pull_apply",
    # Reads are not side-effect-free: a state diff persists BOTH a receipt row
    # and a content-addressed artifact, and its coordinates + diff_digest ride
    # in ``parameters`` so "every diff taken against release R" is listable
    # without a schema change. Additive and untracked: the client types
    # ``operation_type`` as a plain ``str`` and the freeze guardrail pins
    # receipt FIELD NAMES, exempting the receipt shape.
    "state_diff",
    "config_add_constraint",
    "config_add_decision_policy",
    "decision_record_open",
    "decision_record_finalize",
    "decision_record_abandon",
    "source_artifact_register",
    "snapshot_create",
    "lifecycle_supersede",
    "lifecycle_retract",
    # Install ledger. Creating an install, claiming ownership of an object,
    # advancing a phase, and recording a customization verdict each mint their
    # own receipt rather than riding on one install-wide receipt: a
    # crash-recoverable install must be reconstructible step by step, not
    # attempt by attempt.
    "install_create",
    "install_record_owned_object",
    "install_phase_advance",
    "install_object_customization",
]
"""Coarse-grained category of operation that produced a receipt."""

WorkflowReceiptMode = WorkflowResultMode
"""Workflow result mode recorded on workflow receipts."""

NodeType = Literal[
    "query",
    "gate_evaluation",
    "workflow",
    "procedure",
    "entity_lookup",
    "edge_traversal",
    "filter_applied",
    "constraint_check",
    "result",
    "plan_step",
    "mutation",
    "proposal",
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
    "proposed",
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
    payload_metadata: MutationPayloadMetadata | None = Field(
        default=None,
        description=(
            "Retention metadata (content-addressed payload_digest + byte_count) "
            "for the mutation payload, stamped on the root mutation node. Unset "
            "for non-mutation nodes."
        ),
    )
    timestamp: datetime = Field(default_factory=utc_now)


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
    execution_options: dict[str, Any] = Field(default_factory=dict)
    nodes: list[ReceiptNode]
    edges: list[EvidenceEdge]
    results: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    duration_ms: float = 0.0
    operation_type: OperationType = "query"
    head_snapshot_id: str | None = Field(
        default=None,
        description="Instance head snapshot observed when the operation began, if available.",
    )
    read_revision: int | None = Field(
        default=None,
        description=(
            "Instance read revision observed when the operation began, if available. "
            "Together with head_snapshot_id this is the decision-time state coordinate "
            "of the operation. Receipts predating this field load with null and remain "
            "valid; the coordinate is ambiguous across snapshot-restore lineages."
        ),
    )
    workflow_mode: WorkflowReceiptMode | None = Field(
        default=None,
        description="Workflow result mode; unset for non-workflow receipts.",
    )
    committed: bool = Field(
        default=False,
        description=(
            "Whether the operation reached its Cruxible durability boundary. "
            "For read-only operations this is normally false and does not indicate failure."
        ),
    )
    actor_context: GovernedActorContext | None = Field(
        default=None,
        description=(
            "Runtime actor identity for the operation that produced this receipt, "
            "when available (credential-derived under auth-on daemons; the "
            "declared local operator under auth-off daemons). Receipts predating "
            "this field load with a null actor_context and remain valid."
        ),
    )
