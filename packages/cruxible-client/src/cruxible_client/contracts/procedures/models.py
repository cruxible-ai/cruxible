"""The Procedure graph grammar (graph format 7).

This packaged contract module owns the one Cruxible Procedure graph. Its
dependencies are field-typed names resolved at an accepted coordinate (an
execution plan records the exact pins) and typed slot signatures a Line
binds; nothing here performs a mutable config or registry lookup.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactNameRef
from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.claims import ClaimsNode
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
    CLOSED_TRANSFORM_KINDS,
    RUNG_AUTHORITY,
    TERMINAL_NODE_KINDS,
    TERMINAL_REQUIRED_RUNGS,
    AuthorityVerb,
    CallNode,
    CaptureEgressNode,
    EffectiveAuthority,
    EgressNode,
    ExtractNode,
    GuardNode,
    HaltNode,
    InboxEgressNode,
    InvokeNode,
    PredicateScalar,
    ProcedureNode,
    ProcedureTransformSpec,
    ProjectNode,
    ProposalItemsFanOut,
    ProposeChangeSetNode,
    ReadNode,
    RepeatBodyNode,
    RepeatNode,
    SelectNode,
    SettleChangeSetNode,
    TransformKind,
    TransformNode,
    authority_for_rung,
    derived_terminal_capability,
    is_terminal_node,
    predicate_step_aliases,
    refuse_retired_kinds,
    required_authority,
)
from cruxible_client.contracts.procedures.reads import DEFAULT_MAX_READ_ROWS
from cruxible_client.contracts.procedures.slots import (
    ProcedureSlot,
    validate_procedure_slots,
)


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
    #: The rows this run's reads may return in all (absent: the hard cap's).
    #: A Line and its Mandates can only lower it; an invoked child's is what
    #: its parent has left, which may be none.
    max_read_rows: int | None = Field(
        default=None,
        ge=0,
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
    #: The most rows the run's reads may return in all; each read's ceiling is
    #: what is left of it.
    max_read_rows: int = Field(
        default=DEFAULT_MAX_READ_ROWS,
        ge=1,
        le=2**31 - 1,
        exclude_if=lambda value: value == DEFAULT_MAX_READ_ROWS,
    )

    @model_validator(mode="after")
    def _nonzero_wall_clock(self) -> "ProcedureHardCaps":
        if self.max_wall_clock.microseconds == 0:
            raise ValueError("Procedure hard-cap wall clock must be nonzero")
        return self


def caps_budget(caps: ProcedureHardCaps) -> ProcedureBudget:
    """The operating budget of a run whose Line states none: the hard caps themselves.

    A direct run's ephemeral Line spec records this budget; an accepted Line
    states its own ``budgets``, which must fit within the caps.
    """

    return ProcedureBudget(
        wall_clock=caps.max_wall_clock,
        max_provider_calls=caps.max_provider_calls,
        max_capture_bytes=caps.max_capture_bytes,
        max_result_bytes=caps.max_result_bytes,
        max_items=caps.max_items,
        max_read_rows=caps.max_read_rows,
    )


def budget_within_caps(budget: ProcedureBudget, caps: ProcedureHardCaps) -> bool:
    """Whether an operating budget fits within a Procedure's hard caps."""

    return (
        budget.wall_clock.microseconds <= caps.max_wall_clock.microseconds
        and budget.max_provider_calls <= caps.max_provider_calls
        and budget.max_capture_bytes <= caps.max_capture_bytes
        and (budget.max_items is None or budget.max_items <= caps.max_items)
        and (
            budget.max_result_bytes is None
            or caps.max_result_bytes is None
            or budget.max_result_bytes <= caps.max_result_bytes
        )
        and (budget.max_read_rows is None or budget.max_read_rows <= caps.max_read_rows)
    )


