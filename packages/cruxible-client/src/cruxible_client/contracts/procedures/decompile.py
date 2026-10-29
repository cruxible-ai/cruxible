"""Render a retained Procedure in the authored grammar without losing topology."""

from __future__ import annotations

import re
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.procedures.artifacts import ProcedureArtifact
from cruxible_client.contracts.procedures.authored import ProcedureSpec, _template_aliases
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.expressions import (
    parse_procedure_expression,
    render_procedure_expression,
)
from cruxible_client.contracts.procedures.graph import (
    analyze_procedure,
    procedure_guard_halt_targets,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    CaptureEgressNode,
    ClaimTapNode,
    GuardNode,
    GuardPredicate,
    HaltNode,
    InboxEgressNode,
    InvokeNode,
    ProjectNode,
    ProposeChangeSetNode,
    RepeatNode,
    SelectNode,
    SettleChangeSetNode,
    SourceNode,
    StateTapNode,
    TransformNode,
)
from cruxible_client.contracts.temporal import format_iso_duration_microseconds


def _name(pin: Any) -> str:
    if not isinstance(pin, (ArtifactNameRef, ArtifactPin)):
        raise ValueError("open binding needs an explicit step")
    return pin.target.name


def _short(value: Any, tap_aliases: frozenset[str]) -> Any:
    if isinstance(value, str) and value.startswith("$steps."):
        alias = value[len("$steps.") :].partition(".")[0]
        if alias in tap_aliases and value == "$steps." + alias:
            # A whole runtime tap value is not the generated {rows,truncated}
            # view. Keep its explicit reference so resubmission preserves it.
            return value
        if alias in {"input", "item", "params", "steps"}:
            return value
        return "$" + value[len("$steps.") :]
    if isinstance(value, dict):
        rows = value.get("rows")
        if set(value) == {"rows", "truncated"} and isinstance(rows, str):
            parts = rows.split(".")
            if (
                len(parts) == 4
                and parts[0] == "$steps"
                and parts[2:] == ["result", "rows"]
                and parts[1] in tap_aliases
                and parts[1] not in {"input", "item", "params", "steps"}
                and value["truncated"] == f"$steps.{parts[1]}.truncated"
            ):
                return "$" + parts[1]
        return {k: _short(v, tap_aliases) for k, v in value.items()}
    if isinstance(value, list):
        return [_short(v, tap_aliases) for v in value]
    return value


def _expression(predicate: GuardPredicate) -> str | None:
    """Use EXPR only when its parser reproduces the exact retained predicate."""
    try:
        text = render_procedure_expression(predicate)
        return text if parse_procedure_expression(text) == predicate else None
    except (ValueError, RecursionError):
        return None


