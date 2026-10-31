"""Sequence-style authoring that lowers to the existing ProcedureInput contract.

Templates are built from typed models, never hand-written tags: the proposal
items (``ProcedureKeyedClaimProposalItem``, ``ProcedureSubjectProposalItem``,
``ProcedureClaimRetirementItem``), their statement (``ProcedureClaimStatement``
with ``SubjectKeyReference`` and ``LiteralClaimObject``) and the
``workspace.file`` request (``WorkspaceFileSourceRequestV2``) each emit their
frozen format tag, and ``Item(path)`` names the current row's field.

Preview is a local structural check, never an admission or an execution. Accepted
references are resolved by the daemon at prepare; private stand-ins below let the
shared graph validator check topology without pretending to resolve those refs.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from dataclasses import field as dataclass_field
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field, JsonValue, field_validator

from cruxible_client.contracts.artifacts import (
    ArtifactNameRef,
    ArtifactPin,
    parse_artifact_identity,
)
from cruxible_client.contracts.authoring.inputs import (
    AcceptedReferenceInput,
    CarriedContractInput,
    ProcedureInput,
    lower_authoring_input,
)
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.authored import (
    ProcedureSpec,
    ProcedureSpecError,
    lower_procedure_spec,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.contracts import validate_contract_schema
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    analyze_procedure,
)
from cruxible_client.contracts.procedures.models import (
    RUNG_AUTHORITY,
    TERMINAL_NODE_KINDS,
    AuthorityVerb,
    GuardPredicate,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProcedureNode,
    ProcedureTransformSpec,
    TransformKind,
    derived_terminal_capability,
)
from cruxible_client.contracts.procedures.proposal_items import (
    ProcedureClaimRetirementItem,
    ProcedureClaimStatement,
    ProcedureKeyedClaimProposalItem,
    ProcedureSubjectProposalItem,
    SubjectKeyClaimObject,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.procedures.source_program import (
    ProcedureSourceProgram,
    SourceBinding,
    SourceMapEntry,
    SourceSpan,
)
from cruxible_client.contracts.records import RecordConstructor
from cruxible_client.contracts.subject_kinds import SubjectKeyReference
from cruxible_client.contracts.workspace_file import WorkspaceFileSourceRequestV2

Contract: TypeAlias = CarriedContractInput


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


@dataclass(frozen=True)
class Output:
    """An explicit reference to an earlier step, optionally selecting a field."""

    step: str
    path: str = ""


@dataclass(frozen=True)
class Previous:
    """The preceding output (or invocation input at the start of a sequence)."""

    path: str = ""


def Item(path: str = "") -> str:  # noqa: N802 - reads as a reference, like Output and Previous
    """The current record inside a closed transform or a fan-out item template, or its field.

    Lowers to ``"$item.<path>"``, the reference a transform or fan-out resolves per
    row. It is a plain string, so it fits any typed builder field below:
    ``ProcedureKeyedClaimProposalItem(statement=ProcedureClaimStatement(
    subject=SubjectKeyReference(subject_kind="catalog.part", key=Item("part_id")),
    ...), rationale=Item("why"))``.
    """

    return "$item" + (f".{path}" if path else "")


@dataclass(frozen=True)
class Step:
    """Name is the output alias and binding key; edges target the node identity.

    Preserve an existing graph's distinct node identity with ``node_id`` when
    adopting the builder, without renaming outputs or changing its bindings.
    """

    name: str
    next: str | None = None
    node_id: str | None = dataclass_field(default=None, kw_only=True)


@dataclass(frozen=True)
class Read(Step):
    """A read of accepted state through a named Query; it runs to the end."""

    query: str = ""
    parameters: object = None


@dataclass(frozen=True, kw_only=True)
class Call(Step):
    """A typed call through ``slot``; a Line binds the slot to an exact operation.

    ``captures`` names, for each ``capture<K>`` field of the slot's out
    Contract, the CaptureContract its bytes are committed as; a call with
    captures acquires.
    """

    input: object = Previous()
    slot: str
    effect_policy: str | None = None
    captures: dict[str, str] = dataclass_field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class Extract(Step):
    """Read one Capture's records, typed: ``capture`` is ``Output(<call>, <capture field>)``.

    ``row`` pins the Contract whose fields the step reads (``{name, version,
    digest}``); ``fields``, ``key`` and ``header`` map each name to where it
    is read (``{"at": ..., "parse": ..., "presence": ...}``).
    """

    capture: object
    records: str
    row: dict[str, object]
    fields: dict[str, dict[str, object]]
    key: dict[str, dict[str, object]] = dataclass_field(default_factory=dict)
    header: dict[str, dict[str, object]] = dataclass_field(default_factory=dict)
    complete_when: dict[str, object] | None = None
    duplicates: Literal["refuse_run", "refuse_key", "first"] = "refuse_key"
    only_keys: object = None


@dataclass(frozen=True, kw_only=True)
class Transform(Step):
    """A closed transform: its records are typed as they flow; it names no Contracts."""

    transform_kind: TransformKind
    spec: ProcedureTransformSpec


@dataclass(frozen=True, kw_only=True)
class Project(Step):
    fields: object
    contract_out: Contract


@dataclass(frozen=True, kw_only=True)
class Guard(Step):
    predicate: GuardPredicate
    on_true: str | None = None
    on_false: str = "$abort"
    refusal_code: str = "guard_refused"
    message: str = "Procedure guard refused."


@dataclass(frozen=True)
class EmitCapture(Step):
    """Emit evidence as a Capture; ``result`` is the Procedure's typed return value."""

    capture_contract: str = ""
    input: object = Previous()
    result: object = Previous()


