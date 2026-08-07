"""Service-layer governance for state-held procedure definitions."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal, NoReturn

from cruxible_core.config.schema import ContractSchema, CoreConfig, WorkflowStepSchema
from cruxible_core.errors import (
    ConfigError,
    CoreError,
    PermissionDeniedError,
    ProcedureBudgetExceededError,
    ProcedureNotFoundError,
    ProcedureWithdrawalRefusedError,
    QueryExecutionError,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import EvidenceRef, normalize_evidence_ref
from cruxible_core.instance_protocol import InstanceProtocol, ProcedureStoreProtocol
from cruxible_core.primitives import canonical_json
from cruxible_core.procedure.graph_format import definition_format_version
from cruxible_core.procedure.types import (
    MAX_PROCEDURE_EVIDENCE_BYTES,
    PROCEDURE_EVIDENCE_HEAD_BYTES,
    ProcedureBudgetSpent,
    ProcedureContractFieldSchema,
    ProcedureContractSchema,
    ProcedureDefinition,
    ProcedureEvidenceArtifact,
    ProcedureExecutionResult,
    ProcedureGetResult,
    ProcedurePrecondition,
    ProcedureReadRecord,
    ProcedureRecord,
    ProcedureRefusalReason,
    ProcedureRepeatStepSchema,
    ProcedureRun,
    ProcedureRunStatus,
    ProcedureRunVerdict,
    ProcedureStatus,
    ProcedureTier,
    ProcedureTrackRecord,
    ProcedureTransitionResult,
    compute_procedure_definition_digest,
    unwrap_procedure_step,
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
from cruxible_core.workflow.contracts import (
    contract_field_is_required,
    contract_input_example,
    contract_reference_label,
    declared_fields,
    resolve_contract,
)
from cruxible_core.workflow.execution_context import ProcedureExecutionBudget
from cruxible_core.workflow.executor import (
    FAILED_WORKFLOW_RECEIPT_ATTR,
    execute_procedure_plan,
)
from cruxible_core.workflow.refs import iter_step_reference_templates
from cruxible_core.workflow.types import CompiledPlan

_logger = logging.getLogger(__name__)

_TIER_RANK = {"governed_write": 2, "graph_write": 3, "admin": 4}
_PERMISSION_BY_TIER = {
    "governed_write": PermissionMode.GOVERNED_WRITE,
    "graph_write": PermissionMode.GRAPH_WRITE,
    "admin": PermissionMode.ADMIN,
}
_READ_REVISION_STATE_KEY = "read_revision"
_MAX_REGISTERED_PROVIDERS_IN_ERROR = 40
_READ_IMPLYING_PROCEDURE_PREFIXES = ("review_", "get_", "list_", "check_", "inspect_")
_WHOLESALE_PASSTHROUGH_PARAMETER = "arguments"
"""Provider input key that conventionally carries an opaque argument bundle."""
_PREFERRED_PROVIDER_STEP_COUNT = 5
"""Provider steps above which one procedure is doing more than one job."""

WITHDRAW_NON_AUTHOR_PERMISSION = PermissionMode.GRAPH_WRITE
"""Tier required to withdraw a proposal the actor did not author.

