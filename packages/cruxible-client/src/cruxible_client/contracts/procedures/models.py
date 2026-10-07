"""The Procedure graph grammar (graph format 6).

This packaged contract module owns the live Cruxible Procedure graph profile.
Its dependencies are exact Cruxible pins or, on a Blueprint only, interface-typed
slots; nothing here performs a mutable config or registry lookup.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, normalize_canonical
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.measurements import (
    ProcedureMeasurementDeclaration,
)
from cruxible_client.contracts.procedures.source_program import ProcedureSource
from cruxible_client.contracts.query.grammar import QueryBudgets

_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class _StrictProcedureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _canonical_identifier(value: str, pattern: re.Pattern[str], *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be a canonical lowercase identifier")
    return value


class ProcedurePinSlot(_StrictProcedureModel):
    """One interface-typed binding point; it never chooses an implementation."""

    tag: Literal["playbill-procedure-pin-slot-v1"] = "playbill-procedure-pin-slot-v1"
    slot_name: str
    pin_role: str
    artifact_kind: str
    interface_digest: str

    @field_validator("slot_name")
    @classmethod
    def _slot_name(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="Procedure slot_name")

    @field_validator("pin_role")
    @classmethod
    def _pin_role(cls, value: str) -> str:
        return _canonical_identifier(value, _ROLE_RE, label="Procedure slot pin_role")

    @field_validator("artifact_kind")
    @classmethod
    def _artifact_kind(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value or not re.fullmatch(
            r"^[A-Z][A-Za-z0-9_.-]{0,63}$", value
        ):
            raise ValueError("Procedure slot artifact_kind must be a canonical artifact kind")
        return value

    @field_validator("interface_digest")
    @classmethod
    def _interface_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class ProcedurePinSlotRef(_StrictProcedureModel):
    tag: Literal["playbill-procedure-pin-slot-ref-v1"] = "playbill-procedure-pin-slot-ref-v1"
    slot_name: str

    @field_validator("slot_name")
    @classmethod
    def _slot_name(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="Procedure slot reference")


ProcedurePinBinding = ArtifactPin | ProcedurePinSlotRef


class ProcedureBudget(_StrictProcedureModel):
    tag: Literal["playbill-procedure-budget-v1"] = "playbill-procedure-budget-v1"
    wall_clock: CanonicalDuration
    max_provider_calls: int = Field(ge=0, le=1_000_000)
    max_capture_bytes: int = Field(ge=0, le=2**63 - 1)
    max_result_bytes: int | None = Field(default=None, ge=1, exclude_if=lambda v: v is None)
    max_items: int | None = Field(
        default=None,
        ge=1,
        le=2**31 - 1,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _nonzero_wall_clock(self) -> "ProcedureBudget":
        if self.wall_clock.microseconds == 0:
            raise ValueError("Procedure wall-clock budget must be nonzero")
        return self


class ProcedureHardCaps(_StrictProcedureModel):
    tag: Literal["playbill-procedure-hard-caps-v1"] = "playbill-procedure-hard-caps-v1"
    max_wall_clock: CanonicalDuration
    max_provider_calls: int = Field(ge=0, le=1_000_000)
    max_capture_bytes: int = Field(ge=0, le=2**63 - 1)
    max_result_bytes: int | None = Field(default=None, ge=1, exclude_if=lambda v: v is None)
    max_items: int = Field(ge=1, le=2**31 - 1)
    max_repeat_attempts: int = Field(ge=1, le=2**31 - 1)

    @model_validator(mode="after")
    def _nonzero_wall_clock(self) -> "ProcedureHardCaps":
        if self.max_wall_clock.microseconds == 0:
            raise ValueError("Procedure hard-cap wall clock must be nonzero")
        return self


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
            "literal": (self.value is not None, False, False, False),
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
    query: ProcedurePinBinding
    parameters: object = Field(default_factory=dict)
    as_: str = Field(alias="as")
    next: str | None = None
    view: Literal["typed_query"] = "typed_query"
    budgets: QueryBudgets | None = None

    @field_validator("parameters", mode="before")
    @classmethod
    def _parameters(cls, value: object) -> object:
        return normalize_canonical(value)


def _validate_explicit_provider_binding(
    provider: ProcedurePinBinding,
    implementation_digest: str | None,
) -> None:
    if isinstance(provider, ArtifactPin):
        if implementation_digest is None:
            raise ValueError("direct Provider bindings require implementation_digest")
    elif implementation_digest is not None:
        raise ValueError("slot Provider bindings prohibit implementation_digest")


class SourceNode(_StrictProcedureModel):
    kind: Literal["source"] = "source"
    node_id: str
    capture_contract: ProcedurePinBinding
    provider: ProcedurePinBinding
    interface: ArtifactPin
    interface_digest: str
    implementation_digest: str | None = None
    request: object
    as_: str = Field(alias="as")
    next: str | None = None

    _digests = field_validator("interface_digest", "implementation_digest")(
        lambda value: value if value is None else ArtifactDigest.from_tagged(value).tagged
    )

    @field_validator("request", mode="before")
    @classmethod
    def _request(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _provider_binding(self) -> "SourceNode":
        _validate_explicit_provider_binding(self.provider, self.implementation_digest)
        return self


class ExhaustTapNode(_StrictProcedureModel):
    kind: Literal["exhaust_tap"] = "exhaust_tap"
    node_id: str
    reducer_or_query: ProcedurePinBinding
    journal_identity: str
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("journal_identity")
    @classmethod
    def _journal(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="exhaust journal identity")


class CallNode(_StrictProcedureModel):
    """A contracted call; Provider names its implementation, not a node category."""

    kind: Literal["call"] = "call"
    node_id: str
    provider: ProcedurePinBinding
    interface: ArtifactPin
    interface_digest: str
    implementation_digest: str | None = None
    contract_in: ProcedurePinBinding
    contract_out: ProcedurePinBinding
    effect_policy: ProcedurePinBinding | None = None
    input: object
    as_: str = Field(alias="as")
    next: str | None = None

    _digests = field_validator("interface_digest", "implementation_digest")(
        lambda value: value if value is None else ArtifactDigest.from_tagged(value).tagged
    )

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _provider_binding(self) -> "CallNode":
        _validate_explicit_provider_binding(self.provider, self.implementation_digest)
        return self


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
    contract_in: ProcedurePinBinding
    contract_out: ProcedurePinBinding
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
    refusal_code: str
    message: str

    @field_validator("refusal_code")
    @classmethod
    def _refusal_code(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="guard refusal_code")


class ProjectNode(_StrictProcedureModel):
    kind: Literal["project"] = "project"
    node_id: str
    fields: object
    contract_out: ProcedurePinBinding
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("fields", mode="before")
    @classmethod
    def _fields(cls, value: object) -> object:
        return normalize_canonical(value)


class RepeatBodyNode(_StrictProcedureModel):
    """One bounded repeat operation with occurrence-local explicit Provider pins."""

    node_id: str
    operation: Literal["call", "transform"]
    transform_kind: TransformKind | None = None
    provider: ProcedurePinBinding | None = None
    interface: ArtifactPin | None = None
    interface_digest: str | None = None
    implementation_digest: str | None = None
    contract_in: ProcedurePinBinding
    contract_out: ProcedurePinBinding
    effect_policy: ProcedurePinBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    spec: ProcedureTransformSpec | object
    as_: str = Field(alias="as")

    _digests = field_validator("interface_digest", "implementation_digest")(
        lambda value: value if value is None else ArtifactDigest.from_tagged(value).tagged
    )

    @field_validator("spec", mode="before")
    @classmethod
    def _spec(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _operation_shape(self) -> "RepeatBodyNode":
        if self.operation == "call":
            if self.provider is None or self.interface is None or self.interface_digest is None:
                raise ValueError(
                    "repeat provider operations require provider and interface pin/digest"
                )
            if self.transform_kind is not None:
                raise ValueError("repeat provider operations cannot declare transform_kind")
            _validate_explicit_provider_binding(self.provider, self.implementation_digest)
            return self
        if any(
            value is not None
            for value in (
                self.provider,
                self.interface,
                self.interface_digest,
                self.implementation_digest,
                self.effect_policy,
            )
        ):
            raise ValueError("repeat transform operations cannot declare Provider or effect pins")
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
    capture_contract: ProcedurePinBinding
    input: object
    result: object

    _input = field_validator("input", mode="before")(normalize_canonical)
    _result = field_validator("result", mode="before")(normalize_canonical)


class InboxEgressNode(_StrictProcedureModel):
    kind: Literal["post_inbox"] = "post_inbox"
    node_id: str
    input: object

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
    claim_types: tuple[ArtifactPin, ...] = ()
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
    contract_out: ProcedurePinBinding
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
    """A bounded field read using an exact ClaimType and named Subject."""

    kind: Literal["state_claim"] = "state_claim"
    node_id: str
    claim_type: ArtifactPin
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
    """A call to an exact accepted Procedure under the enclosing run's limits."""

    kind: Literal["invoke"] = "invoke"
    node_id: str
    procedure: ArtifactPin
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


