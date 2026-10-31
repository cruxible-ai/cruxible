"""The Procedure graph node models, predicates and terminal authority table.

Moved out of :mod:`~cruxible_client.contracts.procedures.models`, which keeps
``ProcedureDefinition`` and re-exports every name here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactNameRef
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.line_inbox import INBOX_RUN_ITEM_CAP_MAX
from cruxible_client.contracts.procedures.extract import ExtractNode
from cruxible_client.contracts.procedures.model_base import (
    _ALIAS_RE,
    _NAME_RE,
    _canonical_identifier,
    _StrictProcedureModel,
)

PredicateScalar = None | bool | int | str


class PredicateOperand(_StrictProcedureModel):
    """One operand in the closed predicate grammar."""

    tag: Literal["playbill-predicate-operand-v1"] = "playbill-predicate-operand-v1"
    kind: Literal["literal", "input", "step", "parameter", "count", "exists", "truncated"]
    value: PredicateScalar = None
    input_name: str | None = None
    alias: str | None = None
    path: tuple[str, ...] = ()
    parameter_name: str | None = None

    @field_validator("input_name", "alias", "parameter_name")
    @classmethod
    def _optional_name(cls, value: str | None) -> str | None:
        if value is not None:
            _canonical_identifier(value, _ALIAS_RE, label="predicate name")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for member in value:
            _canonical_identifier(member, _ALIAS_RE, label="predicate path member")
        return value

    @model_validator(mode="after")
    def _closed_shape(self) -> "PredicateOperand":
        expected = {
            "literal": (True, False, False, False),
            "input": (False, self.input_name is not None, False, False),
            "step": (False, False, self.alias is not None, False),
            "parameter": (False, False, False, self.parameter_name is not None),
            "count": (False, False, self.alias is not None, False),
            "exists": (False, self.input_name is not None, self.alias is not None, False),
            "truncated": (False, False, self.alias is not None, False),
        }[self.kind]
        actual = (
            self.value is not None,
            self.input_name is not None,
            self.alias is not None,
            self.parameter_name is not None,
        )
        if self.kind == "literal" and self.value is None:
            actual = (True, *actual[1:])
        if self.kind == "exists":
            if (self.input_name is None) == (self.alias is None):
                raise ValueError("exists requires exactly one input_name or alias")
            expected = actual
        if actual != expected:
            raise ValueError(f"predicate operand fields disagree with kind {self.kind!r}")
        if self.path and self.kind not in {"input", "step", "exists"}:
            raise ValueError("predicate paths belong only to input, step, or exists operands")
        return self


ComparisonOperator = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "before", "on_or_before", "after", "on_or_after"
]


class GuardPredicate(_StrictProcedureModel):
    """Closed predicate: exactly one comparison or connective."""

    tag: Literal["playbill-guard-predicate-v1"] = "playbill-guard-predicate-v1"
    left: PredicateOperand | None = None
    operator: ComparisonOperator | None = None
    right: PredicateOperand | None = None
    all_of: tuple[GuardPredicate, ...] | None = None
    any_of: tuple[GuardPredicate, ...] | None = None
    not_of: GuardPredicate | None = None

    @model_validator(mode="after")
    def _one_production(self) -> "GuardPredicate":
        comparison_parts = (self.left, self.operator, self.right)
        set_count = sum(item is not None for item in comparison_parts)
        if set_count not in {0, 3}:
            raise ValueError("guard comparison requires left, operator, and right together")
        productions = sum(
            (
                set_count == 3,
                self.all_of is not None,
                self.any_of is not None,
                self.not_of is not None,
            )
        )
        if productions != 1:
            raise ValueError("guard requires exactly one closed predicate production")
        if self.all_of is not None and not self.all_of:
            raise ValueError("all_of must not be empty")
        if self.any_of is not None and not self.any_of:
            raise ValueError("any_of must not be empty")
        return self

    def step_aliases(self) -> tuple[str, ...]:
        if self.left is not None and self.right is not None:
            return tuple(
                sorted(
                    {
                        operand.alias
                        for operand in (self.left, self.right)
                        if operand.alias is not None
                    },
                    key=lambda item: item.encode("utf-8"),
                )
            )
        children = self.all_of or self.any_of or ((self.not_of,) if self.not_of else ())
        return tuple(
            sorted(
                {alias for child in children for alias in child.step_aliases()},
                key=lambda item: item.encode("utf-8"),
            )
        )


def _slot_name(value: str) -> str:
    from cruxible_client.contracts.provider_operations import validate_slot_name

    return validate_slot_name(value)


class ReadNode(_StrictProcedureModel):
    """A read of accepted state: one named Query, or one ClaimType's Claims about a Subject.

    Exactly one of ``query`` and ``claim_type`` is set. A read runs to the end:
    it evaluates under the run's Procedure read ceiling, never the Query's
    interactive budgets, and refuses ``read_ceiling_exceeded`` rather than
    returning a clipped result.
    """

    kind: Literal["read"] = "read"
    node_id: str
    query: ArtifactNameRef | None = Field(default=None, exclude_if=lambda value: value is None)
    parameters: object = Field(default_factory=dict)
    claim_type: ArtifactNameRef | None = Field(default=None, exclude_if=lambda value: value is None)
    subject_kind: str | None = Field(default=None, exclude_if=lambda value: value is None)
    subject_id: object = Field(default=None, exclude_if=lambda value: value is None)
    cardinality: Literal["one", "all"] = "one"
    limit: int | None = Field(
        default=None, ge=1, le=2**31 - 1, exclude_if=lambda value: value is None
    )
    as_: str = Field(alias="as")
    next: str | None = None

    _parameters = field_validator("parameters", mode="before")(normalize_canonical)
    _subject = field_validator("subject_id", mode="before")(normalize_canonical)

    @model_validator(mode="after")
    def _one_selection(self) -> ReadNode:
        if (self.query is None) == (self.claim_type is None):
            raise ValueError("a read names exactly one of query and claim_type")
        if self.query is not None:
            if (
                self.subject_kind is not None
                or self.subject_id is not None
                or self.cardinality != "one"
                or self.limit is not None
            ):
                raise ValueError("a Query read takes parameters, not a Subject selection")
            return self
        if self.parameters != {}:
            raise ValueError("a Claim read selects a Subject; it takes no Query parameters")
        if self.subject_kind is None or self.subject_id is None:
            raise ValueError("a Claim read names its subject_kind and subject_id")
        if (self.cardinality == "all") != (self.limit is not None):
            raise ValueError("all() requires a positive limit; one() takes no limit")
        return self

    @property
    def reads_claims(self) -> bool:
        return self.claim_type is not None


class CallNode(_StrictProcedureModel):
    """A typed call: the slot names the signature; the Line names the operation.

    ``captures`` pins, for each ``capture<K>`` field of the slot's out Contract,
    the accepted CaptureContract the field's bytes are committed as (``K``
    itself, or any CaptureContract for the generic ``capture<workspace.file>``
    of the core-served read). A call with captures acquires: its capture
    fields are committed byte-exact as Captures and its other fields are
    normalized, and its value names each Capture by digest.
    """

    kind: Literal["call"] = "call"
    node_id: str
    slot: str
    effect_policy: ArtifactNameRef | None = None
    input: object
    captures: dict[str, ArtifactNameRef] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    as_: str = Field(alias="as")
    next: str | None = None

    _slot = field_validator("slot")(_slot_name)

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> object:
        return normalize_canonical(value)

    @field_validator("captures")
    @classmethod
    def _captures(cls, value: dict[str, ArtifactNameRef]) -> dict[str, ArtifactNameRef]:
        for name in value:
            _canonical_identifier(name, _ALIAS_RE, label="capture field")
        return dict(sorted(value.items(), key=lambda item: item[0].encode("utf-8")))

    @property
    def acquires(self) -> bool:
        return bool(self.captures)


#: The closed transform kinds: records in, records (or one value) out, three-valued.
TransformKind = Literal["shape", "filter", "join", "aggregate", "dedupe", "union", "derive"]
CLOSED_TRANSFORM_KINDS = frozenset(
    {"shape", "filter", "join", "aggregate", "dedupe", "union", "derive"}
)


def _closed_predicate(value: object) -> object:
    """A closed predicate from its call form or structured form; anything else refuses."""

    from cruxible_client.contracts.shapes import as_predicate

    if isinstance(value, BaseModel):
        return value
    try:
        return as_predicate(value)  # type: ignore[arg-type]
    except Exception as exc:
        raise ValueError(
            f"predicate_outside_closed_grammar: {exc}; a predicate is built only from "
            "eq/ne/lt/le/gt/ge, in/not_in, present/absent, all_of/any_of/not"
        ) from exc


class ShapeField(_StrictProcedureModel):
    """One shaped field: read ``from`` the current record (``$item.fields.<f>``,
    ``$item.key.<k>``), or a literal ``value``; ``as`` casts it explicitly."""

    from_: str | None = Field(default=None, alias="from", exclude_if=lambda value: value is None)
    value: PredicateScalar = Field(default=None, exclude_if=lambda value: value is None)
    as_: str | None = Field(default=None, alias="as", exclude_if=lambda value: value is None)

    @field_validator("from_")
    @classmethod
    def _from(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != "$item" or parts[1] not in {"fields", "key"}:
            raise ValueError("a shaped field reads $item.fields.<f> or $item.key.<k>")
        _canonical_identifier(parts[2], _ALIAS_RE, label="shaped field source")
        return value

    @field_validator("as_")
    @classmethod
    def _cast(cls, value: str | None) -> str | None:
        if value is not None:
            from cruxible_client.contracts.shapes import parse_type

            parse_type(value)
        return value

    @model_validator(mode="after")
    def _one_source(self) -> ShapeField:
        if (self.from_ is None) == (self.value is None):
            raise ValueError("a shaped field reads from the record or states one literal value")
        return self


class TransformShapeSpec(_StrictProcedureModel):
    tag: Literal["cruxible-transform-shape-spec-v1"] = "cruxible-transform-shape-spec-v1"
    input: object
    fields: dict[str, ShapeField] = Field(min_length=1)

    _input = field_validator("input", mode="before")(normalize_canonical)


class TransformFilterSpec(_StrictProcedureModel):
    """Keep the records whose closed predicate is ``true``; an ``unknown`` one taints the output."""

    tag: Literal["cruxible-transform-filter-spec-v1"] = "cruxible-transform-filter-spec-v1"
    input: object
    where: object

    _input = field_validator("input", mode="before")(normalize_canonical)
    _where = field_validator("where", mode="before")(_closed_predicate)


class TransformJoinSpec(_StrictProcedureModel):
    """Records of ``left`` whose ``left_field`` equals a ``right`` record's ``right_field``.

    ``inner`` keeps each matched pair, adding ``right_fields`` (output name ->
    right field); ``anti`` keeps the left records no right record matches.
    """

    tag: Literal["cruxible-transform-join-spec-v1"] = "cruxible-transform-join-spec-v1"
    left: object
    right: object
    left_field: str
    right_field: str
    mode: Literal["inner", "anti"] = "inner"
    right_fields: dict[str, str] = Field(default_factory=dict)

    _sides = field_validator("left", "right", mode="before")(normalize_canonical)


class TransformAggregateSpec(_StrictProcedureModel):
    """One value over the records: ``count``, ``any``/``all`` of ``where``, ``min``/``max`` of
    ``field``, or the ``first_by`` record ordered by ``field``."""

    tag: Literal["cruxible-transform-aggregate-spec-v1"] = "cruxible-transform-aggregate-spec-v1"
    input: object
    op: Literal["count", "any", "all", "min", "max", "first_by"]
    field: str | None = Field(default=None, exclude_if=lambda value: value is None)
    where: object = Field(default=None, exclude_if=lambda value: value is None)

    _input = field_validator("input", mode="before")(normalize_canonical)

    @field_validator("where", mode="before")
    @classmethod
    def _where(cls, value: object) -> object:
        return None if value is None else _closed_predicate(value)

    @model_validator(mode="after")
    def _operands(self) -> TransformAggregateSpec:
        if self.op in {"min", "max", "first_by"} and self.field is None:
            raise ValueError(f"aggregate {self.op} names its field")
        if self.op in {"any", "all"} and self.where is None:
            raise ValueError(f"aggregate {self.op} names its where predicate")
        return self


class TransformDedupeSpec(_StrictProcedureModel):
    tag: Literal["cruxible-transform-dedupe-spec-v1"] = "cruxible-transform-dedupe-spec-v1"
    input: object
    keys: tuple[str, ...] = Field(min_length=1)

    _input = field_validator("input", mode="before")(normalize_canonical)


class TransformUnionSpec(_StrictProcedureModel):
    tag: Literal["cruxible-transform-union-spec-v1"] = "cruxible-transform-union-spec-v1"
    inputs: tuple[object, ...] = Field(min_length=2)

    _inputs = field_validator("inputs", mode="before")(normalize_canonical)


class DeriveCase(_StrictProcedureModel):
    when: object
    value: PredicateScalar

    _when = field_validator("when", mode="before")(_closed_predicate)


class TransformDeriveSpec(_StrictProcedureModel):
    """``case [{when, value}] else value`` over named inputs (``$item.fields.<name>``).

    The first case whose ``when`` is ``true`` decides; a ``when`` that is
    ``unknown`` before any case decides makes the derived value ``unknown``.
    ``else`` is required.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )

    tag: Literal["cruxible-transform-derive-spec-v1"] = "cruxible-transform-derive-spec-v1"
    input: dict[str, object] = Field(min_length=1)
    cases: tuple[DeriveCase, ...] = Field(min_length=1)
    else_: PredicateScalar = Field(alias="else")

    _input = field_validator("input", mode="before")(normalize_canonical)

    @model_validator(mode="before")
    @classmethod
    def _else_required(cls, value: object) -> object:
        if isinstance(value, Mapping) and "else" not in value and "else_" not in value:
            raise ValueError("derive_else_required: a derive states its else value")
        return value


