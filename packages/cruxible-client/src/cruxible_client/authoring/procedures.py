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

from collections.abc import Mapping
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Literal

from pydantic import BaseModel, Field

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.authoring.inputs import ProcedureInput
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.authored import (
    AbortAction,
    AbortDetail,
    AggregateSpec,
    BranchAction,
    CallStep,
    DedupeSpec,
    DeriveCaseSpec,
    DeriveSpec,
    EmitStep,
    ExtractStep,
    FieldType,
    FilterSpec,
    GotoAction,
    GuardStep,
    HaltAction,
    HaltStep,
    InboxStep,
    InvokeStep,
    JoinSpec,
    ProcedureLimits,
    ProcedureSpec,
    ProcedureSpecError,
    ProjectStep,
    ProposeStep,
    ReadStep,
    RepeatBody,
    RepeatCall,
    RepeatStep,
    RepeatTransform,
    ReturnFields,
    ReturnFrom,
    SelectStep,
    SettleStep,
    ShapeFieldSpec,
    ShapeSpec,
    Step,
    Template,
    TransformSpec,
    TransformStep,
    UnionSpec,
    extract_view_fields,
    lower_procedure_spec,
)
from cruxible_client.contracts.procedures.extract import CompleteWhen, ExtractField
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    analyze_procedure,
)
from cruxible_client.contracts.procedures.models import (
    RUNG_AUTHORITY,
    TERMINAL_NODE_KINDS,
    AuthorityVerb,
    ProcedureDefinition,
    TransformKind,
    derived_terminal_capability,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.procedures.views import query_view_schema
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
    artifact: ProcedureArtifact, direction: Literal["input", "output"]
) -> RecordConstructor:
    pin = (
        artifact.definition.contract_in
        if direction == "input"
        else artifact.definition.contract_out
    )
    if not isinstance(pin, ArtifactPin | ArtifactNameRef):
        raise ValueError("Typed execution requires a resolved owner-carried Contract")
    for contract in artifact.owned_contracts:
        if contract.identity == pin.target and (
            isinstance(pin, ArtifactNameRef)
            or procedure_owned_contract_digest(contract).tagged == pin.artifact_digest
        ):
            return RecordConstructor(contract.contract_schema)
    raise ValueError("Procedure contract is absent from its exact owner closure")


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


def abort(code: str, message: str) -> AbortAction:
    return AbortAction(abort=AbortDetail(code=code, message=message))


def halt(reason: str | None, *, message: str | None = None, id: str | None = None) -> HaltAction:
    return HaltAction(halt=reason, message=message, id=id)


