"""Coordinate- and time-bound dependency impact from one Claim to its dependents.

When a later backing successor supports or contradicts a statement, the
question an agent has to answer is not "what did we believe" but "what is
standing on the thing that moved". This module walks the accepted dependency
edges outward from one Claim -- downstream Claims through their backing inputs,
and Procedures, QueryDefinitions, and LineSpecs through their pins -- and
renders each dependent twice over:

* the **exact artifact it used**, taken from that dependent's own recorded
  backing digest or pin. That coordinate is immutable. Nothing here rewrites,
  reinterprets, or relabels it, because a dependent that was correct against
  the generation it pinned stays correct against the generation it pinned; and
* the **current standing of the source** at one explicit evaluation time --
  whether the accepted artifact has been superseded, and what its verdict and
  currency are now.

The gap between those two is the repair surface: ``stale`` says the pinned
generation is no longer the accepted one, ``impact_reasons`` says why the
source moved, and ``repair_candidate`` is exactly the disjunction of those
reasons. None of it is written anywhere -- this is a read, and the ledger is
untouched.

Search is bounded by what the accepted coordinate can see. Backing edges name
inputs by artifact digest alone, so the walk states the exact digest set it
matched in ``searched_artifact_digests`` rather than implying it searched a
lineage it cannot reach.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_verdicts import evaluate_claim_verdict
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.line_specs import AcceptedLineSpecV1
from cruxible_client.contracts.providers import ProviderV1
from cruxible_client.contracts.query.definitions import AcceptedQueryDefinitionV1
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import CoverageDescriptorV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.backends import ClaimFactRowV1, ClaimQueryFactsV1

DEPENDENCY_IMPACT_RECEIPT_DIGEST_DOMAIN = "playbill-dependency-impact-v1"

DEPENDENT_KINDS: tuple[str, ...] = ("Claim", "LineSpec", "Procedure", "QueryDefinition")

SOURCE_SUPERSEDED = "playbill.impact.source_superseded"
SOURCE_CONTRADICTED = "playbill.impact.source_contradicted"
SOURCE_UNRESOLVED = "playbill.impact.source_unresolved"
SOURCE_UNCOVERED = "playbill.impact.source_uncovered"
SOURCE_EXPIRED = "playbill.impact.source_expired"
SOURCE_CURRENCY_STALE = "playbill.impact.source_currency_stale"

_VERDICT_REASONS: Mapping[str, str] = {
    "contradicted": SOURCE_CONTRADICTED,
    "stale": SOURCE_EXPIRED,
    "uncovered": SOURCE_UNCOVERED,
    "unresolved": SOURCE_UNRESOLVED,
}
"""Each evidence-relative verdict that puts a dependent's footing in question.

