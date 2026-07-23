"""Service-layer governance for state-held procedure definitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, NoReturn

from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError, ProcedureNotFoundError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import EvidenceRef, normalize_evidence_ref
from cruxible_core.instance_protocol import InstanceProtocol, ProcedureStoreProtocol
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    ProcedureRecord,
    ProcedureTier,
    ProcedureTransitionResult,
    compute_procedure_definition_digest,
)
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.temporal import format_datetime, utc_now
from cruxible_core.workflow.compiler import (
    compile_plan_definition,
    load_lock,
    resolve_lock_path,
)
from cruxible_core.workflow.types import CompiledPlan

_TIER_RANK = {"governed_write": 2, "graph_write": 3, "admin": 4}


def validate_procedure_definition_against_config(
    definition: ProcedureDefinition,
    config: CoreConfig,
) -> ProcedureTier:
    """Validate provider exports and return the procedure's effective tier."""
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
        None,
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


def service_promote_procedure(
    instance: InstanceProtocol,
    procedure_id: str,
    *,
    expected_version: int | None,
    actor_context: GovernedActorContext | None,
) -> ProcedureTransitionResult:
    """Promote a pending definition after independent review and recompilation."""
    return _transition_pending_procedure(
        instance,
        procedure_id,
        action="promote",
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
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ProcedureRecord]:
    """Read procedure records without exposing a CLI/MCP/HTTP surface."""
    store = instance.get_procedure_store()
    try:
        return store.list_procedures(
            name=name,
            status=status,
            limit=limit,
            offset=offset,
        )
    finally:
        store.close()


def _transition_pending_procedure(
    instance: InstanceProtocol,
    procedure_id: str,
    *,
    action: Literal["promote", "reject"],
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
        if action == "promote":
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

        updated = ctx.uow.procedures.transition_procedure(
            procedure_id,
            from_status="pending",
            to_status="live" if action == "promote" else "rejected",
            expected_version=procedure.version,
            resolved_actor_context=reviewer,
            resolved_at=format_datetime(now),
            reason=normalized_reason,
            promoted_config_digest=config_digest,
            promoted_lock_digest=lock_digest,
        )
        if not updated:
            _refuse(ctx.builder, "procedure changed during review")

        if action == "promote" and procedure.supersedes_procedure_id is not None:
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
                "promoted_config_digest": config_digest,
                "promoted_lock_digest": lock_digest,
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
        _refuse(builder, f"superseded procedure '{superseded_id}' changed during promotion")


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


__all__ = [
    "compile_procedure_definition",
    "service_get_procedure",
    "service_list_procedures",
    "service_promote_procedure",
    "service_propose_procedure",
    "service_reject_procedure",
    "service_retire_procedure",
    "validate_procedure_definition_against_config",
]
