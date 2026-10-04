"""Canonical service contract for first-class Playbill Claims."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, MutableSet
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts import ClaimViewRecord as ClientClaimViewV2
from cruxible_client.contracts.accepted_attestations import ClaimAttestationEvidence
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.candidates import CandidateMemberEvidence
from cruxible_client.contracts.canonical import (
    file_digest,
)
from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_types import (
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    ClaimVerdictResult,
    ClaimVerdictResultAny,
    ClaimVerdictResultV1,
)
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimArtifactAny,
    ClaimArtifactV2,
    ClaimCitation,
    ClaimFormatError,
    ClaimLawEvidenceAny,
    ClaimLawEvidenceV1,
    ClaimStatementCard,
    ClaimUnsupportedFormatError,
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
from cruxible_client.contracts.errors import (
    ClaimNotFoundError,
    ProjectionIntegrityError,
    ProposalIntegrityError,
)
from cruxible_client.contracts.policies import (
    ClaimVerdict,
    ResolutionContender,
    resolve_claim_contenders,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    CoverageDescriptor,
    ExternalSourceReference,
    OpenSourceRequest,
    SourceDereferenceResult,
    SourceHandle,
)
from cruxible_client.contracts.subjects import (
    parse_subject,
)
from cruxible_core.authoring.id_prefixes import resolve_id_prefix
from cruxible_core.indexes.claims.projection_claims import ClaimProjectionView
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.proposals.settlement import ChangeSetRecord, ChangeSetRecordAnyVersion
from cruxible_core.query.dereference import (
    ExternalSelectionReaderProtocol,
    dereference_source_handle,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.claims.retirement_context import (
    ClaimRetirementContextV1,
    claim_retirement_context,
)
from cruxible_core.storage.cas import BodyAccessContext

if TYPE_CHECKING:
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
    from cruxible_core.storage.cas import ContentAddressedBodyStore


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
    citation_origin: Literal["independent", "self_source", "legacy"]
    capture_contract_identity: str
    capture_contract_digest: str
    status: Literal["admitted", "not_admitted", "not_evidence"]
    decisions: tuple[CaptureEvidenceKindAdmissionV1, ...] = ()


class ClaimViewRecord(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-read-v2"] = "playbill-claim-read-v2"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, object]
    facts: tuple[dict[str, object], ...]
    admission_evaluation_time: datetime
    admission_accounts: tuple[CaptureAdmissionAccountV1, ...]
    statement: ClaimStatementCard

    @field_validator("admission_evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("admission evaluation time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _ordered_accounts(self) -> "ClaimViewRecord":
        ids = tuple(item.citation_id for item in self.admission_accounts)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("ascii"))):
            raise ValueError("admission accounts must be sorted and unique")
        return self


class PlaybillClaimList(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-list-v1"] = "playbill-claim-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    claims: tuple[PlaybillClaimView, ...]


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


class PlaybillClaimExplanationV2(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-explanation-v2"] = "playbill-claim-explanation-v2"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    claim: PlaybillClaimView
    law_evidence: ClaimLawEvidenceV1
    verdict: ClaimVerdictResultV1
    exact_attestations: tuple[ClaimAttestationEvidence, ...]
    approval_coverage: Literal["containing_change_set"] = "containing_change_set"
    source_handles: tuple[SourceHandle, ...]
    coverage: CoverageDescriptor
    admission_evaluation_time: datetime
    admission_accounts: tuple[CaptureAdmissionAccountV1, ...]
    # Review context, not work: what this Claim shares with retired Claims.
    # Absent when it shares nothing, so the explanation's bytes do not change.
    retirement_context: ClaimRetirementContextV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


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
    verdict: ClaimVerdictResult
    exact_attestations: tuple[ClaimAttestationEvidence, ...]
    approval_coverage: Literal["containing_change_set"] = "containing_change_set"
    source_handles: tuple[SourceHandle, ...]
    coverage: CoverageDescriptor
    admission_evaluation_time: datetime
    admission_accounts: tuple[CaptureAdmissionAccountV1, ...]
    freshness: tuple[ClaimEvidenceFreshnessLineV1, ...]
    retirement_context: ClaimRetirementContextV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


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


def _claim_from_view(
    view: PlaybillClaimView | ClaimViewRecord | ClientClaimViewV2,
) -> ClaimArtifactAny:
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
        model: type[ClaimArtifactV2] | type[ClaimArtifact] = ClaimArtifactV2
    elif artifact_format == "playbill-claim-v3":
        model = ClaimArtifact
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
    try:
        claim_path(bare)
    except ClaimFormatError:
        pass
    else:
        return bare
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
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        if history.sequence == 0:
            return ()
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = projection.typed.envelopes(kind="claim")
        return tuple(row.identity.removeprefix("Claim:") for row in rows)


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
) -> ClaimViewRecord:
    expected = "Claim:CLM-<32 lowercase hex> or CLM-<32 lowercase hex>"
    coordinate = _resolve_coordinate(instance, at)
    bare = _resolved_claim_id(instance, identity, coordinate=coordinate)
    try:
        path = claim_path(bare)
    except ClaimFormatError as exc:
        raise ClaimNotFoundError(
            f"Claim not found; expected {expected}; received {identity!r}"
        ) from exc
    qualified = f"Claim:{bare}"
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        if history.sequence == 0:
            raise ClaimNotFoundError(f"Claim not found; expected {expected}; received {identity!r}")
    with instance.bind_accepted_projection(coordinate) as projection:
        claim = projection.claim(qualified)
        from cruxible_core.service.evidence.evidence import _claim_read_history_index

        claim_history = _claim_read_history_index(
            instance, coordinate=coordinate, records=projection.typed.records
        )
    if claim is None:
        raise ClaimNotFoundError(f"Claim not found; expected {expected}; received {identity!r}")
    public = _public_claim(claim)
    if public.envelope.get("path") != path:
        raise ProposalIntegrityError("Claim projection path differs from normalized identity")
    return materialize_playbill_claim_view(
        instance,
        public=public,
        coordinate=coordinate,
        evaluation_time=evaluation_time or _accepted_generation_time(instance, coordinate),
        law=claim_history.law_evidence.get(path),
    )


def materialize_playbill_claim_view(
    instance: PlaybillInstance,
    *,
    public: PlaybillClaimView,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    law: ClaimLawEvidenceAny | None,
    admission_tree: dict[str, bytes] | None = None,
    bodies: ContentAddressedBodyStore | None = None,
) -> ClaimViewRecord:
    """Shared single/batch admission semantics; binding and selection happen upstream."""
    if law is None:
        raise ProposalIntegrityError("accepted Claim has no reproducible Claim law evidence")
    parsed = _claim_from_view(public)
    return ClaimViewRecord(
        coordinate=public.coordinate,
        envelope=public.envelope,
        facts=public.facts,
        admission_evaluation_time=evaluation_time,
        admission_accounts=_claim_admission_accounts(
            instance,
            claim=parsed,
            tree=(
                admission_tree
                if admission_tree is not None
                else _claim_admission_tree(instance, claim=parsed, coordinate=coordinate)
            ),
            law=law,
            bodies=bodies,
            coordinate=coordinate,
        ),
        statement=claim_statement_card(parsed),
    )


def service_list_playbill_claims(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate | None = None,
    subject: SemanticAddress | None = None,
    predicate: str | None = None,
    include_retired: bool = False,
    subject_kind: str | None = None,
) -> PlaybillClaimList:
    coordinate = _resolve_coordinate(instance, at)
    with instance.bind_accepted_projection(coordinate) as projection:
        claims = tuple(
            _public_claim(item)
            for item in projection.list_claims(
                subject=subject,
                predicate=predicate,
                include_retired=include_retired,
                subject_kind=subject_kind,
            )
        )
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
    from cruxible_core.service.evidence.evidence import _claim_read_history_index

    # A one-Claim read needs one account, not a copy of the whole index.
    found = _claim_read_history_index(instance, coordinate=at).law_evidence.get(path)
    if found is None:
        raise ProposalIntegrityError("accepted Claim has no reproducible Claim law evidence")
    return found


def _claim_law_evidence_by_artifact_index(
    instance: PlaybillInstance,
    *,
    at: AcceptedProjectionCoordinate,
) -> dict[tuple[str, str], ClaimLawEvidenceAny]:
    """Index every accepted Claim law account in one bounded history pass."""

    found: dict[tuple[str, str], ClaimLawEvidenceAny] = {}
    with instance.accepted_history_reader(at=AcceptedCoordinate.from_internal(at)) as history:
        records: dict[int, ChangeSetRecordAnyVersion] = {}
        for location in history.claim_law_locations():
            record = records.get(location.sequence)
            if record is None:
                record = history.read_member_record(location, instance.blob_at)
                records[location.sequence] = record
            if isinstance(record, ChangeSetRecord):
                continue
            evidence = next(
                item for item in record.law_evidence if item.path == location.member_path
            )
            raw = evidence.result.get("claim_evidence")
            if raw is not None:
                parsed = parse_claim_law_evidence(raw)
                found.setdefault((evidence.path, parsed.artifact_digest), parsed)
    return found


def _accepted_generation_time(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
) -> datetime:
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        record = history.read_generation_record(history.sequence, instance.blob_at)
    return datetime.fromisoformat(record.candidate.timestamp.replace("Z", "+00:00"))


def _claim_admission_tree(
    instance: PlaybillInstance,
    *,
    claim: ClaimArtifactAny,
    coordinate: AcceptedProjectionCoordinate,
) -> dict[str, bytes]:
    """Read the ClaimType and CaptureContracts cited by this exact accepted Claim."""
    wanted = [claim_type_path(claim.statement.predicate)]
    with instance.bind_accepted_projection(coordinate) as projection:
        wanted.extend(
            row[0]
            for row in projection.typed.connection.execute(
                "SELECT DISTINCT t.path FROM citation_uses u "
                "JOIN captures c ON c.capture_digest=u.capture_digest "
                "JOIN capture_contracts t ON t.artifact_digest=c.contract_digest "
                "WHERE u.owner_kind='Claim' AND u.owner_key=? ORDER BY t.path",
                (claim.identity.qualified,),
            )
        )
        return {path: projection.typed.member_bytes(path) for path in wanted}


def _superseded_contract(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate | None,
    digest: str,
) -> AcceptedCaptureContract | None:
    """A CaptureContract version a successor replaced, read from accepted history.

    A Claim keeps the contract version its evidence was captured under, while
    the tree holds only the current version of each contract identity.
    """

    if coordinate is None:
        return None
    return instance.accepted_capture_contract_version(
        AcceptedCoordinate.from_internal(coordinate), digest
    )


def _claim_admission_accounts(
    instance: PlaybillInstance,
    *,
    claim: ClaimArtifactAny,
    tree: Mapping[str, bytes],
    law: ClaimLawEvidenceAny,
    bodies: ContentAddressedBodyStore | None = None,
    coordinate: AcceptedProjectionCoordinate | None = None,
) -> tuple[CaptureAdmissionAccountV1, ...]:
    from cruxible_core.evidence.attestation_verification import _capture_contracts

    claim_type_path_value = claim_type_path(claim.statement.predicate)
    claim_type = parse_claim_type(tree[claim_type_path_value], path=claim_type_path_value)
    contracts = _capture_contracts(tree)
    accounts: list[CaptureAdmissionAccountV1] = []
    # A batch resolves the confined body store once and passes it down.
    bodies = instance.body_store() if bodies is None else bodies
    for citation in claim_citation_references(claim):
        envelope = parse_capture_envelope(
            bodies.read(
                citation.capture_digest,
                access=BodyAccessContext(principal_id="playbill-service", can_read_body=True),
            )
        )
        contract = contracts.get(envelope.capture_contract_digest) or _superseded_contract(
            instance, coordinate, envelope.capture_contract_digest
        )
        if contract is None:
            raise ProposalIntegrityError("accepted Claim CaptureContract no longer resolves")
        if isinstance(citation, ClaimCitation) and citation.role == "copy":
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
                citation_role=(citation.role if isinstance(citation, ClaimCitation) else "legacy"),
                citation_origin=(
                    citation.origin if isinstance(citation, ClaimCitation) else "legacy"
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
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
    time_boundaries: MutableSet[datetime] | None = None,
    read_context: ClaimVerdictReadContext | None = None,
) -> PlaybillClaimGroupResolution:
    """Resolve one already-listed (Subject, predicate) slot without re-listing.

    Callers that fold every slot at a coordinate list the projection once and
    group in memory; the whole-projection listing used to be repeated inside
    every group, which made the fold quadratic in Claims. A caller that
    evaluates the same Claims elsewhere in the same request may pass a shared
    verdict map, valid only for this exact coordinate and evaluation time.
    ``time_boundaries`` collects the instants each verdict could change at, for
    a caller that memoizes the whole fold over an interval rather than an
    instant.
    """

    from cruxible_core.service.evidence.evidence import (
        service_evaluate_playbill_claim_verdict,
    )

    if read_context is not None and (
        read_context.instance is not instance or read_context.coordinate != coordinate
    ):
        raise ProposalIntegrityError("Claim group read context differs from accepted state")
    type_path = claim_type_path(predicate)
    if read_context is None:
        content = instance.blob_at(coordinate.git_oid, type_path)
        if content is None:
            raise ClaimNotFoundError(f"ClaimType:{predicate}")
        claim_type = parse_claim_type(content, path=type_path)
    else:
        try:
            claim_type = read_context.claim_type(type_path)
        except ClaimNotFoundError as exc:
            raise ClaimNotFoundError(f"ClaimType:{predicate}") from exc
    contenders: list[ResolutionContender] = []
    verdicts: list[ClaimVerdictResultAny] = []
    public_coordinate = PlaybillAcceptedCoordinate.from_internal(coordinate)
    for claim in claims:
        shared = (
            None
            if verdicts_by_identity is None
            else verdicts_by_identity.get(claim.identity.qualified)
        )
        if shared is None:
            shared = service_evaluate_playbill_claim_verdict(
                instance,
                claim_identity=claim.identity.qualified,
                evaluation_time=evaluated_at,
                at=public_coordinate,
                time_boundaries=time_boundaries,
                read_context=read_context,
            ).verdict
            if verdicts_by_identity is not None:
                verdicts_by_identity[claim.identity.qualified] = shared
        evaluated_verdict = shared
        verdicts.append(evaluated_verdict)
        value: object
        if claim.statement.object.kind == "literal":
            value = claim.statement.object.value
        else:
            value = claim.statement.object.model_dump(mode="json")
        contender_verdict: ClaimVerdict = (
            "stale" if evaluated_verdict.verdict == "stale_evidence" else evaluated_verdict.verdict
        )
        contenders.append(
            ResolutionContender(
                claim_identity=claim.identity.name,
                object_value=value,
                verdict=contender_verdict,
                basis_kinds=evaluated_verdict.basis_kinds,
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
    with instance.accepted_history_reader() as history:
        for location in history.member_history(path):
            generation = history.generation(location.sequence)
            record = history.read_member_record(location, load_record=instance._ledger.blob_at)
            member = record.members[location.member_ordinal]
            content = instance.blob_at(generation.git_oid, path)
            if member.disposition == "delete" and content is None:
                continue
            if content is None:
                raise ProjectionIntegrityError("Claim history source is unavailable")
            claim = parse_claim(content, path=path)
            digest = claim_artifact_digest(claim).tagged
            source_digest = (
                file_digest(content).tagged
                if isinstance(member, CandidateMemberEvidence)
                else digest
            )
            if source_digest != location.artifact_digest:
                raise ProjectionIntegrityError("Claim history source binding differs")
            entries.append(
                PlaybillClaimHistoryEntry(
                    sequence=generation.sequence,
                    coordinate=PlaybillAcceptedCoordinate(
                        git_oid=generation.git_oid,
                        semantic_root=generation.semantic_root,
                        generation_root=generation.generation_root,
                        compiler_digest=generation.compiler_digest,
                    ),
                    statement_digest=claim_statement_digest(claim.statement).tagged,
                    artifact_digest=digest,
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
    from cruxible_core.service.evidence.evidence import (
        ClaimVerdictReadContext,
        accepted_claim_attestations,
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
    handles: list[SourceHandle] = []
    capture_context: dict[str, tuple[tuple[str, ...], str, str]] = {}
    citations_by_capture: dict[str, list[str]] = {}
    for citation in claim_citation_references(claim):
        citations_by_capture.setdefault(citation.capture_digest, []).append(citation.citation_id)
    from cruxible_core.evidence.attestation_verification import _capture_contracts

    contracts = _capture_contracts(
        _claim_admission_tree(instance, claim=claim, coordinate=coordinate)
    )
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
            SourceHandle(
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
        contract = contracts.get(envelope.capture_contract_digest) or _superseded_contract(
            instance, coordinate, envelope.capture_contract_digest
        )
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
    retirement_context = claim_retirement_context(
        instance,
        coordinate=coordinate,
        claim_identity=claim.identity.qualified,
    )
    coverage = CoverageDescriptor(
        requested_facets=("governance", "provenance", "sources"),
        available_facets=("governance", "provenance", "sources"),
    )
    if isinstance(verdict.verdict, ClaimVerdictResult):
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
            exact_attestations=accepted_claim_attestations(
                instance,
                coordinate=coordinate,
                tree=ClaimVerdictReadContext(instance, coordinate).tree,
                claim=claim,
                historical=law.verified_attestations,
            ),
            source_handles=tuple(handles),
            coverage=coverage,
            admission_evaluation_time=evaluated_at,
            admission_accounts=read.admission_accounts,
            freshness=tuple(
                sorted(freshness, key=lambda item: item.capture_digest.encode("ascii"))
            ),
            retirement_context=retirement_context,
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
        exact_attestations=accepted_claim_attestations(
            instance,
            coordinate=coordinate,
            tree=ClaimVerdictReadContext(instance, coordinate).tree,
            claim=claim,
            historical=law.verified_attestations,
        ),
        source_handles=tuple(handles),
        coverage=coverage,
        admission_evaluation_time=evaluated_at,
        admission_accounts=read.admission_accounts,
        retirement_context=retirement_context,
    )


def _subject_identity(tree: Mapping[str, bytes], path: str) -> str | None:
    """Return one accepted Subject's identity, or None when it is absent.

    An accepted Claim pins the Subject it is about, so the absent case is
    defensive rather than reachable.
    """

    content = tree.get(path)
    return None if content is None else parse_subject(content, path=path).identity.qualified


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
        return self._instance.blob_at(self._coordinate.git_oid, artifact_path)

    def read_cas(self, content_digest: str, *, access: BodyAccessContext) -> bytes | None:
        if not self._instance.body_store().verify(content_digest):
            return None
        return self._instance.body_store().read(content_digest, access=access)

    def read_external(self, source: ExternalSourceReference) -> object | None:
        if self._external_reader is None:
            return None
        return self._external_reader.read_external_selection(source)


def service_open_playbill_source(
    instance: PlaybillInstance,
    *,
    request: OpenSourceRequest,
    access: BodyAccessContext,
    at: PlaybillAcceptedCoordinate | None = None,
    external_reader: ExternalSelectionReaderProtocol | None = None,
) -> SourceDereferenceResult:
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
    "PlaybillClaimExplanationV2",
    "PlaybillClaimExplanationV3",
    "PlaybillClaimGroupResolution",
    "PlaybillClaimHistory",
    "PlaybillClaimHistoryEntry",
    "PlaybillClaimList",
    "PlaybillClaimView",
    "ClaimViewRecord",
    "resolve_playbill_claim_group",
    "service_explain_playbill_claim",
    "service_get_playbill_claim",
    "service_list_playbill_claims",
    "service_open_playbill_source",
    "service_playbill_claim_history",
]
