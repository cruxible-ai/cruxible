"""Client-held ClaimAttestation submission and explicit-time verdict reads."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, MutableSet
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    capture_contract_digest,
    parse_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyProjectionProtocol
from cruxible_client.contracts.claim_attestations import (
    VerifiedClaimAttestationV1,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    CaptureVerdictEvidenceV1,
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
    ClaimLawEvidenceAny,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
    parse_claim_law_evidence,
)
from cruxible_client.contracts.errors import ClaimNotFoundError, ProposalIntegrityError
from cruxible_client.contracts.providers import ProviderV1, parse_provider
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
from cruxible_core.evidence.source_readers import ExternalSourceReaderProtocol
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.proposals.settlement import ChangeSetRecord, ChangeSetRecordAnyVersion
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    PlaybillAcceptedCoordinate,
)
from cruxible_core.storage.cas import BodyAccessContext


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
    return _accepted_claim_artifact(claim)


def _accepted_claim_artifact(claim: ClaimArtifactAny) -> AcceptedClaim:
    return AcceptedClaim(
        path=claim_path(claim.identity.name),
        claim=claim,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
    )


@dataclass(frozen=True)
class ClaimVerdictReadContext:
    """One request's immutable accepted inputs; never retains current evidence.

    Shares selected accepted bytes, parsed Claim inputs and provider lookups
    across a batch. CAS availability, attestations and time-dependent
    verdicts are still evaluated by the ordinary service for each Claim.
    """

    instance: PlaybillInstance
    coordinate: AcceptedProjectionCoordinate
    _claims: dict[str, ClaimArtifactAny] = dataclass_field(default_factory=dict, init=False)
    _providers: Mapping[str, ProviderV1] | None = dataclass_field(default=None, init=False)
    _source_bytes: dict[str, bytes] = dataclass_field(default_factory=dict, init=False)
    _tree: Mapping[str, bytes] = dataclass_field(init=False)

    def __post_init__(self) -> None:
        from cruxible_core.indexes.evaluated_state import SelectedRows

        object.__setattr__(
            self,
            "_tree",
            SelectedRows(self._source, lambda: self.instance.paths_at(self.coordinate.git_oid)),
        )

    def _source(self, path: str) -> bytes:
        if path not in self._source_bytes:
            content = self.instance.blob_at(self.coordinate.git_oid, path)
            if content is None:
                raise KeyError(path)
            self._source_bytes[path] = content
        return self._source_bytes[path]

    @property
    def tree(self) -> Mapping[str, bytes]:
        return self._tree

    def claims(self) -> tuple[ClaimArtifactAny, ...]:
        with self.instance.bind_accepted_projection(self.coordinate) as projection:
            identities = tuple(row.identity for row in projection.typed.envelopes(kind="claim"))
        for identity in identities:
            self.claim(identity)
        return tuple(self._claims.values())

    def claim(self, identity: str) -> ClaimArtifactAny:
        identity = "Claim:" + identity.removeprefix("Claim:")
        if identity not in self._claims:
            path = claim_path(identity.removeprefix("Claim:"))
            content = self._tree.get(path)
            if content is None:
                raise ClaimNotFoundError(identity)
            self._claims[identity] = parse_claim(content, path=path)
        return self._claims[identity]

    def providers(self) -> Mapping[str, ProviderV1]:
        if self._providers is None:
            object.__setattr__(
                self,
                "_providers",
                accepted_claim_providers(self.instance, coordinate=self.coordinate),
            )
        assert self._providers is not None
        return self._providers


class _AcceptedClaimProviders(Mapping[str, ProviderV1]):
    """One request's provider closure, selected from its immutable accepted coordinate."""

    def __init__(
        self, instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
    ) -> None:
        self._instance = instance
        self._coordinate = coordinate
        self._selected: dict[str, ProviderV1] = {}

    def __getitem__(self, identity: str) -> ProviderV1:
        if identity not in self._selected:
            if not identity.startswith("Provider:"):
                raise KeyError(identity)
            with self._instance.bind_accepted_projection(self._coordinate) as projection:
                provider = projection.typed.source(identity)
                if provider is None:
                    raise KeyError(identity)
                self._selected[identity] = provider
        return self._selected[identity]

    def __iter__(self) -> Iterator[str]:
        with self._instance.bind_accepted_projection(self._coordinate) as projection:
            identities = tuple(row.identity for row in projection.typed.envelopes(kind="provider"))
        return iter(identities)

    def __len__(self) -> int:
        with self._instance.bind_accepted_projection(self._coordinate) as projection:
            return int(
                projection.typed.connection.execute("SELECT count(*) FROM providers").fetchone()[0]
            )