ProcedureTransformSpec = Annotated[
    TransformShapeSpec
    | TransformFilterSpec
    | TransformJoinSpec
    | TransformAggregateSpec
    | TransformDedupeSpec
    | TransformUnionSpec
    | TransformDeriveSpec,
    Field(discriminator="tag"),
]

_TRANSFORM_SPEC_TAGS: dict[TransformKind, str] = {
    "shape": "cruxible-transform-shape-spec-v1",
    "filter": "cruxible-transform-filter-spec-v1",
    "join": "cruxible-transform-join-spec-v1",
    "aggregate": "cruxible-transform-aggregate-spec-v1",
    "dedupe": "cruxible-transform-dedupe-spec-v1",
    "union": "cruxible-transform-union-spec-v1",
    "derive": "cruxible-transform-derive-spec-v1",
}


class TransformNode(_StrictProcedureModel):
    """A closed, deterministic transform: records in, records (or one value) out.

    Its records are typed as they flow (each field keeps its field result), its
    predicates are three-valued, and its output carries completeness.
    """

    kind: Literal["transform"] = "transform"
    node_id: str
    transform_kind: TransformKind
    spec: ProcedureTransformSpec
    as_: str = Field(alias="as")
    next: str | None = None

    @model_validator(mode="after")
    def _spec_matches_kind(self) -> "TransformNode":
        if self.spec.tag != _TRANSFORM_SPEC_TAGS[self.transform_kind]:
            raise ValueError("transform spec tag does not match transform_kind")
        return self