@dataclass(frozen=True)
class PostInbox(Step):
    """Post items to the Line inbox, as they are, for intelligence to decide.

    ``input`` is one item, or ``{"items": Output("rows", "items")}`` to post one
    item per element of a list the run produced. Posting is inert (it never
    touches accepted state) and needs observe authority; a Procedure posts only
    when a Line runs it. ``max_items`` caps what one run posts (the core default
    when absent); items past it are not posted and the run reports the cut.
    """

    input: object = Previous()
    max_items: int | None = None


@dataclass(frozen=True, kw_only=True)
class ProposeChangeSet(Step):
    """Propose the candidate Claims; ``result`` is the Procedure's typed return value.

    ``candidate_templates`` is a fixed tuple of item templates, or
    ``{"items": "$steps.<alias>.items"}`` to propose one Claim per element.
    """

    candidate_templates: tuple[object, ...] | dict[str, object]
    result: object = Previous()


@dataclass(frozen=True, kw_only=True)
class SettleChangeSet(Step):
    """Settle the candidate Claims under the one covering settle mandate, or fall back."""

    candidate_templates: tuple[object, ...] | dict[str, object]
    result: object = Previous()


@dataclass(frozen=True)
class Halt(Step):
    reason: str | None = None


_KINDS = {
    Read: "read",
    Call: "call",
    Extract: "extract",
    Transform: "transform",
    Project: "project",
    Guard: "guard",
    EmitCapture: "emit_capture",
    PostInbox: "post_inbox",
    ProposeChangeSet: "propose_change_set",
    SettleChangeSet: "settle_change_set",
    Halt: "halt",
}


class CompositionDiagnostic(BaseModel):
    step: str | None = None
    code: str
    message: str
    span: SourceSpan | None = None
    related_spans: tuple[SourceSpan, ...] = ()
    hint: str | None = None


class ProcedureStateDependency(BaseModel):
    node_id: str
    kind: Literal["claim", "query"]
    selection: ArtifactPin | ArtifactNameRef
    cardinality: Literal["one", "all", "query"]
    limit: int | None = None
    admitted_context: bool = True
    subject_kind: str | None = None
    selector: JsonValue = None

    _canonical_selector = field_validator("selector", mode="before")(normalize_canonical)


class ProcedureBindingRequirement(BaseModel):
    slot: str
    resolved: SourceBinding | None = None


class ProcedureBranchValue(BaseModel):
    node_id: str
    kind: Literal["guard", "select"]
    producers: tuple[str, ...] = ()
    predicate: GuardPredicate | None = None
    contract: ArtifactPin | ArtifactNameRef | None = None
    successors: dict[str, str] = Field(default_factory=dict)


class ProcedureReturnPath(BaseModel):
    node_id: str
    kind: Literal["pure", "capture", "proposal", "settlement", "halt"]
    contract: ArtifactPin | ArtifactNameRef
    # The authority the path's terminal needs; a pure or halting path needs none.
    required_authority: AuthorityVerb | None


