"""Exact accepted query handles and schema-derived parameter construction."""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.authoring.sdk_types import QueryRef
from cruxible_client.contracts.query.definitions import QueryDefinitionV1, query_definition_digest
from cruxible_client.contracts.query.parameters import QueryParameters


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
