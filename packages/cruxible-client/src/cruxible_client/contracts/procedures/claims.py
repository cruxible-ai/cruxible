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


class ClaimsSubject(_StrictProcedureModel):
    kind: str
    key: dict[str, object] = Field(min_length=1)
    create: Literal["never", "if_absent"] = "never"
    within: object | None = None

    _templates = field_validator("key", "within", mode="before")(normalize_canonical)


CLAIMS_ON_DEFERRED = (
    "claims_on_deferred: a claims template writes Claims about its row's Subject only; "
    "a pair row acting on one of its ends' Claims (on:) is not supported in v1. Write the "
    "end's Claim from a claims step whose row is that end, or have an agent write it"
)


class ClaimsExpectByEnd(_StrictProcedureModel):
    """``expect: {by, ends}``: the run is complete for these ends of the pair part ``by``.

    The row Subject is a pair kind; ``by`` names one of its Subject-valued key
    parts (by predicate or short name) and ``ends`` references the list of
    those ends (text references or structured rows, as ``expect`` names
    Subjects). The pairs judged are those of each end that hold a live fact
    of a template's ClaimType, found through the pair index; each is then
    judged from its own Claims by the ordinary absence gates.
    """

    by: str = Field(min_length=1)
    ends: object

    _ends = field_validator("ends", mode="before")(normalize_canonical)


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
    when_absent: Literal["keep", "surface", "mirror", "inverse"] = Field(
        default="surface", exclude_if=lambda value: value == "surface"
    )

    _when = field_validator("when", mode="before")(_record_predicate)
    _templates = field_validator(
        "value", "qualifier", "effective_from", "effective_until", mode="before"
    )(normalize_canonical)

    @model_validator(mode="before")
    @classmethod
    def _on_deferred(cls, value: Any) -> Any:
        if isinstance(value, dict) and "on" in value:
            raise ValueError(CLAIMS_ON_DEFERRED)
        return value

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
    #: The Subjects whose absence this run judges; the enumeration is never
    #: authored: it is the extract at the root of ``for_each``'s filter/dedupe chain.
    expect: ClaimsExpectByEnd | object | None = Field(
        default=None, exclude_if=lambda value: value is None, union_mode="left_to_right"
    )
    subject: ClaimsSubject
    claims: tuple[ClaimTemplate, ...] = Field(min_length=1, max_length=256)

    _where = field_validator("where", mode="before")(_record_predicate)
    _templates = field_validator("for_each", mode="before")(normalize_canonical)

    @field_validator("expect", mode="before")
    @classmethod
    def _expect(cls, value: Any) -> Any:
        if isinstance(value, ClaimsExpectByEnd):
            return value
        if isinstance(value, dict) and "by" in value:
            return ClaimsExpectByEnd.model_validate(value)
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _absence_rails(self) -> ClaimsNode:
        if self.expect is None and any(
            "when_absent" in item.model_fields_set for item in self.claims
        ):
            raise ValueError("claims_absence_invalid: when_absent requires expect")
        return self


__all__ = [
    "CLAIMS_ON_DEFERRED",
    "ClaimTemplate",
    "ClaimsExpectByEnd",
    "ClaimsNode",
    "ClaimsSubject",
]
