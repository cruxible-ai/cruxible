"""Schema-derived query parameter construction shared by every authoring frontend."""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.contracts import ProcedureContractValidationError
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
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