def accepted_claim_providers(
    instance: ClaimReadSourceProtocol, *, coordinate: AcceptedProjectionCoordinate
) -> Mapping[str, ProviderV1]:
    if isinstance(instance, PlaybillInstance):
        return _AcceptedClaimProviders(instance, coordinate)
    # Cold accepted replay has no published projection yet. Its source tree is
    # the explicit reconstruction oracle, never the live service read path.
    tree = instance.tree_at(coordinate.git_oid)
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
            material = (
                instance.blob_at(
                    envelope.source.coordinate.git_oid, envelope.source.address.artifact_path
                )
                if isinstance(instance, PlaybillInstance)
                else instance.tree_at(envelope.source.coordinate.git_oid).get(
                    envelope.source.address.artifact_path
                )
            )
        except (KeyError, ValueError):
            return False
        return (
            material is not None
            and "sha256:" + hashlib.sha256(material).hexdigest() == envelope.commitment.digest
        )
    if envelope.commitment.materialization == "cas" and store.verify(envelope.commitment.digest):
        return True
    reader = readers.get(envelope.source.source_identity)
    return reader is not None and reader.replay_available(envelope.source)


@dataclass
class ClaimReadHistoryIndex:
    instance: ClaimReadSourceProtocol
    generation_oids: tuple[str, ...]
    law_evidence: Mapping[str, ClaimLawEvidenceAny]
    _claim_types: Mapping[tuple[str, str], ClaimType] | None = None

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


class _IndexedClaimLawEvidence(Mapping[str, ClaimLawEvidenceAny]):
    def __init__(self, instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate):
        self.instance = instance
        self.at = AcceptedCoordinate.from_internal(coordinate)

    def __getitem__(self, path: str) -> ClaimLawEvidenceAny:
        with self.instance.accepted_history_reader(at=self.at) as history:
            locations = history.claim_law_locations(path=path, latest=True)
            if not locations:
                raise KeyError(path)
            record = history.read_member_record(locations[0], self.instance.blob_at)
            if isinstance(record, ChangeSetRecord):
                raise KeyError(path)
            evidence = next((item for item in record.law_evidence if item.path == path), None)
            raw = None if evidence is None else evidence.result.get("claim_evidence")
            if raw is None:
                raise ProposalIntegrityError("accepted Claim law locator has no retained evidence")
            return parse_claim_law_evidence(raw)

    def __iter__(self) -> Iterator[str]:
        with self.instance.accepted_history_reader(at=self.at) as history:
            paths = {item.member_path for item in history.claim_law_locations()}
        return iter(sorted(paths))

    def __len__(self) -> int:
        return sum(1 for _ in self)


class _IndexedClaimTypes(Mapping[tuple[str, str], ClaimType]):
    def __init__(self, instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate):
        self.instance = instance
        self.at = AcceptedCoordinate.from_internal(coordinate)

    def __getitem__(self, key: tuple[str, str]) -> ClaimType:
        path, digest = key
        with self.instance.accepted_history_reader(at=self.at) as history:
            location = history.artifact(digest)
            if location is None or location.path != path:
                raise KeyError(key)
            generation = history.generation(location.occurrence_sequence)
        raw = self.instance.blob_at(generation.git_oid, location.path)
        if raw is None:
            raise ProposalIntegrityError("historical ClaimType source is unavailable")
        claim_type = parse_claim_type(raw, path=path)
        if claim_type_digest(claim_type).tagged != digest:
            raise ProposalIntegrityError("historical ClaimType differs from requested digest")
        return claim_type

    def __iter__(self) -> Iterator[tuple[str, str]]:
        with self.instance.accepted_history_reader(at=self.at) as history:
            keys = {(row.path, row.artifact_digest) for row in history.claim_type_versions()}
        return iter(sorted(keys))

    def __len__(self) -> int:
        return sum(1 for _ in self)