Withdrawing someone else's pending proposal is a review act -- it decides that
proposal's fate without its author -- so it carries the tier ``accept`` and
``reject`` carry. The author's own retraction does not: it stays at the
proposing tier, which is the whole point of the verb.
"""

_PENDING_TERMINAL_STATUS: dict[str, ProcedureStatus] = {
    "accept": "live",
    "reject": "rejected",
    "withdraw": "withdrawn",
}


def _format_registered_providers(config: CoreConfig) -> str:
    provider_names = sorted(config.providers)
    if not provider_names:
        return "none"
    if len(provider_names) > _MAX_REGISTERED_PROVIDERS_IN_ERROR:
        shown = ", ".join(provider_names[:_MAX_REGISTERED_PROVIDERS_IN_ERROR])
        return f"{shown}, ... ({len(provider_names)} total; first 40 shown)"
    return ", ".join(provider_names)


def _format_valid_tiers() -> str:
    """List the accepted ``declared_tier`` values, weakest first."""
    return ", ".join(sorted(_TIER_RANK, key=lambda tier: _TIER_RANK[tier]))


def _tiers_at_or_above(tier: ProcedureTier) -> str:
    """List the ``declared_tier`` values that satisfy an effective tier floor."""
    floor = _TIER_RANK[tier]
    return ", ".join(
        sorted(
            (name for name, rank in _TIER_RANK.items() if rank >= floor),
            key=lambda name: _TIER_RANK[name],
        )
    )


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
    # Which provider forced the floor. Without it the tier refusal names a tier
    # the author never wrote down and leaves them to bisect the provider list to
    # find the one that raised it.
    tier_source: str | None = None
    for provider_name in sorted(definition.referenced_providers()):
        provider = config.providers.get(provider_name)
        if provider is None:
            raise ConfigError(
                f"Procedure '{definition.name}' references unknown provider '{provider_name}' "
                f"(registered providers: {_format_registered_providers(config)})"
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
            tier_source = provider_name

    if _TIER_RANK[definition.declared_tier] < _TIER_RANK[effective_tier]:
        forced_by = (
            f" required by provider '{tier_source}' "
            f"(procedure_access '{config.providers[tier_source].procedure_access}')"
            if tier_source is not None
            else ""
        )
        raise ConfigError(
            f"Procedure '{definition.name}' declares tier '{definition.declared_tier}' "
            f"below its effective provider tier '{effective_tier}'{forced_by}; "
            f"set declared_tier to one of: {_tiers_at_or_above(effective_tier)}"
        )
    return definition.declared_tier


def compile_procedure_definition(
    instance: InstanceProtocol,
    definition: ProcedureDefinition,
    input_payload: dict[str, Any] | None = None,
) -> CompiledPlan:
    """Compile a state-held procedure definition against the active config/lock."""
    plan, _ = _compile_procedure_definition(instance, definition, input_payload)
    return plan


def _compile_procedure_definition(
    instance: InstanceProtocol,
    definition: ProcedureDefinition,
    input_payload: dict[str, Any] | None = None,
) -> tuple[CompiledPlan, list[str]]:
    """Compile a procedure and return its non-blocking authoring warnings."""
    config = instance.load_config()
    validate_procedure_definition_against_config(definition, config)
    warnings = lint_procedure_definition_authoring(definition, config)
    lock = load_lock(resolve_lock_path(instance))
    return (
        compile_plan_definition(
            config,
            lock,
            definition.name,
            definition,
            input_payload,
            config_base_path=instance.get_config_path().parent,
            definition_label="Procedure",
        ),
        warnings,
    )


def lint_procedure_definition_authoring(
    definition: ProcedureDefinition,
    config: CoreConfig,
) -> list[str]:
    """Block impossible input refs and return deterministic authoring warnings."""
    contract = resolve_contract(config, definition.contract_in)
    if contract is None:
        # The compiler owns the existing unknown-contract diagnostic.
        return []

    references = _procedure_step_input_references(definition)
    consumed_fields: set[str] = set()
    for step_id, reference in references:
        if reference == "$input":
            consumed_fields.update(contract.fields)
            continue
        field_name = _input_reference_field(reference)
        if field_name is None:
            continue
        consumed_fields.add(field_name)
        if field_name not in contract.fields and not contract.allow_extra:
            # An ``allow_extra`` contract (built-in ``cruxible.JsonObject``, or
            # any config contract that opts in) accepts keys it never declared,
            # so an undeclared reference is not statically impossible there --
            # the payload may legitimately carry it. Only a closed contract can
            # prove the reference can never resolve.
            contract_name = contract_reference_label(definition.contract_in)
            raise ConfigError(
                f"Procedure '{definition.name}' step '{step_id}' input reference "
                f"'{reference}' uses undeclared contract_in field '{field_name}'; "
                f"contract '{contract_name}' declares: {declared_fields(contract)}"
            )

    warnings: list[str] = []
    if not contract.allow_extra:
        warnings.extend(
            f"contract_in field '{field_name}' is declared but not consumed by any procedure step"
            for field_name in sorted(set(contract.fields) - consumed_fields)
        )

    read_implying_name = definition.name.lower().startswith(_READ_IMPLYING_PROCEDURE_PREFIXES)
    for step in _procedure_workflow_steps(definition):
        if step.provider is not None:
            provider = config.providers.get(step.provider)
            if read_implying_name and provider is not None and provider.side_effects:
                warnings.append(
                    f"procedure name '{definition.name}' implies a read, but step "
                    f"'{step.id}' uses side-effecting provider '{step.provider}'"
                )
            warnings.extend(_stringified_object_input_warnings(step.id, step.input))
            warnings.extend(_wholesale_passthrough_warnings(step.id, step.input, contract))

    warnings.extend(_read_fanout_warnings(definition, config))

    provider_call_count = definition.static_expansion().expanded_provider_calls
    if definition.budget.max_provider_calls > provider_call_count:
        # Under-provisioning is refused by ``ProcedureDefinition`` itself, so the
        # only mismatch that can reach here is slack above the static maximum --
        # headroom the run can never reach, which quietly disarms the ceiling as
        # a review signal.
        warnings.append(
            "budget.max_provider_calls "
            f"({definition.budget.max_provider_calls}) exceeds the expanded "
            f"provider-call count ({provider_call_count}); the extra headroom is unreachable"
        )
    return warnings


def _procedure_workflow_steps(definition: ProcedureDefinition) -> list[WorkflowStepSchema]:
    """Flatten a definition to the plain workflow steps a reference scan walks.

    Wrappers unwrap; guards are skipped, because a guard carries no reference
    template fields -- its operands are the predicate grammar's business, not
    the resolver's.
    """
    steps: list[WorkflowStepSchema] = []
    for wrapper in definition.steps:
        step = unwrap_procedure_step(wrapper)
        if isinstance(step, ProcedureRepeatStepSchema):
            steps.extend(step.repeat.steps)
        elif isinstance(step, WorkflowStepSchema):
            steps.append(step)
    return steps


def _procedure_step_input_references(
    definition: ProcedureDefinition,
) -> list[tuple[str, str]]:
    """Collect ``$input`` references from the fields the resolver actually walks.

    Scope equals resolution scope: ``iter_step_reference_templates`` selects the
    same step fields :func:`resolve_value` visits, and nothing else. Scanning the
    whole dumped step instead would read literal prose -- an assert ``message``
    quoting ``$input.foo`` to explain a failure -- as a reference and block a
    definition that runs correctly.
    """
    references: list[tuple[str, str]] = []
    for step in _procedure_workflow_steps(definition):
        dumped = step.model_dump(mode="python", by_alias=True, exclude_none=True)
        for template in iter_step_reference_templates(dumped):
            references.extend((step.id, ref) for ref in _input_references(template))
    return references


def _input_references(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value == "$input" or value.startswith("$input.") else []
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _input_references(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _input_references(item)]
    return []


def _input_reference_field(reference: str) -> str | None:
    if not reference.startswith("$input."):
        return None
    path = reference[len("$input.") :]
    return path.split(".", 1)[0].split("[", 1)[0]


def _stringified_object_input_warnings(step_id: str, value: Any) -> list[str]:
    warnings: list[str] = []

    def visit(item: Any, path: str) -> None:
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except (json.JSONDecodeError, TypeError):
                return
            if isinstance(parsed, dict):
                warnings.append(
                    f"step '{step_id}' input value at '{path}' is a stringified JSON "
                    "object; pass the object directly"
                )
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                visit(nested, f"{path}.{key}" if path else str(key))
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")

    visit(value, "input")
    return warnings


def _wholesale_passthrough_warnings(
    step_id: str,
    value: Any,
    contract: ContractSchema,
) -> list[str]:
    """Flag a declared string field handed whole to an ``arguments`` parameter.

    Feeding one contract field entire into a parameter named ``arguments`` --
    the ``call_discoverable_agent_tool``-style string argument bundle -- routes
    around the contract: whatever the caller packed into that one string is
    never type-checked, and the declared shape stops describing what the tool
    actually receives. The fix is to declare the individual fields the tool
    needs, so the reference is a warning rather than a refusal.
    """
    warnings: list[str] = []

    def visit(item: Any, path: str, key: str | None) -> None:
        if isinstance(item, str):
            if key != _WHOLESALE_PASSTHROUGH_PARAMETER:
                return
            field_name = _whole_input_reference_field(item)
            if field_name is None:
                return
            field_schema = contract.fields.get(field_name)
            if field_schema is None or field_schema.type != "string":
                return
            warnings.append(
                f"step '{step_id}' input at '{path}' passes the whole contract_in field "
                f"'{field_name}' into an '{_WHOLESALE_PASSTHROUGH_PARAMETER}' parameter; "
                "the contract cannot validate what that string carries -- declare the "
                "individual fields the provider needs"
            )
            return
        if isinstance(item, dict):
            for nested_key, nested in item.items():
                visit(nested, f"{path}.{nested_key}" if path else str(nested_key), str(nested_key))
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]", key)

    visit(value, "input", None)
    return warnings


def _whole_input_reference_field(reference: str) -> str | None:
    """Return the field name when a reference is one whole ``$input.<field>``.

    A path or index into the field (``$input.spec.limit``) is a narrowed read
    and does not defeat validation; only the undivided field does.
    """
    if not reference.startswith("$input."):
        return None
    path = reference[len("$input.") :]
    if not path or "." in path or "[" in path:
        return None
    return path


def _read_fanout_warnings(
    definition: ProcedureDefinition,
    config: CoreConfig,
) -> list[str]:
    """Prefer small, single-purpose procedures over read-plus-write omnibuses.

    A definition that reads widely and then writes is two procedures wearing one
    name: the reads are re-runnable and cheap to review, the write is neither,
    and bundling them means every review of the read half re-reviews the write.
    Guidance only -- a legitimately wide procedure is still proposable.
    """
    read_steps: list[str] = []
    side_effecting_steps: list[str] = []
    provider_steps: list[str] = []
    for step in _procedure_workflow_steps(definition):
        if step.query is not None:
            read_steps.append(step.id)
            continue
        if step.provider is None:
            continue
        provider_steps.append(step.id)
        provider = config.providers.get(step.provider)
        if provider is not None and provider.side_effects:
            side_effecting_steps.append(step.id)
        else:
            read_steps.append(step.id)

    warnings: list[str] = []
    if side_effecting_steps and len(read_steps) > 1:
        warnings.append(
            f"procedure mixes {len(read_steps)} read steps "
            f"({', '.join(read_steps)}) with {len(side_effecting_steps)} side-effecting step(s) "
            f"({', '.join(side_effecting_steps)}); consider splitting reads into a "
            "read-only bundle"
        )
    if len(provider_steps) > _PREFERRED_PROVIDER_STEP_COUNT:
        warnings.append(
            f"procedure declares {len(provider_steps)} provider steps, above the "
            f"{_PREFERRED_PROVIDER_STEP_COUNT}-step guidance for one procedure; "
            "consider splitting reads into a read-only bundle"
        )
    return warnings


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
    format_version, format_warnings = definition_format_version(definition)
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
                # An author who changed their mind about their own PENDING
                # proposal used to dead-end here and route around it by
                # proposing a renamed variant, polluting the namespace. Name the
                # verb that actually resolves it.
                remedy = (
                    "; the author may withdraw the pending proposal and re-propose"
                    if superseded.status == "pending"
                    else ""
                )
                _refuse(
                    ctx.builder,
                    f"superseded procedure '{supersedes_procedure_id}' must be live; "
                    f"found '{superseded.status}'{remedy}",
                )
            if superseded.definition.name != definition.name:
                _refuse(
                    ctx.builder,
                    "a procedure may only supersede a live definition with the same name",
                )

        try:
            # One compile, one lint: the compile helper already runs the
            # authoring lint and hands back its warnings.
            plan, lint_warnings = _compile_procedure_definition(instance, definition)
        except ConfigError as exc:
            ctx.builder.record_validation(
                passed=False,
                detail={"action": "propose", "reason": str(exc)},
            )
            raise
        # The R14 warning has to reach the authoring channel, so it travels with
        # the lint's warnings rather than in a second, parallel warning list.
        warnings = [*format_warnings, *lint_warnings]

        procedure = ProcedureRecord(
            definition=definition,
            definition_digest=definition_digest,
            supersedes_procedure_id=supersedes_procedure_id,
            evidence_refs=[normalize_evidence_ref(ref) for ref in evidence_refs],
            proposed_actor_context=proposer,
            definition_format_version=format_version,
        )
        ctx.uow.procedures.save_procedure(procedure)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "action": "propose",
                "procedure_id": procedure.procedure_id,
                "definition_digest": definition_digest,
                "definition_format_version": format_version,
                "config_digest": plan.config_digest,
                "lock_digest": plan.lock_digest,
                "warnings": warnings,
            },
        )
        result = ProcedureTransitionResult(
            action="propose",
            procedure=procedure,
            warnings=warnings,
        )
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


def service_withdraw_procedure(
    instance: InstanceProtocol,
    procedure_id: str,
    *,
    expected_version: int | None,
    reason: str | None = None,
    actor_context: GovernedActorContext | None,
) -> ProcedureTransitionResult:
    """Retract one pending proposal as its author (or as a reviewer).

    The author's counterpart to ``reject``. ``reject`` is a reviewer's verdict on
    someone else's proposal and requires a reason; ``withdraw`` is the proposing
    actor taking their own proposal back, so the reason is optional and the
    terminal status is ``withdrawn`` rather than ``rejected`` -- the record says
    which of the two happened.

    A withdrawn proposal is not live, so the one-live-definition-per-name law is
    untouched and the name is immediately free for a fresh proposal. That is the
    point: before this verb existed, an author who changed their mind mid-review
    could neither supersede their own pending proposal (supersede requires a LIVE
    target) nor reject it without claiming a reviewer's verdict, so agents
    invented ``_v2`` name variants instead and then broke their own name-based
    lookups.
    """
    return _transition_pending_procedure(
        instance,
        procedure_id,
        action="withdraw",
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
) -> ProcedureReadRecord:
    """Read one procedure record."""
    store = instance.get_procedure_store()
    try:
        procedure = _get_procedure(store, procedure_id)
        track_records = store.get_run_track_records([procedure_id])
        return _procedure_read_record(procedure, track_records.get(procedure_id))
    finally:
        store.close()


def service_get_procedure_details(
    instance: InstanceProtocol,
    procedure_id: str,
) -> ProcedureGetResult:
    """Read a procedure with the active config's resolved input field schema."""
    procedure = service_get_procedure(instance, procedure_id)
    config = instance.load_config()
    contract = resolve_contract(config, procedure.definition.contract_in)
    if contract is None:
        return ProcedureGetResult(procedure=procedure, contract_in_schema=None)
    return ProcedureGetResult(
        procedure=procedure,
        contract_in_schema=ProcedureContractSchema(
            description=contract.description,
            fields=[
                ProcedureContractFieldSchema(
                    name=field_name,
                    # One shared requiredness predicate with the contract
                    # rejection message: a defaulted field is filled in by
                    # contract validation before the optional check runs, so the
                    # caller never has to supply it.
                    required=contract_field_is_required(field_schema),
                    type=field_schema.type,
                    default=field_schema.default,
                    enum=field_schema.enum,
                    enum_ref=field_schema.enum_ref,
                    description=field_schema.description,
                    json_schema=field_schema.json_schema,
                )
                for field_name, field_schema in sorted(contract.fields.items())
            ],
            allow_extra=contract.allow_extra,
            input_example=contract_input_example(config, contract),
        ),
    )


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
        track_records = store.get_run_track_records([procedure.procedure_id for procedure in items])
        read_items = [
            _procedure_read_record(
                procedure,
                track_records.get(procedure.procedure_id),
            )
            for procedure in items
        ]
        total = store.count_procedures(name=name, status=status)
        return ListResult(
            items=read_items,
            total=total,
            limit=limit,
            offset=offset,
            truncated=list_truncated(total=total, offset=offset, returned=len(read_items)),
            read_revision=instance.get_read_revision(),
        )
    finally:
        store.close()


