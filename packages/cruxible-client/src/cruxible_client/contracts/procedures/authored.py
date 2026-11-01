"""The one Procedure authoring form: a declared, fully typed ProcedureSpec.

Every surface (MCP/HTTP JSON, CLI YAML, the SDK builder) submits this spec.
A reference is a typed :class:`~cruxible_client.contracts.refs.Ref`; a guard
or a repeat ``until`` is the same typed closed Predicate the closed transforms
use; a guard arm is a typed action (``abort``, ``halt`` or ``goto``). Nothing
here is an expression string.

Lowering is pure and proportional to this Procedure: it produces the stored
Procedure graph (the stored form, :mod:`~cruxible_client.contracts.procedures.models`).
Vocabulary is supplied by keyed lookups at prepare; the parser never evaluates
text or resolves state. :func:`~cruxible_client.contracts.procedures.decompile.decompile_procedure`
renders a stored graph back in this form, so ``get`` reviews exactly what an
author writes.
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
    StrictBool,
    StrictInt,
    StrictStr,
    Tag,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import DEFAULT_REF_TEMPLATE, GenerateJsonSchema, JsonSchemaMode
from pydantic_core import PydanticUndefined
from typing_extensions import TypeAliasType

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.extract import CompleteWhen, ExtractField
from cruxible_client.contracts.procedures.measurements import ProcedureMeasurementDeclaration
from cruxible_client.contracts.procedures.models import (
    TransformKind,
    derived_terminal_capability,
)
from cruxible_client.contracts.procedures.reads import DEFAULT_MAX_READ_ROWS
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.refs import Ref, input_ref, step_ref
from cruxible_client.contracts.shapes import (
    AllOf,
    AnyOf,
    BoolType,
    Contract,
    ContractField,
    ContractRef,
    CountOperand,
    IntType,
    ListType,
    LiteralOperand,
    Membership,
    NestedType,
    Not,
    Predicate,
    PredicateContext,
    Presence,
    StringType,
    as_predicate,
    parse_contract_ref,
    predicate_context_problem,
)

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,255}$")]
StepId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")]
NodeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]


#: A declared field: a Contract field spelling (``int``, ``string?``,
#: ``list<x.row@1>{max_items: 50}``, ...) or its structured form.
FieldType: TypeAlias = ContractField

#: Spellings of the retired owned-Contract schema grammar, with their replacement.
RETIRED_FIELD_SPELLINGS = {
    "integer": "int",
    "float": "number",
    "json": "a typed shape: a nested Contract name@N, list<T>{max_items: N}, or capture<K>",
    "list": "list<T>{max_items: N} (a list of records names its row Contract: list<name@N>)",
}


def _declared_fields(value: Any) -> Any:
    """Refuse a retired field spelling or structure, naming its Contract replacement."""

    if not isinstance(value, Mapping):
        return value
    for name, field in value.items():
        spelled = field.get("type") if isinstance(field, Mapping) else field
        if isinstance(spelled, str):
            bare = spelled.strip().removesuffix("?").strip()
            if bare in RETIRED_FIELD_SPELLINGS:
                raise ValueError(
                    f"procedure_field_type_retired: field {name!r} is typed {bare!r}; "
                    f"a Procedure declares Contract field types: use "
                    f"{RETIRED_FIELD_SPELLINGS[bare]}"
                )
        if isinstance(field, Mapping) and ("item_fields" in field or "json_schema" in field):
            raise ValueError(
                f"procedure_field_type_retired: field {name!r} uses the retired schema "
                "grammar; a list of records is list<name@N>{max_items: N} over a governed "
                "row Contract"
            )
    return value


def _spelled_fields(value: Mapping[str, ContractField] | None) -> dict[str, object] | None:
    """A declared field map rendered as its author spellings (review and round trip)."""

    if value is None:
        return None
    return {
        name: field.spelling
        if field.description is None
        else {"type": field.spelling, "description": field.description}
        for name, field in value.items()
    }


def _template_tag(value: Any) -> str:
    if isinstance(value, Ref) or (isinstance(value, Mapping) and "$ref" in value):
        return "ref"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list | tuple):
        return "list"
    return "scalar"


#: A value template: literal JSON whose leaves may be typed references. A
#: literal string never names a reference (a reference is always a ``Ref``).
Template = TypeAliasType(
    "Template",
    "Annotated[Ref | dict[str, Template] | list[Template] | StrictStr | StrictBool"
    " | StrictInt | None, Field(union_mode='left_to_right')]",
)


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        json_schema_mode_override="validation",
    )


def _typed_predicate(value: Any, context: PredicateContext) -> Predicate:
    predicate = as_predicate(value)
    problem = predicate_context_problem(predicate, context)
    if problem is not None:
        raise ValueError(problem)
    return predicate


def _run_predicate(value: Any) -> Any:
    return _typed_predicate(value, "run")


def _record_predicate(value: Any) -> Any:
    return None if value is None else _typed_predicate(value, "record")


class StepOptions(_Model):
    """``id`` names the stored node; ``next`` jumps forward past fallthrough."""

    id: NodeId | None = None
    next: NodeId | None = None


class ReadStep(StepOptions):
    """A read of accepted state: a named ``query`` with ``params``, or one ClaimType.

    A Claim read names ``claim_type`` and its ``subject`` (and optionally its
    ``subject_kind``); ``all: N`` reads at most N Claims instead of one.
    """

    read: StepId
    query: str | None = None
    params: dict[str, Template] = Field(default_factory=dict)
    claim_type: str | None = None
    subject: Template = None
    subject_kind: str | None = None
    all: Annotated[int, Field(strict=True, ge=1)] | None = None


class CallStep(StepOptions):
    call: StepId
    slot: str
    input: Template
    effect_policy: str | None = None
    captures: dict[str, str] = Field(
        default_factory=dict,
        description="The CaptureContract each capture<K> field of the slot's out Contract "
        "is committed as.",
    )


class ExtractStep(StepOptions):
    """Read one Capture's records, typed: ``capture`` references ``<call>.<capture field>``.

    ``row`` is the Contract (``name@N``) whose fields the step reads; the rest
    are the extract node's declarations (records, key, fields, header,
    complete_when, duplicates, only_keys).
    """

    extract: StepId
    capture: Ref
    records: str
    row: str
    fields: dict[str, ExtractField]
    key: dict[str, ExtractField] = Field(default_factory=dict)
    header: dict[str, ExtractField] = Field(default_factory=dict)
    complete_when: CompleteWhen | None = None
    duplicates: Literal["refuse_run", "refuse_key", "first"] = "refuse_key"
    only_keys: Template = None

    @field_validator("capture")
    @classmethod
    def _capture(cls, value: Ref) -> Ref:
        if value.root != "step" or len(value.path) != 1:
            raise ValueError("an extract reads one capture field: {$ref: step, step, path: [f]}")
        return value


class ProjectStep(StepOptions):
    """A typed record: ``fields`` resolved from the run, or an exact ``literal``.

    A ``literal`` is data: nothing in it is a reference. ``leaf: true`` makes
    the record this branch's own return value (a leaf has no successor).
    """

    project: StepId
    fields: dict[str, Template] | Ref | None = None
    literal: JsonValue = None
    leaf: bool = False
    #: The record's declared field types, checked as the value flows; a
    #: project pins no Contract.
    output: dict[str, FieldType] | None = None

    _declared = field_validator("output", mode="before")(_declared_fields)
    _spelled = field_serializer("output")(_spelled_fields)

    @model_validator(mode="before")
    @classmethod
    def _retired_contract(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "contract" in value:
            raise ValueError(
                "procedure_step_contract_retired: an intermediate project pins no Contract; "
                "its value is typed as it flows (declare output fields to check it)"
            )
        return value

    @model_validator(mode="after")
    def _one_source(self) -> ProjectStep:
        if (self.fields is None) == (self.literal is None):
            raise ValueError("a project states exactly one of fields and literal")
        return self


# -- the closed transforms, typed -------------------------------------------------------


class ShapeFieldSpec(_Model):
    """One shaped field: read ``from`` the record (``$item.fields.<f>``/``$item.key.<k>``),
    or a literal ``value``; ``as`` casts it explicitly."""

    from_: Ref | None = Field(default=None, alias="from")
    value: str | bool | int | None = None
    as_: str | None = Field(default=None, alias="as")

    @model_validator(mode="after")
    def _one_source(self) -> ShapeFieldSpec:
        if (self.from_ is None) == (self.value is None):
            raise ValueError("a shaped field reads from the record or states one literal value")
        return self

    @field_validator("from_")
    @classmethod
    def _from(cls, value: Ref | None) -> Ref | None:
        if value is not None and (
            value.root != "item" or len(value.path) != 2 or value.path[0] not in {"fields", "key"}
        ):
            raise ValueError("a shaped field reads $item.fields.<f> or $item.key.<k>")
        return value


class ShapeSpec(_Model):
    input: Template
    fields: dict[str, ShapeFieldSpec] = Field(min_length=1)


class FilterSpec(_Model):
    input: Template
    where: Predicate

    _where = field_validator("where", mode="before")(_record_predicate)


class JoinSpec(_Model):
    left: Template
    right: Template
    left_field: str
    right_field: str
    mode: Literal["inner", "anti"] = "inner"
    right_fields: dict[str, str] = Field(default_factory=dict)


class AggregateSpec(_Model):
    input: Template
    op: Literal["count", "any", "all", "min", "max", "first_by"]
    field: str | None = None
    where: Predicate | None = None

    _where = field_validator("where", mode="before")(_record_predicate)


class DedupeSpec(_Model):
    input: Template
    keys: tuple[str, ...] = Field(min_length=1)


class UnionSpec(_Model):
    inputs: tuple[Template, ...] = Field(min_length=2)


class DeriveCaseSpec(_Model):
    when: Predicate
    value: str | bool | int | None

    _when = field_validator("when", mode="before")(_record_predicate)


class DeriveSpec(_Model):
    input: dict[str, Template] = Field(min_length=1)
    cases: tuple[DeriveCaseSpec, ...] = Field(min_length=1)
    else_: str | bool | int | None = Field(alias="else")


TransformSpec: TypeAlias = (
    ShapeSpec | FilterSpec | JoinSpec | AggregateSpec | DedupeSpec | UnionSpec | DeriveSpec
)
_TRANSFORM_SPECS: dict[str, type[BaseModel]] = {
    "shape": ShapeSpec,
    "filter": FilterSpec,
    "join": JoinSpec,
    "aggregate": AggregateSpec,
    "dedupe": DedupeSpec,
    "union": UnionSpec,
    "derive": DeriveSpec,
}
_GRAPH_SPEC_TAGS = {
    "shape": "cruxible-transform-shape-spec-v1",
    "filter": "cruxible-transform-filter-spec-v1",
    "join": "cruxible-transform-join-spec-v1",
    "aggregate": "cruxible-transform-aggregate-spec-v1",
    "dedupe": "cruxible-transform-dedupe-spec-v1",
    "union": "cruxible-transform-union-spec-v1",
    "derive": "cruxible-transform-derive-spec-v1",
}


def _kind_spec(value: Any) -> Any:
    """Validate ``spec`` with the model its sibling ``kind`` names."""

    if isinstance(value, Mapping) and "kind" in value and isinstance(value.get("spec"), Mapping):
        model = _TRANSFORM_SPECS.get(str(value["kind"]))
        if model is not None:
            return {**value, "spec": model.model_validate(value["spec"])}
    return value


class TransformStep(StepOptions):
    """A closed transform (shape, filter, join, aggregate, dedupe, union, derive).

    Its records are typed as they flow; it names no Contracts of its own.
    """

    transform: StepId
    kind: TransformKind
    spec: TransformSpec

    _spec = model_validator(mode="before")(_kind_spec)

    @model_validator(mode="after")
    def _spec_kind(self) -> TransformStep:
        if not isinstance(self.spec, _TRANSFORM_SPECS[self.kind]):
            raise ValueError(f"a {self.kind} transform states a {self.kind} spec")
        return self


# -- guards: typed branch actions --------------------------------------------------------


class AbortDetail(_Model):
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,255}$")]
    message: str


class AbortAction(_Model):
    """Refuse the run with this diagnostic."""

    abort: AbortDetail


class HaltAction(_Model):
    """End this branch successfully without a result; ``id`` names the halt node."""

    halt: str | None
    message: str | None = None
    id: NodeId | None = None


class GotoAction(_Model):
    """Continue at a later step."""

    goto: NodeId


def _action_tag(value: Any) -> str | None:
    for key in ("abort", "halt", "goto"):
        if isinstance(value, BaseModel) and hasattr(value, key):
            return key
        if isinstance(value, Mapping) and key in value:
            return key
    return None


BranchAction: TypeAlias = Annotated[
    Annotated[AbortAction, Tag("abort")]
    | Annotated[HaltAction, Tag("halt")]
    | Annotated[GotoAction, Tag("goto")],
    Discriminator(_action_tag),
]

DEFAULT_ABORT = AbortAction(
    abort=AbortDetail(code="guard_refused", message="Procedure guard refused.")
)


class GuardStep(StepOptions):
    """Branch on a typed run predicate.

    ``then`` (default: continue with the next step) and ``else`` (default:
    abort ``guard_refused``) are typed actions.
    """

    guard: Predicate
    then: BranchAction | None = None
    else_: BranchAction = Field(alias="else", default=DEFAULT_ABORT)

    _guard = field_validator("guard", mode="before")(_run_predicate)


class InboxStep(StepOptions):
    """Post typed items to the Line inbox: ``inbox`` names the item Contract, ``name@N``.

    ``for_each`` references a list the run produced (each element posts one
    item, read with ``item`` references); ``fields`` make the item;
    ``dedupe_key`` names the fields that are an item's inbox identity.
    """

    inbox: str
    fields: dict[str, Template]
    for_each: Ref | None = None
    dedupe_key: tuple[str, ...] = ()
    max_items: Annotated[int, Field(strict=True, ge=1)] | None = None


class ProposeStep(StepOptions):
    """Propose Claims: a fixed tuple of item templates, or one item per element of a list."""

    propose: Ref | tuple[Template, ...]
    result: Template = None


class SettleStep(StepOptions):
    settle: Ref | tuple[Template, ...]
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

    _declared = field_validator("output", mode="before")(_declared_fields)
    _spelled = field_serializer("output")(_spelled_fields)

    @model_validator(mode="before")
    @classmethod
    def _retired_contract(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "contract" in value:
            raise ValueError(
                "procedure_step_contract_retired: a select pins no Contract; its value is "
                "typed as it flows"
            )
        return value


class RepeatCall(_Model):
    """One bounded repeat attempt's slotted call."""

    call: StepId
    id: NodeId | None = None
    slot: str
    input: Template
    effect_policy: str | None = None


