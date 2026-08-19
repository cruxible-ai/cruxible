"""Receipt tree: the structured evidence DAG produced by a Cruxible operation.

Rehomed out of ``cruxible_core.workflow`` in PC-F. A receipt is a proof, not a
log: it records which entities were consulted, which edges were traversed,
which filters and constraints passed or failed, and what produced the final
result. The behavior is unchanged by the move -- ``Receipt`` and every node,
edge, and payload-retention shape it carries are byte-identical to what the
workflow donor built.

The move is a lifetime fix, not a redesign. ``cruxible_core.workflow`` retires
with the rest of the PC-F donor island, while ``cruxible_core.procedure`` keeps
carrying a ``Receipt`` on every finalized run until PC-H. Living under the
donor made the shorter-lived package own the longer-lived type. This package
imports no donor module, so it survives the purge on its own.

This is NOT the Playbill canonical query receipt. Canonical Claim-native reads
receipt through ``cruxible_core.playbill.query.engine.QueryExecutionReceiptV1``
and the ``query-receipt-journal-v1`` exhaust; the two are unrelated shapes with
unrelated authority.
"""

from __future__ import annotations

from cruxible_core.receipt_tree.builder import ReceiptBuilder
from cruxible_core.receipt_tree.payloads import (
    MAX_RETAINED_PAYLOAD_BYTES,
    RETAINED_PAYLOAD_HEAD_BYTES,
    MutationPayloadMetadata,
    MutationPayloadRetention,
    compute_payload_digest,
    retain_mutation_payload,
)
from cruxible_core.receipt_tree.serializer import to_json, to_markdown, to_mermaid
from cruxible_core.receipt_tree.types import (
    EdgeType,
    EvidenceEdge,
    NodeType,
    OperationType,
    Receipt,
    ReceiptNode,
    WorkflowReceiptMode,
)

__all__ = [
    "MAX_RETAINED_PAYLOAD_BYTES",
    "RETAINED_PAYLOAD_HEAD_BYTES",
    "EdgeType",
    "EvidenceEdge",
    "MutationPayloadMetadata",
    "MutationPayloadRetention",
    "NodeType",
    "OperationType",
    "Receipt",
    "ReceiptBuilder",
    "ReceiptNode",
    "WorkflowReceiptMode",
    "compute_payload_digest",
    "retain_mutation_payload",
    "to_json",
    "to_markdown",
    "to_mermaid",
]
