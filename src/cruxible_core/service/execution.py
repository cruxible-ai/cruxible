"""Workflow execution service functions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import ValidationError

from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.group.types import CandidateMember
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.types import Receipt
from cruxible_core.service.decisions import (
    _append_event_if_context,
    ensure_decision_record_open,
)
from cruxible_core.service.groups import service_propose_group
from cruxible_core.service.types import (
    ApplyWorkflowResult,
    LockServiceResult,
    OperationContext,
    PlanServiceResult,
    ProposeGroupResult,
    ProposeWorkflowResult,
    RunServiceResult,
    TestServiceResult,
)
from cruxible_core.workflow import (
    build_lock,
    compile_workflow,
    compute_lock_digest,
    execute_workflow,
    get_lock_path,
    load_lock,
    resolve_lock_path,
    write_lock,
)
from cruxible_core.workflow.types import (
    RelationshipGroupProposalArtifact,
    WorkflowTestCaseResult,
)

WorkflowExecutionResultT = TypeVar(
    "WorkflowExecutionResultT",
    RunServiceResult,
    ApplyWorkflowResult,
)


def _build_workflow_execution_result(
    result: Any,
    result_type: type[WorkflowExecutionResultT],
) -> WorkflowExecutionResultT:
    """Normalize workflow execution output into the service result shape."""
    return result_type(
        workflow=result.workflow,
        output=result.output,
        receipt_id=result.receipt.receipt_id,
        mode=result.mode,
        canonical=result.canonical,
        apply_digest=result.apply_digest,
        head_snapshot_id=result.head_snapshot_id,
        committed_snapshot_id=result.committed_snapshot_id,
        apply_previews=result.apply_previews,
        query_receipt_ids=result.query_receipt_ids,
        trace_ids=[trace.trace_id for trace in result.traces],
        receipt=result.receipt,
        traces=result.traces,
    )


def _save_workflow_receipt(instance: InstanceProtocol, receipt: Receipt) -> None:
    store = instance.get_receipt_store()
    try:
        store.save_receipt(receipt)
    finally:
        store.close()


def _finalize_proposal_receipt(
    receipt: Receipt,
    *,
    head_snapshot_id: str | None,
    group_result: ProposeGroupResult,
) -> Receipt:
    """Return the workflow receipt annotated with the governed proposal write result."""
    root = receipt.nodes[0]
    root_detail = dict(root.detail)
    root_detail.update(
        {
            "mode": "propose",
            "group_id": group_result.group_id,
            "group_status": group_result.status,
            "group_receipt_id": group_result.receipt_id,
        }
    )
    nodes = list(receipt.nodes)
    nodes[0] = root.model_copy(update={"detail": root_detail})
    # A proposal workflow is committed only after the bridge reaches the
    # governed-group durability boundary. Suppressed no-op proposals still get a
    # receipt, but committed remains false because no group state was written.
    return receipt.model_copy(
        update={
            "nodes": nodes,
            "head_snapshot_id": head_snapshot_id,
            "workflow_mode": "proposal",
            "committed": group_result.receipt_id is not None,
        }
    )


def _enforce_decision_support_context(
    instance: InstanceProtocol,
    workflow: Any,
    context: OperationContext | None,
) -> None:
    if workflow.purpose != "decision_support":
        return
    if context is None or context.decision_record_id is None:
        raise ConfigError("decision_support workflows require decision_record_id")
    ensure_decision_record_open(instance, context.decision_record_id)


def service_lock(instance: InstanceProtocol, *, force: bool = False) -> LockServiceResult:
    """Generate and persist a workflow lock file for the instance config."""
    config = instance.load_config()
    lock = build_lock(config, instance.get_config_path().parent, force=force)
    lock_path = get_lock_path(instance)
    write_lock(lock, lock_path)
    return LockServiceResult(
        lock_path=str(lock_path),
        config_digest=lock.config_digest,
        providers_locked=len(lock.providers),
        artifacts_locked=len(lock.artifacts),
    )


def service_plan(
    instance: InstanceProtocol,
    workflow_name: str,
    input_payload: dict[str, Any],
) -> PlanServiceResult:
    """Compile a workflow plan using the current config and generated lock."""
    config = instance.load_config()
    lock = load_lock(resolve_lock_path(instance))
    plan = compile_workflow(
        config,
        lock,
        workflow_name,
        input_payload,
        config_base_path=instance.get_config_path().parent,
    )
    return PlanServiceResult(plan=plan)


def service_run(
    instance: InstanceProtocol,
    workflow_name: str,
    input_payload: dict[str, Any],
    *,
    context: OperationContext | None = None,
) -> RunServiceResult:
    """Execute a workflow and return output plus receipt/trace identifiers."""
    started_at = datetime.now(timezone.utc)
    input_event = {"workflow_name": workflow_name, "input": input_payload, "mode": "run"}
    try:
        config = instance.load_config()
        workflow = config.workflows.get(workflow_name)
        if workflow is None:
            raise ConfigError(f"Workflow '{workflow_name}' not found in workflows")
        _enforce_decision_support_context(instance, workflow, context)
        if _workflow_returns_relationship_proposal(workflow):
            raise QueryExecutionError(
                f"Workflow '{workflow_name}' produces a governed proposal; use "
                f"'cruxible propose --workflow {workflow_name}' to bridge output into a "
                "candidate group."
            )
        result = execute_workflow(instance, config, workflow_name, input_payload)
        service_result = _build_workflow_execution_result(result, RunServiceResult)
    except Exception as exc:
        _append_event_if_context(
            instance,
            context,
            command=f"workflow_run:{workflow_name}",
            status="error",
            input_payload=input_event,
            error=exc,
            started_at=started_at,
        )
        raise

    _append_event_if_context(
        instance,
        context,
        command=f"workflow_run:{workflow_name}",
        status="success",
        input_payload=input_event,
        output_payload={
            "output": service_result.output,
            "mode": service_result.mode,
            "apply_digest": service_result.apply_digest,
            "committed_snapshot_id": service_result.committed_snapshot_id,
        },
        receipt_id=service_result.receipt_id,
        trace_ids=service_result.trace_ids,
        head_snapshot_id=service_result.head_snapshot_id,
        started_at=started_at,
    )
    return service_result


def service_apply_workflow(
    instance: InstanceProtocol,
    workflow_name: str,
    input_payload: dict[str, Any],
    *,
    expected_apply_digest: str,
    expected_head_snapshot_id: str | None,
    context: OperationContext | None = None,
) -> ApplyWorkflowResult:
    """Apply a canonical workflow after verifying preview identity."""
    started_at = datetime.now(timezone.utc)
    input_event = {
        "workflow_name": workflow_name,
        "input": input_payload,
        "mode": "apply",
        "expected_apply_digest": expected_apply_digest,
        "expected_head_snapshot_id": expected_head_snapshot_id,
    }
    try:
        config = instance.load_config()
        workflow = config.workflows.get(workflow_name)
        if workflow is None:
            raise ConfigError(f"Workflow '{workflow_name}' not found in workflows")
        _enforce_decision_support_context(instance, workflow, context)
        if not workflow.canonical:
            raise ConfigError(f"Workflow '{workflow_name}' is not canonical and cannot be applied")

        preview = execute_workflow(
            instance,
            config,
            workflow_name,
            input_payload,
            mode="preview",
            persist_receipt=False,
            persist_traces=False,
        )
        if preview.apply_digest != expected_apply_digest:
            raise ConfigError("Workflow apply digest mismatch; rerun workflow preview before apply")
        if preview.head_snapshot_id != expected_head_snapshot_id:
            raise ConfigError(
                "Workflow head snapshot changed between preview and apply.\n"
                "Apply requires both --apply-digest AND --head-snapshot from the preview output,\n"
                "or pass --preview-file <path> if you used 'run --save-preview'.\n"
                "Rerun the preview if output was not captured."
            )

        current_lock = load_lock(resolve_lock_path(instance))
        current_lock_digest = compute_lock_digest(current_lock)
        if preview.receipt.nodes[0].detail.get("lock_digest") != current_lock_digest:
            raise ConfigError("Workflow lock changed; rerun workflow preview before apply")

        result = execute_workflow(
            instance,
            config,
            workflow_name,
            input_payload,
            mode="apply",
            persist_receipt=True,
            persist_traces=True,
        )
        service_result = _build_workflow_execution_result(result, ApplyWorkflowResult)
    except Exception as exc:
        _append_event_if_context(
            instance,
            context,
            command=f"workflow_apply:{workflow_name}",
            status="error",
            input_payload=input_event,
            error=exc,
            started_at=started_at,
        )
        raise

    _append_event_if_context(
        instance,
        context,
        command=f"workflow_apply:{workflow_name}",
        status="success",
        input_payload=input_event,
        output_payload={
            "output": service_result.output,
            "mode": service_result.mode,
            "committed_snapshot_id": service_result.committed_snapshot_id,
        },
        receipt_id=service_result.receipt_id,
        trace_ids=service_result.trace_ids,
        head_snapshot_id=service_result.head_snapshot_id,
        started_at=started_at,
    )
    return service_result


def service_propose_workflow(
    instance: InstanceProtocol,
    workflow_name: str,
    input_payload: dict[str, Any],
    *,
    context: OperationContext | None = None,
) -> ProposeWorkflowResult:
    """Execute a workflow and bridge its returned proposal artifact into a candidate group."""
    started_at = datetime.now(timezone.utc)
    input_event = {
        "workflow_name": workflow_name,
        "input": input_payload,
        "mode": "propose",
    }
    try:
        config = instance.load_config()
        workflow = config.workflows.get(workflow_name)
        if workflow is None:
            raise ConfigError(f"Workflow '{workflow_name}' not found in workflows")
        if workflow.purpose != "proposal":
            raise ConfigError(
                f"Workflow '{workflow_name}' must set purpose: proposal to be used with "
                "propose_workflow"
            )
        if workflow.canonical:
            raise ConfigError(
                f"Canonical workflow '{workflow_name}' cannot be used with propose_workflow"
            )
        result = execute_workflow(
            instance,
            config,
            workflow_name,
            input_payload,
            persist_receipt=False,
        )
        try:
            proposal_payload = RelationshipGroupProposalArtifact.model_validate(result.output)
        except ValidationError as exc:
            raise QueryExecutionError(
                f"Workflow '{workflow_name}' must return a relationship proposal artifact"
            ) from exc
        relationship_type = proposal_payload.relationship_type
        members = [
            CandidateMember(
                from_type=member.from_type,
                from_id=member.from_id,
                to_type=member.to_type,
                to_id=member.to_id,
                relationship_type=relationship_type,
                signals=member.signals,
                properties=member.properties,
            )
            for member in proposal_payload.members
        ]

        source_step_id = result.alias_step_ids.get(config.workflows[workflow_name].returns)
        source_trace_ids = [trace.trace_id for trace in result.traces]
        group_result = service_propose_group(
            instance,
            relationship_type,
            members,
            thesis_text=proposal_payload.thesis_text,
            thesis_facts=proposal_payload.thesis_facts,
            pending_refresh_mode=proposal_payload.pending_refresh_mode,
            analysis_state=proposal_payload.analysis_state,
            integrations_used=proposal_payload.integrations_used,
            proposed_by=proposal_payload.proposed_by,
            suggested_priority=proposal_payload.suggested_priority,
            source_workflow_name=workflow_name,
            source_workflow_receipt_id=result.receipt.receipt_id,
            source_trace_ids=source_trace_ids,
            source_step_ids=[source_step_id] if source_step_id is not None else [],
        )
        proposal_receipt = _finalize_proposal_receipt(
            result.receipt,
            head_snapshot_id=result.head_snapshot_id,
            group_result=group_result,
        )
        _save_workflow_receipt(instance, proposal_receipt)
        service_result = ProposeWorkflowResult(
            workflow=result.workflow,
            output=result.output,
            receipt_id=proposal_receipt.receipt_id,
            group_id=group_result.group_id,
            group_status=group_result.status,
            review_priority=group_result.review_priority,
            suppressed=group_result.suppressed,
            suppressed_members=group_result.suppressed_members,
            query_receipt_ids=result.query_receipt_ids,
            trace_ids=[trace.trace_id for trace in result.traces],
            prior_resolution=group_result.prior_resolution,
            policy_summary=group_result.policy_summary,
            receipt=proposal_receipt,
            traces=result.traces,
        )
    except Exception as exc:
        _append_event_if_context(
            instance,
            context,
            command=f"workflow_propose:{workflow_name}",
            status="error",
            input_payload=input_event,
            error=exc,
            started_at=started_at,
        )
        raise

    _append_event_if_context(
        instance,
        context,
        command=f"workflow_propose:{workflow_name}",
        status="success",
        input_payload=input_event,
        output_payload={
            "output": service_result.output,
            "group_id": service_result.group_id,
            "group_status": service_result.group_status,
        },
        receipt_id=service_result.receipt_id,
        trace_ids=service_result.trace_ids,
        head_snapshot_id=(
            service_result.receipt.head_snapshot_id if service_result.receipt else None
        ),
        started_at=started_at,
    )
    return service_result


def service_test(instance: InstanceProtocol, test_name: str | None = None) -> TestServiceResult:
    """Execute config-defined workflow tests."""
    config = instance.load_config()
    tests = config.tests
    if test_name is not None:
        tests = [test for test in tests if test.name == test_name]
        if not tests:
            raise ConfigError(f"Test '{test_name}' not found in config")
    if not tests:
        raise ConfigError("No workflow tests are defined in config")

    cases: list[WorkflowTestCaseResult] = []
    passed = 0

    for test in tests:
        try:
            result = execute_workflow(instance, config, test.workflow, test.input)
            _validate_test_expectation(
                test.expect.output_equals,
                result.output,
                test.name,
                "output_equals",
            )
            if test.expect.output_contains is not None:
                if not _contains_subset(result.output, test.expect.output_contains):
                    raise QueryExecutionError(
                        f"Test '{test.name}' failed: output does not contain expected subset"
                    )
            if test.expect.required_providers:
                providers_used = {trace.provider_name for trace in result.traces}
                missing = [
                    name for name in test.expect.required_providers if name not in providers_used
                ]
                if missing:
                    missing_str = ", ".join(missing)
                    raise QueryExecutionError(
                        f"Test '{test.name}' failed: missing provider evidence for {missing_str}"
                    )
            if test.expect.error_contains is not None:
                raise QueryExecutionError(
                    "Test "
                    f"'{test.name}' expected error containing "
                    f"'{test.expect.error_contains}' but run succeeded"
                )
        except Exception as exc:
            error_text = str(exc)
            expected_error = test.expect.error_contains
            if expected_error is not None and expected_error in error_text:
                passed += 1
                cases.append(
                    WorkflowTestCaseResult(
                        name=test.name,
                        workflow=test.workflow,
                        passed=True,
                        error=error_text,
                    )
                )
                continue
            cases.append(
                WorkflowTestCaseResult(
                    name=test.name,
                    workflow=test.workflow,
                    passed=False,
                    error=error_text,
                )
            )
            continue

        passed += 1
        cases.append(
            WorkflowTestCaseResult(
                name=test.name,
                workflow=test.workflow,
                passed=True,
                output=result.output,
                receipt_id=result.receipt.receipt_id,
            )
        )

    total = len(cases)
    return TestServiceResult(total=total, passed=passed, failed=total - passed, cases=cases)


def _validate_test_expectation(expected: Any, actual: Any, test_name: str, field_name: str) -> None:
    if expected is not None and actual != expected:
        raise QueryExecutionError(
            f"Test '{test_name}' failed: {field_name} expected {expected!r}, got {actual!r}"
        )


def _contains_subset(actual: Any, expected_subset: Any) -> bool:
    if isinstance(expected_subset, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and _contains_subset(actual[key], expected_value)
            for key, expected_value in expected_subset.items()
        )

    if isinstance(expected_subset, list):
        if not isinstance(actual, list) or len(expected_subset) > len(actual):
            return False
        return all(
            _contains_subset(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected_subset, strict=False)
        )

    return actual == expected_subset


def _workflow_returns_relationship_proposal(workflow: Any) -> bool:
    """Return True when a workflow returns a built-in relationship proposal artifact."""
    return any(
        bool(step.as_ == workflow.returns) and step.propose_relationship_group is not None
        for step in workflow.steps
    )