class ProcedureChildCall(BaseModel):
    node_id: str
    procedure: ArtifactPin | ArtifactNameRef
    inherits_authority: bool = True
    shares_budget: bool = True


class ProcedurePreview(BaseModel):
    """JSON-serializable inspection; no provider calls or accepted-state writes."""

    name: str
    ready_for_prepare: bool
    contracts: tuple[CarriedContractInput, ...]
    contract_in: ArtifactPin | ArtifactNameRef | dict[str, Any]
    contract_out: ArtifactPin | ArtifactNameRef | dict[str, Any]
    # The most this Procedure's terminals can do: observe, propose or settle.
    authority: AuthorityVerb
    nodes: tuple[ProcedureNode | dict[str, Any], ...]
    edges: dict[str, dict[str, str]] = Field(default_factory=dict)
    slots: dict[str, ProcedureSlot] = Field(default_factory=dict)
    terminals: tuple[str, ...] = ()
    returns: str | None
    budget: ProcedureBudget
    hard_caps: ProcedureHardCaps
    errors: tuple[CompositionDiagnostic, ...] = ()
    source: ProcedureSourceProgram | None = None
    source_map: tuple[SourceMapEntry, ...] = ()
    state_dependencies: tuple[ProcedureStateDependency, ...] = ()
    binding_requirements: tuple[ProcedureBindingRequirement, ...] = ()
    branch_values: tuple[ProcedureBranchValue, ...] = ()
    return_paths: tuple[ProcedureReturnPath, ...] = ()
    children: tuple[ProcedureChildCall, ...] = ()
    pending_checks: tuple[str, ...] = (
        "Resolve accepted references and pin slot Contracts at the intent base.",
        "Validate runtime values against contracts; preview does not execute any path.",
        "Check authority, installation availability and effective policy budgets at admission.",
    )


class ProcedureCompositionError(ValueError):
    def __init__(self, preview: ProcedurePreview):
        self.preview = preview
        super().__init__("; ".join(f"{d.step or 'sequence'}: {d.message}" for d in preview.errors))


def _accepted(name: str, kind: str, role: str) -> dict[str, str]:
    target = name if name.startswith(kind + ":") else kind + ":" + name
    parse_artifact_identity(target)
    return AcceptedReferenceInput(kind="accepted", target=target, role=role).model_dump()


def _template(value: object, previous: str | None) -> Any:
    if isinstance(value, (Previous, Output)):
        base = (
            ("$input" if previous is None else f"$steps.{previous}")
            if isinstance(value, Previous)
            else f"$steps.{value.step}"
        )
        return base + ("." + value.path if value.path else "")
    if isinstance(value, BaseModel):
        return _template(value.model_dump(mode="json", by_alias=True), previous)
    if isinstance(value, dict):
        return {k: _template(v, previous) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_template(v, previous) for v in value]
    return value