class GuardNode(_StrictProcedureModel):
    kind: Literal["guard"] = "guard"
    node_id: str
    predicate: GuardPredicate
    on_true: str | None = None
    on_false: str = "$abort"
    refusal_code: str | None = Field(default=None, exclude_if=lambda value: value is None)
    message: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("refusal_code")
    @classmethod
    def _refusal_code(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _canonical_identifier(value, _NAME_RE, label="guard refusal_code")
        )

    @model_validator(mode="after")
    def _abort_diagnostic(self) -> "GuardNode":
        if "$abort" in {self.on_true, self.on_false} and (
            self.refusal_code is None or self.message is None
        ):
            raise ValueError(f"guard {self.node_id!r}: abort requires refusal_code and message")
        return self


class ProjectNode(_StrictProcedureModel):
    """A typed record: ``fields`` resolved from the run, or an exact ``literal``.

    A ``literal`` record is data: dollar-prefixed strings in it are never
    references. A ``leaf`` project is a successful terminal that returns its
    own value, so each branch may return its own exact result.
    """

    kind: Literal["project"] = "project"
    node_id: str
    fields: object = Field(default=None, exclude_if=lambda value: value is None)
    literal: object = Field(default=None, exclude_if=lambda value: value is None)
    contract_out: ArtifactNameRef
    leaf: bool = False
    as_: str = Field(alias="as")
    next: str | None = None

    _fields = field_validator("fields", "literal", mode="before")(normalize_canonical)

    @model_validator(mode="after")
    def _one_source(self) -> ProjectNode:
        if (self.fields is None) == (self.literal is None):
            raise ValueError("a project declares exactly one of fields and literal")
        if self.leaf and self.next is not None:
            raise ValueError("a leaf project returns its value and has no successor")
        return self

    @property
    def template(self) -> object:
        """What the run resolves: the fields template, or nothing for a literal."""

        return {} if self.fields is None else self.fields


