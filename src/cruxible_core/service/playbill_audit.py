"""Visibility-first G9 audit patrol and operational coverage accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes
from cruxible_client.contracts.captures import parse_capture_envelope
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_attestations import VerifiedClaimAttestationV1
from cruxible_client.contracts.claim_verdicts import (
    CaptureVerdictEvidenceV1,
    ClaimVerdictResultAny,
    ClaimVerdictResultV2,
    EvidenceCurrency,
    EvidenceRelativeClaimVerdict,
    evaluate_claim_verdict,
)
from cruxible_client.contracts.claims import (
    ClaimLawEvidenceAny,
    claim_artifact_digest,
    parse_claim,
    parse_claim_law_evidence,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.providers import ProviderV1
from cruxible_client.contracts.query.definitions import QueryEvaluationPolicyV1
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.audit import (
    AUDIT_PARTITION_ID,
    AuditBudgetV1,
    AuditClaimFactorsV1,
    AuditClaimRowV1,
    AuditCoverageV1,
    AuditCoveredClaimV1,
    AuditCursorV1,
    AuditDependentRefV1,
    AuditEvidenceRefV1,
    AuditRunV1,
    AuditScopeV1,
    audit_result_digest,
    audit_row_order,
    audit_scope_digest,
    build_audit_cursor,
    build_audit_run,
    build_reverse_dependency_index,
)
from cruxible_core.playbill.consumption import consumption_aggregate
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.query.backends import ClaimFactRowV1, claim_row_visibility
from cruxible_core.playbill.review_operational import (
    ReviewOperationalHeadV1,
    ReviewOperationalStoreError,
    build_review_operational_head,
)
from cruxible_core.service.playbill_query import build_accepted_query_facts

_ALL_VERDICTS: tuple[EvidenceRelativeClaimVerdict, ...] = (
    "contradicted",
    "stale",
    "supported",
    "uncovered",
    "unresolved",
)
_ALL_CURRENCY: tuple[EvidenceCurrency, ...] = ("current", "not_applicable", "stale")
_AUDIT_VISIBILITY_POLICY = QueryEvaluationPolicyV1(
    visible_verdicts=_ALL_VERDICTS,
    visible_currency=_ALL_CURRENCY,
    conflict_behavior="surface_conflicts",
)


class PlaybillAuditError(PlaybillError):
    code = "playbill.audit.invalid"

    @property
    def error_code(self) -> str:
        return self.code

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class PlaybillAuditCoordinateNotAccepted(PlaybillAuditError):
    code = "playbill.audit.coordinate_not_accepted"


class PlaybillAuditCursorInvalid(PlaybillAuditError):
    code = "playbill.audit.cursor_invalid"


class PlaybillAuditAccessProfileInvalid(PlaybillAuditError):
    code = "playbill.audit.access_profile_invalid"


class PlaybillAuditBudgetInvalid(PlaybillAuditError):
    code = "playbill.audit.budget_invalid"


class PlaybillAuditOperationalStoreInvalid(PlaybillAuditError):
    code = "playbill.audit.operational_store_invalid"


class _StrictAuditServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillAuditRequestV1(_StrictAuditServiceModel):
    tag: Literal["playbill-audit-request-v1"] = "playbill-audit-request-v1"
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime
    access_profile: CoverageAccessProfileV1
    scope: AuditScopeV1 = AuditScopeV1()
    budget: AuditBudgetV1 = AuditBudgetV1()
    cursor: AuditCursorV1 | None = None

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit evaluation_time must be an absolute instant")
        return value

    @model_validator(mode="after")
    def _cursor_shape(self) -> PlaybillAuditRequestV1:
        if self.cursor is not None and self.at is not None and self.cursor.coordinate != self.at:
            raise ValueError("audit cursor and request coordinate differ")
        return self


class PlaybillAuditResultV1(_StrictAuditServiceModel):
    tag: Literal["playbill-audit-result-v1"] = "playbill-audit-result-v1"
    coordinate: AcceptedCoordinate
    generation: int
    evaluation_time: datetime
    operational_input_head_digest: str
    audited_through_generation: int | None = None
    rows: tuple[AuditClaimRowV1, ...]
    coverage: AuditCoverageV1
    next_cursor: AuditCursorV1 | None = None
    result_digest: str

    @field_validator("operational_input_head_digest", "result_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> PlaybillAuditResultV1:
        if self.coverage.access_permitted != (self.audited_through_generation is not None):
            raise ValueError("only a completed audit advances audited-through generation")
        if self.result_digest != audit_result_digest(self):
            raise ValueError("audit result digest does not reproduce")
        return self


@dataclass(frozen=True)
class _AuditHistoryIndex:
    claim_lineages: dict[str, tuple[str, ...]]
    first_statement_generation: dict[tuple[str, str], int]
    lineage_creation_actor: dict[tuple[str, str], str]
    attestation_first_generation: dict[tuple[str, str, str], int]


def _generation(instance: PlaybillInstance, coordinate: AcceptedCoordinate) -> int:
    matches = tuple(
        item.sequence for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if len(matches) != 1:
        raise PlaybillAuditCoordinateNotAccepted("audit coordinate is not accepted here")
    return matches[0]


def _resolve_coordinate(
    instance: PlaybillInstance, requested: AcceptedCoordinate | None
) -> tuple[AcceptedProjectionCoordinate, AcceptedCoordinate]:
    if requested is None:
        internal = instance.accepted_coordinate()
        return internal, AcceptedCoordinate.from_internal(internal)
    try:
        internal = instance.resolve_accepted_coordinate(
            git_oid=requested.git_oid,
            semantic_root=requested.semantic_root,
            generation_root=requested.generation_root,
            compiler_digest=requested.compiler_digest,
        )
    except (KeyError, PlaybillError, ValueError) as exc:
        raise PlaybillAuditCoordinateNotAccepted("audit coordinate is not accepted here") from exc
    return internal, requested


def _operational_input_head(instance: PlaybillInstance) -> ReviewOperationalHeadV1:
    """Commit every operational input while excluding prior audit outputs."""

    head = instance.review_operational_store().head()
    partitions = tuple(item for item in head.partitions if item.family != "audit")
    return build_review_operational_head(
        initialized_coordinate=(head.initialized_coordinate if partitions else None),
        initialized_generation=(head.initialized_generation if partitions else None),
        partitions=partitions,
    )


def _record_claim_law_evidence(record: object) -> tuple[tuple[str, ClaimLawEvidenceAny], ...]:
    found: list[tuple[str, ClaimLawEvidenceAny]] = []
    for member in getattr(record, "law_evidence", ()):
        raw = member.result.get("claim_evidence")
        if raw is not None:
            found.append((member.path, parse_claim_law_evidence(raw)))
    return tuple(found)


def _history_index(
    instance: PlaybillInstance,
    *,
    current_claims: Mapping[str, ClaimFactRowV1],
    target_generation: int,
) -> _AuditHistoryIndex:
    paths = set(current_claims)
    lineages: dict[str, set[str]] = {path: set() for path in paths}
    first: dict[tuple[str, str], int] = {}
    actors: dict[tuple[str, str], str] = {}
    attestations: dict[tuple[str, str, str], int] = {}
    for generation in instance.accepted_history():
        if generation.sequence > target_generation:
            break
        tree = instance.tree_at(generation.oid)
        for path in paths:
            raw = tree.get(path)
            if raw is not None:
                lineages[path].add(claim_artifact_digest(parse_claim(raw, path=path)).tagged)
        record = generation.record
        if record is None:
            continue
        actor = record.actor_binding.actor_id
        for path, law in _record_claim_law_evidence(record):
            if path not in paths:
                continue
            key = (path, law.statement_digest)
            first.setdefault(key, generation.sequence)
            actors.setdefault(key, actor)
            for attestation in law.verified_attestations:
                attestations.setdefault(
                    (path, law.statement_digest, attestation.attestation_digest),
                    generation.sequence,
                )
    for path, row in current_claims.items():
        key = (path, row.accepted.statement_digest)
        first.setdefault(key, target_generation)
        lineages[path].add(row.accepted.artifact_digest)
    return _AuditHistoryIndex(
        claim_lineages={
            path: tuple(sorted(digests, key=lambda item: item.encode("ascii")))
            for path, digests in lineages.items()
        },
        first_statement_generation=first,
        lineage_creation_actor=actors,
        attestation_first_generation=attestations,
    )


def _connected_to_lineage_actor(
    *,
    attestation: VerifiedClaimAttestationV1,
    lineage_actor: str | None,
    providers: Mapping[str, ProviderV1],
) -> bool:
    if lineage_actor is None:
        return True
    pending = [
        attestation.statement.provider_or_principal,
        *attestation.upstream_provenance,
    ]
    seen: set[str] = set()
    while pending:
        identity = pending.pop()
        if identity.qualified in seen:
            continue
        seen.add(identity.qualified)
        if identity.kind == "Principal" and identity.name == lineage_actor:
            return True
        provider = providers.get(identity.qualified)
        if provider is not None:
            if provider.control_domain == lineage_actor:
                return True
            pending.extend(provider.upstream_provenance)
    return attestation.control_domain == lineage_actor


def _current_verdict(
    row: ClaimFactRowV1,
    *,
    evaluation_time: datetime,
    providers: Mapping[str, ProviderV1],
) -> ClaimVerdictResultAny:
    statement = row.accepted.claim.statement
    return evaluate_claim_verdict(
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


def _logical_source_keys(
    instance: PlaybillInstance, captures: tuple[CaptureVerdictEvidenceV1, ...]
) -> dict[str, str]:
    result: dict[str, str] = {}
    access = BodyAccessContext(principal_id="playbill-audit", can_read_body=True)
    store = instance.body_store()
    for capture in captures:
        try:
            envelope = parse_capture_envelope(store.read(capture.capture_digest, access=access))
        except (OSError, PlaybillError, ValueError) as exc:
            raise PlaybillAuditError(
                f"supporting Capture cannot be reproduced: {capture.capture_digest}"
            ) from exc
        result[capture.capture_digest] = getattr(
            envelope.source,
            "source_identity",
            capture.producer.qualified,
        )
    return result


def _row(
    instance: PlaybillInstance,
    *,
    row: ClaimFactRowV1,
    subject_identity: ArtifactIdentity,
    generation: int,
    evaluation_time: datetime,
    providers: Mapping[str, ProviderV1],
    dependents: tuple[AuditDependentRefV1, ...],
    qualifying_consumption_touch_count: int,
    history: _AuditHistoryIndex,
) -> AuditClaimRowV1:
    verdict = _current_verdict(row, evaluation_time=evaluation_time, providers=providers)
    effective = {
        digest for component in verdict.control_components for digest in component.evidence_digests
    }.intersection(verdict.supporting_evidence_digests)
    captures = tuple(item for item in row.captures if item.capture_digest in effective)
    components = tuple(
        item for item in verdict.control_components if effective.intersection(item.evidence_digests)
    )
    source_keys = _logical_source_keys(instance, captures)
    statement_key = (row.accepted.path, row.accepted.statement_digest)
    first_generation = history.first_statement_generation[statement_key]
    lineage_actor = history.lineage_creation_actor.get(statement_key)
    independent = tuple(
        item
        for item in row.attestations
        if not _connected_to_lineage_actor(
            attestation=item,
            lineage_actor=lineage_actor,
            providers=providers,
        )
    )
    verification_generations = tuple(
        history.attestation_first_generation.get(
            (row.accepted.path, row.accepted.statement_digest, item.attestation_digest),
            first_generation,
        )
        for item in independent
    )
    last_verification = (
        max(verification_generations) if verification_generations else first_generation
    )
    independent_support = {
        item.attestation_digest for item in independent if item.attestation_digest in effective
    }
    near_horizon = False
    if isinstance(verdict, ClaimVerdictResultV2) and row.rule.max_evidence_age is not None:
        quarter = timedelta(microseconds=row.rule.max_evidence_age.microseconds) / 4
        near_horizon = any(
            item.capture_digest in effective
            and evaluation_time < item.expires_at
            and item.expires_at - evaluation_time <= quarter
            for item in verdict.freshness_expirations
        )
    single_source = bool(source_keys) and len(set(source_keys.values())) == 1
    proposer_only = (
        bool(captures)
        and all(item.provenance_grade == "self-asserted" for item in captures)
        and not independent_support
    )
    factors = AuditClaimFactorsV1(
        unique_dependent_count=len(dependents),
        qualifying_consumption_touch_count=qualifying_consumption_touch_count,
        stake=1 + len(dependents) + qualifying_consumption_touch_count,
        single_source=single_source,
        proposer_observed_only=proposer_only,
        zero_corroboration=len(components) < 2,
        near_freshness_horizon=near_horizon,
        weakness=1
        + sum(
            int(item)
            for item in (
                single_source,
                proposer_only,
                len(components) < 2,
                near_horizon,
            )
        ),
        first_accepted_generation=first_generation,
        last_independent_verification_generation=last_verification,
        never_verified=not verification_generations,
        staleness=1 + generation - last_verification,
    )
    refs: list[AuditEvidenceRefV1] = [
        AuditEvidenceRefV1(
            kind="accepted_claim",
            identity=row.accepted.claim.identity.qualified,
            artifact_digest=row.accepted.artifact_digest,
            generation=generation,
            facts={
                "statement_digest": row.accepted.statement_digest,
                "verdict": verdict.verdict,
                "currency": verdict.currency,
            },
        ),
        AuditEvidenceRefV1(
            kind="claim_type",
            identity=f"ClaimType:{row.accepted.claim.statement.predicate}",
            artifact_digest=row.rule.claim_type_digest,
        ),
        AuditEvidenceRefV1(
            kind="consumption_aggregate",
            identity=row.accepted.claim.identity.qualified,
            facts={"qualifying_touch_count": qualifying_consumption_touch_count},
        ),
    ]
    refs.extend(
        AuditEvidenceRefV1(
            kind="dependent",
            identity=item.identity.qualified,
            facts={"dependent_kind": item.kind, "path": item.path},
        )
        for item in dependents
    )
    refs.extend(
        AuditEvidenceRefV1(
            kind="supporting_capture",
            identity=item.capture_digest,
            artifact_digest=item.capture_digest,
            facts={
                "logical_source_key": source_keys[item.capture_digest],
                "provenance_grade": item.provenance_grade,
            },
        )
        for item in captures
    )
    refs.extend(
        AuditEvidenceRefV1(
            kind="claim_attestation",
            identity=item.attestation_digest,
            artifact_digest=item.attestation_digest,
            generation=history.attestation_first_generation.get(
                (row.accepted.path, row.accepted.statement_digest, item.attestation_digest)
            ),
            facts={
                "independent_of_lineage_creator": item in independent,
                "stance": item.statement.stance,
            },
        )
        for item in row.attestations
    )
    ordered_refs = tuple(
        sorted(refs, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
    )
    return AuditClaimRowV1(
        claim_path=row.accepted.path,
        claim_identity=row.accepted.claim.identity,
        claim_artifact_digest=row.accepted.artifact_digest,
        claim_statement_digest=row.accepted.statement_digest,
        subject_identity=subject_identity,
        claim_type_identity=ArtifactIdentity(
            kind="ClaimType", name=row.accepted.claim.statement.predicate
        ),
        verdict=verdict.verdict,
        currency=verdict.currency,
        factors=factors,
        rank_score=factors.stake * factors.weakness * factors.staleness,
        evidence_refs=ordered_refs,
    )


def _cursor_offset(
    request: PlaybillAuditRequestV1,
    *,
    coordinate: AcceptedCoordinate,
    operational_input_head_digest: str,
) -> int:
    cursor = request.cursor
    if cursor is None:
        return 0
    if (
        cursor.coordinate != coordinate
        or cursor.evaluation_time != request.evaluation_time
        or cursor.operational_input_head_digest != operational_input_head_digest
        or cursor.scope_digest != audit_scope_digest(request.scope)
    ):
        raise PlaybillAuditCursorInvalid(
            "audit cursor is stale or belongs to a different request scope"
        )
    return cursor.next_offset


def _result(
    *,
    coordinate: AcceptedCoordinate,
    generation: int,
    evaluation_time: datetime,
    operational_input_head_digest: str,
    audited_through_generation: int | None,
    rows: tuple[AuditClaimRowV1, ...],
    coverage: AuditCoverageV1,
    next_cursor: AuditCursorV1 | None,
) -> PlaybillAuditResultV1:
    placeholder = "sha256:" + "0" * 64
    draft = PlaybillAuditResultV1.model_construct(
        tag="playbill-audit-result-v1",
        coordinate=coordinate,
        generation=generation,
        evaluation_time=evaluation_time,
        operational_input_head_digest=operational_input_head_digest,
        audited_through_generation=audited_through_generation,
        rows=rows,
        coverage=coverage,
        next_cursor=next_cursor,
        result_digest=placeholder,
    )
    return PlaybillAuditResultV1(
        coordinate=coordinate,
        generation=generation,
        evaluation_time=evaluation_time,
        operational_input_head_digest=operational_input_head_digest,
        audited_through_generation=audited_through_generation,
        rows=rows,
        coverage=coverage,
        next_cursor=next_cursor,
        result_digest=audit_result_digest(draft),
    )


def completed_audit_runs(instance: PlaybillInstance) -> tuple[AuditRunV1, ...]:
    """Replay only validated completed runs; incomplete attempts never exist."""

    try:
        return tuple(
            AuditRunV1.model_validate(payload)
            for _event, payload in instance.review_operational_store().events(family="audit")
        )
    except ValueError as exc:
        raise PlaybillAuditOperationalStoreInvalid(
            "completed audit run payload is malformed"
        ) from exc


def _service_playbill_audit(
    instance: PlaybillInstance,
    *,
    request: PlaybillAuditRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillAuditResultV1:
    """Rank visible live Claims and append one idempotent completed-run record."""

    internal, coordinate = _resolve_coordinate(
        instance,
        request.cursor.coordinate if request.cursor is not None else request.at,
    )
    generation = _generation(instance, coordinate)
    input_head = _operational_input_head(instance)
    if not request.access_profile.permits("instance"):
        coverage = AuditCoverageV1(
            access_permitted=False,
            declared_scope=request.scope,
            covered_claims=(),
            candidate_claim_count=0,
            returned_claim_count=0,
            omitted_claim_count=0,
        )
        return _result(
            coordinate=coordinate,
            generation=generation,
            evaluation_time=request.evaluation_time,
            operational_input_head_digest=input_head.head_digest,
            audited_through_generation=None,
            rows=(),
            coverage=coverage,
            next_cursor=None,
        )

    facts = build_accepted_query_facts(instance, coordinate=internal)
    subjects = {item.path: item for item in facts.subjects}
    providers = {item.identity.qualified: item for item in facts.providers}
    visible: dict[str, ClaimFactRowV1] = {}
    subject_identities: dict[str, ArtifactIdentity] = {}
    for row in facts.claims:
        subject = subjects.get(row.subject_path)
        shown = claim_row_visibility(
            row,
            subject=subject,
            providers=providers,
            policy=_AUDIT_VISIBILITY_POLICY,
            evaluation_time=request.evaluation_time,
        )
        if shown is None or subject is None or row.accepted.claim.lifecycle.state != "live":
            continue
        claim_type_identity = f"ClaimType:{row.accepted.claim.statement.predicate}"
        if request.scope.claim_type_identities and (
            claim_type_identity not in request.scope.claim_type_identities
        ):
            continue
        if request.scope.subject_kinds and (
            subject.shell.subject_kind not in request.scope.subject_kinds
        ):
            continue
        visible[row.accepted.path] = row
        subject_identities[row.accepted.path] = subject.shell.identity

    history = _history_index(
        instance,
        current_claims=visible,
        target_generation=generation,
    )
    tree = instance.tree_at(coordinate.git_oid)
    reverse_index = build_reverse_dependency_index(
        tree=tree,
        facts=facts,
        claim_lineages=history.claim_lineages,
    )
    aggregate = consumption_aggregate(instance)
    consumption = {
        item.artifact_identity.qualified: item.qualifying_touch_count
        for item in aggregate.artifacts
    }
    ranked = tuple(
        sorted(
            (
                _row(
                    instance,
                    row=row,
                    subject_identity=subject_identities[path],
                    generation=generation,
                    evaluation_time=request.evaluation_time,
                    providers=providers,
                    dependents=reverse_index.get(path, ()),
                    qualifying_consumption_touch_count=consumption.get(
                        row.accepted.claim.identity.qualified, 0
                    ),
                    history=history,
                )
                for path, row in visible.items()
            ),
            key=audit_row_order,
        )
    )
    offset = _cursor_offset(
        request,
        coordinate=coordinate,
        operational_input_head_digest=input_head.head_digest,
    )
    if offset > len(ranked):
        raise PlaybillAuditCursorInvalid("audit cursor offset exceeds the ranked worklist")
    kept = ranked[offset : offset + request.budget.max_rows]
    reasons: set[Literal["byte_budget_exceeded", "row_budget_exceeded"]] = set()
    if len(kept) < len(ranked) - offset or offset:
        reasons.add("row_budget_exceeded")
    while (
        kept
        and len(canonical_bytes([item.model_dump(mode="json") for item in kept]))
        > request.budget.max_bytes
    ):
        kept = kept[:-1]
        reasons.add("byte_budget_exceeded")
    next_offset = offset + len(kept)
    next_cursor = (
        build_audit_cursor(
            coordinate=coordinate,
            evaluation_time=request.evaluation_time,
            operational_input_head_digest=input_head.head_digest,
            scope_digest=audit_scope_digest(request.scope),
            next_offset=next_offset,
        )
        if kept and next_offset < len(ranked)
        else None
    )
    covered = tuple(
        AuditCoveredClaimV1(
            claim_identity=item.claim_identity,
            artifact_digest=item.claim_artifact_digest,
        )
        for item in sorted(ranked, key=lambda row: row.claim_identity.qualified.encode("utf-8"))
    )
    coverage = AuditCoverageV1(
        access_permitted=True,
        declared_scope=request.scope,
        covered_claims=covered,
        candidate_claim_count=len(ranked),
        returned_claim_count=len(kept),
        omitted_claim_count=len(ranked) - len(kept),
        omission_reasons=tuple(sorted(reasons, key=lambda item: item.encode("ascii"))),
    )
    request_payload = request.model_dump(mode="json")
    provisional = _result(
        coordinate=coordinate,
        generation=generation,
        evaluation_time=request.evaluation_time,
        operational_input_head_digest=input_head.head_digest,
        audited_through_generation=generation,
        rows=kept,
        coverage=coverage,
        next_cursor=next_cursor,
    )
    run = build_audit_run(
        request=request_payload,
        accepted_coordinate=coordinate,
        accepted_generation=generation,
        evaluation_time=request.evaluation_time,
        access_profile_id=request.access_profile.profile_id,
        budget=request.budget,
        operational_input_head_digest=input_head.head_digest,
        coverage=coverage,
        result_digest=provisional.result_digest,
    )
    result = provisional
    # Audit-run output is excluded from the next audit input head.  The public
    # result therefore stays byte-identical across an idempotent retry while
    # the internal run ID commits its exact result digest.
    instance.review_operational_store().append(
        family="audit",
        partition_id=AUDIT_PARTITION_ID,
        event_id=run.audit_run_id,
        payload=run,
        coordinate=coordinate,
        generation=generation,
        actor_context=actor_context,
        recorded_at=actor_context.timestamp,
    )
    return result


def validate_playbill_audit_request(
    value: PlaybillAuditRequestV1 | Mapping[str, object],
) -> PlaybillAuditRequestV1:
    if isinstance(value, PlaybillAuditRequestV1):
        return value
    try:
        return PlaybillAuditRequestV1.model_validate(value)
    except ValidationError as exc:
        roots = {str(item["loc"][0]) for item in exc.errors() if item["loc"]}
        if "access_profile" in roots:
            raise PlaybillAuditAccessProfileInvalid(
                "audit access profile is not a valid closed profile"
            ) from exc
        if "budget" in roots:
            raise PlaybillAuditBudgetInvalid("audit budget is outside the frozen ceilings") from exc
        if "cursor" in roots:
            raise PlaybillAuditCursorInvalid("audit cursor is malformed") from exc
        raise PlaybillAuditError("audit request is malformed") from exc


def service_playbill_audit(
    instance: PlaybillInstance,
    *,
    request: PlaybillAuditRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillAuditResultV1:
    try:
        return _service_playbill_audit(
            instance,
            request=request,
            actor_context=actor_context,
        )
    except ReviewOperationalStoreError as exc:
        raise PlaybillAuditOperationalStoreInvalid(
            "audit operational inputs or completion store failed verification"
        ) from exc


__all__ = [
    "PlaybillAuditCoordinateNotAccepted",
    "PlaybillAuditCursorInvalid",
    "PlaybillAuditAccessProfileInvalid",
    "PlaybillAuditBudgetInvalid",
    "PlaybillAuditError",
    "PlaybillAuditOperationalStoreInvalid",
    "PlaybillAuditRequestV1",
    "PlaybillAuditResultV1",
    "completed_audit_runs",
    "service_playbill_audit",
    "validate_playbill_audit_request",
]
