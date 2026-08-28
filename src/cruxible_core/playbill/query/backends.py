"""Accepted Claim facts, the materialized Subject view, and the backend contract.

A query backend is a disposable materialization of accepted projection facts at
exactly one accepted coordinate. It holds no authority: it is built only from
accepted Subject/Claim facts, it may be deleted and rebuilt at any time without
touching the ledger, and every answer it gives is reproducible byte for byte
from the same facts. Nothing here reads a wall clock or resolves a conflict.

The evaluator in :mod:`cruxible_core.playbill.query.engine` runs over exactly
five primitives plus the coordinate the backend was materialized at:

``subjects(kinds, subject_id=...)``
    the accepted Subject paths of the declared kinds, in ledger-path byte order.
``subject(artifact_path)``
    one accepted Subject row by its exact ledger path.
``claims_on(artifact_path, predicate)``
    the visible Claim rows whose statement subject is that Subject, per
    (subject, predicate) in claim-path byte order.
``claims_to(artifact_path, predicate)``
    the visible Claim rows whose Subject-typed object is that Subject, in the
    same claim-path byte order.
``visibility(row)``
    one Claim row's verdict and currency at the explicit evaluation time.

Relation traversal is derived from those primitives, never stored separately, so
a backend cannot invent an edge the Claim facts do not carry. Verdicts are
computed in exactly one place -- :func:`claim_row_visibility` -- so a backend
differs from the reference index in storage and traversal only, never in
adjudication.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_attestations import VerifiedClaimAttestationV1
from cruxible_client.contracts.claim_verdicts import (
    CaptureVerdictEvidenceV1,
    ClaimAdjudicationRuleV1,
    EvidenceCurrency,
    EvidenceRelativeClaimVerdict,
    claim_verdict_v1_compat,
    evaluate_claim_verdict,
)
from cruxible_client.contracts.claims import AcceptedClaim, SubjectClaimObject
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.providers import ProviderV1
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
)
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.subjects import AcceptedSubject
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate

VIEW_DIGEST_DOMAIN = "playbill-subject-query-view-v1"


class ClaimQueryBackendError(PlaybillError):
    """A query backend was read outside the lifetime of its materialization."""


class _StrictQueryBackendModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- accepted evaluation facts -------------------------------------------


class ClaimFactRowV1(_StrictQueryBackendModel):
    """One accepted Claim with the exact inputs its verdict needs at any time."""

    tag: Literal["playbill-query-claim-fact-v1"] = "playbill-query-claim-fact-v1"
    accepted: AcceptedClaim
    rule: ClaimAdjudicationRuleV1
    captures: tuple[CaptureVerdictEvidenceV1, ...] = ()
    attestations: tuple[VerifiedClaimAttestationV1, ...] = ()
    referent_current: bool = True
    resolved_authority_basis: tuple[str, ...] = ()

    @field_validator("resolved_authority_basis")
    @classmethod
    def _authority_basis(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("Claim authority basis must be sorted and unique")
        return value

    @property
    def subject_path(self) -> str:
        """Return the ledger path of the Subject this Claim states something about."""

        return self.accepted.claim.statement.subject.artifact_path

    @property
    def object_subject_path(self) -> str | None:
        """Return the related Subject's ledger path, or None for a non-relation Claim."""

        item = self.accepted.claim.statement.object
        return item.address.artifact_path if isinstance(item, SubjectClaimObject) else None