class RepeatBodyNode(_StrictProcedureModel):
    """One bounded repeat operation: a slotted call or a closed transform."""

    node_id: str
    operation: Literal["call", "transform"]
    transform_kind: TransformKind | None = None
    slot: str | None = Field(default=None, exclude_if=lambda value: value is None)
    effect_policy: ArtifactNameRef | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    spec: ProcedureTransformSpec | object
    as_: str = Field(alias="as")

    @field_validator("slot")
    @classmethod
    def _slot(cls, value: str | None) -> str | None:
        return None if value is None else _slot_name(value)

    @field_validator("spec", mode="before")
    @classmethod
    def _spec(cls, value: object) -> object:
        if isinstance(value, BaseModel):
            return value
        canonical = normalize_canonical(value)
        if isinstance(canonical, Mapping) and str(canonical.get("tag", "")).startswith(
            "cruxible-transform-"
        ):
            from pydantic import TypeAdapter

            return TypeAdapter(ProcedureTransformSpec).validate_python(canonical)
        return canonical

    @model_validator(mode="before")
    @classmethod
    def _retired_transform(cls, value: object) -> object:
        if isinstance(value, Mapping):
            refuse_retired_kinds([value], location="repeat body")
        return value

    @model_validator(mode="after")
    def _operation_shape(self) -> "RepeatBodyNode":
        if self.operation == "call":
            if self.slot is None:
                raise ValueError("a repeat call names its slot")
            if self.transform_kind is not None:
                raise ValueError("repeat calls cannot declare transform_kind")
            return self
        if self.slot is not None or self.effect_policy is not None:
            raise ValueError("repeat transform operations cannot declare a slot or effect policy")
        if self.transform_kind is None or not isinstance(self.spec, BaseModel):
            raise ValueError("repeat transform operations require a typed transform spec")
        if getattr(self.spec, "tag", None) != _TRANSFORM_SPEC_TAGS[self.transform_kind]:
            raise ValueError("repeat transform spec tag does not match transform_kind")
        return self


