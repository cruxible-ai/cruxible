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
from typing import TYPE_CHECKING, Any, Callable, Literal

from cruxible_client.authoring.inputs import CarriedContractInput, ProcedureInput
from cruxible_client.authoring.procedures import ProviderBinding
from cruxible_client.authoring.queries import QueryBinding
from cruxible_client.authoring.sdk_types import ProcedureRef, QueryRef
from cruxible_client.contracts.procedures.models import ProcedureBudgetV3, ProcedureHardCapsV3
from cruxible_client.contracts.procedures.source_program import SourceContract
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreviewRequestV1,
    ProcedureSourcePreviewV1,
    ProcedureSourceRequestV1,
    SourceProcedureSelection,
    SourceProviderSelection,
    SourceQuerySelection,
    SourceSelection,
)

if TYPE_CHECKING:
    from cruxible_client.authoring.world import World


def _contract(value: CarriedContractInput) -> SourceContract:
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

    def __call__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "A Procedure blueprint is source, not executable Python. "
            "Use preview/build or run its accepted Procedure."
        )

    def bind(
        self, **bindings: ProviderBinding | QueryBinding | QueryRef | ProcedureRef
    ) -> ProcedureBlueprint:
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

    def preview(self, *, world: World) -> ProcedureSourcePreviewV1:
        from cruxible_client.contracts.projection import AcceptedCoordinate

        request = ProcedureSourcePreviewRequestV1(
            source=self._at(world),
            at=AcceptedCoordinate.model_validate(world.coordinate.model_dump(mode="json")),
        )
        return world._playbill._client.preview_playbill_procedure_source(
            world._playbill._instance_id, request=request
        )

    def build(self, *, world: World) -> ProcedureInput:
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


def procedure(
    *,
    name: str,
    input: CarriedContractInput,
    output: CarriedContractInput,
    budget: ProcedureBudgetV3,
    hard_caps: ProcedureHardCapsV3,
    terminal_capability: Literal[1, 2, 3] = 1,
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
                terminal_capability=terminal_capability,
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
