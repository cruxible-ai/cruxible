"""Deterministic G9 audit ranking and completed-run records.

Audit is a read-side patrol over accepted Claim facts plus explicitly bounded
operational observations.  It never recommends a change and its completion
records live only in the review operational store.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.closure import dependency_artifacts
from cruxible_core.playbill.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    AUDIT_BUDGET_MAX_MAX_BYTES,
    AUDIT_BUDGET_MAX_MAX_ROWS,
    AUDIT_BUDGET_MIN_MAX_BYTES,
    AUDIT_BUDGET_MIN_MAX_ROWS,
    AUDIT_CONSUMPTION_STAKE_WEIGHT,
    AUDIT_DEPENDENT_STAKE_WEIGHT,
    AUDIT_RANK_STAKE_WEIGHT,
    AUDIT_RANK_STALENESS_WEIGHT,
    AUDIT_RANK_WEAKNESS_WEIGHT,
    AUDIT_STAKE_BASE,
    AUDIT_WEAKNESS_BASE,
    AUDIT_WEAKNESS_SIGNAL_WEIGHT,
)
from cruxible_core.playbill.query.backends import ClaimQueryFactsV1

AUDIT_REQUEST_DIGEST_DOMAIN = "playbill-audit-request-v1"
AUDIT_RESULT_DIGEST_DOMAIN = "playbill-audit-result-v1"
AUDIT_RUN_ID_DOMAIN = "playbill-audit-run-v1"
AUDIT_CURSOR_DIGEST_DOMAIN = "playbill-audit-cursor-v1"
AUDIT_SCOPE_DIGEST_DOMAIN = "playbill-audit-scope-v1"
AUDIT_PARTITION_ID = "completed-runs"

AuditOmissionReason: TypeAlias = Literal[
    "byte_budget_exceeded",
    "row_budget_exceeded",
]
AuditEvidenceKind: TypeAlias = Literal[
    "accepted_claim",
    "claim_attestation",
    "claim_type",
    "consumption_aggregate",
    "dependent",
    "supporting_capture",
]


class _StrictAuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditScopeV1(_StrictAuditModel):
    """Declared visible scope; empty selectors mean all visible live Claims."""

    tag: Literal["playbill-audit-scope-v1"] = "playbill-audit-scope-v1"
    claim_type_identities: tuple[str, ...] = ()
    subject_kinds: tuple[str, ...] = ()

    @field_validator("claim_type_identities", "subject_kinds")
    @classmethod
    def _ordered(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("audit scope selectors must be byte-sorted and unique")
        return value

    @field_validator("claim_type_identities")
    @classmethod
    def _claim_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.startswith("ClaimType:") for item in value):
            raise ValueError("audit ClaimType scope requires qualified ClaimType identities")
        return value


class AuditBudgetV1(_StrictAuditModel):
    tag: Literal["playbill-audit-budget-v1"] = "playbill-audit-budget-v1"
    max_rows: int = Field(
        default=AUDIT_BUDGET_DEFAULT_MAX_ROWS,
        ge=AUDIT_BUDGET_MIN_MAX_ROWS,
        le=AUDIT_BUDGET_MAX_MAX_ROWS,
    )
    max_bytes: int = Field(
        default=AUDIT_BUDGET_DEFAULT_MAX_BYTES,
        ge=AUDIT_BUDGET_MIN_MAX_BYTES,
        le=AUDIT_BUDGET_MAX_MAX_BYTES,
    )


class AuditCursorV1(_StrictAuditModel):
    tag: Literal["playbill-audit-cursor-v1"] = "playbill-audit-cursor-v1"
    coordinate: AcceptedCoordinate
    evaluation_time: datetime
    operational_input_head_digest: str
    scope_digest: str
    next_offset: int = Field(ge=1)
    cursor_digest: str

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit cursor time must be absolute")
        return value

    @field_validator("operational_input_head_digest", "scope_digest", "cursor_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reproduces(self) -> AuditCursorV1:
        if self.cursor_digest != audit_cursor_digest(self):
            raise ValueError("audit cursor digest does not reproduce")
        return self


class AuditEvidenceRefV1(_StrictAuditModel):
    kind: AuditEvidenceKind
    identity: str = Field(min_length=1)
    artifact_digest: str | None = None
    generation: int | None = Field(default=None, ge=0)
    facts: dict[str, object] = Field(default_factory=dict)

    @field_validator("artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value


class AuditClaimFactorsV1(_StrictAuditModel):
    unique_dependent_count: int = Field(ge=0)
    qualifying_consumption_touch_count: int = Field(ge=0)
    stake: int = Field(ge=1)
    single_source: bool
    proposer_observed_only: bool
    zero_corroboration: bool
    near_freshness_horizon: bool
    weakness: int = Field(ge=1, le=5)
    first_accepted_generation: int = Field(ge=0)
    last_independent_verification_generation: int = Field(ge=0)
    never_verified: bool
    staleness: int = Field(ge=1)

    @model_validator(mode="after")
    def _arithmetic(self) -> AuditClaimFactorsV1:
        expected_stake = (
            AUDIT_STAKE_BASE
            + AUDIT_DEPENDENT_STAKE_WEIGHT * self.unique_dependent_count
            + AUDIT_CONSUMPTION_STAKE_WEIGHT * self.qualifying_consumption_touch_count
        )
        if self.stake != expected_stake:
            raise ValueError("audit stake does not reproduce")
        expected_weakness = AUDIT_WEAKNESS_BASE + AUDIT_WEAKNESS_SIGNAL_WEIGHT * sum(
            int(item)
            for item in (
                self.single_source,
                self.proposer_observed_only,
                self.zero_corroboration,
                self.near_freshness_horizon,
            )
        )
        if self.weakness != expected_weakness:
            raise ValueError("audit weakness does not reproduce")
        if self.last_independent_verification_generation < self.first_accepted_generation:
            raise ValueError("audit verification generation predates the statement lineage")
        return self


class AuditClaimRowV1(_StrictAuditModel):
    tag: Literal["playbill-audit-claim-row-v1"] = "playbill-audit-claim-row-v1"
    claim_path: str
    claim_identity: ArtifactIdentity
    claim_artifact_digest: str
    claim_statement_digest: str
    subject_identity: ArtifactIdentity
    claim_type_identity: ArtifactIdentity
    verdict: str
    currency: str
    factors: AuditClaimFactorsV1
    rank_score: int = Field(ge=1)
    evidence_refs: tuple[AuditEvidenceRefV1, ...]

    @field_validator("claim_artifact_digest", "claim_statement_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _score(self) -> AuditClaimRowV1:
        expected_score = (
            AUDIT_RANK_STAKE_WEIGHT
            * self.factors.stake
            * AUDIT_RANK_WEAKNESS_WEIGHT
            * self.factors.weakness
            * AUDIT_RANK_STALENESS_WEIGHT
            * self.factors.staleness
        )
        if self.rank_score != expected_score:
            raise ValueError("audit rank score does not reproduce")
        ordered = tuple(
            sorted(
                self.evidence_refs,
                key=lambda item: canonical_bytes(item.model_dump(mode="json")),
            )
        )
        if self.evidence_refs != ordered:
            raise ValueError("audit evidence references must be canonically ordered")
        return self


class AuditCoveredClaimV1(_StrictAuditModel):
    claim_identity: ArtifactIdentity
    artifact_digest: str

    @field_validator("artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class AuditCoverageV1(_StrictAuditModel):
    tag: Literal["playbill-audit-coverage-v1"] = "playbill-audit-coverage-v1"
    access_permitted: bool
    declared_scope: AuditScopeV1
    covered_claims: tuple[AuditCoveredClaimV1, ...]
    candidate_claim_count: int = Field(ge=0)
    returned_claim_count: int = Field(ge=0)
    omitted_claim_count: int = Field(ge=0)
    omission_reasons: tuple[AuditOmissionReason, ...] = ()

    @model_validator(mode="after")
    def _counts(self) -> AuditCoverageV1:
        if self.candidate_claim_count != len(self.covered_claims):
            raise ValueError("audit candidate count differs from actual covered Claims")
        if self.returned_claim_count + self.omitted_claim_count != self.candidate_claim_count:
            raise ValueError("audit returned and omitted counts do not cover the candidate set")
        if not self.access_permitted and any(
            (
                self.covered_claims,
                self.candidate_claim_count,
                self.returned_claim_count,
                self.omitted_claim_count,
                self.omission_reasons,
            )
        ):
            raise ValueError("an access-omitted audit cannot disclose counts or identities")
        if self.omission_reasons != tuple(
            sorted(set(self.omission_reasons), key=lambda item: item.encode("ascii"))
        ):
            raise ValueError("audit omission reasons must be byte-sorted and unique")
        return self


class AuditRunV1(_StrictAuditModel):
    """One successful, append-only coverage-accounting record."""

    tag: Literal["playbill-audit-run-v1"] = "playbill-audit-run-v1"
    event_id: str
    audit_run_id: str
    request: dict[str, object]
    accepted_coordinate: AcceptedCoordinate
    accepted_generation: int = Field(ge=0)
    evaluation_time: datetime
    access_profile_id: str
    budget: AuditBudgetV1
    operational_input_head_digest: str
    coverage: AuditCoverageV1
    result_digest: str

    @field_validator("event_id", "audit_run_id", "operational_input_head_digest", "result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reproduces(self) -> AuditRunV1:
        if self.event_id != self.audit_run_id or self.audit_run_id != audit_run_id(self):
            raise ValueError("audit run identity does not reproduce")
        return self


class AuditDependentRefV1(_StrictAuditModel):
    kind: Literal["Claim", "LineSpec", "Procedure", "QueryDefinition"]
    identity: ArtifactIdentity
    path: str


def audit_scope_digest(scope: AuditScopeV1) -> str:
    payload = scope.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        AUDIT_SCOPE_DIGEST_DOMAIN,
        payload,
    ).tagged


def audit_cursor_digest(cursor: AuditCursorV1) -> str:
    payload = cursor.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("cursor_digest")
    return typed_digest(Sha256Value, AUDIT_CURSOR_DIGEST_DOMAIN, payload).tagged


def build_audit_cursor(
    *,
    coordinate: AcceptedCoordinate,
    evaluation_time: datetime,
    operational_input_head_digest: str,
    scope_digest: str,
    next_offset: int,
) -> AuditCursorV1:
    placeholder = "sha256:" + "0" * 64
    draft = AuditCursorV1.model_construct(
        tag="playbill-audit-cursor-v1",
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        operational_input_head_digest=operational_input_head_digest,
        scope_digest=scope_digest,
        next_offset=next_offset,
        cursor_digest=placeholder,
    )
    return AuditCursorV1(
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        operational_input_head_digest=operational_input_head_digest,
        scope_digest=scope_digest,
        next_offset=next_offset,
        cursor_digest=audit_cursor_digest(draft),
    )


def audit_request_digest(request: Mapping[str, object]) -> str:
    payload = dict(request)
    payload.pop("tag", None)
    return typed_digest(Sha256Value, AUDIT_REQUEST_DIGEST_DOMAIN, payload).tagged


def audit_result_digest(result: BaseModel) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("tag", None)
    payload.pop("result_digest", None)
    return typed_digest(Sha256Value, AUDIT_RESULT_DIGEST_DOMAIN, payload).tagged


def audit_run_id(run: AuditRunV1) -> str:
    return typed_digest(
        Sha256Value,
        AUDIT_RUN_ID_DOMAIN,
        {
            "request": run.request,
            "accepted_coordinate": run.accepted_coordinate.model_dump(mode="json"),
            "accepted_generation": run.accepted_generation,
            "evaluation_time": run.evaluation_time.isoformat(),
            "operational_input_head_digest": run.operational_input_head_digest,
            "coverage": run.coverage.model_dump(mode="json"),
            "result_digest": run.result_digest,
        },
    ).tagged


def build_audit_run(
    *,
    request: dict[str, object],
    accepted_coordinate: AcceptedCoordinate,
    accepted_generation: int,
    evaluation_time: datetime,
    access_profile_id: str,
    budget: AuditBudgetV1,
    operational_input_head_digest: str,
    coverage: AuditCoverageV1,
    result_digest: str,
) -> AuditRunV1:
    placeholder = "sha256:" + "0" * 64
    draft = AuditRunV1.model_construct(
        tag="playbill-audit-run-v1",
        event_id=placeholder,
        audit_run_id=placeholder,
        request=request,
        accepted_coordinate=accepted_coordinate,
        accepted_generation=accepted_generation,
        evaluation_time=evaluation_time,
        access_profile_id=access_profile_id,
        budget=budget,
        operational_input_head_digest=operational_input_head_digest,
        coverage=coverage,
        result_digest=result_digest,
    )
    identity = audit_run_id(draft)
    return AuditRunV1(
        event_id=identity,
        audit_run_id=identity,
        request=request,
        accepted_coordinate=accepted_coordinate,
        accepted_generation=accepted_generation,
        evaluation_time=evaluation_time,
        access_profile_id=access_profile_id,
        budget=budget,
        operational_input_head_digest=operational_input_head_digest,
        coverage=coverage,
        result_digest=result_digest,
    )


def audit_row_order(row: AuditClaimRowV1) -> tuple[int, int, int, int, bytes]:
    """The complete frozen deterministic ranking order."""

    return (
        -row.rank_score,
        -row.factors.stake,
        -row.factors.weakness,
        -row.factors.staleness,
        row.claim_path.encode("utf-8"),
    )


def build_reverse_dependency_index(
    *,
    tree: Mapping[str, bytes],
    facts: ClaimQueryFactsV1,
    claim_lineages: Mapping[str, tuple[str, ...]],
) -> dict[str, tuple[AuditDependentRefV1, ...]]:
    """Index exact impact edges once for all current Claim sources.

    The edge meanings deliberately match ``build_dependency_impact``: Claim
    backing inputs match any digest in the source lineage and pins match the
    source Claim identity.  Dependents are unique by accepted artifact, because
    audit stake counts artifacts rather than duplicate edge spellings.
    """

    current = {
        row.accepted.path: row
        for row in facts.claims
        if row.accepted.claim.lifecycle.state == "live"
    }
    source_by_identity = {
        row.accepted.claim.identity.qualified: path for path, row in current.items()
    }
    source_by_digest = {
        digest: path for path, digests in claim_lineages.items() for digest in digests
    }
    found: dict[str, dict[tuple[str, str, str], AuditDependentRefV1]] = defaultdict(dict)

    def add(source_path: str, *, kind: str, identity: ArtifactIdentity, path: str) -> None:
        if source_path == path:
            return
        ref = AuditDependentRefV1(kind=kind, identity=identity, path=path)  # type: ignore[arg-type]
        found[source_path][(kind, identity.qualified, path)] = ref

    for row in current.values():
        for digest in row.accepted.claim.backing.input_claim_digests:
            source_path = source_by_digest.get(digest)
            if source_path is not None:
                add(
                    source_path,
                    kind="Claim",
                    identity=row.accepted.claim.identity,
                    path=row.accepted.path,
                )

    kinds = {
        "claim": "Claim",
        "line": "LineSpec",
        "procedure": "Procedure",
        "query-definition": "QueryDefinition",
    }
    for state in dependency_artifacts(tree):
        kind = kinds.get(state.artifact_kind)
        if kind is None or state.lifecycle.state != "live":
            continue
        for pin in state.pins:
            source_path = source_by_identity.get(pin.target.qualified)
            if source_path is not None:
                add(source_path, kind=kind, identity=state.identity, path=state.path)

    return {
        path: tuple(
            by_key[key]
            for key in sorted(
                by_key,
                key=lambda item: (
                    item[0].encode("ascii"),
                    item[1].encode("utf-8"),
                    item[2].encode("utf-8"),
                ),
            )
        )
        for path, by_key in found.items()
    }


__all__ = [
    "AUDIT_CURSOR_DIGEST_DOMAIN",
    "AUDIT_PARTITION_ID",
    "AUDIT_REQUEST_DIGEST_DOMAIN",
    "AUDIT_RESULT_DIGEST_DOMAIN",
    "AUDIT_RUN_ID_DOMAIN",
    "AUDIT_SCOPE_DIGEST_DOMAIN",
    "AuditBudgetV1",
    "AuditClaimFactorsV1",
    "AuditClaimRowV1",
    "AuditCoverageV1",
    "AuditCoveredClaimV1",
    "AuditCursorV1",
    "AuditDependentRefV1",
    "AuditEvidenceRefV1",
    "AuditRunV1",
    "AuditScopeV1",
    "audit_cursor_digest",
    "audit_request_digest",
    "audit_result_digest",
    "audit_row_order",
    "audit_run_id",
    "audit_scope_digest",
    "build_audit_cursor",
    "build_audit_run",
    "build_reverse_dependency_index",
]
