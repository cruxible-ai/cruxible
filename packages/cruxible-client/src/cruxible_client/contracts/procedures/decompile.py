"""Render a stored Procedure graph in the one typed authoring form.

Every graph node has a typed step, so the rendering is total: ``get`` shows a
Procedure exactly as an author writes one, and lowering the rendered spec
reproduces the stored graph.
"""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.procedures.artifacts import ProcedureArtifact
from cruxible_client.contracts.procedures.authored import ProcedureSpec, _template_aliases
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.graph import (
    analyze_procedure,
    procedure_guard_halt_targets,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    CaptureEgressNode,
    ExtractNode,
    GuardNode,
    HaltNode,
    InboxEgressNode,
    InvokeNode,
    ProcedureDefinition,
    ProjectNode,
    ProposalItemsFanOut,
    ProposeChangeSetNode,
    ReadNode,
    RepeatNode,
    SelectNode,
    SettleChangeSetNode,
    TransformNode,
)
from cruxible_client.contracts.procedures.reads import DEFAULT_MAX_READ_ROWS
from cruxible_client.contracts.refs import ref_from_graph_text
from cruxible_client.contracts.temporal import format_iso_duration_microseconds


class ProcedureNotDecompilable(ValueError):
    """A stored graph that no typed spec lowers to (it was not authored as a spec)."""


def _name(pin: Any) -> str:
    if not isinstance(pin, (ArtifactNameRef, ArtifactPin)):
        raise ProcedureNotDecompilable("a Procedure dependency is not a named reference")
    return pin.target.name


def typed_template(value: Any) -> Any:
    """A stored graph template with each reference string as a typed ``Ref``."""

    if isinstance(value, str):
        ref = ref_from_graph_text(value)
        return value if ref is None else ref.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {key: typed_template(member) for key, member in value.items()}
    if isinstance(value, list | tuple):
        return [typed_template(member) for member in value]
    return value


def _transform_spec(kind: str, spec: Any) -> dict[str, Any]:
    data: dict[str, Any] = spec.model_dump(mode="json", by_alias=True)
    data.pop("tag", None)
    if kind in {"shape", "filter", "aggregate", "dedupe", "derive"}:
        data["input"] = typed_template(data["input"])
    if kind == "join":
        data["left"], data["right"] = typed_template(data["left"]), typed_template(data["right"])
    if kind == "union":
        data["inputs"] = typed_template(data["inputs"])
    if kind == "shape":
        data["fields"] = {
            name: {
                **{key: member for key, member in field.items() if key != "from"},
                **({"from": typed_template(field["from"])} if "from" in field else {}),
            }
            for name, field in data["fields"].items()
        }
    return data


def _incoming(definition: ProcedureDefinition) -> dict[str, list[str]]:
    incoming: dict[str, list[str]] = {}
    for node_id, edges in analyze_procedure(definition).edges.items():
        for target in edges.values():
            incoming.setdefault(target, []).append(node_id)
    return incoming


def _folded_halts(
    definition: ProcedureDefinition, incoming: dict[str, list[str]]
) -> dict[tuple[str, str], HaltNode]:
    """The halts a guard's typed ``halt`` action regenerates exactly, by (guard, arm).

    Lowering appends each guard's generated halts, in step order, after every
    step; a stored graph whose trailing halts are exactly those (each reached
    only from its one arm) renders them as actions. Any other halt stays a step.
    """

    nodes = definition.nodes
    by_id = {node.node_id: node for node in nodes}
    candidates: list[tuple[str, str, HaltNode]] = []
    for node in nodes:
        if not isinstance(node, GuardNode):
            continue
        for arm, target in (("on_true", node.on_true), ("on_false", node.on_false)):
            halt = by_id.get(target or "")
            if isinstance(halt, HaltNode) and incoming.get(halt.node_id) == [node.node_id]:
                candidates.append((node.node_id, arm, halt))
    if not candidates:
        return {}
    tail = [node.node_id for node in nodes[len(nodes) - len(candidates) :]]
    if tail != [halt.node_id for _guard, _arm, halt in candidates]:
        return {}
    return {(guard, arm): halt for guard, arm, halt in candidates}


