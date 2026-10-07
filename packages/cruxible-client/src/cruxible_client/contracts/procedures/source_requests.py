"""Symbolic source-authoring requests: callers never supply version digests."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.procedures.models import (
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProcedureNode,
)
from cruxible_client.contracts.procedures.source_program import (
    SourceContract,
    SourceDiagnostic,
    SourceMapEntry,
)
from cruxible_client.contracts.projection import AcceptedCoordinate


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceProviderSelection(_Closed):
    kind: Literal["provider"] = "provider"
    provider: str
    interface: str


class SourceQuerySelection(_Closed):
    kind: Literal["query"] = "query"
    name: str


class SourceProcedureSelection(_Closed):
    kind: Literal["procedure"] = "procedure"
    name: str


SourceSelection = Annotated[
    SourceProviderSelection | SourceQuerySelection | SourceProcedureSelection,
    Field(discriminator="kind"),
]


class ProcedureSourceRequest(_Closed):
    source_format: Literal["cruxible.procedure-source-request.v1"] = (
        "cruxible.procedure-source-request.v1"
    )
    name: str
    text: str
    filename: str
    first_line: int = Field(ge=1)
    function: str
    input: SourceContract
    output: SourceContract
    contracts: dict[str, SourceContract]
    bindings: dict[str, SourceSelection] = Field(default_factory=dict)
    budget: ProcedureBudget
    hard_caps: ProcedureHardCaps
    description: str | None = None


class ProcedureSourcePreviewRequest(_Closed):
    source: ProcedureSourceRequest
    at: AcceptedCoordinate


class ProcedureSourcePreview(_Closed):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")
    name: str
    coordinate: AcceptedCoordinate
    definition: ProcedureDefinition | None = None
    contracts: tuple[SourceContract, ...] = ()
    source_map: tuple[SourceMapEntry, ...] = ()
    errors: tuple[SourceDiagnostic, ...] = ()
    pending_checks: tuple[str, ...] = (
        "Admission verifies installation, authority and effective budgets.",
        "Runtime values must satisfy the pinned contracts. Preview executes no effects.",
    )

    @property
    def ready_for_prepare(self) -> bool:
        return self.definition is not None and not self.errors

    @property
    def nodes(self) -> tuple[ProcedureNode, ...]:
        return () if self.definition is None else self.definition.nodes

    @property
    def edges(self) -> dict[str, dict[str, str]]:
        if self.definition is None:
            return {}
        from cruxible_client.contracts.procedures.graph import analyze_procedure

        return analyze_procedure(self.definition).edges
