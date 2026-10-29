"""The Procedure graph node models, predicates and terminal authority table.

Moved out of :mod:`~cruxible_client.contracts.procedures.models`, which keeps
``ProcedureDefinition`` and re-exports every name here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import BaseModel, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactNameRef
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.line_inbox import INBOX_RUN_ITEM_CAP_MAX
from cruxible_client.contracts.procedures.model_base import (
    _ALIAS_RE,
    _NAME_RE,
    _canonical_identifier,
    _StrictProcedureModel,
)
from cruxible_client.contracts.query.grammar import QueryBudgets

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


class StateTapNode(_StrictProcedureModel):
    """Request-bound query with a typed view over the retained query result."""

    kind: Literal["state_tap"] = "state_tap"
    node_id: str
    query: ArtifactNameRef
    parameters: object = Field(default_factory=dict)
    as_: str = Field(alias="as")
    next: str | None = None
    view: Literal["typed_query"] = "typed_query"
    budgets: QueryBudgets | None = None

    @field_validator("parameters", mode="before")
    @classmethod
    def _parameters(cls, value: object) -> object:
        return normalize_canonical(value)


def _slot_name(value: str) -> str:
    from cruxible_client.contracts.provider_operations import validate_slot_name

    return validate_slot_name(value)


class SourceNode(_StrictProcedureModel):
    """An acquisition: the slot's bound operation returns material core captures."""

    kind: Literal["source"] = "source"
    node_id: str
    capture_contract: ArtifactNameRef
    slot: str
    request: object
    as_: str = Field(alias="as")
    next: str | None = None

    _slot = field_validator("slot")(_slot_name)

    @field_validator("request", mode="before")
    @classmethod
    def _request(cls, value: object) -> object:
        return normalize_canonical(value)


class ExhaustTapNode(_StrictProcedureModel):
    kind: Literal["exhaust_tap"] = "exhaust_tap"
    node_id: str
    reducer_or_query: ArtifactNameRef
    journal_identity: str
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("journal_identity")
    @classmethod
    def _journal(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="exhaust journal identity")


class CallNode(_StrictProcedureModel):
    """A typed call: the slot names the signature; the Line names the operation."""

    kind: Literal["call"] = "call"
    node_id: str
    slot: str
    effect_policy: ArtifactNameRef | None = None
    input: object
    as_: str = Field(alias="as")
    next: str | None = None

    _slot = field_validator("slot")(_slot_name)

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> object:
        return normalize_canonical(value)


TransformKind = Literal[
    "shape_items",
    "join_items",
    "filter_items",
    "aggregate_items",
    "dedupe_items",
    "adapter",
]


class TransformAdapterSpec(_StrictProcedureModel):
    tag: Literal["playbill-transform-adapter-spec-v1"] = "playbill-transform-adapter-spec-v1"
    value: object

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)


class TransformShapeItemsSpec(_StrictProcedureModel):
    tag: Literal["playbill-transform-shape-items-spec-v1"] = (
        "playbill-transform-shape-items-spec-v1"
    )
    items: object
    fields: dict[str, object]
    include_input: bool = False

    @field_validator("items", "fields", mode="before")
    @classmethod
    def _canonical(cls, value: object) -> object:
        return normalize_canonical(value)


class TransformFilterItemsSpec(_StrictProcedureModel):
    tag: Literal["playbill-transform-filter-items-spec-v1"] = (
        "playbill-transform-filter-items-spec-v1"
    )
    items: object
    where: dict[str, object]

    @field_validator("items", "where", mode="before")
    @classmethod
    def _canonical(cls, value: object) -> object:
        return normalize_canonical(value)


class TransformDedupeItemsSpec(_StrictProcedureModel):
    tag: Literal["playbill-transform-dedupe-items-spec-v1"] = (
        "playbill-transform-dedupe-items-spec-v1"
    )
    items: object
    keys: tuple[str, ...]

    @field_validator("items", mode="before")
    @classmethod
    def _items(cls, value: object) -> object:
        return normalize_canonical(value)


class TransformJoinItemsSpec(_StrictProcedureModel):
    tag: Literal["playbill-transform-join-items-spec-v1"] = "playbill-transform-join-items-spec-v1"
    left_items: object
    right_items: object
    left_key: str
    right_key: str
    fields: dict[str, object]

    @field_validator("left_items", "right_items", "fields", mode="before")
    @classmethod
    def _canonical(cls, value: object) -> object:
        return normalize_canonical(value)


class TransformAggregateItemsSpec(_StrictProcedureModel):
    tag: Literal["playbill-transform-aggregate-items-spec-v1"] = (
        "playbill-transform-aggregate-items-spec-v1"
    )
    items: object

    @field_validator("items", mode="before")
    @classmethod
    def _items(cls, value: object) -> object:
        return normalize_canonical(value)


ProcedureTransformSpec = Annotated[
    TransformAdapterSpec
    | TransformShapeItemsSpec
    | TransformFilterItemsSpec
    | TransformDedupeItemsSpec
    | TransformJoinItemsSpec
    | TransformAggregateItemsSpec,
    Field(discriminator="tag"),
]

