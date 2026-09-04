"""Canonical service contract for first-class Playbill Claims."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import (
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_attestations import VerifiedClaimAttestationV1
from cruxible_client.contracts.claim_type_structure import claim_type_structural_signature
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    ClaimVerdictResultAny,
    ClaimVerdictResultV1,
    ClaimVerdictResultV2,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV2,
    ClaimArtifactV3,
    ClaimCitationV1,
    ClaimLawEvidenceAny,
    ClaimLawEvidenceV1,
    ClaimStatementCardV1,
    ClaimUnsupportedFormatError,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
    claim_statement_address,
    claim_statement_card,
    claim_statement_digest,
    evaluate_capture_evidence_admissions,
    parse_claim,
    parse_claim_law_evidence,
)
from cruxible_client.contracts.diagnostics import GovernedOperationReference
from cruxible_client.contracts.discovery import ContextCapsuleV1, ExpandRequestV1
from cruxible_client.contracts.errors import (
    ClaimNotFoundError,
    ProposalIntegrityError,
)
from cruxible_client.contracts.policies import (
    ClaimVerdict,
    ResolutionContenderV1,
    resolve_claim_contenders,
)
from cruxible_client.contracts.query.definitions import (
    parse_query_definition,
    query_definition_digest,
)
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    CoverageDescriptorV1,
    ExternalSourceReferenceV1,
    OpenSourceRequestV1,
    SourceDereferenceResultV1,
    SourceHandleV1,
    source_handle_digest,
)
from cruxible_client.contracts.subjects import (
    parse_subject,
    subject_digest,
    subject_reuse_signature,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.dereference import (
    ExternalSelectionReaderProtocol,
    dereference_source_handle,
)
from cruxible_core.playbill.id_prefixes import resolve_id_prefix
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.projection_claims import ClaimProjectionView
from cruxible_core.playbill.query.cards import (
    ClaimTypeUsageRowV1,
    SemanticRelationV1,
    build_claim_type_card,
    build_subject_profile,
)
from cruxible_core.playbill.query.semantic_discovery import DiscoveryEntryV1
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.settlement import ChangeSetRecord


class _StrictClaimServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillClaimView(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-read-v1"] = "playbill-claim-read-v1"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, object]
    facts: tuple[dict[str, object], ...]


class CaptureEvidenceKindAdmissionV1(_StrictClaimServiceModel):
    tag: Literal["playbill-capture-evidence-kind-admission-v1"] = (
        "playbill-capture-evidence-kind-admission-v1"
    )
    evidence_kind: str
    status: Literal["admitted", "not_admitted"]
    rule_id: str | None = None
    admission: Literal["origin_only", "direct", "derivational"] | None = None
    refusal_code: str | None = None
    closest_rule_id: str | None = None


class CaptureAdmissionAccountV1(_StrictClaimServiceModel):
    tag: Literal["playbill-capture-admission-account-v1"] = "playbill-capture-admission-account-v1"
    citation_id: str
    capture_digest: str
    citation_role: Literal["evidence", "copy", "legacy"]
    citation_origin: Literal["independent", "self_source", "self_published", "legacy"]
    capture_contract_identity: str
    capture_contract_digest: str
    status: Literal["admitted", "not_admitted", "not_evidence"]
    decisions: tuple[CaptureEvidenceKindAdmissionV1, ...] = ()


class PlaybillClaimViewV2(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-read-v2"] = "playbill-claim-read-v2"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, object]
    facts: tuple[dict[str, object], ...]
    admission_evaluation_time: datetime
    admission_accounts: tuple[CaptureAdmissionAccountV1, ...]
    statement: ClaimStatementCardV1

    @field_validator("admission_evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("admission evaluation time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _ordered_accounts(self) -> "PlaybillClaimViewV2":
        ids = tuple(item.citation_id for item in self.admission_accounts)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("ascii"))):
            raise ValueError("admission accounts must be sorted and unique")
        return self


class PlaybillClaimList(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-list-v1"] = "playbill-claim-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    claims: tuple[PlaybillClaimView, ...]


class PlaybillClaimQueryResult(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-query-v1"] = "playbill-claim-query-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    subject: SemanticAddress
    predicate: str
    cardinality: Literal["one", "many"]
    status: Literal["resolved", "unresolved", "refused"]
    selected_claim_identities: tuple[str, ...]
    contender_claim_identities: tuple[str, ...]
    claims: tuple[PlaybillClaimView, ...]
    verdicts: tuple[ClaimVerdictResultV1, ...]


class PlaybillClaimQueryResultV2(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-query-v2"] = "playbill-claim-query-v2"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    subject: SemanticAddress
    predicate: str
    cardinality: Literal["one", "many"]
    status: Literal["resolved", "unresolved", "refused"]
    selected_claim_identities: tuple[str, ...]
    contender_claim_identities: tuple[str, ...]
    claims: tuple[PlaybillClaimView, ...]
    verdicts: tuple[ClaimVerdictResultAny, ...]


@dataclass(frozen=True)
class PlaybillClaimGroupResolution:
    """One resolved (Subject, predicate) slot, carried without its wire envelope."""

    subject: SemanticAddress
    predicate: str
    cardinality: Literal["one", "many"]
    status: Literal["resolved", "unresolved", "refused"]
    selected_claim_identities: tuple[str, ...]
    contender_claim_identities: tuple[str, ...]
    claims: tuple[ClaimArtifactAny, ...]
    verdicts: tuple[ClaimVerdictResultAny, ...]


class PlaybillClaimHistoryEntry(_StrictClaimServiceModel):
    sequence: int
    coordinate: PlaybillAcceptedCoordinate
    statement_digest: str
    artifact_digest: str
    predecessor_digest: str | None
    lifecycle_state: Literal["live", "retired"]
    change_set_path: str
    changeset_digest: str
    candidate_digest: str


class PlaybillClaimHistory(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-history-v1"] = "playbill-claim-history-v1"
    identity: str
    entries: tuple[PlaybillClaimHistoryEntry, ...]


class PlaybillClaimExplanationV1(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-explanation-v1"] = "playbill-claim-explanation-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    claim: PlaybillClaimView
    law_evidence: ClaimLawEvidenceV1
    verdict: ClaimVerdictResultV1
    exact_attestations: tuple[VerifiedClaimAttestationV1, ...]
    approval_coverage: Literal["containing_change_set"] = "containing_change_set"
    source_handles: tuple[SourceHandleV1, ...]
    coverage: CoverageDescriptorV1


class PlaybillClaimExplanationV2(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-explanation-v2"] = "playbill-claim-explanation-v2"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    claim: PlaybillClaimView
    law_evidence: ClaimLawEvidenceV1
    verdict: ClaimVerdictResultV1
    exact_attestations: tuple[VerifiedClaimAttestationV1, ...]
    approval_coverage: Literal["containing_change_set"] = "containing_change_set"
    source_handles: tuple[SourceHandleV1, ...]
    coverage: CoverageDescriptorV1
    admission_evaluation_time: datetime
    admission_accounts: tuple[CaptureAdmissionAccountV1, ...]


class EvidenceRecaptureOperationV1(_StrictClaimServiceModel):
    tag: Literal["playbill-evidence-recapture-operation-v1"] = (
        "playbill-evidence-recapture-operation-v1"
    )
    operation: Literal["playbill.authoring.bind"] = "playbill.authoring.bind"
    claim_identity: str
    capture_contract_identity: str
    logical_source: str


class ClaimEvidenceFreshnessLineV1(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-evidence-freshness-line-v1"] = (
        "playbill-claim-evidence-freshness-line-v1"
    )
    capture_digest: str
    citation_ids: tuple[str, ...]
    capture_contract_identity: str
    logical_source: str
    observed_at: datetime
    expires_at: datetime
    state: Literal["current", "expiring", "expired"]
    recapture_operation: EvidenceRecaptureOperationV1


class PlaybillClaimExplanationV3(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-explanation-v3"] = "playbill-claim-explanation-v3"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    claim: PlaybillClaimView
    law_evidence: ClaimLawEvidenceAny
    verdict: ClaimVerdictResultV2
    exact_attestations: tuple[VerifiedClaimAttestationV1, ...]
    approval_coverage: Literal["containing_change_set"] = "containing_change_set"
    source_handles: tuple[SourceHandleV1, ...]
    coverage: CoverageDescriptorV1
    admission_evaluation_time: datetime
    admission_accounts: tuple[CaptureAdmissionAccountV1, ...]
    freshness: tuple[ClaimEvidenceFreshnessLineV1, ...]


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: PlaybillAcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def _public_claim(view: ClaimProjectionView) -> PlaybillClaimView:
    if view.coordinate_kind != "canonical" or not isinstance(
        view.coordinate, AcceptedProjectionCoordinate
    ):
        raise ProposalIntegrityError("canonical Claim service received a provisional view")
    return PlaybillClaimView(
        coordinate=PlaybillAcceptedCoordinate.from_internal(view.coordinate),
        envelope=view.envelope.model_dump(mode="json"),
        facts=tuple(fact.model_dump(mode="json") for fact in view.facts),
    )


def _claim_from_view(view: PlaybillClaimView | PlaybillClaimViewV2) -> ClaimArtifactAny:
    path = view.envelope.get("path")
    if not isinstance(path, str):
        raise ProposalIntegrityError("Claim projection envelope has no path")
    statement = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "playbill.claim.statement"
        ),
        None,
    )
    backing = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "playbill.claim.backing"
        ),
        None,
    )
    lifecycle = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "playbill.claim.lifecycle"
        ),
        None,
    )
    identity = view.envelope.get("identity")
    if not (
        isinstance(identity, str)
        and isinstance(statement, dict)
        and isinstance(backing, dict)
        and isinstance(lifecycle, dict)
    ):
        raise ProposalIntegrityError("Claim projection lacks its complete canonical artifact")
    artifact_format = view.envelope.get("format_tag")
    if artifact_format == "playbill-claim-v2":
        model: type[ClaimArtifactV2] | type[ClaimArtifactV3] = ClaimArtifactV2
    elif artifact_format == "playbill-claim-v3":
        model = ClaimArtifactV3
    else:
        raise ClaimUnsupportedFormatError(
            f"{ClaimUnsupportedFormatError.error_code}: {artifact_format!r}"
        )
    return model.model_validate(
        {
            "artifact_format": artifact_format,
            "identity": {
                "kind": "Claim",
                "name": identity.removeprefix("Claim:"),
            },
            "statement": statement,
            "backing": backing,
            "pins": lifecycle.get("pins"),
            "lifecycle": lifecycle.get("lifecycle"),
            **(
                {"retirement": lifecycle.get("retirement")}
                if artifact_format == "playbill-claim-v3"
                else {}
            ),
        }
    )


def _resolved_claim_id(
    instance: PlaybillInstance,
    identity: str,
    *,
    coordinate: AcceptedProjectionCoordinate,
) -> str:
    """Accept a unique CLM- prefix where a full Claim id is expected."""

    bare = identity.removeprefix("Claim:")
    return resolve_id_prefix(
        bare,
        _accepted_claim_ids(instance, coordinate=coordinate),
        marker="CLM-",
        label="Claim",
    )


def _accepted_claim_ids(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
) -> tuple[str, ...]:
    tree = instance.tree_at(coordinate.git_oid)
    return tuple(
        path.rsplit("/", 1)[-1].removesuffix(".json")
        for path in tree
        if path.startswith("claims/") and path.endswith(".json")
    )


def _observed_at(timestamp: str) -> datetime:
    raw = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ProposalIntegrityError("authenticated request timestamp is malformed") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProposalIntegrityError("authenticated request timestamp must be timezone-aware")
    return value


def service_get_playbill_claim(
    instance: PlaybillInstance,
    *,
    identity: str,
    at: PlaybillAcceptedCoordinate | None = None,
    evaluation_time: datetime | None = None,
) -> PlaybillClaimViewV2:
    expected = "Claim:CLM-<32 lowercase hex> or CLM-<32 lowercase hex>"
    coordinate = _resolve_coordinate(instance, at)
    bare = _resolved_claim_id(instance, identity, coordinate=coordinate)
    try:
        path = claim_path(bare)
    except ValueError as exc:
        raise ClaimNotFoundError(
            f"Claim not found; expected {expected}; received {identity!r}"
        ) from exc
    qualified = f"Claim:{bare}"
    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.sequence == 0:
        raise ClaimNotFoundError(f"Claim not found; expected {expected}; received {identity!r}")
    with instance.bind_accepted_projection(coordinate) as projection:
        claim = projection.claim(qualified)
    if claim is None:
        raise ClaimNotFoundError(f"Claim not found; expected {expected}; received {identity!r}")
    public = _public_claim(claim)
    if public.envelope.get("path") != path:
        raise ProposalIntegrityError("Claim projection path differs from normalized identity")
    evaluated_at = evaluation_time or _accepted_generation_time(instance, coordinate)
    parsed = _claim_from_view(public)
    return PlaybillClaimViewV2(
        coordinate=public.coordinate,
        envelope=public.envelope,
        facts=public.facts,
        admission_evaluation_time=evaluated_at,
        admission_accounts=_claim_admission_accounts(
            instance,
            claim=parsed,
            tree=instance.tree_at(coordinate.git_oid),
            law=_claim_law_evidence(instance, path=path, at=coordinate),
        ),
        statement=claim_statement_card(parsed),
    )


def projected_playbill_claim_views(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
) -> tuple[PlaybillClaimView, ...]:
    """Materialize every projected Claim view at one accepted coordinate."""

    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.sequence == 0:
        return ()
    with instance.bind_accepted_projection(coordinate) as projection:
        return tuple(_public_claim(item) for item in projection.list_claims())


def service_list_playbill_claims(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate | None = None,
    subject: SemanticAddress | None = None,
    predicate: str | None = None,
    include_retired: bool = False,
) -> PlaybillClaimList:
    coordinate = _resolve_coordinate(instance, at)
    projected = projected_playbill_claim_views(instance, coordinate=coordinate)
    claims: tuple[PlaybillClaimView, ...]
    if include_retired and subject is None and predicate is None:
        claims = projected
    else:
        # One reconstruction per row: the filter used to rebuild the whole
        # ClaimArtifact once per bound predicate in the conjunction.
        selected: list[PlaybillClaimView] = []
        for item in projected:
            parsed = _claim_from_view(item)
            if not include_retired and parsed.lifecycle.state != "live":
                continue
            if subject is not None and parsed.statement.subject != subject:
                continue
            if predicate is not None and parsed.statement.predicate != predicate:
                continue
            selected.append(item)
        claims = tuple(selected)
    return PlaybillClaimList(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        claims=claims,
    )


def _claim_law_evidence(
    instance: PlaybillInstance,
    *,
    path: str,
    at: AcceptedProjectionCoordinate,
) -> ClaimLawEvidenceAny:
    found = _claim_law_evidence_index(instance, at=at).get(path)
    if found is None:
        raise ProposalIntegrityError("accepted Claim has no reproducible Claim law evidence")
    return found


def _claim_law_evidence_index(
    instance: PlaybillInstance,
    *,
    at: AcceptedProjectionCoordinate,
) -> dict[str, ClaimLawEvidenceAny]:
    """Index the latest accepted Claim evidence with one history traversal."""

    found: dict[str, ClaimLawEvidenceAny] = {}
    target_sequence = next(
        item.sequence for item in instance.accepted_history() if item.oid == at.git_oid
    )
    for generation in instance.accepted_history()[1:]:
        if generation.sequence > target_sequence:
            break
        record = generation.record
        # The v1 receipt predates structured member law evidence; every later
        # receipt version carries it in the same shape.
        if record is None or isinstance(record, ChangeSetRecord):
            continue
        for evidence in record.law_evidence:
            raw = evidence.result.get("claim_evidence")
            if raw is not None:
                found[evidence.path] = parse_claim_law_evidence(raw)
    return found


def _claim_law_evidence_by_artifact_index(
    instance: PlaybillInstance,
    *,
    at: AcceptedProjectionCoordinate,
) -> dict[tuple[str, str], ClaimLawEvidenceAny]:
    """Index every accepted Claim law account in one bounded history pass."""

    found: dict[tuple[str, str], ClaimLawEvidenceAny] = {}
    target_sequence = next(
        item.sequence for item in instance.accepted_history() if item.oid == at.git_oid
    )
    for generation in instance.accepted_history()[1:]:
        if generation.sequence > target_sequence:
            break
        record = generation.record
        if record is None or isinstance(record, ChangeSetRecord):
            continue
        for evidence in record.law_evidence:
            raw = evidence.result.get("claim_evidence")
            if raw is None:
                continue
            parsed = parse_claim_law_evidence(raw)
            found[(evidence.path, parsed.artifact_digest)] = parsed
    return found


def _accepted_generation_time(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
) -> datetime:
    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.record is None:
        raise ProposalIntegrityError("a Claim read requires an accepted candidate timestamp")
    return datetime.fromisoformat(generation.record.candidate.timestamp.replace("Z", "+00:00"))


def _claim_admission_accounts(
    instance: PlaybillInstance,
    *,
    claim: ClaimArtifactAny,
    tree: dict[str, bytes],
    law: ClaimLawEvidenceAny,
) -> tuple[CaptureAdmissionAccountV1, ...]:
    from cruxible_core.service.playbill_evidence import _capture_contracts

    claim_type_path_value = claim_type_path(claim.statement.predicate)
    claim_type = parse_claim_type(tree[claim_type_path_value], path=claim_type_path_value)
    contracts = _capture_contracts(tree)
    accounts: list[CaptureAdmissionAccountV1] = []
    for citation in claim_citation_references(claim):
        envelope = parse_capture_envelope(
            instance.body_store().read(
                citation.capture_digest,
                access=BodyAccessContext(principal_id="playbill-service", can_read_body=True),
            )
        )
        contract = contracts.get(envelope.capture_contract_digest)
        if contract is None:
            raise ProposalIntegrityError("accepted Claim CaptureContract no longer resolves")
        if isinstance(citation, ClaimCitationV1) and citation.role == "copy":
            accounts.append(
                CaptureAdmissionAccountV1(
                    citation_id=citation.citation_id,
                    capture_digest=citation.capture_digest,
                    citation_role=citation.role,
                    citation_origin=citation.origin,
                    capture_contract_identity=contract.contract.identity.qualified,
                    capture_contract_digest=contract.artifact_digest,
                    status="not_evidence",
                )
            )
            continue
        traces = evaluate_capture_evidence_admissions(
            claim,
            claim_type=claim_type,
            capture_digest=citation.capture_digest,
            capture_contract=contract,
            envelope=envelope,
            verified_attestations=law.verified_attestations,
        )
        decisions = tuple(
            CaptureEvidenceKindAdmissionV1(
                evidence_kind=item.evidence_kind,
                status="admitted" if item.trace.result.verdict == "eligible" else "not_admitted",
                rule_id=item.trace.result.rule_id,
                admission=item.trace.result.admission,
                refusal_code=item.trace.result.refusal_code,
                closest_rule_id=item.trace.closest_rule_id,
            )
            for item in traces
        )
        accounts.append(
            CaptureAdmissionAccountV1(
                citation_id=citation.citation_id,
                capture_digest=citation.capture_digest,
                citation_role=(
                    citation.role if isinstance(citation, ClaimCitationV1) else "legacy"
                ),
                citation_origin=(
                    citation.origin if isinstance(citation, ClaimCitationV1) else "legacy"
                ),
                capture_contract_identity=contract.contract.identity.qualified,
                capture_contract_digest=contract.artifact_digest,
                status=(
                    "admitted"
                    if any(item.status == "admitted" for item in decisions)
                    else "not_admitted"
                ),
                decisions=decisions,
            )
        )
    return tuple(accounts)


def resolve_playbill_claim_group(
    instance: PlaybillInstance,
    *,
    subject: SemanticAddress,
    predicate: str,
    coordinate: AcceptedProjectionCoordinate,
    evaluated_at: datetime,
    claims: tuple[ClaimArtifactAny, ...],
) -> PlaybillClaimGroupResolution:
    """Resolve one already-listed (Subject, predicate) slot without re-listing.

    Callers that fold every slot at a coordinate list the projection once and
    group in memory; the whole-projection listing used to be repeated inside
    every group, which made the fold quadratic in Claims.
    """

    from cruxible_core.service.playbill_evidence import (
        service_evaluate_playbill_claim_verdict,
    )

    type_path = claim_type_path(predicate)
    content = instance.blob_at(coordinate.git_oid, type_path)
    if content is None:
        raise ClaimNotFoundError(f"ClaimType:{predicate}")
    claim_type = parse_claim_type(content, path=type_path)
    contenders: list[ResolutionContenderV1] = []
    verdicts: list[ClaimVerdictResultAny] = []
    for claim in claims:
        evaluated = service_evaluate_playbill_claim_verdict(
            instance,
            claim_identity=claim.identity.qualified,
            evaluation_time=evaluated_at,
            at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        )
        verdicts.append(evaluated.verdict)
        value: object
        if claim.statement.object.kind == "literal":
            value = claim.statement.object.value
        else:
            value = claim.statement.object.model_dump(mode="json")
        contender_verdict: ClaimVerdict = (
            "stale" if evaluated.verdict.verdict == "stale_evidence" else evaluated.verdict.verdict
        )
        contenders.append(
            ResolutionContenderV1(
                claim_identity=claim.identity.name,
                object_value=value,
                verdict=contender_verdict,
                basis_kinds=evaluated.verdict.basis_kinds,
            )
        )
    resolution = resolve_claim_contenders(
        claim_type.resolution_policy,
        tuple(contenders),
    )
    return PlaybillClaimGroupResolution(
        subject=subject,
        predicate=predicate,
        cardinality=claim_type.cardinality,
        status=resolution.status,
        selected_claim_identities=tuple(
            f"Claim:{item}" for item in resolution.selected_claim_identities
        ),
        contender_claim_identities=tuple(
            f"Claim:{item}" for item in resolution.contender_claim_identities
        ),
        claims=claims,
        verdicts=tuple(verdicts),
    )


def service_query_playbill_claims(
    instance: PlaybillInstance,
    *,
    subject: SemanticAddress,
    predicate: str,
    at: PlaybillAcceptedCoordinate | None = None,
    evaluation_time: datetime | None = None,
) -> PlaybillClaimQueryResult | PlaybillClaimQueryResultV2:
    coordinate = _resolve_coordinate(instance, at)
    evaluated_at = evaluation_time or datetime.now(UTC)
    listed = service_list_playbill_claims(
        instance,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        subject=subject,
        predicate=predicate,
    )
    group = resolve_playbill_claim_group(
        instance,
        subject=subject,
        predicate=predicate,
        coordinate=coordinate,
        evaluated_at=evaluated_at,
        claims=tuple(_claim_from_view(view) for view in listed.claims),
    )
    if any(isinstance(item, ClaimVerdictResultV2) for item in group.verdicts):
        return PlaybillClaimQueryResultV2(
            coordinate=listed.coordinate,
            evaluation_time=evaluated_at,
            subject=subject,
            predicate=predicate,
            cardinality=group.cardinality,
            status=group.status,
            selected_claim_identities=group.selected_claim_identities,
            contender_claim_identities=group.contender_claim_identities,
            claims=listed.claims,
            verdicts=group.verdicts,
        )
    v1_verdicts = tuple(item for item in group.verdicts if isinstance(item, ClaimVerdictResultV1))
    return PlaybillClaimQueryResult(
        coordinate=listed.coordinate,
        evaluation_time=evaluated_at,
        subject=subject,
        predicate=predicate,
        cardinality=group.cardinality,
        status=group.status,
        selected_claim_identities=group.selected_claim_identities,
        contender_claim_identities=group.contender_claim_identities,
        claims=listed.claims,
        verdicts=v1_verdicts,
    )


def service_playbill_claim_history(
    instance: PlaybillInstance,
    *,
    identity: str,
) -> PlaybillClaimHistory:
    parsed_identity = ArtifactIdentity(
        kind="Claim",
        name=_resolved_claim_id(instance, identity, coordinate=_resolve_coordinate(instance, None)),
    )
    path = claim_path(parsed_identity.name)
    entries: list[PlaybillClaimHistoryEntry] = []
    for generation in instance.accepted_history()[1:]:
        record = generation.record
        if record is None or not any(member.path == path for member in record.members):
            continue
        content = instance.tree_at(generation.oid).get(path)
        if content is None:
            continue
        claim = parse_claim(content, path=path)
        entries.append(
            PlaybillClaimHistoryEntry(
                sequence=generation.sequence,
                coordinate=PlaybillAcceptedCoordinate.from_internal(
                    instance.coordinate_for_oid(generation.oid)
                ),
                statement_digest=claim_statement_digest(claim.statement).tagged,
                artifact_digest=claim_artifact_digest(claim).tagged,
                predecessor_digest=claim.lifecycle.predecessor_digest,
                lifecycle_state=claim.lifecycle.state,
                change_set_path=f"changesets/cs-{record.sequence:020d}.json",
                changeset_digest=record.changeset_digest,
                candidate_digest=record.candidate_digest,
            )
        )
    if not entries:
        raise ClaimNotFoundError(identity)
    return PlaybillClaimHistory(identity=parsed_identity.qualified, entries=tuple(entries))


def service_explain_playbill_claim(
    instance: PlaybillInstance,
    *,
    identity: str,
    at: PlaybillAcceptedCoordinate | None = None,
    evaluation_time: datetime | None = None,
) -> PlaybillClaimExplanationV2 | PlaybillClaimExplanationV3:
    from cruxible_core.service.playbill_evidence import (
        service_evaluate_playbill_claim_verdict,
    )

    coordinate = _resolve_coordinate(instance, at)
    evaluated_at = evaluation_time or datetime.now(UTC)
    read = service_get_playbill_claim(
        instance,
        identity=identity,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        evaluation_time=evaluated_at,
    )
    view = PlaybillClaimView(
        coordinate=read.coordinate,
        envelope=read.envelope,
        facts=read.facts,
    )
    claim = _claim_from_view(view)
    law = _claim_law_evidence(
        instance,
        path=claim_path(claim.identity.name),
        at=coordinate,
    )
    verdict = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=claim.identity.qualified,
        evaluation_time=evaluated_at,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
    )
    handles: list[SourceHandleV1] = []
    capture_context: dict[str, tuple[tuple[str, ...], str, str]] = {}
    citations_by_capture: dict[str, list[str]] = {}
    for citation in claim_citation_references(claim):
        citations_by_capture.setdefault(citation.capture_digest, []).append(citation.citation_id)
    from cruxible_core.service.playbill_evidence import _capture_contracts

    contracts = _capture_contracts(instance.tree_at(coordinate.git_oid))
    for digest in claim.backing.capture_digests:
        envelope = parse_capture_envelope(
            instance.body_store().read(
                digest,
                access=BodyAccessContext(principal_id="playbill-service", can_read_body=True),
            )
        )
        spans = tuple(
            span
            for mapping in claim.backing.source_mappings
            for span in mapping.spans
            if span.content_digest == envelope.commitment.digest
        )
        handles.append(
            SourceHandleV1(
                subject=claim_statement_address(claim_path(claim.identity.name)),
                at=PlaybillAcceptedCoordinate.from_internal(coordinate),
                source=envelope.source,
                commitment=envelope.commitment,
                media_type=(
                    "application/json" if envelope.commitment.digest_kind != "exact_bytes" else None
                ),
                exact_spans=spans,
                access_class="instance",
            )
        )
        contract = contracts.get(envelope.capture_contract_digest)
        if contract is None:
            raise ProposalIntegrityError("accepted Claim CaptureContract no longer resolves")
        logical_source = getattr(envelope.source, "source_identity", None)
        if logical_source is None:
            logical_source = contract.contract.logical_source_identities[0]
        capture_context[digest] = (
            tuple(
                sorted(
                    citations_by_capture.get(digest, ()),
                    key=lambda item: item.encode("ascii"),
                )
            ),
            contract.contract.identity.qualified,
            logical_source,
        )
    public_coordinate = PlaybillAcceptedCoordinate.from_internal(coordinate)
    coverage = CoverageDescriptorV1(
        requested_facets=("governance", "provenance", "sources"),
        available_facets=("governance", "provenance", "sources"),
    )
    if isinstance(verdict.verdict, ClaimVerdictResultV2):
        freshness: list[ClaimEvidenceFreshnessLineV1] = []
        for expiration in verdict.verdict.freshness_expirations:
            context = capture_context.get(expiration.capture_digest)
            if context is None:
                raise ProposalIntegrityError(
                    "playbill.claim.evidence_freshness_invalid: expiration has no Claim citation"
                )
            citation_ids, contract_identity, logical_source = context
            freshness.append(
                ClaimEvidenceFreshnessLineV1(
                    capture_digest=expiration.capture_digest,
                    citation_ids=citation_ids,
                    capture_contract_identity=contract_identity,
                    logical_source=logical_source,
                    observed_at=expiration.observed_at,
                    expires_at=expiration.expires_at,
                    state="expired" if evaluated_at >= expiration.expires_at else "current",
                    recapture_operation=EvidenceRecaptureOperationV1(
                        claim_identity=claim.identity.qualified,
                        capture_contract_identity=contract_identity,
                        logical_source=logical_source,
                    ),
                )
            )
        return PlaybillClaimExplanationV3(
            coordinate=public_coordinate,
            evaluation_time=evaluated_at,
            claim=view,
            law_evidence=law,
            verdict=verdict.verdict,
            exact_attestations=law.verified_attestations,
            source_handles=tuple(handles),
            coverage=coverage,
            admission_evaluation_time=evaluated_at,
            admission_accounts=read.admission_accounts,
            freshness=tuple(
                sorted(freshness, key=lambda item: item.capture_digest.encode("ascii"))
            ),
        )
    if not isinstance(law, ClaimLawEvidenceV1):
        raise ProposalIntegrityError(
            "playbill.claim.evidence_freshness_invalid: v2 law evidence produced a v1 verdict"
        )
    return PlaybillClaimExplanationV2(
        coordinate=public_coordinate,
        evaluation_time=evaluated_at,
        claim=view,
        law_evidence=law,
        verdict=verdict.verdict,
        exact_attestations=law.verified_attestations,
        source_handles=tuple(handles),
        coverage=coverage,
        admission_evaluation_time=evaluated_at,
        admission_accounts=read.admission_accounts,
    )


_EXPAND_FACETS = frozenset(
    {
        "attestation_coverage",
        "claim_context",
        "claim_type_card",
        "governance",
        "profile",
        "provenance",
        "relations",
        "sources",
        "summary",
    }
)


def _expand_subject_relations(
    tree: dict[str, bytes],
    address: SemanticAddress,
) -> tuple[dict[str, object], ...]:
    relations: list[dict[str, object]] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if claim.lifecycle.state != "live" or claim.statement.predicate not in {
            "semantic.alias",
            "semantic.distinct_from",
            "semantic.related_to",
            "semantic.tag",
        }:
            continue
        object_address = (
            claim.statement.object.address
            if isinstance(claim.statement.object, SubjectClaimObject)
            else None
        )
        if claim.statement.subject != address and object_address != address:
            continue
        relations.append(
            {
                "claim": claim_statement_address(path).model_dump(mode="json"),
                "predicate": claim.statement.predicate,
                "subject": claim.statement.subject.model_dump(mode="json"),
                "object": claim.statement.object.model_dump(mode="json"),
                "statement_digest": claim_statement_digest(claim.statement).tagged,
            }
        )
    return tuple(relations)


def _interface_vocabulary(
    relations: tuple[dict[str, object], ...],
    address: SemanticAddress,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[SemanticRelationV1, ...]]:
    """Split the accepted descriptor Claims of one address into card vocabulary.

    An alias or a tag belongs to the address the Claim states it about, while a
    typed relation is indexed from both ends and marked inbound on the end that
    did not author it.
    """

    owner = address.model_dump(mode="json")
    aliases: set[str] = set()
    tags: set[str] = set()
    edges: dict[bytes, SemanticRelationV1] = {}
    for row in relations:
        predicate = row["predicate"]
        subject_value = row["subject"]
        object_value = row["object"]
        if not isinstance(object_value, dict):
            continue
        if predicate in {"semantic.alias", "semantic.tag"} and subject_value == owner:
            value = object_value.get("value")
            if object_value.get("kind") == "literal" and isinstance(value, str):
                (aliases if predicate == "semantic.alias" else tags).add(value)
        elif predicate in {"semantic.distinct_from", "semantic.related_to"}:
            if object_value.get("kind") != "subject":
                continue
            inbound = subject_value != owner
            target = subject_value if inbound else object_value["address"]
            edge = SemanticRelationV1(
                predicate=predicate,  # type: ignore[arg-type]
                target=SemanticAddress.model_validate(target),
                inbound=inbound,
            )
            edges[canonical_bytes(edge.model_dump(mode="json"))] = edge
    return (
        byte_sorted(tuple(aliases)),
        byte_sorted(tuple(tags)),
        tuple(edges[key] for key in sorted(edges)),
    )


def _subject_identity(tree: dict[str, bytes], path: str) -> str | None:
    """Return one accepted Subject's identity, or None when it is absent.

    An accepted Claim pins the Subject it is about, so the absent case is
    defensive rather than reachable.
    """

    content = tree.get(path)
    return None if content is None else parse_subject(content, path=path).identity.qualified


def _claim_source_handles(
    instance: PlaybillInstance,
    *,
    claim: ClaimArtifactAny,
    coordinate: AcceptedProjectionCoordinate,
) -> tuple[SourceHandleV1, ...]:
    explanation = service_explain_playbill_claim(
        instance,
        identity=claim.identity.qualified,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
    )
    return explanation.source_handles


def service_expand_playbill_semantic(
    instance: PlaybillInstance,
    *,
    request: ExpandRequestV1,
) -> ContextCapsuleV1:
    """Return a bounded, coordinate-bound semantic context capsule without mutation."""

    if not isinstance(request.at, PlaybillAcceptedCoordinate):
        raise ProposalIntegrityError("PC-B expand accepts only verified accepted coordinates")
    coordinate = _resolve_coordinate(instance, request.at)
    try:
        _observed_at(request.evaluation_time)
    except ProposalIntegrityError as exc:
        raise ProposalIntegrityError("expand evaluation_time must be timezone-aware") from exc
    if request.facets != tuple(sorted(set(request.facets), key=lambda item: item.encode("utf-8"))):
        raise ProposalIntegrityError("expand facets must be sorted and unique")
    unknown = set(request.facets).difference(_EXPAND_FACETS)
    if unknown:
        raise ProposalIntegrityError(f"unknown expand facets: {sorted(unknown)!r}")

    tree = instance.tree_at(coordinate.git_oid)
    path = request.address.artifact_path
    content = tree.get(path)
    if content is None:
        raise ClaimNotFoundError(path)

    all_relations = _expand_subject_relations(tree, request.address)
    aliases, tags, relation_edges = _interface_vocabulary(all_relations, request.address)
    at = PlaybillAcceptedCoordinate.from_internal(coordinate)

    canonical_summary: object
    governance: object
    provenance: object
    claim_context: object | None = None
    claim_type_card: object | None = None
    subject_profile: object | None = None
    source_handles: tuple[SourceHandleV1, ...] = ()
    source_budget_truncated = False
    kind: str

    if path.startswith("claims/"):
        from cruxible_core.service.playbill_evidence import (
            service_evaluate_playbill_claim_verdict,
        )

        if request.address.selector.scheme not in {"artifact-v1", "claim-statement-v1"}:
            raise ProposalIntegrityError("Claim expansion requires artifact or statement identity")
        claim = parse_claim(content, path=path)
        law = _claim_law_evidence(instance, path=path, at=coordinate)
        kind = "claim"
        canonical_summary = {
            "artifact_digest": claim_artifact_digest(claim).tagged,
            "identity": claim.identity.qualified,
            "statement": claim.statement.model_dump(mode="json"),
            "statement_digest": claim_statement_digest(claim.statement).tagged,
        }
        governance = {
            "approval_coverage": "containing_change_set",
            "lifecycle": claim.lifecycle.model_dump(mode="json"),
        }
        provenance = {
            "backing": claim.backing.model_dump(mode="json"),
            "pins": [item.model_dump(mode="json") for item in claim.pins],
        }
        all_source_handles = _claim_source_handles(
            instance,
            claim=claim,
            coordinate=coordinate,
        )
        source_handles = all_source_handles[: request.budget.max_source_handles]
        source_budget_truncated = len(all_source_handles) > len(source_handles)
        competitors = service_list_playbill_claims(
            instance,
            at=PlaybillAcceptedCoordinate.from_internal(coordinate),
            subject=claim.statement.subject,
            predicate=claim.statement.predicate,
        ).claims
        claim_context = {
            "competing_claim_identities": [
                item.envelope["identity"]
                for item in competitors
                if item.envelope["identity"] != claim.identity.qualified
            ],
            "law_evidence": law.model_dump(mode="json"),
            "verdict": service_evaluate_playbill_claim_verdict(
                instance,
                claim_identity=claim.identity.qualified,
                evaluation_time=_observed_at(request.evaluation_time),
                at=PlaybillAcceptedCoordinate.from_internal(coordinate),
            ).verdict.model_dump(mode="json"),
            "source_handles": [item.model_dump(mode="json") for item in source_handles],
        }
    elif path.startswith("subjects/"):
        if request.address.selector.scheme != "artifact-v1":
            raise ProposalIntegrityError("Subject expansion requires whole-artifact identity")
        subject = parse_subject(content, path=path)
        kind = "subject"
        canonical_summary = {
            "artifact_digest": subject_digest(subject).tagged,
            "identity": subject.identity.qualified,
            "subject_id": subject.subject_id,
            "subject_kind": subject.subject_kind,
        }
        governance = {
            "lifecycle": subject.lifecycle.model_dump(mode="json"),
        }
        provenance = {"pins": [item.model_dump(mode="json") for item in subject.pins]}
        claims = service_list_playbill_claims(
            instance,
            at=at,
            subject=request.address,
        ).claims
        artifacts = tuple(_claim_from_view(view) for view in claims)
        cardinalities: dict[str, str] = {}
        for artifact in artifacts:
            contract_path = claim_type_path(artifact.statement.predicate)
            contract_content = tree.get(contract_path)
            if contract_content is not None:
                cardinalities[artifact.statement.predicate] = parse_claim_type(
                    contract_content,
                    path=contract_path,
                ).cardinality
        # The profile is taken without an evaluation time: it is coordinate-pure
        # accepted structure, and the verdict-bearing read stays claim_context.
        subject_profile = build_subject_profile(
            at=at,
            entry=DiscoveryEntryV1(
                kind="Subject",
                address=request.address,
                identity=subject.identity.qualified,
                label=subject.identity.qualified,
                aliases=aliases,
                tags=tags,
                lexical_terms=byte_sorted((subject.subject_id, subject.subject_kind)),
                structural_signature_digest=subject_reuse_signature(subject.identity),
            ),
            subject_kind=subject.subject_kind,
            subject_id=subject.subject_id,
            artifact_digest=subject_digest(subject).tagged,
            claims=artifacts,
            cardinalities=cardinalities,
            relations=relation_edges,
        ).model_dump(mode="json")
    elif path.startswith("claim-types/"):
        if request.address.selector.scheme != "artifact-v1":
            raise ProposalIntegrityError("ClaimType expansion requires whole-artifact identity")
        claim_type = parse_claim_type(content, path=path)
        kind = "claim_type"
        canonical_summary = {
            "artifact_digest": claim_type_digest(claim_type).tagged,
            "identity": claim_type.identity.qualified,
            "predicate": claim_type.predicate,
            "structure": claim_type.structure.model_dump(mode="json"),
        }
        governance = {
            "lifecycle": claim_type.lifecycle.model_dump(mode="json"),
        }
        provenance = {"pins": [item.model_dump(mode="json") for item in claim_type.pins]}
        usage_rows: list[ClaimTypeUsageRowV1] = []
        for view in service_list_playbill_claims(
            instance,
            at=at,
            predicate=claim_type.predicate,
        ).claims:
            statement_subject = _claim_from_view(view).statement.subject.artifact_path
            subject_identity = _subject_identity(tree, statement_subject)
            if subject_identity is None:
                continue
            usage_rows.append(
                ClaimTypeUsageRowV1(
                    subject_path=statement_subject,
                    subject_identity=subject_identity,
                )
            )
        claim_type_card = build_claim_type_card(
            claim_type,
            at=at,
            entry=DiscoveryEntryV1(
                kind="ClaimType",
                address=request.address,
                identity=claim_type.identity.qualified,
                label=claim_type.predicate,
                aliases=aliases,
                tags=tags,
                lexical_terms=byte_sorted(
                    (
                        claim_type.predicate,
                        claim_type.predicate.rpartition(".")[2],
                        *claim_type.allowed_subject_kinds,
                    )
                ),
                structural_signature_digest=claim_type_structural_signature(claim_type.structure),
            ),
            usage_rows=tuple(usage_rows),
            relations=relation_edges,
        ).model_dump(mode="json")
    elif path.startswith("query-definitions/"):
        # A named entrypoint advertises its contract before any row is read: the
        # compact interface is the canonical summary, not a separate capsule slot.
        if request.address.selector.scheme != "artifact-v1":
            raise ProposalIntegrityError(
                "QueryDefinition expansion requires whole-artifact identity"
            )
        definition = parse_query_definition(content, path=path)
        kind = "query_definition"
        canonical_summary = {
            "artifact_digest": query_definition_digest(definition).tagged,
            "default_budgets": definition.default_budgets.model_dump(mode="json"),
            "description": definition.description,
            "entrypoint_name": definition.identity.name,
            "evaluation_policy": definition.evaluation_policy.model_dump(mode="json"),
            "identity": definition.identity.qualified,
            "parameters": [item.model_dump(mode="json") for item in definition.parameters],
            "referenced_predicates": list(definition.referenced_predicates),
            "result_cardinality": definition.result_cardinality,
            "result_shape": definition.result_shape,
            "subject_kinds": list(definition.subject_kinds),
        }
        governance = {
            "lifecycle": definition.lifecycle.model_dump(mode="json"),
        }
        provenance = {"pins": [item.model_dump(mode="json") for item in definition.pins]}
    else:
        # Procedure and LineSpec expansion needs their full pin closure rendered;
        # discovery already returns their handles, and PC-G lands the expansion.
        raise ProposalIntegrityError(
            "expand supports Claim, Subject, ClaimType, and QueryDefinition"
        )

    relations = all_relations[: request.budget.max_relations]
    relation_budget_truncated = len(all_relations) > len(relations)
    requested = set(request.facets)
    available = {
        "attestation_coverage",
        "governance",
        "provenance",
        "summary",
    }
    if claim_context is not None:
        available.add("claim_context")
    if claim_type_card is not None:
        available.add("claim_type_card")
    if subject_profile is not None:
        available.add("profile")
    if relations:
        available.add("relations")
    if source_handles:
        available.add("sources")

    # Facets are additive presentation. Keep the required summary/governance/provenance
    # slots structurally stable while replacing unrequested content with a typed omission.
    if request.facets:
        canonical_summary = canonical_summary if "summary" in requested else None
        governance = governance if "governance" in requested else None
        provenance = provenance if "provenance" in requested else None
        claim_context = claim_context if "claim_context" in requested else None
        claim_type_card = claim_type_card if "claim_type_card" in requested else None
        subject_profile = subject_profile if "profile" in requested else None
        relations = relations if "relations" in requested else ()

    truncated: set[str] = set()
    if source_budget_truncated and (not request.facets or "sources" in requested):
        truncated.add("sources")
    if relation_budget_truncated and (not request.facets or "relations" in requested):
        truncated.add("relations")

    def material_size() -> int:
        return len(
            canonical_bytes(
                {
                    "canonical_summary": canonical_summary,
                    "claim_context": claim_context,
                    "claim_type_card": claim_type_card,
                    "governance": governance,
                    "provenance": provenance,
                    "relations": list(relations),
                    "subject_profile": subject_profile,
                }
            )
        )

    if material_size() > request.budget.max_bytes and relations:
        relations = ()
        truncated.add("relations")
    if material_size() > request.budget.max_bytes and isinstance(claim_context, dict):
        claim_context = {
            "law_evidence": claim_context["law_evidence"],
            "source_handle_digests": [source_handle_digest(item) for item in source_handles],
        }
        truncated.add("sources")
    if material_size() > request.budget.max_bytes:
        claim_context = None
        claim_type_card = None
        subject_profile = None
        truncated.update({"claim_context", "claim_type_card", "profile"}.intersection(requested))
    if material_size() > request.budget.max_bytes:
        provenance = None
        governance = None
        truncated.update({"governance", "provenance"}.intersection(requested))
    if material_size() > request.budget.max_bytes:
        canonical_summary = None
        truncated.add("summary")
    if material_size() > request.budget.max_bytes:
        raise ProposalIntegrityError("expand byte budget is smaller than the minimum capsule")

    available_facets = tuple(
        sorted((available.intersection(requested or available)).difference(truncated))
    )
    reason_codes: tuple[str, ...] = (
        ("open_source_required",) if "sources" in requested and source_handles else ()
    )
    coverage = CoverageDescriptorV1(
        requested_facets=request.facets,
        available_facets=available_facets,
        truncated_facets=tuple(sorted(truncated, key=lambda item: item.encode("utf-8"))),
        reason_codes=reason_codes,
    )
    receipt_digest = typed_digest(
        Sha256Value,
        "playbill-context-capsule-receipt-v1",
        {
            "address": request.address.model_dump(mode="json"),
            "at": request.at.model_dump(mode="json"),
            "budget": request.budget.model_dump(mode="json"),
            "coverage": coverage.model_dump(mode="json"),
            "evaluation_time": request.evaluation_time,
            "kind": kind,
        },
    ).tagged
    next_reads = (
        (GovernedOperationReference(operation="open_source", subject=request.address),)
        if source_handles
        else ()
    )
    return ContextCapsuleV1(
        address=request.address,
        at=request.at,
        evaluation_time=request.evaluation_time,
        canonical_summary=canonical_summary,
        governance=governance,
        provenance=provenance,
        attestation_coverage="containing_change_set",
        claim_context=claim_context,
        claim_type_card=claim_type_card,
        subject_profile=subject_profile,
        relations=relations,
        next_reads=next_reads,
        coverage=coverage,
        receipt_digest=receipt_digest,
    )


class _InstanceSourceMaterialResolver:
    """Bind the shared dereference engine to one accepted coordinate's retained bytes."""

    def __init__(
        self,
        instance: PlaybillInstance,
        *,
        coordinate: AcceptedProjectionCoordinate,
        external_reader: ExternalSelectionReaderProtocol | None,
    ) -> None:
        self._instance = instance
        self._coordinate = coordinate
        self._external_reader = external_reader

    def read_ledger(self, artifact_path: str) -> bytes | None:
        return self._instance.tree_at(self._coordinate.git_oid).get(artifact_path)

    def read_cas(self, content_digest: str, *, access: BodyAccessContext) -> bytes | None:
        if not self._instance.body_store().verify(content_digest):
            return None
        return self._instance.body_store().read(content_digest, access=access)

    def read_external(self, source: ExternalSourceReferenceV1) -> object | None:
        if self._external_reader is None:
            return None
        return self._external_reader.read_external_selection(source)


