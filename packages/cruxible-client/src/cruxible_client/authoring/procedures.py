"""Sequence-style authoring that lowers to the existing ProcedureInput contract.

Preview is a local structural check, never an admission or an execution. Accepted
references are resolved by the daemon at prepare; private stand-ins below let the
shared graph validator check topology without pretending to resolve those refs.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from dataclasses import field as dataclass_field
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from cruxible_client.contracts.artifacts import ArtifactPin, parse_artifact_identity
from cruxible_client.contracts.authoring.inputs import (
    AcceptedReferenceInput,
    CarriedContractInput,
    ProcedureInput,
    lower_authoring_input,
)
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactAny,
    ProcedureArtifactV2,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.contracts import validate_contract_schema
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    analyze_procedure_v4,
)
from cruxible_client.contracts.procedures.models import (
    RUNG_AUTHORITY,
    TERMINAL_NODE_KINDS,
    AuthorityVerb,
    GuardPredicateV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV5,
    ProcedureHardCapsV3,
    ProcedureNodeV6,
    ProcedurePinSlotRefV1,
    ProcedureTransformSpecV1,
    TransformKindV1,
    derived_terminal_capability,
)
from cruxible_client.contracts.procedures.source_program import (
    ProcedureSourceV1,
    SourceBinding,
    SourceMapEntry,
    SourceSpan,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_contracts import ProviderOperationContractV1
from cruxible_client.contracts.records import RecordConstructor

if TYPE_CHECKING:
    from cruxible_client.contracts import PlaybillProviderInterfaceEntry

Contract: TypeAlias = CarriedContractInput


def procedure_record_constructor(
    artifact: ProcedureArtifactAny, direction: Literal["input", "output"]
) -> RecordConstructor:
    pin = (
        artifact.definition.contract_in
        if direction == "input"
        else artifact.definition.contract_out
    )
    if not isinstance(artifact, ProcedureArtifactV2) or not isinstance(pin, ArtifactPin):
        raise ValueError("Typed execution requires a resolved owner-carried Contract")
    for contract in artifact.owned_contracts:
        if (
            contract.identity == pin.target
            and procedure_owned_contract_digest(contract).tagged == pin.artifact_digest
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


class ProviderBinding(BaseModel):
    """Names and exact interface/implementation identifiers from provider discovery.

    These are authoring selections, not installation or execution permission.
    The daemon verifies them against the accepted authoring base.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str
    interface: str
    interface_digest: str
    implementation_digest: str
    effect_class: Literal["none", "external_read", "external_mutation"] | None = None
    operation_contract: ProviderOperationContractV1 | None = None
    coordinate: AcceptedCoordinate | None = None

    @property
    def input(self) -> RecordConstructor:
        if self.operation_contract is None:
            raise ValueError("This interface has no declared operation contract")
        return RecordConstructor(self.operation_contract.input)

    @field_validator("interface_digest", "implementation_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        from cruxible_client.contracts.canonical import ArtifactDigest

        return ArtifactDigest.from_tagged(value).tagged

    @classmethod
    def from_interface(
        cls, entry: PlaybillProviderInterfaceEntry, *, provider: str | None = None
    ) -> ProviderBinding:
        identity = (
            None if provider is None else _accepted(provider, "Provider", "provider")["target"]
        )
        matches = [
            p for p in entry.providers if identity is None or p.provider_identity == identity
        ]
        if len(matches) != 1:
            raise ValueError("Select exactly one registered provider for this interface")
        selected = matches[0]
        return cls(
            provider=selected.provider_identity,
            interface=entry.identity,
            interface_digest=entry.interface_digest,
            implementation_digest=selected.implementation_digest,
            effect_class=entry.effect_class,
            operation_contract=entry.operation_contract,
        )


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
class StateTap(Step):
    query: str = ""
    parameters: object = None


@dataclass(frozen=True)
class Source(Step):
    capture_contract: str = ""
    request: object = Previous()
    provider: ProviderBinding | None = None


@dataclass(frozen=True, kw_only=True)
class Call(Step):
    contract_in: Contract
    contract_out: Contract
    input: object = Previous()
    provider: ProviderBinding | None = None
    effect_policy: str | None = None


@dataclass(frozen=True, kw_only=True)
class Transform(Step):
    transform_kind: TransformKindV1
    contract_in: Contract
    contract_out: Contract
    spec: ProcedureTransformSpecV1


@dataclass(frozen=True, kw_only=True)
class Project(Step):
    fields: object
    contract_out: Contract


@dataclass(frozen=True, kw_only=True)
class Guard(Step):
    predicate: GuardPredicateV1
    on_true: str | None = None
    on_false: str = "$abort"
    refusal_code: str = "guard_refused"
    message: str = "Procedure guard refused."


@dataclass(frozen=True)
class EmitCapture(Step):
    capture_contract: str = ""
    input: object = Previous()


@dataclass(frozen=True, kw_only=True)
class ProposeChangeSet(Step):
    candidate_templates: tuple[object, ...]


@dataclass(frozen=True)
class Halt(Step):
    reason: str | None = None


_KINDS = {
    StateTap: "state_tap",
    Source: "source",
    Call: "call",
    Transform: "transform",
    Project: "project",
    Guard: "guard",
    EmitCapture: "emit_capture",
    ProposeChangeSet: "propose_change_set",
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
    selection: ArtifactPin | ProcedurePinSlotRefV1
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
    predicate: GuardPredicateV1 | None = None
    contract: ArtifactPin | ProcedurePinSlotRefV1 | None = None
    successors: dict[str, str] = Field(default_factory=dict)


class ProcedureReturnPath(BaseModel):
    node_id: str
    kind: Literal["pure", "capture", "proposal", "halt"]
    contract: ArtifactPin | ProcedurePinSlotRefV1
    required_terminal_rung: int


class ProcedureChildCall(BaseModel):
    node_id: str
    procedure: ArtifactPin
    inherits_authority: bool = True
    shares_budget: bool = True


class ProcedurePreview(BaseModel):
    """JSON-serializable inspection; no provider calls or accepted-state writes."""

    name: str
    ready_for_prepare: bool
    contracts: tuple[CarriedContractInput, ...]
    contract_in: ArtifactPin | ProcedurePinSlotRefV1 | dict[str, Any]
    contract_out: ArtifactPin | ProcedurePinSlotRefV1 | dict[str, Any]
    # The most this Procedure's terminals can do: observe, propose or settle.
    authority: AuthorityVerb
    acquisition_policy: str | None
    nodes: tuple[ProcedureNodeV6 | dict[str, Any], ...]
    edges: dict[str, dict[str, str]] = Field(default_factory=dict)
    providers: dict[str, ProviderBinding] = Field(default_factory=dict)
    terminals: tuple[str, ...] = ()
    returns: str | None
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    errors: tuple[CompositionDiagnostic, ...] = ()
    source: ProcedureSourceV1 | None = None
    source_map: tuple[SourceMapEntry, ...] = ()
    state_dependencies: tuple[ProcedureStateDependency, ...] = ()
    binding_requirements: tuple[ProcedureBindingRequirement, ...] = ()
    branch_values: tuple[ProcedureBranchValue, ...] = ()
    return_paths: tuple[ProcedureReturnPath, ...] = ()
    children: tuple[ProcedureChildCall, ...] = ()
    pending_checks: tuple[str, ...] = (
        "Resolve accepted references at the intent base and verify provider interfaces.",
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


def _symbolic_pins(value: Any) -> Any:
    # These pins are exclusively for local shape validation. They never escape
    # preview or enter the submitted payload, retained state, or a receipt.
    if isinstance(value, dict):
        if value.get("kind") in {"accepted", "carried_contract"}:
            target = value.get("target", "Contract:" + value.get("name", ""))
            return ArtifactPin(
                role=value["role"],
                target=parse_artifact_identity(target),
                artifact_digest="sha256:" + sha256(target.encode()).hexdigest(),
            ).model_dump(mode="json")
        return {k: _symbolic_pins(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_symbolic_pins(v) for v in value]
    return value


def _schema(contract: CarriedContractInput) -> ContractSchema:
    return ContractSchema(fields=contract.fields, allow_extra=contract.allow_extra)


@dataclass(frozen=True)
class Sequence:
    """Compose forward steps; guards may name explicit forward branch targets.

    ``bind(fetch=selection)`` returns a new blueprint. ``preview()`` is local;
    ``build()`` produces the shared ProcedureInput consumed by every surface.
    """

    steps: tuple[Step, ...] | list[Step]
    name: str
    contract_in: Contract
    contract_out: Contract
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    returns: str | None = None
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot"
    acquisition_policy: str | None = None
    description: str | None = None

    def bind(self, **providers: ProviderBinding) -> Sequence:
        by_name = {step.name: step for step in self.steps}
        for name, provider in providers.items():
            if not isinstance(by_name.get(name), (Source, Call)):
                raise ValueError(f"{name!r} is not a provider-backed step")
            if not isinstance(provider, ProviderBinding):
                raise TypeError("bind requires a ProviderBinding from accepted interface discovery")
        return replace(
            self,
            steps=tuple(
                replace(step, provider=providers[step.name])
                if step.name in providers and isinstance(step, (Source, Call))
                else step
                for step in self.steps
            ),
        )

    def _compile(self) -> tuple[ProcedureInput, ProcedurePreview]:
        contracts: dict[str, CarriedContractInput] = {}
        errors: list[CompositionDiagnostic] = []
        nodes: list[dict[str, Any]] = []
        previous: str | None = None
        output_contracts: dict[str, Contract | None] = {}

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
                if field.name in {"name", "node_id", "next", "provider"}:
                    continue
                value = getattr(step, field.name)
                if field.name in {"contract_in", "contract_out"}:
                    node[field.name] = contract_ref(value, field.name.replace("_", "-"))
                elif field.name == "capture_contract":
                    node[field.name] = _accepted(value, "CaptureContract", "capture-contract")
                elif field.name == "query":
                    node[field.name] = _accepted(value, "QueryDefinition", "query")
                elif field.name == "effect_policy" and value is not None:
                    node[field.name] = _accepted(value, "EffectPolicy", "effect-policy")
                else:
                    node[field.name] = _template(value, previous)
            if isinstance(step, StateTap) and step.parameters is None:
                node["parameters"] = {}
            if isinstance(step, (Source, Call)):
                binding = step.provider
                if binding is None:
                    error(
                        step.name,
                        "provider_unbound",
                        "Bind an accepted provider interface before prepare.",
                    )
                    # Shape-check the rest of the graph, including all branches.
                    binding = ProviderBinding(
                        provider="unbound",
                        interface="unbound",
                        interface_digest="sha256:" + "0" * 64,
                        implementation_digest="sha256:" + "0" * 64,
                    )
                node.update(
                    provider=_accepted(binding.provider, "Provider", "provider"),
                    interface=_accepted(
                        binding.interface, "ProviderInterface", "provider-interface"
                    ),
                    interface_digest=binding.interface_digest,
                    implementation_digest=binding.implementation_digest,
                )
            if isinstance(step, (Guard, EmitCapture, ProposeChangeSet, Halt)):
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
            if (
                isinstance(step, Call)
                and isinstance(step.input, (Previous, Output))
                and not step.input.path
            ):
                source_contract = (
                    self.contract_in
                    if previous is None and isinstance(step.input, Previous)
                    else output_contracts.get(
                        (previous or "") if isinstance(step.input, Previous) else step.input.step
                    )
                )
                if (
                    isinstance(source_contract, CarriedContractInput)
                    and isinstance(step.contract_in, CarriedContractInput)
                    and _schema(source_contract) != _schema(step.contract_in)
                ):
                    error(
                        step.name,
                        "contract_mismatch",
                        "Automatic whole-output wiring requires matching carried schemas; "
                        "use an explicit field mapping or adapter.",
                    )
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
                            value = (
                                value["value"]
                                if node["transform_kind"] == "adapter"
                                else {k: v for k, v in value.items() if k != "tag"}
                            )
                        validate_contract_schema(_schema(schema_ref), value)
                    except ValueError as exc:
                        error(step.name, "contract_value_invalid", str(exc))
            nodes.append(node)
            if "as" in node:
                previous = step.name
                output_contracts[step.name] = getattr(step, "contract_out", None)
        returns = self.returns or previous or "result"
        returned_contract = output_contracts.get(returns)
        if (
            isinstance(returned_contract, CarriedContractInput)
            and isinstance(self.contract_out, CarriedContractInput)
            and _schema(returned_contract) != _schema(self.contract_out)
        ):
            error(
                returns,
                "return_contract_mismatch",
                "The returned output schema differs from the Procedure output contract.",
            )
        definition: dict[str, Any] = dict(
            graph_format=5,
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
        edges: dict[str, dict[str, str]] = {}
        try:
            checked = ProcedureDefinitionV5.model_validate(_symbolic_pins(definition))
            edges = analyze_procedure_v4(checked).edges
        except (ValueError, ProcedureGraphFormatError) as exc:
            error(None, "graph_invalid", str(exc))
        input_ = ProcedureInput(
            kind="procedure",
            definition=definition,
            activation_policy=self.activation_policy,
            acquisition_policy=self.acquisition_policy,
            contracts=tuple(contracts[name] for name in sorted(contracts)),
        )
        lower_authoring_input(input_)  # Same pure payload lowering used by SDK/CLI/HTTP.
        # Do not expose internal unbound stand-ins as if they were selections.
        visible_nodes = tuple(
            {
                k: v
                for k, v in node.items()
                if k not in {"provider", "interface", "interface_digest", "implementation_digest"}
            }
            if isinstance(step, (Source, Call)) and step.provider is None
            else node
            for step, node in zip(self.steps, nodes, strict=True)
        )
        preview = ProcedurePreview(
            name=self.name,
            ready_for_prepare=not errors,
            contracts=input_.contracts,
            contract_in=root_in,
            contract_out=root_out,
            authority=RUNG_AUTHORITY[derived_terminal_capability(nodes)],
            acquisition_policy=self.acquisition_policy,
            providers={
                step.name: step.provider
                for step in self.steps
                if isinstance(step, (Source, Call)) and step.provider is not None
            },
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
        return input_


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
    "Guard",
    "Halt",
    "Output",
    "Previous",
    "ProcedureCompositionError",
    "ProcedurePreview",
    "Project",
    "ProposeChangeSet",
    "ProviderBinding",
    "Sequence",
    "Source",
    "StateTap",
    "Transform",
]
