"""Declared, decision-only Procedure syntax shared by all authoring clients.

Lowering is pure and proportional to this Procedure. Vocabulary is supplied by
keyed lookups at prepare; the parser never evaluates text or resolves state.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from difflib import get_close_matches
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    Tag,
    field_validator,
    model_serializer,
)
from pydantic_core import PydanticUndefined

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.contract_schema import (
    ContractSchema,
    PropertySchema,
    PropertyType,
)
from cruxible_client.contracts.procedures.expressions import (
    ProcedureExpressionError,
    parse_procedure_expression,
)
from cruxible_client.contracts.procedures.measurements import ProcedureMeasurementDeclaration
from cruxible_client.contracts.procedures.models import (
    GuardPredicate,
    PredicateOperand,
    ProcedureNode,
    ProcedureTransformSpec,
    RepeatBodyNode,
    TransformKind,
    derived_terminal_capability,
)
from cruxible_client.contracts.procedures.source_program import ProcedureSourceProgram

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,255}$")]
StepId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")]
NodeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]


class ProcedureField(PropertySchema):
    """Strict authored schema; retained PropertySchema encodings stay frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, from_attributes=True)

    @field_validator("item_fields", mode="before")
    @classmethod
    def _nested_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {name: cls.model_validate(field) for name, field in value.items()}
        return value


FieldType: TypeAlias = PropertyType | ProcedureField
Template: TypeAlias = JsonValue


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, json_schema_mode_override="validation"
    )


class StepOptions(_Model):
    """Explicit topology is available when a graph cannot use fallthrough."""

    id: NodeId | None = None
    next: str | None = None


class SourceStep(StepOptions):
    source: StepId
    contract: str
    interface: str
    provider: str | None = None
    request: Template


class TapStep(StepOptions):
    tap: StepId
    query: str
    params: Template = Field(default_factory=dict)
    budgets: JsonValue = None


class ClaimStep(StepOptions):
    claim: StepId
    claim_type: str
    subject: Template
    subject_kind: str | None = None
    all: Annotated[int, Field(strict=True, ge=1)] | None = None


class CallStep(StepOptions):
    call: StepId
    interface: str
    provider: str | None = None
    input: Template
    effect_policy: str | None = None


class ProjectStep(StepOptions):
    project: StepId
    fields: dict[str, Template]
    output: dict[str, FieldType] | None = None
    contract: str | None = None


class TransformStep(StepOptions):
    transform: StepId
    kind: TransformKind
    spec: ProcedureTransformSpec
    input: dict[str, FieldType]
    output: dict[str, FieldType]
    contract_in: str | None = None
    contract_out: str | None = None


class GuardStep(StepOptions):
    guard: str
    then: str | None = None
    refusal_code: str | None = None
    message: str | None = None
    else_id: NodeId | None = None
    else_: str = Field(alias="else", default='abort(guard_refused, "Procedure guard refused.")')


class InboxStep(StepOptions):
    inbox: Template
    max_items: Annotated[int, Field(strict=True, ge=1)] | None = None


class ProposeStep(StepOptions):
    propose: str | tuple[Template, ...]
    result: Template = None


class SettleStep(StepOptions):
    settle: str | tuple[Template, ...]
    result: Template = None


class EmitStep(StepOptions):
    emit: str
    input: Template
    result: Template


class InvokeStep(StepOptions):
    invoke: str
    input: Template
    as_: StepId | None = Field(alias="as", default=None)


class SelectStep(StepOptions):
    select: StepId
    sources: tuple[StepId, ...]
    output: dict[str, FieldType] | None = None
    contract: str | None = None


class RepeatStep(StepOptions):
    repeat: StepId
    max_attempts: Annotated[int, Field(strict=True, ge=1)]
    body: tuple[RepeatBodyNode, ...]
    until: str


class HaltStep(StepOptions):
    halt: str
    message: str | None = None


class ExplicitStep(_Model):
    """Lossless fallback for a stored graph outside the recognized patterns."""

    explicit: ProcedureNode


def _verb(value: Any) -> str | None:
    data = value.model_dump() if isinstance(value, BaseModel) else value
    if not isinstance(data, dict):
        return None
    verbs = [key for key in _VERBS if key in data]
    return verbs[0] if len(verbs) == 1 else None


