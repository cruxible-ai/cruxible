"""Name-referenced graph format 7; format 6 remains a frozen read grammar.

The format-7 node models live in ``nodes`` and its slot and binding types in
``slots``; both are re-exported here unchanged.
"""

from __future__ import annotations

from typing import Literal

from cruxible_client.contracts.procedures import models as old
from cruxible_client.contracts.procedures.nodes import (
    NamedCallNode,
    NamedCaptureEgressNode,
    NamedClaimTapNode,
    NamedConstantNode,
    NamedExhaustTapNode,
    NamedInvokeNode,
    NamedProcedureNode,
    NamedProjectNode,
    NamedProposeChangeSetNode,
    NamedRepeatBodyNode,
    NamedRepeatNode,
    NamedReturnNode,
    NamedSelectNode,
    NamedSettleChangeSetNode,
    NamedSourceNode,
    NamedStateTapNode,
    NamedTransformNode,
)
from cruxible_client.contracts.procedures.slots import NameBinding, NamedProcedurePinSlot
from cruxible_client.contracts.procedures.source_program import NamedProcedureSourceProgram


class NamedProcedureDefinition(old.ProcedureDefinition):
    graph_format: Literal[7] = 7  # type: ignore[assignment]
    contract_in: NameBinding  # type: ignore[assignment]
    contract_out: NameBinding  # type: ignore[assignment]
    parameter_contract: NameBinding | None = None  # type: ignore[assignment]
    nodes: tuple[NamedProcedureNode, ...]
    pin_slots: tuple[NamedProcedurePinSlot, ...] = ()
    source: NamedProcedureSourceProgram | None = None


__all__ = [
    "NameBinding",
    "NamedCallNode",
    "NamedCaptureEgressNode",
    "NamedClaimTapNode",
    "NamedConstantNode",
    "NamedExhaustTapNode",
    "NamedInvokeNode",
    "NamedProcedureDefinition",
    "NamedProcedureNode",
    "NamedProcedurePinSlot",
    "NamedProjectNode",
    "NamedProposeChangeSetNode",
    "NamedRepeatBodyNode",
    "NamedRepeatNode",
    "NamedReturnNode",
    "NamedSelectNode",
    "NamedSettleChangeSetNode",
    "NamedSourceNode",
    "NamedStateTapNode",
    "NamedTransformNode",
]