class RepeatNode(_StrictProcedureModel):
    kind: Literal["repeat"] = "repeat"
    node_id: str
    max_attempts: int = Field(ge=1, le=2**31 - 1)
    body: tuple[RepeatBodyNode, ...]
    until: GuardPredicate
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("body")
    @classmethod
    def _body(cls, value: tuple[RepeatBodyNode, ...]) -> tuple[RepeatBodyNode, ...]:
        ids = tuple(item.node_id for item in value)
        aliases = tuple(item.as_ for item in value)
        if not value or len(set(ids)) != len(ids) or len(set(aliases)) != len(aliases):
            raise ValueError("repeat body requires nonempty, unique node ids and aliases")
        return value


class CaptureEgressNode(_StrictProcedureModel):
    kind: Literal["emit_capture"] = "emit_capture"
    node_id: str
    capture_contract: ArtifactNameRef
    input: object
    result: object

    _input = field_validator("input", mode="before")(normalize_canonical)
    _result = field_validator("result", mode="before")(normalize_canonical)


class InboxEgressNode(_StrictProcedureModel):
    """Terminal output into the Line inbox: items sent, as they are, to intelligence.

    ``input`` is one item, or ``{"items": ...}`` to fan out over a list the run
    produced. Posting is inert: it never touches accepted state. ``max_items``
    caps what one run posts (``INBOX_RUN_ITEM_CAP_DEFAULT`` when absent); items
    past it are not posted and the run reports the truncation.
    """

    kind: Literal["post_inbox"] = "post_inbox"
    node_id: str
    input: object
    max_items: int | None = Field(
        default=None,
        ge=1,
        le=INBOX_RUN_ITEM_CAP_MAX,
        exclude_if=lambda value: value is None,
    )

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> object:
        return normalize_canonical(value)