def service_open_playbill_source(
    instance: PlaybillInstance,
    *,
    request: OpenSourceRequestV1,
    access: BodyAccessContext,
    at: PlaybillAcceptedCoordinate | None = None,
    external_reader: ExternalSelectionReaderProtocol | None = None,
) -> SourceDereferenceResultV1:
    """Dereference only the coordinate-bound handle; never mutate or refresh a source.

    Ledger, CAS, and external selections all resolve through one engine, so the
    coverage, budget, and access laws cannot drift apart by source kind.
    """

    coordinate = _resolve_coordinate(instance, at)
    return dereference_source_handle(
        request,
        access=access,
        resolver=_InstanceSourceMaterialResolver(
            instance,
            coordinate=coordinate,
            external_reader=external_reader,
        ),
    )


__all__ = [
    "CaptureAdmissionAccountV1",
    "CaptureEvidenceKindAdmissionV1",
    "ClaimEvidenceFreshnessLineV1",
    "PlaybillClaimExplanationV1",
    "PlaybillClaimExplanationV2",
    "PlaybillClaimExplanationV3",
    "PlaybillClaimGroupResolution",
    "PlaybillClaimHistory",
    "PlaybillClaimHistoryEntry",
    "PlaybillClaimList",
    "PlaybillClaimQueryResult",
    "PlaybillClaimQueryResultV2",
    "PlaybillClaimView",
    "PlaybillClaimViewV2",
    "projected_playbill_claim_views",
    "resolve_playbill_claim_group",
    "service_expand_playbill_semantic",
    "service_explain_playbill_claim",
    "service_get_playbill_claim",
    "service_list_playbill_claims",
    "service_open_playbill_source",
    "service_playbill_claim_history",
    "service_query_playbill_claims",
]
