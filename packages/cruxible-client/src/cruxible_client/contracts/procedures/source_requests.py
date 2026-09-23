"""Symbolic source-authoring requests: callers never supply version digests."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.procedures.models import (
    ProcedureBudgetV3,
    ProcedureDefinitionV6,
    ProcedureHardCapsV3,
    ProcedureNodeV6,
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


class ProcedureSourceRequestV1(_Closed):
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
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    description: str | None = None


class ProcedureSourcePreviewRequestV1(_Closed):
    source: ProcedureSourceRequestV1
    at: AcceptedCoordinate


class ProcedureSourcePreviewV1(_Closed):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")
    name: str
    coordinate: AcceptedCoordinate
    definition: ProcedureDefinitionV6 | None = None
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
    def nodes(self) -> tuple[ProcedureNodeV6, ...]:
        return () if self.definition is None else self.definition.nodes

    @property
    def edges(self) -> dict[str, dict[str, str]]:
        if self.definition is None:
            return {}
        from cruxible_client.contracts.procedures.graph import analyze_procedure_v4

        return analyze_procedure_v4(self.definition).edges