class ProposalItemsFanOut(_StrictProcedureModel):
    """Candidates from data: each element of ``items`` is one Claim proposal item.

    ``items`` resolves at run time (typically ``$steps.<alias>.items``, a list a
    provider, Source or transform produced); every element becomes one item of
    the one proposal, with its own dependency closure and evidence, exactly as
    the capture and inbox terminals fan out ``{"items": ...}``.
    """

    items: object

    _items = field_validator("items", mode="before")(normalize_canonical)


class ProposeChangeSetNode(_StrictProcedureModel):
    """Terminal output into proposal receive; it has no activation field.

    ``candidate_templates`` is a fixed, non-empty list of item templates, or
    ``{"items": ...}`` to fan out over data (:class:`ProposalItemsFanOut`).
    """

    kind: Literal["propose_change_set"] = "propose_change_set"
    node_id: str
    candidate_templates: tuple[object, ...] | ProposalItemsFanOut
    claim_types: tuple[ArtifactNameRef, ...] = ()
    result: object

    _result = field_validator("result", mode="before")(normalize_canonical)

    @field_validator("candidate_templates", mode="before")
    @classmethod
    def _templates(cls, value: object) -> object:
        if isinstance(value, ProposalItemsFanOut):
            return value
        if isinstance(value, dict):
            if set(value) != {"items"}:
                raise ValueError("a propose_change_set fan-out is exactly {'items': <reference>}")
            return ProposalItemsFanOut(items=value["items"])
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("propose_change_set requires at least one candidate template")
        return tuple(normalize_canonical(item) for item in value)


class SettleChangeSetNode(ProposeChangeSetNode):
    """Terminal settle: the proposal terminal's Claims, settled under delegated authority.

    It carries no mandate: Core selects the one accepted settle ProcedureMandate
    that covers the change, evaluates its condition, and falls back as that
    mandate declares. Compiler revision 31.
    """

    kind: Literal["settle_change_set"] = "settle_change_set"  # type: ignore[assignment]


class HaltNode(_StrictProcedureModel):
    """A successful graph leaf that deliberately produces no result."""

    kind: Literal["halt"] = "halt"
    node_id: str
    reason: str | None = None
    message: str | None = Field(default=None, exclude_if=lambda value: value is None)


