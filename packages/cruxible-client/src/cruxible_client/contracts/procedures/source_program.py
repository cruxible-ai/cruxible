"""Retained, non-executing inputs to the versioned Procedure source compiler."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import ArtifactDigest
from cruxible_client.contracts.claim_type_structure import ClaimTypeStructure
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.provider_contracts import ProviderOperationContractV1
from cruxible_client.contracts.query.definitions import QueryDefinitionV1


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


class SourceProviderBinding(_Closed):
    kind: Literal["provider"] = "provider"
    provider: str
    provider_version: str
    interface: str
    interface_version: str
    interface_digest: str
    implementation_digest: str
    effect_class: Literal["none", "external_read", "external_mutation"]
    operation: ProviderOperationContractV1

    _versions = field_validator(
        "provider_version", "interface_version", "interface_digest", "implementation_digest"
    )(lambda value: ArtifactDigest.from_tagged(value).tagged)


class SourceQueryBinding(_Closed):
    kind: Literal["query"] = "query"
    name: str
    version: str
    definition: QueryDefinitionV1

    _versions = field_validator("version")(lambda value: ArtifactDigest.from_tagged(value).tagged)


class SourceProcedureBinding(_Closed):
    kind: Literal["procedure"] = "procedure"
    name: str
    version: str
    input: ContractSchema
    output: ContractSchema
    capture_terminal: bool = False
    required_terminal_rung: int = Field(default=0, ge=0, le=3)

    _versions = field_validator("version")(lambda value: ArtifactDigest.from_tagged(value).tagged)


SourceBinding = Annotated[
    SourceProviderBinding | SourceQueryBinding | SourceProcedureBinding,
    Field(discriminator="kind"),
]


class SourceClaimType(_Closed):
    version: str
    structure: ClaimTypeStructure

    _versions = field_validator("version")(lambda value: ArtifactDigest.from_tagged(value).tagged)


class ProcedureSourceV1(_Closed):
    """Source plus explicit data dependencies; no closures or executable imports."""

    rules: Literal["cruxible.procedure-source.v1"] = "cruxible.procedure-source.v1"
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
