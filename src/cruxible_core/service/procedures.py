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
from cruxible_core.procedure.analysis import (
    build_procedure_graph,
    enumerate_control_paths,
    has_path_avoiding,
)
from cruxible_core.procedure.digest import compute_node_digests
from cruxible_core.procedure.graph_format import (
    DEFINITION_FORMAT_V1,
    GRAPH_FORMAT_DECLARED_WITHOUT_CONSTRUCT,
    definition_format_version,
)
from cruxible_core.procedure.guards import PredicateOperand
from cruxible_core.procedure.pins import (
    AcceptanceNodePin,
    build_acceptance_node_pins,
    expected_pin_keys,
    receipt_pin_material,
    verify_pin_currency,
    verify_pin_integrity,
)
from cruxible_core.procedure.types import (
    MAX_PROCEDURE_ENUMERATED_PATHS,
    MAX_PROCEDURE_EVIDENCE_BYTES,
    PROCEDURE_EVIDENCE_HEAD_BYTES,
    ProcedureAuthoringWarning,
    ProcedureBudgetSpent,
    ProcedureContractFieldSchema,
    ProcedureContractSchema,
    ProcedureDefinition,
    ProcedureEvidenceArtifact,
    ProcedureExecutionResult,
    ProcedureGetResult,
    ProcedureGuardStepSchema,
    ProcedurePathEnumeration,
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
from cruxible_core.service.groups import service_propose_group
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
    _validate_procedure_output_contract,
    execute_procedure_plan,
)
from cruxible_core.workflow.refs import iter_step_reference_templates
from cruxible_core.workflow.types import CompiledPlan, WorkflowLock

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

WARNING_CONTRACT_FIELD_UNCONSUMED = "contract_field_unconsumed"
WARNING_CONTRACT_FIELD_PATH_CONDITIONAL = "contract_field_path_conditional"
WARNING_READ_IMPLYING_NAME_WRITES = "read_implying_name_writes"
WARNING_STRINGIFIED_OBJECT_INPUT = "stringified_object_input"
WARNING_WHOLESALE_PASSTHROUGH = "wholesale_passthrough"
WARNING_READ_WRITE_OMNIBUS = "read_write_omnibus"
WARNING_PROVIDER_STEP_FANOUT = "provider_step_fanout"
WARNING_BUDGET_HEADROOM_UNREACHABLE = "budget_headroom_unreachable"

PROCEDURE_AUTHORING_WARNING_CODES: frozenset[str] = frozenset(
    {
        WARNING_CONTRACT_FIELD_UNCONSUMED,
        WARNING_CONTRACT_FIELD_PATH_CONDITIONAL,
        WARNING_READ_IMPLYING_NAME_WRITES,
        WARNING_STRINGIFIED_OBJECT_INPUT,
        WARNING_WHOLESALE_PASSTHROUGH,
        WARNING_READ_WRITE_OMNIBUS,
        WARNING_PROVIDER_STEP_FANOUT,
        WARNING_BUDGET_HEADROOM_UNREACHABLE,
        GRAPH_FORMAT_DECLARED_WITHOUT_CONSTRUCT,
    }
)
"""Every code this core emits, enumerable so a surface can group by it.

Deliberately a set and never a score. Per ``dd-specificity-doctrine`` the
warning family is a design razor: counting or weighting these would turn a
nudge into a target, and the axes they sit on (§3.6) are independent, so no
aggregate over them means anything.
"""

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
) -> tuple[CompiledPlan, list[ProcedureAuthoringWarning]]:
    """Compile a procedure and return its non-blocking authoring warnings."""
    config = instance.load_config()
    validate_procedure_definition_against_config(definition, config)
    warnings = lint_procedure_definition_authoring_typed(definition, config)
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
    """DEPRECATED string channel, removed in 0.5.0. Use the typed lint.

    DERIVED, never built in parallel: two independently assembled lists would
    drift the moment one call site gained a warning the other did not, and the
    dual-emit window exists precisely so callers can migrate without watching
    for that.
    """
    return [
        warning.message for warning in lint_procedure_definition_authoring_typed(definition, config)
    ]


