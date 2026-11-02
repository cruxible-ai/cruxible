"""Core-owned Claim templates. Data can supply values, never write instructions."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactNameRef
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.procedures.model_base import _StrictProcedureModel
from cruxible_client.contracts.shapes import (
    Predicate,
    as_predicate,
    claim_rationale,
    predicate_context_problem,
)


@lru_cache(maxsize=1024)
def rationale_references(value: str) -> tuple[str, ...]:
    """Only closed reference placeholders interpolate in a rationale."""
    return tuple(re.findall(r"\{(\$(?:item|steps|params|input)(?:\.[a-zA-Z0-9_]+)*)\}", value))


def _record_predicate(value: Any) -> object:
    if value is None:
        return None
    predicate = as_predicate(value)
    problem = predicate_context_problem(predicate, "record")
    if problem is not None:
        raise ValueError(problem)
    return predicate


class ClaimsFeatureDeferred(ValueError):
    """An absence instruction cannot run before its safety rails ship."""

    code = "cruxible.procedure.claims_feature_deferred"


def refuse_absence(value: object) -> object:
    if isinstance(value, dict):
        for name in ("when_absent", "covers", "enumeration", "mirror"):
            if name in value:
                raise ClaimsFeatureDeferred(
                    f"{ClaimsFeatureDeferred.code}: {name} lands in B4b; omit it until "
                    "coverage, completeness and witness rails are available"
                )
    return value


class ClaimsSubject(_StrictProcedureModel):
    kind: str
    key: dict[str, object] = Field(min_length=1)
    create: Literal["never", "if_absent"] = "never"
    within: object | None = None

    _templates = field_validator("key", "within", mode="before")(normalize_canonical)


class ClaimTemplate(_StrictProcedureModel):
    claim_type: ArtifactNameRef = Field(alias="type")
    value: object
    qualifier: object = None
    effective_from: object = None
    effective_until: object = None
    when: Predicate | None = None
    role: ClaimRole | None = None
    replaces: tuple[ClaimRole, ...] | None = None
    inputs: tuple[object, ...] = ()
    rationale: str = "Claim from declared Procedure template."

    _absence = model_validator(mode="before")(refuse_absence)
    _when = field_validator("when", mode="before")(_record_predicate)
    _templates = field_validator(
        "value", "qualifier", "effective_from", "effective_until", mode="before"
    )(normalize_canonical)

    @field_validator("inputs", mode="before")
    @classmethod
    def _inputs(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            return tuple(normalize_canonical(item) for item in value)
        return value

    @model_validator(mode="after")
    def _derivation(self) -> ClaimTemplate:
        if self.inputs and self.role != "derivation":
            raise ValueError("claims_inputs_invalid: inputs require role: derivation")
        if self.replaces is not None and len(set(self.replaces)) != len(self.replaces):
            raise ValueError("replaces must contain distinct roles")
        return self

    _rationale_line = field_validator("rationale")(claim_rationale)


class ClaimsNode(_StrictProcedureModel):
    kind: Literal["claims"] = "claims"
    node_id: str
    for_each: object
    where: Predicate | None = None
    subject: ClaimsSubject
    claims: tuple[ClaimTemplate, ...] = Field(min_length=1, max_length=256)

    _absence = model_validator(mode="before")(refuse_absence)
    _where = field_validator("where", mode="before")(_record_predicate)
    _templates = field_validator("for_each", mode="before")(normalize_canonical)


__all__ = ["ClaimTemplate", "ClaimsFeatureDeferred", "ClaimsNode", "ClaimsSubject"]
