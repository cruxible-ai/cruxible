"""Retained, non-executing inputs to the versioned Procedure source compiler."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import ArtifactDigest
from cruxible_client.contracts.claim_type_structure import ClaimTypeStructure
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.provider_operations import OperationEffect, ProcedureSlot
from cruxible_client.contracts.query.definitions import NamedQueryDefinition, QueryDefinition
from cruxible_client.contracts.shapes import ContractRef


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class SourceSpan(_Closed):
    filename: str
    line: int = Field(ge=1)
    column: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=0)


class SourceContract(_Closed):
    model_config = ConfigDict(serialize_by_alias=True)

    name: str
    schema_: ContractSchema = Field(alias="schema")


class SourceSlotBinding(_Closed):
    """A slot the source calls: its signature, and the compiler's typing views of it.

    ``in`` and ``out`` pin the slot's Contracts; ``input`` and ``output`` are
    the compiler's typing views of those Contracts (derived by core when the
    source is resolved, never authored). A Line binds the slot to one exact
    build and operation.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_mode_override="validation",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    kind: Literal["slot"] = "slot"
    in_: ContractRef = Field(alias="in")
    out: ContractRef
    effect: OperationEffect
    input: ContractSchema
    output: ContractSchema

    def signature(self) -> ProcedureSlot:
        return ProcedureSlot.model_validate(
            {"in": self.in_, "out": self.out, "effect": self.effect}
        )


class SourceQueryBinding(_Closed):
    kind: Literal["query"] = "query"
    name: str
    version: str
    definition: QueryDefinition

    _versions = field_validator("version", check_fields=False)(
        lambda value: ArtifactDigest.from_tagged(value).tagged
    )


class SourceProcedureBinding(_Closed):
    kind: Literal["procedure"] = "procedure"
    name: str
    version: str
    input: ContractSchema
    output: ContractSchema
    capture_terminal: bool = False
    required_terminal_rung: int = Field(default=0, ge=0, le=3)

    _versions = field_validator("version", check_fields=False)(
        lambda value: ArtifactDigest.from_tagged(value).tagged
    )


SourceBinding = Annotated[
    SourceSlotBinding | SourceQueryBinding | SourceProcedureBinding,
    Field(discriminator="kind"),
]


class SourceClaimType(_Closed):
    version: str
    structure: ClaimTypeStructure

    _versions = field_validator("version", check_fields=False)(
        lambda value: ArtifactDigest.from_tagged(value).tagged
    )


class ProcedureSourceProgram(_Closed):
    """Source plus explicit data dependencies; no closures or executable imports."""

    rules: Literal["cruxible.procedure-source.v1", "cruxible.procedure-source.v2"] = (
        "cruxible.procedure-source.v1"
    )
    text: str
    filename: str
    first_line: int = Field(default=1, ge=1)
    function: str
    contracts: dict[str, SourceContract] = Field(default_factory=dict)
    bindings: dict[str, SourceBinding] = Field(default_factory=dict)
    claim_types: dict[str, SourceClaimType] = Field(default_factory=dict)
    capture_contracts: dict[str, str] = Field(default_factory=dict)
    subject_kinds: tuple[str, ...] = ()


class SourceMapEntry(_Closed):
    node_id: str
    span: SourceSpan


class SourceDiagnostic(_Closed):
    code: str
    message: str
    span: SourceSpan
    hint: str | None = None
    related_spans: tuple[SourceSpan, ...] = ()


class NamedSourceQueryBinding(SourceQueryBinding):
    definition: NamedQueryDefinition
    version: ClassVar[None] = None  # type: ignore[misc, assignment]


class NamedSourceProcedureBinding(SourceProcedureBinding):
    version: ClassVar[None] = None  # type: ignore[misc, assignment]


class NamedSourceClaimType(SourceClaimType):
    version: ClassVar[None] = None  # type: ignore[misc, assignment]


class NamedProcedureSourceProgram(ProcedureSourceProgram):
    syntax_rules: Literal["cruxible.procedure-source.v1", "cruxible.procedure-source.v2"] = (
        "cruxible.procedure-source.v2"
    )
    rules: Literal["cruxible.procedure-source.v3"] = "cruxible.procedure-source.v3"  # type: ignore[assignment]
    bindings: dict[
        str,
        Annotated[
            SourceSlotBinding | NamedSourceQueryBinding | NamedSourceProcedureBinding,
            Field(discriminator="kind"),
        ],
    ] = Field(default_factory=dict)  # type: ignore[assignment]
    claim_types: dict[str, NamedSourceClaimType] = Field(default_factory=dict)  # type: ignore[assignment]
    capture_contracts: tuple[str, ...] = ()  # type: ignore[assignment]
