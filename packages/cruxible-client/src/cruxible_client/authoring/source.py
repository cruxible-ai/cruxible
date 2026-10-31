"""Retained Python authoring; the daemon resolves and compiles every dependency.

The decorated function is never called. SDK code selects names/typed handles;
source compilation and accepted artifact identities belong to the backend.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Literal, cast

from pydantic import JsonValue

from cruxible_client.authoring.inputs import CarriedContractInput, ProcedureInput
from cruxible_client.authoring.procedures import (
    CompositionDiagnostic,
    ProcedureBindingRequirement,
    ProcedureBranchValue,
    ProcedureChildCall,
    ProcedureCompositionError,
    ProcedurePreview,
    ProcedureReturnPath,
    ProcedureStateDependency,
)
from cruxible_client.authoring.sdk_types import ProcedureRef, QueryRef
from cruxible_client.contracts.artifacts import ArtifactNameRef
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.procedures.models import (
    RUNG_AUTHORITY,
    ProcedureBudget,
    ProcedureHardCaps,
    derived_terminal_capability,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.procedures.source_program import SourceContract
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreviewRequest,
    ProcedureSourceRequest,
    SourceProcedureSelection,
    SourceQuerySelection,
    SourceSelection,
    SourceSlotSelection,
)

if TYPE_CHECKING:
    from cruxible_client.authoring.world import World


def _contract(value: CarriedContractInput) -> SourceContract:
    if not isinstance(value, CarriedContractInput):
        raise TypeError(
            "Procedure Contracts must be owner-carried; "
            "standalone accepted Contract references do not exist"
        )
    return SourceContract(name=value.name, schema=value.value.schema)


@dataclass(frozen=True)
class ProcedureSource:
    """Retained Procedure source and its binding selections.

    ``slots`` (slot name -> ``{in, out, effect}``) are the signatures the
    source calls through ``bindings.<slot>``. A Procedure never names a
    provider: a Line binds each slot to one exact build and operation.
    """

    _request: ProcedureSourceRequest
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot"
    _bindings: Mapping[str, QueryRef | ProcedureRef] = field(
        default_factory=lambda: MappingProxyType({})
    )
    slots: Mapping[str, ProcedureSlot] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def name(self) -> str:
        return self._request.name

    @property
    def source(self) -> str:
        return self._request.text

    @property
    def filename(self) -> str:
        return self._request.filename

    @property
    def contract_in(self) -> CarriedContractInput:
        c = self._request.input
        return CarriedContractInput(
            name=c.name, fields=c.schema_.fields, allow_extra=c.schema_.allow_extra
        )

    @property
    def contract_out(self) -> CarriedContractInput:
        c = self._request.output
        return CarriedContractInput(
            name=c.name, fields=c.schema_.fields, allow_extra=c.schema_.allow_extra
        )

    @property
    def budget(self) -> ProcedureBudget:
        return self._request.budget.model_copy(deep=True)

    @property
    def hard_caps(self) -> ProcedureHardCaps:
        return self._request.hard_caps.model_copy(deep=True)

    @property
    def description(self) -> str | None:
        return self._request.description

    @property
    def bindings(self) -> Mapping[str, QueryRef | ProcedureRef]:
        return self._bindings

    def __call__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "Procedure source is not executable Python. "
            "Use preview/build or run its accepted Procedure."
        )

    def bind(self, **bindings: QueryRef | ProcedureRef) -> ProcedureSource:
        """Bind query and child-Procedure names; slots are bound by a Line, not here."""

        unknown = set(bindings) - _binding_slots(self.source)
        if unknown:
            raise ValueError(f"Unknown Procedure binding names: {sorted(unknown)}")
        for name, value in bindings.items():
            if not name.isidentifier() or not isinstance(value, (QueryRef, ProcedureRef)):
                raise TypeError("Bindings require named accepted query/Procedure handles")
            if name in self.slots:
                raise TypeError(
                    f"{name!r} is a slot: a Line binds it to a provider operation, not the source"
                )
        return replace(self, _bindings=MappingProxyType(dict(self._bindings, **bindings)))

    def _at(self, world: World) -> ProcedureSourceRequest:
        selections: dict[str, SourceSelection] = {}
        for name, value in self._bindings.items():
            if isinstance(value, QueryRef):
                world._playbill._assert_coordinate(value.coordinate)
                selections[name] = SourceQuerySelection(name=value.address)
            else:
                world._playbill._assert_coordinate(value.coordinate)
                selections[name] = SourceProcedureSelection(name=value.address)
        for name, slot in self.slots.items():
            selections[name] = SourceSlotSelection.model_validate(
                {"in": slot.in_.spelling, "out": slot.out.spelling, "effect": slot.effect}
            )
        return self._request.model_copy(update={"bindings": selections}, deep=True)

    def preview(self, *, world: World | None = None) -> ProcedurePreview:
        from cruxible_client.contracts.projection import AcceptedCoordinate

        if world is None:
            return ProcedurePreview(
                name=self.name,
                ready_for_prepare=False,
                contracts=(self.contract_in, self.contract_out),
                contract_in=self._request.input.schema_.model_dump(),
                contract_out=self._request.output.schema_.model_dump(),
                authority="observe",
                nodes=(),
                returns="",
                budget=self.budget,
                hard_caps=self.hard_caps,
                binding_requirements=tuple(
                    ProcedureBindingRequirement(slot=s) for s in sorted(_binding_slots(self.source))
                ),
                errors=(
                    CompositionDiagnostic(
                        code="cruxible.source.context_required",
                        message="Supply world=cx.world() for backend source compilation.",
                    ),
                ),
            )
        request = ProcedureSourcePreviewRequest(
            source=self._at(world),
            at=AcceptedCoordinate.model_validate(world.coordinate.model_dump(mode="json")),
        )
        compiled = world._playbill._client.preview_procedure_source(
            world._playbill._instance_id, request=request
        )
        from cruxible_client.contracts.procedures.models import (
            TERMINAL_REQUIRED_RUNGS,
            GuardNode,
            InvokeNode,
            ReadNode,
            SelectNode,
            is_terminal_node,
            required_authority,
        )

        definition = compiled.definition
        source = None if definition is None else definition.source
        edges = compiled.edges
        terminal_kinds: dict[str, Literal["pure", "capture", "proposal", "settlement", "halt"]] = {
            "project": "pure",
            "emit_capture": "capture",
            "propose_change_set": "proposal",
            "settle_change_set": "settlement",
            "halt": "halt",
        }
        return ProcedurePreview(
            name=self.name,
            ready_for_prepare=compiled.ready_for_prepare,
            contracts=tuple(
                CarriedContractInput(
                    name=c.name, fields=c.schema_.fields, allow_extra=c.schema_.allow_extra
                )
                for c in compiled.contracts
            ),
            contract_in=definition.contract_in
            if definition
            else self._request.input.schema_.model_dump(),
            contract_out=definition.contract_out
            if definition
            else self._request.output.schema_.model_dump(),
            authority=RUNG_AUTHORITY[
                definition.terminal_capability
                if definition
                else derived_terminal_capability(compiled.nodes)
            ],
            nodes=compiled.nodes,
            edges=edges,
            slots=dict(self.slots),
            terminals=tuple(n.node_id for n in compiled.nodes if is_terminal_node(n)),
            returns=definition.returns if definition else "",
            budget=self._request.budget,
            hard_caps=self._request.hard_caps,
            errors=tuple(CompositionDiagnostic(**error.model_dump()) for error in compiled.errors),
            pending_checks=compiled.pending_checks,
            source=source,
            source_map=compiled.source_map,
            state_dependencies=tuple(
                ProcedureStateDependency(
                    node_id=n.node_id,
                    kind="claim" if n.reads_claims else "query",
                    selection=cast(ArtifactNameRef, n.claim_type or n.query),
                    cardinality=n.cardinality if n.reads_claims else "query",
                    limit=n.limit if n.reads_claims else None,
                    subject_kind=n.subject_kind if n.reads_claims else None,
                    selector=cast(
                        JsonValue,
                        normalize_canonical(n.subject_id if n.reads_claims else n.parameters),
                    ),
                )
                for n in compiled.nodes
                if isinstance(n, ReadNode)
            ),
            binding_requirements=tuple(
                ProcedureBindingRequirement(
                    slot=slot, resolved=None if source is None else source.bindings.get(slot)
                )
                for slot in sorted(set(self._bindings) | _binding_slots(self._request.text))
            ),
            branch_values=tuple(
                ProcedureBranchValue(
                    node_id=n.node_id,
                    kind=n.kind,
                    producers=n.sources if isinstance(n, SelectNode) else (),
                    predicate=n.predicate if isinstance(n, GuardNode) else None,
                    contract=n.contract_out if isinstance(n, SelectNode) else None,
                    successors=edges.get(n.node_id, {}),
                )
                for n in compiled.nodes
                if isinstance(n, (GuardNode, SelectNode))
            ),
            return_paths=tuple(
                ProcedureReturnPath(
                    node_id=n.node_id,
                    kind=terminal_kinds[n.kind],
                    contract=definition.contract_out,
                    required_authority=(
                        required_authority(TERMINAL_REQUIRED_RUNGS[n.kind])
                        if n.kind in TERMINAL_REQUIRED_RUNGS
                        else None
                    ),
                )
                for n in compiled.nodes
                if is_terminal_node(n) and n.kind in terminal_kinds
            )
            if definition
            else (),
            children=tuple(
                ProcedureChildCall(node_id=n.node_id, procedure=n.procedure)
                for n in compiled.nodes
                if isinstance(n, InvokeNode)
            ),
        )

    def build(self, *, world: World | None = None) -> ProcedureInput:
        """The shared Procedure authoring input; its slots are bound by a Line."""

        preview = self.preview(world=world)
        if not preview.ready_for_prepare:
            raise ProcedureCompositionError(preview)
        assert world is not None
        request = self._at(world)
        # This is the same symbolic authoring input used by HTTP/CLI/MCP. It
        # contains no resolved digests; prepare runs the shared backend compiler.
        definition: dict[str, object] = {
            "name": request.name,
            "source_request": request.model_dump(mode="json", by_alias=True),
        }
        contracts = tuple(
            CarriedContractInput(
                name=c.name, fields=c.schema_.fields, allow_extra=c.schema_.allow_extra
            )
            for c in {c.name: c for c in (request.input, request.output)}.values()
        )
        return ProcedureInput(
            kind="procedure",
            definition=definition,
            activation_policy=self.activation_policy,
            contracts=contracts,
        )


def _binding_slots(text: str) -> set[str]:
    try:
        parsed = ast.parse(text)
    except SyntaxError:
        return set()
    return {
        node.attr
        for node in ast.walk(parsed)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "bindings"
    }


def procedure(
    *,
    name: str,
    input: CarriedContractInput,
    output: CarriedContractInput,
    budget: ProcedureBudget,
    hard_caps: ProcedureHardCaps,
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot",
    description: str | None = None,
    slots: Mapping[str, ProcedureSlot | Mapping[str, object]] | None = None,
) -> Callable[[Callable[..., Any]], ProcedureSource]:
    """Declare Procedure source. ``slots`` maps each slot to ``{in, out, effect}``.

    Every declared slot must be used in the source as ``bindings.<slot>`` passed
    to ``source(...)`` or ``call(...)``. A Line binds each slot to one exact
    provider build and operation (or core fits it: exactly one live operation
    must serve its signature).
    """

    declared = {
        key: value if isinstance(value, ProcedureSlot) else ProcedureSlot.model_validate(value)
        for key, value in (slots or {}).items()
    }

    def decorate(function: Callable[..., Any]) -> ProcedureSource:
        try:
            lines, first_line = inspect.getsourcelines(function)
            text = textwrap.dedent("".join(lines))
            parsed = ast.parse(text)
            selected = next(
                node
                for node in parsed.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
        except (OSError, TypeError, SyntaxError, StopIteration) as exc:
            raise ValueError(
                "Procedure source is unavailable; define it in an inspectable Python module"
            ) from exc
        text = "\n".join(text.splitlines()[selected.lineno - 1 :]) + "\n"
        first_line += selected.lineno - 1
        referenced = {node.id for node in ast.walk(selected) if isinstance(node, ast.Name)}
        visible = dict(function.__globals__)
        # Capture declared schema objects from enclosing authoring helpers too;
        # no closure code is executed and no arbitrary closure value is retained.
        visible.update(inspect.getclosurevars(function).nonlocals)
        contracts = {
            key: _contract(value)
            for key, value in visible.items()
            if key in referenced and isinstance(value, CarriedContractInput)
        }
        used = _binding_slots(text)
        unused = sorted(set(declared) - used)
        if unused:
            raise ValueError(f"Declared slots the source never uses: {unused}")
        for slot in declared:
            if not slot.isidentifier():
                raise ValueError("slots map a binding name to its {in, out, effect} signature")
        # Only schema data crosses the boundary. No functions, closures or module objects.
        return ProcedureSource(
            ProcedureSourceRequest(
                name=name,
                text=text,
                filename=inspect.getsourcefile(function) or "<procedure>",
                first_line=first_line,
                function=function.__name__,
                input=_contract(input),
                output=_contract(output),
                contracts=contracts,
                budget=budget,
                hard_caps=hard_caps,
                description=description,
            ),
            activation_policy=activation_policy,
            slots=MappingProxyType(declared),
        )

    return decorate


def _intrinsic(*args: object, **kwargs: object) -> Any:
    raise TypeError(
        "Procedure operations are source intrinsics; use them inside @procedure, "
        "never execute them directly"
    )


query = _intrinsic
source = _intrinsic
call = _intrinsic
require = _intrinsic
invoke = _intrinsic
emit_capture = _intrinsic
claim_candidate = _intrinsic
propose_change_set = _intrinsic
settle_change_set = _intrinsic
halt = _intrinsic

__all__ = [
    "ProcedureSource",
    "procedure",
    "query",
    "source",
    "call",
    "require",
    "invoke",
    "emit_capture",
    "claim_candidate",
    "propose_change_set",
    "settle_change_set",
    "halt",
]
