"""Service-layer governance for state-held procedure definitions."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal, NoReturn

from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import (
    ConfigError,
    CoreError,
    ProcedureBudgetExceededError,
    ProcedureNotFoundError,
    QueryExecutionError,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import EvidenceRef, normalize_evidence_ref
from cruxible_core.instance_protocol import InstanceProtocol, ProcedureStoreProtocol
from cruxible_core.procedure.types import (
    ProcedureBudgetSpent,
    ProcedureDefinition,
    ProcedureExecutionResult,
    ProcedurePrecondition,
    ProcedureRecord,
    ProcedureRun,
    ProcedureRunStatus,
    ProcedureRunVerdict,
    ProcedureStatus,
    ProcedureTier,
    ProcedureTransitionResult,
    compute_procedure_definition_digest,
)
from cruxible_core.query.entity_state import entity_matches_query_state
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.types import Receipt
from cruxible_core.runtime.permissions import PermissionMode, get_current_mode
from cruxible_core.service.gates import entity_matches_property_equality_condition
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.service.types import ListResult, list_truncated
from cruxible_core.temporal import format_datetime, utc_now
from cruxible_core.workflow.compiler import (
    compile_plan_definition,
    compute_lock_config_digest,
    compute_lock_digest,
    load_lock,
    resolve_lock_path,
)
from cruxible_core.workflow.execution_context import ProcedureExecutionBudget
from cruxible_core.workflow.executor import (
    FAILED_WORKFLOW_RECEIPT_ATTR,
    execute_procedure_plan,
)
from cruxible_core.workflow.types import CompiledPlan

_TIER_RANK = {"governed_write": 2, "graph_write": 3, "admin": 4}
_PERMISSION_BY_TIER = {
    "governed_write": PermissionMode.GOVERNED_WRITE,
    "graph_write": PermissionMode.GRAPH_WRITE,
    "admin": PermissionMode.ADMIN,
}
_READ_REVISION_STATE_KEY = "read_revision"


def validate_procedure_definition_against_config(
    definition: ProcedureDefinition,
    config: CoreConfig,
) -> ProcedureTier:
    """Validate provider exports and return the procedure's effective tier."""
    precondition_entity_type = definition.precondition.entity_type
    if precondition_entity_type is not None and precondition_entity_type not in config.entity_types:
        raise ConfigError(
            f"Procedure '{definition.name}' precondition references unknown entity type "
            f"'{precondition_entity_type}'"
        )

    effective_tier: ProcedureTier = "governed_write"
    for provider_name in sorted(definition.referenced_providers()):
        provider = config.providers.get(provider_name)
        if provider is None:
            raise ConfigError(
                f"Procedure '{definition.name}' references unknown provider '{provider_name}'"
            )
        if provider.procedure_access == "disabled":
            raise ConfigError(
                f"Provider '{provider_name}' is not exported to procedures "
                "(procedure_access is disabled)"
            )
        if provider.runtime == "python":
            raise ConfigError(
                f"Provider '{provider_name}' uses the in-process Python transport and "
                "cannot be exported to procedures"
            )
        provider_tier = _provider_tier(provider.procedure_access)
        if _TIER_RANK[provider_tier] > _TIER_RANK[effective_tier]:
            effective_tier = provider_tier

    if _TIER_RANK[definition.declared_tier] < _TIER_RANK[effective_tier]:
        raise ConfigError(
            f"Procedure '{definition.name}' declares tier '{definition.declared_tier}' "
            f"below its effective provider tier '{effective_tier}'"
        )
    return definition.declared_tier


def compile_procedure_definition(
    instance: InstanceProtocol,
    definition: ProcedureDefinition,
    input_payload: dict[str, Any] | None = None,
) -> CompiledPlan:
    """Compile a state-held procedure definition against the active config/lock."""
    config = instance.load_config()
    validate_procedure_definition_against_config(definition, config)
    lock = load_lock(resolve_lock_path(instance))
    return compile_plan_definition(
        config,
        lock,
        definition.name,
        definition,
        input_payload,
        config_base_path=instance.get_config_path().parent,
        definition_label="Procedure",
    )


