"""Client-held ClaimAttestation submission and explicit-time verdict reads."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    capture_contract_digest,
    parse_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyProjectionProtocol
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationStatement,
    VerifiedClaimAttestationV1,
    store_claim_attestation,
    verify_claim_attestation,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    ClaimAdjudicationRuleV1,
    ClaimVerdictResultV1,
    ClaimVerdictResultV2,
    claim_adjudication_rule,
    claim_adjudication_rule_digest,
    evaluate_claim_verdict,
    verify_claim_verdict_freshness,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifactAny,
    ClaimArtifactV3,
    ClaimBacking,
    ClaimBackingV2,
    ClaimLawEvidenceAny,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
    parse_claim_law_evidence,
    render_claim,
)
from cruxible_client.contracts.errors import ClaimNotFoundError, ProposalIntegrityError
from cruxible_client.contracts.principals import principal_registry_from_tree
from cruxible_client.contracts.providers import ProviderV1, parse_provider, provider_digest
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    LedgerSourceReferenceV1,
)
from cruxible_client.contracts.standing_mandates import (
    StandingMandateQueryResultV1,
    parse_standing_mandate,
    standing_mandate_digest,
    standing_mandate_path,
)
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)
from cruxible_core.playbill.service.proposal_names import canonical_playbill_proposal_name
from cruxible_core.playbill.settlement import ChangeSetRecord, ChangeSetRecordAnyVersion
from cruxible_core.playbill.source_readers import ExternalSourceReaderProtocol


class _StrictEvidenceServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AcceptedHistoryGenerationProtocol(Protocol):
    """The accepted-history fields required by deterministic Claim fact replay."""

    @property
    def oid(self) -> str: ...

    @property
    def record(self) -> ChangeSetRecordAnyVersion | None: ...


class ClaimReadSourceProtocol(Protocol):
    """Accepted-state read seam shared by live instances and recovery replay."""

    def accepted_history(self) -> tuple[AcceptedHistoryGenerationProtocol, ...]: ...

    def tree_at(self, oid: str) -> dict[str, bytes]: ...

    def body_store(self) -> BodyProjectionProtocol: ...


class PreparedClaimAttestationV1(_StrictEvidenceServiceModel):
    tag: Literal["playbill-prepared-claim-attestation-v1"] = (
        "playbill-prepared-claim-attestation-v1"
    )
    claim_identity: str
    claim_artifact_digest: str
    statement: ClaimAttestationStatement


class ClaimAttestationProposalV1(_StrictEvidenceServiceModel):
    tag: Literal["playbill-claim-attestation-proposal-v1"] = (
        "playbill-claim-attestation-proposal-v1"
    )
    proposal: PlaybillProposalInspection
    claim_identity: str
    predecessor_artifact_digest: str
    candidate_artifact_digest: str
    attestation_digest: str
    competing_claim_identities: tuple[str, ...]


class PlaybillClaimVerdictQueryV1(_StrictEvidenceServiceModel):
    tag: Literal["playbill-claim-verdict-query-v1"] = "playbill-claim-verdict-query-v1"
    coordinate: PlaybillAcceptedCoordinate
    claim_identity: str
    evaluation_time: datetime
    verdict: ClaimVerdictResultV1

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Claim verdict evaluation time must be timezone-aware")
        return value


class PlaybillClaimVerdictQueryV2(_StrictEvidenceServiceModel):
    tag: Literal["playbill-claim-verdict-query-v2"] = "playbill-claim-verdict-query-v2"
    coordinate: PlaybillAcceptedCoordinate
    claim_identity: str
    evaluation_time: datetime
    verdict: ClaimVerdictResultV2

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Claim verdict evaluation time must be timezone-aware")
        return value


PlaybillClaimVerdictQueryAny = PlaybillClaimVerdictQueryV1 | PlaybillClaimVerdictQueryV2


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


def _accepted_claim(tree: Mapping[str, bytes], identity: str) -> AcceptedClaim:
    name = identity.removeprefix("Claim:")
    path = claim_path(name)
    content = tree.get(path)
    if content is None:
        raise ClaimNotFoundError(identity)
    claim = parse_claim(content, path=path)
    return AcceptedClaim(
        path=path,
        claim=claim,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
    )


def accepted_claim_providers(tree: Mapping[str, bytes]) -> dict[str, ProviderV1]:
    result: dict[str, ProviderV1] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("providers/"):
            continue
        provider = parse_provider(tree[path], path=path)
        result[provider.identity.qualified] = provider
    return result


def current_verified_claim_attestations(
    tree: Mapping[str, bytes],
    claim: ClaimArtifactAny,
    attestations: tuple[VerifiedClaimAttestationV1, ...],
) -> tuple[VerifiedClaimAttestationV1, ...]:
    """Re-grade accepted attestations against the current referent shells."""

    subject_content_digest, object_content_digest = _referent_digests(tree, claim)
    return tuple(
        item.model_copy(
            update={
                "coverage": (
                    "exact_subject"
                    if item.statement.subject_content_digest == subject_content_digest
                    and item.statement.object_content_digest == object_content_digest
                    else "shell_stale"
                ),
                "current": item.statement.subject_content_digest == subject_content_digest
                and item.statement.object_content_digest == object_content_digest,
            }
        )
        for item in attestations
    )


def _capture_contracts(
    tree: Mapping[str, bytes],
) -> dict[str, AcceptedCaptureContract]:
    result: dict[str, AcceptedCaptureContract] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("capture-contracts/"):
            continue
        contract = parse_capture_contract(tree[path], path=path)
        digest = capture_contract_digest(contract).tagged
        result[digest] = AcceptedCaptureContract(
            path=path,
            contract=contract,
            artifact_digest=digest,
        )
    return result


def _referent_digest(tree: Mapping[str, bytes], path: str) -> str:
    content = tree.get(path)
    if content is None:
        raise ProposalIntegrityError(f"Claim referent is absent: {path}")
    if path.startswith("subjects/"):
        return subject_digest(parse_subject(content, path=path)).tagged
    if path.startswith("claim-types/"):
        return claim_type_digest(parse_claim_type(content, path=path)).tagged
    raise ProposalIntegrityError(f"unsupported Claim referent path: {path}")


def _referent_digests(
    tree: Mapping[str, bytes],
    claim: ClaimArtifactAny,
) -> tuple[str, str | None]:
    subject_digest_value = _referent_digest(tree, claim.statement.subject.artifact_path)
    object_digest = (
        _referent_digest(tree, claim.statement.object.address.artifact_path)
        if isinstance(claim.statement.object, SubjectClaimObject)
        else None
    )
    return subject_digest_value, object_digest


def service_prepare_claim_attestation(
    instance: PlaybillInstance,
    *,
    claim_identity: str,
    stance: Literal["support", "contradict", "unsure"],
    signer: ArtifactIdentity,
    signing_key_id: str,
    capture_digests: tuple[str, ...],
    observed_at: datetime,
    valid_until: datetime | None = None,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PreparedClaimAttestationV1:
    """Prepare the exact public preimage; the private key remains client-held."""

    coordinate = _resolve_coordinate(instance, at)
    tree = instance.tree_at(coordinate.git_oid)
    accepted = _accepted_claim(tree, claim_identity)
    if isinstance(accepted.claim, ClaimArtifactV3):
        raise ProposalIntegrityError(
            "an attributed retired Claim is terminal and cannot accept new attestation backing"
        )
    subject_content_digest, object_content_digest = _referent_digests(tree, accepted.claim)
    statement = ClaimAttestationStatement(
        instance_id=coordinate.instance_id,
        referent_coordinate=AcceptedCoordinate.from_internal(coordinate),
        subject=accepted.claim.statement.subject,
        subject_content_digest=subject_content_digest,
        object_subject=(
            accepted.claim.statement.object.address
            if isinstance(accepted.claim.statement.object, SubjectClaimObject)
            else None
        ),
        object_content_digest=object_content_digest,
        claim_statement_digest=accepted.statement_digest,
        stance=stance,
        provider_or_principal=signer,
        signing_key_id=signing_key_id,
        capture_digests=capture_digests,
        observed_at=observed_at,
        valid_until=valid_until,
    )
    return PreparedClaimAttestationV1(
        claim_identity=accepted.claim.identity.qualified,
        claim_artifact_digest=accepted.artifact_digest,
        statement=statement,
    )


def _provider_pin(
    provider: ProviderV1,
) -> ArtifactPin:
    return ArtifactPin(
        role="provider",
        target=provider.identity,
        artifact_digest=provider_digest(provider).tagged,
    )


def service_propose_claim_attestation(
    instance: PlaybillInstance,
    *,
    claim_identity: str,
    attestation: ClaimAttestation,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    competing_claims: tuple[ClaimArtifactAny, ...] = (),
    base: PlaybillAcceptedCoordinate | None = None,
) -> ClaimAttestationProposalV1:
    """Verify/store one inert signature and propose exact tested-statement backing."""

    coordinate = _resolve_coordinate(instance, base)
    if coordinate != instance.accepted_coordinate():
        raise ProposalIntegrityError("ClaimAttestation proposals require the current accepted base")
    tree = instance.tree_at(coordinate.git_oid)
    accepted = _accepted_claim(tree, claim_identity)
    if isinstance(accepted.claim, ClaimArtifactV3):
        raise ProposalIntegrityError(
            "an attributed retired Claim is terminal and cannot accept new attestation backing"
        )
    subject_content_digest, object_content_digest = _referent_digests(tree, accepted.claim)
    principals = principal_registry_from_tree(tree, semantic_root=coordinate.semantic_root)
    providers = accepted_claim_providers(tree)
    verify_claim_attestation(
        attestation,
        verification_time=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
        expected_instance_id=coordinate.instance_id,
        expected_coordinate=AcceptedCoordinate.from_internal(coordinate),
        claim=accepted,
        referent_subject_content_digest=subject_content_digest,
        referent_object_content_digest=object_content_digest,
        principals=principals,
        providers=providers,
        store=instance.body_store(),
        current_subject_content_digest=subject_content_digest,
        current_object_content_digest=object_content_digest,
    )
    attestation_digest = store_claim_attestation(attestation, store=instance.body_store())
    contracts = _capture_contracts(tree)
    pins = {(pin.role, pin.target.qualified): pin for pin in accepted.claim.pins}
    for capture_digest_value in attestation.capture_digests:
        envelope = parse_capture_envelope(
            instance.body_store().read(
                capture_digest_value,
                access=BodyAccessContext(
                    principal_id="playbill-evidence-service",
                    can_read_body=True,
                ),
            )
        )
        contract = contracts.get(envelope.capture_contract_digest)
        if contract is None:
            raise ProposalIntegrityError("attestation CaptureContract is not accepted")
        pins[("capture-contract", contract.contract.identity.qualified)] = ArtifactPin(
            role="capture-contract",
            target=contract.contract.identity,
            artifact_digest=contract.artifact_digest,
        )
        for provider_identity in {
            envelope.producer.qualified,
            envelope.run_coordinate.executable_identity.qualified,
        }:
            provider = providers.get(provider_identity)
            if provider is not None:
                pins[("provider", provider.identity.qualified)] = _provider_pin(provider)
    new_capture_digests = set(attestation.capture_digests) - set(
        accepted.claim.backing.capture_digests
    )
    if isinstance(accepted.claim.backing, ClaimBackingV2) and new_capture_digests:
        raise ProposalIntegrityError(
            "a v2 Claim must attach new attestation Captures through explicit citations"
        )
    backing_type = (
        ClaimBackingV2 if isinstance(accepted.claim.backing, ClaimBackingV2) else ClaimBacking
    )
    backing_payload = {
        "referent_context": accepted.claim.backing.referent_context.model_copy(
            update={"observed_at": attestation.observed_at}
        ),
        "capture_digests": tuple(
            sorted(
                {
                    *accepted.claim.backing.capture_digests,
                    *attestation.capture_digests,
                }
            )
        ),
        "attestation_digests": tuple(
            sorted(
                {
                    *accepted.claim.backing.attestation_digests,
                    attestation_digest,
                }
            )
        ),
        "input_claim_digests": accepted.claim.backing.input_claim_digests,
        "reducer_digest": accepted.claim.backing.reducer_digest,
        "source_mappings": accepted.claim.backing.source_mappings,
    }
    if isinstance(accepted.claim.backing, ClaimBackingV2):
        backing_payload["citations"] = accepted.claim.backing.citations
    successor = accepted.claim.model_copy(
        update={
            "backing": backing_type.model_validate(backing_payload),
            "pins": tuple(
                sorted(
                    pins.values(),
                    key=lambda item: (item.role, item.target.qualified),
                )
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=accepted.artifact_digest),
        }
    )
    candidate_tree = dict(tree)
    candidate_tree[accepted.path] = render_claim(successor)
    competing_identities: list[str] = []
    for competing in competing_claims:
        competing_path = claim_path(competing.identity.name)
        if competing_path == accepted.path:
            raise ProposalIntegrityError("competing Claim cannot replace the tested lineage")
        if competing.statement.subject != successor.statement.subject:
            raise ProposalIntegrityError("competing Claim must address the tested subject")
        candidate_tree[competing_path] = render_claim(competing)
        competing_identities.append(competing.identity.qualified)
    ref_name = canonical_playbill_proposal_name(proposal_name, family="claim attestation")
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{ref_name}",
            proposed_base_oid=coordinate.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    return ClaimAttestationProposalV1(
        proposal=PlaybillProposalInspection(
            proposal=proposed,
            accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
                instance.accepted_coordinate()
            ),
        ),
        claim_identity=successor.identity.qualified,
        predecessor_artifact_digest=accepted.artifact_digest,
        candidate_artifact_digest=claim_artifact_digest(successor).tagged,
        attestation_digest=attestation_digest,
        competing_claim_identities=tuple(sorted(competing_identities)),
    )


def _current_replay_available(
    instance: ClaimReadSourceProtocol,
    capture_digest_value: str,
    *,
    readers: Mapping[str, ExternalSourceReaderProtocol],
) -> bool:
    store = instance.body_store()
    if not store.verify(capture_digest_value):
        return False
    envelope = parse_capture_envelope(
        store.read(
            capture_digest_value,
            access=BodyAccessContext(principal_id="playbill-verdict", can_read_body=True),
        )
    )
    if isinstance(envelope.source, CasSourceReferenceV1):
        return store.verify(envelope.source.content_digest)
    if isinstance(envelope.source, LedgerSourceReferenceV1):
        try:
            material = instance.tree_at(envelope.source.coordinate.git_oid)[
                envelope.source.address.artifact_path
            ]
        except (KeyError, ValueError):
            return False
        return "sha256:" + hashlib.sha256(material).hexdigest() == envelope.commitment.digest
    if envelope.commitment.materialization == "cas" and store.verify(envelope.commitment.digest):
        return True
    reader = readers.get(envelope.source.source_identity)
    return reader is not None and reader.replay_available(envelope.source)


@dataclass
class ClaimReadHistoryIndex:
    instance: ClaimReadSourceProtocol
    generation_oids: tuple[str, ...]
    law_evidence: dict[str, ClaimLawEvidenceAny]
    _claim_types: dict[tuple[str, str], ClaimType] | None = None

    def claim_types(self) -> Mapping[tuple[str, str], ClaimType]:
        """Materialize historical trees once, only when stale evidence needs them."""

        if self._claim_types is None:
            claim_types: dict[tuple[str, str], ClaimType] = {}
            for oid in self.generation_oids:
                tree = self.instance.tree_at(oid)
                for path in sorted(tree, key=lambda item: item.encode("utf-8")):
                    if not path.startswith("claim-types/"):
                        continue
                    claim_type = parse_claim_type(tree[path], path=path)
                    digest = claim_type_digest(claim_type).tagged
                    claim_types[(path, digest)] = claim_type
            self._claim_types = claim_types
        return self._claim_types


def _claim_read_history_index(
    instance: ClaimReadSourceProtocol,
    *,
    coordinate: AcceptedProjectionCoordinate,
) -> ClaimReadHistoryIndex:
    """Index ClaimTypes and latest Claim evidence with one history traversal."""

    history = instance.accepted_history()
    found_coordinate = False
    generation_oids: list[str] = []
    law_evidence: dict[str, ClaimLawEvidenceAny] = {}
    for generation in history:
        generation_oids.append(generation.oid)
        record = generation.record
        # The v1 receipt predates structured member law evidence; every later
        # receipt version carries it in the same shape.
        if record is not None and not isinstance(record, ChangeSetRecord):
            for evidence in record.law_evidence:
                raw = evidence.result.get("claim_evidence")
                if raw is not None:
                    law_evidence[evidence.path] = parse_claim_law_evidence(raw)
        if generation.oid == coordinate.git_oid:
            found_coordinate = True
            break
    if not found_coordinate:
        raise ProposalIntegrityError("ClaimType history coordinate is not accepted")
    return ClaimReadHistoryIndex(
        instance=instance,
        generation_oids=tuple(generation_oids),
        law_evidence=law_evidence,
    )


def _reproduced_claim_adjudication_rule(
    *,
    claim_type: ClaimType,
    evidence_digest: str,
    history: ClaimReadHistoryIndex,
) -> ClaimAdjudicationRuleV1:
    """Return the current rule when accepted evidence proves the same verdict contract.

    Queue-only policy changes cannot invalidate an otherwise identical
    adjudication receipt. The exact-current digest remains preferred. Recovery
    compares every historical predecessor's verdict-bearing projection directly
    with the current rule, so a verdict-semantic change cannot be hidden by a
    later revert.
    """

    def verdict_projection(item: ClaimType) -> dict[str, object]:
        payload = item.model_dump(mode="json")
        for field in (
            "artifact_format",
            "authority",
            "lifecycle",
            "subject_scope",
            "slot_policy",
            "attestation_consequence_policy",
        ):
            payload.pop(field, None)
        # The v1 wire omitted the later null placeholder. Parsing preserves its
        # meaning, so normalize the historical spelling before comparison.
        payload["evidence_freshness"] = (
            None
            if item.evidence_freshness is None
            else item.evidence_freshness.model_dump(mode="json")
        )
        return payload

    current_digest = claim_type_digest(claim_type).tagged
    current_rule = claim_adjudication_rule(
        claim_type,
        claim_type_digest=current_digest,
    )
    if claim_adjudication_rule_digest(current_rule) == evidence_digest:
        return current_rule
    current_projection = verdict_projection(claim_type)
    claim_type_versions = history.claim_types()
    path = claim_type_path(claim_type.predicate)
    current = claim_type
    seen = {current_digest}
    while current.lifecycle.predecessor_digest is not None:
        predecessor_digest = current.lifecycle.predecessor_digest
        if predecessor_digest in seen:
            raise ProposalIntegrityError("accepted ClaimType predecessor chain contains a cycle")
        seen.add(predecessor_digest)
        predecessor = claim_type_versions.get((path, predecessor_digest))
        if predecessor is None:
            raise ProposalIntegrityError(
                "accepted ClaimType predecessor is absent from accepted history"
            )
        predecessor_rule = claim_adjudication_rule(
            predecessor,
            claim_type_digest=predecessor_digest,
        )
        predecessor_projection = verdict_projection(predecessor)
        if predecessor_projection != current_projection:
            raise ProposalIntegrityError("accepted Claim adjudication rule does not reproduce")
        if claim_adjudication_rule_digest(predecessor_rule) == evidence_digest:
            return current_rule
        current = predecessor
    raise ProposalIntegrityError("accepted Claim adjudication rule does not reproduce")


def service_evaluate_playbill_claim_verdict(
    instance: PlaybillInstance,
    *,
    claim_identity: str,
    evaluation_time: datetime,
    at: PlaybillAcceptedCoordinate | None = None,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
) -> PlaybillClaimVerdictQueryAny:
    """Recompute currency/verdict from accepted evidence at one explicit time."""

    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ProposalIntegrityError("Claim verdict evaluation_time must be timezone-aware")
    coordinate = _resolve_coordinate(instance, at)
    tree = instance.tree_at(coordinate.git_oid)
    accepted = _accepted_claim(tree, claim_identity)
    history = _claim_read_history_index(instance, coordinate=coordinate)
    evidence = history.law_evidence.get(accepted.path)
    if evidence is None:
        raise ProposalIntegrityError("accepted Claim has no verdict law evidence")
    type_path = claim_type_path(accepted.claim.statement.predicate)
    claim_type = parse_claim_type(tree[type_path], path=type_path)
    rule = _reproduced_claim_adjudication_rule(
        claim_type=claim_type,
        evidence_digest=evidence.adjudication_rule_digest,
        history=history,
    )
    readers = external_readers or {}
    captures = tuple(
        item.model_copy(
            update={
                "current_replay_available": _current_replay_available(
                    instance,
                    item.capture_digest,
                    readers=readers,
                )
            }
        )
        for item in evidence.verdict_captures
    )
    if evidence.verdict_result is not None:
        verify_claim_verdict_freshness(
            evidence.verdict_result,
            rule=rule,
            captures=evidence.verdict_captures,
        )
    subject_content_digest, object_content_digest = _referent_digests(tree, accepted.claim)
    referent_current = (
        accepted.claim.backing.referent_context.subject_content_digest == subject_content_digest
        and accepted.claim.backing.referent_context.object_content_digest == object_content_digest
    )
    attestations = current_verified_claim_attestations(
        tree,
        accepted.claim,
        evidence.verified_attestations,
    )
    verdict = evaluate_claim_verdict(
        claim_statement_digest=accepted.statement_digest,
        rule=rule,
        evaluation_time=evaluation_time,
        captures=captures,
        attestations=attestations,
        providers=accepted_claim_providers(tree),
        claim_effective_from=accepted.claim.statement.effective_from,
        claim_effective_until=accepted.claim.statement.effective_until,
        referent_current=referent_current,
        # Authority is resolved from live mandate/resolution state by PC-E1's
        # resolver. Acceptance-time verdict output is never carried forward.
        resolved_authority_basis=(),
    )
    if isinstance(verdict, ClaimVerdictResultV2):
        return PlaybillClaimVerdictQueryV2(
            coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
            claim_identity=accepted.claim.identity.qualified,
            evaluation_time=evaluation_time,
            verdict=verdict,
        )
    return PlaybillClaimVerdictQueryV1(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        claim_identity=accepted.claim.identity.qualified,
        evaluation_time=evaluation_time,
        verdict=verdict,
    )


def service_get_playbill_standing_mandate(
    instance: PlaybillInstance,
    *,
    identity: str,
    at: PlaybillAcceptedCoordinate | None = None,
) -> StandingMandateQueryResultV1:
    """Read one exact StandingMandate at a resolved accepted coordinate."""

    coordinate = _resolve_coordinate(instance, at)
    name = identity.removeprefix("StandingMandate:")
    path = standing_mandate_path(name)
    tree = instance.tree_at(coordinate.git_oid)
    content = tree.get(path)
    if content is None:
        raise ProposalIntegrityError(f"StandingMandate is absent: {identity}")
    mandate = parse_standing_mandate(content, path=path)
    if mandate.identity.qualified != identity:
        raise ProposalIntegrityError("StandingMandate identity/path disagreement")
    return StandingMandateQueryResultV1(
        coordinate=AcceptedCoordinate.from_internal(coordinate),
        mandate=mandate,
        mandate_digest=standing_mandate_digest(mandate).tagged,
    )


__all__ = [
    "ClaimAttestationProposalV1",
    "PlaybillClaimVerdictQueryV1",
    "PlaybillClaimVerdictQueryV2",
    "PlaybillClaimVerdictQueryAny",
    "PreparedClaimAttestationV1",
    "accepted_claim_providers",
    "current_verified_claim_attestations",
    "service_evaluate_playbill_claim_verdict",
    "service_get_playbill_standing_mandate",
    "service_prepare_claim_attestation",
    "service_propose_claim_attestation",
]
