"""Workflow execution service functions."""

from __future__ import annotations

import hashlib
from typing import Any, Literal, TypeVar

from pydantic import ValidationError

from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.group.types import CandidateMember
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.primitives import canonical_json, ordered_unique
from cruxible_core.receipt.types import Receipt
from cruxible_core.service.decisions import (
    ensure_decision_record_open,
    record_decision_event_for_context,
)
from cruxible_core.service.groups import (
    build_workflow_proposal_signature_facts,
    service_propose_group,
)
from cruxible_core.service.types import (
    ApplyPreviewReference,
    ApplyWorkflowResult,
    LockServiceResult,
    OperationContext,
    PlanServiceResult,
    ProposeGroupResult,
    ProposeWorkflowResult,
    RunServiceResult,
    TestServiceResult,
    WorkflowTestCaseServiceResult,
)
from cruxible_core.temporal import utc_now
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
        workflow_type=result.workflow_type,
        apply_digest=result.apply_digest,
        head_snapshot_id=result.head_snapshot_id,
        committed_snapshot_id=result.committed_snapshot_id,
        apply_previews=result.apply_previews,
        query_receipt_ids=result.query_receipt_ids,
        read_metadata=result.read_metadata,
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


def _apply_previews_from_receipt(receipt: Receipt) -> dict[str, Any]:
    """Extract apply preview summaries from workflow receipt validation nodes."""
    previews: dict[str, Any] = {}
    nodes_by_id = {node.node_id: node for node in receipt.nodes}
    plan_steps = {
        node.node_id: node.detail.get("step_id")
        for node in receipt.nodes
        if node.node_type == "plan_step"
        and node.detail.get("kind") in {"apply_entities", "apply_relationships", "apply_all"}
        and isinstance(node.detail.get("step_id"), str)
    }
    for edge in receipt.edges:
        step_id = plan_steps.get(edge.from_node)
        if step_id is None or edge.edge_type != "validated":
            continue
        validation = nodes_by_id.get(edge.to_node)
        if validation is None or validation.node_type != "validation":
            continue
        detail = dict(validation.detail)
        detail.pop("passed", None)
        previews[step_id] = detail
    return previews


def apply_preview_reference_from_receipt(receipt: Receipt) -> ApplyPreviewReference | None:
    """Return an apply reference when a receipt is a usable canonical preview."""
    if receipt.operation_type != "workflow" or receipt.workflow_mode != "preview":
        return None
    apply_digest = receipt.nodes[0].detail.get("apply_digest") if receipt.nodes else None
    if not isinstance(apply_digest, str) or not apply_digest:
        return None
    return ApplyPreviewReference(
        workflow=receipt.query_name,
        input_payload=receipt.parameters,
        apply_digest=apply_digest,
        head_snapshot_id=receipt.head_snapshot_id,
        receipt_id=receipt.receipt_id,
        created_at=receipt.created_at,
        apply_previews=_apply_previews_from_receipt(receipt),
    )


def service_find_apply_preview(
    instance: InstanceProtocol,
    workflow_name: str,
    *,
    limit: int = 50,
) -> ApplyPreviewReference:
    """Return the latest stored canonical preview usable for workflow apply."""
    store = instance.get_receipt_store()
    try:
        summaries = store.list_receipts(
            query_name=workflow_name,
            operation_type="workflow",
            limit=limit,
        )
        for summary in summaries:
            receipt_id = summary.get("receipt_id")
            if not isinstance(receipt_id, str):
                continue
            receipt = store.get_receipt(receipt_id)
            if receipt is None:
                continue
            reference = apply_preview_reference_from_receipt(receipt)
            if reference is not None:
                return reference
    finally:
        store.close()
    raise ConfigError(
        f"No stored canonical preview found for workflow '{workflow_name}'. "
        "Run the workflow in preview mode before applying."
    )


def _workflow_proposal_source_step_ids(workflow: Any, result: Any) -> list[str]:
    """Return proposal and signal-mapping step ids that explain a bridged group."""
    source_step_ids: list[str] = []

    def add(step_id: str | None) -> None:
        if step_id is not None and step_id not in source_step_ids:
            source_step_ids.append(step_id)

    returns_alias = workflow.returns
    add(result.alias_step_ids.get(returns_alias))

    for step in workflow.steps:
        step_alias = step.as_ if step.as_ is not None else step.id
        if step_alias != returns_alias or step.propose_relationship_group is None:
            continue
        for signal_alias in step.propose_relationship_group.signals_from:
            add(result.alias_step_ids.get(signal_alias))
        break

    return source_step_ids