class RepeatTransform(_Model):
    """One bounded repeat attempt's closed transform."""

    transform: StepId
    id: NodeId | None = None
    kind: TransformKind
    spec: TransformSpec

    _spec = model_validator(mode="before")(_kind_spec)


def _body_tag(value: Any) -> str | None:
    for key in ("call", "transform"):
        if isinstance(value, BaseModel) and hasattr(value, key):
            return key
        if isinstance(value, Mapping) and key in value:
            return key
    return None


RepeatBody: TypeAlias = Annotated[
    Annotated[RepeatCall, Tag("call")] | Annotated[RepeatTransform, Tag("transform")],
    Discriminator(_body_tag),
]


class RepeatStep(StepOptions):
    repeat: StepId
    max_attempts: Annotated[int, Field(strict=True, ge=1)]
    body: tuple[RepeatBody, ...] = Field(min_length=1)
    until: Predicate

    _until = field_validator("until", mode="before")(_run_predicate)


class HaltStep(StepOptions):
    halt: str | None
    message: str | None = None


def _verb(value: Any) -> str | None:
    data = value.model_dump() if isinstance(value, BaseModel) else value
    if not isinstance(data, dict):
        return None
    verbs = [key for key in _VERBS if key in data]
    return verbs[0] if len(verbs) == 1 else None


