"""Mechanical G9 curation detector folds over accepted and operational facts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction

from cruxible_client.contracts.artifacts import ArtifactIdentity, parse_artifact_identity
from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV1,
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
)
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    parse_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_attestations import VerifiedClaimAttestationV1
from cruxible_client.contracts.claim_types import parse_claim_type
from cruxible_client.contracts.claim_verdicts import (
    CaptureVerdictEvidenceV1,
    ClaimVerdictResultAny,
    EvidenceCurrency,
    EvidenceRelativeClaimVerdict,
    evaluate_claim_verdict,
    evidence_control_components,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    LiteralClaimObject,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.providers import ProviderV1
from cruxible_client.contracts.query.definitions import QueryEvaluationPolicyV1
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_client.contracts.subjects import AcceptedSubject, SubjectShell, parse_subject
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.claim_slots import classify_claim_slot
from cruxible_core.playbill.closure import dependency_artifacts, parse_dependency_artifact
from cruxible_core.playbill.consumption import consumption_aggregate
from cruxible_core.playbill.curation import (
    CURATION_PATTERN_KINDS,
    CurationCoverageOmissionReason,
    CurationDetectionV1,
    CurationDetectorCoverageV1,
    CurationEvidenceRefV1,
    CurationPatternKind,
    build_curation_detection,
)
from cruxible_core.playbill.curation_calibration import (
    ADMISSION_FAILURE_MINIMUM_DISTINCT_DURABLE_ATTEMPTS,
    BLOCK_CHURN_ACCEPTED_GENERATION_WINDOW,
    BLOCK_CHURN_MINIMUM_DISTINCT_BODY_DIGESTS,
    BLOCK_CHURN_MINIMUM_OBSERVED_GENERATIONS,
    DEAD_VOCABULARY_MINIMUM_ZERO_TOUCH_GENERATIONS,
    DUPLICATE_STATEMENT_MINIMUM_LIVE_CLAIM_IDENTITIES,
    FRESHNESS_MINIMUM_CHANGED_COMMITMENT_INTERVALS,
    FRESHNESS_RATIO_LOWER,
    FRESHNESS_RATIO_UPPER,
    LITERAL_SUBJECT_REFERENCE_DETECTOR_ENABLED,
    PROVENANCE_CONCENTRATED_CONTROL_COMPONENT_COUNT,
    PROVENANCE_MINIMUM_ACTIVE_WRITING_PRINCIPALS,
    PROVENANCE_MINIMUM_LIVE_SUPPORTED_CLAIMS,
    QUALIFIER_MINIMUM_DISTINCT_SUBJECT_ADDRESSES,
    RECURRING_CONFLICT_MINIMUM_UNRESOLVED_SLOTS,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.query.backends import ClaimFactRowV1, claim_row_visibility
from cruxible_core.playbill.review_operational import PlaybillReviewOperationalEventV1
from cruxible_core.service.playbill_query import build_accepted_query_facts

_ALL_VERDICTS: tuple[EvidenceRelativeClaimVerdict, ...] = (
    "contradicted",
    "stale",
    "supported",
    "uncovered",
    "unresolved",
)
_ALL_CURRENCY: tuple[EvidenceCurrency, ...] = ("current", "not_applicable", "stale")
_CURATION_VISIBILITY_POLICY = QueryEvaluationPolicyV1(
    visible_verdicts=_ALL_VERDICTS,
    visible_currency=_ALL_CURRENCY,
    conflict_behavior="surface_conflicts",
)

_SCHEMA_DEFINING_ARTIFACT_KINDS = frozenset(
    {
        "claim-type",
        "procedure",
        "query-definition",
        "capture-contract",
        "standing-mandate",
        "source-acquisition-policy",
        "provider",
    }
)
_PAYLOAD_BEARING_ARTIFACT_KINDS = frozenset({"claim", "document", "subject"})


@dataclass(frozen=True)
class CurationDetectorResult:
    detections: tuple[CurationDetectionV1, ...]
    coverage: tuple[CurationDetectorCoverageV1, ...]


@dataclass
class _Coverage:
    pattern_kind: CurationPatternKind
    evaluated: int = 0
    omissions: dict[CurationCoverageOmissionReason, int] | None = None

    def omit(self, reason: CurationCoverageOmissionReason, count: int = 1) -> None:
        if self.omissions is None:
            self.omissions = {}
        self.omissions[reason] = self.omissions.get(reason, 0) + count

    def freeze(self) -> CurationDetectorCoverageV1:
        from cruxible_core.playbill.curation import CurationCoverageOmissionV1

        omissions = self.omissions or {}
        return CurationDetectorCoverageV1(
            pattern_kind=self.pattern_kind,
            status="partial" if omissions else "complete",
            evaluated_fact_count=self.evaluated,
            omissions=tuple(
                CurationCoverageOmissionV1(reason=reason, count=omissions[reason])
                for reason in sorted(omissions, key=lambda item: item.encode("utf-8"))
            ),
        )


@dataclass(frozen=True)
class _CurationHistoryIndex:
    claims: tuple[tuple[int, str, ClaimArtifactAny], ...]
    capture_contract_identities: Mapping[str, str]
    first_accepted_generations: Mapping[str, int]
    last_generation: int


def _curation_history_index(instance: PlaybillInstance) -> _CurationHistoryIndex:
    claims: list[tuple[int, str, ClaimArtifactAny]] = []
    contracts: dict[str, str] = {}
    first: dict[str, int] = {}
    last_generation = 0
    for generation in instance.accepted_history():
        last_generation = generation.sequence
        tree = instance.tree_at(generation.oid)
        for state in dependency_artifacts(tree):
            first.setdefault(state.identity.qualified, generation.sequence)
        for path in sorted(tree, key=lambda item: item.encode("utf-8")):
            if path.startswith("claims/"):
                claims.append((generation.sequence, path, parse_claim(tree[path], path=path)))
            elif path.startswith("capture-contracts/"):
                contract = parse_capture_contract(tree[path], path=path)
                contracts[capture_contract_digest(contract).tagged] = contract.identity.qualified
    return _CurationHistoryIndex(
        claims=tuple(claims),
        capture_contract_identities=contracts,
        first_accepted_generations=first,
        last_generation=last_generation,
    )


def _artifact_ref(
    *,
    identity: ArtifactIdentity,
    path: str,
    generation: int,
    artifact_digest: str,
    statement_digest: str | None = None,
    facts: dict[str, object] | None = None,
) -> CurationEvidenceRefV1:
    return CurationEvidenceRefV1(
        kind="accepted_artifact",
        identity=identity.qualified,
        path=path,
        generation=generation,
        artifact_digest=artifact_digest,
        statement_digest=statement_digest,
        facts=facts or {},
    )


def _slot_key(claims: tuple[ClaimArtifactAny, ...]) -> str:
    first = claims[0]
    statement = first.statement
    return typed_digest(
        Sha256Value,
        "playbill-curation-slot-key-v1",
        {
            "subject": statement.subject.model_dump(mode="json"),
            "predicate": statement.predicate,
            "qualifier": statement.qualifier,
        },
    ).tagged


def _current_claim_rows(
    facts: tuple[ClaimFactRowV1, ...],
    *,
    subjects: tuple[AcceptedSubject, ...],
    providers: tuple[ProviderV1, ...],
    evaluation_time: datetime,
) -> tuple[ClaimFactRowV1, ...]:
    subjects_by_path = {item.path: item for item in subjects}
    providers_by_identity = {item.identity.qualified: item for item in providers}
    return tuple(
        row
        for row in facts
        if claim_row_visibility(
            row,
            subject=subjects_by_path.get(row.subject_path),
            providers=providers_by_identity,
            policy=_CURATION_VISIBILITY_POLICY,
            evaluation_time=evaluation_time,
        )
        is not None
    )


def _recurring_conflicts(
    *,
    tree: Mapping[str, bytes],
    rows: tuple[ClaimFactRowV1, ...],
    generation: int,
) -> tuple[tuple[CurationDetectionV1, ...], CurationDetectorCoverageV1]:
    kind: CurationPatternKind = "playbill.curation.recurring_conflict_per_type.v1"
    coverage = _Coverage(kind)
    types = {
        item.identity.name: (item, state.path, state.artifact_digest)
        for state in dependency_artifacts(tree)
        if state.artifact_kind == "claim-type" and state.lifecycle.state == "live"
        for item in (parse_claim_type(tree[state.path], path=state.path),)
    }
    grouped: dict[tuple[bytes, str, str | None], list[ClaimFactRowV1]] = defaultdict(list)
    for row in rows:
        claim = row.accepted.claim
        grouped[
            (
                canonical_bytes(claim.statement.subject.model_dump(mode="json")),
                claim.statement.predicate,
                claim.statement.qualifier,
            )
        ].append(row)
    by_type: dict[str, list[tuple[ClaimFactRowV1, ...]]] = defaultdict(list)
    for (_subject, predicate, _qualifier), members in grouped.items():
        current_type = types.get(predicate)
        if current_type is None or current_type[0].cardinality != "one":
            continue
        coverage.evaluated += 1
        frozen = tuple(members)
        if classify_claim_slot(item.accepted.claim for item in frozen).resolution == "unresolved":
            by_type[predicate].append(frozen)
    detections: list[CurationDetectionV1] = []
    frozen_coverage = coverage.freeze()
    for predicate in sorted(by_type, key=lambda item: item.encode("utf-8")):
        if len(by_type[predicate]) < RECURRING_CONFLICT_MINIMUM_UNRESOLVED_SLOTS:
            continue
        claim_type, type_path, type_digest = types[predicate]
        refs: list[CurationEvidenceRefV1] = [
            _artifact_ref(
                identity=claim_type.identity,
                path=type_path,
                generation=generation,
                artifact_digest=type_digest,
            )
        ]
        for slot in sorted(
            by_type[predicate], key=lambda item: _slot_key(tuple(x.accepted.claim for x in item))
        ):
            key = _slot_key(tuple(item.accepted.claim for item in slot))
            refs.append(
                CurationEvidenceRefV1(
                    kind="slot",
                    identity=key,
                    generation=generation,
                    facts={
                        "claim_count": len(slot),
                        "contender_count": classify_claim_slot(
                            item.accepted.claim for item in slot
                        ).contender_count,
                    },
                )
            )
            refs.extend(
                _artifact_ref(
                    identity=item.accepted.claim.identity,
                    path=item.accepted.path,
                    generation=generation,
                    artifact_digest=item.accepted.artifact_digest,
                    statement_digest=item.accepted.statement_digest,
                )
                for item in slot
            )
        detections.append(
            build_curation_detection(
                pattern_kind=kind,
                subject=claim_type.identity,
                detail={
                    "cardinality": "one",
                    "slot_partition": "subject+predicate+qualifier",
                },
                coverage=frozen_coverage,
                evidence_refs=tuple(refs),
            )
        )
    return tuple(detections), frozen_coverage


def _qualifier_crystallization(
    *, rows: tuple[ClaimFactRowV1, ...], generation: int
) -> tuple[tuple[CurationDetectionV1, ...], CurationDetectorCoverageV1]:
    kind: CurationPatternKind = "playbill.curation.qualifier_crystallization.v1"
    coverage = _Coverage(kind)
    grouped: dict[tuple[str, str], dict[bytes, list[ClaimFactRowV1]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        statement = row.accepted.claim.statement
        if statement.qualifier is None:
            continue
        coverage.evaluated += 1
        grouped[(statement.predicate, statement.qualifier)][
            canonical_bytes(statement.subject.model_dump(mode="json"))
        ].append(row)
    frozen_coverage = coverage.freeze()
    detections: list[CurationDetectionV1] = []
    for (predicate, qualifier), subjects in sorted(
        grouped.items(), key=lambda item: (item[0][0].encode(), item[0][1].encode())
    ):
        if len(subjects) < QUALIFIER_MINIMUM_DISTINCT_SUBJECT_ADDRESSES:
            continue
        refs = tuple(
            _artifact_ref(
                identity=row.accepted.claim.identity,
                path=row.accepted.path,
                generation=generation,
                artifact_digest=row.accepted.artifact_digest,
                statement_digest=row.accepted.statement_digest,
                facts={"subject": row.accepted.claim.statement.subject.model_dump(mode="json")},
            )
            for key in sorted(subjects)
            for row in subjects[key]
        )
        detections.append(
            build_curation_detection(
                pattern_kind=kind,
                subject=ArtifactIdentity(kind="ClaimType", name=predicate),
                detail={"qualifier": qualifier},
                coverage=frozen_coverage,
                evidence_refs=refs,
            )
        )
    return tuple(detections), frozen_coverage


def _duplicate_statements(
    *,
    tree: Mapping[str, bytes],
    generation: int,
) -> tuple[tuple[CurationDetectionV1, ...], CurationDetectorCoverageV1]:
    kind: CurationPatternKind = "playbill.curation.duplicate_statement_lineages.v1"
    coverage = _Coverage(kind)
    grouped: dict[tuple[str, str], dict[str, CurationEvidenceRefV1]] = defaultdict(dict)
    for state in dependency_artifacts(tree):
        if state.artifact_kind != "claim" or state.lifecycle.state != "live":
            continue
        claim = parse_claim(tree[state.path], path=state.path)
        coverage.evaluated += 1
        statement = claim_statement_digest(claim.statement).tagged
        grouped[(claim.statement.predicate, statement)][claim.identity.qualified] = _artifact_ref(
            identity=claim.identity,
            path=state.path,
            generation=generation,
            artifact_digest=state.artifact_digest,
            statement_digest=statement,
        )
    frozen_coverage = coverage.freeze()
    detections: list[CurationDetectionV1] = []
    for (predicate, statement), lineages in sorted(
        grouped.items(), key=lambda item: (item[0][0].encode(), item[0][1].encode())
    ):
        if len(lineages) < DUPLICATE_STATEMENT_MINIMUM_LIVE_CLAIM_IDENTITIES:
            continue
        detections.append(
            build_curation_detection(
                pattern_kind=kind,
                subject=ArtifactIdentity(kind="ClaimType", name=predicate),
                detail={"statement_digest": statement},
                coverage=frozen_coverage,
                evidence_refs=tuple(
                    lineages[key] for key in sorted(lineages, key=lambda item: item.encode("utf-8"))
                ),
            )
        )
    return tuple(detections), frozen_coverage


def _literal_subject_references(
    *,
    tree: Mapping[str, bytes],
    generation: int,
) -> tuple[tuple[CurationDetectionV1, ...], CurationDetectorCoverageV1]:
    """Flag live literal Claims that exactly equal at least one live Subject ID."""

    kind: CurationPatternKind = "playbill.curation.literal_subject_reference.v1"
    coverage = _Coverage(kind)
    if not LITERAL_SUBJECT_REFERENCE_DETECTOR_ENABLED:
        return (), coverage.freeze()

    subjects_by_id: dict[str, list[tuple[SubjectShell, str, str]]] = defaultdict(list)
    states = tuple(dependency_artifacts(tree))
    for state in states:
        if state.artifact_kind != "subject" or state.lifecycle.state != "live":
            continue
        subject = parse_subject(tree[state.path], path=state.path)
        subjects_by_id[subject.subject_id].append((subject, state.path, state.artifact_digest))

    pending: list[
        tuple[
            ArtifactIdentity,
            str,
            list[str],
            tuple[CurationEvidenceRefV1, ...],
        ]
    ] = []
    for state in states:
        if state.artifact_kind != "claim" or state.lifecycle.state != "live":
            continue
        claim = parse_claim(tree[state.path], path=state.path)
        obj = claim.statement.object
        if not isinstance(obj, LiteralClaimObject) or not isinstance(obj.value, str):
            continue
        coverage.evaluated += 1
        matches = subjects_by_id.get(obj.value, ())
        if not matches:
            continue
        subject_kinds = sorted(
            {item.subject_kind for item, _path, _digest in matches},
            key=lambda item: item.encode("utf-8"),
        )
        refs = [
            _artifact_ref(
                identity=claim.identity,
                path=state.path,
                generation=generation,
                artifact_digest=state.artifact_digest,
                statement_digest=claim_statement_digest(claim.statement).tagged,
            )
        ]
        refs.extend(
            _artifact_ref(
                identity=item.identity,
                path=path,
                generation=generation,
                artifact_digest=artifact_digest,
                facts={"subject_id": item.subject_id, "subject_kind": item.subject_kind},
            )
            for item, path, artifact_digest in matches
        )
        pending.append(
            (
                claim.identity,
                obj.value,
                subject_kinds,
                tuple(refs),
            )
        )
    frozen_coverage = coverage.freeze()
    detections = tuple(
        build_curation_detection(
            pattern_kind=kind,
            subject=identity,
            detail={
                "literal_value": value,
                "matching_subject_kinds": subject_kinds,
                "message": (
                    "literal looks like a subject reference; consider a subject-valued object"
                ),
            },
            coverage=frozen_coverage,
            evidence_refs=refs,
        )
        for identity, value, subject_kinds, refs in pending
    )
    return detections, frozen_coverage


def _block_churn(
    *,
    instance: PlaybillInstance,
    generation: int,
    document_association_omissions: int = 0,
) -> tuple[tuple[CurationDetectionV1, ...], CurationDetectorCoverageV1]:
    from cruxible_core.service.playbill_curation import BlockObservationV1

    kind: CurationPatternKind = "playbill.curation.block_churn.v1"
    coverage = _Coverage(kind)
    for _ in range(document_association_omissions):
        coverage.omit("block_document_association_unavailable")
    current_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    state_by_identity = {
        state.identity.qualified: state for state in dependency_artifacts(current_tree)
    }
    grouped: dict[
        tuple[str, str, str],
        list[tuple[PlaybillReviewOperationalEventV1, BlockObservationV1]],
    ] = defaultdict(list)
    for event, payload in instance.review_operational_store().events(family="block_observation"):
        try:
            observation = BlockObservationV1.model_validate(payload)
        except ValueError:
            coverage.omit("block_observation_invalid")
            continue
        coverage.evaluated += 1
        grouped[
            (
                observation.document_identity.qualified,
                observation.source_id,
                observation.block_id,
            )
        ].append((event, observation))
    frozen_coverage = coverage.freeze()
    detections: list[CurationDetectionV1] = []
    lower = max(0, generation - (BLOCK_CHURN_ACCEPTED_GENERATION_WINDOW - 1))
    for (document, source_id, block_id), observations in sorted(grouped.items()):
        window = tuple(
            sorted(
                (
                    (event, observation)
                    for event, observation in observations
                    if lower <= observation.scan_generation <= generation
                ),
                key=lambda item: (
                    item[1].scan_generation,
                    item[0].sequence,
                    item[0].event_digest,
                ),
            )
        )
        collapsed: list[tuple[PlaybillReviewOperationalEventV1, BlockObservationV1]] = []
        for pair in window:
            if (
                collapsed
                and collapsed[-1][1].marker_summary.observed_body_digest
                == pair[1].marker_summary.observed_body_digest
            ):
                continue
            collapsed.append(pair)
        body_digests = {item.marker_summary.observed_body_digest for _event, item in collapsed}
        generations = {item.scan_generation for _event, item in collapsed}
        if (
            len(body_digests) < BLOCK_CHURN_MINIMUM_DISTINCT_BODY_DIGESTS
            or len(generations) < BLOCK_CHURN_MINIMUM_OBSERVED_GENERATIONS
        ):
            continue
        identity = parse_artifact_identity(document)
        refs = list(
            CurationEvidenceRefV1(
                kind="block_observation",
                identity=observation.observation_id,
                generation=observation.scan_generation,
                event_digest=event.event_digest,
                artifact_digest=observation.marker_summary.observed_body_digest,
                facts={
                    "declared_body_digest": observation.marker_summary.stamp.body_digest,
                    "observation_basis": observation.observation_basis,
                    "request_source_digest": observation.request_source_digest,
                    "scan_coordinate": observation.scan_coordinate.model_dump(mode="json"),
                },
            )
            for event, observation in collapsed
        )
        for backing in sorted(
            {
                item.identity.qualified: item.identity
                for _event, observation in collapsed
                for item in observation.marker_summary.stamp.backing
            }.values(),
            key=lambda item: item.qualified.encode("utf-8"),
        ):
            state = state_by_identity.get(backing.qualified)
            if state is not None:
                refs.append(
                    _artifact_ref(
                        identity=backing,
                        path=state.path,
                        generation=generation,
                        artifact_digest=state.artifact_digest,
                        facts={"projection_backing": True},
                    )
                )
        detections.append(
            build_curation_detection(
                pattern_kind=kind,
                subject=identity,
                detail={"block_id": block_id, "source_id": source_id},
                coverage=frozen_coverage,
                evidence_refs=tuple(refs),
            )
        )
    return tuple(detections), frozen_coverage


def _dead_vocabulary(
    *,
    instance: PlaybillInstance,
    tree: Mapping[str, bytes],
    generation: int,
    operational_head_digest: str,
    history: _CurationHistoryIndex | None = None,
) -> tuple[tuple[CurationDetectionV1, ...], CurationDetectorCoverageV1]:
    kind: CurationPatternKind = "playbill.curation.dead_vocabulary.v1"
    coverage = _Coverage(kind)
    aggregate = consumption_aggregate(instance)
    if not aggregate.initialized or aggregate.consumption_epoch_generation is None:
        coverage.omit("consumption_epoch_uninitialized")
        return (), coverage.freeze()
    by_identity = {item.artifact_identity.qualified: item for item in aggregate.artifacts}
    first = (history or _curation_history_index(instance)).first_accepted_generations
    allowed = {
        "subject": "Subject",
        "claim-type": "ClaimType",
        "query-definition": "QueryDefinition",
        "procedure": "Procedure",
    }
    frozen_coverage: CurationDetectorCoverageV1 | None = None
    detections: list[CurationDetectionV1] = []
    for state in dependency_artifacts(tree):
        family = allowed.get(state.artifact_kind)
        if family is None or state.lifecycle.state != "live":
            continue
        coverage.evaluated += 1
        touches = by_identity.get(state.identity.qualified)
        qualifying = 0 if touches is None else touches.qualifying_touch_count
        since = max(
            first.get(state.identity.qualified, generation),
            aggregate.consumption_epoch_generation,
        )
        if qualifying != 0 or generation - since < DEAD_VOCABULARY_MINIMUM_ZERO_TOUCH_GENERATIONS:
            continue
        if frozen_coverage is None:
            # Final coverage is rebuilt below before these provisional values
            # are returned; the detection builder requires a coverage object.
            frozen_coverage = coverage.freeze()
        refs = (
            _artifact_ref(
                identity=state.identity,
                path=state.path,
                generation=generation,
                artifact_digest=state.artifact_digest,
                facts={"first_accepted_generation": first[state.identity.qualified]},
            ),
            CurationEvidenceRefV1(
                kind="consumption_aggregate",
                identity=state.identity.qualified,
                generation=generation,
                event_digest=operational_head_digest,
                facts={
                    "consumption_epoch_generation": aggregate.consumption_epoch_generation,
                    "qualifying_touch_count": qualifying,
                    "qualifying_zero_since_generation": since,
                },
            ),
        )
        detections.append(
            build_curation_detection(
                pattern_kind=kind,
                subject=state.identity,
                detail={"artifact_family": family},
                coverage=frozen_coverage,
                evidence_refs=refs,
            )
        )
    final_coverage = coverage.freeze()
    # Evidence digests commit coverage, so rebuild after the evaluated count is
    # final instead of letting traversal order leak into output bytes.
    rebuilt = tuple(
        build_curation_detection(
            pattern_kind=item.pattern_kind,
            subject=item.subject,
            detail=item.detail,
            coverage=final_coverage,
            evidence_refs=item.evidence_refs,
        )
        for item in detections
    )
    return rebuilt, final_coverage


def _attempt_subject_from_path(
    *, tree: Mapping[str, bytes], path: str
) -> tuple[ArtifactIdentity, str] | None:
    content = tree.get(path)
    if content is None:
        return None
    try:
        parsed = parse_dependency_artifact(path, content)
    except (PlaybillError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.artifact_kind == "claim":
        claim = parse_claim(content, path=path)
        return claim.statement.claim_type, "payload_side"
    if parsed.artifact_kind in _SCHEMA_DEFINING_ARTIFACT_KINDS:
        return parsed.identity, "schema_side"
    if parsed.artifact_kind in _PAYLOAD_BEARING_ARTIFACT_KINDS:
        return parsed.identity, "payload_side"
    return parsed.identity, "unclassified"


def _admission_failures(
    *, instance: PlaybillInstance
) -> tuple[tuple[CurationDetectionV1, ...], CurationDetectorCoverageV1]:
    kind: CurationPatternKind = "playbill.curation.admission_failure_cluster.v1"
    coverage = _Coverage(kind)
    attempts: dict[tuple[str, str, str], dict[str, CurationEvidenceRefV1]] = defaultdict(dict)
    evidence = instance.proposal_evidence()
    admissions = {item.proposal_id: item for item in evidence.list_admissions()}
    for evaluation in evidence.list_evaluations():
        if evaluation.verdict != "refused":
            continue
        admission = admissions.get(evaluation.proposal_id)
        if admission is None:
            coverage.omit("admission_record_missing")
            continue
        try:
            candidate_tree = instance.proposal_tree(admission.candidate_tree_oid)
            base_tree = instance.tree_at(admission.proposed_base_oid)
        except (OSError, PlaybillError, ValueError):
            coverage.omit("admission_tree_unavailable")
            continue
        attempt_payload = evaluation.model_dump(mode="json")
        attempt_payload.pop("tag")
        attempt_id = typed_digest(
            Sha256Value,
            "playbill-curation-proposal-attempt-v1",
            attempt_payload,
        ).tagged
        for diagnostic in evaluation.diagnostics:
            coverage.evaluated += 1
            path = None if diagnostic.subject is None else diagnostic.subject.artifact_path
            if path is None:
                coverage.omit("admission_subject_unresolved")
                continue
            resolved = _attempt_subject_from_path(tree=candidate_tree, path=path)
            if resolved is None:
                resolved = _attempt_subject_from_path(tree=base_tree, path=path)
            if resolved is None:
                coverage.omit("admission_subject_unresolved")
                continue
            proposal_subject, direction = resolved
            attempts[(proposal_subject.qualified, diagnostic.code, direction)][attempt_id] = (
                CurationEvidenceRefV1(
                    kind="proposal_attempt",
                    identity=attempt_id,
                    path=path,
                    event_digest=evaluation.proposal_id,
                    facts={
                        "diagnostic_code": diagnostic.code,
                        "refusal_direction": direction,
                        "evaluated_base_oid": evaluation.evaluated_base_oid,
                        "proposal_id": evaluation.proposal_id,
                    },
                )
            )

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    for event in coordinator.store.events():
        preflight = event.intent.last_preflight
        if preflight is None or not preflight.frontier.diagnostics:
            continue
        payload = event.intent.payload
        authoring_subject: ArtifactIdentity | None
        authoring_direction: str
        if isinstance(payload, ClaimAuthoringPayloadV1):
            authoring_subject = ArtifactIdentity(kind="ClaimType", name=payload.statement.predicate)
            authoring_direction = "payload_side"
        elif isinstance(payload, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2):
            name = payload.definition.get("name")
            authoring_subject = (
                ArtifactIdentity(kind="Procedure", name=name) if isinstance(name, str) else None
            )
            authoring_direction = "schema_side"
        else:  # pragma: no cover - frozen authoring payload union
            authoring_subject = None
            authoring_direction = "unclassified"
        for authoring_diagnostic in preflight.frontier.diagnostics:
            coverage.evaluated += 1
            if authoring_subject is None:
                coverage.omit("admission_subject_unresolved")
                continue
            attempt_id = preflight.certificate.certificate_digest
            attempts[(authoring_subject.qualified, authoring_diagnostic.code, authoring_direction)][
                attempt_id
            ] = CurationEvidenceRefV1(
                kind="authoring_attempt",
                identity=attempt_id,
                event_digest=event.event_digest,
                facts={
                    "diagnostic_code": authoring_diagnostic.code,
                    "refusal_direction": authoring_direction,
                    "frontier_digest": preflight.frontier.digest,
                    "intent_id": event.intent.intent_id,
                },
            )

    frozen_coverage = coverage.freeze()
    detections: list[CurationDetectionV1] = []
    for (subject_value, code, direction), refs in sorted(
        attempts.items(),
        key=lambda item: (item[0][0].encode(), item[0][1].encode(), item[0][2].encode()),
    ):
        if len(refs) < ADMISSION_FAILURE_MINIMUM_DISTINCT_DURABLE_ATTEMPTS:
            continue
        from cruxible_client.contracts.artifacts import parse_artifact_identity

        detections.append(
            build_curation_detection(
                pattern_kind=kind,
                subject=parse_artifact_identity(subject_value),
                detail={"diagnostic_code": code, "refusal_direction": direction},
                coverage=frozen_coverage,
                evidence_refs=tuple(
                    refs[key] for key in sorted(refs, key=lambda item: item.encode("ascii"))
                ),
            )
        )
    return tuple(detections), frozen_coverage


@dataclass(frozen=True)
class _CaptureObservation:
    predicate: str
    generation: int
    capture_digest: str
    contract_identity: str
    source_identity: str
    selector_type: str
    selector_key: bytes
    selector_digest: str
    coordinate_digest: str
    commitment_digest: str
    observed_at: datetime


def _freshness_calibration(
    *,
    instance: PlaybillInstance,
    tree: Mapping[str, bytes],
    generation: int | None = None,
    history: _CurationHistoryIndex | None = None,
) -> tuple[tuple[CurationDetectionV1, ...], CurationDetectorCoverageV1]:
    kind: CurationPatternKind = "playbill.curation.freshness_drift_calibration.v1"
    coverage = _Coverage(kind)
    current_types = {
        item.identity.name: (item, state.artifact_digest, state.path)
        for state in dependency_artifacts(tree)
        if state.artifact_kind == "claim-type" and state.lifecycle.state == "live"
        for item in (parse_claim_type(tree[state.path], path=state.path),)
        if item.evidence_freshness is not None
    }
    indexed = history or _curation_history_index(instance)
    contracts = indexed.capture_contract_identities
    captures: dict[tuple[str, str], int] = {}
    for accepted_generation, _path, claim in indexed.claims:
        if claim.statement.predicate not in current_types:
            continue
        for capture_digest_value in claim.backing.capture_digests:
            captures.setdefault(
                (claim.statement.predicate, capture_digest_value), accepted_generation
            )
    body_store = instance.body_store()
    access = BodyAccessContext(principal_id="playbill-curation", can_read_body=True)
    observations: list[_CaptureObservation] = []
    for (predicate, capture_digest_value), accepted_generation in sorted(captures.items()):
        coverage.evaluated += 1
        try:
            envelope = parse_capture_envelope(body_store.read(capture_digest_value, access=access))
        except (OSError, PlaybillError, ValueError):
            coverage.omit("drift_series_unavailable")
            continue
        if not isinstance(envelope.source, ExternalSourceReferenceV1):
            coverage.omit("drift_series_unavailable")
            continue
        contract_identity = contracts.get(envelope.capture_contract_digest)
        if contract_identity is None:
            coverage.omit("capture_contract_identity_unresolved")
            continue
        observations.append(
            _CaptureObservation(
                predicate=predicate,
                generation=accepted_generation,
                capture_digest=capture_digest_value,
                contract_identity=contract_identity,
                source_identity=envelope.source.source_identity,
                selector_type=envelope.source.selector_type,
                selector_key=canonical_bytes(envelope.source.selector),
                selector_digest=typed_digest(
                    Sha256Value,
                    "playbill-curation-external-selector-v1",
                    {"value": envelope.source.selector},
                ).tagged,
                coordinate_digest=typed_digest(
                    Sha256Value,
                    "playbill-curation-external-coordinate-v1",
                    {"value": envelope.source.coordinate},
                ).tagged,
                commitment_digest=envelope.commitment.digest,
                observed_at=envelope.observed_at,
            )
        )
    by_group: dict[tuple[str, str, str, str], dict[bytes, list[_CaptureObservation]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for item in observations:
        by_group[
            (
                item.predicate,
                item.contract_identity,
                item.source_identity,
                item.selector_type,
            )
        ][item.selector_key].append(item)
    frozen_coverage = coverage.freeze()
    detections: list[CurationDetectionV1] = []
    for group, selectors in sorted(by_group.items()):
        intervals: list[int] = []
        transitions: list[tuple[_CaptureObservation, _CaptureObservation, int]] = []
        for selector_items in selectors.values():
            ordered = sorted(
                selector_items,
                key=lambda item: (
                    item.observed_at,
                    item.capture_digest.encode("ascii"),
                ),
            )
            previous: _CaptureObservation | None = None
            for current in ordered:
                if previous is not None and current.commitment_digest != previous.commitment_digest:
                    interval = (current.observed_at - previous.observed_at) // timedelta(
                        microseconds=1
                    )
                    if interval > 0:
                        intervals.append(interval)
                        transitions.append((previous, current, interval))
                previous = current
        if len(intervals) < FRESHNESS_MINIMUM_CHANGED_COMMITMENT_INTERVALS:
            continue
        ordered_intervals = sorted(intervals)
        midpoint = len(ordered_intervals) // 2
        median = (
            Fraction(ordered_intervals[midpoint], 1)
            if len(ordered_intervals) % 2
            else Fraction(ordered_intervals[midpoint - 1] + ordered_intervals[midpoint], 2)
        )
        claim_type, type_digest, type_path = current_types[group[0]]
        assert claim_type.evidence_freshness is not None
        horizon = claim_type.evidence_freshness.stale_after.microseconds
        ratio = Fraction(horizon, 1) / median
        if FRESHNESS_RATIO_LOWER <= ratio <= FRESHNESS_RATIO_UPPER:
            continue
        refs: list[CurationEvidenceRefV1] = [
            _artifact_ref(
                identity=claim_type.identity,
                path=type_path,
                generation=(generation if generation is not None else indexed.last_generation),
                artifact_digest=type_digest,
                facts={"stale_after_microseconds": horizon},
            )
        ]
        for prior, current, interval in transitions:
            identity = typed_digest(
                Sha256Value,
                "playbill-curation-capture-transition-v1",
                {
                    "prior_capture_digest": prior.capture_digest,
                    "current_capture_digest": current.capture_digest,
                },
            ).tagged
            refs.append(
                CurationEvidenceRefV1(
                    kind="capture_transition",
                    identity=identity,
                    generation=current.generation,
                    artifact_digest=current.capture_digest,
                    facts={
                        "coordinate_digest": current.coordinate_digest,
                        "current_commitment_digest": current.commitment_digest,
                        "interval_microseconds": interval,
                        "prior_commitment_digest": prior.commitment_digest,
                        "selector_digest": current.selector_digest,
                    },
                )
            )
        refs.append(
            CurationEvidenceRefV1(
                kind="capture_transition",
                identity=typed_digest(
                    Sha256Value,
                    "playbill-curation-freshness-sample-v1",
                    {"group": list(group), "intervals": ordered_intervals},
                ).tagged,
                facts={
                    "changed_interval_count": len(intervals),
                    "median_denominator": median.denominator,
                    "median_numerator": median.numerator,
                    "ratio_denominator": ratio.denominator,
                    "ratio_numerator": ratio.numerator,
                },
            )
        )
        detections.append(
            build_curation_detection(
                pattern_kind=kind,
                subject=claim_type.identity,
                detail={
                    "capture_contract_identity": group[1],
                    "external_source_identity": group[2],
                    "selector_type": group[3],
                },
                coverage=frozen_coverage,
                evidence_refs=tuple(refs),
            )
        )
    return tuple(detections), frozen_coverage


def _provenance_concentration(
    *,
    rows: tuple[ClaimFactRowV1, ...],
    providers: tuple[ProviderV1, ...],
    evaluation_time: datetime,
    generation: int,
    active_writing_principal_count: int,
) -> tuple[tuple[CurationDetectionV1, ...], CurationDetectorCoverageV1]:
    kind: CurationPatternKind = "playbill.curation.provenance_concentration.v1"
    coverage = _Coverage(kind)
    provider_map = {item.identity.qualified: item for item in providers}
    supported: dict[str, list[tuple[ClaimFactRowV1, ClaimVerdictResultAny]]] = defaultdict(list)
    for row in rows:
        coverage.evaluated += 1
        claim = row.accepted.claim
        verdict = evaluate_claim_verdict(
            claim_statement_digest=row.accepted.statement_digest,
            rule=row.rule,
            evaluation_time=evaluation_time,
            captures=row.captures,
            attestations=row.attestations,
            providers=provider_map,
            claim_effective_from=claim.statement.effective_from,
            claim_effective_until=claim.statement.effective_until,
            referent_current=row.referent_current,
            resolved_authority_basis=row.resolved_authority_basis,
        )
        if verdict.verdict == "supported" and verdict.currency == "current":
            supported[claim.statement.predicate].append((row, verdict))
    frozen_coverage = coverage.freeze()
    if active_writing_principal_count < PROVENANCE_MINIMUM_ACTIVE_WRITING_PRINCIPALS:
        return (), frozen_coverage
    detections: list[CurationDetectionV1] = []
    for predicate, members in sorted(supported.items()):
        if len(members) < PROVENANCE_MINIMUM_LIVE_SUPPORTED_CLAIMS:
            continue
        captures: dict[str, CaptureVerdictEvidenceV1] = {}
        attestations: dict[str, VerifiedClaimAttestationV1] = {}
        refs: list[CurationEvidenceRefV1] = []
        for row, verdict in members:
            current_evidence = {
                digest
                for component in verdict.control_components
                for digest in component.evidence_digests
            }
            supporting = set(verdict.supporting_evidence_digests).intersection(current_evidence)
            for capture in row.captures:
                if capture.capture_digest in supporting:
                    captures[capture.capture_digest] = capture
            for attestation in row.attestations:
                if attestation.attestation_digest in supporting:
                    attestations[attestation.attestation_digest] = attestation
            refs.append(
                _artifact_ref(
                    identity=row.accepted.claim.identity,
                    path=row.accepted.path,
                    generation=generation,
                    artifact_digest=row.accepted.artifact_digest,
                    statement_digest=row.accepted.statement_digest,
                    facts={
                        "supporting_evidence_digests": list(verdict.supporting_evidence_digests),
                        "effective_supporting_evidence_digests": sorted(supporting),
                    },
                )
            )
        components = evidence_control_components(
            tuple(captures[key] for key in sorted(captures)),
            tuple(attestations[key] for key in sorted(attestations)),
            providers=provider_map,
        )
        if len(components) != PROVENANCE_CONCENTRATED_CONTROL_COMPONENT_COUNT:
            continue
        component = components[0]
        component_id = typed_digest(
            Sha256Value,
            "playbill-curation-control-component-v1",
            component.model_dump(mode="json"),
        ).tagged
        refs.append(
            CurationEvidenceRefV1(
                kind="control_component",
                identity=component_id,
                facts=component.model_dump(mode="json"),
            )
        )
        detections.append(
            build_curation_detection(
                pattern_kind=kind,
                subject=ArtifactIdentity(kind="ClaimType", name=predicate),
                detail={"basis": "effective_supporting_control_components"},
                coverage=frozen_coverage,
                evidence_refs=tuple(refs),
            )
        )
    return tuple(detections), frozen_coverage


def _active_writing_principal_count(instance: PlaybillInstance, *, git_oid: str) -> int:
    """Count active client principals capable of ordinary governed authorship."""

    generation = next(item for item in instance.accepted_history() if item.oid == git_oid)
    return sum(
        principal.status == "active" and principal.kind == "ordinary"
        for principal in generation.principals.principals
    )


def run_curation_detectors(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
    generation: int,
    evaluation_time: datetime,
    operational_head_digest: str,
    block_document_association_omissions: int = 0,
) -> CurationDetectorResult:
    """Run every frozen v1 detector at the exact current read coordinate."""

    internal = instance.resolve_accepted_coordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler_digest,
    )
    tree = instance.tree_at(internal.git_oid)
    facts = build_accepted_query_facts(instance, coordinate=internal)
    rows = _current_claim_rows(
        facts.claims,
        subjects=facts.subjects,
        providers=facts.providers,
        evaluation_time=evaluation_time,
    )
    history = _curation_history_index(instance)
    results = (
        _recurring_conflicts(tree=tree, rows=rows, generation=generation),
        _admission_failures(instance=instance),
        _freshness_calibration(
            instance=instance,
            tree=tree,
            generation=generation,
            history=history,
        ),
        _provenance_concentration(
            rows=rows,
            providers=facts.providers,
            evaluation_time=evaluation_time,
            generation=generation,
            active_writing_principal_count=_active_writing_principal_count(
                instance, git_oid=internal.git_oid
            ),
        ),
        _duplicate_statements(tree=tree, generation=generation),
        _qualifier_crystallization(rows=rows, generation=generation),
        _block_churn(
            instance=instance,
            generation=generation,
            document_association_omissions=block_document_association_omissions,
        ),
        _dead_vocabulary(
            instance=instance,
            tree=tree,
            generation=generation,
            operational_head_digest=operational_head_digest,
            history=history,
        ),
        _literal_subject_references(tree=tree, generation=generation),
    )
    detections = tuple(
        sorted(
            (item for detected, _coverage in results for item in detected),
            key=lambda item: (
                CURATION_PATTERN_KINDS.index(item.pattern_kind),
                item.pattern_id.encode("ascii"),
            ),
        )
    )
    coverage_by_kind = {item.pattern_kind: item for _detected, item in results}
    return CurationDetectorResult(
        detections=detections,
        coverage=tuple(coverage_by_kind[kind] for kind in CURATION_PATTERN_KINDS),
    )


__all__ = ["CurationDetectorResult", "run_curation_detectors"]