def _procedure_read_record(
    procedure: ProcedureRecord,
    track_record: ProcedureTrackRecord | None,
) -> ProcedureReadRecord:
    """Widen a stored record to its read shape, revalidating the whole thing.

    ``model_construct`` would be faster and would skip exactly the check worth
    keeping: the store's record and the read record share every field, so a
    field added to one and not the other must fail here rather than silently
    produce a half-populated read payload.
    """
    return ProcedureReadRecord(
        **dict(procedure),
        track_record=track_record or ProcedureTrackRecord(),
    )


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
    # Classified where the refusal is DECIDED, not reconstructed from the
    # message afterwards: the message carries ids and digests, so the read
    # surface could never count it. Checks that live inside the preflight
    # helpers below share the generic bucket; the receipt keeps their detail.
    preflight_reason: ProcedureRefusalReason = "preflight_refused"

    try:
        if procedure.status != "live":
            preflight_reason = "procedure_not_live"
            raise ConfigError(
                f"Procedure '{procedure.procedure_id}' must be live to run; "
                f"found '{procedure.status}'"
            )
        current_definition_digest = compute_procedure_definition_digest(procedure.definition)
        if current_definition_digest != procedure.definition_digest:
            preflight_reason = "definition_digest_changed"
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
        # Ordered after the definition-vs-config checks on purpose: when the
        # config drift is one those checks already name precisely (a provider
        # de-exported, removed, or tier-raised), the operator is better served by
        # that specific refusal than by the generic pin mismatch.
        _verify_acceptance_pins(
            procedure,
            executed_config_digest=executed_config_digest,
            executed_lock_digest=executed_lock_digest,
        )
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
            if isinstance(exc, ConfigError | PermissionDeniedError)
            else ConfigError(f"Procedure preflight failed closed: {type(exc).__name__}: {exc}")
        )
        if preflight_reason == "preflight_refused" and isinstance(
            refusal_error, PermissionDeniedError
        ):
            preflight_reason = "tier_not_permitted"
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
            refusal_reason=preflight_reason,
        )
        _tag_procedure_exception(refusal_error, finalized_run, receipt)
        if refusal_error is exc:
            raise
        raise refusal_error from exc

    refusal: ConfigError | None = None
    refusal_reason: ProcedureRefusalReason | None = None
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
            refusal_reason = "procedure_not_live"
            refusal = ConfigError(
                f"Procedure '{authorization_procedure.procedure_id}' must be live to run; "
                f"found '{authorization_procedure.status}'"
            )
        elif authorization_procedure.definition_digest != procedure.definition_digest:
            refusal_reason = "definition_digest_changed"
            refusal = ConfigError(
                "Procedure definition digest changed before authorization: "
                f"started={procedure.definition_digest}, "
                f"current={authorization_procedure.definition_digest}"
            )
        elif authorization_definition_digest != authorization_procedure.definition_digest:
            refusal_reason = "definition_digest_changed"
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
                refusal_reason = "precondition_evaluation_failed"
                refusal = ConfigError(
                    f"Procedure precondition evaluation failed closed: {type(exc).__name__}: {exc}"
                )

        satisfied = refusal is None and (
            authorization_procedure.definition.precondition.is_empty or bool(satisfiers)
        )
        if not satisfied and refusal is None:
            refusal_reason = "precondition_unsatisfied"
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
            assert refusal_reason is not None
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
                refusal_reason=refusal_reason,
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

    with instance.write_transaction() as uow:
        receipt, finalized_run = _persist_built_procedure_receipt_in_uow(
            uow,
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
        # Evidence rows commit atomically with the run finalize: a crash here
        # rolls back both, leaving the run 'started' (crash-visible) instead of
        # a succeeded run with silently absent declared evidence. A
        # deterministic persistence failure must not fail a run that already
        # succeeded, so it degrades to no auto-refs with a logged warning.
        try:
            evidence_refs = _persist_procedure_evidence_outputs_in_uow(
                uow,
                procedure=procedure,
                run=finalized_run,
                receipt=receipt,
                output=execution.output,
                step_outputs=execution.step_outputs,
            )
        except Exception:
            _logger.warning(
                "procedure evidence persistence failed for run %s; "
                "the run succeeded but returns no auto evidence refs",
                finalized_run.run_id,
                exc_info=True,
            )
            evidence_refs = []
    return ProcedureExecutionResult(
        procedure=procedure,
        run=finalized_run,
        output=execution.output,
        receipt=receipt,
        step_outputs=execution.step_outputs,
        evidence_refs=evidence_refs,
    )


def _verify_acceptance_pins(
    procedure: ProcedureRecord,
    *,
    executed_config_digest: str,
    executed_lock_digest: str,
) -> None:
    """Refuse a run whose config/lock differ from the ones acceptance pinned.

    Acceptance records the config and lock digests the reviewer recompiled the
    definition against, and every run receipt reports ``accepted_against`` beside
    ``executed_against``. Recompiling at run time proves the definition still
    compiles -- it does not prove it compiles against the same modelled world the
    reviewer approved. A provider re-pointed at a different endpoint, an entity
    type redefined, a query rewritten: each recompiles cleanly while changing
    what the approved procedure actually does. The pins are compared here so that
    divergence is a refusal on the receipt rather than a field nobody reads.

    A MISSING pin fails closed for the same reason a mismatched one does: with no
    recorded digest there is no approved world to compare against, and "no pin"
    would otherwise be the one way to run a procedure unverified. Nothing
    legitimate is caught by this -- acceptance writes both pins, and clones and
    snapshots carry them across -- the target is a row written before the columns
    existed, which would otherwise run indefinitely with its acceptance
    unverifiable.
    """
    missing = [
        label
        for label, accepted in (
            ("config_digest", procedure.acceptance_config_digest),
            ("lock_digest", procedure.acceptance_lock_digest),
        )
        if accepted is None
    ]
    if missing:
        raise ConfigError(
            f"Procedure '{procedure.procedure_id}' has no recorded acceptance "
            f"{' or '.join(missing)}, so there is no accepted config and lock to verify "
            "this run against. The procedure predates acceptance pinning; running it "
            "would execute against a model no reviewer is known to have approved, so it "
            "is refused. Recover by re-proposing the definition and having an "
            "independent reviewer accept it against the current config and lock "
            f"(`cruxible procedure propose <file> --supersedes {procedure.procedure_id}`, "
            "then `cruxible procedure resolve <new-id> --action accept`)."
        )
    mismatches = [
        (label, accepted, executed)
        for label, accepted, executed in (
            ("config_digest", procedure.acceptance_config_digest, executed_config_digest),
            ("lock_digest", procedure.acceptance_lock_digest, executed_lock_digest),
        )
        if accepted is not None and accepted != executed
    ]
    if not mismatches:
        return
    detail = "; ".join(
        f"{label}: accepted against {accepted}, now {executed}"
        for label, accepted, executed in mismatches
    )
    raise ConfigError(
        f"Procedure '{procedure.procedure_id}' is pinned to the config and lock it was "
        f"accepted against, which no longer match this instance ({detail}). The run is "
        "refused rather than executed against an unreviewed model. Recover by "
        "re-proposing the definition and having an independent reviewer accept it "
        "against the current config and lock (`cruxible procedure propose <file> "
        f"--supersedes {procedure.procedure_id}`, then `cruxible procedure resolve "
        "<new-id> --action accept`), or by restoring the accepted config and "
        "re-running `cruxible lock`."
    )


def _persist_procedure_evidence_outputs_in_uow(
    uow: Any,
    *,
    procedure: ProcedureRecord,
    run: ProcedureRun,
    receipt: Receipt,
    output: Any,
    step_outputs: Mapping[str, Any],
) -> list[EvidenceRef]:
    """Persist only the definition-approved typed outputs as whole artifacts."""
    declared = procedure.definition.evidence_outputs
    selected = (
        [(procedure.definition.returns, output)]
        if declared is None
        else [(alias, step_outputs[alias]) for alias in declared]
    )
    if not selected:
        return []
    for output_alias, value in selected:
        artifact = _procedure_evidence_artifact(value)
        uow.procedures.save_evidence_artifact(artifact)
        uow.procedures.link_run_evidence(
            run_id=run.run_id,
            output_alias=output_alias,
            artifact_id=artifact.artifact_id,
            receipt_id=receipt.receipt_id,
        )
    refs: list[EvidenceRef] = uow.procedures.list_run_evidence_refs(run.run_id)
    return refs


def _procedure_evidence_artifact(value: Any) -> ProcedureEvidenceArtifact:
    canonical = canonical_json(value)
    encoded = canonical.encode("utf-8")
    digest_hex = hashlib.sha256(encoded).hexdigest()
    content_digest = f"sha256:{digest_hex}"
    oversized = len(encoded) > MAX_PROCEDURE_EVIDENCE_BYTES
    return ProcedureEvidenceArtifact(
        artifact_id=f"PJA-{digest_hex}",
        content_digest=content_digest,
        byte_count=len(encoded),
        payload=None if oversized else value,
        truncated_head=(
            encoded[:PROCEDURE_EVIDENCE_HEAD_BYTES].decode("utf-8", errors="replace")
            if oversized
            else None
        ),
        oversized=oversized,
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
        raise PermissionDeniedError(
            "cruxible_run_procedure",
            current_mode.name,
            required_mode.name,
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
    refusal_reason: ProcedureRefusalReason | None = None,
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
            refusal_reason=refusal_reason,
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
    refusal_reason: ProcedureRefusalReason | None = None,
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
        refusal_reason=refusal_reason,
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
    refusal_reason: ProcedureRefusalReason | None = None,
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
            refusal_reason=refusal_reason,
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
    refusal_reason: ProcedureRefusalReason | None = None,
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
    if refusal_reason is not None:
        root_detail["refusal_reason"] = refusal_reason
    receipt.committed = True
    uow.receipts.save_receipt(receipt)
    finalized_at = utc_now()
    updated = uow.procedures.finalize_run(
        started_run.run_id,
        verdict=verdict,
        budget_spent=budget_spent,
        receipt_id=receipt.receipt_id,
        finalized_at=format_datetime(finalized_at),
        refusal_reason=refusal_reason,
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
    action: Literal["accept", "reject", "withdraw"],
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
        if action == "withdraw":
            resolving_actor = _require_actor(
                actor_context,
                role="withdrawing author",
                builder=ctx.builder,
                rationale="cannot prove the withdrawal is the proposal's own author",
            )
        else:
            resolving_actor = _require_actor(actor_context, role="reviewer", builder=ctx.builder)
        procedure = _get_procedure(ctx.uow.procedures, procedure_id, builder=ctx.builder)
        _validate_status_and_version(
            procedure,
            expected_status="pending",
            expected_version=expected_version,
            builder=ctx.builder,
        )
        if action == "accept":
            _validate_reviewer_independence(procedure, resolving_actor, builder=ctx.builder)
        withdrawn_by: Literal["author", "reviewer"] | None = None
        if action == "withdraw":
            withdrawn_by = _authorize_withdrawal(procedure, resolving_actor, builder=ctx.builder)
        normalized_reason = None
        if action == "reject":
            normalized_reason = _require_reason(reason, action="reject", builder=ctx.builder)
        elif action == "withdraw":
            # Optional: an author retracting their own proposal owes no verdict.
            normalized_reason = (reason or "").strip() or None

        format_version, format_warnings = definition_format_version(procedure.definition)
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
            to_status=_PENDING_TERMINAL_STATUS[action],
            expected_version=procedure.version,
            resolved_actor_context=resolving_actor,
            resolved_at=format_datetime(now),
            reason=normalized_reason,
            acceptance_config_digest=config_digest,
            acceptance_lock_digest=lock_digest,
        )
        if not updated:
            _refuse(
                ctx.builder,
                "procedure changed during withdrawal"
                if action == "withdraw"
                else "procedure changed during review",
            )

        if action == "accept" and procedure.supersedes_procedure_id is not None:
            _retire_superseded_procedure(
                ctx.uow.procedures,
                procedure.supersedes_procedure_id,
                replacement_id=procedure_id,
                actor_context=resolving_actor,
                builder=ctx.builder,
            )

        transitioned = _get_procedure(ctx.uow.procedures, procedure_id)
        detail: dict[str, Any] = {
            "action": action,
            "procedure_id": procedure_id,
            "from_version": procedure.version,
            "to_version": transitioned.version,
            "definition_digest": procedure.definition_digest,
            "definition_format_version": format_version,
            "acceptance_config_digest": config_digest,
            "acceptance_lock_digest": lock_digest,
            "reason": normalized_reason,
        }
        if format_warnings:
            # Recorded, not returned: acceptance has no authoring channel, and a
            # warning the reviewer acted under belongs on the receipt.
            detail["format_warnings"] = format_warnings
        if withdrawn_by is not None:
            detail["withdrawn_by"] = withdrawn_by
        ctx.builder.record_validation(passed=True, detail=detail)
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
    rationale: str = "cannot prove reviewer independence",
) -> GovernedActorContext:
    if actor_context is None:
        _refuse(
            builder,
            f"procedure {role} actor context is required; missing/null attribution {rationale}",
        )
    return actor_context


def _authorize_withdrawal(
    procedure: ProcedureRecord,
    actor: GovernedActorContext,
    *,
    builder: ReceiptBuilder,
) -> Literal["author", "reviewer"]:
    """Admit the proposal's own author, or anyone holding the reviewer tier.

    Identity comes from the proposal's recorded ``proposed_actor_context`` -- the
    attribution written when the proposal was created -- compared on the same
    ``(org_id, actor_id)`` pair reviewer independence uses. A proposal with no
    recorded author cannot be matched by anyone, so only the reviewer tier can
    retract it.
    """
    proposer = procedure.proposed_actor_context
    if proposer is not None and (proposer.org_id, proposer.actor_id) == (
        actor.org_id,
        actor.actor_id,
    ):
        return "author"

    current_mode = get_current_mode()
    if current_mode >= WITHDRAW_NON_AUTHOR_PERMISSION:
        return "reviewer"

    author_label = (
        f"actor '{proposer.actor_id}' in org '{proposer.org_id}'"
        if proposer is not None
        else "an unattributed author"
    )
    reason = (
        f"procedure '{procedure.procedure_id}' may be withdrawn only by its proposing author "
        f"({author_label}) at their own tier, or by a reviewer holding "
        f"{WITHDRAW_NON_AUTHOR_PERMISSION.name}; actor '{actor.actor_id}' in org "
        f"'{actor.org_id}' is neither (current mode {current_mode.name})"
    )
    builder.record_validation(passed=False, detail={"action": "withdraw", "reason": reason})
    raise ProcedureWithdrawalRefusedError(
        procedure.procedure_id,
        current_mode=current_mode.name,
        required_mode=WITHDRAW_NON_AUTHOR_PERMISSION.name,
        message=reason,
    )


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
    raise ConfigError(
        f"Unsupported procedure_access '{access}'; valid values: disabled, {_format_valid_tiers()}"
    )


def _validate_list_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ConfigError("Procedure list limit must be at least 1")
    if offset < 0:
        raise ConfigError("Procedure list offset must be at least 0")


__all__ = [
    "compile_procedure_definition",
    "lint_procedure_definition_authoring",
    "service_get_procedure",
    "service_get_procedure_details",
    "service_list_procedure_runs",
    "service_list_procedures",
    "service_accept_procedure",
    "service_propose_procedure",
    "service_reject_procedure",
    "service_retire_procedure",
    "service_withdraw_procedure",
    "validate_procedure_definition_against_config",
]
