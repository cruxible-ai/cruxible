"""Mutable state shared by workflow step handlers during one execution."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ProcedureBudgetExceededError
from cruxible_core.governance.actors import GovernedActorContext
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
class ProcedureExecutionBudget:
    """Mutable exact accounting and deadline state for one procedure run."""

    wall_clock_s: float
    max_provider_calls: int
    started_monotonic: float
    clock: Callable[[], float] = time.monotonic
    provider_calls: int = 0

    def elapsed_s(self) -> float:
        return max(0.0, self.clock() - self.started_monotonic)

    def remaining_wall_clock_s(self) -> float:
        return self.wall_clock_s - self.elapsed_s()

    def check_wall_clock(self) -> None:
        if self.remaining_wall_clock_s() <= 0:
            raise ProcedureBudgetExceededError(
                f"Procedure wall-clock budget of {self.wall_clock_s}s was exceeded"
            )

    def before_provider_invocation(self) -> float:
        self.check_wall_clock()
        if self.provider_calls >= self.max_provider_calls:
            raise ProcedureBudgetExceededError(
                f"Procedure provider-call budget exceeded: maximum {self.max_provider_calls}"
            )
        remaining = self.remaining_wall_clock_s()
        if remaining <= 0:
            raise ProcedureBudgetExceededError(
                f"Procedure wall-clock budget of {self.wall_clock_s}s was exceeded"
            )
        self.provider_calls += 1
        return remaining


@dataclass
class WorkflowExecutionContext:
    """Execution state shared by the coordinator and step handlers."""

    instance: InstanceProtocol
    config: CoreConfig
    workflow_name: str
    workflow: Any
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
    actor_context: GovernedActorContext | None = None
    procedure_budget: ProcedureExecutionBudget | None = None
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

    def check_procedure_wall_clock(self) -> None:
        if self.procedure_budget is not None:
            self.procedure_budget.check_wall_clock()

    def before_provider_invocation(self) -> float | None:
        if self.procedure_budget is None:
            return None
        return self.procedure_budget.before_provider_invocation()