def lint_procedure_definition_authoring_typed(
    definition: ProcedureDefinition,
    config: CoreConfig,
) -> list[ProcedureAuthoringWarning]:
    """Block impossible input refs and return deterministic authoring warnings."""
    contract = resolve_contract(config, definition.contract_in)
    if contract is None:
        # The compiler owns the existing unknown-contract diagnostic.
        return []

    references = _procedure_step_input_references(definition)
    consumed_fields: set[str] = set()
    consuming_nodes: dict[str, set[str]] = {}
    for node_id, step_id, reference in references:
        if reference == "$input":
            # The whole payload: every declared field is consumed HERE, so this
            # node reads all of them for path purposes too.
            consumed_fields.update(contract.fields)
            for declared in contract.fields:
                consuming_nodes.setdefault(declared, set()).add(node_id)
            continue
        field_name = _input_reference_field(reference)
        if field_name is None:
            continue
        consumed_fields.add(field_name)
        consuming_nodes.setdefault(field_name, set()).add(node_id)
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

    warnings: list[ProcedureAuthoringWarning] = []
    if not contract.allow_extra:
        # Consumed on NO path. Every node is reachable (R3), so a field no node
        # references is a field no execution reads -- the path analysis leaves
        # this verdict exactly where it was.
        warnings.extend(
            ProcedureAuthoringWarning(
                code=WARNING_CONTRACT_FIELD_UNCONSUMED,
                message=(
                    f"contract_in field '{field_name}' is declared but not consumed "
                    "by any procedure step"
                ),
            )
            for field_name in sorted(set(contract.fields) - consumed_fields)
        )
    warnings.extend(_path_conditional_field_warnings(definition, consuming_nodes))

    read_implying_name = definition.name.lower().startswith(_READ_IMPLYING_PROCEDURE_PREFIXES)
    for node_id, step in _procedure_node_steps(definition):
        if step.provider is not None:
            provider = config.providers.get(step.provider)
            if read_implying_name and provider is not None and provider.side_effects:
                warnings.append(
                    ProcedureAuthoringWarning(
                        code=WARNING_READ_IMPLYING_NAME_WRITES,
                        message=(
                            f"procedure name '{definition.name}' implies a read, but step "
                            f"'{step.id}' uses side-effecting provider '{step.provider}'"
                        ),
                        node_ids=[node_id],
                    )
                )
            warnings.extend(_stringified_object_input_warnings(node_id, step.id, step.input))
            warnings.extend(_wholesale_passthrough_warnings(node_id, step.id, step.input, contract))

    warnings.extend(_read_fanout_warnings(definition, config))

    expansion = definition.static_expansion()
    provider_call_count = expansion.expanded_provider_calls
    if definition.budget.max_provider_calls > provider_call_count:
        # Under-provisioning is refused by ``ProcedureDefinition`` itself, so the
        # only mismatch that can reach here is slack above the static maximum --
        # headroom the run can never reach, which quietly disarms the ceiling as
        # a review signal. The count is now the LONGEST PATH's (§3.3), so on a
        # branching definition this reads "no arm can spend it" rather than "the
        # sum of every arm cannot".
        warnings.append(
            ProcedureAuthoringWarning(
                code=WARNING_BUDGET_HEADROOM_UNREACHABLE,
                message=(
                    "budget.max_provider_calls "
                    f"({definition.budget.max_provider_calls}) exceeds the expanded "
                    f"provider-call count ({provider_call_count}); the extra headroom "
                    "is unreachable"
                ),
                node_ids=list(expansion.expanded_provider_calls_path),
            )
        )
    return warnings


