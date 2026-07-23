"""Schema and persisted types for governed procedures."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_core.config.schema import (
    AssertSpec,
    ContractReference,
    WorkflowStepSchema,
    reject_reserved_property_equality_condition_keys,
    workflow_step_kind,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.primitives import canonical_json, new_id
from cruxible_core.temporal import utc_now

ProcedureStatus = Literal["pending", "live", "rejected", "retired"]
ProcedureTier = Literal["governed_write", "graph_write", "admin"]
ProcedureRunStatus = Literal["started", "finalized"]
ProcedureRunVerdict = Literal["succeeded", "failed", "refused", "budget_exceeded"]

MAX_PROCEDURE_STEPS = 100
"""Maximum stored step definitions, counting repeat containers and nested steps once."""

MAX_PROCEDURE_EXPANDED_STEPS = 500
"""Maximum steps after expanding every repeat to its declared maximum attempts."""

MAX_PROCEDURE_EXPANDED_PROVIDER_CALLS = 250
"""Maximum provider calls after expanding every repeat to its maximum attempts."""

MAX_PROCEDURE_REPEAT_ATTEMPTS = 25
"""Maximum attempts accepted by one bounded repeat step."""

_TOP_LEVEL_STEP_KINDS = frozenset(
    {
        "query",
        "provider",
        "assert",
        "assert_not_truncated",
        "assert_count",
        "assert_exists",
        "shape_items",
        "join_items",
        "filter_items",
        "aggregate_items",
        "dedupe_items",
    }
)
_NESTED_STEP_KINDS = _TOP_LEVEL_STEP_KINDS - {"query"}


class ProcedureBudget(BaseModel):
    """Required hard bounds for a procedure invocation."""

    wall_clock_s: float = Field(gt=0, le=600)
    max_provider_calls: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class ProcedureRepeatSpec(BaseModel):
    """One statically bounded procedure repeat body.

    A repeat executes at most 25 attempts. Its nested body deliberately excludes
    queries and repeat itself: only provider calls, assert-family invariants, and
    item-shaping steps are accepted. ``until`` uses the existing assert condition
    shape and is evaluated against the current attempt's outputs.
    """

    max_attempts: int = Field(ge=1, le=MAX_PROCEDURE_REPEAT_ATTEMPTS)
    until: AssertSpec
    steps: list[WorkflowStepSchema] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_nested_step_subset(self) -> ProcedureRepeatSpec:
        disallowed = sorted(
            {
                workflow_step_kind(step)
                for step in self.steps
                if workflow_step_kind(step) not in _NESTED_STEP_KINDS
            }
        )
        if disallowed:
            allowed = ", ".join(sorted(_NESTED_STEP_KINDS))
            raise ValueError(
                "repeat nested steps may only use provider, assert-family, and "
                f"item-shaping kinds ({allowed}); found {disallowed}"
            )
        _validate_unique_step_ids(self.steps, context="repeat nested steps")
        nested_aliases = {step.as_ for step in self.steps if step.as_ is not None}
        for ref in _workflow_references([self.until.left, self.until.right]):
            if not ref.startswith("$steps."):
                raise ValueError(
                    "repeat.until may reference only current-attempt '$steps.<alias>' outputs"
                )
            alias = ref[len("$steps.") :].split(".", 1)[0].split("[", 1)[0]
            if alias not in nested_aliases:
                raise ValueError(
                    f"repeat.until reference '{ref}' does not name a current-attempt "
                    "nested step alias"
                )
        return self


class ProcedureRepeatStepSchema(BaseModel):
    """Top-level procedure repeat step with an explicit output alias."""

    id: str
    repeat: ProcedureRepeatSpec
    as_: str = Field(alias="as")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


ProcedureStepSchema = WorkflowStepSchema | ProcedureRepeatStepSchema


class ProcedureStaticExpansion(BaseModel):
    """Review-visible static upper bounds computed from a procedure body."""

    total_steps: int
    expanded_steps: int
    expanded_provider_calls: int

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureDefinition(BaseModel):
    """Agent-proposable utility plan constrained to the procedure step subset.

    ``type`` is intentionally absent and forbidden: procedure bodies always use
    utility workflow semantics. Validation caps stored definitions at 100 steps,
    their repeat-expanded execution at 500 steps, and their repeat-expanded
    provider calls at 250. Refusal messages include all three computed counts so
    reviewers can see which bound the definition exceeded.
    """

    name: str
    description: str | None = None
    contract_in: ContractReference = "cruxible.EmptyInput"
    contract_out: ContractReference | None = None
    steps: list[ProcedureStepSchema] = Field(min_length=1)
    returns: str
    precondition: dict[str, str | int | float | bool]
    budget: ProcedureBudget
    declared_tier: ProcedureTier = "governed_write"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_definition(self) -> ProcedureDefinition:
        if not self.name.strip():
            raise ValueError("procedure name must be non-empty")
        reject_reserved_property_equality_condition_keys(self.precondition)

        disallowed = sorted(
            {
                workflow_step_kind(step)
                for step in self.steps
                if isinstance(step, WorkflowStepSchema)
                and workflow_step_kind(step) not in _TOP_LEVEL_STEP_KINDS
            }
        )
        if disallowed:
            allowed = ", ".join(sorted(_TOP_LEVEL_STEP_KINDS | {"repeat"}))
            raise ValueError(
                f"procedure steps may only use {allowed}; found disallowed kinds {disallowed}"
            )
        _validate_unique_step_ids(self.steps, context="procedure steps")

        expansion = self.static_expansion()
        refusals: list[str] = []
        if expansion.total_steps > MAX_PROCEDURE_STEPS:
            refusals.append(f"total step ceiling is {MAX_PROCEDURE_STEPS}")
        if expansion.expanded_steps > MAX_PROCEDURE_EXPANDED_STEPS:
            refusals.append(f"expanded step ceiling is {MAX_PROCEDURE_EXPANDED_STEPS}")
        if expansion.expanded_provider_calls > MAX_PROCEDURE_EXPANDED_PROVIDER_CALLS:
            refusals.append(
                f"expanded provider-call ceiling is {MAX_PROCEDURE_EXPANDED_PROVIDER_CALLS}"
            )
        if self.budget.max_provider_calls < expansion.expanded_provider_calls:
            refusals.append(
                "budget.max_provider_calls must be at least the expanded provider-call count"
            )
        if refusals:
            counts = (
                f"computed total_steps={expansion.total_steps}, "
                f"expanded_steps={expansion.expanded_steps}, "
                f"expanded_provider_calls={expansion.expanded_provider_calls}, "
                f"declared max_provider_calls={self.budget.max_provider_calls}"
            )
            raise ValueError(f"procedure static expansion refused: {counts}; {'; '.join(refusals)}")
        return self

    def static_expansion(self) -> ProcedureStaticExpansion:
        """Return the maximum statically expanded step/provider counts."""
        total_steps = 0
        expanded_steps = 0
        expanded_provider_calls = 0
        for step in self.steps:
            if isinstance(step, ProcedureRepeatStepSchema):
                nested_count = len(step.repeat.steps)
                nested_provider_count = sum(
                    workflow_step_kind(nested) == "provider" for nested in step.repeat.steps
                )
                total_steps += 1 + nested_count
                expanded_steps += 1 + step.repeat.max_attempts * nested_count
                expanded_provider_calls += step.repeat.max_attempts * nested_provider_count
                continue
            total_steps += 1
            expanded_steps += 1
            if workflow_step_kind(step) == "provider":
                expanded_provider_calls += 1
        return ProcedureStaticExpansion(
            total_steps=total_steps,
            expanded_steps=expanded_steps,
            expanded_provider_calls=expanded_provider_calls,
        )

    def referenced_providers(self) -> set[str]:
        """Return every provider referenced at top level or inside repeat."""
        names: set[str] = set()
        for step in self.steps:
            if isinstance(step, ProcedureRepeatStepSchema):
                names.update(
                    nested.provider for nested in step.repeat.steps if nested.provider is not None
                )
            elif step.provider is not None:
                names.add(step.provider)
        return names


class ProcedureRecord(BaseModel):
    """Persisted immutable procedure definition plus governance state."""

    procedure_id: str = Field(default_factory=lambda: new_id("PRC"))
    definition: ProcedureDefinition
    definition_digest: str
    status: ProcedureStatus = "pending"
    version: int = Field(default=1, ge=1)
    supersedes_procedure_id: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    proposed_actor_context: GovernedActorContext | None
    proposed_at: datetime = Field(default_factory=utc_now)
    resolved_actor_context: GovernedActorContext | None = None
    resolved_at: datetime | None = None
    retired_actor_context: GovernedActorContext | None = None
    retired_at: datetime | None = None
    reason: str | None = None
    promoted_config_digest: str | None = None
    promoted_lock_digest: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProcedureBudgetSpent(BaseModel):
    """Budget accounting persisted for one procedure invocation."""

    wall_clock_s: float = Field(default=0, ge=0)
    provider_calls: int = Field(default=0, ge=0)

    model_config = ConfigDict(extra="forbid")


class ProcedureRun(BaseModel):
    """Crash-visible procedure invocation record; execution lands after Stage A."""

    run_id: str = Field(default_factory=lambda: new_id("PRN"))
    procedure_id: str
    definition_digest: str
    status: ProcedureRunStatus = "started"
    verdict: ProcedureRunVerdict | None = None
    budget_spent: ProcedureBudgetSpent = Field(default_factory=ProcedureBudgetSpent)
    receipt_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    finalized_at: datetime | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_status_and_verdict(self) -> ProcedureRun:
        if self.status == "started" and self.verdict is not None:
            raise ValueError("started procedure runs must have a null verdict")
        if self.status == "finalized" and self.verdict is None:
            raise ValueError("finalized procedure runs require a verdict")
        return self


class ProcedureTransitionResult(BaseModel):
    """Service result for one receipted procedure lifecycle transition."""

    action: Literal["propose", "promote", "reject", "retire"]
    procedure: ProcedureRecord
    receipt_id: str | None = None


def compute_procedure_definition_digest(definition: ProcedureDefinition) -> str:
    """Return the stable content digest of one validated definition."""
    payload = definition.model_dump(mode="json", by_alias=True, exclude_none=True)
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _validate_unique_step_ids(
    steps: list[WorkflowStepSchema] | list[ProcedureStepSchema],
    *,
    context: str,
) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for step in steps:
        if step.id in seen:
            duplicates.add(step.id)
        seen.add(step.id)
    if duplicates:
        raise ValueError(f"{context} contain duplicate step id(s): {sorted(duplicates)}")


def _workflow_references(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith("$") else []
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _workflow_references(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _workflow_references(item)]
    return []


__all__ = [
    "MAX_PROCEDURE_EXPANDED_PROVIDER_CALLS",
    "MAX_PROCEDURE_EXPANDED_STEPS",
    "MAX_PROCEDURE_REPEAT_ATTEMPTS",
    "MAX_PROCEDURE_STEPS",
    "ProcedureBudget",
    "ProcedureBudgetSpent",
    "ProcedureDefinition",
    "ProcedureRecord",
    "ProcedureRepeatSpec",
    "ProcedureRepeatStepSchema",
    "ProcedureRun",
    "ProcedureRunStatus",
    "ProcedureRunVerdict",
    "ProcedureStaticExpansion",
    "ProcedureStatus",
    "ProcedureStepSchema",
    "ProcedureTier",
    "ProcedureTransitionResult",
    "compute_procedure_definition_digest",
]