#: Authored step verbs the current syntax no longer has, each with its replacement.
RETIRED_STEP_VERBS: dict[str, str] = {
    "source": "'call:' with 'captures' (the slot's out Contract carries capture<K> fields)",
    "tap": "'read:' naming its 'query'",
    "claim": "'read:' naming its 'claim_type'",
    "explicit": "the typed step for the node's kind (every graph node has one)",
}

_VERBS = (
    "read",
    "call",
    "extract",
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
)
Step: TypeAlias = Annotated[
    Annotated[ReadStep, Tag("read")]
    | Annotated[CallStep, Tag("call")]
    | Annotated[ExtractStep, Tag("extract")]
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
    | Annotated[HaltStep, Tag("halt")],
    Discriminator(_verb),
]


class ProcedureLimits(_Model):
    """A Procedure's hard caps as authored (lowered to ``ProcedureHardCaps``)."""

    wall_clock: str = "PT1M"
    provider_calls: Annotated[int, Field(strict=True, ge=0)] = 100
    capture_bytes: Annotated[int, Field(strict=True, ge=0)] | str = 1048576
    result_bytes: Annotated[int, Field(strict=True, ge=1)] | None = None
    items: Annotated[int, Field(strict=True, ge=1)] | None = None
    repeat_attempts: Annotated[int, Field(strict=True, ge=1)] = 1
    #: The rows the run's reads may return in all (default: the hard cap's).
    read_rows: Annotated[int, Field(strict=True, ge=1)] | None = None


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
    input: dict[str, FieldType] = Field(
        default_factory=dict,
        description=(
            "Input fields (Contract field spellings); core co-proposes the governed "
            "<procedure>.input@N Contract. Or name one with input_contract."
        ),
    )
    params: dict[str, FieldType] = Field(default_factory=dict)
    returns: StepId | ReturnFrom | ReturnFields | None = None
    activation: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot"
    hard_caps: ProcedureLimits = Field(
        default=ProcedureLimits(),
        description=(
            "The most any run of this Procedure may use. The operating budget is the "
            "Line's (a direct run's ephemeral Line spec), within these caps."
        ),
    )
    slots: dict[str, ProcedureSlot] = Field(
        default_factory=dict,
        description="Each slot's signature {in: name@N, out: name@N, effect}; a Line binds it.",
    )
    steps: tuple[Step, ...] = Field(min_length=1)
    # A governed Contract named name@N instead of inline fields: the Procedure
    # pins it, and core co-proposes nothing for that role.
    input_contract: str | None = None
    output_contract: str | None = None
    parameter_contract: str | None = None
    output: dict[str, FieldType] | None = None
    measurements: tuple[ProcedureMeasurementDeclaration, ...] = ()
    annotations: JsonValue = Field(default_factory=dict)

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = DEFAULT_REF_TEMPLATE,
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        # Honor the model's validation-mode override at the generator root.
        # Otherwise standalone serialization generation can hoist recursive
        # PropertySchema definitions under Output but leave their Input refs.
        return super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode="validation",
            union_format=union_format,
        )

    # No return-schema annotation: this wrap only omits defaults from the
    # model's own shape. A dict return schema would erase the typed served
    # schema and split request/response components in OpenAPI.
    @model_serializer(mode="wrap")
    def _wire(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        return cast(dict[str, Any], _authored_defaults(self, handler(self)))

    @model_validator(mode="before")
    @classmethod
    def _retired_fields(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "source" in value:
            raise ValueError(
                "procedure_source_retired: a ProcedureSpec carries no retained source; "
                "the typed spec is the one authoring form"
            )
        if isinstance(value, Mapping) and ("budget" in value or "caps" in value):
            raise ValueError(
                "procedure_budget_retired: a Procedure declares hard_caps only; the "
                "operating budget is its Line's, within those caps"
            )
        if isinstance(value, Mapping) and "contracts" in value:
            raise ValueError(
                "procedure_owned_contracts_retired: a Procedure carries no Contracts; declare "
                "input/output/params inline (core co-proposes <procedure>.input|output|"
                "params@N) or name a governed Contract (input_contract: name@N)"
            )
        if isinstance(value, Mapping) and "acquisition_defaults" in value:
            raise ValueError(
                "procedure_acquisition_defaults_retired: acquisition rules live on the "
                "Line only; declare them as the Line's acquisition per capture output "
                "(<alias>.<field>)"
            )
        return value

    @field_validator("steps", mode="before")
    @classmethod
    def _retired_steps(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            for index, step in enumerate(value):
                if not isinstance(step, Mapping):
                    continue
                for verb, replacement in RETIRED_STEP_VERBS.items():
                    if verb in step:
                        raise ValueError(
                            f"procedure_node_kind_retired: steps[{index}] uses retired step "
                            f"{verb!r}; use {replacement}"
                        )
        return value

    _declared = field_validator("input", "params", "output", mode="before")(_declared_fields)
    _spelled = field_serializer("input", "params", "output")(_spelled_fields)

    @model_validator(mode="after")
    def _one_contract_per_role(self) -> ProcedureSpec:
        for role, inline, named in (
            ("input", self.input, self.input_contract),
            ("params", self.params, self.parameter_contract),
            ("output", self.output, self.output_contract),
        ):
            if named is None:
                continue
            if inline:
                raise ValueError(
                    f"procedure_contract_ambiguous: {role} is declared inline and as the "
                    f"governed Contract {named!r}; state one"
                )
            parse_contract_ref(named)
        return self


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
                and name not in {"kind", "tag", "op", "root"}
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


def extract_view_fields(
    row_fields: Mapping[str, PropertySchema] | None,
) -> dict[str, PropertySchema]:
    """How later steps see an extract's value: its rows typed by the row Contract."""

    opaque = PropertySchema(type="json", json_schema={"type": "array"})
    return {
        "results": opaque,
        "records": opaque,
        "rows": opaque
        if row_fields is None
        else PropertySchema(type="list", item_fields=dict(row_fields)),
        "header": PropertySchema(type="json", json_schema={"type": "object"}),
        "complete": PropertySchema(type="bool"),
        "count": PropertySchema(type="int"),
    }


def invoke_view_fields(
    child_output: Mapping[str, PropertySchema] | None,
) -> dict[str, PropertySchema] | None:
    """How later steps see an invoke's value: ``{succeeded, status, receipt, value}``.

    ``value`` (the child's output record) is present when the child succeeded.
    """

    if child_output is None:
        return None
    from cruxible_client.contracts.procedures.views import schema_json_type

    return {
        "succeeded": PropertySchema(type="bool"),
        "status": PropertySchema(type="string"),
        "receipt": PropertySchema(type="json", json_schema={"type": "object"}),
        "value": PropertySchema(
            type="json",
            optional=True,
            json_schema=schema_json_type(ContractSchema(fields=dict(child_output))),
        ),
        "terminal": PropertySchema(type="json", optional=True, json_schema={"type": "object"}),
    }


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


def _view_fields(
    fields: Mapping[str, ContractField],
    resolve_contract: Callable[[ContractRef], Contract | None],
) -> dict[str, PropertySchema]:
    """How the flow checker reads declared Contract fields (the operation view)."""

    from cruxible_client.contracts.provider_operations import contract_schema_view

    declared = Contract(name="authored.view", version=1, fields=dict(fields))
    return dict(contract_schema_view(declared, resolve_contract).fields)


def _literal_shape(value: object) -> ContractField | None:
    """The Contract type a literal scalar flows as; ``None`` for structured data."""

    if isinstance(value, bool):
        return ContractField(type=BoolType())
    if isinstance(value, int):
        return ContractField(type=IntType())
    if isinstance(value, str):
        return ContractField(type=StringType())
    return None


#: The governed Contract role of each Procedure boundary, and its definition field.
CONTRACT_ROLES = {"input": "contract_in", "params": "parameter_contract", "output": "contract_out"}


def generated_contract_name(procedure: str, role: str) -> str:
    """The governed Contract core co-proposes for one boundary: ``<procedure>.<role>``."""

    return f"{procedure}.{role}"


def _ref(kind: str, name: str) -> dict[str, Any]:
    return {"target": {"kind": kind, "name": name.removeprefix(kind + ":")}}


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


def graph_template(value: Any, path: str) -> Any:
    """A template in the stored graph's spelling: each Ref becomes its reference string.

    The stored graph reads a ``$``-prefixed string as a reference, so a literal
    string may not begin with ``$``; a ``param`` reference is read only by
    predicates.
    """

    if isinstance(value, Ref):
        if value.root == "param":
            raise ProcedureSpecError(
                "procedure_ref_unsupported",
                path,
                f"{value.render()!r}: a Line parameter is read by guard and transform "
                "predicates, not by value templates",
            )
        return value.graph_text()
    if isinstance(value, str):
        if value.startswith("$"):
            raise ProcedureSpecError(
                "procedure_literal_reserved",
                path,
                f"The literal {value!r} begins with '$'; name a reference with "
                "{\"$ref\": ...}, or write a literal that does not begin with '$'",
            )
        return value
    if isinstance(value, BaseModel):
        return graph_template(value.model_dump(mode="python", by_alias=True), path)
    if isinstance(value, Mapping):
        return {key: graph_template(member, f"{path}.{key}") for key, member in value.items()}
    if isinstance(value, list | tuple):
        return [graph_template(member, f"{path}[{i}]") for i, member in enumerate(value)]
    return value


def lower_procedure_spec(spec: ProcedureSpec, *, resolve: Resolver | None = None) -> Any:
    """Produce the lowered Procedure payload, leaving accepted-name pinning to prepare.

    Without vocabulary, unresolved call output schemas are deferred to prepare.
    Parse/topology/input/parameter references are still checked offline.
    """
    from cruxible_client.contracts.authoring.inputs import lowered_procedure_payload

    def resolve_contract(ref: ContractRef) -> Contract | None:
        if resolve is None:
            return None
        found = resolve("contract", ref)
        return found if isinstance(found, Contract) else None

    def view(fields: Mapping[str, ContractField]) -> dict[str, PropertySchema]:
        return _view_fields(fields, resolve_contract)

    generated: dict[str, dict[str, ContractField]] = {}

    def boundary(
        role: str, inline: Mapping[str, ContractField] | None, named: str | None
    ) -> tuple[dict[str, Any], dict[str, ContractField] | None]:
        """The role's Contract reference and its fields (``None`` when not yet resolvable)."""

        if named is not None:
            ref = parse_contract_ref(named)
            found = resolve_contract(ref)
            if found is None and resolve is not None:
                raise ProcedureSpecError(
                    "artifact_reference_unresolved",
                    f"$.{role}_contract",
                    f"{ref.spelling} is not an accepted Contract",
                )
            return {"target": ref.identity.model_dump(mode="json")}, (
                None if found is None else dict(found.fields)
            )
        fields = dict(inline or {})
        generated[role] = fields
        name = generated_contract_name(spec.procedure, role)
        return _ref("Contract", f"{name}/v1"), fields

    schemas: dict[str, dict[str, PropertySchema] | None] = {}
    #: Each step's value in Contract fields, where the flow knows them.
    shapes: dict[str, dict[str, ContractField] | None] = {}
    aliases = {
        _output_alias(step, i)
        for i, step in enumerate(spec.steps)
        if not isinstance(step, (GuardStep, HaltStep, InboxStep, ProposeStep, SettleStep, EmitStep))
    }
    collection_aliases = {
        step.read
        for step in spec.steps
        if isinstance(step, ReadStep) and step.claim_type is not None and step.all is not None
    }
    used_ids = {_step_id(step, index) for index, step in enumerate(spec.steps)}
    generated_halts: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []

    root_in, input_shape = boundary("input", spec.input, spec.input_contract)
    root_params: dict[str, Any] | None = None
    param_shape: dict[str, ContractField] | None = {}
    if spec.params or spec.parameter_contract is not None:
        root_params, param_shape = boundary("params", spec.params, spec.parameter_contract)
    input_fields = None if input_shape is None else view(input_shape)
    param_fields = None if param_shape is None else view(param_shape)

    def shape_of(ref: Ref) -> ContractField | None:
        """The Contract field a one-member reference reads, where the flow knows it."""

        if len(ref.path) != 1:
            return None
        fields = (
            input_shape
            if ref.root == "input"
            else param_shape
            if ref.root == "param"
            else shapes.get(str(ref.step))
            if ref.root == "step"
            else None
        )
        return None if fields is None else fields.get(ref.path[0])

    def template_shape(value: Any) -> dict[str, ContractField] | None:
        """A record template's Contract fields, when every field's type is known."""

        if not isinstance(value, Mapping):
            return None
        found: dict[str, ContractField] = {}
        for key, member in value.items():
            field = shape_of(member) if isinstance(member, Ref) else _literal_shape(member)
            if field is None:
                return None
            found[key] = field
        return found

    def lookup(ref: Ref, path: str) -> PropertySchema | None:
        fields: dict[str, PropertySchema] | None
        rest = ref.path
        collection = False
        if ref.root == "param":
            fields = param_fields
        elif ref.root == "input":
            fields = input_fields
        elif ref.root == "item":
            return None  # the typed item transform validates this scoped reference
        else:
            step = str(ref.step)
            if step not in aliases:
                _unknown(path, step, aliases)
            if step not in schemas:
                raise ProcedureSpecError(
                    "procedure_ref_unknown",
                    path,
                    f"Step {step!r} has no output before this step",
                )
            fields = schemas[step]
            collection = step in collection_aliases
        if fields is None:
            return None
        if not rest:
            if collection:
                return PropertySchema(type="list", item_fields=fields)
            return PropertySchema(type="json", json_schema={"type": "object"})
        if collection:
            raise ProcedureSpecError(
                "procedure_ref_unknown",
                path,
                "A bounded all-Claim read is a list; select fields in an item transform",
            )
        field: PropertySchema | None = None
        for index, member in enumerate(rest):
            if member.isdigit() and field is not None and field.type == "list":
                continue  # one element of the list: its fields are the item's
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
        _check_refs(value, path)
        return graph_template(value, path)

    def _check_refs(value: Any, path: str) -> None:
        if isinstance(value, Ref):
            lookup(value, path)
        elif isinstance(value, Mapping):
            for key, member in value.items():
                _check_refs(member, f"{path}.{key}")
        elif isinstance(value, list | tuple):
            for i, member in enumerate(value):
                _check_refs(member, f"{path}[{i}]")

    def predicate(value: Predicate, path: str) -> dict[str, Any]:
        _check_predicate(value, path, lookup)
        return value.model_dump(mode="json", by_alias=True)

    def transform_spec(kind: str, value: BaseModel, path: str) -> dict[str, Any]:
        def dumped(predicate: Predicate | None, where: str) -> dict[str, Any] | None:
            if predicate is None:
                return None
            _check_predicate(predicate, where, lookup)
            return predicate.model_dump(mode="json", by_alias=True)

        data: dict[str, Any] = {"tag": _GRAPH_SPEC_TAGS[kind]}
        if isinstance(value, ShapeSpec):
            data["input"] = template(value.input, path + ".input")
            data["fields"] = {
                name: {
                    key: member
                    for key, member in (
                        ("from", None if shaped.from_ is None else shaped.from_.graph_text()),
                        ("value", shaped.value),
                        ("as", shaped.as_),
                    )
                    if member is not None
                }
                for name, shaped in value.fields.items()
            }
        elif isinstance(value, FilterSpec):
            data["input"] = template(value.input, path + ".input")
            data["where"] = dumped(value.where, path + ".where")
        elif isinstance(value, JoinSpec):
            data.update(
                left=template(value.left, path + ".left"),
                right=template(value.right, path + ".right"),
                left_field=value.left_field,
                right_field=value.right_field,
                mode=value.mode,
                right_fields=dict(value.right_fields),
            )
        elif isinstance(value, AggregateSpec):
            data.update(input=template(value.input, path + ".input"), op=value.op)
            if value.field is not None:
                data["field"] = value.field
            if value.where is not None:
                data["where"] = dumped(value.where, path + ".where")
        elif isinstance(value, DedupeSpec):
            data.update(input=template(value.input, path + ".input"), keys=list(value.keys))
        elif isinstance(value, UnionSpec):
            data["inputs"] = [
                template(member, f"{path}.inputs[{i}]") for i, member in enumerate(value.inputs)
            ]
        elif isinstance(value, DeriveSpec):
            data.update(
                input=template(value.input, path + ".input"),
                cases=[
                    {"when": dumped(case.when, f"{path}.cases[{i}].when"), "value": case.value}
                    for i, case in enumerate(value.cases)
                ],
            )
            data["else"] = value.else_
        return data

    def action(
        value: AbortAction | HaltAction | GotoAction,
        guard: dict[str, Any],
        guard_id: str,
        path: str,
    ) -> str:
        if isinstance(value, GotoAction):
            return value.goto
        if isinstance(value, AbortAction):
            stated = (guard.get("refusal_code"), guard.get("message"))
            wanted = (value.abort.code, value.abort.message)
            if stated != (None, None) and stated != wanted:
                raise ProcedureSpecError(
                    "procedure_step_invalid",
                    path,
                    "Both arms of one guard abort; they state one diagnostic",
                )
            guard.update(refusal_code=value.abort.code, message=value.abort.message)
            return "$abort"
        halt_id = value.id
        if halt_id is None:
            halt_id = guard_id[:123] + "_halt"
            suffix = 2
            while halt_id in used_ids:
                halt_id = guard_id[:110] + "_halt_" + str(suffix)
                suffix += 1
        used_ids.add(halt_id)
        node: dict[str, Any] = {"kind": "halt", "node_id": halt_id, "reason": value.halt}
        if value.message is not None:
            node["message"] = value.message
        generated_halts.append(node)
        return halt_id

    if isinstance(spec.returns, (ReturnFrom, ReturnFields)):
        aliases.add(spec.returns.id)
        schemas[spec.returns.id] = None if spec.output is None else view(spec.output)
    previous: str | None = None
    for index, step in enumerate(spec.steps):
        path = f"$.steps[{index}]"
        id_ = _step_id(step, index)
        node: dict[str, Any] = {"node_id": id_}
        if step.next is not None:
            node["next"] = step.next
        output: dict[str, PropertySchema] | None = None
        output_shape: dict[str, ContractField] | None = None
        alias: str | None = None
        if isinstance(step, CallStep):
            details = {} if resolve is None else resolve("binding", step)
            if not isinstance(details, dict):
                raise ValueError("binding resolver must return binding details")
            if step.slot not in spec.slots:
                raise ProcedureSpecError(
                    "procedure_slot_undeclared",
                    path + ".slot",
                    f"Slot {step.slot!r} is not declared in slots: {sorted(spec.slots)}",
                )
            node.update(kind="call", slot=step.slot)
            output = details.get("output")
            output_shape = details.get("output_contract")
            if step.captures:
                node["captures"] = {
                    field: _ref("CaptureContract", name)
                    for field, name in sorted(step.captures.items())
                }
                node["input"] = template(
                    {**details.get("request_defaults", {}), **step.input}
                    if isinstance(step.input, dict)
                    else step.input,
                    path + ".input",
                )
            else:
                node["input"] = template(step.input, path + ".input")
                _check_call_input(
                    step.input,
                    details.get("input"),
                    path + ".input",
                    lookup,
                    allow_extra=details.get("input_allow_extra", False),
                )
            if step.effect_policy:
                node["effect_policy"] = _ref("EffectPolicy", step.effect_policy)
            alias = step.call
        elif isinstance(step, ExtractStep):
            capture = template(step.capture, path + ".capture")
            details = {} if resolve is None else resolve("extract_row", step)
            row = details.get("row") or parse_contract_ref(step.row).model_dump(mode="json")
            node.update(
                kind="extract",
                capture=capture,
                records=step.records,
                row=row,
                fields={name: field.model_dump(mode="json") for name, field in step.fields.items()},
                duplicates=step.duplicates,
            )
            if step.key:
                node["key"] = {
                    name: field.model_dump(mode="json") for name, field in step.key.items()
                }
            if step.header:
                node["header"] = {
                    name: field.model_dump(mode="json") for name, field in step.header.items()
                }
            if step.complete_when is not None:
                node["complete_when"] = step.complete_when.model_dump(mode="json")
            if step.only_keys is not None:
                node["only_keys"] = template(step.only_keys, path + ".only_keys")
            output = extract_view_fields(details.get("row_fields"))
            alias = step.extract
        elif isinstance(step, ReadStep) and step.query is not None:
            if step.claim_type is not None or step.subject is not None or step.all is not None:
                raise ProcedureSpecError(
                    "procedure_step_invalid",
                    path,
                    "A read names exactly one of query and claim_type",
                )
            query = None if resolve is None else resolve("query", step)
            node.update(
                kind="read",
                query=_ref("QueryDefinition", step.query),
                parameters=template(step.params, path + ".params"),
            )
            output = _query_view_fields()
            row_ref = getattr(query, "row_contract", None)
            if isinstance(row_ref, ContractRef):
                # A read's rows are typed by its Query's row Contract.
                rows = ContractField(
                    type=ListType(
                        item=NestedType(contract=row_ref),
                        max_items=spec.hard_caps.read_rows or DEFAULT_MAX_READ_ROWS,
                    )
                )
                output_shape = {"rows": rows}
                output = {**output, **view(output_shape)}
            alias = step.read
        elif isinstance(step, ReadStep):
            if step.claim_type is None or step.subject is None:
                raise ProcedureSpecError(
                    "procedure_step_invalid",
                    path,
                    "A read names a query, or a claim_type and its subject",
                )
            details = {} if resolve is None else resolve("claim", step)
            output = details.get("output")
            node.update(
                kind="read",
                claim_type=_ref("ClaimType", step.claim_type),
                subject_kind=details.get("subject_kind")
                or step.subject_kind
                or step.claim_type.rpartition(".")[0],
                subject_id=template(step.subject, path + ".subject"),
                cardinality="one" if step.all is None else "all",
            )
            if step.all is not None:
                node["limit"] = step.all
            alias = step.read
        elif isinstance(step, ProjectStep):
            if step.literal is not None:
                if step.output is not None:
                    output, output_shape = view(step.output), dict(step.output)
                elif isinstance(step.literal, dict):
                    output_shape = template_shape(step.literal)
                    output = {
                        key: _template_type(value, path + ".literal." + key, lookup)
                        for key, value in step.literal.items()
                    }
                node.update(kind="project", literal=step.literal)
            else:
                assert step.fields is not None
                fields = template(step.fields, path + ".fields")
                if step.output is not None:
                    output, output_shape = view(step.output), dict(step.output)
                    if isinstance(step.fields, dict):
                        # A declared record checks its fields, literal and referenced.
                        _check_call_input(
                            step.fields, output, path + ".fields", lookup, allow_extra=True
                        )
                elif isinstance(step.fields, dict):
                    output = {
                        key: _template_type(value, path + ".fields." + key, lookup)
                        for key, value in step.fields.items()
                    }
                    output_shape = template_shape(step.fields)
                node.update(kind="project", fields=fields)
            if step.leaf:
                node["leaf"] = True
            alias = step.project
        elif isinstance(step, TransformStep):
            # A closed transform's output is typed as it flows (records, or one
            # aggregate or derived value): later references resolve at run time.
            output = None
            node.update(
                kind="transform",
                transform_kind=step.kind,
                spec=transform_spec(step.kind, step.spec, path + ".spec"),
            )
            alias = step.transform
        elif isinstance(step, GuardStep):
            node.update(kind="guard", predicate=predicate(step.guard, path + ".guard"))
            if step.then is not None:
                node["on_true"] = action(step.then, node, id_, path + ".then")
            node["on_false"] = action(step.else_, node, id_, path + ".else")
        elif isinstance(step, HaltStep):
            node.update(kind="halt", reason=step.halt)
            if step.message is not None:
                node["message"] = step.message
        elif isinstance(step, InboxStep):
            node.update(
                kind="post_inbox",
                contract=step.inbox,
                fields=template(step.fields, path + ".fields"),
            )
            if step.for_each is not None:
                node["for_each"] = template(step.for_each, path + ".for_each")
            if step.dedupe_key:
                node["dedupe_key"] = list(step.dedupe_key)
            if step.max_items is not None:
                node["max_items"] = step.max_items
        elif isinstance(step, (ProposeStep, SettleStep)):
            candidates: Any = step.propose if isinstance(step, ProposeStep) else step.settle
            if isinstance(candidates, Ref):
                candidates = {"items": candidates}
            returned = (
                spec.returns.id
                if isinstance(spec.returns, (ReturnFrom, ReturnFields))
                else spec.returns
                if isinstance(spec.returns, str)
                else previous
            )
            default_result = input_ref() if returned is None else step_ref(returned)
            node.update(
                kind="propose_change_set" if isinstance(step, ProposeStep) else "settle_change_set",
                candidate_templates=template(candidates, path + ".candidates"),
                result=template(
                    step.result if step.result is not None else default_result,
                    path + ".result",
                ),
            )
        elif isinstance(step, EmitStep):
            node.update(
                kind="emit_capture",
                capture_contract=_ref("CaptureContract", step.emit),
                input=template(step.input, path + ".input"),
                result=template(step.result, path + ".result"),
            )
        elif isinstance(step, InvokeStep):
            node.update(
                kind="invoke",
                procedure=_ref("Procedure", step.invoke),
                input=template(step.input, path + ".input"),
            )
            if resolve:
                output = invoke_view_fields(resolve("procedure_output", step))
            alias = step.as_ or id_
        elif isinstance(step, SelectStep):
            for source in step.sources:
                lookup(step_ref(source), path + ".sources")
            output = view(step.output) if step.output is not None else schemas.get(step.sources[0])
            output_shape = (
                dict(step.output) if step.output is not None else shapes.get(step.sources[0])
            )
            node.update(kind="select", sources=list(step.sources))
            alias = step.select
        elif isinstance(step, RepeatStep):
            enclosing_schemas, enclosing_aliases = dict(schemas), set(aliases)
            body: list[dict[str, Any]] = []
            for position, member in enumerate(step.body):
                member_path = f"{path}.body[{position}]"
                if isinstance(member, RepeatCall):
                    if member.slot not in spec.slots:
                        raise ProcedureSpecError(
                            "procedure_slot_undeclared",
                            member_path + ".slot",
                            f"Slot {member.slot!r} is not declared in slots: {sorted(spec.slots)}",
                        )
                    item: dict[str, Any] = {
                        "node_id": member.id or member.call,
                        "operation": "call",
                        "slot": member.slot,
                        "spec": template(member.input, member_path + ".input"),
                        "as": member.call,
                    }
                    if member.effect_policy is not None:
                        item["effect_policy"] = _ref("EffectPolicy", member.effect_policy)
                    alias_name = member.call
                else:
                    item = {
                        "node_id": member.id or member.transform,
                        "operation": "transform",
                        "transform_kind": member.kind,
                        "spec": transform_spec(member.kind, member.spec, member_path + ".spec"),
                        "as": member.transform,
                    }
                    alias_name = member.transform
                body.append(item)
                aliases.add(alias_name)
                schemas[alias_name] = None
            until = predicate(step.until, path + ".until")
            schemas.clear()
            schemas.update(enclosing_schemas)
            aliases.clear()
            aliases.update(enclosing_aliases)
            node.update(kind="repeat", max_attempts=step.max_attempts, body=body, until=until)
            alias = step.repeat
        if alias is not None:
            node["as"] = alias
            schemas[alias] = output
            shapes[alias] = output_shape
            previous = alias
        nodes.append(node)
    leaves = any(isinstance(step, ProjectStep) and step.leaf for step in spec.steps)
    returns: Any = None if leaves and spec.returns is None else spec.returns or previous
    if isinstance(returns, (ReturnFrom, ReturnFields)):
        fields_template: Any = (
            returns.fields
            if isinstance(returns, ReturnFields)
            else {
                name: step_ref(returns.from_, name)
                for name in (schemas.get(returns.from_) or {})
                if name not in returns.omit
            }
        )
        if isinstance(returns, ReturnFrom):
            lookup(step_ref(returns.from_), "$.returns.from")
            if schemas.get(returns.from_) is None and resolve is not None:
                raise ProcedureSpecError(
                    "procedure_ref_unknown",
                    "$.returns",
                    "Return projection requires the slot output schema at prepare",
                )
            if schemas.get(returns.from_) is not None:
                for name in returns.omit:
                    if name not in (schemas.get(returns.from_) or {}):
                        _unknown("$.returns.omit", name, schemas.get(returns.from_) or {})
        output = (
            view(spec.output)
            if spec.output is not None
            else {
                name: _template_type(value, "$.returns.fields." + name, lookup)
                for name, value in fields_template.items()
            }
        )
        return_alias = returns.id
        return_node = {
            "kind": "project",
            "node_id": return_alias,
            "as": return_alias,
            "fields": template(fields_template, "$.returns"),
        }
        # A returned summary must dominate every successful terminal. Insert it
        # just after its last data dependency, before guards and egress.
        dependencies = _template_aliases(return_node["fields"])
        position = max(
            (i + 1 for i, node in enumerate(nodes) if node.get("as") in dependencies), default=0
        )
        nodes.insert(position, return_node)
        schemas[return_alias] = output
        shapes[return_alias] = template_shape(fields_template)
        returns = return_alias
    if returns is not None and returns not in schemas and schemas:
        _unknown("$.returns", str(returns), schemas)
    returned_shape = shapes.get(returns or "") if isinstance(returns, str) else None
    if spec.output is not None and returned_shape is not None:
        missing = sorted(
            name
            for name, field in spec.output.items()
            if not field.optional and name not in returned_shape
        )
        undeclared = sorted(set(returned_shape) - set(spec.output))
        from cruxible_client.contracts.shapes import edge_refusal

        mistyped = sorted(
            f"{name}: {problem}"
            for name, field in returned_shape.items()
            if name in spec.output
            and (problem := edge_refusal(field.type, spec.output[name].type)) is not None
        )
        if missing or undeclared or mistyped:
            raise ProcedureSpecError(
                "procedure_operand_type_mismatch",
                "$.output",
                f"The returned value does not fit the declared output Contract: missing "
                f"{missing}, undeclared {undeclared}, mistyped {mistyped}",
            )
    root_shape: dict[str, ContractField] | None = (
        None if spec.output_contract is not None else spec.output
    )
    if root_shape is None and spec.output_contract is None:
        root_shape = shapes.get(returns or "")
        if root_shape is None:
            if resolve is not None and not leaves:
                raise ProcedureSpecError(
                    "procedure_output_undeclared",
                    "$.output",
                    "The returned value is not typed by a Contract here; declare the output "
                    "fields (core co-proposes <procedure>.output@N) or name output_contract",
                )
            root_shape = {}  # deferred at the daemon; never submitted as a resolved graph
    root_out, _ = boundary("output", root_shape, spec.output_contract)
    caps = spec.hard_caps
    definition: dict[str, Any] = dict(
        graph_format=8,
        name=spec.procedure,
        description=spec.description,
        contract_in=root_in,
        contract_out=root_out,
        nodes=nodes + generated_halts,
        returns=returns,
        terminal_capability=derived_terminal_capability(nodes),
        hard_caps={
            "max_wall_clock": _duration(caps.wall_clock),
            "max_provider_calls": caps.provider_calls,
            "max_capture_bytes": _bytes(caps.capture_bytes),
            "max_items": caps.items or 5000,
            "max_repeat_attempts": caps.repeat_attempts,
        },
    )
    if spec.measurements:
        definition["measurements"] = [m.model_dump(mode="json") for m in spec.measurements]
    if spec.annotations:
        definition["annotations"] = spec.annotations
    if caps.read_rows is not None:
        definition["hard_caps"]["max_read_rows"] = caps.read_rows
    if caps.result_bytes is not None:
        definition["hard_caps"]["max_result_bytes"] = caps.result_bytes
    if spec.slots:
        definition["slots"] = {
            name: (resolve("slot", slot) if resolve is not None else slot).model_dump(
                mode="json", by_alias=True
            )
            for name, slot in sorted(spec.slots.items())
        }
    if root_params is not None:
        definition["parameter_contract"] = root_params
    return lowered_procedure_payload(
        definition=definition,
        activation_policy=spec.activation,
        generated={role: fields for role, fields in sorted(generated.items())},
    )


def _query_view_fields() -> dict[str, PropertySchema]:
    from cruxible_client.contracts.procedures.views import query_view_schema

    return query_view_schema().fields


def _output_alias(step: Step, index: int) -> str:
    if isinstance(step, InvokeStep):
        return step.as_ or step.id or f"step_{index + 1}"
    for key in ("read", "call", "extract", "project", "transform", "select", "repeat"):
        value = getattr(step, key, None)
        if value is not None:
            return str(value)
    return _step_id(step, index)


def _step_id(step: Step, index: int) -> str:
    if step.id is not None:
        return step.id
    for key in ("read", "call", "extract", "project", "transform", "select", "repeat"):
        value = getattr(step, key, None)
        if value is not None:
            return str(value)
    if isinstance(step, InvokeStep):
        return step.as_ or step.id or f"step_{index + 1}"
    return f"step_{index + 1}"


def _json_field(schema: dict[str, Any]) -> PropertySchema:
    type_ = schema.get("type", "json")
    if type_ == "boolean":
        type_ = "bool"
    if (
        isinstance(type_, list)
        or type_ in {"object", "array", "null", "json"}
        or set(schema) - {"type", "enum"}
    ):
        return PropertySchema(type="json", json_schema=schema)
    return PropertySchema(type=type_, enum=schema.get("enum"))


def _template_type(value: Any, path: str, lookup: Any) -> PropertySchema:
    if isinstance(value, Ref):
        return lookup(value, path) or PropertySchema(type="json")
    if isinstance(value, bool):
        return PropertySchema(type="bool")
    if isinstance(value, int):
        return PropertySchema(type="integer")
    if isinstance(value, str):
        return PropertySchema(type="string")
    if isinstance(value, list | tuple):
        return PropertySchema(type="json", json_schema={"type": "array"})
    return PropertySchema(type="json")


def _literal_type(value: object) -> PropertySchema:
    if isinstance(value, bool):
        return PropertySchema(type="bool")
    if isinstance(value, int):
        return PropertySchema(type="integer")
    return PropertySchema(type="string")


def _check_predicate(predicate: Predicate, path: str, lookup: Any) -> None:
    """Every reference resolves, and each comparison's operands have one type."""

    if isinstance(predicate, AllOf | AnyOf):
        for child in predicate.of:
            _check_predicate(child, path, lookup)
        return
    if isinstance(predicate, Not):
        _check_predicate(predicate.of, path, lookup)
        return
    if isinstance(predicate, Presence):
        lookup(predicate.of, path)
        return

    def type_of(operand: object) -> PropertySchema | None:
        if isinstance(operand, LiteralOperand):
            return _literal_type(operand.literal)
        if isinstance(operand, CountOperand):
            value = lookup(operand.count, path)
            if value is not None and value.type not in {"list", "json"}:
                raise ProcedureSpecError(
                    "procedure_operand_type_mismatch",
                    path,
                    f"count requires a collection, got {value.type}",
                )
            return PropertySchema(type="integer")
        assert isinstance(operand, Ref)
        return cast(PropertySchema | None, lookup(operand, path))

    if isinstance(predicate, Membership):
        type_of(predicate.left)
        if isinstance(predicate.right, Ref):
            lookup(predicate.right, path)
        return
    left, right = type_of(predicate.left), type_of(predicate.right)
    if left is None or right is None:
        return
    from cruxible_client.contracts.procedures.views import schema_json_type

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
    if a != b or (
        predicate.op not in {"eq", "ne"} and a not in {"integer", "string", "date", "datetime"}
    ):
        raise ProcedureSpecError(
            "procedure_operand_type_mismatch",
            path,
            f"Comparison operands have incompatible types {a} and {b}",
        )
    for operand, other in ((predicate.left, right_schema), (predicate.right, left_schema)):
        if isinstance(operand, LiteralOperand) and other.get("enum") is not None:
            from cruxible_client.contracts.canonical import canonical_bytes

            if canonical_bytes(operand.literal) not in {canonical_bytes(v) for v in other["enum"]}:
                raise ProcedureSpecError(
                    "procedure_operand_type_mismatch",
                    path,
                    f"Literal {operand.literal!r} is outside enum {other['enum']!r}",
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
                    f"Required field {name!r} is missing",
                )
            continue
        if value[name] is None and field.optional:
            continue
        if isinstance(value[name], Ref):
            declared = lookup(value[name], path + "." + name)
            if declared is None:
                continue
        actual = _template_type(value[name], path + "." + name, lookup)
        from cruxible_client.contracts.procedures.views import schema_json_type

        actual_type = schema_json_type(actual).get("type", actual.type)
        expected_type = schema_json_type(field).get("type", field.type)
        actual_types = set(actual_type) if isinstance(actual_type, list) else {actual_type}
        expected_types = set(expected_type) if isinstance(expected_type, list) else {expected_type}
        if field.type != "json" and not actual_types <= expected_types:
            raise ProcedureSpecError(
                "procedure_operand_type_mismatch",
                path + "." + name,
                f"Field {name!r} expects {expected_type}, got {actual_type}",
            )


def _template_aliases(value: Any) -> set[str]:
    if isinstance(value, str) and value.startswith("$steps."):
        return {value.split(".")[1]}
    if isinstance(value, dict):
        return set().union(*(_template_aliases(v) for v in value.values()))
    if isinstance(value, (tuple, list)):
        return set().union(*(_template_aliases(v) for v in value))
    return set()


__all__ = [
    "AbortAction",
    "AbortDetail",
    "AggregateSpec",
    "BranchAction",
    "CallStep",
    "DedupeSpec",
    "DeriveCaseSpec",
    "DeriveSpec",
    "EmitStep",
    "ExtractStep",
    "FilterSpec",
    "GotoAction",
    "GuardStep",
    "HaltAction",
    "HaltStep",
    "InboxStep",
    "InvokeStep",
    "JoinSpec",
    "CONTRACT_ROLES",
    "RETIRED_FIELD_SPELLINGS",
    "ProcedureLimits",
    "ProcedureSpec",
    "ProcedureSpecError",
    "ProjectStep",
    "ProposeStep",
    "ReadStep",
    "Ref",
    "RepeatCall",
    "RepeatStep",
    "RepeatTransform",
    "ReturnFields",
    "ReturnFrom",
    "SelectStep",
    "SettleStep",
    "ShapeFieldSpec",
    "ShapeSpec",
    "Step",
    "Template",
    "TransformSpec",
    "TransformStep",
    "UnionSpec",
    "generated_contract_name",
    "graph_template",
    "lower_procedure_spec",
]