TERMINAL_REQUIRED_RUNGS = {
    "emit_capture": 0,
    "post_inbox": 1,
    "propose_change_set": 2,
    "settle_change_set": 3,
}
TERMINAL_NODE_KINDS = frozenset((*TERMINAL_REQUIRED_RUNGS, "halt"))


def is_terminal_node(node: object) -> bool:
    """Whether a node ends its path: an egress terminal, a halt, or a leaf project."""

    kind = node.get("kind") if isinstance(node, Mapping) else getattr(node, "kind", None)
    if kind in TERMINAL_NODE_KINDS:
        return True
    leaf = node.get("leaf") if isinstance(node, Mapping) else getattr(node, "leaf", False)
    return kind == "project" and leaf is True


#: Node kinds graph format 7 no longer has, each with what replaces it.
RETIRED_NODE_KINDS: dict[str, str] = {
    "source": "a 'call' whose slot's out Contract carries capture<K> fields (with 'captures')",
    "state_tap": "a 'read' naming its 'query'",
    "state_claim": "a 'read' naming its 'claim_type'",
    "exhaust_tap": "nothing: no run lane serves exhaust reads; read accepted state with 'read'",
    "return": "a 'project' with 'leaf: true'",
    "constant": "a 'project' with a 'literal' record",
}
#: Transform kinds graph format 7 no longer has, each with what replaces it.
RETIRED_TRANSFORM_KINDS: dict[str, str] = {
    "adapter": "a typed 'project', or a closed transform ('shape')",
    "shape_items": "the closed transform 'shape' (with 'as' casts)",
    "filter_items": "the closed transform 'filter' (a three-valued 'where')",
    "join_items": "the closed transform 'join' (inner or anti)",
    "aggregate_items": "the closed transform 'aggregate' (count, any, all, min, max, first_by)",
    "dedupe_items": "the closed transform 'dedupe'",
}


class RetiredNodeKind(ValueError):
    """A node or transform kind the current graph format no longer has."""

    code = "procedure_node_kind_retired"

    def __init__(self, kind: str, replacement: str, *, location: str) -> None:
        self.kind = kind
        self.replacement = replacement
        self.location = location
        super().__init__(
            f"procedure_node_kind_retired: {location} uses retired kind {kind!r}; use {replacement}"
        )


def refuse_retired_kinds(nodes: object, *, location: str = "Procedure node") -> None:
    """Refuse a retired node or transform kind by name, naming its replacement."""

    if not isinstance(nodes, list | tuple):
        return
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            continue
        where = f"{location} {node.get('node_id', node.get('id', index))!r}"
        kind = node.get("kind")
        if isinstance(kind, str) and kind in RETIRED_NODE_KINDS:
            raise RetiredNodeKind(kind, RETIRED_NODE_KINDS[kind], location=where)
        transform_kind = node.get("transform_kind")
        if isinstance(transform_kind, str) and transform_kind in RETIRED_TRANSFORM_KINDS:
            raise RetiredNodeKind(
                transform_kind, RETIRED_TRANSFORM_KINDS[transform_kind], location=where
            )
        body = node.get("body")
        if kind == "repeat" and isinstance(body, list | tuple):
            refuse_retired_kinds(body, location=f"{where} repeat body")


AuthorityVerb = Literal["observe", "propose", "settle"]
# Authored surfaces and results speak these verbs; the numbers are internal ordering.
AUTHORITY_RUNG: dict[str, Literal[1, 2, 3]] = {"observe": 1, "propose": 2, "settle": 3}
RUNG_AUTHORITY: dict[int, AuthorityVerb] = {1: "observe", 2: "propose", 3: "settle"}
#: What a served result names in place of an ordering value: a run capped below
#: observation may egress nothing at all.
EffectiveAuthority = Literal["none", "observe", "propose", "settle"]


def required_authority(rung: int) -> AuthorityVerb:
    """The verb a terminal's required rung serves as; capture (rung 0) observes too."""

    return RUNG_AUTHORITY[max(rung, 1)]


