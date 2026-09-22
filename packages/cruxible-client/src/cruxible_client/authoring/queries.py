"""Exact accepted query handles and schema-derived parameter construction."""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.authoring.sdk_types import QueryRef
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.contracts import ProcedureContractValidationError
from cruxible_client.contracts.query.definitions import QueryDefinitionV1, query_definition_digest
from cruxible_client.contracts.query.values import coerce_query_value
from cruxible_client.contracts.records import Record, RecordConstructor


@dataclass(frozen=True)
class QueryParameters:
    """A constructor bound to one exact query's parameter declarations."""

    definition: QueryDefinitionV1

    @property
    def schema(self) -> ContractSchema:
        # Query null/default semantics intentionally differ from Contract fields.
        # Reuse query type checking after the common closed-record construction.
        return ContractSchema(
            fields={
                item.name: PropertySchema(
                    type="json", optional=not item.required, default=item.default
                )
                for item in self.definition.parameters
            }
        )

    def __call__(self, **fields: object) -> Record:
        result = RecordConstructor(self.schema)(**fields)
        for declaration in self.definition.parameters:
            value = result.get(declaration.name, declaration.default)
            if value is not None and not coerce_query_value(value, declaration.value_type).ok:
                raise ProcedureContractValidationError(
                    f"{declaration.name}: expected {declaration.value_type}",
                    field_path=declaration.name,
                )
        return result


@dataclass(frozen=True)
class QueryBinding:
    ref: QueryRef
    definition: QueryDefinitionV1
    artifact_digest: str

    def __post_init__(self) -> None:
        if self.definition.identity.name != self.ref.address.removeprefix("QueryDefinition:"):
            raise ValueError("query binding identity differs from its accepted definition")
        if query_definition_digest(self.definition).tagged != self.artifact_digest:
            raise ValueError("query binding does not reproduce its accepted digest")
        object.__setattr__(self, "definition", self.definition.model_copy(deep=True))

    @property
    def parameters(self) -> QueryParameters:
        return QueryParameters(self.definition.model_copy(deep=True))
