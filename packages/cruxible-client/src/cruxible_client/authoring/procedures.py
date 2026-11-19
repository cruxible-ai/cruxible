"""A thin typed builder over the one Procedure authoring form, :class:`ProcedureSpec`.

``Sequence`` appends typed spec steps and returns a handle per output step.
A handle is a real object: ``handle["field"]`` is the typed :class:`Ref` to one
of its output fields, checked against the fields the step is known to produce
(its declared output, a query read's view, or a supplied slot Contract); an
unknown field refuses at the line that names it. The builder never assembles
a graph: :meth:`Sequence.spec` is the spec every surface submits, and
:meth:`Sequence.preview` lowers it locally with the same shared lowering.

Preview is a local structural check, never an admission or an execution.
Accepted references are resolved by the daemon at prepare.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Literal

from pydantic import BaseModel, Field

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.authoring.inputs import ProcedureInput
from cruxible_client.contracts.capabilities import Capability
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
)
from cruxible_client.contracts.procedures.authored import (
    EXTRACT_VIEW_FIELDS,
    OPEN_TRANSFORM_KINDS,
    CallStep,
    ClaimsExpectBySpec,
    ClaimsStep,
    ClaimsSubjectSpec,
    ClaimTemplateSpec,
    DedupeSpec,
    FieldType,
    FilterSpec,
    InboxStep,
    ProcedureLimits,
    ProcedureSpec,
    ProcedureSpecError,
    ReadStep,
    ReturnFields,
    ReturnFrom,
    Step,
    Template,
    TransformStep,
    lower_procedure_spec,
    refuse_closed_steps,
)
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    analyze_procedure,
)
from cruxible_client.contracts.procedures.layout_extract import KeyTypeOverride, LayoutExtractStep
from cruxible_client.contracts.procedures.models import (
    RUNG_AUTHORITY,
    TERMINAL_NODE_KINDS,
    AuthorityVerb,
    ProcedureDefinition,
    derived_terminal_capability,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.procedures.views import query_view_fields
from cruxible_client.contracts.records import RecordConstructor
from cruxible_client.contracts.refs import Ref, item_ref, make_ref
from cruxible_client.contracts.shapes import (
    AllOf,
    AnyOf,
    Comparison,
    ComparisonOp,
    Contract,
    CountOperand,
    LiteralOperand,
    Membership,
    Not,
    Operand,
    Predicate,
    Presence,
)


def procedure_record_constructor(
    artifact: ProcedureArtifact,
    direction: Literal["input", "params", "output"],
    contracts: Iterable[Contract],
) -> RecordConstructor:
    """A typed record constructor for one boundary of an accepted Procedure.

    ``contracts`` are the governed Contracts the Procedure's boundaries pin at
    its coordinate (with the Contracts they nest), as its readiness read returns
    them.
    """

    pin = (
        artifact.definition.contract_in
        if direction == "input"
        else artifact.definition.parameter_contract
        if direction == "params"
        else artifact.definition.contract_out
    )
    if pin is None:
        raise ValueError("the Procedure declares no params")
    if not isinstance(pin, ArtifactPin | ArtifactNameRef):
        raise ValueError("Typed execution requires a named boundary Contract")
    by_identity = {contract.identity: contract for contract in contracts}
    found = by_identity.get(pin.target)
    if found is None:
        raise ValueError(f"the Procedure's {direction} Contract {pin.target.qualified} is unread")
    return RecordConstructor(
        found,
        _resolve=lambda ref: by_identity.get(ref.identity),
        _omittable_fields=frozenset(artifact.definition.parameter_defaults)
        if direction == "params"
        else frozenset(),
    )


# -- typed references and predicates ------------------------------------------------------


@dataclass(frozen=True)
class Handle:
    """One value the run holds: the Procedure input, its parameters, or a step's output.

    ``handle["field"]`` (or :meth:`ref`) is the typed :class:`Ref` to a member.
    When the handle's fields are known, a name it does not have refuses.
    """

    root: Literal["input", "param", "step", "item"]
    step: str | None = None
    fields: tuple[str, ...] | None = None

    def ref(self, *path: str) -> Ref:
        if path and self.fields is not None and path[0] not in self.fields:
            near = get_close_matches(path[0], self.fields, n=3)
            raise KeyError(
                f"{self._label()} has no field {path[0]!r}"
                + (f"; did you mean {', '.join(near)}?" if near else f"; fields: {self.fields}")
            )
        return make_ref(self.root, *path, step=self.step)

    def __getitem__(self, field: str) -> Ref:
        return self.ref(field)

    @property
    def whole(self) -> Ref:
        """The whole value (the step's entire output)."""

        return make_ref(self.root, step=self.step)

    def _label(self) -> str:
        return f"step {self.step!r}" if self.step is not None else f"the Procedure {self.root}"


def item(*path: str) -> Ref:
    """The current record (or element) inside a ``for_each``, a transform, or a fan-out."""

    return item_ref(*path)


def field(name: str) -> Ref:
    """The current record's field ``name`` (``$item.fields.<name>``), in a transform."""

    return item_ref("fields", name)


def _operand(value: object) -> Operand:
    if isinstance(value, Ref | LiteralOperand | CountOperand):
        return value
    if isinstance(value, bool | int | str):
        return LiteralOperand(literal=value)
    raise TypeError(f"a predicate operand is a Ref, count(...) or a literal, not {value!r}")


def _compare(op: ComparisonOp, left: object, right: object) -> Comparison:
    return Comparison(op=op, left=_operand(left), right=_operand(right))


def eq(left: object, right: object) -> Comparison:
    return _compare("eq", left, right)


def ne(left: object, right: object) -> Comparison:
    return _compare("ne", left, right)


def lt(left: object, right: object) -> Comparison:
    return _compare("lt", left, right)


def le(left: object, right: object) -> Comparison:
    return _compare("le", left, right)


def gt(left: object, right: object) -> Comparison:
    return _compare("gt", left, right)


def ge(left: object, right: object) -> Comparison:
    return _compare("ge", left, right)


def is_in(left: object, members: tuple[str | int | bool, ...] | Ref) -> Membership:
    return Membership(op="in", left=_operand(left), right=members)


def not_in(left: object, members: tuple[str | int | bool, ...] | Ref) -> Membership:
    return Membership(op="not_in", left=_operand(left), right=members)


def present(ref: Ref) -> Presence:
    return Presence(op="present", of=ref)


def absent(ref: Ref) -> Presence:
    return Presence(op="absent", of=ref)


def all_of(*parts: Predicate) -> AllOf:
    return AllOf(of=parts)


def any_of(*parts: Predicate) -> AnyOf:
    return AnyOf(of=parts)


def not_(part: Predicate) -> Not:
    return Not(of=part)


def count(ref: Ref) -> CountOperand:
    return CountOperand(count=ref)


# -- the builder ---------------------------------------------------------------------


class CompositionDiagnostic(BaseModel):
    step: str | None = None
    code: str
    message: str


class ProcedurePreview(BaseModel):
    """JSON-serializable inspection; no provider calls or accepted-state writes."""

    name: str
    ready_for_prepare: bool
    spec: ProcedureSpec
    # The most this Procedure's terminals can do: observe, propose or settle.
    authority: AuthorityVerb | None = None
    nodes: tuple[dict[str, Any], ...] = ()
    edges: dict[str, dict[str, str]] = Field(default_factory=dict)
    terminals: tuple[str, ...] = ()
    returns: str | None = None
    errors: tuple[CompositionDiagnostic, ...] = ()
    pending_checks: tuple[str, ...] = (
        "Resolve accepted references and pin slot capabilities at the intent base.",
        "Validate runtime values against contracts; preview does not execute any path.",
        "Check authority, installation availability and effective policy budgets at admission.",
    )


class ProcedureCompositionError(ValueError):
    def __init__(self, preview: ProcedurePreview):
        self.preview = preview
        super().__init__("; ".join(f"{d.step or 'sequence'}: {d.message}" for d in preview.errors))


def _contract_fields(contract: Contract | None) -> tuple[str, ...] | None:
    return None if contract is None else tuple(contract.fields)


class Sequence:
    """Build one ProcedureSpec, step by step, with typed handles and predicates.

    ``slots`` names each slot's capability, ``{"capability": "name@N"}`` (plus
    ``may_truncate``); authoring pins it and a Line binds it. ``capabilities``
    and ``contracts`` optionally supply accepted capabilities and Contracts by
    ``name@N`` (a slot's capability and its out Contract) so call handles check
    their field names; without them, a call's fields are checked at prepare.
    ``hard_caps`` is the most any run may use; the operating budget is the Line's.
    """

    def __init__(
        self,
        procedure: str,
        *,
        input: Mapping[str, FieldType | str] | None = None,
        params: Mapping[str, FieldType | str] | None = None,
        output: Mapping[str, FieldType | str] | None = None,
        slots: Mapping[str, ProcedureSlot | Mapping[str, object]] | None = None,
        capabilities: Mapping[str, Capability] | None = None,
        contracts: Mapping[str, Contract] | None = None,
        description: str | None = None,
        activation: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot",
        hard_caps: ProcedureLimits | None = None,
    ) -> None:
        self.procedure = procedure
        self._input = dict(input or {})
        self._params = dict(params or {})
        self._output = None if output is None else dict(output)
        self._slots = {
            name: ProcedureSlot.model_validate(slot) for name, slot in (slots or {}).items()
        }
        self._capabilities = dict(capabilities or {})
        self._contracts = dict(contracts or {})
        self._description = description
        self._activation = activation
        self._hard_caps = hard_caps
        self._steps: list[Step] = []
        self._returns: str | ReturnFrom | ReturnFields | None = None
        self.input = Handle("input", fields=tuple(self._input))
        self.params = Handle("param", fields=tuple(self._params))

    # -- steps that produce a value -------------------------------------------------

    def _add(self, step: Step, name: str, fields: tuple[str, ...] | None) -> Handle:
        self._steps.append(step)
        return Handle("step", step=name, fields=fields)

    def read(
        self,
        name: str,
        *,
        query: str,
        params: Mapping[str, Template] | None = None,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """Read accepted state through a named Query; it runs to the end."""

        step = ReadStep(read=name, query=query, params=dict(params or {}), id=id, next=next)
        return self._add(step, name, tuple(query_view_fields()))

    def read_claims(
        self,
        name: str,
        *,
        claim_type: str,
        subject: Template,
        subject_kind: str | None = None,
        all: int | None = None,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """Read one ClaimType's Claims about a Subject (``all``: at most that many)."""

        step = ReadStep(
            read=name,
            claim_type=claim_type,
            subject=subject,
            subject_kind=subject_kind,
            all=all,
            id=id,
            next=next,
        )
        return self._add(step, name, None)

    def call(
        self,
        name: str,
        *,
        slot: str,
        input: Template,
        captures: Mapping[str, str] | None = None,
        effect_policy: str | None = None,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """Call ``slot``; a Line binds it. ``captures`` names each capture field's
        CaptureContract (a call with captures acquires)."""

        step = CallStep(
            call=name,
            slot=slot,
            input=input,
            captures=dict(captures or {}),
            effect_policy=effect_policy,
            id=id,
            next=next,
        )
        declared = self._slots.get(slot)
        capability = (
            None if declared is None else self._capabilities.get(declared.capability.spelling)
        )
        out = None if capability is None else self._contracts.get(capability.out.spelling)
        return self._add(step, name, _contract_fields(out))

    def extract(
        self,
        name: str,
        *,
        capture: Ref,
        records: str,
        header: tuple[str, ...] | list[str] = (),
        key: Mapping[str, KeyTypeOverride | Mapping[str, object] | str] | None = None,
        duplicates: Literal["refuse_run", "refuse_key", "first"] = "refuse_key",
        only_keys: Template = None,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """Read record set ``records`` of the Capture ``capture`` names, through its
        CaptureKind's layout: the layout types the records; ``header`` selects some
        of its header fields; ``key`` may give a plain-string key part a Subject key
        type (``{part: "key_part<K:p>"}``)."""

        step = LayoutExtractStep.model_validate(
            dict(
                extract=name,
                capture=capture,
                records=records,
                header=tuple(sorted(header, key=lambda item: item.encode("utf-8"))),
                key={
                    part: {"type": value} if isinstance(value, str) else value
                    for part, value in (key or {}).items()
                },
                duplicates=duplicates,
                only_keys=only_keys,
                id=id,
                next=next,
            )
        )
        return self._add(step, name, EXTRACT_VIEW_FIELDS)

    def transform(
        self,
        name: str,
        *,
        kind: Literal["filter", "dedupe"],
        spec: FilterSpec | DedupeSpec,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """Narrow records with a filter or dedupe; their records are typed as they flow.

        The other transform kinds are closed (computation lives in a derive).
        """

        if kind not in OPEN_TRANSFORM_KINDS:
            raise ProcedureSpecError(
                "procedure_step_closed",
                name,
                f"The {kind} transform is closed: a Procedure narrows with filter and "
                "dedupe only. Computation lives in a provider derive.",
            )
        step = TransformStep(transform=name, kind=kind, spec=spec, id=id, next=next)
        return self._add(step, name, None)

    def inbox(
        self,
        contract: str,
        *,
        fields: Mapping[str, Template],
        for_each: Ref | None = None,
        dedupe_key: tuple[str, ...] = (),
        max_items: int | None = None,
        id: str | None = None,
    ) -> None:
        """Post typed items (records of ``contract``, ``name@N``) to the Line inbox."""

        self._steps.append(
            InboxStep(
                inbox=contract,
                fields=dict(fields),
                for_each=for_each,
                dedupe_key=dedupe_key,
                max_items=max_items,
                id=id,
            )
        )

    def propose(self, *args: Any, **kwargs: Any) -> Handle:
        raise ProcedureSpecError("retired_node", "propose", "Use the claims node via .claims().")

    def settle(self, *args: Any, **kwargs: Any) -> Handle:
        raise ProcedureSpecError("retired_node", "settle", "Use the claims node via .claims().")

    def claims(
        self,
        templates: tuple[ClaimTemplateSpec, ...],
        *,
        for_each: Ref,
        subject: ClaimsSubjectSpec,
        where: Predicate | None = None,
        expect: Ref | ClaimsExpectBySpec | None = None,
        id: str | None = None,
    ) -> None:
        """Map typed records to Claims; the run's Mandate chooses proposal or settlement."""
        self._steps.append(
            ClaimsStep(
                claims=templates,
                for_each=for_each,
                subject=subject,
                where=where,
                expect=expect,
                id=id,
            )
        )

    def returns(self, value: str | Handle | ReturnFrom | ReturnFields) -> None:
        """What the Procedure returns: a step (or its handle), or a return projection."""

        self._returns = value.step if isinstance(value, Handle) else value

    # -- the spec ----------------------------------------------------------------------

    def spec(self) -> ProcedureSpec:
        """The one authoring form this builder states (validated)."""

        data: dict[str, Any] = dict(
            procedure=self.procedure,
            description=self._description,
            input=self._input,
            params=self._params,
            output=self._output,
            slots=self._slots,
            activation=self._activation,
            steps=tuple(self._steps),
            returns=self._returns,
        )
        if self._hard_caps is not None:
            data["hard_caps"] = self._hard_caps
        return ProcedureSpec.model_validate(data)

    def preview(self) -> ProcedurePreview:
        """Lower the spec locally (the shared lowering) and inspect its graph."""

        spec = self.spec()
        errors: list[CompositionDiagnostic] = []
        nodes: tuple[dict[str, Any], ...] = ()
        edges: dict[str, dict[str, str]] = {}
        returns: str | None = None
        authority: AuthorityVerb | None = None
        try:
            refuse_closed_steps(spec)
            lowered = lower_procedure_spec(spec)
            definition = dict(lowered.definition)
            nodes = tuple(definition["nodes"])
            returns = definition.get("returns")
            authority = RUNG_AUTHORITY[derived_terminal_capability(nodes)]
            checked = ProcedureDefinition.model_validate(definition)
            edges = analyze_procedure(checked).edges
        except ProcedureSpecError as exc:
            index = exc.field_path.partition("$.steps[")[2].partition("]")[0]
            step = (
                spec.steps[int(index)] if index.isdigit() and int(index) < len(spec.steps) else None
            )
            errors.append(
                CompositionDiagnostic(
                    step=getattr(step, "id", None),
                    code=exc.code.removeprefix("cruxible.authoring."),
                    message=str(exc),
                )
            )
        except (ValueError, ProcedureGraphFormatError) as exc:
            errors.append(CompositionDiagnostic(code="graph_invalid", message=str(exc)))
        return ProcedurePreview(
            name=self.procedure,
            ready_for_prepare=not errors,
            spec=spec,
            authority=authority,
            nodes=nodes,
            edges=edges,
            terminals=tuple(
                str(node["node_id"])
                for node in nodes
                if node["kind"] in TERMINAL_NODE_KINDS
                or node.get("leaf") is True
                or not edges.get(str(node["node_id"]))
            ),
            returns=returns,
            errors=tuple(errors),
        )

    def build(self) -> ProcedureInput:
        """The authoring input every surface submits: ``{kind: procedure, spec}``."""

        preview = self.preview()
        if preview.errors:
            raise ProcedureCompositionError(preview)
        return ProcedureInput(kind="procedure", spec=preview.spec)


__all__ = [
    "CompositionDiagnostic",
    "DedupeSpec",
    "FilterSpec",
    "Handle",
    "ProcedureCompositionError",
    "ProcedureLimits",
    "ProcedurePreview",
    "ProcedureSpec",
    "Ref",
    "ReturnFields",
    "ReturnFrom",
    "Sequence",
    "absent",
    "all_of",
    "any_of",
    "count",
    "eq",
    "field",
    "ge",
    "gt",
    "is_in",
    "item",
    "le",
    "lower_procedure_spec",
    "lt",
    "ne",
    "not_",
    "not_in",
    "present",
]