class ProcedureDefinition(_StrictProcedureModel):
    """The one Procedure graph: explicit value joins and typed return paths.

    Every dependency field names a governed identity; an execution plan
    resolves each to its exact pin at the run's accepted coordinate.
    ``slots`` declares each provider-calling node's signature ``{in, out,
    effect}`` by name. The Procedure never names a provider; a Line binds every
    slot to one exact build and operation when it is accepted.
    """

    graph_format: Literal[8] = 8
    name: str
    description: str | None = None
    contract_in: ArtifactNameRef
    contract_out: ArtifactNameRef
    parameter_contract: ArtifactNameRef | None = None
    #: Each parameter's default by name. The params Contract stays shape-only;
    #: acceptance checks each default against its field, and a run's
    #: effective parameters are the Line's values over these defaults.
    parameter_defaults: dict[str, object] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    nodes: tuple[ProcedureNode, ...]
    returns: str | None = None
    slots: dict[str, ProcedureSlot] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    measurements: tuple[ProcedureMeasurementDeclaration, ...] = ()
    #: The Procedure's limits are its hard caps alone: the operating budget of a
    #: run is its Line's (a direct run's ephemeral Line spec), within these caps.
    hard_caps: ProcedureHardCaps
    terminal_capability: Literal[1, 2, 3]
    annotations: object = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _retired_kinds(cls, value: object) -> object:
        if isinstance(value, dict):
            refuse_retired_kinds(value.get("nodes"))
            if "budget" in value:
                raise ValueError(
                    "procedure_budget_retired: a Procedure declares hard_caps only; the "
                    "operating budget is its Line's (budgets), within those caps"
                )
        return value

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

    @field_validator("parameter_defaults", mode="before")
    @classmethod
    def _parameter_defaults(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("parameter_defaults maps each parameter name to its default")
        if not all(isinstance(name, str) for name in value):
            raise ValueError("a parameter default is keyed by its parameter name")
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
        if self.parameter_defaults and self.parameter_contract is None:
            raise ValueError(
                "procedure_parameter_default_undeclared: parameter_defaults name "
                "parameters, and this Procedure declares no params Contract"
            )
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
                if isinstance(item, CallNode) and item.acquires:
                    if effect == "external_mutation":
                        raise ValueError(
                            f"call {item.node_id!r}: a call with captures acquires; its slot "
                            "cannot mutate"
                        )
                    if item.effect_policy is not None:
                        raise ValueError(
                            f"call {item.node_id!r}: an acquiring call declares no effect policy"
                        )
                elif (effect == "external_mutation") != (
                    getattr(item, "effect_policy", None) is not None
                ):
                    raise ValueError(
                        f"node {getattr(item, 'node_id', '?')!r}: an effect policy is declared "
                        "exactly when its slot's effect is external_mutation"
                    )
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

    def effective_parameters(self, supplied: Mapping[str, object]) -> CanonicalValue:
        """A run's parameters: the supplied values (its Line's) over the defaults.

        The result is checked against the params Contract after the defaults
        apply; a parameter neither supplied nor defaulted stays absent.
        """

        return normalize_canonical({**self.parameter_defaults, **supplied})

    def slot_nodes(self) -> tuple[tuple[str, str], ...]:
        """``(occurrence id, slot)`` for every slotted node, repeat bodies included."""

        found: list[tuple[str, str]] = []
        for node in self.nodes:
            if isinstance(node, CallNode):
                found.append((node.node_id, node.slot))
            elif isinstance(node, RepeatNode):
                found.extend(
                    (f"{node.node_id}.{body.node_id}", body.slot)
                    for body in node.body
                    if body.operation == "call" and body.slot is not None
                )
        return tuple(found)


#: The pin role each dependency field resolves under.
REFERENCE_ROLES = {
    "contract_in": "contract-in",
    "contract_out": "contract-out",
    "parameter_contract": "parameter-contract",
    "query": "query",
    "capture_contract": "capture-contract",
    "captures": "capture-contract",
    "claim_type": "claim-type",
    "subject_kind": "subject-kind",
    "procedure": "procedure",
    "effect_policy": "effect-policy",
}


def iter_role_references(value: object) -> tuple[tuple[str, ArtifactNameRef], ...]:
    """Every dependency reference nested in a Procedure model, with its field's role."""

    found: list[tuple[str, ArtifactNameRef]] = []

    def visit(item: object, field: str) -> None:
        from cruxible_client.contracts.procedures.claims import ClaimsSubject

        if isinstance(item, ClaimsSubject):
            from cruxible_client.contracts.artifacts import ArtifactIdentity

            found.append(
                (
                    "subject-kind",
                    ArtifactNameRef(target=ArtifactIdentity(kind="SubjectKind", name=item.kind)),
                )
            )
        if isinstance(item, ArtifactNameRef):
            found.append((REFERENCE_ROLES[field], item))
            return
        if isinstance(item, BaseModel):
            for field_name in item.__class__.model_fields:
                visit(getattr(item, field_name), field_name)
            return
        if isinstance(item, tuple | list):
            for member in item:
                visit(member, field)
            return
        if isinstance(item, dict):
            for member in item.values():
                visit(member, field)

    visit(value, "")
    return tuple(found)


def iter_references(value: object) -> tuple[ArtifactNameRef, ...]:
    """Every dependency reference nested in a Procedure model."""

    return tuple(reference for _role, reference in iter_role_references(value))


__all__ = [
    "AUTHORITY_RUNG",
    "CLOSED_TRANSFORM_KINDS",
    "AuthorityVerb",
    "CallNode",
    "CaptureEgressNode",
    "EffectiveAuthority",
    "EgressNode",
    "ExtractNode",
    "GuardNode",
    "HaltNode",
    "InboxEgressNode",
    "InvokeNode",
    "PredicateScalar",
    "ProcedureBudget",
    "ProcedureDefinition",
    "ProcedureHardCaps",
    "ProcedureMeasurementDeclaration",
    "ProcedureNode",
    "ProcedureSlot",
    "ProcedureTransformSpec",
    "ProjectNode",
    "ProposalItemsFanOut",
    "ClaimsNode",
    "ProposeChangeSetNode",
    "ReadNode",
    "RUNG_AUTHORITY",
    "RepeatBodyNode",
    "RepeatNode",
    "SelectNode",
    "SettleChangeSetNode",
    "TERMINAL_NODE_KINDS",
    "TERMINAL_REQUIRED_RUNGS",
    "TransformKind",
    "TransformNode",
    "authority_for_rung",
    "budget_within_caps",
    "caps_budget",
    "is_terminal_node",
    "derived_terminal_capability",
    "REFERENCE_ROLES",
    "iter_references",
    "iter_role_references",
    "predicate_step_aliases",
    "required_authority",
]