def _path_conditional_field_warnings(
    definition: ProcedureDefinition,
    consuming_nodes: dict[str, set[str]],
) -> list[ProcedureAuthoringWarning]:
    """Warn for a contract field consumed on SOME paths but not all (§3.5).

    The verdict between "consumed" and "unconsumed" that a linear grammar had
    no room for. A caller supplying an input only the escalation arm reads gets
    no signal today: the field is consumed, so the unconsumed warning is
    silent, and nothing else says the value may never be looked at.

    A broken control graph produces no verdict rather than an exception. The
    compiler refuses it a moment later with the same message this would raise,
    and an advisory pass is the wrong place to surface a structural refusal.
    """
    if not consuming_nodes:
        return []
    try:
        graph = build_procedure_graph(definition)
    except ConfigError:
        return []
    warnings: list[ProcedureAuthoringWarning] = []
    for field_name, nodes in sorted(consuming_nodes.items()):
        if not has_path_avoiding(graph, nodes):
            continue
        readers = ", ".join(sorted(nodes))
        warnings.append(
            ProcedureAuthoringWarning(
                code=WARNING_CONTRACT_FIELD_PATH_CONDITIONAL,
                message=(
                    f"contract_in field '{field_name}' is consumed only on some control "
                    f"paths (by: {readers}); an execution that takes another path never "
                    "reads it"
                ),
                node_ids=sorted(nodes),
            )
        )
    return warnings


def _procedure_node_steps(
    definition: ProcedureDefinition,
) -> list[tuple[str, WorkflowStepSchema]]:
    """Flatten to ``(owning graph node id, plain step)`` pairs.

    Wrappers unwrap; guards are skipped, because a guard carries no reference
    template fields -- its operands are the predicate grammar's business, not
    the resolver's, and they are collected separately.

    A repeat's nested step is not a graph node: it has no control edges and no
    place on any path of its own. Its findings are attributed to the repeat
    CONTAINER, which is the node paths run through, while the message keeps
    naming the nested step so the author can find it.
    """
    steps: list[tuple[str, WorkflowStepSchema]] = []
    for wrapper in definition.steps:
        step = unwrap_procedure_step(wrapper)
        if isinstance(step, ProcedureRepeatStepSchema):
            steps.extend((str(step.id), nested) for nested in step.repeat.steps)
        elif isinstance(step, WorkflowStepSchema):
            steps.append((str(step.id), step))
    return steps


def _procedure_workflow_steps(definition: ProcedureDefinition) -> list[WorkflowStepSchema]:
    """The plain workflow steps a reference scan walks, node attribution dropped."""
    return [step for _node_id, step in _procedure_node_steps(definition)]


def _procedure_step_input_references(
    definition: ProcedureDefinition,
) -> list[tuple[str, str, str]]:
    """Collect ``$input`` references as ``(node id, step id, reference)``.

    Scope equals resolution scope: ``iter_step_reference_templates`` selects the
    same step fields :func:`resolve_value` visits, and nothing else. Scanning the
    whole dumped step instead would read literal prose -- an assert ``message``
    quoting ``$input.foo`` to explain a failure -- as a reference and block a
    definition that runs correctly.

    GUARD OPERANDS COUNT. A guard reading ``$input.tier`` consumes that field
    as surely as a provider input does, and it is the whole reason the
    path-conditional verdict exists -- the branch that decides whether the
    escalation arm runs is usually the only thing that reads the escalation
    input. Before graph nodes existed there was no such position, so the scan
    had none to look at; leaving it that way would report a consumed field as
    unconsumed and let an undeclared one past R10.
    """
    references: list[tuple[str, str, str]] = []
    for node_id, step in _procedure_node_steps(definition):
        dumped = step.model_dump(mode="python", by_alias=True, exclude_none=True)
        for template in iter_step_reference_templates(dumped):
            references.extend((node_id, step.id, ref) for ref in _input_references(template))
    for wrapper in definition.steps:
        if not isinstance(wrapper, ProcedureGuardStepSchema):
            continue
        node_id = str(wrapper.id)
        for operand in wrapper.guard.operands():
            reference = _guard_operand_input_reference(operand)
            if reference is not None:
                references.append((node_id, node_id, reference))
    return references