def _named(value: Any) -> Any:
    """The authoring references as graph names, for local shape validation only."""
    if isinstance(value, dict):
        if value.get("kind") in {"accepted", "carried_contract"}:
            target = value.get("target", "Contract:" + value.get("name", ""))
            return {"target": parse_artifact_identity(target).model_dump(mode="json")}
        return {k: _named(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_named(v) for v in value]
    return value


def _schema(contract: CarriedContractInput) -> ContractSchema:
    return ContractSchema(fields=contract.fields, allow_extra=contract.allow_extra)


@dataclass(frozen=True)
class Sequence:
    """Compose forward steps; guards may name explicit forward branch targets.

    Call steps name a slot of ``slots``; a Line binds each slot to an
    exact operation. ``preview()`` is local; ``build()`` produces the shared
    ProcedureInput consumed by every surface.
    """

    steps: tuple[Step, ...] | list[Step]
    name: str
    contract_in: Contract
    contract_out: Contract
    budget: ProcedureBudget
    hard_caps: ProcedureHardCaps
    returns: str | None = None
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot"
    description: str | None = None
    slots: dict[str, ProcedureSlot] = dataclass_field(default_factory=dict)

    def _compile(self) -> tuple[ProcedureInput, ProcedurePreview]:
        contracts: dict[str, CarriedContractInput] = {}
        errors: list[CompositionDiagnostic] = []
        nodes: list[dict[str, Any]] = []
        previous: str | None = None
        output_contracts: dict[str, ContractSchema | None] = {}

        def error(step: str | None, code: str, message: str) -> None:
            errors.append(CompositionDiagnostic(step=step, code=code, message=message))

        def contract_ref(contract: Contract, role: str) -> dict[str, Any]:
            if isinstance(contract, CarriedContractInput):
                if contract.name in contracts and contracts[contract.name] != contract:
                    error(
                        None, "contract_conflict", f"Conflicting definitions for {contract.name!r}"
                    )
                contracts[contract.name] = contract
                return {"kind": "carried_contract", "name": contract.name, "role": role}
            raise TypeError(
                "Procedure Contracts must be owner-carried; "
                "standalone accepted Contract references do not exist"
            )

        root_in = contract_ref(self.contract_in, "contract-in")
        root_out = contract_ref(self.contract_out, "contract-out")
        for step in self.steps:
            kind = _KINDS.get(type(step))
            if kind is None:
                raise TypeError(f"Unsupported sequence step: {type(step).__name__}")
            node: dict[str, Any] = {
                "kind": kind,
                "node_id": step.name if step.node_id is None else step.node_id,
            }
            for field in fields(step):
                if field.name in {"name", "node_id", "next"}:
                    continue
                value = getattr(step, field.name)
                if field.name in {"contract_in", "contract_out"}:
                    node[field.name] = contract_ref(value, field.name.replace("_", "-"))
                elif field.name == "capture_contract":
                    node[field.name] = _accepted(value, "CaptureContract", "capture-contract")
                elif field.name == "captures":
                    if value:
                        node[field.name] = {
                            name: _accepted(contract, "CaptureContract", "capture-contract")
                            for name, contract in sorted(value.items())
                        }
                elif field.name == "query":
                    node[field.name] = _accepted(value, "QueryDefinition", "query")
                elif field.name == "effect_policy":
                    if value is not None:
                        node[field.name] = _accepted(value, "EffectPolicy", "effect-policy")
                elif field.name == "slot":
                    node["slot"] = value
                else:
                    node[field.name] = _template(value, previous)
            if isinstance(step, Read) and step.parameters is None:
                node["parameters"] = {}
            if isinstance(step, PostInbox) and step.max_items is None:
                del node["max_items"]  # absent takes the core default cap
            if isinstance(step, Call) and step.slot not in self.slots:
                error(
                    step.name,
                    "slot_undeclared",
                    f"Declare slot {step.slot!r} in slots with its {{in, out, effect}} signature.",
                )
            if isinstance(
                step, (Guard, EmitCapture, PostInbox, ProposeChangeSet, SettleChangeSet, Halt)
            ):
                if step.next is not None:
                    error(
                        step.name,
                        "invalid_next",
                        "Use guard arms; terminal steps have no successor.",
                    )
            else:
                node["as"] = step.name
                if step.next is not None:
                    node["next"] = step.next
            for field_name in ("input", "fields", "spec"):
                if field_name not in node:
                    continue
                schema_ref = getattr(
                    step, "contract_out" if field_name == "fields" else "contract_in", None
                )
                if isinstance(schema_ref, CarriedContractInput) and not _has_reference(
                    node[field_name]
                ):
                    try:
                        value = node[field_name]
                        if field_name == "spec":
                            value = {k: v for k, v in value.items() if k != "tag"}
                        validate_contract_schema(_schema(schema_ref), value)
                    except ValueError as exc:
                        error(step.name, "contract_value_invalid", str(exc))
            nodes.append(node)
            if "as" in node:
                previous = step.name
                carried = getattr(step, "contract_out", None)
                output_contracts[step.name] = (
                    _schema(carried) if isinstance(carried, CarriedContractInput) else None
                )
        returns = self.returns or previous or "result"
        returned_contract = output_contracts.get(returns)
        if returned_contract is not None and returned_contract != _schema(self.contract_out):
            error(
                returns,
                "return_contract_mismatch",
                "The returned output schema differs from the Procedure output contract.",
            )
        definition: dict[str, Any] = dict(
            name=self.name,
            description=self.description,
            contract_in=root_in,
            contract_out=root_out,
            nodes=nodes,
            returns=returns,
            budget=self.budget.model_dump(mode="json"),
            hard_caps=self.hard_caps.model_dump(mode="json"),
            terminal_capability=derived_terminal_capability(nodes),
        )
        if self.slots:
            definition["slots"] = {
                name: slot.model_dump(mode="json", by_alias=True)
                for name, slot in sorted(self.slots.items())
            }
        edges: dict[str, dict[str, str]] = {}
        try:
            checked = ProcedureDefinition.model_validate(_named(definition))
            edges = analyze_procedure(checked).edges
        except (ValueError, ProcedureGraphFormatError) as exc:
            error(None, "graph_invalid", str(exc))
        input_ = ProcedureInput(
            kind="procedure",
            definition=definition,
            activation_policy=self.activation_policy,
            contracts=tuple(contracts[name] for name in sorted(contracts)),
        )
        lower_authoring_input(input_)  # Same pure payload lowering used by SDK/CLI/HTTP.
        if not errors:
            authored = _authored_input(input_)
            assert authored.spec is not None
            try:
                lower_procedure_spec(authored.spec)
            except ProcedureSpecError as exc:
                index = exc.field_path.partition("$.steps[")[2].partition("]")[0]
                authored_step = (
                    authored.spec.steps[int(index)]
                    if index.isdigit() and int(index) < len(authored.spec.steps)
                    else None
                )
                error(
                    getattr(authored_step, "id", None),
                    exc.code.removeprefix("cruxible.authoring."),
                    str(exc),
                )
        visible_nodes = tuple(nodes)
        preview = ProcedurePreview(
            name=self.name,
            ready_for_prepare=not errors,
            contracts=input_.contracts,
            contract_in=root_in,
            contract_out=root_out,
            authority=RUNG_AUTHORITY[derived_terminal_capability(nodes)],
            slots=dict(self.slots),
            nodes=visible_nodes,
            edges=edges,
            terminals=tuple(
                n["node_id"]
                for n in nodes
                if n["kind"] in TERMINAL_NODE_KINDS or not edges.get(n["node_id"])
            ),
            returns=returns,
            budget=self.budget,
            hard_caps=self.hard_caps,
            errors=tuple(errors),
        )
        return input_, preview

    def preview(self) -> ProcedurePreview:
        return self._compile()[1]

    def build(self) -> ProcedureInput:
        input_, preview = self._compile()
        if preview.errors:
            raise ProcedureCompositionError(preview)
        return _authored_input(input_)


def _authored_input(input_: ProcedureInput) -> ProcedureInput:
    """One conversion shared by preview validation and the submitted input."""
    from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContract
    from cruxible_client.contracts.procedures.decompile import decompile_procedure

    definition = ProcedureDefinition.model_validate(_named(input_.definition))
    owner = ProcedureArtifact.model_construct(
        definition=definition,
        owned_contracts=tuple(
            ProcedureOwnedContract(
                identity=parse_artifact_identity("Contract:" + c.name),
                schema=_schema(c),
            )
            for c in input_.contracts
        ),
        activation_policy=input_.activation_policy,
        acquisition_defaults=input_.acquisition_defaults,
    )
    return ProcedureInput(kind="procedure", spec=decompile_procedure(owner))


def _has_reference(value: object) -> bool:
    if isinstance(value, str):
        return value.startswith(("$input", "$steps", "$params", "$item"))
    if isinstance(value, dict):
        return any(_has_reference(v) for v in value.values())
    if isinstance(value, (tuple, list)):
        return any(_has_reference(v) for v in value)
    return False


__all__ = [
    "Call",
    "CompositionDiagnostic",
    "Contract",
    "EmitCapture",
    "Extract",
    "Guard",
    "Halt",
    "Item",
    "LiteralClaimObject",
    "Output",
    "PostInbox",
    "Previous",
    "ProcedureClaimRetirementItem",
    "ProcedureClaimStatement",
    "ProcedureCompositionError",
    "ProcedureKeyedClaimProposalItem",
    "ProcedurePreview",
    "ProcedureSubjectProposalItem",
    "Project",
    "ProposeChangeSet",
    "SettleChangeSet",
    "Sequence",
    "ProcedureSpec",
    "lower_procedure_spec",
    "Read",
    "SubjectKeyClaimObject",
    "SubjectKeyReference",
    "Transform",
    "WorkspaceFileSourceRequestV2",
]