_TRANSFORM_SPEC_TAGS: dict[TransformKind, str] = {
    "adapter": "playbill-transform-adapter-spec-v1",
    "shape_items": "playbill-transform-shape-items-spec-v1",
    "filter_items": "playbill-transform-filter-items-spec-v1",
    "dedupe_items": "playbill-transform-dedupe-items-spec-v1",
    "join_items": "playbill-transform-join-items-spec-v1",
    "aggregate_items": "playbill-transform-aggregate-items-spec-v1",
}


class TransformNode(_StrictProcedureModel):
    kind: Literal["transform"] = "transform"
    node_id: str
    transform_kind: TransformKind
    contract_in: ArtifactNameRef
    contract_out: ArtifactNameRef
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
    kind: Literal["project"] = "project"
    node_id: str
    fields: object
    contract_out: ArtifactNameRef
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("fields", mode="before")
    @classmethod
    def _fields(cls, value: object) -> object:
        return normalize_canonical(value)


class RepeatBodyNode(_StrictProcedureModel):
    """One bounded repeat operation: a slotted call or a typed transform."""

    node_id: str
    operation: Literal["call", "transform"]
    transform_kind: TransformKind | None = None
    slot: str | None = Field(default=None, exclude_if=lambda value: value is None)
    contract_in: ArtifactNameRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    contract_out: ArtifactNameRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
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
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _operation_shape(self) -> "RepeatBodyNode":
        if self.operation == "call":
            if self.slot is None:
                raise ValueError("a repeat call names its slot")
            if self.transform_kind is not None:
                raise ValueError("repeat calls cannot declare transform_kind")
            if self.contract_in is not None or self.contract_out is not None:
                raise ValueError("a repeat call's I/O is its slot signature")
            return self
        if self.contract_in is None or self.contract_out is None:
            raise ValueError("repeat transforms require input and output contracts")
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
TERMINAL_NODE_KINDS = frozenset((*TERMINAL_REQUIRED_RUNGS, "halt", "return"))


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


class ReturnNode(ProjectNode):
    """A typed successful leaf; each branch may return its own exact value."""

    kind: Literal["return"] = "return"  # type: ignore[assignment]


class ConstantNode(ProjectNode):
    """Exact literal record; dollar-prefixed strings are data, never references."""

    kind: Literal["constant"] = "constant"  # type: ignore[assignment]


class ClaimTapNode(_StrictProcedureModel):
    """A bounded field read of a named ClaimType and Subject."""

    kind: Literal["state_claim"] = "state_claim"
    node_id: str
    claim_type: ArtifactNameRef
    subject_kind: str
    subject_id: object
    cardinality: Literal["one", "all"] = "one"
    limit: int | None = Field(default=None, ge=1, le=2**31 - 1)
    as_: str = Field(alias="as")
    next: str | None = None

    _subject = field_validator("subject_id", mode="before")(normalize_canonical)

    @model_validator(mode="after")
    def _bounded_selection(self) -> ClaimTapNode:
        if (self.cardinality == "all") != (self.limit is not None):
            raise ValueError("all() requires a positive limit; one() takes no limit")
        return self


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
    | ClaimTapNode
    | StateTapNode
    | SourceNode
    | ExhaustTapNode
    | CallNode
    | TransformNode
    | GuardNode
    | ProjectNode
    | RepeatNode
    | CaptureEgressNode
    | InboxEgressNode
    | ProposeChangeSetNode
    | SettleChangeSetNode
    | HaltNode
    | SelectNode
    | ReturnNode
    | ConstantNode,
    Field(discriminator="kind"),
]

#: Terminals a graph may end in; egress nodes are the ones that act outward.
EgressNode: TypeAlias = CaptureEgressNode | InboxEgressNode | ProposeChangeSetNode


__all__ = [
    "AUTHORITY_RUNG",
    "AuthorityVerb",
    "CallNode",
    "CaptureEgressNode",
    "ClaimTapNode",
    "ComparisonOperator",
    "ConstantNode",
    "EffectiveAuthority",
    "EgressNode",
    "ExhaustTapNode",
    "GuardNode",
    "GuardPredicate",
    "HaltNode",
    "InboxEgressNode",
    "InvokeNode",
    "PredicateOperand",
    "PredicateScalar",
    "ProcedureNode",
    "ProcedureTransformSpec",
    "ProjectNode",
    "ProposalItemsFanOut",
    "ProposeChangeSetNode",
    "RUNG_AUTHORITY",
    "RepeatBodyNode",
    "RepeatNode",
    "ReturnNode",
    "SelectNode",
    "SettleChangeSetNode",
    "SourceNode",
    "StateTapNode",
    "TERMINAL_NODE_KINDS",
    "TERMINAL_REQUIRED_RUNGS",
    "TransformAdapterSpec",
    "TransformAggregateItemsSpec",
    "TransformDedupeItemsSpec",
    "TransformFilterItemsSpec",
    "TransformJoinItemsSpec",
    "TransformKind",
    "TransformNode",
    "TransformShapeItemsSpec",
    "authority_for_rung",
    "derived_terminal_capability",
    "required_authority",
]