def goto(step: str) -> GotoAction:
    return GotoAction(goto=step)


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
        "Resolve accepted references and pin slot Contracts at the intent base.",
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

    ``slots`` declares each slot's ``{in, out, effect}`` signature; a Line binds
    it. ``contracts`` optionally supplies accepted Contracts by ``name@N`` (a
    slot's out Contract) so call handles check their field names; without one,
    a call's fields are checked at prepare.
    """

    def __init__(
        self,
        procedure: str,
        *,
        input: Mapping[str, FieldType],
        params: Mapping[str, FieldType] | None = None,
        output: Mapping[str, FieldType] | None = None,
        slots: Mapping[str, ProcedureSlot | Mapping[str, object]] | None = None,
        contracts: Mapping[str, Contract] | None = None,
        description: str | None = None,
        activation: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot",
        budget: ProcedureLimits | None = None,
        caps: ProcedureLimits | None = None,
    ) -> None:
        self.procedure = procedure
        self._input = dict(input)
        self._params = dict(params or {})
        self._output = None if output is None else dict(output)
        self._slots = {
            name: ProcedureSlot.model_validate(slot) for name, slot in (slots or {}).items()
        }
        self._contracts = dict(contracts or {})
        self._description = description
        self._activation = activation
        self._budget = budget
        self._caps = caps
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
        return self._add(step, name, tuple(query_view_schema().fields))

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
        signature = self._slots.get(slot)
        out = None if signature is None else self._contracts.get(signature.out.spelling)
        return self._add(step, name, _contract_fields(out))

    def extract(
        self,
        name: str,
        *,
        capture: Ref,
        records: str,
        row: str,
        fields: Mapping[str, ExtractField | Mapping[str, object]],
        key: Mapping[str, ExtractField | Mapping[str, object]] | None = None,
        header: Mapping[str, ExtractField | Mapping[str, object]] | None = None,
        complete_when: CompleteWhen | None = None,
        duplicates: Literal["refuse_run", "refuse_key", "first"] = "refuse_key",
        only_keys: Template = None,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """Read one Capture's records, typed by the ``row`` Contract (``name@N``)."""

        step = ExtractStep.model_validate(
            dict(
                extract=name,
                capture=capture,
                records=records,
                row=row,
                fields=dict(fields),
                key=dict(key or {}),
                header=dict(header or {}),
                complete_when=complete_when,
                duplicates=duplicates,
                only_keys=only_keys,
                id=id,
                next=next,
            )
        )
        return self._add(step, name, tuple(extract_view_fields(None)))

    def project(
        self,
        name: str,
        *,
        fields: Mapping[str, Template] | Ref | None = None,
        literal: object = None,
        output: Mapping[str, FieldType] | None = None,
        contract: str | None = None,
        leaf: bool = False,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """A typed record from run values (``fields``) or exact data (``literal``)."""

        step = ProjectStep.model_validate(
            dict(
                project=name,
                fields=dict(fields) if isinstance(fields, Mapping) else fields,
                literal=literal,
                output=None if output is None else dict(output),
                contract=contract,
                leaf=leaf,
                id=id,
                next=next,
            )
        )
        known = (
            tuple(output)
            if output is not None
            else tuple(fields)
            if isinstance(fields, Mapping)
            else None
        )
        return self._add(step, name, known)

    def transform(
        self,
        name: str,
        *,
        kind: TransformKind,
        spec: TransformSpec,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """A closed transform; its records are typed as they flow."""

        step = TransformStep(transform=name, kind=kind, spec=spec, id=id, next=next)
        return self._add(step, name, None)

    def invoke(
        self,
        procedure: str,
        *,
        input: Template,
        as_: str,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """Invoke an accepted Procedure under this run's limits and authority."""

        step = InvokeStep.model_validate(
            {"invoke": procedure, "input": input, "as": as_, "id": id, "next": next}
        )
        return self._add(step, as_, None)

    def select(
        self,
        name: str,
        *,
        sources: tuple[str, ...],
        output: Mapping[str, FieldType] | None = None,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """Join mutually exclusive branch producers into one value."""

        step = SelectStep(
            select=name,
            sources=sources,
            output=None if output is None else dict(output),
            id=id,
            next=next,
        )
        return self._add(step, name, None if output is None else tuple(output))

    def repeat(
        self,
        name: str,
        *,
        max_attempts: int,
        body: tuple[RepeatBody, ...],
        until: Predicate,
        id: str | None = None,
        next: str | None = None,
    ) -> Handle:
        """Run ``body`` (calls and closed transforms) until ``until`` holds, bounded."""

        step = RepeatStep(
            repeat=name, max_attempts=max_attempts, body=body, until=until, id=id, next=next
        )
        return self._add(step, name, None)

    # -- control and terminals ---------------------------------------------------------

    def guard(
        self,
        predicate: Predicate,
        *,
        then: BranchAction | None = None,
        else_: BranchAction | None = None,
        id: str | None = None,
    ) -> None:
        """Branch: ``then`` (default: continue), ``else`` (default: abort guard_refused)."""

        data: dict[str, Any] = {"guard": predicate, "then": then, "id": id}
        if else_ is not None:
            data["else"] = else_
        self._steps.append(GuardStep.model_validate(data))

    def halt(
        self, reason: str | None, *, message: str | None = None, id: str | None = None
    ) -> None:
        self._steps.append(HaltStep(halt=reason, message=message, id=id))

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

    def propose(
        self,
        candidates: Ref | tuple[Template, ...],
        *,
        result: Template = None,
        id: str | None = None,
    ) -> None:
        """Propose Claims: fixed item templates, or one item per element of a list."""

        self._steps.append(ProposeStep(propose=candidates, result=result, id=id))

    def settle(
        self,
        candidates: Ref | tuple[Template, ...],
        *,
        result: Template = None,
        id: str | None = None,
    ) -> None:
        """Settle Claims under the one covering settle grant, or fall back."""

        self._steps.append(SettleStep(settle=candidates, result=result, id=id))

    def emit(
        self,
        capture_contract: str,
        *,
        input: Template,
        result: Template,
        id: str | None = None,
    ) -> None:
        """Emit evidence as a Capture; ``result`` is the Procedure's typed return value."""

        self._steps.append(EmitStep(emit=capture_contract, input=input, result=result, id=id))

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
        if self._budget is not None:
            data["budget"] = self._budget
        if self._caps is not None:
            data["caps"] = self._caps
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
    "AggregateSpec",
    "CompositionDiagnostic",
    "DedupeSpec",
    "DeriveCaseSpec",
    "DeriveSpec",
    "FilterSpec",
    "Handle",
    "JoinSpec",
    "ProcedureCompositionError",
    "ProcedureLimits",
    "ProcedurePreview",
    "ProcedureSpec",
    "Ref",
    "RepeatCall",
    "RepeatTransform",
    "ReturnFields",
    "ReturnFrom",
    "Sequence",
    "ShapeFieldSpec",
    "ShapeSpec",
    "UnionSpec",
    "abort",
    "absent",
    "all_of",
    "any_of",
    "count",
    "eq",
    "field",
    "ge",
    "goto",
    "gt",
    "halt",
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