class ClaimQueryFactsV1(_StrictQueryBackendModel):
    """The accepted Subject/Claim facts one evaluation is permitted to read."""

    tag: Literal["playbill-query-facts-v1"] = "playbill-query-facts-v1"
    coordinate: AcceptedProjectionCoordinate
    subjects: tuple[AcceptedSubject, ...] = ()
    claims: tuple[ClaimFactRowV1, ...] = ()
    providers: tuple[ProviderV1, ...] = ()

    @field_validator("subjects")
    @classmethod
    def _subjects(cls, value: tuple[AcceptedSubject, ...]) -> tuple[AcceptedSubject, ...]:
        paths = tuple(item.path for item in value)
        if paths != byte_sorted(paths):
            raise ValueError("accepted query Subject facts must be sorted and unique by path")
        return value

    @field_validator("claims")
    @classmethod
    def _claims(cls, value: tuple[ClaimFactRowV1, ...]) -> tuple[ClaimFactRowV1, ...]:
        paths = tuple(item.accepted.path for item in value)
        if paths != byte_sorted(paths):
            raise ValueError("accepted query Claim facts must be sorted and unique by path")
        return value

    @field_validator("providers")
    @classmethod
    def _providers(cls, value: tuple[ProviderV1, ...]) -> tuple[ProviderV1, ...]:
        names = tuple(item.identity.qualified for item in value)
        if names != byte_sorted(names):
            raise ValueError("accepted query Provider facts must be sorted and unique by identity")
        return value


# -- the visible Claim row ------------------------------------------------


class QueryClaimVisibilityV1(_StrictQueryBackendModel):
    """Why one Claim row is present: its verdict and currency at the read time."""

    tag: Literal["playbill-query-claim-visibility-v1"] = "playbill-query-claim-visibility-v1"
    claim_path: str
    statement_digest: str
    artifact_digest: str
    predicate: str
    subject_identity: str
    verdict: EvidenceRelativeClaimVerdict
    currency: EvidenceCurrency


@dataclass(frozen=True)
class VisibleClaimRow:
    """One accepted Claim admitted by the owning query's verdict policy."""

    row: ClaimFactRowV1
    visibility: QueryClaimVisibilityV1

    @property
    def subject_path(self) -> str:
        return self.row.subject_path

    @property
    def object_subject_path(self) -> str | None:
        return self.row.object_subject_path


@dataclass(frozen=True)
class ClaimRowOutcomeV1:
    """What one Claim row resolved to, and -- when hidden -- by which verdict.

    `visible is None` alone cannot say why a row vanished, so a Claim the policy
    hid is indistinguishable from a Claim that does not exist. `hidden_verdict`
    names the verdict that hid it, and is set only for that reason: an absent
    Subject or an out-of-policy currency leaves it None.
    """

    visible: VisibleClaimRow | None
    hidden_verdict: str | None = None