``supported`` is deliberately absent: a supported source is not an impact.
"""


class DependencyImpactError(PlaybillError):
    """A dependency-impact read could not be answered at the named coordinate."""


class _StrictImpactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DependencyImpactBudgetV1(_StrictImpactModel):
    tag: Literal["playbill-dependency-impact-budget-v1"] = "playbill-dependency-impact-budget-v1"
    max_dependents: int = Field(default=50, ge=1)
    max_bytes: int = Field(default=32_768, ge=1)


class DependencyImpactRequestV1(_StrictImpactModel):
    """One impact read: exactly one target, one coordinate, one explicit instant."""

    tag: Literal["playbill-dependency-impact-request-v1"] = "playbill-dependency-impact-request-v1"
    at: AcceptedCoordinate
    address: SemanticAddress | None = None
    statement_digest: str | None = None
    evaluation_time: datetime
    budget: DependencyImpactBudgetV1 = DependencyImpactBudgetV1()

    @field_validator("statement_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("a dependency-impact evaluation time must be an absolute instant")
        return value

    @model_validator(mode="after")
    def _target(self) -> "DependencyImpactRequestV1":
        if (self.address is None) == (self.statement_digest is None):
            raise ValueError("dependency impact requires exactly one address or statement digest")
        if self.address is not None and not self.address.artifact_path.startswith("claims/"):
            raise ValueError("dependency impact starts from a Claim artifact or statement")
        return self


class DependencyImpactSourceV1(_StrictImpactModel):
    """One resolved source Claim: what it is now, and what the walk searched for."""

    tag: Literal["playbill-dependency-impact-source-v1"] = "playbill-dependency-impact-source-v1"
    address: SemanticAddress
    claim_path: str
    identity: str
    statement_digest: str
    accepted_artifact_digest: str
    lifecycle_state: str
    predecessor_digest: str | None = None
    searched_artifact_digests: tuple[str, ...]
    verdict: str
    currency: str

    @field_validator("searched_artifact_digests")
    @classmethod
    def _searched(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != byte_sorted(value):
            raise ValueError("searched artifact digests must be nonempty, sorted, and unique")
        return value


class DependentImpactV1(_StrictImpactModel):
    """One downstream dependent, paired: the generation it used, the standing now.

    ``used_artifact_digest`` is historical and immutable. It is read out of the
    dependent's own accepted bytes and is never replaced by the current digest,
    so an impact rendering can never retroactively relabel what a dependent
    actually depended on.
    """

    tag: Literal["playbill-dependency-impact-dependent-v1"] = (
        "playbill-dependency-impact-dependent-v1"
    )
    kind: Literal["Claim", "LineSpec", "Procedure", "QueryDefinition"]
    address: SemanticAddress
    identity: str
    artifact_digest: str
    dependency_kind: Literal["backing_input", "pin"]
    used_pin_role: str | None = None
    source_claim_path: str
    used_artifact_digest: str
    current_artifact_digest: str
    stale: bool
    dependent_verdict: str | None = None
    dependent_currency: str | None = None
    impact_reasons: tuple[str, ...] = ()
    repair_candidate: bool

    @field_validator("impact_reasons")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("dependency impact reasons must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "DependentImpactV1":
        if self.stale != (self.used_artifact_digest != self.current_artifact_digest):
            raise ValueError("dependency staleness must reproduce from its two exact digests")
        if self.repair_candidate != bool(self.impact_reasons):
            raise ValueError("a repair candidate is exactly a dependent with an impact reason")
        if (self.dependency_kind == "pin") != (self.used_pin_role is not None):
            raise ValueError("a pin dependency names its exact pin role")
        if self.kind != "Claim" and (
            self.dependent_verdict is not None or self.dependent_currency is not None
        ):
            raise ValueError("only a Claim dependent carries an evidence-relative verdict")
        return self


class DependencyImpactV1(_StrictImpactModel):
    """The bounded, deterministic downstream impact of one Claim at one instant."""

    tag: Literal["playbill-dependency-impact-v1"] = "playbill-dependency-impact-v1"
    at: AcceptedCoordinate
    evaluated_at: datetime
    sources: tuple[DependencyImpactSourceV1, ...]
    dependents: tuple[DependentImpactV1, ...] = ()
    candidate_dependent_count: int = Field(ge=0)
    coverage: CoverageDescriptorV1
    receipt_digest: str

    @field_validator("receipt_digest")
    @classmethod
    def _receipt(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> "DependencyImpactV1":
        if self.candidate_dependent_count < len(self.dependents):
            raise ValueError("impact candidates cannot be fewer than the dependents returned")
        truncated = self.candidate_dependent_count > len(self.dependents)
        if truncated != bool(self.coverage.truncated_facets):
            raise ValueError("impact truncation must be stated in coverage")
        return self

    @property
    def repair_candidates(self) -> tuple[DependentImpactV1, ...]:
        """Return the dependents whose footing moved, in the rendered order."""

        return tuple(item for item in self.dependents if item.repair_candidate)


# -- source resolution ----------------------------------------------------


def _live_claims(facts: ClaimQueryFactsV1) -> tuple[ClaimFactRowV1, ...]:
    return tuple(row for row in facts.claims if row.accepted.claim.lifecycle.state == "live")


def _verdict(
    row: ClaimFactRowV1,
    *,
    evaluation_time: datetime,
    providers: Mapping[str, ProviderV1],
) -> tuple[str, str]:
    statement = row.accepted.claim.statement
    result = evaluate_claim_verdict(
        claim_statement_digest=row.accepted.statement_digest,
        rule=row.rule,
        evaluation_time=evaluation_time,
        captures=row.captures,
        attestations=row.attestations,
        providers=providers,
        claim_effective_from=statement.effective_from,
        claim_effective_until=statement.effective_until,
        referent_current=row.referent_current,
        resolved_authority_basis=row.resolved_authority_basis,
    )
    return (result.verdict, result.currency)


def _sources(
    request: DependencyImpactRequestV1,
    rows: Sequence[ClaimFactRowV1],
    *,
    providers: Mapping[str, ProviderV1],
    source_lineages: Mapping[str, Sequence[str]],
) -> tuple[DependencyImpactSourceV1, ...]:
    if request.address is not None:
        path = request.address.artifact_path
        selected = tuple(row for row in rows if row.accepted.path == path)
        if not selected:
            raise DependencyImpactError(
                "the impact target Claim is not live at the accepted coordinate"
            )
    else:
        selected = tuple(
            row for row in rows if row.accepted.statement_digest == request.statement_digest
        )
        if not selected:
            raise DependencyImpactError(
                "no live accepted Claim carries the impact target statement digest"
            )
    sources: list[DependencyImpactSourceV1] = []
    for row in selected:
        lifecycle = row.accepted.claim.lifecycle
        verdict, currency = _verdict(
            row,
            evaluation_time=request.evaluation_time,
            providers=providers,
        )
        predecessor = lifecycle.predecessor_digest
        supplied_lineage = tuple(source_lineages.get(row.accepted.path, ()))
        searched = (
            byte_sorted(supplied_lineage)
            if supplied_lineage
            else byte_sorted(
                (row.accepted.artifact_digest, *((predecessor,) if predecessor else ()))
            )
        )
        if row.accepted.artifact_digest not in searched:
            raise DependencyImpactError("source lineage omits the current accepted artifact")
        sources.append(
            DependencyImpactSourceV1(
                address=SemanticAddress.claim_statement(row.accepted.path),
                claim_path=row.accepted.path,
                identity=row.accepted.claim.identity.qualified,
                statement_digest=row.accepted.statement_digest,
                accepted_artifact_digest=row.accepted.artifact_digest,
                lifecycle_state=lifecycle.state,
                predecessor_digest=predecessor,
                searched_artifact_digests=searched,
                verdict=verdict,
                currency=currency,
            )
        )
    return tuple(sorted(sources, key=lambda item: item.claim_path.encode("utf-8")))


def _impact_reasons(source: DependencyImpactSourceV1, *, stale: bool) -> tuple[str, ...]:
    reasons: set[str] = set()
    if stale:
        reasons.add(SOURCE_SUPERSEDED)
    verdict_reason = _VERDICT_REASONS.get(source.verdict)
    if verdict_reason is not None:
        reasons.add(verdict_reason)
    if source.currency == "stale":
        reasons.add(SOURCE_CURRENCY_STALE)
    return byte_sorted(tuple(reasons))


# -- dependent traversal --------------------------------------------------


def _dependent(
    *,
    kind: str,
    address: SemanticAddress,
    identity: str,
    artifact_digest: str,
    dependency_kind: str,
    used_pin_role: str | None,
    source: DependencyImpactSourceV1,
    used_artifact_digest: str,
    dependent_verdict: str | None = None,
    dependent_currency: str | None = None,
) -> DependentImpactV1:
    stale = used_artifact_digest != source.accepted_artifact_digest
    reasons = _impact_reasons(source, stale=stale)
    return DependentImpactV1(
        kind=kind,  # type: ignore[arg-type]
        address=address,
        identity=identity,
        artifact_digest=artifact_digest,
        dependency_kind=dependency_kind,  # type: ignore[arg-type]
        used_pin_role=used_pin_role,
        source_claim_path=source.claim_path,
        used_artifact_digest=used_artifact_digest,
        current_artifact_digest=source.accepted_artifact_digest,
        stale=stale,
        dependent_verdict=dependent_verdict,
        dependent_currency=dependent_currency,
        impact_reasons=reasons,
        repair_candidate=bool(reasons),
    )


def _claim_identity(source: DependencyImpactSourceV1) -> ArtifactIdentity:
    kind, _separator, name = source.identity.partition(":")
    return ArtifactIdentity(kind=kind, name=name)


def _matching_pins(
    pins: Iterable[ArtifactPin],
    identity: ArtifactIdentity,
) -> tuple[ArtifactPin, ...]:
    return tuple(pin for pin in pins if pin.target == identity)


def _claim_dependents(
    rows: Sequence[ClaimFactRowV1],
    source: DependencyImpactSourceV1,
    *,
    evaluation_time: datetime,
    providers: Mapping[str, ProviderV1],
) -> list[DependentImpactV1]:
    identity = _claim_identity(source)
    searched = set(source.searched_artifact_digests)
    dependents: list[DependentImpactV1] = []
    for row in rows:
        if (
            row.accepted.path == source.claim_path
            or row.accepted.claim.lifecycle.state != "live"
        ):
            continue
        claim = row.accepted.claim
        used_inputs = byte_sorted(tuple(searched.intersection(claim.backing.input_claim_digests)))
        pins = _matching_pins(claim.pins, identity)
        if not used_inputs and not pins:
            continue
        verdict, currency = _verdict(row, evaluation_time=evaluation_time, providers=providers)
        address = SemanticAddress.claim_statement(row.accepted.path)
        for digest in used_inputs:
            dependents.append(
                _dependent(
                    kind="Claim",
                    address=address,
                    identity=claim.identity.qualified,
                    artifact_digest=row.accepted.artifact_digest,
                    dependency_kind="backing_input",
                    used_pin_role=None,
                    source=source,
                    used_artifact_digest=digest,
                    dependent_verdict=verdict,
                    dependent_currency=currency,
                )
            )
        for pin in pins:
            dependents.append(
                _dependent(
                    kind="Claim",
                    address=address,
                    identity=claim.identity.qualified,
                    artifact_digest=row.accepted.artifact_digest,
                    dependency_kind="pin",
                    used_pin_role=pin.role,
                    source=source,
                    used_artifact_digest=pin.artifact_digest,
                    dependent_verdict=verdict,
                    dependent_currency=currency,
                )
            )
    return dependents


def _pinned_dependents(
    source: DependencyImpactSourceV1,
    *,
    definitions: Iterable[AcceptedQueryDefinitionV1],
    procedures: Iterable[AcceptedProcedureV1],
    line_specs: Iterable[AcceptedLineSpecV1],
) -> list[DependentImpactV1]:
    identity = _claim_identity(source)
    dependents: list[DependentImpactV1] = []
    holders: tuple[tuple[str, str, SemanticAddress, Iterable[ArtifactPin], str], ...] = (
        *(
            (
                "QueryDefinition",
                item.query.identity.qualified,
                SemanticAddress.whole_artifact(item.path),
                item.query.pins,
                item.artifact_digest,
            )
            for item in definitions
            if item.query.lifecycle.state == "live"
        ),
        *(
            (
                "Procedure",
                item.procedure.identity.qualified,
                SemanticAddress.procedure_unit(item.path),
                item.procedure.pins,
                item.artifact_digest,
            )
            for item in procedures
            if item.procedure.lifecycle.state == "live"
        ),
        *(
            (
                "LineSpec",
                item.line.identity.qualified,
                SemanticAddress.line(item.path),
                item.line.pins,
                item.artifact_digest,
            )
            for item in line_specs
            if item.line.lifecycle.state == "live"
        ),
    )
    for kind, qualified, address, pins, artifact_digest in holders:
        for pin in _matching_pins(pins, identity):
            dependents.append(
                _dependent(
                    kind=kind,
                    address=address,
                    identity=qualified,
                    artifact_digest=artifact_digest,
                    dependency_kind="pin",
                    used_pin_role=pin.role,
                    source=source,
                    used_artifact_digest=pin.artifact_digest,
                )
            )
    return dependents


def _dependent_order(item: DependentImpactV1) -> tuple[bytes, bytes, bytes, bytes, bytes]:
    return (
        item.kind.encode("utf-8"),
        canonical_bytes(item.address.model_dump(mode="json")),
        item.source_claim_path.encode("utf-8"),
        item.dependency_kind.encode("utf-8"),
        (item.used_pin_role or "").encode("utf-8") + item.used_artifact_digest.encode("utf-8"),
    )


def build_dependency_impact(
    request: DependencyImpactRequestV1,
    *,
    facts: ClaimQueryFactsV1,
    definitions: Iterable[AcceptedQueryDefinitionV1] = (),
    procedures: Iterable[AcceptedProcedureV1] = (),
    line_specs: Iterable[AcceptedLineSpecV1] = (),
    source_lineages: Mapping[str, Sequence[str]] | None = None,
    include_retired_sources: bool = False,
) -> DependencyImpactV1:
    """Render the downstream impact of one Claim without writing or relabelling.

    The result is a pure function of the accepted facts, the pinned artifacts,
    and the explicit evaluation time: the same inputs always produce the same
    bytes, and running it twice cannot change what any dependent recorded.
    """

    if request.at != AcceptedCoordinate.from_internal(facts.coordinate):
        raise DependencyImpactError("dependency impact requires one accepted coordinate")
    providers = {item.identity.qualified: item for item in facts.providers}
    rows = facts.claims if include_retired_sources else _live_claims(facts)
    sources = _sources(
        request,
        rows,
        providers=providers,
        source_lineages=source_lineages or {},
    )
    definitions = tuple(definitions)
    procedures = tuple(procedures)
    line_specs = tuple(line_specs)

    found: list[DependentImpactV1] = []
    for source in sources:
        found.extend(
            _claim_dependents(
                rows,
                source,
                evaluation_time=request.evaluation_time,
                providers=providers,
            )
        )
        found.extend(
            _pinned_dependents(
                source,
                definitions=definitions,
                procedures=procedures,
                line_specs=line_specs,
            )
        )
    ordered = tuple(sorted(found, key=_dependent_order))
    candidate_count = len(ordered)

    truncated: set[str] = set()
    reasons: set[str] = set()
    kept = ordered[: request.budget.max_dependents]
    if len(kept) != candidate_count:
        truncated.update(item.kind for item in ordered[len(kept) :])
        reasons.add("dependent_budget_exceeded")
    while kept and len(canonical_bytes([item.model_dump(mode="json") for item in kept])) > (
        request.budget.max_bytes
    ):
        truncated.add(kept[-1].kind)
        kept = kept[:-1]
        reasons.add("byte_budget_exceeded")

    coverage = CoverageDescriptorV1(
        requested_facets=byte_sorted(DEPENDENT_KINDS),
        available_facets=byte_sorted(tuple({item.kind for item in kept})),
        truncated_facets=byte_sorted(tuple(truncated)),
        reason_codes=byte_sorted(tuple(reasons)),
    )
    receipt_digest = typed_digest(
        Sha256Value,
        DEPENDENCY_IMPACT_RECEIPT_DIGEST_DOMAIN,
        {
            "at": request.at.model_dump(mode="json"),
            "candidate_dependent_count": candidate_count,
            "coverage": coverage.model_dump(mode="json"),
            "dependents": [item.model_dump(mode="json") for item in kept],
            "evaluation_time": request.evaluation_time.isoformat(),
            "sources": [item.model_dump(mode="json") for item in sources],
        },
    ).tagged
    return DependencyImpactV1(
        at=request.at,
        evaluated_at=request.evaluation_time,
        sources=sources,
        dependents=kept,
        candidate_dependent_count=candidate_count,
        coverage=coverage,
        receipt_digest=receipt_digest,
    )


__all__ = [
    "DEPENDENCY_IMPACT_RECEIPT_DIGEST_DOMAIN",
    "DEPENDENT_KINDS",
    "SOURCE_CONTRADICTED",
    "SOURCE_CURRENCY_STALE",
    "SOURCE_EXPIRED",
    "SOURCE_SUPERSEDED",
    "SOURCE_UNCOVERED",
    "SOURCE_UNRESOLVED",
    "DependencyImpactBudgetV1",
    "DependencyImpactError",
    "DependencyImpactRequestV1",
    "DependencyImpactSourceV1",
    "DependencyImpactV1",
    "DependentImpactV1",
    "build_dependency_impact",
]