def authority_for_rung(rung: int) -> EffectiveAuthority:
    """The verb an effective rung serves as, or none below observation."""

    return "none" if rung < 0 else required_authority(rung)


def derived_terminal_capability(
    nodes: Iterable[object], *, child_rung: int = 0
) -> Literal[1, 2, 3]:
    """What a Procedure's own terminals (and any child it invokes) require it to do.

    Authors do not state a Procedure's capability: it is the highest level any
    terminal node or invoked child needs, and never below observe (1).
    """

    required = [child_rung]
    for node in nodes:
        kind = node.get("kind") if isinstance(node, Mapping) else getattr(node, "kind", None)
        if isinstance(kind, str):
            required.append(TERMINAL_REQUIRED_RUNGS.get(kind, 0))
    level = max(1, *required)
    if level > 3:
        raise ValueError("a Procedure terminal requires an unknown authority level")
    return cast(Literal[1, 2, 3], level)


class SelectNode(_StrictProcedureModel):
    """Join mutually exclusive record producers without guessing a winner."""

    kind: Literal["select"] = "select"
    node_id: str
    sources: tuple[str, ...]
    contract_out: ArtifactNameRef
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("sources")
    @classmethod
    def _sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) < 2 or len(set(value)) != len(value):
            raise ValueError("select requires at least two distinct producer aliases")
        for alias in value:
            _canonical_identifier(alias, _ALIAS_RE, label="select producer")
        return value


class InvokeNode(_StrictProcedureModel):
    """A call to a named accepted Procedure under the enclosing run's limits."""

    kind: Literal["invoke"] = "invoke"
    node_id: str
    procedure: ArtifactNameRef
    input: object
    as_: str = Field(alias="as")
    next: str | None = None

    _input = field_validator("input", mode="before")(normalize_canonical)


ProcedureNode = Annotated[
    InvokeNode
    | ReadNode
    | CallNode
    | ExtractNode
    | TransformNode
    | GuardNode
    | ProjectNode
    | RepeatNode
    | CaptureEgressNode
    | InboxEgressNode
    | ProposeChangeSetNode
    | SettleChangeSetNode
    | HaltNode
    | SelectNode,
    Field(discriminator="kind"),
]

#: Terminals a graph may end in; egress nodes are the ones that act outward.
EgressNode: TypeAlias = CaptureEgressNode | InboxEgressNode | ProposeChangeSetNode


__all__ = [
    "TransformUnionSpec",
    "TransformShapeSpec",
    "TransformJoinSpec",
    "TransformFilterSpec",
    "TransformDeriveSpec",
    "TransformDedupeSpec",
    "TransformAggregateSpec",
    "ShapeField",
    "DeriveCase",
    "CLOSED_TRANSFORM_KINDS",
    "AUTHORITY_RUNG",
    "AuthorityVerb",
    "CallNode",
    "CaptureEgressNode",
    "ComparisonOperator",
    "EffectiveAuthority",
    "EgressNode",
    "ExtractNode",
    "GuardNode",
    "GuardPredicate",
    "HaltNode",
    "InboxEgressNode",
    "InvokeNode",
    "PredicateOperand",
    "PredicateScalar",
    "ReadNode",
    "ProcedureNode",
    "ProcedureTransformSpec",
    "ProjectNode",
    "ProposalItemsFanOut",
    "ProposeChangeSetNode",
    "RETIRED_NODE_KINDS",
    "RETIRED_TRANSFORM_KINDS",
    "RUNG_AUTHORITY",
    "RetiredNodeKind",
    "RepeatBodyNode",
    "RepeatNode",
    "SelectNode",
    "SettleChangeSetNode",
    "TERMINAL_NODE_KINDS",
    "TERMINAL_REQUIRED_RUNGS",
    "TransformKind",
    "TransformNode",
    "authority_for_rung",
    "is_terminal_node",
    "derived_terminal_capability",
    "refuse_retired_kinds",
    "required_authority",
]
