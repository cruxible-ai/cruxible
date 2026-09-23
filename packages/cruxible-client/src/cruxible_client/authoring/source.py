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
    ProviderBinding,
)
from cruxible_client.authoring.queries import QueryBinding
from cruxible_client.authoring.sdk_types import ProcedureRef, QueryRef
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.procedures.models import (
    RUNG_AUTHORITY,
    ProcedureBudgetV3,
    ProcedureHardCapsV3,
    derived_terminal_capability,
)
from cruxible_client.contracts.procedures.source_program import SourceContract
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreviewRequestV1,
    ProcedureSourceRequestV1,
    SourceProcedureSelection,
    SourceProviderSelection,
    SourceQuerySelection,
    SourceSelection,
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
class ProcedureBlueprint:
    _request: ProcedureSourceRequestV1
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot"
    acquisition_policy: str | None = None
    _bindings: Mapping[str, ProviderBinding | QueryBinding | QueryRef | ProcedureRef] = field(
        default_factory=lambda: MappingProxyType({})
    )

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
    def budget(self) -> ProcedureBudgetV3:
        return self._request.budget.model_copy(deep=True)

    @property
    def hard_caps(self) -> ProcedureHardCapsV3:
        return self._request.hard_caps.model_copy(deep=True)

    @property
    def description(self) -> str | None:
        return self._request.description

    @property
    def bindings(self) -> Mapping[str, ProviderBinding | QueryBinding | QueryRef | ProcedureRef]:
        return self._bindings

    def __call__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "A Procedure blueprint is source, not executable Python. "
            "Use preview/build or run its accepted Procedure."
        )

    def bind(
        self, **bindings: ProviderBinding | QueryBinding | QueryRef | ProcedureRef
    ) -> ProcedureBlueprint:
        unknown = set(bindings) - _binding_slots(self.source)
        if unknown:
            raise ValueError(f"Unknown Procedure binding slots: {sorted(unknown)}")
        for name, value in bindings.items():
            if not name.isidentifier() or not isinstance(
                value, (ProviderBinding, QueryBinding, QueryRef, ProcedureRef)
            ):
                raise TypeError("Bindings require named accepted provider/query/Procedure handles")
        return replace(self, _bindings=MappingProxyType(dict(self._bindings, **bindings)))

    def _at(self, world: World) -> ProcedureSourceRequestV1:
        selections: dict[str, SourceSelection] = {}
        for name, value in self._bindings.items():
            if isinstance(value, ProviderBinding):
                if value.coordinate is None:
                    raise ValueError("Select source providers through pb.provider_binding()")
                world._playbill._assert_coordinate(value.coordinate)
                selections[name] = SourceProviderSelection(
                    provider=value.provider, interface=value.interface
                )
            elif isinstance(value, QueryBinding):
                world._playbill._assert_coordinate(value.ref.coordinate)
                selections[name] = SourceQuerySelection(name=value.ref.address)
            elif isinstance(value, QueryRef):
                world._playbill._assert_coordinate(value.coordinate)
                selections[name] = SourceQuerySelection(name=value.address)
            else:
                world._playbill._assert_coordinate(value.coordinate)
                selections[name] = SourceProcedureSelection(name=value.address)
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
                acquisition_policy=self.acquisition_policy,
                nodes=(),
                returns="",
                budget=self.budget,
                hard_caps=self.hard_caps,
                binding_requirements=tuple(
                    ProcedureBindingRequirement(slot=s) for s in sorted(_binding_slots(self.source))
                ),
                errors=(
                    CompositionDiagnostic(
                        code="playbill.source.context_required",
                        message="Supply world=pb.world() for backend source compilation.",
                    ),
                ),
            )
        request = ProcedureSourcePreviewRequestV1(
            source=self._at(world),
            at=AcceptedCoordinate.model_validate(world.coordinate.model_dump(mode="json")),
        )
        compiled = world._playbill._client.preview_playbill_procedure_source(
            world._playbill._instance_id, request=request
        )
        from cruxible_client.contracts.procedures.models import (
            TERMINAL_REQUIRED_RUNGS,
            ClaimTapNodeV6,
            GuardNodeV3,
            InvokeNodeV6,
            SelectNodeV6,
            StateTapNodeV6,
        )

        definition = compiled.definition
        source = None if definition is None else definition.source
        edges = compiled.edges
        terminal_kinds: dict[str, Literal["pure", "capture", "proposal", "halt"]] = {
            "return": "pure",
            "emit_capture": "capture",
            "propose_change_set": "proposal",
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
            acquisition_policy=self.acquisition_policy,
            nodes=compiled.nodes,
            edges=edges,
            providers={k: v for k, v in self._bindings.items() if isinstance(v, ProviderBinding)},
            terminals=tuple(n.node_id for n in compiled.nodes if n.kind in terminal_kinds),
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
                    kind="claim" if isinstance(n, ClaimTapNodeV6) else "query",
                    selection=n.claim_type if isinstance(n, ClaimTapNodeV6) else n.query,
                    cardinality=n.cardinality if isinstance(n, ClaimTapNodeV6) else "query",
                    limit=n.limit if isinstance(n, ClaimTapNodeV6) else None,
                    subject_kind=n.subject_kind if isinstance(n, ClaimTapNodeV6) else None,
                    selector=cast(
                        JsonValue,
                        normalize_canonical(
                            n.subject_id if isinstance(n, ClaimTapNodeV6) else n.parameters
                        ),
                    ),
                )
                for n in compiled.nodes
                if isinstance(n, (ClaimTapNodeV6, StateTapNodeV6))
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
                    producers=n.sources if isinstance(n, SelectNodeV6) else (),
                    predicate=n.predicate if isinstance(n, GuardNodeV3) else None,
                    contract=n.contract_out if isinstance(n, SelectNodeV6) else None,
                    successors=edges.get(n.node_id, {}),
                )
                for n in compiled.nodes
                if isinstance(n, (GuardNodeV3, SelectNodeV6))
            ),
            return_paths=tuple(
                ProcedureReturnPath(
                    node_id=n.node_id,
                    kind=terminal_kinds[n.kind],
                    contract=definition.contract_out,
                    required_terminal_rung=TERMINAL_REQUIRED_RUNGS.get(n.kind, 0),
                )
                for n in compiled.nodes
                if n.kind in terminal_kinds
            )
            if definition
            else (),
            children=tuple(
                ProcedureChildCall(node_id=n.node_id, procedure=n.procedure)
                for n in compiled.nodes
                if isinstance(n, InvokeNodeV6)
            ),
        )

    def build(self, *, world: World | None = None) -> ProcedureInput:
        preview = self.preview(world=world)
        if not preview.ready_for_prepare:
            raise ProcedureCompositionError(preview)
        assert world is not None
        request = self._at(world)
        # This is the same symbolic authoring input used by HTTP/CLI/MCP. It
        # contains no resolved digests; prepare runs the shared backend compiler.
        return ProcedureInput(
            kind="procedure",
            definition={
                "name": request.name,
                "source_request": request.model_dump(mode="json", by_alias=True),
            },
            activation_policy=self.activation_policy,
            acquisition_policy=self.acquisition_policy,
            contracts=tuple(
                CarriedContractInput(
                    name=c.name, fields=c.schema_.fields, allow_extra=c.schema_.allow_extra
                )
                for c in {c.name: c for c in (request.input, request.output)}.values()
            ),
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
    budget: ProcedureBudgetV3,
    hard_caps: ProcedureHardCapsV3,
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot",
    acquisition_policy: str | None = None,
    description: str | None = None,
) -> Callable[[Callable[..., Any]], ProcedureBlueprint]:
    def decorate(function: Callable[..., Any]) -> ProcedureBlueprint:
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
        # Only schema data crosses the boundary. No functions, closures or module objects.
        return ProcedureBlueprint(
            ProcedureSourceRequestV1(
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
            acquisition_policy=acquisition_policy,
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
halt = _intrinsic

__all__ = [
    "ProcedureBlueprint",
    "procedure",
    "query",
    "source",
    "call",
    "require",
    "invoke",
    "emit_capture",
    "claim_candidate",
    "propose_change_set",
    "halt",
]