def _finalize_proposal_receipt(
    receipt: Receipt,
    *,
    head_snapshot_id: str | None,
    group_result: ProposeGroupResult,
    output: dict[str, Any] | None = None,
) -> Receipt:
    """Return the workflow receipt annotated with the governed proposal write result."""
    root = receipt.nodes[0]
    root_detail = dict(root.detail)
    # Receipt.workflow_mode is the structural mode; this root detail is the
    # agent-facing proposal bridge summary used by rendered receipt surfaces.
    root_detail.update(
        {
            "mode": "proposal",
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
    updates: dict[str, Any] = {
        "nodes": nodes,
        "head_snapshot_id": head_snapshot_id,
        "committed": group_result.receipt_id is not None,
    }
    if output is not None:
        updates["results"] = [{"output": output}]
    return receipt.model_copy(update=updates)


def _workflow_proposal_logic_digest(
    *,
    config: CoreConfig,
    workflow: Any,
    proposal_step_id: str,
    relationship_type: str,
) -> str:
    """Return a stable digest of the executable proposal dependency slice."""
    step_payloads: list[dict[str, Any]] = []
    provider_names: set[str] = set()
    query_names: set[str] = set()
    for step in workflow.steps:
        payload = step.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        if step.provider is not None:
            provider_names.add(step.provider)
        if isinstance(step.query, str):
            query_names.add(step.query)
        proposal_spec = payload.get("propose_relationship_group")
        if isinstance(proposal_spec, dict):
            proposal_spec.pop("thesis_text", None)
            proposal_spec.pop("analysis_state", None)
            proposal_spec.pop("suggested_priority", None)
        step_payloads.append(payload)
        if step.id == proposal_step_id:
            break

    providers = {
        name: config.providers[name].model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        for name in sorted(provider_names)
        if name in config.providers
    }
    queries = {
        name: config.named_queries[name].model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        for name in sorted(query_names)
        if name in config.named_queries
    }
    rel_schema = config.get_relationship(relationship_type)
    relationship_policy = (
        rel_schema.proposal_policy.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        if rel_schema is not None and rel_schema.proposal_policy is not None
        else None
    )
    payload = {
        "version": 1,
        "workflow": {
            "type": workflow.type,
            "contract_in": workflow.contract_in,
            "contract_out": workflow.contract_out,
            "returns": workflow.returns,
        },
        "steps": step_payloads,
        "providers": providers,
        "queries": queries,
        "relationship": {
            "type": relationship_type,
            "proposal_identity": rel_schema.proposal_identity if rel_schema is not None else None,
            "proposal_policy": relationship_policy,
        },
    }
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return f"sha256:{digest}"


def _enforce_decision_support_context(
    instance: InstanceProtocol,
    workflow: Any,
    context: OperationContext | None,
) -> None:
    if workflow.type != "decision_support":
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
    started_at = utc_now()
    input_event = {"workflow_name": workflow_name, "input": input_payload, "mode": "run"}
    try:
        config = instance.load_config()
        workflow = config.workflows.get(workflow_name)
        if workflow is None:
            raise ConfigError(f"Workflow '{workflow_name}' not found in workflows")
        execution_action: Literal["run", "preview"] = (
            "preview" if workflow.type == "canonical" else "run"
        )
        input_event["mode"] = execution_action
        _enforce_decision_support_context(instance, workflow, context)
        if workflow.type == "proposal":
            raise QueryExecutionError(
                f"Workflow '{workflow_name}' produces a governed proposal; use "
                f"'cruxible propose --workflow {workflow_name}' to bridge output into a "
                "candidate group."
            )
        result = execute_workflow(
            instance,
            config,
            workflow_name,
            input_payload,
            mode=execution_action,
        )
        service_result = _build_workflow_execution_result(result, RunServiceResult)
    except Exception as exc:
        record_decision_event_for_context(
            instance,
            context,
            command=f"workflow_run:{workflow_name}",
            status="error",
            input_payload=input_event,
            error=exc,
            started_at=started_at,
        )
        raise

    record_decision_event_for_context(
        instance,
        context,
        command=f"workflow_run:{workflow_name}",
        status="success",
        input_payload=input_event,
        output_payload={
            "output": service_result.output,
            "mode": service_result.mode,
            "workflow_type": service_result.workflow_type,
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
    started_at = utc_now()
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
        if workflow.type != "canonical":
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
                "Apply requires the apply digest and head snapshot id produced by "
                "the same preview execution."
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
        record_decision_event_for_context(
            instance,
            context,
            command=f"workflow_apply:{workflow_name}",
            status="error",
            input_payload=input_event,
            error=exc,
            started_at=started_at,
        )
        raise

    record_decision_event_for_context(
        instance,
        context,
        command=f"workflow_apply:{workflow_name}",
        status="success",
        input_payload=input_event,
        output_payload={
            "output": service_result.output,
            "mode": service_result.mode,
            "workflow_type": service_result.workflow_type,
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
    started_at = utc_now()
    input_event = {
        "workflow_name": workflow_name,
        "input": input_payload,
        "mode": "proposal",
    }
    try:
        config = instance.load_config()
        workflow = config.workflows.get(workflow_name)
        if workflow is None:
            raise ConfigError(f"Workflow '{workflow_name}' not found in workflows")
        if workflow.type != "proposal":
            raise ConfigError(
                f"Workflow '{workflow_name}' must set type: proposal to be used with "
                "propose_workflow"
            )
        result = execute_workflow(
            instance,
            config,
            workflow_name,
            input_payload,
            persist_receipt=False,
            persist_query_receipts=True,
        )
        try:
            proposal_payload = RelationshipGroupProposalArtifact.model_validate(result.output)
        except ValidationError as exc:
            raise QueryExecutionError(
                f"Workflow '{workflow_name}' must return a relationship proposal artifact"
            ) from exc
        relationship_type = proposal_payload.relationship_type
        rel_schema = config.get_relationship(relationship_type)
        if rel_schema is None:
            raise ConfigError(f"Relationship type '{relationship_type}' not found in config")
        proposal_step_id = proposal_payload.proposal_step_id or result.alias_step_ids.get(
            workflow.returns,
            workflow.returns,
        )
        workflow_thesis_facts = build_workflow_proposal_signature_facts(
            rel_schema=rel_schema,
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            step_id=proposal_step_id,
            proposal_logic_digest=_workflow_proposal_logic_digest(
                config=config,
                workflow=workflow,
                proposal_step_id=proposal_step_id,
                relationship_type=relationship_type,
            ),
            candidates_from=proposal_payload.candidates_from or "",
            signal_sources_used=proposal_payload.signal_sources_used,
        )
        proposal_payload = proposal_payload.model_copy(
            update={"thesis_facts": workflow_thesis_facts}
        )
        proposal_output = proposal_payload.model_dump(mode="python")
        if proposal_payload.status == "no_candidates":
            no_group_result = ProposeGroupResult(
                group_id=None,
                signature="",
                status="no_candidates",
                review_priority="normal",
                member_count=0,
                prior_resolution=None,
                suppressed=False,
                suppressed_members=[],
                policy_summary={},
                receipt_id=None,
            )
            proposal_receipt = _finalize_proposal_receipt(
                result.receipt,
                head_snapshot_id=result.head_snapshot_id,
                group_result=no_group_result,
                output=proposal_output,
            )
            _save_workflow_receipt(instance, proposal_receipt)
            service_result = ProposeWorkflowResult(
                workflow=result.workflow,
                output=proposal_output,
                receipt_id=proposal_receipt.receipt_id,
                group_id=None,
                group_status="no_candidates",
                review_priority="normal",
                mode=result.mode,
                workflow_type=result.workflow_type,
                suppressed=False,
                suppressed_members=[],
                query_receipt_ids=result.query_receipt_ids,
                read_metadata=result.read_metadata,
                trace_ids=[trace.trace_id for trace in result.traces],
                prior_resolution=None,
                policy_summary={},
                receipt=proposal_receipt,
                traces=result.traces,
            )
            record_decision_event_for_context(
                instance,
                context,
                command=f"workflow_propose:{workflow_name}",
                status="success",
                input_payload=input_event,
                output_payload={
                    "output": service_result.output,
                    "mode": service_result.mode,
                    "workflow_type": service_result.workflow_type,
                    "group_id": service_result.group_id,
                    "group_status": service_result.group_status,
                },
                receipt_id=service_result.receipt_id,
                trace_ids=service_result.trace_ids,
                head_snapshot_id=(
                    service_result.receipt.head_snapshot_id
                    if service_result.receipt
                    else None
                ),
                started_at=started_at,
            )
            return service_result
        members = [
            CandidateMember(
                from_type=member.from_type,
                from_id=member.from_id,
                to_type=member.to_type,
                to_id=member.to_id,
                relationship_type=relationship_type,
                signals=member.signals,
                source_query_evidence=member.source_query_evidence,
                properties=member.properties,
            )
            for member in proposal_payload.members
        ]

        source_step_ids = _workflow_proposal_source_step_ids(workflow, result)
        source_query_receipt_ids = ordered_unique(
            [*result.query_receipt_ids, *proposal_payload.query_receipt_ids]
        )
        source_trace_ids = [trace.trace_id for trace in result.traces]
        group_result = service_propose_group(
            instance,
            relationship_type,
            members,
            thesis_text=proposal_payload.thesis_text,
            thesis_facts=proposal_payload.thesis_facts,
            pending_refresh_mode=proposal_payload.pending_refresh_mode,
            analysis_state=proposal_payload.analysis_state,
            signal_sources_used=proposal_payload.signal_sources_used,
            proposed_by=proposal_payload.proposed_by,
            suggested_priority=proposal_payload.suggested_priority,
            source_workflow_name=workflow_name,
            source_workflow_receipt_id=result.receipt.receipt_id,
            source_query_receipt_ids=source_query_receipt_ids,
            source_trace_ids=source_trace_ids,
            source_step_ids=source_step_ids,
        )
        proposal_receipt = _finalize_proposal_receipt(
            result.receipt,
            head_snapshot_id=result.head_snapshot_id,
            group_result=group_result,
            output=proposal_output,
        )
        _save_workflow_receipt(instance, proposal_receipt)
        service_result = ProposeWorkflowResult(
            workflow=result.workflow,
            output=proposal_output,
            receipt_id=proposal_receipt.receipt_id,
            group_id=group_result.group_id,
            group_status=group_result.status,
            review_priority=group_result.review_priority,
            mode=result.mode,
            workflow_type=result.workflow_type,
            suppressed=group_result.suppressed,
            suppressed_members=group_result.suppressed_members,
            query_receipt_ids=result.query_receipt_ids,
            read_metadata=result.read_metadata,
            trace_ids=[trace.trace_id for trace in result.traces],
            prior_resolution=group_result.prior_resolution,
            policy_summary=group_result.policy_summary,
            receipt=proposal_receipt,
            traces=result.traces,
        )
    except Exception as exc:
        record_decision_event_for_context(
            instance,
            context,
            command=f"workflow_propose:{workflow_name}",
            status="error",
            input_payload=input_event,
            error=exc,
            started_at=started_at,
        )
        raise

    record_decision_event_for_context(
        instance,
        context,
        command=f"workflow_propose:{workflow_name}",
        status="success",
        input_payload=input_event,
        output_payload={
            "output": service_result.output,
            "mode": service_result.mode,
            "workflow_type": service_result.workflow_type,
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
    """Execute config-defined workflow fixture tests.

    Config tests validate workflow output shape and canonical preview behavior.
    They do not simulate proposal bridging or decision-record lifecycle gates.
    """
    config = instance.load_config()
    tests = config.tests
    if test_name is not None:
        tests = [test for test in tests if test.name == test_name]
        if not tests:
            raise ConfigError(f"Test '{test_name}' not found in config")
    if not tests:
        raise ConfigError("No workflow tests are defined in config")
    for test in tests:
        if test.workflow not in config.workflows:
            raise ConfigError(
                f"Test '{test.name}' references unknown workflow '{test.workflow}'"
            )

    cases: list[WorkflowTestCaseServiceResult] = []
    passed = 0

    for test in tests:
        try:
            workflow = config.workflows[test.workflow]
            execution_action: Literal["run", "preview"] = (
                "preview" if workflow.type == "canonical" else "run"
            )
            result = execute_workflow(
                instance,
                config,
                test.workflow,
                test.input,
                mode=execution_action,
            )
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
                    WorkflowTestCaseServiceResult(
                        name=test.name,
                        workflow=test.workflow,
                        passed=True,
                        error=error_text,
                    )
                )
                continue
            cases.append(
                WorkflowTestCaseServiceResult(
                    name=test.name,
                    workflow=test.workflow,
                    passed=False,
                    error=error_text,
                )
            )
            continue

        passed += 1
        cases.append(
            WorkflowTestCaseServiceResult(
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