class ProcedureDefinition(_StrictProcedureModel):
    """The one Procedure graph format (6): explicit value joins and typed return paths.

    Slots (``pin_slots`` plus slot references in node pins) are interface-typed
    binding points. Only a Blueprint may leave them open; an accepted Procedure
    pins every Provider exactly.
    """

    graph_format: Literal[6] = 6
    name: str
    description: str | None = None
    contract_in: ProcedurePinBinding
    contract_out: ProcedurePinBinding
    parameter_contract: ProcedurePinBinding | None = None
    nodes: tuple[ProcedureNode, ...]
    returns: str | None = None
    pin_slots: tuple[ProcedurePinSlot, ...] = ()
    measurements: tuple[ProcedureMeasurementDeclaration, ...] = ()
    budget: ProcedureBudget
    hard_caps: ProcedureHardCaps
    terminal_capability: Literal[1, 2, 3]
    annotations: object = Field(default_factory=dict)
    source: ProcedureSource | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="Procedure name")

    @field_validator("returns")
    @classmethod
    def _returns(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _canonical_identifier(value, _ALIAS_RE, label="Procedure returns")
        )

    @field_validator("pin_slots")
    @classmethod
    def _slots(cls, value: tuple[ProcedurePinSlot, ...]) -> tuple[ProcedurePinSlot, ...]:
        names = tuple(item.slot_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Procedure pin slots must be sorted and unique by slot_name")
        return value

    @field_validator("measurements")
    @classmethod
    def _measurements(
        cls,
        value: tuple[ProcedureMeasurementDeclaration, ...],
    ) -> tuple[ProcedureMeasurementDeclaration, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("M3: Procedure measurements must be sorted and unique by name")
        return value

    @field_validator("annotations", mode="before")
    @classmethod
    def _annotations(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _basic_shape(self) -> "ProcedureDefinition":
        if not self.nodes:
            raise ValueError("Procedure definition requires at least one node")
        node_ids = tuple(node.node_id for node in self.nodes)
        aliases = tuple(
            node.as_ for node in self.nodes if hasattr(node, "as_") and node.as_ is not None
        )
        for node_id in node_ids:
            _canonical_identifier(node_id, _NODE_ID_RE, label="Procedure node_id")
        for alias in aliases:
            _canonical_identifier(alias, _ALIAS_RE, label="Procedure output alias")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("Procedure node ids must be unique")
        if len(set(aliases)) != len(aliases):
            raise ValueError("Procedure output aliases must be unique")
        # Source-v2 returns through explicit terminal nodes, never one global
        # alias; source-v1 retains its display-only alias.
        if (
            self.source is not None
            and self.source.rules == "cruxible.procedure-source.v2"
            and self.returns is not None
        ):
            raise ValueError("Source-v2 uses explicit return paths, not a return alias")
        if self.budget.wall_clock.microseconds > self.hard_caps.max_wall_clock.microseconds:
            raise ValueError("Procedure budget exceeds its wall-clock hard cap")
        if self.budget.max_provider_calls > self.hard_caps.max_provider_calls:
            raise ValueError("Procedure budget exceeds its provider-call hard cap")
        if (
            self.budget.max_result_bytes is not None
            and self.hard_caps.max_result_bytes is not None
            and self.budget.max_result_bytes > self.hard_caps.max_result_bytes
        ):
            raise ValueError("Procedure result budget exceeds hard caps")
        if self.budget.max_capture_bytes > self.hard_caps.max_capture_bytes:
            raise ValueError("Procedure budget exceeds its capture-byte hard cap")
        if self.budget.max_items is not None and self.budget.max_items > self.hard_caps.max_items:
            raise ValueError("Procedure budget exceeds its item hard cap")
        if any(
            isinstance(node, RepeatNode) and node.max_attempts > self.hard_caps.max_repeat_attempts
            for node in self.nodes
        ):
            raise ValueError("Procedure repeat exceeds its repeat-attempt hard cap")
        # Import lazily so the model grammar does not depend on static-analysis
        # modules while its own classes are still being defined.
        from cruxible_client.contracts.procedures.graph import analyze_procedure
        from cruxible_client.contracts.procedures.pin_expectations import (
            validate_procedure_pin_expectations,
        )

        validate_procedure_pin_expectations(self)
        graph = analyze_procedure(self)
        for measurement in self.measurements:
            if measurement.subject_grain == "procedure_unit":
                continue
            measurement_node_id = measurement.node_id
            if measurement_node_id is None:  # pragma: no cover - declaration invariant
                raise ValueError("M1: non-unit measurement requires node_id")
            if measurement_node_id not in graph.kinds:
                raise ValueError(
                    f"M1: measurement node_id {measurement_node_id!r} does not name "
                    "a node in this definition"
                )
            if measurement.subject_grain != "arm":
                continue
            from_node_id = measurement.from_node_id
            arm_label = measurement.arm_label
            if from_node_id is None or arm_label is None:  # pragma: no cover
                raise ValueError("M2: arm measurement requires complete arm coordinates")
            successor = graph.edges.get(from_node_id, {}).get(arm_label)
            if successor != measurement_node_id:
                raise ValueError(
                    f"M2: measurement arm {from_node_id!r} "
                    f"{arm_label!r} does not target {measurement_node_id!r}"
                )
        return self

    @property
    def open_slots(self) -> tuple[str, ...]:
        """Slot names some node pin still references; empty for a runnable Procedure."""

        return tuple(
            sorted(
                {
                    binding.slot_name
                    for binding in iter_pin_bindings(self)
                    if isinstance(binding, ProcedurePinSlotRef)
                },
                key=lambda item: item.encode("utf-8"),
            )
        )


def iter_pin_bindings(value: object) -> tuple[ProcedurePinBinding, ...]:
    """Return every exact pin or slot reference nested in a Procedure model."""

    found: list[ProcedurePinBinding] = []

    def visit(item: object) -> None:
        if isinstance(item, ArtifactPin | ProcedurePinSlotRef):
            found.append(item)
            return
        if isinstance(item, BaseModel):
            for field_name in item.__class__.model_fields:
                visit(getattr(item, field_name))
            return
        if isinstance(item, tuple | list):
            for member in item:
                visit(member)
            return
        if isinstance(item, dict):
            for member in item.values():
                visit(member)

    visit(value)
    return tuple(found)


__all__ = [
    "AUTHORITY_RUNG",
    "AuthorityVerb",
    "CallNode",
    "CaptureEgressNode",
    "ClaimTapNode",
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
    "ProcedureBudget",
    "ProcedureDefinition",
    "ProcedureHardCaps",
    "ProcedureMeasurementDeclaration",
    "ProcedureNode",
    "ProcedurePinBinding",
    "ProcedurePinSlot",
    "ProcedurePinSlotRef",
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
    "iter_pin_bindings",
    "required_authority",
]
