"""Frozen graph-format-v3 Procedure grammar.

The historical graph-format-v1/v2 readers live in ``cruxible_core.procedure``
and are deliberately not imported here.  V3 is a new, explicitly tagged wire
format whose dependencies are exact Playbill pins or interface-typed LineSpec
slots.  Nothing in this module performs a mutable config or registry lookup.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.artifacts import ArtifactPin
from cruxible_core.playbill.canonical import ArtifactDigest, normalize_canonical
from cruxible_core.playbill.captures import CanonicalDurationV1
from cruxible_core.playbill.procedures.measurements import (
    ProcedureMeasurementDeclarationV1,
)

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


class ProcedurePinSlotV1(_StrictProcedureModel):
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


class ProcedurePinSlotRefV1(_StrictProcedureModel):
    tag: Literal["playbill-procedure-pin-slot-ref-v1"] = "playbill-procedure-pin-slot-ref-v1"
    slot_name: str

    @field_validator("slot_name")
    @classmethod
    def _slot_name(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="Procedure slot reference")


ProcedurePinBindingV1 = ArtifactPin | ProcedurePinSlotRefV1


class ProcedureBudgetV3(_StrictProcedureModel):
    tag: Literal["playbill-procedure-budget-v1"] = "playbill-procedure-budget-v1"
    wall_clock: CanonicalDurationV1
    max_provider_calls: int = Field(ge=0, le=1_000_000)
    max_capture_bytes: int = Field(ge=0, le=2**63 - 1)
    max_items: int = Field(ge=1, le=2**31 - 1)

    @model_validator(mode="after")
    def _nonzero_wall_clock(self) -> "ProcedureBudgetV3":
        if self.wall_clock.microseconds == 0:
            raise ValueError("Procedure wall-clock budget must be nonzero")
        return self


class ProcedureHardCapsV3(_StrictProcedureModel):
    tag: Literal["playbill-procedure-hard-caps-v1"] = "playbill-procedure-hard-caps-v1"
    max_wall_clock: CanonicalDurationV1
    max_provider_calls: int = Field(ge=0, le=1_000_000)
    max_capture_bytes: int = Field(ge=0, le=2**63 - 1)
    max_items: int = Field(ge=1, le=2**31 - 1)
    max_repeat_attempts: int = Field(ge=1, le=25)

    @model_validator(mode="after")
    def _nonzero_wall_clock(self) -> "ProcedureHardCapsV3":
        if self.max_wall_clock.microseconds == 0:
            raise ValueError("Procedure hard-cap wall clock must be nonzero")
        return self


PredicateScalarV1 = None | bool | int | str


class PredicateOperandV1(_StrictProcedureModel):
    """One operand in the closed v3 predicate grammar."""

    tag: Literal["playbill-predicate-operand-v1"] = "playbill-predicate-operand-v1"
    kind: Literal["literal", "input", "step", "parameter", "count", "exists", "truncated"]
    value: PredicateScalarV1 = None
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
    def _closed_shape(self) -> "PredicateOperandV1":
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


ComparisonOperatorV1 = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "before", "on_or_before", "after", "on_or_after"
]


class GuardPredicateV1(_StrictProcedureModel):
    """Closed predicate: exactly one comparison or connective."""

    tag: Literal["playbill-guard-predicate-v1"] = "playbill-guard-predicate-v1"
    left: PredicateOperandV1 | None = None
    operator: ComparisonOperatorV1 | None = None
    right: PredicateOperandV1 | None = None
    all_of: tuple[GuardPredicateV1, ...] | None = None
    any_of: tuple[GuardPredicateV1, ...] | None = None
    not_of: GuardPredicateV1 | None = None

    @model_validator(mode="after")
    def _one_production(self) -> "GuardPredicateV1":
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


class StateTapNodeV3(_StrictProcedureModel):
    kind: Literal["state_tap"] = "state_tap"
    node_id: str
    query: ProcedurePinBindingV1
    parameters: object = Field(default_factory=dict)
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("parameters", mode="before")
    @classmethod
    def _parameters(cls, value: object) -> object:
        return normalize_canonical(value)


class SourceNodeV3(_StrictProcedureModel):
    kind: Literal["source"] = "source"
    node_id: str
    capture_contract: ProcedurePinBindingV1
    provider: ProcedurePinBindingV1
    request: object
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("request", mode="before")
    @classmethod
    def _request(cls, value: object) -> object:
        return normalize_canonical(value)


class ExhaustTapNodeV3(_StrictProcedureModel):
    kind: Literal["exhaust_tap"] = "exhaust_tap"
    node_id: str
    reducer_or_query: ProcedurePinBindingV1
    journal_identity: str
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("journal_identity")
    @classmethod
    def _journal(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="exhaust journal identity")


class ProviderNodeV3(_StrictProcedureModel):
    kind: Literal["provider"] = "provider"
    node_id: str
    provider: ProcedurePinBindingV1
    contract_in: ProcedurePinBindingV1
    contract_out: ProcedurePinBindingV1
    environment: ProcedurePinBindingV1
    effect_policy: ProcedurePinBindingV1 | None = None
    input: object
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> object:
        return normalize_canonical(value)


class TransformNodeV3(_StrictProcedureModel):
    kind: Literal["transform"] = "transform"
    node_id: str
    transform_kind: Literal[
        "shape_items", "join_items", "filter_items", "aggregate_items", "dedupe_items", "adapter"
    ]
    contract_in: ProcedurePinBindingV1
    contract_out: ProcedurePinBindingV1
    spec: object
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("spec", mode="before")
    @classmethod
    def _spec(cls, value: object) -> object:
        return normalize_canonical(value)


class GuardNodeV3(_StrictProcedureModel):
    kind: Literal["guard"] = "guard"
    node_id: str
    predicate: GuardPredicateV1
    on_true: str | None = None
    on_false: str = "$abort"
    refusal_code: str
    message: str

    @field_validator("refusal_code")
    @classmethod
    def _refusal_code(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="guard refusal_code")


class ProjectNodeV3(_StrictProcedureModel):
    kind: Literal["project"] = "project"
    node_id: str
    fields: object
    contract_out: ProcedurePinBindingV1
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("fields", mode="before")
    @classmethod
    def _fields(cls, value: object) -> object:
        return normalize_canonical(value)


class RepeatBodyNodeV3(_StrictProcedureModel):
    """One deterministic nested operation in a bounded repeat container."""

    node_id: str
    operation: Literal["provider", "transform"]
    provider: ProcedurePinBindingV1 | None = None
    contract_in: ProcedurePinBindingV1
    contract_out: ProcedurePinBindingV1
    environment: ProcedurePinBindingV1 | None = None
    spec: object
    as_: str = Field(alias="as")

    @field_validator("spec", mode="before")
    @classmethod
    def _spec(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _operation_shape(self) -> "RepeatBodyNodeV3":
        provider_fields = self.provider is not None and self.environment is not None
        if (self.operation == "provider") != provider_fields:
            raise ValueError("repeat provider operations require provider and environment pins")
        return self


class RepeatNodeV3(_StrictProcedureModel):
    kind: Literal["repeat"] = "repeat"
    node_id: str
    max_attempts: int = Field(ge=1, le=25)
    body: tuple[RepeatBodyNodeV3, ...]
    until: GuardPredicateV1
    as_: str = Field(alias="as")
    next: str | None = None

    @field_validator("body")
    @classmethod
    def _body(cls, value: tuple[RepeatBodyNodeV3, ...]) -> tuple[RepeatBodyNodeV3, ...]:
        ids = tuple(item.node_id for item in value)
        aliases = tuple(item.as_ for item in value)
        if not value or len(set(ids)) != len(ids) or len(set(aliases)) != len(aliases):
            raise ValueError("repeat body requires nonempty, unique node ids and aliases")
        return value


class CaptureEgressNodeV3(_StrictProcedureModel):
    kind: Literal["emit_capture"] = "emit_capture"
    node_id: str
    capture_contract: ProcedurePinBindingV1
    input: object

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> object:
        return normalize_canonical(value)


class InboxEgressNodeV3(_StrictProcedureModel):
    kind: Literal["post_inbox"] = "post_inbox"
    node_id: str
    input: object

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> object:
        return normalize_canonical(value)


class ProposeChangeSetNodeV3(_StrictProcedureModel):
    """Terminal rung-2 output into proposal receive; it has no activation field."""

    kind: Literal["propose_change_set"] = "propose_change_set"
    node_id: str
    candidate_templates: tuple[object, ...]

    @field_validator("candidate_templates", mode="before")
    @classmethod
    def _templates(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("propose_change_set requires at least one candidate template")
        return tuple(normalize_canonical(item) for item in value)


class MandateSettlementNodeV3(_StrictProcedureModel):
    kind: Literal["mandate_settlement"] = "mandate_settlement"
    node_id: str
    mandate: ProcedurePinBindingV1
    target_law: ProcedurePinBindingV1
    input: object

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> object:
        return normalize_canonical(value)


ProcedureNodeV3 = Annotated[
    StateTapNodeV3
    | SourceNodeV3
    | ExhaustTapNodeV3
    | ProviderNodeV3
    | TransformNodeV3
    | GuardNodeV3
    | ProjectNodeV3
    | RepeatNodeV3
    | CaptureEgressNodeV3
    | InboxEgressNodeV3
    | ProposeChangeSetNodeV3
    | MandateSettlementNodeV3,
    Field(discriminator="kind"),
]

TERMINAL_REQUIRED_RUNGS = {
    "emit_capture": 0,
    "post_inbox": 1,
    "propose_change_set": 2,
    "mandate_settlement": 3,
}
TERMINAL_NODE_KINDS = frozenset(TERMINAL_REQUIRED_RUNGS)


class ProcedureDefinitionV3(_StrictProcedureModel):
    """One complete graph-format-v3 definition; no mutable-core references."""

    graph_format: Literal[3] = 3
    name: str
    description: str | None = None
    contract_in: ProcedurePinBindingV1
    contract_out: ProcedurePinBindingV1
    parameter_contract: ProcedurePinBindingV1 | None = None
    nodes: tuple[ProcedureNodeV3, ...]
    returns: str
    pin_slots: tuple[ProcedurePinSlotV1, ...] = ()
    measurements: tuple[ProcedureMeasurementDeclarationV1, ...] = ()
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    terminal_capability: Literal[1, 2, 3]
    annotations: object = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="Procedure name")

    @field_validator("returns")
    @classmethod
    def _returns(cls, value: str) -> str:
        return _canonical_identifier(value, _ALIAS_RE, label="Procedure returns")

    @field_validator("pin_slots")
    @classmethod
    def _slots(cls, value: tuple[ProcedurePinSlotV1, ...]) -> tuple[ProcedurePinSlotV1, ...]:
        names = tuple(item.slot_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Procedure pin slots must be sorted and unique by slot_name")
        return value

    @field_validator("measurements")
    @classmethod
    def _measurements(
        cls,
        value: tuple[ProcedureMeasurementDeclarationV1, ...],
    ) -> tuple[ProcedureMeasurementDeclarationV1, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("M3: Procedure measurements must be sorted and unique by name")
        return value

    @field_validator("annotations", mode="before")
    @classmethod
    def _annotations(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _basic_shape(self) -> "ProcedureDefinitionV3":
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
        if self.returns not in aliases:
            raise ValueError("Procedure returns must name one declared output alias")
        if self.budget.wall_clock.microseconds > self.hard_caps.max_wall_clock.microseconds:
            raise ValueError("Procedure budget exceeds its wall-clock hard cap")
        if self.budget.max_provider_calls > self.hard_caps.max_provider_calls:
            raise ValueError("Procedure budget exceeds its provider-call hard cap")
        if self.budget.max_capture_bytes > self.hard_caps.max_capture_bytes:
            raise ValueError("Procedure budget exceeds its capture-byte hard cap")
        if self.budget.max_items > self.hard_caps.max_items:
            raise ValueError("Procedure budget exceeds its item hard cap")
        if any(
            isinstance(node, RepeatNodeV3)
            and node.max_attempts > self.hard_caps.max_repeat_attempts
            for node in self.nodes
        ):
            raise ValueError("Procedure repeat exceeds its repeat-attempt hard cap")
        # Import lazily so the model grammar does not depend on static-analysis
        # modules while its own classes are still being defined.
        from cruxible_core.playbill.procedures.graph import analyze_procedure_v3
        from cruxible_core.playbill.procedures.pin_expectations import (
            validate_procedure_pin_expectations,
        )

        validate_procedure_pin_expectations(self)
        graph = analyze_procedure_v3(self)
        for measurement in self.measurements:
            if measurement.subject_grain == "procedure_unit":
                continue
            measurement_node_id = measurement.node_id
            if measurement_node_id is None:  # pragma: no cover - declaration model invariant
                raise ValueError("M1: non-unit measurement requires node_id")
            if measurement_node_id not in graph.kinds:
                raise ValueError(
                    f"M1: measurement node_id {measurement_node_id!r} does not name "
                    "a node in this graph-v3 definition"
                )
            if measurement.subject_grain != "arm":
                continue
            from_node_id = measurement.from_node_id
            arm_label = measurement.arm_label
            if from_node_id is None or arm_label is None:  # pragma: no cover - model invariant
                raise ValueError("M2: arm measurement requires complete arm coordinates")
            successor = graph.edges.get(from_node_id, {}).get(arm_label)
            if successor != measurement_node_id:
                raise ValueError(
                    f"M2: measurement arm {from_node_id!r} "
                    f"{arm_label!r} does not target {measurement_node_id!r}"
                )
        return self


def iter_pin_bindings(value: object) -> tuple[ProcedurePinBindingV1, ...]:
    """Return every exact pin or slot reference nested in a v3 model."""

    found: list[ProcedurePinBindingV1] = []

    def visit(item: object) -> None:
        if isinstance(item, ArtifactPin | ProcedurePinSlotRefV1):
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
    "CaptureEgressNodeV3",
    "ExhaustTapNodeV3",
    "GuardNodeV3",
    "GuardPredicateV1",
    "InboxEgressNodeV3",
    "MandateSettlementNodeV3",
    "PredicateOperandV1",
    "ProcedureBudgetV3",
    "ProcedureDefinitionV3",
    "ProcedureHardCapsV3",
    "ProcedureMeasurementDeclarationV1",
    "ProcedureNodeV3",
    "ProcedurePinBindingV1",
    "ProcedurePinSlotRefV1",
    "ProcedurePinSlotV1",
    "ProjectNodeV3",
    "ProposeChangeSetNodeV3",
    "ProviderNodeV3",
    "RepeatBodyNodeV3",
    "RepeatNodeV3",
    "SourceNodeV3",
    "StateTapNodeV3",
    "TERMINAL_NODE_KINDS",
    "TERMINAL_REQUIRED_RUNGS",
    "TransformNodeV3",
    "iter_pin_bindings",
]