def decompile_procedure(procedure: ProcedureArtifact) -> ProcedureSpec:
    """Recognized nodes use verbs; all other nodes keep their explicit typed form.

    A stored graph is the authority. Pattern folding is used only where the
    resulting lowering can reproduce its output and branching exactly.
    """
    definition = procedure.definition
    contracts = {c.identity.name: c.contract_schema for c in procedure.owned_contracts}

    def schema(pin: Any) -> ContractSchema:
        return contracts[_name(pin)]

    import json

    nodes = definition.nodes
    expressions = {
        node.node_id: _expression(node.predicate if isinstance(node, GuardNode) else node.until)
        for node in nodes
        if isinstance(node, (GuardNode, RepeatNode))
    }
    tap_aliases = frozenset(node.as_ for node in nodes if isinstance(node, StateTapNode))

    def short(value: Any) -> Any:
        return _short(value, tap_aliases)

    by_id = {node.node_id: node for node in nodes}
    incoming: dict[str, list[str]] = {}
    for node_id, edges in analyze_procedure(definition).edges.items():
        for target in edges.values():
            incoming.setdefault(target, []).append(node_id)
    folded_halts = {
        node.on_false: node.node_id
        for node in nodes
        if isinstance(node, GuardNode)
        and expressions[node.node_id] is not None
        and not (set(node.predicate.step_aliases()) & {"input", "param"})
        and isinstance(by_id.get(node.on_false), HaltNode)
        and re.fullmatch(r"[a-z][a-z0-9_.-]*", getattr(by_id[node.on_false], "reason", None) or "")
        is not None
        and incoming.get(node.on_false) == [node.node_id]
    }
    returns: Any = definition.returns
    folded_project: str | None = None
    for index, node in enumerate(nodes):
        if (
            len(nodes) > 1
            and isinstance(node, ProjectNode)
            and node.node_id == node.as_ == returns
            and isinstance(node.fields, dict)
        ):
            dependencies = _template_aliases(node.fields)
            position = max(
                (
                    i + 1
                    for i, previous in enumerate(nodes)
                    if getattr(previous, "as_", None) in dependencies
                ),
                default=0,
            )
            if position == index and all(
                source == (nodes[index - 1].node_id if index else "")
                for source in incoming.get(node.node_id, [])
            ):
                folded_project = node.node_id
                returns = {"fields": short(node.fields), "id": node.as_}
                break
    guard_halts = procedure_guard_halt_targets(definition)
    steps: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if node.node_id in folded_halts or node.node_id == folded_project:
            continue
        raw = node.model_dump(mode="json", by_alias=True)
        common: dict[str, Any] = {"id": node.node_id}
        if raw.get("next") is not None and (
            index + 1 == len(definition.nodes)
            or raw["next"] != definition.nodes[index + 1].node_id
            or raw["next"] in guard_halts
        ):
            common["next"] = raw["next"]
        try:
            if isinstance(node, SourceNode):
                step = dict(
                    source=node.as_,
                    contract=_name(node.capture_contract),
                    slot=node.slot,
                    request=short(node.request),
                )
            elif isinstance(node, CallNode):
                step = dict(
                    call=node.as_,
                    slot=node.slot,
                    input=short(node.input),
                )
                if node.effect_policy is not None:
                    step["effect_policy"] = _name(node.effect_policy)
            elif isinstance(node, ClaimTapNode):
                step = dict(
                    claim=node.as_,
                    claim_type=_name(node.claim_type),
                    subject=short(node.subject_id),
                    subject_kind=node.subject_kind,
                    all=node.limit,
                )
            elif isinstance(node, InvokeNode):
                step = dict(
                    invoke=_name(node.procedure), input=short(node.input), **{"as": node.as_}
                )
            elif isinstance(node, SelectNode):
                step = dict(
                    select=node.as_,
                    sources=node.sources,
                    output=schema(node.contract_out).fields,
                    contract=_name(node.contract_out),
                )
            elif isinstance(node, RepeatNode):
                if expressions[node.node_id] is None:
                    steps.append({"explicit": node})
                    continue
                step = dict(
                    repeat=node.as_,
                    max_attempts=node.max_attempts,
                    body=node.body,
                    until=expressions[node.node_id],
                )
            elif isinstance(node, StateTapNode):
                step = dict(tap=node.as_, query=_name(node.query), params=short(node.parameters))
                if node.budgets is not None:
                    step["budgets"] = node.budgets.model_dump(mode="json")
            elif isinstance(node, ProjectNode) and isinstance(node.fields, dict):
                step = dict(
                    project=node.as_,
                    fields=short(node.fields),
                    output=schema(node.contract_out).fields,
                    contract=_name(node.contract_out),
                )
            elif isinstance(node, TransformNode):
                step = dict(
                    transform=node.as_,
                    kind=node.transform_kind,
                    spec=short(node.spec.model_dump(mode="json")),
                    input=schema(node.contract_in).fields,
                    output=schema(node.contract_out).fields,
                    contract_in=_name(node.contract_in),
                    contract_out=_name(node.contract_out),
                )
            elif isinstance(node, GuardNode):
                if expressions[node.node_id] is None or (
                    set(node.predicate.step_aliases()) & {"input", "param"}
                ):
                    steps.append({"explicit": node})
                    continue
                # Non-abort diagnostics are inert at runtime, but remain part of
                # historical definition digests. Preserve them explicitly.
                branch = (
                    f"abort({node.refusal_code}, {json.dumps(node.message)})"
                    if node.on_false == "$abort"
                    else node.on_false
                )
                if node.on_false in folded_halts:
                    halt = by_id[node.on_false]
                    assert isinstance(halt, HaltNode)
                    branch = (
                        f"halt({halt.reason}"
                        + (", " + json.dumps(halt.message) if halt.message is not None else "")
                        + ")"
                    )
                step = dict(
                    guard=expressions[node.node_id],
                    then=node.on_true,
                    **{"else": branch},
                    refusal_code=node.refusal_code,
                    message=node.message,
                    else_id=node.on_false if node.on_false in folded_halts else None,
                )
            elif isinstance(node, HaltNode) and node.reason is not None:
                step = dict(halt=node.reason, message=node.message)
            elif isinstance(node, SettleChangeSetNode):
                candidates = raw["candidate_templates"]
                step = dict(
                    settle=short(
                        candidates["items"] if isinstance(candidates, dict) else candidates
                    ),
                    result=short(node.result),
                )
            elif isinstance(node, ProposeChangeSetNode) and not node.claim_types:
                candidates = raw["candidate_templates"]
                step = dict(
                    propose=short(
                        candidates["items"] if isinstance(candidates, dict) else candidates
                    ),
                    result=short(node.result),
                )
            elif isinstance(node, InboxEgressNode):
                step = dict(inbox=short(node.input), max_items=node.max_items)
            elif isinstance(node, CaptureEgressNode):
                step = dict(
                    emit=_name(node.capture_contract),
                    input=short(node.input),
                    result=short(node.result),
                )
            else:
                steps.append({"explicit": node})
                continue
            steps.append({**common, **step})
        except (ValueError, KeyError):
            steps.append({"explicit": node})
    budget, caps = definition.budget, definition.hard_caps
    return ProcedureSpec.model_validate(
        dict(
            procedure=definition.name,
            description=definition.description,
            slots={
                name: slot.model_dump(mode="json", by_alias=True)
                for name, slot in definition.slots.items()
            },
            input=schema(definition.contract_in).fields,
            input_contract=_name(definition.contract_in),
            output=schema(definition.contract_out).fields,
            output_contract=_name(definition.contract_out),
            params={}
            if definition.parameter_contract is None
            else schema(definition.parameter_contract).fields,
            parameter_contract=None
            if definition.parameter_contract is None
            else _name(definition.parameter_contract),
            returns=returns,
            source=definition.source,
            measurements=definition.measurements,
            annotations=definition.annotations,
            halt_order=tuple(node.node_id for node in nodes if node.node_id in folded_halts),
            activation=procedure.activation_policy,
            acquisition_defaults={
                alias: rule.model_dump(mode="json")
                for alias, rule in procedure.acquisition_defaults.items()
            },
            contracts=contracts,
            steps=steps,
            budget=dict(
                wall_clock=format_iso_duration_microseconds(budget.wall_clock.microseconds),
                provider_calls=budget.max_provider_calls,
                capture_bytes=budget.max_capture_bytes,
                result_bytes=budget.max_result_bytes,
                items=budget.max_items,
            ),
            caps=dict(
                wall_clock=format_iso_duration_microseconds(caps.max_wall_clock.microseconds),
                provider_calls=caps.max_provider_calls,
                capture_bytes=caps.max_capture_bytes,
                result_bytes=caps.max_result_bytes,
                items=caps.max_items,
                repeat_attempts=caps.max_repeat_attempts,
            ),
        )
    )
