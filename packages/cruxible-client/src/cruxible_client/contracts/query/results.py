"""Typed exact definitions returned by artifact queries."""

from pydantic import BaseModel, ConfigDict, model_validator

from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest, claim_type_path
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactAny,
    procedure_artifact_digest,
    procedure_path,
)


class QueryArtifactDefinitionV2(BaseModel):
    """The full definition and its exact accepted identity/path/version binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    identity: str
    path: str
    artifact_digest: str
    definition: ClaimType | ProcedureArtifactAny

    @model_validator(mode="after")
    def _binding(self) -> "QueryArtifactDefinitionV2":
        source = self.definition
        if isinstance(source, ClaimType):
            path, digest = claim_type_path(source.predicate), claim_type_digest(source).tagged
        else:
            path, digest = (
                procedure_path(source.identity.name),
                procedure_artifact_digest(source).tagged,
            )
        if (self.identity, self.path, self.artifact_digest) != (
            source.identity.qualified,
            path,
            digest,
        ):
            raise ValueError("query definition row does not reproduce its identity/path/digest")
        return self