def decompile_procedure(procedure: ProcedureArtifact) -> ProcedureSpec:
    """The typed spec a stored Procedure graph lowers from."""

    definition = procedure.definition
    contracts = {c.identity.name: c.contract_schema for c in procedure.owned_contracts}

    def schema(pin: Any) -> ContractSchema:
        return contracts[_name(pin)]

    nodes = definition.nodes
    incoming = _incoming(definition)
    folded = _folded_halts(definition, incoming)
    folded_ids = {halt.node_id for halt in folded.values()}
    returns: Any = definition.returns
    folded_project: str | None = None
    for index, node in enumerate(nodes):
        if (
            len(nodes) > 1
            and isinstance(node, ProjectNode)
            and not node.leaf
            and node.node_id == node.as_ == returns
            and isinstance(node.fields, dict)
            and node.contract_out == definition.contract_out
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
                returns = {"fields": typed_template(node.fields), "id": node.as_}
                break

    def action(guard: GuardNode, arm: str) -> dict[str, Any] | None:
        target = getattr(guard, arm)
        if target is None:
            return None
        if target == "$abort":
            return {"abort": {"code": guard.refusal_code, "message": guard.message}}
        halt = folded.get((guard.node_id, arm))
        if halt is None:
            return {"goto": target}
        rendered: dict[str, Any] = {"halt": halt.reason}
        if halt.message is not None:
            rendered["message"] = halt.message
        if halt.node_id != guard.node_id[:123] + "_halt":
            rendered["id"] = halt.node_id
        return rendered

    guard_halts = procedure_guard_halt_targets(definition)
    steps: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if node.node_id in folded_ids or node.node_id == folded_project:
            continue
        common: dict[str, Any] = {"id": node.node_id}
        following = nodes[index + 1].node_id if index + 1 < len(nodes) else None
        target = getattr(node, "next", None)
        # Fallthrough never enters a guard's halt, so an edge into one is explicit.
        if target is not None and (target != following or target in guard_halts):
            common["next"] = target
        step: dict[str, Any]
        if isinstance(node, CallNode):
            step = dict(call=node.as_, slot=node.slot, input=typed_template(node.input))
            if node.captures:
                step["captures"] = {
                    field: _name(capture) for field, capture in node.captures.items()
                }
            if node.effect_policy is not None:
                step["effect_policy"] = _name(node.effect_policy)
        elif isinstance(node, ExtractNode):
            step = dict(
                extract=node.as_,
                capture=typed_template(node.capture),
                records=node.records,
                row=node.row.spelling,
                fields={k: v.model_dump(mode="json") for k, v in node.fields.items()},
                key={k: v.model_dump(mode="json") for k, v in node.key.items()},
                header={k: v.model_dump(mode="json") for k, v in node.header.items()},
                complete_when=None
                if node.complete_when is None
                else node.complete_when.model_dump(mode="json"),
                duplicates=node.duplicates,
                only_keys=typed_template(node.only_keys),
            )
        elif isinstance(node, ReadNode) and node.claim_type is not None:
            step = dict(
                read=node.as_,
                claim_type=_name(node.claim_type),
                subject=typed_template(node.subject_id),
                subject_kind=node.subject_kind,
                all=node.limit,
            )
        elif isinstance(node, ReadNode):
            step = dict(
                read=node.as_, query=_name(node.query), params=typed_template(node.parameters)
            )
        elif isinstance(node, InvokeNode):
            step = dict(
                invoke=_name(node.procedure), input=typed_template(node.input), **{"as": node.as_}
            )
        elif isinstance(node, SelectNode):
            step = dict(
                select=node.as_,
                sources=node.sources,
                output=schema(node.contract_out).fields,
                contract=_name(node.contract_out),
            )
        elif isinstance(node, RepeatNode):
            body: list[dict[str, Any]] = []
            for member in node.body:
                item: dict[str, Any]
                if member.operation == "call":
                    item = dict(
                        call=member.as_, slot=member.slot, input=typed_template(member.spec)
                    )
                    if member.effect_policy is not None:
                        item["effect_policy"] = _name(member.effect_policy)
                else:
                    item = dict(
                        transform=member.as_,
                        kind=member.transform_kind,
                        spec=_transform_spec(str(member.transform_kind), member.spec),
                    )
                if member.node_id != member.as_:
                    item["id"] = member.node_id
                body.append(item)
            step = dict(
                repeat=node.as_,
                max_attempts=node.max_attempts,
                body=body,
                until=node.until.model_dump(mode="json", by_alias=True),
            )
        elif isinstance(node, ProjectNode):
            step = dict(
                project=node.as_,
                output=schema(node.contract_out).fields,
                contract=_name(node.contract_out),
            )
            if node.literal is not None:
                step["literal"] = node.literal
            else:
                step["fields"] = typed_template(node.fields)
            if node.leaf:
                step["leaf"] = True
        elif isinstance(node, TransformNode):
            step = dict(
                transform=node.as_,
                kind=node.transform_kind,
                spec=_transform_spec(node.transform_kind, node.spec),
            )
        elif isinstance(node, GuardNode):
            step = dict(guard=node.predicate.model_dump(mode="json", by_alias=True))
            then = action(node, "on_true")
            if then is not None:
                step["then"] = then
            step["else"] = action(node, "on_false")
        elif isinstance(node, HaltNode):
            step = dict(halt=node.reason, message=node.message)
        elif isinstance(node, ProposeChangeSetNode):
            templates = node.candidate_templates
            candidates = (
                typed_template(templates.items)
                if isinstance(templates, ProposalItemsFanOut)
                else typed_template(list(templates))
            )
            verb = "settle" if isinstance(node, SettleChangeSetNode) else "propose"
            step = {verb: candidates, "result": typed_template(node.result)}
        elif isinstance(node, InboxEgressNode):
            step = dict(
                inbox=node.contract.spelling,
                fields=typed_template(node.fields),
                for_each=typed_template(node.for_each),
                dedupe_key=node.dedupe_key,
                max_items=node.max_items,
            )
        elif isinstance(node, CaptureEgressNode):
            step = dict(
                emit=_name(node.capture_contract),
                input=typed_template(node.input),
                result=typed_template(node.result),
            )
        else:  # pragma: no cover - every node kind is matched above
            raise ProcedureNotDecompilable(f"no typed step renders node kind {node.kind!r}")
        verb_alias = next(
            (
                step[key]
                for key in ("read", "call", "extract", "project", "transform", "select", "repeat")
                if key in step
            ),
            step.get("as"),
        )
        if verb_alias == node.node_id:
            common.pop("id")  # the default: a value step's node is named by its output
        steps.append({**common, **step})
    caps = definition.hard_caps
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
            measurements=definition.measurements,
            annotations=definition.annotations,
            activation=procedure.activation_policy,
            contracts=contracts,
            steps=steps,
            hard_caps=dict(
                wall_clock=format_iso_duration_microseconds(caps.max_wall_clock.microseconds),
                provider_calls=caps.max_provider_calls,
                capture_bytes=caps.max_capture_bytes,
                result_bytes=caps.max_result_bytes,
                items=caps.max_items,
                repeat_attempts=caps.max_repeat_attempts,
                read_rows=(
                    None if caps.max_read_rows == DEFAULT_MAX_READ_ROWS else caps.max_read_rows
                ),
            ),
        )
    )


__all__ = ["ProcedureNotDecompilable", "decompile_procedure", "typed_template"]