def _guard_operand_input_reference(operand: PredicateOperand) -> str | None:
    """Return the ``$input`` reference one parsed guard operand reads, if any.

    ``exists($input.x)`` reads it too: the accessor's argument is a reference
    parsed by the same grammar, so an existence test over an undeclared field
    is exactly as impossible as a comparison against one.
    """
    if operand.form == "input_path":
        return "$input" if operand.path is None else f"$input.{operand.path}"
    if operand.form == "exists" and operand.ref is not None and operand.ref.startswith("$input"):
        return operand.ref
    return None


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


def _stringified_object_input_warnings(
    node_id: str,
    step_id: str,
    value: Any,
) -> list[ProcedureAuthoringWarning]:
    warnings: list[ProcedureAuthoringWarning] = []

    def visit(item: Any, path: str) -> None:
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except (json.JSONDecodeError, TypeError):
                return
            if isinstance(parsed, dict):
                warnings.append(
                    ProcedureAuthoringWarning(
                        code=WARNING_STRINGIFIED_OBJECT_INPUT,
                        message=(
                            f"step '{step_id}' input value at '{path}' is a stringified "
                            "JSON object; pass the object directly"
                        ),
                        node_ids=[node_id],
                    )
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
    node_id: str,
    step_id: str,
    value: Any,
    contract: ContractSchema,
) -> list[ProcedureAuthoringWarning]:
    """Flag a declared string field handed whole to an ``arguments`` parameter.

    Feeding one contract field entire into a parameter named ``arguments`` --
    the ``call_discoverable_agent_tool``-style string argument bundle -- routes
    around the contract: whatever the caller packed into that one string is
    never type-checked, and the declared shape stops describing what the tool
    actually receives. The fix is to declare the individual fields the tool
    needs, so the reference is a warning rather than a refusal.
    """
    warnings: list[ProcedureAuthoringWarning] = []

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
                ProcedureAuthoringWarning(
                    code=WARNING_WHOLESALE_PASSTHROUGH,
                    message=(
                        f"step '{step_id}' input at '{path}' passes the whole contract_in "
                        f"field '{field_name}' into an "
                        f"'{_WHOLESALE_PASSTHROUGH_PARAMETER}' parameter; the contract "
                        "cannot validate what that string carries -- declare the "
                        "individual fields the provider needs"
                    ),
                    node_ids=[node_id],
                )
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
) -> list[ProcedureAuthoringWarning]:
    """Prefer small, single-purpose procedures over read-plus-write omnibuses.

    A definition that reads widely and then writes is two procedures wearing one
    name: the reads are re-runnable and cheap to review, the write is neither,
    and bundling them means every review of the read half re-reviews the write.
    Guidance only -- a legitimately wide procedure is still proposable.
    """
    read_steps: list[str] = []
    side_effecting_steps: list[str] = []
    provider_steps: list[str] = []
    read_nodes: list[str] = []
    side_effecting_nodes: list[str] = []
    provider_nodes: list[str] = []
    for node_id, step in _procedure_node_steps(definition):
        if step.query is not None:
            read_steps.append(step.id)
            read_nodes.append(node_id)
            continue
        if step.provider is None:
            continue
        provider_steps.append(step.id)
        provider_nodes.append(node_id)
        provider = config.providers.get(step.provider)
        if provider is not None and provider.side_effects:
            side_effecting_steps.append(step.id)
            side_effecting_nodes.append(node_id)
        else:
            read_steps.append(step.id)
            read_nodes.append(node_id)

    warnings: list[ProcedureAuthoringWarning] = []
    if side_effecting_steps and len(read_steps) > 1:
        warnings.append(
            ProcedureAuthoringWarning(
                code=WARNING_READ_WRITE_OMNIBUS,
                message=(
                    f"procedure mixes {len(read_steps)} read steps "
                    f"({', '.join(read_steps)}) with {len(side_effecting_steps)} "
                    f"side-effecting step(s) ({', '.join(side_effecting_steps)}); "
                    "consider splitting reads into a read-only bundle"
                ),
                node_ids=sorted(set(read_nodes) | set(side_effecting_nodes)),
            )
        )
    if len(provider_steps) > _PREFERRED_PROVIDER_STEP_COUNT:
        warnings.append(
            ProcedureAuthoringWarning(
                code=WARNING_PROVIDER_STEP_FANOUT,
                message=(
                    f"procedure declares {len(provider_steps)} provider steps, above the "
                    f"{_PREFERRED_PROVIDER_STEP_COUNT}-step guidance for one procedure; "
                    "consider splitting reads into a read-only bundle"
                ),
                node_ids=sorted(set(provider_nodes)),
            )
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
        typed_warnings = [
            ProcedureAuthoringWarning(
                code=GRAPH_FORMAT_DECLARED_WITHOUT_CONSTRUCT,
                message=message,
            )
            for message in format_warnings
        ] + lint_warnings
        warnings = [warning.message for warning in typed_warnings]

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
                # The receipt records the CODES, not a second copy of the
                # prose: a reviewer reading the ledger can then count how often
                # a finding class fires without re-parsing English, and the
                # messages already ride in the line above.
                "warning_codes": [warning.code for warning in typed_warnings],
            },
        )
        result = ProcedureTransitionResult(
            action="propose",
            procedure=procedure,
            warnings=warnings,
            typed_warnings=typed_warnings,
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
    """Read one procedure record, backfilling its node digests if absent."""
    store = instance.get_procedure_store()
    try:
        procedure = _get_procedure(store, procedure_id)
        track_records = store.get_run_track_records([procedure_id])
        needs_backfill = procedure.status == "live" and not store.list_node_digests(procedure_id)
    finally:
        store.close()
    if needs_backfill:
        _backfill_node_digests(instance, procedure)
    return _procedure_read_record(procedure, track_records.get(procedure_id))


def _backfill_node_digests(instance: InstanceProtocol, procedure: ProcedureRecord) -> None:
    """Lazily populate node digests for a procedure that predates the table.

    A procedure accepted BEFORE migration 0009 has no digest rows and never
    passes through an acceptance or a restore again, so nothing else would ever
    write them and it would stay digest-less forever -- unjoinable to any
    reading about its decision points.

    Lazy rather than a migration sweep, per the spec's letter, and the reasons
    are structural: the migration runs under a write lock that forbids the kind
    of work parsing every stored definition requires, and a torn sweep would
    leave a stamped database half-populated with no record of which half. This
    fires once per procedure and only for LIVE ones, so a read pays the cost at
    most once and the write lock is taken only when there is something to write.

    The computation is pure and the write is idempotent, so a failure here
    costs a later retry and nothing else -- which is why it degrades to a
    logged warning rather than failing a read verb over derived data.
    """
    try:
        digests = list(compute_node_digests(procedure.definition).values())
        with instance.write_transaction() as uow:
            uow.procedures.save_node_digests(procedure.procedure_id, digests)
    except Exception:  # noqa: BLE001 - derived data must not break a read
        _logger.warning(
            "could not backfill node digests for procedure %s; "
            "the rows stay absent and the next read retries",
            procedure.procedure_id,
            exc_info=True,
        )


def _procedure_control_paths(definition: ProcedureDefinition) -> ProcedurePathEnumeration | None:
    """Enumerate the definition's control paths for the review surface (§3.1).

    Degrades to ``None`` rather than raising. A stored definition whose graph
    does not resolve still has to be readable -- refusing the whole read is how
    a reviewer loses the one view that would show them what is wrong with it.
    """
    try:
        graph = build_procedure_graph(definition)
        paths, truncated = enumerate_control_paths(graph)
    except ConfigError:
        return None
    return ProcedurePathEnumeration(
        paths=[list(path) for path in paths],
        truncated=truncated,
        cap=MAX_PROCEDURE_ENUMERATED_PATHS,
    )


def service_get_procedure_details(
    instance: InstanceProtocol,
    procedure_id: str,
) -> ProcedureGetResult:
    """Read a procedure with the active config's resolved input field schema."""
    procedure = service_get_procedure(instance, procedure_id)
    config = instance.load_config()
    control_paths = _procedure_control_paths(procedure.definition)
    contract = resolve_contract(config, procedure.definition.contract_in)
    if contract is None:
        return ProcedureGetResult(
            procedure=procedure,
            contract_in_schema=None,
            control_paths=control_paths,
        )
    return ProcedureGetResult(
        procedure=procedure,
        control_paths=control_paths,
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
    *,
    dry_run: bool = False,
) -> ProcedureExecutionResult:
    """Run live, or dry-run a pending/live procedure without landing writes."""
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
            "dry_run": dry_run,
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
    stored_node_pins: list[AcceptanceNodePin] = []

    try:
        allowed_status = procedure.status == "live" or (dry_run and procedure.status == "pending")
        if not allowed_status:
            preflight_reason = "procedure_not_live"
            raise ConfigError(
                f"Procedure '{procedure.procedure_id}' must be live to run"
                f"{' or pending to dry-run' if dry_run else ''}; "
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
        if dry_run:
            unsafe_providers = sorted(
                name
                for name in procedure.definition.referenced_providers()
                if not config.providers[name].deterministic or config.providers[name].side_effects
            )
            if unsafe_providers:
                raise ConfigError(
                    "Procedure dry-run requires deterministic, side-effect-free providers; "
                    f"refused {unsafe_providers}"
                )
        # Ordered after the definition-vs-config checks on purpose: when the
        # config drift is one those checks already name precisely (a provider
        # de-exported, removed, or tier-raised), the operator is better served by
        # that specific refusal than by the generic pin mismatch.
        if procedure.status == "live":
            stored_node_pins = _load_acceptance_node_pins(instance, procedure)
            _verify_acceptance_pins(
                procedure,
                executed_config_digest=executed_config_digest,
                executed_lock_digest=executed_lock_digest,
                node_pins=stored_node_pins,
                config=config,
                lock=lock,
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
        authorization_status_allowed = authorization_procedure.status == "live" or (
            dry_run and authorization_procedure.status == "pending"
        )
        if not authorization_status_allowed:
            refusal_reason = "procedure_not_live"
            refusal = ConfigError(
                f"Procedure '{authorization_procedure.procedure_id}' must be live to run"
                f"{' or pending to dry-run' if dry_run else ''}; "
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
            procedure_id=procedure.procedure_id,
            procedure_definition_digest=procedure.definition_digest,
            procedure_run_id=started_run.run_id,
            procedure_dry_run=dry_run,
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
            node_pins=stored_node_pins,
        )
        _tag_procedure_exception(failure, finalized_run, receipt)
        if failure is original_exc:
            raise
        raise failure from original_exc

    try:
        with instance.write_transaction() as uow:
            if not dry_run:
                _land_procedure_group_proposals(
                    instance,
                    procedure=procedure,
                    execution=execution,
                    actor_context=actor_context,
                )
            else:
                execution.receipt.nodes[0].detail["dry_run"] = True
            execution.output = _validate_procedure_output_contract(
                config,
                procedure.definition.name,
                procedure.definition,
                execution.output,
            )
            execution.receipt.results = [{"output": execution.output}]
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
                node_pins=stored_node_pins,
            )
            # Evidence rows commit atomically with the run finalize: a crash here
            # rolls back both, leaving the run 'started' (crash-visible) instead of
            # a succeeded run with silently absent declared evidence. A
            # deterministic persistence failure must not fail a run that already
            # succeeded, so it degrades to no auto-refs with a logged warning.
            if dry_run:
                evidence_refs = []
            else:
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
    except Exception as exc:
        failed_receipt = execution.receipt.model_copy(deep=True)
        failed_receipt.committed = False
        failed_receipt.results = [{"output": None, "error": str(exc)}]
        failed_receipt.nodes[0].detail["error"] = str(exc)
        failed_receipt.nodes[0].detail["procedure_group_proposal_landed"] = False
        receipt, finalized_run = _persist_built_procedure_receipt(
            instance,
            procedure=procedure,
            started_run=started_run,
            receipt=failed_receipt,
            verdict="failed",
            budget=budget,
            precondition_detail=precondition_detail,
            acceptance_config_digest=procedure.acceptance_config_digest,
            acceptance_lock_digest=procedure.acceptance_lock_digest,
            executed_config_digest=plan.config_digest,
            executed_lock_digest=plan.lock_digest,
            error=exc,
            node_pins=stored_node_pins,
        )
        _tag_procedure_exception(exc, finalized_run, receipt)
        raise
    return ProcedureExecutionResult(
        procedure=procedure,
        run=finalized_run,
        output=execution.output,
        receipt=receipt,
        step_outputs=execution.step_outputs,
        evidence_refs=evidence_refs,
        dry_run=dry_run,
    )


def _land_procedure_group_proposals(
    instance: InstanceProtocol,
    *,
    procedure: ProcedureRecord,
    execution: Any,
    actor_context: GovernedActorContext | None,
) -> None:
    """Land staged bridge intents inside the procedure success transaction."""
    for intent in execution.procedure_group_proposals:
        result = service_propose_group(
            instance,
            intent["relationship_type"],
            intent["members"],
            thesis_text=intent["thesis_text"],
            thesis_facts=intent["thesis_facts"],
            pending_refresh_mode=intent["pending_refresh_mode"],
            analysis_state=intent["analysis_state"],
            signal_sources_used=intent["signal_sources_used"],
            suggested_priority=intent["suggested_priority"],
            source_workflow_name=f"procedure:{procedure.definition.name}",
            source_workflow_receipt_id=execution.receipt.receipt_id,
            source_query_receipt_ids=list(execution.query_receipt_ids),
            source_trace_ids=[trace.trace_id for trace in execution.traces],
            source_step_ids=[intent["step_id"]],
            actor_context=actor_context,
            force_review=True,
        )
        if result.status not in {"pending_review", "suppressed"}:
            raise ConfigError(
                "Procedure group bridge may only produce pending or suppressed groups; "
                f"found '{result.status}'"
            )
        output = execution.step_outputs[intent["output_key"]]
        output.update(
            {
                "group_id": result.group_id,
                "group_receipt_id": result.receipt_id,
                "group_status": result.status,
                "review_priority": result.review_priority,
                "member_count": result.member_count,
                "suppressed": result.suppressed,
            }
        )
        for node in execution.receipt.nodes:
            if node.node_type != "plan_step" or node.detail.get("step_id") != intent["step_id"]:
                continue
            node.detail.update(
                {
                    "group_id": result.group_id,
                    "group_receipt_id": result.receipt_id,
                    "group_status": result.status,
                    "review_priority": result.review_priority,
                    "member_count": result.member_count,
                    "suppressed": result.suppressed,
                }
            )
        execution.receipt.results = [{"output": execution.output}]
        execution.receipt.nodes[0].detail["procedure_group_id"] = result.group_id
        execution.receipt.nodes[0].detail["procedure_group_proposal_landed"] = True


def _load_acceptance_node_pins(
    instance: InstanceProtocol,
    procedure: ProcedureRecord,
) -> list[AcceptanceNodePin]:
    store = instance.get_procedure_store()
    try:
        pins: list[AcceptanceNodePin] = store.list_acceptance_node_pins(procedure.procedure_id)
        return pins
    finally:
        store.close()


def _verify_acceptance_pins(
    procedure: ProcedureRecord,
    *,
    executed_config_digest: str,
    executed_lock_digest: str,
    node_pins: list[AcceptanceNodePin],
    config: CoreConfig,
    lock: WorkflowLock,
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
    # COMPLETENESS is a set question. A count, or a non-emptiness test, misses
    # both halves of it: a stored set missing one row of two is incomplete
    # while still being non-empty and still matching its coarse digests, and a
    # v2 graph of guards and projections alone has no external dependencies at
    # all, so the EMPTY set is the correct and complete answer for it.
    #
    # Read the format from the DEFINITION, which is authoritative: the record
    # column is a convenience for readers, and a row restored from an older
    # snapshot can carry the default while its definition says otherwise.
    stored_format = definition_format_version(procedure.definition)[0]
    stored_keys: set[tuple[str, str, str]] = {
        (pin.node_id, str(pin.pin_kind), pin.pin_key) for pin in node_pins
    }
    check_completeness = stored_format != DEFINITION_FORMAT_V1 or bool(stored_keys)
    if check_completeness:
        expected_keys = expected_pin_keys(definition=procedure.definition, config=config, lock=lock)
        missing_pins = sorted(expected_keys - stored_keys)
        unexpected_pins = sorted(stored_keys - expected_keys)
        if missing_pins or unexpected_pins:
            parts: list[str] = []
            if missing_pins:
                parts.append(f"missing {missing_pins}")
            if unexpected_pins:
                parts.append(f"unrecorded {unexpected_pins}")
            raise ConfigError(
                f"Procedure '{procedure.procedure_id}' has an incomplete set of "
                f"per-node acceptance pins ({'; '.join(parts)}). A pin set that does "
                "not cover every dependency the definition declares cannot show the "
                "run was reviewed against the world it is about to execute in, so "
                "the run is refused. Recover by re-proposing the definition for an "
                "independent reviewer to accept."
            )
    verify_pin_integrity(node_pins)

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
    # The coarse digests moved. Name WHAT moved before reporting that something
    # did: a per-node currency mismatch says "provider X's entrypoint changed at
    # node Y", which the whole-config digest never could.
    verify_pin_currency(
        node_pins,
        definition=procedure.definition,
        config=config,
        lock=lock,
    )
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
    node_pins: Sequence[AcceptanceNodePin] = (),
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
            node_pins=node_pins,
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
    node_pins: Sequence[AcceptanceNodePin] = (),
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
    if node_pins:
        # In the ROOT NODE's detail, never as a new top-level Receipt field. A
        # top-level field is silently DROPPED by a 0.3 reader -- the worst of
        # the three forward-compatibility behaviours, because the receipt would
        # look complete and be incomplete. `detail` is arbitrary by contract, so
        # this joins `accepted_against`/`executed_against` where they already
        # live. Payloads are deduplicated by digest, so a run id recovers the
        # exact accepted world without consulting a config that may have drifted.
        pin_map, pin_payloads = receipt_pin_material(list(node_pins))
        root_detail["node_pins"] = pin_map
        root_detail["pin_payloads"] = pin_payloads
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
        node_pins: list[AcceptanceNodePin] = []
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
            node_pins = build_acceptance_node_pins(
                procedure_id=procedure_id,
                definition=procedure.definition,
                config=instance.load_config(),
                lock=load_lock(resolve_lock_path(instance)),
            )
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

        if action == "accept":
            # Written in the SAME transaction as the status change, beside the
            # coarse acceptance digests: a live procedure with no pins would be
            # a procedure nobody can prove was reviewed against anything.
            ctx.uow.procedures.save_acceptance_node_pins(node_pins)
            # Derived data, written where the definition is first known to be
            # live. It is backfillable -- the computation is pure -- so this is
            # a cache warm, not a commitment.
            ctx.uow.procedures.save_node_digests(
                procedure_id,
                list(compute_node_digests(procedure.definition).values()),
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
        if action == "accept":
            detail["acceptance_node_pins"] = len(node_pins)
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