def _claim_read_history_index(
    instance: ClaimReadSourceProtocol,
    *,
    coordinate: AcceptedProjectionCoordinate,
) -> ClaimReadHistoryIndex:
    """Live reads use retained locators; unprojected replay folds its supplied source."""
    if isinstance(instance, PlaybillInstance):
        return ClaimReadHistoryIndex(
            instance=instance,
            generation_oids=(),
            law_evidence=_IndexedClaimLawEvidence(instance, coordinate),
            _claim_types=_IndexedClaimTypes(instance, coordinate),
        )
    return _build_claim_read_history_index(instance, coordinate=coordinate)


def _build_claim_read_history_index(
    instance: ClaimReadSourceProtocol,
    *,
    coordinate: AcceptedProjectionCoordinate,
) -> ClaimReadHistoryIndex:
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


def _record_verdict_time_boundaries(
    boundaries: MutableSet[datetime],
    *,
    rule: ClaimAdjudicationRuleV1,
    claim: ClaimArtifactAny,
    captures: tuple[CaptureVerdictEvidenceV1, ...],
    attestations: tuple[VerifiedClaimAttestationV1, ...],
) -> None:
    """Record every instant at which this verdict's answer could change.

    A verdict is a step function of the evaluation instant, and these are its
    only breakpoints: the Claim's own effective interval, each capture's
    observation, source expiry and freshness horizon, and each attestation's
    observation and validity end. Between two consecutive breakpoints the answer
    is the same at every instant, which is what lets a read be remembered
    without pinning the wall clock into its key.
    """

    for instant in (claim.statement.effective_from, claim.statement.effective_until):
        if instant is not None:
            boundaries.add(instant)
    for capture in captures:
        boundaries.add(capture.observed_at)
        if capture.source_effective_until is not None:
            boundaries.add(capture.source_effective_until)
        if rule.max_evidence_age is not None:
            boundaries.add(
                capture.observed_at + timedelta(microseconds=rule.max_evidence_age.microseconds)
            )
    for attestation in attestations:
        boundaries.add(attestation.statement.observed_at)
        if attestation.statement.valid_until is not None:
            boundaries.add(attestation.statement.valid_until)


def service_evaluate_playbill_claim_verdict(
    instance: PlaybillInstance,
    *,
    claim_identity: str,
    evaluation_time: datetime,
    at: PlaybillAcceptedCoordinate | None = None,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
    time_boundaries: MutableSet[datetime] | None = None,
    read_context: ClaimVerdictReadContext | None = None,
) -> PlaybillClaimVerdictQueryAny:
    """Recompute currency/verdict from accepted evidence at one explicit time.

    ``time_boundaries``, when supplied, collects the instants this verdict's
    answer could change at, so a caller memoizing a whole derivation can say
    over what interval its answer holds instead of keying on the wall clock.
    """

    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ProposalIntegrityError("Claim verdict evaluation_time must be timezone-aware")
    coordinate = _resolve_coordinate(instance, at)
    if read_context is not None and (
        read_context.instance is not instance or read_context.coordinate != coordinate
    ):
        raise ProposalIntegrityError("Claim read context differs from requested accepted state")
    read_context = read_context or ClaimVerdictReadContext(instance, coordinate)
    tree = read_context.tree
    accepted = _accepted_claim_artifact(read_context.claim(claim_identity))
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
    if time_boundaries is not None:
        _record_verdict_time_boundaries(
            time_boundaries,
            rule=rule,
            claim=accepted.claim,
            captures=captures,
            attestations=attestations,
        )
    verdict = evaluate_claim_verdict(
        claim_statement_digest=accepted.statement_digest,
        rule=rule,
        evaluation_time=evaluation_time,
        captures=captures,
        attestations=attestations,
        providers=read_context.providers(),
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
    content = instance.blob_at(coordinate.git_oid, path)
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
    "PlaybillClaimVerdictQueryV1",
    "PlaybillClaimVerdictQueryV2",
    "PlaybillClaimVerdictQueryAny",
    "accepted_claim_providers",
    "current_verified_claim_attestations",
    "service_evaluate_playbill_claim_verdict",
    "service_get_playbill_standing_mandate",
]
