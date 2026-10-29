"""The Procedure graph grammar (graph format 6).

This packaged contract module owns the live Cruxible Procedure graph profile.
Its dependencies are exact Cruxible pins and typed slot signatures a Line
binds; nothing here performs a mutable config or registry lookup.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.measurements import (
    ProcedureMeasurementDeclaration,
)
from cruxible_client.contracts.procedures.model_base import (
    _ALIAS_RE,
    _NAME_RE,
    _NODE_ID_RE,
    _canonical_identifier,
    _StrictProcedureModel,
)
from cruxible_client.contracts.procedures.model_base import (
    _ROLE_RE as _ROLE_RE,
)

# Node models, predicates and the terminal authority table live in ``nodes``;
# slots and pin bindings in ``slots``. Both are re-exported here unchanged.
from cruxible_client.contracts.procedures.nodes import (
    _TRANSFORM_SPEC_TAGS as _TRANSFORM_SPEC_TAGS,
)
from cruxible_client.contracts.procedures.nodes import (
    AUTHORITY_RUNG,
    RUNG_AUTHORITY,
    TERMINAL_NODE_KINDS,
    TERMINAL_REQUIRED_RUNGS,
    AuthorityVerb,
    CallNode,
    CaptureEgressNode,
    ClaimTapNode,
    ComparisonOperator,
    ConstantNode,
    EffectiveAuthority,
    EgressNode,
    ExhaustTapNode,
    GuardNode,
    GuardPredicate,
    HaltNode,
    InboxEgressNode,
    InvokeNode,
    PredicateOperand,
    PredicateScalar,
    ProcedureNode,
    ProcedureTransformSpec,
    ProjectNode,
    ProposalItemsFanOut,
    ProposeChangeSetNode,
    RepeatBodyNode,
    RepeatNode,
    ReturnNode,
    SelectNode,
    SettleChangeSetNode,
    SourceNode,
    StateTapNode,
    TransformAdapterSpec,
    TransformAggregateItemsSpec,
    TransformDedupeItemsSpec,
    TransformFilterItemsSpec,
    TransformJoinItemsSpec,
    TransformKind,
    TransformNode,
    TransformShapeItemsSpec,
    authority_for_rung,
    derived_terminal_capability,
    required_authority,
)
from cruxible_client.contracts.procedures.slots import (
    LegacyProcedurePinBinding,
    ProcedurePinBinding,
    ProcedureSlot,
    validate_procedure_slots,
)
from cruxible_client.contracts.procedures.source_program import ProcedureSourceProgram


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


class ProcedureDefinition(_StrictProcedureModel):
    """The one Procedure graph format (6): explicit value joins and typed return paths.

    ``slots`` declares each provider-calling node's signature ``{in, out,
    effect}`` by name. The Procedure never names a provider; a Line binds every
    slot to one exact build and operation when it is accepted.
    """

    graph_format: Literal[6] = 6
    name: str
    description: str | None = None
    contract_in: LegacyProcedurePinBinding
    contract_out: LegacyProcedurePinBinding
    parameter_contract: LegacyProcedurePinBinding | None = None
    nodes: tuple[ProcedureNode, ...]
    returns: str | None = None
    slots: dict[str, ProcedureSlot] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    measurements: tuple[ProcedureMeasurementDeclaration, ...] = ()
    budget: ProcedureBudget
    hard_caps: ProcedureHardCaps
    terminal_capability: Literal[1, 2, 3]
    annotations: object = Field(default_factory=dict)
    source: ProcedureSourceProgram | None = None

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

    _slots = field_validator("slots")(validate_procedure_slots)

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
            and getattr(self.source, "syntax_rules", self.source.rules)
            == "cruxible.procedure-source.v2"
            and self.returns is not None
        ):
            raise ValueError("Source-v2 uses explicit return paths, not a return alias")
        used = {slot for _occurrence, slot in self.slot_nodes()}
        if used != set(self.slots):
            undeclared = sorted(used - set(self.slots))
            unused = sorted(set(self.slots) - used)
            raise ValueError(
                "every slotted node names a declared slot and every slot is used"
                + (f"; undeclared: {undeclared}" if undeclared else "")
                + (f"; unused: {unused}" if unused else "")
            )
        for node in self.nodes:
            bodies: tuple[object, ...] = (node,)
            if isinstance(node, RepeatNode):
                bodies = tuple(body for body in node.body if body.operation == "call")
            for item in bodies:
                slot_name = getattr(item, "slot", None)
                if slot_name is None or slot_name not in self.slots:
                    continue
                effect = self.slots[slot_name].effect
                if isinstance(item, SourceNode):
                    if effect == "external_mutation":
                        raise ValueError(f"Source {item.node_id!r}: a Source slot cannot mutate")
                elif (effect == "external_mutation") != (
                    getattr(item, "effect_policy", None) is not None
                ):
                    raise ValueError(
                        f"node {getattr(item, 'node_id', '?')!r}: an effect policy is declared "
                        "exactly when its slot's effect is external_mutation"
                    )
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

    def slot_nodes(self) -> tuple[tuple[str, str], ...]:
        """``(occurrence id, slot)`` for every slotted node, repeat bodies included."""

        found: list[tuple[str, str]] = []
        for node in self.nodes:
            if isinstance(node, CallNode | SourceNode):
                found.append((node.node_id, node.slot))
            elif isinstance(node, RepeatNode):
                found.extend(
                    (f"{node.node_id}.{body.node_id}", body.slot)
                    for body in node.body
                    if body.operation == "call" and body.slot is not None
                )
        return tuple(found)


def iter_pin_bindings(value: object) -> tuple[ProcedurePinBinding, ...]:
    """Return every exact pin or slot reference nested in a Procedure model."""

    found: list[ProcedurePinBinding] = []

    def visit(item: object) -> None:
        if isinstance(item, ArtifactPin | ArtifactNameRef):
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
    "LegacyProcedurePinBinding",
    "PredicateOperand",
    "PredicateScalar",
    "ProcedureBudget",
    "ProcedureDefinition",
    "ProcedureHardCaps",
    "ProcedureMeasurementDeclaration",
    "ProcedureNode",
    "ProcedurePinBinding",
    "ProcedureSlot",
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