def service_propose_procedure(
    instance: InstanceProtocol,
    definition: ProcedureDefinition,
    *,
    actor_context: GovernedActorContext | None,
    supersedes_procedure_id: str | None = None,
    evidence_refs: Sequence[EvidenceRef | Mapping[str, Any]] = (),
) -> ProcedureTransitionResult:
    """Validate, compile, and persist one pending procedure proposal."""
    definition_digest = compute_procedure_definition_digest(definition)
    with mutation_receipt(
        instance,
        "procedure_transition",
        {
            "action": "propose",
            "name": definition.name,
            "definition_digest": definition_digest,
            "supersedes_procedure_id": supersedes_procedure_id,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        proposer = _require_actor(actor_context, role="proposer", builder=ctx.builder)
        if supersedes_procedure_id is not None:
            superseded = _get_procedure(ctx.uow.procedures, supersedes_procedure_id)
            if superseded.status != "live":
                _refuse(
                    ctx.builder,
                    f"superseded procedure '{supersedes_procedure_id}' must be live; "
                    f"found '{superseded.status}'",
                )
            if superseded.definition.name != definition.name:
                _refuse(
                    ctx.builder,
                    "a procedure may only supersede a live definition with the same name",
                )

        try:
            plan = compile_procedure_definition(instance, definition)
        except ConfigError as exc:
            ctx.builder.record_validation(
                passed=False,
                detail={"action": "propose", "reason": str(exc)},
            )
            raise

        procedure = ProcedureRecord(
            definition=definition,
            definition_digest=definition_digest,
            supersedes_procedure_id=supersedes_procedure_id,
            evidence_refs=[normalize_evidence_ref(ref) for ref in evidence_refs],
            proposed_actor_context=proposer,
        )
        ctx.uow.procedures.save_procedure(procedure)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "action": "propose",
                "procedure_id": procedure.procedure_id,
                "definition_digest": definition_digest,
                "config_digest": plan.config_digest,
                "lock_digest": plan.lock_digest,
            },
        )
        result = ProcedureTransitionResult(action="propose", procedure=procedure)
        ctx.set_result(result)

    return result


def service_accept_procedure(
    instance: InstanceProtocol,
    procedure_id: str,
    *,
    expected_version: int | None,
    actor_context: GovernedActorContext | None,
) -> ProcedureTransitionResult:
    """Accept a pending definition after independent review and recompilation."""
    return _transition_pending_procedure(
        instance,
        procedure_id,
        action="accept",
        expected_version=expected_version,
        actor_context=actor_context,
        reason=None,
    )


def service_reject_procedure(
    instance: InstanceProtocol,
    procedure_id: str,
    *,
    expected_version: int | None,
    reason: str,
    actor_context: GovernedActorContext | None,
) -> ProcedureTransitionResult:
    """Reject a pending definition with a required independent-review reason."""
    return _transition_pending_procedure(
        instance,
        procedure_id,
        action="reject",
        expected_version=expected_version,
        actor_context=actor_context,
        reason=reason,
    )


def service_retire_procedure(
    instance: InstanceProtocol,
    procedure_id: str,
    *,
    expected_version: int | None,
    reason: str,
    actor_context: GovernedActorContext | None,
) -> ProcedureTransitionResult:
    """Retire a live immutable definition with an attributed reason."""
    with mutation_receipt(
        instance,
        "procedure_transition",
        {
            "action": "retire",
            "procedure_id": procedure_id,
            "expected_version": expected_version,
            "reason": reason,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        retiring_actor = _require_actor(
            actor_context, role="retiring reviewer", builder=ctx.builder
        )
        normalized_reason = _require_reason(reason, action="retire", builder=ctx.builder)
        procedure = _get_procedure(ctx.uow.procedures, procedure_id, builder=ctx.builder)
        _validate_status_and_version(
            procedure,
            expected_status="live",
            expected_version=expected_version,
            builder=ctx.builder,
        )
        now = utc_now()
        updated = ctx.uow.procedures.transition_procedure(
            procedure_id,
            from_status="live",
            to_status="retired",
            expected_version=procedure.version,
            retired_actor_context=retiring_actor,
            retired_at=format_datetime(now),
            reason=normalized_reason,
        )
        if not updated:
            _refuse(ctx.builder, "procedure changed during retirement")
        retired = _get_procedure(ctx.uow.procedures, procedure_id)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "action": "retire",
                "procedure_id": procedure_id,
                "from_version": procedure.version,
                "to_version": retired.version,
                "reason": normalized_reason,
            },
        )
        result = ProcedureTransitionResult(action="retire", procedure=retired)
        ctx.set_result(result)
    return result