_VERBS = (
    "source",
    "tap",
    "claim",
    "call",
    "project",
    "transform",
    "guard",
    "inbox",
    "propose",
    "settle",
    "emit",
    "invoke",
    "select",
    "repeat",
    "halt",
    "explicit",
)
Step: TypeAlias = Annotated[
    Annotated[SourceStep, Tag("source")]
    | Annotated[TapStep, Tag("tap")]
    | Annotated[ClaimStep, Tag("claim")]
    | Annotated[CallStep, Tag("call")]
    | Annotated[ProjectStep, Tag("project")]
    | Annotated[TransformStep, Tag("transform")]
    | Annotated[GuardStep, Tag("guard")]
    | Annotated[InboxStep, Tag("inbox")]
    | Annotated[ProposeStep, Tag("propose")]
    | Annotated[SettleStep, Tag("settle")]
    | Annotated[EmitStep, Tag("emit")]
    | Annotated[InvokeStep, Tag("invoke")]
    | Annotated[SelectStep, Tag("select")]
    | Annotated[RepeatStep, Tag("repeat")]
    | Annotated[HaltStep, Tag("halt")]
    | Annotated[ExplicitStep, Tag("explicit")],
    Discriminator(_verb),
]


class ProcedureLimits(_Model):
    wall_clock: str = "PT1M"
    provider_calls: Annotated[int, Field(strict=True, ge=0)] = 100
    capture_bytes: Annotated[int, Field(strict=True, ge=0)] | str = 1048576
    result_bytes: Annotated[int, Field(strict=True, ge=1)] | None = None
    items: Annotated[int, Field(strict=True, ge=1)] | None = None
    repeat_attempts: Annotated[int, Field(strict=True, ge=1)] = 1


class ReturnFrom(_Model):
    from_: StepId = Field(alias="from")
    omit: tuple[str, ...] = ()
    id: StepId = "summary"


class ReturnFields(_Model):
    fields: dict[str, Template]
    id: StepId = "summary"


