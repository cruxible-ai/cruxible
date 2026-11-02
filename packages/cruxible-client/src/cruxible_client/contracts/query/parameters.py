"""Schema-derived query parameter construction shared by every authoring frontend."""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.procedures.contracts import ProcedureContractValidationError
from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_client.contracts.query.values import coerce_query_value
from cruxible_client.contracts.records import Record, RecordConstructor
from cruxible_client.contracts.shapes import Contract, ContractField, parse_type


@dataclass(frozen=True)
class QueryParameters:
    """A constructor bound to one exact query's parameter declarations."""

    definition: QueryDefinition

    @property
    def schema(self) -> Contract:
        # Query null/default semantics intentionally differ from Contract fields.
        # Reuse query type checking after the common closed-record construction.
        return Contract(
            name="query.parameters",
            version=1,
            fields={
                item.name: ContractField(
                    type=parse_type(
                        {
                            "integer": "int",
                            "boolean": "bool",
                            "decimal": "decimal",
                            "timestamp": "datetime",
                            "subject_reference": "string",
                        }.get(item.value_type, item.value_type)
                    ),
                    optional=not item.required,
                )
                for item in self.definition.parameters
            },
        )

    def __call__(self, **fields: object) -> Record:
        supplied = {
            item.name: item.default
            for item in self.definition.parameters
            if item.default is not None
        }
        supplied.update(fields)
        result = RecordConstructor(self.schema)(**supplied)
        for declaration in self.definition.parameters:
            value = result.get(declaration.name, declaration.default)
            if value is not None and not coerce_query_value(value, declaration.value_type).ok:
                raise ProcedureContractValidationError(
                    f"{declaration.name}: expected {declaration.value_type}",
                    field_path=declaration.name,
                )
        return result