def service_get_procedure(
    instance: InstanceProtocol,
    procedure_id: str,
) -> ProcedureRecord:
    """Read one procedure record."""
    store = instance.get_procedure_store()
    try:
        return _get_procedure(store, procedure_id)
    finally:
        store.close()


def service_list_procedures(
    instance: InstanceProtocol,
    *,
    name: str | None = None,
    status: ProcedureStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ListResult:
    """List procedure records with the standard read-surface envelope."""
    _validate_list_page(limit=limit, offset=offset)
    store = instance.get_procedure_store()
    try:
        items = store.list_procedures(
            name=name,
            status=status,
            limit=limit,
            offset=offset,
        )
        total = store.count_procedures(name=name, status=status)
        return ListResult(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            truncated=list_truncated(total=total, offset=offset, returned=len(items)),
            read_revision=instance.get_read_revision(),
        )
    finally:
        store.close()


def service_list_procedure_runs(
    instance: InstanceProtocol,
    procedure_id: str,
    *,
    status: ProcedureRunStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ListResult:
    """List invocation records, including crash-visible started tombstones."""
    _validate_list_page(limit=limit, offset=offset)
    store = instance.get_procedure_store()
    try:
        _get_procedure(store, procedure_id)
        items = store.list_runs(
            procedure_id=procedure_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        total = store.count_runs(procedure_id=procedure_id, status=status)
        return ListResult(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            truncated=list_truncated(total=total, offset=offset, returned=len(items)),
            read_revision=instance.get_read_revision(),
        )
    finally:
        store.close()


def service_run_procedure(
    instance: InstanceProtocol,
    procedure_id: str,
    input_payload: dict[str, Any],
    actor_context: GovernedActorContext | None,
) -> ProcedureExecutionResult:
    """Run one live procedure with short authorization and crash-safe audit state."""
    invocation_started = time.monotonic()
    with instance.write_transaction() as uow:
        procedure = _get_procedure(uow.procedures, procedure_id)
        started_run = ProcedureRun(
            procedure_id=procedure.procedure_id,
            definition_digest=procedure.definition_digest,
        )
        uow.procedures.save_run(started_run)

    budget = ProcedureExecutionBudget(
        wall_clock_s=procedure.definition.budget.wall_clock_s,
        max_provider_calls=procedure.definition.budget.max_provider_calls,
        started_monotonic=invocation_started,
    )
    builder = ReceiptBuilder(
        query_name=procedure.definition.name,
        parameters={
            "procedure_id": procedure.procedure_id,
            "definition_digest": procedure.definition_digest,
            "input": input_payload,
        },
        operation_type="procedure",
        head_snapshot_id=instance.get_head_snapshot_id(),
        actor_context=actor_context,
    )
    precondition_detail: dict[str, Any] = {
        "evaluated": False,
        "read_revision": None,
        "entity_type": procedure.definition.precondition.entity_type,
        "condition": dict(procedure.definition.precondition.condition or {}),
        "satisfying_entity_ids": [],
    }
    executed_config_digest: str | None = None
    executed_lock_digest: str | None = None

    try:
        if procedure.status != "live":
            raise ConfigError(
                f"Procedure '{procedure.procedure_id}' must be live to run; "
                f"found '{procedure.status}'"
            )
        current_definition_digest = compute_procedure_definition_digest(procedure.definition)
        if current_definition_digest != procedure.definition_digest:
            raise ConfigError(
                "Procedure definition digest changed since acceptance: "
                f"stored={procedure.definition_digest}, computed={current_definition_digest}"
            )

        config = instance.load_config()
        lock = load_lock(resolve_lock_path(instance))
        executed_config_digest = compute_lock_config_digest(config)
        executed_lock_digest = compute_lock_digest(lock)
        effective_tier = validate_procedure_definition_against_config(
            procedure.definition,
            config,
        )
        _require_procedure_execution_tier(effective_tier)
        plan = compile_plan_definition(
            config,
            lock,
            procedure.definition.name,
            procedure.definition,
            input_payload,
            config_base_path=instance.get_config_path().parent,
            definition_label="Procedure",
        )
    except Exception as exc:
        refusal_error = (
            exc
            if isinstance(exc, ConfigError)
            else ConfigError(f"Procedure preflight failed closed: {type(exc).__name__}: {exc}")
        )
        builder.record_validation(
            passed=False,
            detail={
                "kind": "procedure_preflight",
                "reason": str(refusal_error),
            },
        )
        builder.record_validation(
            passed=False,
            detail={
                "kind": "procedure_precondition",
                **precondition_detail,
                "reason": "not evaluated because procedure preflight was refused",
            },
        )
        receipt, finalized_run = _finalize_procedure_invocation(
            instance,
            procedure=procedure,
            started_run=started_run,
            builder=builder,
            verdict="refused",
            budget=budget,
            precondition_detail=precondition_detail,
            acceptance_config_digest=procedure.acceptance_config_digest,
            acceptance_lock_digest=procedure.acceptance_lock_digest,
            executed_config_digest=executed_config_digest,
            executed_lock_digest=executed_lock_digest,
            error=refusal_error,
        )
        _tag_procedure_exception(refusal_error, finalized_run, receipt)
        if refusal_error is exc:
            raise
        raise refusal_error from exc

    refusal: ConfigError | None = None
    refusal_receipt: Receipt | None = None
    refusal_run: ProcedureRun | None = None
    with instance.write_transaction() as uow:
        revision_value = uow.snapshots.get_instance_state(_READ_REVISION_STATE_KEY)
        read_revision = int(revision_value) if isinstance(revision_value, int) else 0
        authorization_procedure = _get_procedure(uow.procedures, procedure_id)
        authorization_definition_digest = compute_procedure_definition_digest(
            authorization_procedure.definition
        )
        if authorization_procedure.status != "live":
            refusal = ConfigError(
                f"Procedure '{authorization_procedure.procedure_id}' must be live to run; "
                f"found '{authorization_procedure.status}'"
            )
        elif authorization_procedure.definition_digest != procedure.definition_digest:
            refusal = ConfigError(
                "Procedure definition digest changed before authorization: "
                f"started={procedure.definition_digest}, "
                f"current={authorization_procedure.definition_digest}"
            )
        elif authorization_definition_digest != authorization_procedure.definition_digest:
            refusal = ConfigError(
                "Procedure definition digest changed before authorization: "
                f"stored={authorization_procedure.definition_digest}, "
                f"computed={authorization_definition_digest}"
            )

        satisfiers: list[tuple[str, str]] = []
        precondition_evaluated = refusal is None
        if precondition_evaluated:
            try:
                satisfiers = _procedure_precondition_satisfiers(
                    config,
                    uow.graph.load_graph(),
                    authorization_procedure.definition.precondition,
                )
            except Exception as exc:
                refusal = ConfigError(
                    f"Procedure precondition evaluation failed closed: {type(exc).__name__}: {exc}"
                )

        satisfied = refusal is None and (
            authorization_procedure.definition.precondition.is_empty or bool(satisfiers)
        )
        if not satisfied and refusal is None:
            refusal = ConfigError(
                f"Procedure '{procedure.procedure_id}' precondition was unsatisfied"
            )
        satisfying_ids = [entity_id for _, entity_id in satisfiers]
        precondition_detail = {
            "evaluated": precondition_evaluated,
            "read_revision": read_revision,
            "procedure_status": authorization_procedure.status,
            "definition_digest": authorization_procedure.definition_digest,
            "entity_type": authorization_procedure.definition.precondition.entity_type,
            "condition": dict(authorization_procedure.definition.precondition.condition or {}),
            "satisfied": satisfied,
            "satisfying_entity_ids": satisfying_ids,
            "satisfiers": [
                {"entity_type": entity_type, "entity_id": entity_id}
                for entity_type, entity_id in satisfiers
            ],
        }
        if refusal is not None:
            precondition_detail["reason"] = str(refusal)
        precondition_node = builder.record_validation(
            passed=satisfied,
            detail={"kind": "procedure_precondition", **precondition_detail},
        )
        for entity_type, entity_id in satisfiers:
            builder.record_entity_lookup(
                entity_type,
                entity_id,
                parent_id=precondition_node,
            )
        if not satisfied:
            assert refusal is not None
            refusal_receipt, refusal_run = _finalize_procedure_invocation_in_uow(
                uow,
                procedure=procedure,
                started_run=started_run,
                builder=builder,
                verdict="refused",
                budget=budget,
                precondition_detail=precondition_detail,
                acceptance_config_digest=procedure.acceptance_config_digest,
                acceptance_lock_digest=procedure.acceptance_lock_digest,
                executed_config_digest=plan.config_digest,
                executed_lock_digest=plan.lock_digest,
                error=refusal,
            )

    if refusal is not None:
        assert refusal_receipt is not None
        assert refusal_run is not None
        _tag_procedure_exception(refusal, refusal_run, refusal_receipt)
        raise refusal

    try:
        execution = execute_procedure_plan(
            instance,
            config,
            procedure.definition,
            plan,
            lock,
            builder,
            budget,
            actor_context=actor_context,
        )
    except Exception as exc:
        original_exc = exc
        failure: BaseException
        failed_receipt = getattr(original_exc, FAILED_WORKFLOW_RECEIPT_ATTR, None)
        if not isinstance(failed_receipt, Receipt):
            if isinstance(original_exc, CoreError):
                execution_error = original_exc
            else:
                execution_error = QueryExecutionError(
                    f"Unexpected procedure execution failure: {type(original_exc).__name__}"
                )
            builder.record_results([{"output": None, "error": str(execution_error)}])
            receipt = builder.build(results=[{"output": None, "error": str(execution_error)}])
            failure = execution_error
        else:
            receipt = failed_receipt
            failure = original_exc
        wall_clock_exceeded = budget.remaining_wall_clock_s() <= 0
        verdict: ProcedureRunVerdict = (
            "budget_exceeded"
            if isinstance(failure, ProcedureBudgetExceededError)
            or bool(getattr(failure, "budget_exceeded", False))
            or wall_clock_exceeded
            else "failed"
        )
        receipt, finalized_run = _persist_built_procedure_receipt(
            instance,
            procedure=procedure,
            started_run=started_run,
            receipt=receipt,
            verdict=verdict,
            budget=budget,
            precondition_detail=precondition_detail,
            acceptance_config_digest=procedure.acceptance_config_digest,
            acceptance_lock_digest=procedure.acceptance_lock_digest,
            executed_config_digest=plan.config_digest,
            executed_lock_digest=plan.lock_digest,
            error=failure,
        )
        _tag_procedure_exception(failure, finalized_run, receipt)
        if failure is original_exc:
            raise
        raise failure from original_exc

    receipt, finalized_run = _persist_built_procedure_receipt(
        instance,
        procedure=procedure,
        started_run=started_run,
        receipt=execution.receipt,
        verdict="succeeded",
        budget=budget,
        precondition_detail=precondition_detail,
        acceptance_config_digest=procedure.acceptance_config_digest,
        acceptance_lock_digest=procedure.acceptance_lock_digest,
        executed_config_digest=plan.config_digest,
        executed_lock_digest=plan.lock_digest,
        error=None,
    )
    return ProcedureExecutionResult(
        procedure=procedure,
        run=finalized_run,
        output=execution.output,
        receipt=receipt,
        step_outputs=execution.step_outputs,
    )


def _procedure_precondition_satisfiers(
    config: CoreConfig,
    graph: Any,
    precondition: ProcedurePrecondition,
) -> list[tuple[str, str]]:
    """Return live satisfiers in stable ID order for one named entity type."""
    if precondition.is_empty:
        return []
    assert precondition.entity_type is not None
    assert precondition.condition is not None
    satisfiers: list[tuple[str, str]] = []
    for entity in graph.list_entities(precondition.entity_type):
        if not entity_matches_query_state(entity.metadata, "live"):
            continue
        if entity_matches_property_equality_condition(
            config,
            entity,
            precondition.condition,
        ):
            satisfiers.append((precondition.entity_type, entity.entity_id))
    return sorted(satisfiers)


def _require_procedure_execution_tier(effective_tier: ProcedureTier) -> None:
    current_mode = get_current_mode()
    required_mode = _PERMISSION_BY_TIER[effective_tier]
    if current_mode < required_mode:
        raise ConfigError(
            f"Procedure execution requires tier '{effective_tier}', but the caller "
            f"ceiling is '{current_mode.name.lower()}'"
        )


def _procedure_budget_spent(
    budget: ProcedureExecutionBudget,
) -> ProcedureBudgetSpent:
    return ProcedureBudgetSpent(
        wall_clock_s=budget.elapsed_s(),
        provider_calls=budget.provider_calls,
    )


def _finalize_procedure_invocation(
    instance: InstanceProtocol,
    *,
    procedure: ProcedureRecord,
    started_run: ProcedureRun,
    builder: ReceiptBuilder,
    verdict: ProcedureRunVerdict,
    budget: ProcedureExecutionBudget,
    precondition_detail: dict[str, Any],
    acceptance_config_digest: str | None,
    acceptance_lock_digest: str | None,
    executed_config_digest: str | None,
    executed_lock_digest: str | None,
    error: BaseException | None,
) -> tuple[Receipt, ProcedureRun]:
    with instance.write_transaction() as uow:
        return _finalize_procedure_invocation_in_uow(
            uow,
            procedure=procedure,
            started_run=started_run,
            builder=builder,
            verdict=verdict,
            budget=budget,
            precondition_detail=precondition_detail,
            acceptance_config_digest=acceptance_config_digest,
            acceptance_lock_digest=acceptance_lock_digest,
            executed_config_digest=executed_config_digest,
            executed_lock_digest=executed_lock_digest,
            error=error,
        )


def _finalize_procedure_invocation_in_uow(
    uow: Any,
    *,
    procedure: ProcedureRecord,
    started_run: ProcedureRun,
    builder: ReceiptBuilder,
    verdict: ProcedureRunVerdict,
    budget: ProcedureExecutionBudget,
    precondition_detail: dict[str, Any],
    acceptance_config_digest: str | None,
    acceptance_lock_digest: str | None,
    executed_config_digest: str | None,
    executed_lock_digest: str | None,
    error: BaseException | None,
) -> tuple[Receipt, ProcedureRun]:
    results = [{"output": None, "error": str(error)}] if error is not None else [{"output": None}]
    builder.record_results(results)
    receipt = builder.build(results=results)
    return _persist_built_procedure_receipt_in_uow(
        uow,
        procedure=procedure,
        started_run=started_run,
        receipt=receipt,
        verdict=verdict,
        budget=budget,
        precondition_detail=precondition_detail,
        acceptance_config_digest=acceptance_config_digest,
        acceptance_lock_digest=acceptance_lock_digest,
        executed_config_digest=executed_config_digest,
        executed_lock_digest=executed_lock_digest,
        error=error,
    )


def _persist_built_procedure_receipt(
    instance: InstanceProtocol,
    *,
    procedure: ProcedureRecord,
    started_run: ProcedureRun,
    receipt: Receipt,
    verdict: ProcedureRunVerdict,
    budget: ProcedureExecutionBudget,
    precondition_detail: dict[str, Any],
    acceptance_config_digest: str | None,
    acceptance_lock_digest: str | None,
    executed_config_digest: str | None,
    executed_lock_digest: str | None,
    error: BaseException | None,
) -> tuple[Receipt, ProcedureRun]:
    with instance.write_transaction() as uow:
        return _persist_built_procedure_receipt_in_uow(
            uow,
            procedure=procedure,
            started_run=started_run,
            receipt=receipt,
            verdict=verdict,
            budget=budget,
            precondition_detail=precondition_detail,
            acceptance_config_digest=acceptance_config_digest,
            acceptance_lock_digest=acceptance_lock_digest,
            executed_config_digest=executed_config_digest,
            executed_lock_digest=executed_lock_digest,
            error=error,
        )


def _persist_built_procedure_receipt_in_uow(
    uow: Any,
    *,
    procedure: ProcedureRecord,
    started_run: ProcedureRun,
    receipt: Receipt,
    verdict: ProcedureRunVerdict,
    budget: ProcedureExecutionBudget,
    precondition_detail: dict[str, Any],
    acceptance_config_digest: str | None,
    acceptance_lock_digest: str | None,
    executed_config_digest: str | None,
    executed_lock_digest: str | None,
    error: BaseException | None,
) -> tuple[Receipt, ProcedureRun]:
    budget_spent = _procedure_budget_spent(budget)
    root_detail = receipt.nodes[0].detail
    root_detail.update(
        {
            "procedure_id": procedure.procedure_id,
            "definition_digest": procedure.definition_digest,
            "accepted_against": {
                "config_digest": acceptance_config_digest,
                "lock_digest": acceptance_lock_digest,
            },
            "executed_against": {
                "config_digest": executed_config_digest,
                "lock_digest": executed_lock_digest,
            },
            "precondition": precondition_detail,
            "budget": {
                "declared": procedure.definition.budget.model_dump(mode="json"),
                "spent": budget_spent.model_dump(mode="json"),
            },
            "verdict": verdict,
        }
    )
    if error is not None:
        root_detail.update(
            {
                "error": str(error),
                "error_type": type(error).__name__,
            }
        )
    if verdict == "budget_exceeded" or bool(getattr(error, "budget_exceeded", False)):
        root_detail["budget_exceeded"] = True
    if bool(getattr(error, "repeat_exhausted", False)):
        root_detail["repeat_exhausted"] = True
    receipt.committed = True
    uow.receipts.save_receipt(receipt)
    finalized_at = utc_now()
    updated = uow.procedures.finalize_run(
        started_run.run_id,
        verdict=verdict,
        budget_spent=budget_spent,
        receipt_id=receipt.receipt_id,
        finalized_at=format_datetime(finalized_at),
    )
    if not updated:
        raise QueryExecutionError(
            f"Procedure run '{started_run.run_id}' was not in started state at finalization"
        )
    finalized_run = uow.procedures.get_run(started_run.run_id)
    if finalized_run is None:
        raise QueryExecutionError(
            f"Procedure run '{started_run.run_id}' disappeared during finalization"
        )
    return receipt, finalized_run


def _tag_procedure_exception(
    exc: BaseException,
    run: ProcedureRun,
    receipt: Receipt,
) -> None:
    if isinstance(exc, CoreError):
        exc.mutation_receipt_id = receipt.receipt_id
    setattr(exc, "procedure_run_id", run.run_id)
    setattr(exc, "procedure_receipt_id", receipt.receipt_id)


def _transition_pending_procedure(
    instance: InstanceProtocol,
    procedure_id: str,
    *,
    action: Literal["accept", "reject"],
    expected_version: int | None,
    actor_context: GovernedActorContext | None,
    reason: str | None,
) -> ProcedureTransitionResult:
    with mutation_receipt(
        instance,
        "procedure_transition",
        {
            "action": action,
            "procedure_id": procedure_id,
            "expected_version": expected_version,
            "reason": reason,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        reviewer = _require_actor(actor_context, role="reviewer", builder=ctx.builder)
        procedure = _get_procedure(ctx.uow.procedures, procedure_id, builder=ctx.builder)
        _validate_status_and_version(
            procedure,
            expected_status="pending",
            expected_version=expected_version,
            builder=ctx.builder,
        )
        if action == "accept":
            _validate_reviewer_independence(procedure, reviewer, builder=ctx.builder)
        normalized_reason = None
        if action == "reject":
            normalized_reason = _require_reason(reason, action="reject", builder=ctx.builder)

        current_digest = compute_procedure_definition_digest(procedure.definition)
        if current_digest != procedure.definition_digest:
            _refuse(
                ctx.builder,
                "procedure definition digest changed since proposal: "
                f"stored={procedure.definition_digest}, computed={current_digest}",
            )

        now = utc_now()
        config_digest: str | None = None
        lock_digest: str | None = None
        if action == "accept":
            try:
                plan = compile_procedure_definition(instance, procedure.definition)
            except ConfigError as exc:
                ctx.builder.record_validation(
                    passed=False,
                    detail={"action": action, "reason": str(exc)},
                )
                raise
            config_digest = plan.config_digest
            lock_digest = plan.lock_digest
            allowed_live_ids = {procedure.procedure_id, procedure.supersedes_procedure_id}
            conflicting = sorted(
                row.procedure_id
                for row in ctx.uow.procedures.list_procedures(
                    name=procedure.definition.name, status="live"
                )
                if row.procedure_id not in allowed_live_ids
            )
            if conflicting:
                _refuse(
                    ctx.builder,
                    "another live procedure already holds name "
                    f"'{procedure.definition.name}': {', '.join(conflicting)}; "
                    "one live version per name",
                )

        updated = ctx.uow.procedures.transition_procedure(
            procedure_id,
            from_status="pending",
            to_status="live" if action == "accept" else "rejected",
            expected_version=procedure.version,
            resolved_actor_context=reviewer,
            resolved_at=format_datetime(now),
            reason=normalized_reason,
            acceptance_config_digest=config_digest,
            acceptance_lock_digest=lock_digest,
        )
        if not updated:
            _refuse(ctx.builder, "procedure changed during review")

        if action == "accept" and procedure.supersedes_procedure_id is not None:
            _retire_superseded_procedure(
                ctx.uow.procedures,
                procedure.supersedes_procedure_id,
                replacement_id=procedure_id,
                actor_context=reviewer,
                builder=ctx.builder,
            )

        transitioned = _get_procedure(ctx.uow.procedures, procedure_id)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "action": action,
                "procedure_id": procedure_id,
                "from_version": procedure.version,
                "to_version": transitioned.version,
                "definition_digest": procedure.definition_digest,
                "acceptance_config_digest": config_digest,
                "acceptance_lock_digest": lock_digest,
                "reason": normalized_reason,
            },
        )
        result = ProcedureTransitionResult(action=action, procedure=transitioned)
        ctx.set_result(result)
    return result


def _retire_superseded_procedure(
    store: ProcedureStoreProtocol,
    superseded_id: str,
    *,
    replacement_id: str,
    actor_context: GovernedActorContext,
    builder: ReceiptBuilder,
) -> None:
    superseded = _get_procedure(store, superseded_id, builder=builder)
    if superseded.status == "retired":
        return
    if superseded.status != "live":
        _refuse(
            builder,
            f"superseded procedure '{superseded_id}' is no longer live; "
            f"found '{superseded.status}'",
        )
    now = utc_now()
    reason = f"superseded by procedure '{replacement_id}'"
    updated = store.transition_procedure(
        superseded_id,
        from_status="live",
        to_status="retired",
        expected_version=superseded.version,
        retired_actor_context=actor_context,
        retired_at=format_datetime(now),
        reason=reason,
    )
    if not updated:
        _refuse(builder, f"superseded procedure '{superseded_id}' changed during acceptance")


def _get_procedure(
    store: ProcedureStoreProtocol,
    procedure_id: str,
    *,
    builder: ReceiptBuilder | None = None,
) -> ProcedureRecord:
    procedure = store.get_procedure(procedure_id)
    if procedure is None:
        if builder is not None:
            builder.record_validation(
                passed=False,
                detail={"reason": f"procedure '{procedure_id}' not found"},
            )
        raise ProcedureNotFoundError(procedure_id)
    return procedure


def _require_actor(
    actor_context: GovernedActorContext | None,
    *,
    role: str,
    builder: ReceiptBuilder,
) -> GovernedActorContext:
    if actor_context is None:
        _refuse(
            builder,
            f"procedure {role} actor context is required; missing/null attribution "
            "cannot prove reviewer independence",
        )
    return actor_context


def _validate_reviewer_independence(
    procedure: ProcedureRecord,
    reviewer: GovernedActorContext,
    *,
    builder: ReceiptBuilder,
) -> None:
    proposer = procedure.proposed_actor_context
    if proposer is None:
        _refuse(
            builder,
            "procedure proposer actor context is missing/null; reviewer independence "
            "cannot be proven",
        )
    if (proposer.org_id, proposer.actor_id) == (reviewer.org_id, reviewer.actor_id):
        _refuse(
            builder,
            "procedure reviewer must be independent from the proposer; "
            f"both identify actor '{reviewer.actor_id}' in org '{reviewer.org_id}'",
        )


def _validate_status_and_version(
    procedure: ProcedureRecord,
    *,
    expected_status: Literal["pending", "live"],
    expected_version: int | None,
    builder: ReceiptBuilder,
) -> None:
    if expected_version is None:
        _refuse(builder, "procedure transition requires expected_version")
    if procedure.status != expected_status:
        _refuse(
            builder,
            f"procedure '{procedure.procedure_id}' must be {expected_status}; "
            f"found '{procedure.status}'",
        )
    if procedure.version != expected_version:
        _refuse(
            builder,
            "procedure changed during review; expected version "
            f"{expected_version}, found {procedure.version}",
        )


def _require_reason(
    reason: str | None,
    *,
    action: Literal["reject", "retire"],
    builder: ReceiptBuilder,
) -> str:
    normalized = "" if reason is None else reason.strip()
    if not normalized:
        _refuse(builder, f"procedure {action} requires a non-empty reason")
    return normalized


def _refuse(builder: ReceiptBuilder, reason: str) -> NoReturn:
    builder.record_validation(passed=False, detail={"reason": reason})
    raise ConfigError(reason)


def _provider_tier(access: str) -> ProcedureTier:
    if access == "governed_write":
        return "governed_write"
    if access == "graph_write":
        return "graph_write"
    if access == "admin":
        return "admin"
    raise ConfigError(f"Unsupported procedure_access '{access}'")


def _validate_list_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ConfigError("Procedure list limit must be at least 1")
    if offset < 0:
        raise ConfigError("Procedure list offset must be at least 0")


__all__ = [
    "compile_procedure_definition",
    "service_get_procedure",
    "service_list_procedure_runs",
    "service_list_procedures",
    "service_accept_procedure",
    "service_propose_procedure",
    "service_reject_procedure",
    "service_retire_procedure",
    "validate_procedure_definition_against_config",
]
