"""Tests for workflow step-handler registry seams."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from tests.support.workflow_helpers import dataflow_instance, write_lock_for_instance

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import StepKind
from cruxible_core.workflow import execute_workflow
from cruxible_core.workflow.execution_context import WorkflowExecutionContext
from cruxible_core.workflow.step_handlers import (
    DEFAULT_STEP_HANDLER_REGISTRY,
    WorkflowStepRegistry,
)
from cruxible_core.workflow.types import CompiledPlanStep


def test_default_registry_covers_every_step_kind_once() -> None:
    registered = DEFAULT_STEP_HANDLER_REGISTRY.registered_kinds

    assert registered == tuple(sorted(get_args(StepKind)))
    assert len(registered) == len(set(registered))


def test_registry_rejects_duplicate_registration() -> None:
    handler = DEFAULT_STEP_HANDLER_REGISTRY.execute
    registry = WorkflowStepRegistry()
    registry.register("query", handler)

    with pytest.raises(ValueError, match="Duplicate workflow step handler"):
        registry.register("query", handler)


def test_registry_rejects_missing_registrations() -> None:
    registry = WorkflowStepRegistry([("query", DEFAULT_STEP_HANDLER_REGISTRY.execute)])

    with pytest.raises(ValueError, match="Missing workflow step handler"):
        registry.validate_complete()


def test_registry_rejects_unknown_registration() -> None:
    registry = WorkflowStepRegistry()

    with pytest.raises(ValueError, match="Unknown workflow step kind"):
        registry.register("not_a_step_kind", DEFAULT_STEP_HANDLER_REGISTRY.execute)


def test_execute_workflow_dispatches_transform_step_through_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = dataflow_instance(
        tmp_path,
        steps_yaml="""
        - id: shaped
          shape_items:
            items:
              - id: A
                region: west
            fields:
              id: $item.id
              region: $item.region
          as: shaped
        """,
        returns="shaped",
    )
    seen: list[str] = []
    original_execute = DEFAULT_STEP_HANDLER_REGISTRY.execute

    def spy_execute(
        context: WorkflowExecutionContext,
        compiled_step: CompiledPlanStep,
    ) -> None:
        seen.append(compiled_step.kind)
        original_execute(context, compiled_step)

    monkeypatch.setattr(DEFAULT_STEP_HANDLER_REGISTRY, "execute", spy_execute)

    result = execute_workflow(instance, instance.load_config(), "dataflow", {})

    assert seen == ["shape_items"]
    assert result.output["items"] == [{"id": "A", "region": "west"}]


def test_execute_workflow_dispatches_provider_steps_and_records_trace_ids(
    workflow_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_lock_for_instance(workflow_instance)
    seen: list[str] = []
    original_execute = DEFAULT_STEP_HANDLER_REGISTRY.execute

    def spy_execute(
        context: WorkflowExecutionContext,
        compiled_step: CompiledPlanStep,
    ) -> None:
        seen.append(compiled_step.kind)
        original_execute(context, compiled_step)

    monkeypatch.setattr(DEFAULT_STEP_HANDLER_REGISTRY, "execute", spy_execute)

    result = execute_workflow(
        workflow_instance,
        workflow_instance.load_config(),
        "evaluate_promo",
        {
            "sku": "SKU-123",
            "start_date": "2026-03-01",
            "end_date": "2026-03-07",
        },
    )

    assert seen == ["query", "provider", "provider", "assert"]
    assert result.step_trace_ids["lift"]
    assert result.step_trace_ids["margin"]
