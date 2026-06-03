"""Mutable state shared by workflow step handlers during one execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cruxible_core.config.schema import CoreConfig, WorkflowSchema
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.workflow.types import CompiledPlan, CompiledPlanStep, WorkflowLock
from cruxible_core.workflow_execution_types import (
    WorkflowExecutionAction,
    WorkflowResultMode,
)


@dataclass
class WorkflowExecutionContext:
    """Execution state shared by the coordinator and step handlers."""

    instance: InstanceProtocol
    config: CoreConfig
    workflow_name: str
    workflow: WorkflowSchema
    lock: WorkflowLock
    plan: CompiledPlan
    graph: EntityGraph
    receipt_builder: ReceiptBuilder
    execution_action: WorkflowExecutionAction
    result_mode: WorkflowResultMode
    persist_receipt: bool
    persist_query_receipts: bool
    persist_traces: bool
    config_base_path: Path
    head_snapshot_id: str | None
    step_outputs: dict[str, Any] = field(default_factory=dict)
    alias_step_ids: dict[str, str] = field(default_factory=dict)
    step_trace_ids: dict[str, list[str]] = field(default_factory=dict)
    query_receipt_ids: list[str] = field(default_factory=list)
    traces: list[ExecutionTrace] = field(default_factory=list)
    apply_previews: dict[str, Any] = field(default_factory=dict)
    applied_entities: dict[tuple[str, str], EntityInstance] = field(default_factory=dict)
    applied_relationships: dict[int, RelationshipInstance] = field(default_factory=dict)

    def output_key(self, compiled_step: CompiledPlanStep) -> str:
        """Return the public output key for a step, honoring aliases."""
        return compiled_step.as_name or compiled_step.step_id

    def set_step_output(self, compiled_step: CompiledPlanStep, value: Any) -> None:
        """Store a step output and keep alias bookkeeping in sync."""
        self.step_outputs[self.output_key(compiled_step)] = value
        if compiled_step.as_name is not None:
            self.alias_step_ids[compiled_step.as_name] = compiled_step.step_id
