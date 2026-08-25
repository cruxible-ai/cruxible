"""Shared, strictly canonical contracts for locally declared projection blocks."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.grammar import QueryValueTypeV1
from cruxible_client.contracts.temporal import ensure_utc

PROJECTION_MARKER_GRAMMAR: Literal["playbill-projection-marker-grammar-v1"] = (
    "playbill-projection-marker-grammar-v1"
)
PROJECTION_QUERY_SEMANTIC_RESULT_DOMAIN = "playbill-projection-query-semantic-result-v1"
PROJECTION_QUERY_PARAMETER_DOMAIN = "playbill-query-parameters-v1"
MAX_PROJECTION_SOURCE_BYTES = 4 * 1024 * 1024
MAX_PROJECTION_BLOCKS_PER_SOURCE = 128
MAX_PROJECTION_STAMP_BYTES = 16 * 1024
MAX_PROJECTION_BACKINGS_PER_BLOCK = 64
MAX_PROJECTION_SCAN_BYTES = 32 * 1024 * 1024
MAX_PROJECTION_CARDS_PER_SOURCE = 256


class _StrictDeclaredBlockModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillPresentationPolicyV1(_StrictDeclaredBlockModel):
    """Local-only suppression policy for presentation diagnostics."""

    tag: Literal["playbill-presentation-policy-v1"] = "playbill-presentation-policy-v1"
    archival_source_ids: tuple[str, ...] = ()

    @field_validator("archival_source_ids")
    @classmethod
    def _source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        source_pattern = r"^[a-z][a-z0-9_.-]{0,127}$"
        if any(re.fullmatch(source_pattern, item) is None for item in value):
            raise ValueError("presentation policy contains an invalid source ID")
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("archival source IDs must be UTF-8-byte-sorted and unique")
        return value


PlaybillPresentationPolicyNoteV1: TypeAlias = Literal[
    "presentation_policy_malformed",
    "presentation_policy_path_escape",
    "presentation_policy_unknown_source_id",
    "presentation_policy_unreadable",
]


class ProjectionClaimBackingV1(_StrictDeclaredBlockModel):
    tag: Literal["playbill-projection-claim-backing-v1"] = "playbill-projection-claim-backing-v1"
    identity: ArtifactIdentity
    statement_digest: str

    @field_validator("statement_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _identity(self) -> ProjectionClaimBackingV1:
        if self.identity.kind != "Claim":
            raise ValueError("a projection Claim backing must identify a Claim")
        return self


class ProjectionResolvedParameterBindingV1(_StrictDeclaredBlockModel):
    """The exact existing query-parameter-binding wire spelling, shared by both sides."""

    tag: Literal["playbill-query-parameter-binding-v1"] = "playbill-query-parameter-binding-v1"
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value_type: QueryValueTypeV1
    value: object = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)


def projection_parameter_digest(
    parameters: tuple[ProjectionResolvedParameterBindingV1, ...],
) -> str:
    return typed_digest(
        Sha256Value,
        PROJECTION_QUERY_PARAMETER_DOMAIN,
        {"parameters": [item.model_dump(mode="json") for item in parameters]},
    ).tagged


class ProjectionQueryBackingV1(_StrictDeclaredBlockModel):
    tag: Literal["playbill-projection-query-backing-v1"] = "playbill-projection-query-backing-v1"
    identity: ArtifactIdentity
    definition_digest: str
    resolved_parameter_bindings: tuple[ProjectionResolvedParameterBindingV1, ...] = ()
    canonical_param_digest: str
    declared_evaluation_time: datetime
    semantic_result_digest: str

    @field_validator("definition_digest", "canonical_param_digest", "semantic_result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("declared_evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("a projection query backing requires an absolute evaluation time")
        return ensure_utc(value)

    @model_validator(mode="after")
    def _bindings(self) -> ProjectionQueryBackingV1:
        if self.identity.kind != "QueryDefinition":
            raise ValueError("a projection query backing must identify a QueryDefinition")
        names = tuple(item.name for item in self.resolved_parameter_bindings)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("projection query parameter bindings must be sorted and unique")
        if (
            projection_parameter_digest(self.resolved_parameter_bindings)
            != self.canonical_param_digest
        ):
            raise ValueError("projection query parameter digest does not reproduce its bindings")
        return self


ProjectionBackingV1: TypeAlias = Annotated[
    ProjectionClaimBackingV1 | ProjectionQueryBackingV1,
    Field(discriminator="tag"),
]


class ProjectionBlockStampV1(_StrictDeclaredBlockModel):
    tag: Literal["playbill-projection-stamp-v1"] = "playbill-projection-stamp-v1"
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    block_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    declared_generation: int = Field(ge=0)
    declared_coordinate: AcceptedCoordinate
    backing: tuple[ProjectionBackingV1, ...] = Field(
        min_length=1,
        max_length=MAX_PROJECTION_BACKINGS_PER_BLOCK,
    )
    body_digest: str
    grammar_version: Literal["playbill-projection-marker-grammar-v1"] = PROJECTION_MARKER_GRAMMAR

    @field_validator("body_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("backing")
    @classmethod
    def _backing(cls, value: tuple[ProjectionBackingV1, ...]) -> tuple[ProjectionBackingV1, ...]:
        identities = tuple(item.identity.qualified for item in value)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("projection block backings must be sorted and unique by identity")
        return value


class ProjectionMarkerSummaryV1(_StrictDeclaredBlockModel):
    stamp: ProjectionBlockStampV1
    observed_body_digest: str
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)

    @field_validator("observed_body_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _window(self) -> ProjectionMarkerSummaryV1:
        if self.end_byte <= self.start_byte:
            raise ValueError("projection marker summary byte range must be increasing")
        return self


def projection_query_semantic_result_digest(result: object) -> str:
    """Commit result meaning only, excluding coordinate, clock, receipt, and prose."""

    payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    if not isinstance(payload, dict):
        raise ValueError("a projection query semantic result must be an object")
    fields = (
        "rows",
        "conflicts",
        "result_shape",
        "result_cardinality",
        "result_binding",
        "dedupe",
    )
    if any(field not in payload for field in fields):
        raise ValueError("a projection query semantic result omits a required result field")
    return typed_digest(
        Sha256Value,
        PROJECTION_QUERY_SEMANTIC_RESULT_DOMAIN,
        {field: payload[field] for field in fields},
    ).tagged


__all__ = [
    "MAX_PROJECTION_BACKINGS_PER_BLOCK",
    "MAX_PROJECTION_BLOCKS_PER_SOURCE",
    "MAX_PROJECTION_CARDS_PER_SOURCE",
    "MAX_PROJECTION_SCAN_BYTES",
    "MAX_PROJECTION_SOURCE_BYTES",
    "MAX_PROJECTION_STAMP_BYTES",
    "PROJECTION_MARKER_GRAMMAR",
    "PROJECTION_QUERY_PARAMETER_DOMAIN",
    "PROJECTION_QUERY_SEMANTIC_RESULT_DOMAIN",
    "PlaybillPresentationPolicyV1",
    "ProjectionBackingV1",
    "ProjectionBlockStampV1",
    "ProjectionClaimBackingV1",
    "ProjectionMarkerSummaryV1",
    "ProjectionQueryBackingV1",
    "ProjectionResolvedParameterBindingV1",
    "projection_parameter_digest",
    "projection_query_semantic_result_digest",
]