def claim_row_outcome(
    row: ClaimFactRowV1,
    *,
    subject: AcceptedSubject | None,
    providers: Mapping[str, ProviderV1],
    policy: QueryEvaluationPolicyV1,
    evaluation_time: datetime,
) -> ClaimRowOutcomeV1:
    """Resolve one Claim row's visibility, keeping the reason it was hidden."""

    if subject is None:
        return ClaimRowOutcomeV1(visible=None)
    statement = row.accepted.claim.statement
    verdict = evaluate_claim_verdict(
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
    if verdict.verdict not in policy.visible_verdicts:
        return ClaimRowOutcomeV1(
            visible=None,
            hidden_verdict=claim_verdict_v1_compat(verdict).verdict,
        )
    if verdict.currency not in policy.visible_currency:
        return ClaimRowOutcomeV1(visible=None)
    return ClaimRowOutcomeV1(
        visible=VisibleClaimRow(
            row=row,
            visibility=QueryClaimVisibilityV1(
                claim_path=row.accepted.path,
                statement_digest=row.accepted.statement_digest,
                artifact_digest=row.accepted.artifact_digest,
                predicate=statement.predicate,
                subject_identity=subject.shell.identity.qualified,
                verdict=claim_verdict_v1_compat(verdict).verdict,
                currency=verdict.currency,
            ),
        )
    )


def claim_row_visibility(
    row: ClaimFactRowV1,
    *,
    subject: AcceptedSubject | None,
    providers: Mapping[str, ProviderV1],
    policy: QueryEvaluationPolicyV1,
    evaluation_time: datetime,
) -> VisibleClaimRow | None:
    """Return the Claim row's visibility, or None when the policy hides it.

    This is the single verdict path every backend shares. A Claim whose
    statement subject is absent at the accepted coordinate is unreachable under
    any policy, so it is never visible and never materialized.
    """

    return claim_row_outcome(
        row,
        subject=subject,
        providers=providers,
        policy=policy,
        evaluation_time=evaluation_time,
    ).visible


# -- the materialized Subject view ---------------------------------------


class SubjectViewRowV1(_StrictQueryBackendModel):
    """One accepted Subject as the materialized view names it."""

    tag: Literal["playbill-subject-view-row-v1"] = "playbill-subject-view-row-v1"
    path: str
    identity: str
    subject_kind: str
    subject_id: str
    artifact_digest: str


class ClaimViewRowV1(_StrictQueryBackendModel):
    """One accepted Claim's structure: its Subject, predicate, and relation target."""

    tag: Literal["playbill-claim-view-row-v1"] = "playbill-claim-view-row-v1"
    claim_path: str
    subject_path: str
    predicate: str
    statement_digest: str
    artifact_digest: str
    object_subject_path: str | None = None


class SubjectViewAdjacencyV1(_StrictQueryBackendModel):
    """The materialized Claim adjacency of one Subject path under one predicate.

    ``subject_path`` may name a Subject that is absent at the coordinate: a
    relation Claim can point at an unresolved target, and the traversal that
    reaches it must refuse rather than silently drop the edge.
    """

    tag: Literal["playbill-subject-view-adjacency-v1"] = "playbill-subject-view-adjacency-v1"
    subject_path: str
    predicate: str
    asserted_claim_paths: tuple[str, ...] = ()
    incident_claim_paths: tuple[str, ...] = ()

    @field_validator("asserted_claim_paths", "incident_claim_paths")
    @classmethod
    def _claim_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("materialized Claim adjacency must be sorted and unique by path")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "SubjectViewAdjacencyV1":
        if not self.asserted_claim_paths and not self.incident_claim_paths:
            raise ValueError("a materialized adjacency row carries at least one Claim path")
        return self


class SubjectQueryViewV1(_StrictQueryBackendModel):
    """The deterministic logical export a query backend materializes.

    The view is a pure function of the accepted facts at one coordinate. It
    carries structure only: no evaluation time, no verdict policy, and no
    resolved conflict, because those belong to a read rather than to the state
    being read.
    """

    tag: Literal["playbill-subject-query-view-v1"] = "playbill-subject-query-view-v1"
    coordinate: AcceptedProjectionCoordinate
    subjects: tuple[SubjectViewRowV1, ...] = ()
    claims: tuple[ClaimViewRowV1, ...] = ()
    adjacency: tuple[SubjectViewAdjacencyV1, ...] = ()

    @field_validator("subjects")
    @classmethod
    def _subjects(cls, value: tuple[SubjectViewRowV1, ...]) -> tuple[SubjectViewRowV1, ...]:
        paths = tuple(item.path for item in value)
        if paths != byte_sorted(paths):
            raise ValueError("materialized Subject rows must be sorted and unique by ledger path")
        return value

    @field_validator("claims")
    @classmethod
    def _claims(cls, value: tuple[ClaimViewRowV1, ...]) -> tuple[ClaimViewRowV1, ...]:
        paths = tuple(item.claim_path for item in value)
        if paths != byte_sorted(paths):
            raise ValueError("materialized Claim rows must be sorted and unique by ledger path")
        return value

    @field_validator("adjacency")
    @classmethod
    def _adjacency(
        cls, value: tuple[SubjectViewAdjacencyV1, ...]
    ) -> tuple[SubjectViewAdjacencyV1, ...]:
        keys = tuple(_adjacency_key(item.subject_path, item.predicate) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError(
                "materialized adjacency must be sorted and unique by Subject path and predicate"
            )
        return value


def _adjacency_key(subject_path: str, predicate: str) -> tuple[bytes, bytes]:
    return (subject_path.encode("utf-8"), predicate.encode("utf-8"))


def subject_view_row(subject: AcceptedSubject) -> SubjectViewRowV1:
    """Return one accepted Subject's materialized view row."""

    return SubjectViewRowV1(
        path=subject.path,
        identity=subject.shell.identity.qualified,
        subject_kind=subject.shell.subject_kind,
        subject_id=subject.shell.subject_id,
        artifact_digest=subject.artifact_digest,
    )


def claim_view_row(row: ClaimFactRowV1) -> ClaimViewRowV1:
    """Return one accepted Claim's materialized structural view row."""

    return ClaimViewRowV1(
        claim_path=row.accepted.path,
        subject_path=row.subject_path,
        predicate=row.accepted.claim.statement.predicate,
        statement_digest=row.accepted.statement_digest,
        artifact_digest=row.accepted.artifact_digest,
        object_subject_path=row.object_subject_path,
    )


def subject_query_view(facts: ClaimQueryFactsV1) -> SubjectQueryViewV1:
    """Export the accepted Subject/relation-Claim structure at one coordinate."""

    resolved = {subject.path for subject in facts.subjects}
    claims: list[ClaimViewRowV1] = []
    asserted: dict[tuple[str, str], list[str]] = {}
    incident: dict[tuple[str, str], list[str]] = {}
    for row in facts.claims:
        if row.subject_path not in resolved:
            continue
        view_row = claim_view_row(row)
        claims.append(view_row)
        asserted.setdefault((view_row.subject_path, view_row.predicate), []).append(
            view_row.claim_path
        )
        if view_row.object_subject_path is not None:
            incident.setdefault((view_row.object_subject_path, view_row.predicate), []).append(
                view_row.claim_path
            )
    return SubjectQueryViewV1(
        coordinate=facts.coordinate,
        subjects=tuple(subject_view_row(subject) for subject in facts.subjects),
        claims=tuple(claims),
        adjacency=adjacency_rows(asserted, incident),
    )


def adjacency_rows(
    asserted: Mapping[tuple[str, str], list[str]],
    incident: Mapping[tuple[str, str], list[str]],
) -> tuple[SubjectViewAdjacencyV1, ...]:
    """Return the canonically ordered adjacency of one materialized view."""

    keys = sorted(set(asserted) | set(incident), key=lambda item: _adjacency_key(*item))
    return tuple(
        SubjectViewAdjacencyV1(
            subject_path=subject_path,
            predicate=predicate,
            asserted_claim_paths=byte_sorted(tuple(asserted.get((subject_path, predicate), ()))),
            incident_claim_paths=byte_sorted(tuple(incident.get((subject_path, predicate), ()))),
        )
        for subject_path, predicate in keys
    )


def render_subject_query_view(view: SubjectQueryViewV1) -> bytes:
    """Return the canonical wire bytes of one materialized Subject view."""

    return canonical_bytes(view.model_dump(mode="json")) + b"\n"


def subject_query_view_digest(view: SubjectQueryViewV1) -> str:
    """Digest one materialized Subject view for replay and rebuild comparison."""

    payload = view.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, VIEW_DIGEST_DOMAIN, payload).tagged


# -- the backend contract -------------------------------------------------


class ClaimQueryBackendV1(Protocol):
    """The exact read surface an accepted QueryDefinition is evaluated over."""

    @property
    def coordinate(self) -> AcceptedProjectionCoordinate:
        """Return the accepted coordinate this backend was materialized at."""

    def subjects(self, kinds: tuple[str, ...], *, subject_id: str | None = None) -> tuple[str, ...]:
        """Return the canonically ordered Subject paths of the declared kinds."""

    def subject(self, artifact_path: str) -> AcceptedSubject | None:
        """Return one accepted Subject row by its exact ledger path."""

    def claims_on(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        """Return the visible Claims whose statement subject is that Subject."""

    def claims_to(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        """Return the visible Claims whose Subject-typed object is that Subject."""

    def visibility(self, row: ClaimFactRowV1) -> VisibleClaimRow | None:
        """Return the Claim row's visibility, or None when the policy hides it."""

    @property
    def excluded_by_verdict(self) -> tuple[tuple[str, int], ...]:
        """Return how many Claims each out-of-policy verdict hid, verdict-sorted.

        A backend that hides a Claim owes the caller that count: without it a
        hidden Claim and an absent Claim are the same silence.
        """


class ClaimQueryBackendFactoryV1(Protocol):
    """Materialize one backend from accepted facts, a definition, and a read time."""

    def __call__(
        self,
        facts: ClaimQueryFactsV1,
        /,
        *,
        definition: QueryDefinitionV1,
        evaluation_time: datetime,
    ) -> ClaimQueryBackendV1: ...


class DirectClaimFactIndex:
    """The reference backend: accepted facts read directly, with no materialization.

    Every other backend is measured against this one. A divergence in rows,
    order, or verdict is a defect in the other backend by definition.
    """

    def __init__(
        self,
        facts: ClaimQueryFactsV1,
        *,
        definition: QueryDefinitionV1,
        evaluation_time: datetime,
    ) -> None:
        self._facts = facts
        self._definition = definition
        self._evaluation_time = evaluation_time
        self._providers: dict[str, ProviderV1] = {
            provider.identity.qualified: provider for provider in facts.providers
        }
        self._subjects: dict[str, AcceptedSubject] = {
            subject.path: subject for subject in facts.subjects
        }
        self._by_subject: dict[tuple[str, str], list[VisibleClaimRow]] = {}
        self._by_object: dict[tuple[str, str], list[VisibleClaimRow]] = {}
        excluded: dict[str, int] = {}
        for row in facts.claims:
            outcome = self._outcome(row)
            visible = outcome.visible
            if visible is None:
                if outcome.hidden_verdict is not None:
                    excluded[outcome.hidden_verdict] = excluded.get(outcome.hidden_verdict, 0) + 1
                continue
            predicate = row.accepted.claim.statement.predicate
            self._by_subject.setdefault((row.subject_path, predicate), []).append(visible)
            target = visible.object_subject_path
            if target is not None:
                self._by_object.setdefault((target, predicate), []).append(visible)
        self._excluded_by_verdict = tuple(
            sorted(excluded.items(), key=lambda item: item[0].encode("utf-8"))
        )

    @property
    def coordinate(self) -> AcceptedProjectionCoordinate:
        """Return the accepted coordinate of the facts this index reads."""

        return self._facts.coordinate

    @property
    def excluded_by_verdict(self) -> tuple[tuple[str, int], ...]:
        """Return how many Claims each out-of-policy verdict hid, verdict-sorted."""

        return self._excluded_by_verdict

    def _outcome(self, row: ClaimFactRowV1) -> ClaimRowOutcomeV1:
        return claim_row_outcome(
            row,
            subject=self._subjects.get(row.subject_path),
            providers=self._providers,
            policy=self._definition.evaluation_policy,
            evaluation_time=self._evaluation_time,
        )

    def visibility(self, row: ClaimFactRowV1) -> VisibleClaimRow | None:
        """Return the Claim row's visibility, or None when the policy hides it."""

        return self._outcome(row).visible

    def subjects(self, kinds: tuple[str, ...], *, subject_id: str | None = None) -> tuple[str, ...]:
        """Return the canonically ordered Subject paths of the declared kinds."""

        admitted = set(kinds)
        return tuple(
            subject.path
            for subject in self._facts.subjects
            if subject.shell.subject_kind in admitted
            and (subject_id is None or subject.shell.subject_id == subject_id)
        )

    def subject(self, artifact_path: str) -> AcceptedSubject | None:
        """Return one accepted Subject row by its exact ledger path."""

        return self._subjects.get(artifact_path)

    def claims_on(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        """Return the visible Claims whose statement subject is that Subject."""

        return tuple(self._by_subject.get((artifact_path, predicate), ()))

    def claims_to(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        """Return the visible Claims whose Subject-typed object is that Subject."""

        return tuple(self._by_object.get((artifact_path, predicate), ()))


__all__ = [
    "VIEW_DIGEST_DOMAIN",
    "ClaimFactRowV1",
    "ClaimQueryBackendError",
    "ClaimQueryBackendFactoryV1",
    "ClaimQueryBackendV1",
    "ClaimQueryFactsV1",
    "ClaimViewRowV1",
    "DirectClaimFactIndex",
    "QueryClaimVisibilityV1",
    "SubjectQueryViewV1",
    "SubjectViewAdjacencyV1",
    "SubjectViewRowV1",
    "VisibleClaimRow",
    "adjacency_rows",
    "claim_row_visibility",
    "claim_view_row",
    "render_subject_query_view",
    "subject_query_view",
    "subject_query_view_digest",
    "subject_view_row",
]