class ProcedureSpec(_Model):
    procedure: Name
    description: str | None = None
    input: dict[str, FieldType]
    params: dict[str, FieldType] = Field(default_factory=dict)
    returns: StepId | ReturnFrom | ReturnFields | None = None
    activation: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot"
    budget: ProcedureLimits = ProcedureLimits()
    caps: ProcedureLimits | None = None
    slots: dict[str, str] = Field(default_factory=dict)
    acquisition_policy: str | None = None
    steps: tuple[Step, ...] = Field(min_length=1)
    # Names/schema details are optional: retained owners can preserve their
    # contract identities; ordinary new Procedures derive them from their name.
    input_contract: str | None = None
    output_contract: str | None = None
    parameter_contract: str | None = None
    output: dict[str, FieldType] | None = None
    contracts: dict[str, ContractSchema] = Field(default_factory=dict)
    halt_order: tuple[NodeId, ...] = ()
    source: ProcedureSourceProgram | None = None
    measurements: tuple[ProcedureMeasurementDeclaration, ...] = ()
    annotations: JsonValue = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _wire(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return cast(dict[str, Any], _authored_defaults(self, handler(self)))

    @field_validator("input", "params", "output", mode="before")
    @classmethod
    def _field_types(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                name: {"type": field} if isinstance(field, str) else field
                for name, field in value.items()
            }
        return value


def _authored_defaults(value: Any, wire: Any) -> Any:
    """Omit model defaults while preserving every scalar in JSON templates."""
    if isinstance(value, BaseModel) and isinstance(wire, dict):
        result = {}
        for name, field in type(value).model_fields.items():
            key = field.alias if field.alias in wire else name
            if key not in wire:
                continue
            member = getattr(value, name)
            default = field.get_default(call_default_factory=True)
            fixed_type = isinstance(value, PropertySchema) and name == "type"
            if (
                not fixed_type
                and name not in {"kind", "tag"}
                and default is not PydanticUndefined
                and member == default
            ):
                continue
            result[key] = _authored_defaults(member, wire[key])
        return result
    if isinstance(value, dict) and isinstance(wire, dict):
        return {key: _authored_defaults(value[key], member) for key, member in wire.items()}
    if isinstance(value, (tuple, list)) and isinstance(wire, (tuple, list)):
        return [
            _authored_defaults(member, encoded) for member, encoded in zip(value, wire, strict=True)
        ]
    return wire


class ProcedureSpecError(ValueError):
    def __init__(self, code: str, path: str, message: str):
        self.code = "cruxible.authoring." + code
        self.field_path = path
        self.message = message
        self.repair = "Repair the indicated authored step and prepare again."
        super().__init__(f"{self.code} at {path}: {message}")


# A resolver returns only the requested artifact's typed schema/binding details.
# It never enumerates an instance. N1b owns conversion of these names to runtime pins.
Resolver = Callable[[str, Any], Any]


def _fields(value: Mapping[str, FieldType]) -> dict[str, PropertySchema]:
    return {
        name: PropertySchema(type=field)
        if isinstance(field, str)
        else PropertySchema.model_validate(field.model_dump(mode="python"))
        for name, field in value.items()
    }


def _ref(kind: str, name: str, role: str) -> dict[str, str]:
    return {
        "kind": "accepted",
        "role": role,
        "target": name if name.startswith(kind + ":") else kind + ":" + name,
    }


def _duration(text: str) -> dict[str, Any]:
    from cruxible_client.contracts.temporal import parse_iso_duration_microseconds

    return CanonicalDuration(microseconds=parse_iso_duration_microseconds(text)).model_dump(
        mode="json"
    )


def _bytes(value: int | str) -> int:
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"([0-9]+)(B|KiB|MiB|GiB)", value)
    if match is None:
        raise ValueError("size must be an integer or B/KiB/MiB/GiB")
    return int(match[1]) * {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[match[2]]


def _unknown(path: str, value: str, choices: Any) -> None:
    near = get_close_matches(value, list(choices), n=3)
    raise ProcedureSpecError(
        "procedure_ref_unknown",
        path,
        f"Unknown reference {value!r}."
        + (" Did you mean " + ", ".join(near) + "?" if near else ""),
    )


def lower_procedure_spec(spec: ProcedureSpec, *, resolve: Resolver | None = None) -> Any:
    """Produce the existing ProcedureInput, leaving accepted-name pinning to prepare.

    Without vocabulary, unresolved call output schemas are deferred to prepare.
    Parse/topology/input/parameter references are still checked offline.
    """
    from cruxible_client.contracts.authoring.inputs import CarriedContractInput, ProcedureInput

    contracts = dict(spec.contracts)
    schemas: dict[str, dict[str, PropertySchema] | None] = {}
    aliases = {
        _output_alias(step, i)
        for i, step in enumerate(spec.steps)
        if not isinstance(step, (GuardStep, HaltStep, InboxStep, ProposeStep, SettleStep, EmitStep))
    }
    taps = {step.tap for step in spec.steps if isinstance(step, TapStep)}
    collection_aliases = {
        step.claim for step in spec.steps if isinstance(step, ClaimStep) and step.all is not None
    }
    used_ids = {_step_id(step, index) for index, step in enumerate(spec.steps)}
    generated: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []

    def contract(name: str, fields: Mapping[str, PropertySchema], role: str) -> dict[str, str]:
        schema = contracts.get(name, ContractSchema(fields=dict(fields)))
        if schema.fields != fields:
            raise ProcedureSpecError(
                "procedure_operand_type_mismatch",
                "$.contracts." + name,
                "Conflicting contract fields",
            )
        contracts[name] = schema
        return {"kind": "carried_contract", "name": name, "role": role}

    root_in = contract(
        spec.input_contract or spec.procedure + "-input", _fields(spec.input), "contract-in"
    )
    input_fields, param_fields = _fields(spec.input), _fields(spec.params)

    def lookup_ref(text: str, path: str) -> PropertySchema | None:
        fields: dict[str, PropertySchema] | None
        parts = text.removeprefix("$").split(".")
        explicit_step = parts[0] == "steps"
        if explicit_step:
            parts = parts[1:]
        if not explicit_step and parts[0] == "input":
            fields, rest = input_fields, parts[1:]
        elif not explicit_step and parts[0] in {"params", "param"}:
            fields, rest = param_fields, parts[1:]
        elif not explicit_step and parts[0] == "item":
            return None  # the typed item transform validates this scoped reference
        else:
            if parts[0] not in aliases:
                _unknown(path, parts[0], aliases)
            if parts[0] not in schemas:
                raise ProcedureSpecError(
                    "procedure_ref_unknown",
                    path,
                    f"Step {parts[0]!r} has no output before this step",
                )
            fields, rest = schemas[parts[0]], parts[1:]
        if fields is None:
            return None
        if not rest:
            if parts[0] in collection_aliases:
                return PropertySchema(type="list", item_fields=fields)
            return PropertySchema(type="json", json_schema={"type": "object"})
        if parts[0] in collection_aliases:
            raise ProcedureSpecError(
                "procedure_ref_unknown",
                path,
                "A bounded all-Claim read is a list; select fields in an item transform",
            )
        field: PropertySchema | None = None
        for index, member in enumerate(rest):
            if member not in fields:
                _unknown(path, member, fields)
            field = fields[member]
            if index < len(rest) - 1:
                if field.item_fields is not None:
                    fields = field.item_fields
                elif field.type == "json":
                    properties = (field.json_schema or {}).get("properties")
                    if not isinstance(properties, dict):
                        return None
                    fields = {k: _json_field(v) for k, v in properties.items()}
                else:
                    _unknown(path, rest[index + 1], ())
        return field

    def template(value: Any, path: str) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            lookup_ref(value, path)
            if value[1:] in taps:
                return {
                    "rows": "$steps." + value[1:] + ".result.rows",
                    "truncated": "$steps." + value[1:] + ".truncated",
                }
            if any(
                value == root or value.startswith(root + ".")
                for root in ("$input", "$item", "$steps")
            ):
                return value
            return "$steps." + value[1:]
        if isinstance(value, dict):
            return {k: template(v, path + "." + k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [template(v, f"{path}[{i}]") for i, v in enumerate(value)]
        return value

    def predicate(text: str, path: str) -> GuardPredicate:
        try:
            result = parse_procedure_expression(text)
        except ProcedureExpressionError as exc:
            raise ProcedureSpecError(
                "procedure_expression_invalid", path, f"{text!r}: {exc}"
            ) from exc
        _check_predicate(result, path, lookup_ref)
        return result

    if isinstance(spec.returns, (ReturnFrom, ReturnFields)):
        aliases.add(spec.returns.id)
        schemas[spec.returns.id] = None if spec.output is None else _fields(spec.output)
    previous: str | None = None
    tap_fields: dict[str, PropertySchema] | None = None
    for index, step in enumerate(spec.steps):
        path = f"$.steps[{index}]"
        id_ = _step_id(step, index)
        if isinstance(step, ExplicitStep):
            explicit_node = step.explicit.model_dump(mode="json", by_alias=True)
            if hasattr(step.explicit, "as_"):
                schemas[step.explicit.as_] = None
                previous = step.explicit.as_
            nodes.append(explicit_node)
            continue
        node: dict[str, Any] = {"node_id": id_}
        if step.next is not None:
            node["next"] = step.next
        output: dict[str, PropertySchema] | None = None
        alias: str | None = None
        if isinstance(step, (CallStep, SourceStep)):
            details = {} if resolve is None else resolve("binding", step)
            if not isinstance(details, dict):
                raise ValueError("binding resolver must return binding details")
            node.update(
                kind="call" if isinstance(step, CallStep) else "source",
                provider=_ref("Provider", step.provider or "unbound", "provider"),
                interface=_ref(
                    "ProviderInterface", _interface_name(step.interface), "provider-interface"
                ),
            )
            node.update(details.get("bindings", {}))
            if step.provider in spec.slots:
                node["provider"] = {"kind": "slot", "slot_name": step.provider}
            output = details.get("output")
            if isinstance(step, CallStep):
                node["input"] = template(step.input, path + ".input")
                if step.effect_policy:
                    node["effect_policy"] = _ref(
                        "EffectPolicy", step.effect_policy, "effect-policy"
                    )
                _check_call_input(
                    step.input,
                    details.get("input"),
                    path + ".input",
                    lookup_ref,
                    allow_extra=details.get("input_allow_extra", False),
                )
                alias = step.call
            else:
                node.update(
                    capture_contract=_ref("CaptureContract", step.contract, "capture-contract"),
                    request=template(
                        {**details.get("request_defaults", {}), **step.request}
                        if isinstance(step.request, dict)
                        else step.request,
                        path + ".request",
                    ),
                )
                alias = step.source
        elif isinstance(step, TapStep):
            node.update(
                kind="state_tap",
                query=_ref("QueryDefinition", step.query, "query"),
                parameters=template(step.params, path + ".params"),
            )
            if step.budgets is not None:
                node["budgets"] = step.budgets
            from cruxible_client.contracts.procedures.source_views import query_view_schema

            if tap_fields is None:
                tap_fields = query_view_schema().fields
            output = tap_fields
            alias = step.tap
        elif isinstance(step, ClaimStep):
            details = {} if resolve is None else resolve("claim", step)
            output = details.get("output")
            node.update(
                kind="state_claim",
                claim_type=_ref("ClaimType", step.claim_type, "claim-type"),
                subject_kind=details.get("subject_kind")
                or step.subject_kind
                or step.claim_type.rpartition(".")[0],
                subject_id=template(step.subject, path + ".subject"),
                cardinality="one" if step.all is None else "all",
                limit=step.all,
            )
            alias = step.claim
        elif isinstance(step, ProjectStep):
            fields = template(step.fields, path + ".fields")
            output = (
                _fields(step.output)
                if step.output is not None
                else {
                    key: _template_type(value, path + ".fields." + key, lookup_ref)
                    for key, value in step.fields.items()
                }
            )
            node.update(
                kind="project",
                fields=fields,
                contract_out=contract(
                    step.contract or spec.procedure + "-step-" + step.project,
                    output,
                    "contract-out",
                ),
            )
            alias = step.project
        elif isinstance(step, TransformStep):
            output = _fields(step.output)
            node.update(
                kind="transform",
                transform_kind=step.kind,
                spec=template(step.spec.model_dump(mode="json"), path + ".spec"),
                contract_in=contract(
                    step.contract_in or spec.procedure + "-step-" + step.transform + "-input",
                    _fields(step.input),
                    "contract-in",
                ),
                contract_out=contract(
                    step.contract_out or spec.procedure + "-step-" + step.transform + "-output",
                    output,
                    "contract-out",
                ),
            )
            alias = step.transform
        elif isinstance(step, GuardStep):
            node.update(
                kind="guard",
                predicate=predicate(step.guard, path + ".guard").model_dump(mode="json"),
            )
            if step.then:
                node["on_true"] = step.then
            halt_id = id_[:123] + "_halt"
            suffix = 2
            while halt_id in used_ids:
                halt_id = id_[:110] + "_halt_" + str(suffix)
                suffix += 1
            used_ids.add(halt_id)
            node["on_false"] = _branch(
                step.else_, id_, generated, node, path + ".else", halt_id=halt_id
            )
            if (
                step.else_id is not None
                and generated
                and node["on_false"] == generated[-1]["node_id"]
            ):
                generated[-1]["node_id"] = step.else_id
                node["on_false"] = step.else_id
            if step.refusal_code is not None:
                node["refusal_code"] = step.refusal_code
            if step.message is not None:
                node["message"] = step.message
        elif isinstance(step, HaltStep):
            node.update(kind="halt", reason=step.halt)
            if step.message is not None:
                node["message"] = step.message
        elif isinstance(step, InboxStep):
            node.update(kind="post_inbox", input=template(step.inbox, path + ".inbox"))
            if step.max_items is not None:
                node["max_items"] = step.max_items
        elif isinstance(step, (ProposeStep, SettleStep)):
            candidates: Any = step.propose if isinstance(step, ProposeStep) else step.settle
            if isinstance(candidates, str):
                candidates = {"items": candidates}
            node.update(
                kind="propose_change_set" if isinstance(step, ProposeStep) else "settle_change_set",
                candidate_templates=template(candidates, path + ".candidates"),
                result=template(
                    step.result if step.result is not None else "$" + (previous or "input"),
                    path + ".result",
                ),
            )
        elif isinstance(step, EmitStep):
            node.update(
                kind="emit_capture",
                capture_contract=_ref("CaptureContract", step.emit, "capture-contract"),
                input=template(step.input, path + ".input"),
                result=template(step.result, path + ".result"),
            )
        elif isinstance(step, InvokeStep):
            node.update(
                kind="invoke",
                procedure=_ref("Procedure", step.invoke, "procedure"),
                input=template(step.input, path + ".input"),
            )
            if resolve:
                output = resolve("procedure_output", step.invoke)
            alias = step.as_ or id_
        elif isinstance(step, SelectStep):
            for source in step.sources:
                lookup_ref("$" + source, path + ".sources")
            output = (
                _fields(step.output) if step.output is not None else schemas.get(step.sources[0])
            )
            if output is None:
                raise ProcedureSpecError(
                    "procedure_ref_unknown", path, "Select output needs a resolved schema"
                )
            node.update(
                kind="select",
                sources=list(step.sources),
                contract_out=contract(
                    step.contract or spec.procedure + "-step-" + step.select, output, "contract-out"
                ),
            )
            alias = step.select
        elif isinstance(step, RepeatStep):
            enclosing_schemas, enclosing_aliases = dict(schemas), set(aliases)
            for body in step.body:
                aliases.add(body.as_)
                schemas[body.as_] = None
            until = predicate(step.until, path + ".until")
            schemas.clear()
            schemas.update(enclosing_schemas)
            aliases.clear()
            aliases.update(enclosing_aliases)
            node.update(
                kind="repeat",
                max_attempts=step.max_attempts,
                body=[body.model_dump(mode="json", by_alias=True) for body in step.body],
                until=until.model_dump(mode="json"),
            )
            alias = step.repeat
        if alias is not None:
            node["as"] = alias
            schemas[alias] = output
            previous = alias
        nodes.append(node)
    returns = (
        None
        if spec.source is not None and spec.source.rules == "cruxible.procedure-source.v2"
        else spec.returns or previous
    )
    if isinstance(returns, (ReturnFrom, ReturnFields)):
        fields = (
            returns.fields
            if isinstance(returns, ReturnFields)
            else {
                name: "$" + returns.from_ + "." + name
                for name in (schemas.get(returns.from_) or {})
                if name not in returns.omit
            }
        )
        if isinstance(returns, ReturnFrom):
            lookup_ref("$" + returns.from_, "$.returns.from")
            if schemas.get(returns.from_) is None and resolve is not None:
                raise ProcedureSpecError(
                    "procedure_ref_unknown",
                    "$.returns",
                    "Return projection requires the interface output schema at prepare",
                )
            for name in returns.omit:
                if name not in (schemas.get(returns.from_) or {}):
                    _unknown("$.returns.omit", name, schemas.get(returns.from_) or {})
        output = (
            _fields(spec.output)
            if spec.output is not None
            else {
                name: _template_type(value, "$.returns.fields." + name, lookup_ref)
                for name, value in fields.items()
            }
        )
        return_alias = returns.id
        return_node = {
            "kind": "project",
            "node_id": return_alias,
            "as": return_alias,
            "fields": template(fields, "$.returns"),
            "contract_out": contract(
                spec.output_contract or spec.procedure + "-output", output, "contract-out"
            ),
        }
        # A returned summary must dominate every successful terminal. Insert it
        # just after its last data dependency, before guards and egress.
        dependencies = _template_aliases(return_node["fields"])
        position = max(
            (i + 1 for i, node in enumerate(nodes) if node.get("as") in dependencies), default=0
        )
        nodes.insert(position, return_node)
        schemas[return_alias] = output
        returns = return_alias
    if returns is not None and returns not in schemas and schemas:
        _unknown("$.returns", str(returns), schemas)
    output = _fields(spec.output) if spec.output is not None else schemas.get(returns or "")
    if output is None:
        if resolve is not None:
            raise ProcedureSpecError(
                "procedure_ref_unknown",
                "$.returns",
                "Output schema is unavailable; declare output fields",
            )
        output = {}  # deferred at the daemon; never submitted as a resolved graph
    root_out = contract(spec.output_contract or spec.procedure + "-output", output, "contract-out")
    limits = spec.budget
    caps = spec.caps or limits
    if spec.halt_order:
        order = {id_: i for i, id_ in enumerate(spec.halt_order)}
        generated.sort(key=lambda node: order.get(node["node_id"], len(order)))
    definition: dict[str, Any] = dict(
        graph_format=6,
        name=spec.procedure,
        description=spec.description,
        contract_in=root_in,
        contract_out=root_out,
        nodes=nodes + generated,
        returns=returns,
        terminal_capability=derived_terminal_capability(nodes),
        budget={
            "wall_clock": _duration(limits.wall_clock),
            "max_provider_calls": limits.provider_calls,
            "max_capture_bytes": _bytes(limits.capture_bytes),
        },
        hard_caps={
            "max_wall_clock": _duration(caps.wall_clock),
            "max_provider_calls": caps.provider_calls,
            "max_capture_bytes": _bytes(caps.capture_bytes),
            "max_items": caps.items or 5000,
            "max_repeat_attempts": caps.repeat_attempts,
        },
    )
    if spec.source is not None:
        definition["source"] = spec.source.model_dump(mode="json", by_alias=True)
    if spec.measurements:
        definition["measurements"] = [m.model_dump(mode="json") for m in spec.measurements]
    if spec.annotations:
        definition["annotations"] = spec.annotations
    if limits.items is not None:
        definition["budget"]["max_items"] = limits.items
    if limits.result_bytes is not None:
        definition["budget"]["max_result_bytes"] = limits.result_bytes
    if caps.result_bytes is not None:
        definition["hard_caps"]["max_result_bytes"] = caps.result_bytes
    if spec.slots:
        definition["pin_slots"] = (
            [
                {
                    "slot_name": slot,
                    "pin_role": "provider",
                    "artifact_kind": "Provider",
                    "interface_digest": resolve("slot", interface),
                }
                for slot, interface in sorted(spec.slots.items())
            ]
            if resolve is not None
            else []
        )
    if spec.params:
        definition["parameter_contract"] = contract(
            spec.parameter_contract or spec.procedure + "-params",
            param_fields,
            "parameter-contract",
        )
    referenced = _carried_names(definition)
    return ProcedureInput(
        kind="procedure",
        definition=definition,
        activation_policy=spec.activation,
        acquisition_policy=spec.acquisition_policy,
        contracts=tuple(
            CarriedContractInput(
                name=name,
                description=schema.description,
                fields=schema.fields,
                allow_extra=schema.allow_extra,
            )
            for name, schema in sorted(contracts.items())
            if name in referenced
        ),
    )


def _output_alias(step: Step, index: int) -> str:
    if isinstance(step, ExplicitStep):
        return str(getattr(step.explicit, "as_", step.explicit.node_id))
    if isinstance(step, InvokeStep):
        return step.as_ or step.id or f"step_{index + 1}"
    for key in ("source", "tap", "claim", "call", "project", "transform", "select", "repeat"):
        value = getattr(step, key, None)
        if value is not None:
            return str(value)
    return _step_id(step, index)


def _step_id(step: Step, index: int) -> str:
    if isinstance(step, ExplicitStep):
        return step.explicit.node_id
    if step.id is not None:
        return step.id
    for key in ("source", "tap", "claim", "call", "project", "transform", "select", "repeat"):
        value = getattr(step, key, None)
        if value is not None:
            return str(value)
    if isinstance(step, InvokeStep):
        return step.as_ or step.id or f"step_{index + 1}"
    return f"step_{index + 1}"


def _interface_name(value: str) -> str:
    value = value.removeprefix("ProviderInterface:")
    name, at, version = value.rpartition("@")
    return f"{name}/v{version}" if at else value


def _branch(
    text: str,
    id_: str,
    generated: list[dict[str, Any]],
    guard: dict[str, Any],
    path: str,
    *,
    halt_id: str | None = None,
) -> str:
    match = re.fullmatch(r'(halt|abort)\(([a-z][a-z0-9_.-]*)(?:,\s*("(?:[^"\\]|\\.)*"))?\)', text)
    if match is None:
        if re.fullmatch(r"[a-z][a-z0-9_.-]*", text):
            return text
        raise ProcedureSpecError(
            "procedure_expression_invalid",
            path,
            'Expected halt(reason[, "message"]), abort(code, "message") or a step id',
        )
    import json

    verb, reason, message = match.groups()
    if verb == "abort":
        if message is None:
            raise ProcedureSpecError(
                "procedure_expression_invalid", path, "abort requires a message"
            )
        guard.update(refusal_code=reason, message=json.loads(message))
        return "$abort"
    node: dict[str, Any] = {
        "kind": "halt",
        "node_id": halt_id or id_[:123] + "_halt",
        "reason": reason,
    }
    if message is not None:
        node["message"] = json.loads(message)
    generated.append(node)
    return str(node["node_id"])


def _json_field(schema: dict[str, Any]) -> PropertySchema:
    type_ = schema.get("type", "json")
    if type_ == "boolean":
        type_ = "bool"
    if isinstance(type_, list) or type_ in {"object", "array"}:
        return PropertySchema(type="json", json_schema=schema)
    return PropertySchema(type=type_, enum=schema.get("enum"))


def _template_type(value: Any, path: str, lookup: Any) -> PropertySchema:
    if isinstance(value, str) and value.startswith("$"):
        return lookup(value, path) or PropertySchema(type="json")
    if isinstance(value, bool):
        return PropertySchema(type="bool")
    if isinstance(value, int):
        return PropertySchema(type="integer")
    if isinstance(value, str):
        return PropertySchema(type="string")
    return PropertySchema(type="json")


def _check_predicate(predicate: GuardPredicate, path: str, lookup: Any) -> None:
    if predicate.left is None:
        for child in (
            predicate.all_of
            or predicate.any_of
            or ((predicate.not_of,) if predicate.not_of else ())
        ):
            _check_predicate(child, path, lookup)
        return
    assert predicate.right is not None

    def type_of(operand: PredicateOperand) -> PropertySchema | None:
        if operand.kind == "literal":
            return _template_type(operand.value, path, lookup)
        if operand.kind == "parameter":
            return cast(
                PropertySchema | None, lookup("$params." + str(operand.parameter_name), path)
            )
        root = "$input." + operand.input_name if operand.input_name else "$" + str(operand.alias)
        value = lookup(root + ("." + ".".join(operand.path) if operand.path else ""), path)
        if operand.kind in {"count", "truncated", "exists"}:
            if operand.kind == "count" and value is not None and value.type not in {"list", "json"}:
                raise ProcedureSpecError(
                    "procedure_operand_type_mismatch",
                    path,
                    f"count requires a collection, got {value.type}",
                )
            return PropertySchema(type="integer" if operand.kind == "count" else "bool")
        return cast(PropertySchema | None, value)

    left, right = type_of(predicate.left), type_of(predicate.right)
    if left is None or right is None:
        return
    from cruxible_client.contracts.procedures.source_views import schema_json_type

    left_schema, right_schema = schema_json_type(left), schema_json_type(right)
    a, b = left_schema.get("type", left.type), right_schema.get("type", right.type)
    if isinstance(a, list):
        a = (
            next((kind for kind in a if kind != "null"), "null")
            if len(set(a) - {"null"}) <= 1
            else "union"
        )
    if isinstance(b, list):
        b = (
            next((kind for kind in b if kind != "null"), "null")
            if len(set(b) - {"null"}) <= 1
            else "union"
        )
    null = any(
        op.kind == "literal" and op.value is None for op in (predicate.left, predicate.right)
    )
    if (a != b and not (null and predicate.operator in {"eq", "ne"})) or (
        predicate.operator not in {"eq", "ne"}
        and a not in {"integer", "string", "date", "datetime"}
    ):
        raise ProcedureSpecError(
            "procedure_operand_type_mismatch",
            path,
            f"Comparison operands have incompatible types {a} and {b}",
        )
    for operand, other in ((predicate.left, right_schema), (predicate.right, left_schema)):
        if operand.kind == "literal" and other.get("enum") is not None:
            from cruxible_client.contracts.canonical import canonical_bytes

            if canonical_bytes(operand.value) not in {canonical_bytes(v) for v in other["enum"]}:
                raise ProcedureSpecError(
                    "procedure_operand_type_mismatch",
                    path,
                    f"Literal {operand.value!r} is outside enum {other['enum']!r}",
                )


def _check_call_input(
    value: Any,
    fields: dict[str, PropertySchema] | None,
    path: str,
    lookup: Any,
    *,
    allow_extra: bool = False,
) -> None:
    if fields is None or not isinstance(value, dict):
        return
    if not allow_extra:
        for name in value:
            if name not in fields:
                _unknown(path + "." + name, name, fields)
    for name, field in fields.items():
        if name not in value:
            if not field.optional and field.default is None:
                raise ProcedureSpecError(
                    "procedure_ref_unknown",
                    path,
                    f"Required interface input field {name!r} is missing",
                )
            continue
        if value[name] is None and field.optional:
            continue
        if isinstance(value[name], str) and value[name].startswith("$"):
            declared = lookup(value[name], path + "." + name)
            if declared is None:
                continue
        actual = _template_type(value[name], path + "." + name, lookup)
        normalize = {"int": "integer"}
        if field.type != "json" and normalize.get(actual.type, actual.type) != normalize.get(
            field.type, field.type
        ):
            raise ProcedureSpecError(
                "procedure_operand_type_mismatch",
                path + "." + name,
                f"Interface input expects {field.type}, got {actual.type}",
            )


def _template_aliases(value: Any) -> set[str]:
    if isinstance(value, str) and value.startswith("$steps."):
        return {value.split(".")[1]}
    if isinstance(value, dict):
        return set().union(*(_template_aliases(v) for v in value.values()))
    if isinstance(value, (tuple, list)):
        return set().union(*(_template_aliases(v) for v in value))
    return set()


def _carried_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        if value.get("kind") == "carried_contract":
            return {value["name"]}
        return set().union(*(_carried_names(v) for v in value.values()))
    if isinstance(value, (tuple, list)):
        return set().union(*(_carried_names(v) for v in value))
    return set()
