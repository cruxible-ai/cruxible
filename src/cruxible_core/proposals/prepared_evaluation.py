"""Instance-owned, single-submit handoff of a pure proposal evaluation.

No token crosses a transport, survives its call scope, or authorizes a write.
Submission still validates ingress and current writable/actor/base/ref bindings.
CAS observations are freshly replayed; operational query/receipt/promotion reads
and CAS writes make an evaluation ineligible until they have revision bindings.

The same observations carry an admission evaluation on to the settlement of the
same request (``AdmittedEvaluation``, maintainer ruling
settle-reuses-first-evaluation-1015). There a settle run's own producer
receipts are stable (its fenced, append-only journal records, written under
this request), and accepted query facts are observed and replayed like bodies.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from cruxible_client.contracts.authoring_profiles import ClaimTypeExpansionEvidence
from cruxible_client.contracts.candidates import CandidateRecordAnyVersion
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    CaptureObjectStoreProtocol,
    CaptureProductionRecord,
    ProcedureProducerReceiptProtocol,
    ProducerReceiptResolverProtocol,
    ProviderProducerReceiptResolution,
    VerifiedCaptureMemo,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.documents import BodyVerifierProtocol
from cruxible_client.contracts.proposal_models import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalReceiveLimits,
    ProposalResult,
)
from cruxible_core.derived.derived_state import (
    DerivedState,
    IndexDefinition,
    SnapshotTree,
    without_cards,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate

if TYPE_CHECKING:
    from cruxible_core.proposals.delegated_authority import DelegatedAuthorityVerdict
    from cruxible_core.proposals.proposals import CandidateEvaluation


@dataclass(frozen=True, slots=True)
class _BodyObservation:
    method: str
    digest: str
    access: bytes | None
    result: bytes | bool


def _binding(
    current: AcceptedProjectionCoordinate,
    actor: AuthenticatedActor,
    request: ProposalAdmissionRequest,
    limits: ProposalReceiveLimits,
    timestamp: str,
) -> bytes:
    return canonical_bytes(
        {
            "current": current.model_dump(mode="json"),
            "actor": actor.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "limits": limits.model_dump(mode="json"),
            "timestamp": timestamp,
        }
    )


class _ObservedBodies:
    def __init__(self, source: CaptureObjectStoreProtocol, scope: PreparedEvaluationScope) -> None:
        self.source, self.scope = source, scope

    def verify(self, digest: str) -> bool:
        return cast(bool, self._call("verify", digest, None))

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes:
        return cast(bytes, self._call("read", digest, access))

    def _call(self, method: str, digest: str, access: BodyAccessContext | None) -> Any:
        encoded = None if access is None else canonical_bytes(access.model_dump(mode="json"))
        try:
            value = (
                self.source.verify(digest)
                if access is None
                else self.source.read(digest, access=access)
            )
        except BaseException:
            self.scope.ineligible = True
            raise
        self.scope.observe(_BodyObservation(method, digest, encoded, value))
        return value

    def __getattr__(self, name: str) -> Any:
        self.scope.ineligible = True
        return getattr(self.source, name)

    def store(self, content: bytes, *, journal_payload: bool = False):  # type: ignore[no-untyped-def]
        # Stored as an evidence body either way: over-advancing the evidence
        # generation only costs a memo miss.
        del journal_payload
        self.scope.ineligible = True
        return self.source.store(content)


class _OperationalVerifier:
    def __init__(self, source: Any, scope: PreparedEvaluationScope) -> None:
        self.source, self.scope = source, scope

    def verify_promotion(self, promotion):  # type: ignore[no-untyped-def]
        self.scope.ineligible = True
        return self.source.verify_promotion(promotion)


class _ObservedReceipts:
    def __init__(self, source: ProducerReceiptResolverProtocol, scope: PreparedEvaluationScope):
        self.source, self.scope = source, scope
        # Verifications this evaluation finished itself, through its observed bodies.
        self._own: set[str] = set()

    def producer_receipt(
        self, digest: str, *, run_id: str
    ) -> ProviderProducerReceiptResolution | ProcedureProducerReceiptProtocol | None:
        if run_id != self.scope.stable_run:
            self.scope.ineligible = True
        return self.source.producer_receipt(digest, run_id=run_id)

    def produced_capture(
        self, capture_digest: str, *, run_id: str
    ) -> tuple[CaptureProductionRecord, ...]:
        if run_id != self.scope.stable_run:
            self.scope.ineligible = True
        return self.source.produced_capture(capture_digest, run_id=run_id)

    def recall_verified(self, capture_digest: str) -> VerifiedCaptureMemo | None:
        memo = self.source.recall_verified(capture_digest)
        if memo is not None and capture_digest not in self._own and self.scope.stable_run is None:
            # An answer this evaluation did not observe in the bodies it reads.
            # A settle run's request verified it from immutable journal records.
            self.scope.ineligible = True
        return memo

    def remember_verified(self, memo: VerifiedCaptureMemo) -> None:
        self._own.add(memo.capture_digest)
        self.source.remember_verified(memo)


class PreparedEvaluationScope:
    """Private capability held by exactly one synchronous coordinator submit."""

    def __init__(
        self, owner: PreparedEvaluationAdapter, epoch: int, *, stable_run: str | None = None
    ) -> None:
        self.owner, self.epoch = owner, epoch
        #: The settle run whose own journal receipts are stable in this request.
        self.stable_run = stable_run
        self.closed = False
        self.consumed = False
        self.ineligible = False
        self._observations: dict[tuple[str, str, bytes | None], _BodyObservation] = {}
        self._facts: list[FactsObservation] = []
        self._bytes = 0
        self._operation: bytes | None = None
        self._binding: bytes | None = None
        self._outcome: CandidateEvaluation | None = None
        self.submission_tree: SnapshotTree | None = None

    def observe(self, observation: _BodyObservation) -> None:
        if self.ineligible:
            return
        key = observation.method, observation.digest, observation.access
        previous = self._observations.get(key)
        if previous is not None:
            if previous != observation:
                self.ineligible = True
            return
        self._bytes += len(observation.result) if isinstance(observation.result, bytes) else 1
        self._bytes += len(observation.digest) + len(observation.access or b"") + 128
        if (
            self._bytes > self.owner.max_bytes
            or len(self._observations) >= self.owner.max_observations
        ):
            self.ineligible = True
            self._observations.clear()
        else:
            self._observations[key] = observation

    def bodies(self, source: BodyVerifierProtocol) -> BodyVerifierProtocol:
        # Preserve runtime protocol eligibility; a verify-only store must not
        # suddenly appear to implement the managed Capture store interface.
        if not isinstance(source, CaptureObjectStoreProtocol):
            self.ineligible = True
            return source
        return _ObservedBodies(source, self)

    def operational(self, source: Any) -> Any:
        if source is None:
            return None

        def observed(*args: Any, **kwargs: Any) -> Any:
            self.ineligible = True
            return source(*args, **kwargs)

        return observed

    def facts(self, source: Any) -> Any:
        """Accepted query facts, observed so a settlement can replay them exactly."""
        if source is None:
            return None

        def observed(
            coordinate: AcceptedProjectionCoordinate,
            *,
            predicates: tuple[str, ...] | None = None,
            subject_kinds: tuple[str, ...] | None = None,
        ) -> Any:
            value, observation = observed_facts_build(
                source,
                coordinate,
                predicates=predicates,
                subject_kinds=subject_kinds,
                inputs=self.owner.fact_inputs,
            )
            if not self.ineligible:
                if observation is None or len(self._facts) >= self.owner.max_observations:
                    # A build no writer-owned binding names cannot be replayed.
                    self.ineligible = True
                    self._facts.clear()
                else:
                    self._facts.append(observation)
            return value

        return observed

    def receipts(
        self, source: ProducerReceiptResolverProtocol | None
    ) -> ProducerReceiptResolverProtocol | None:
        """Journal facts are not body observations: consulting them ends reuse."""

        return None if source is None else _ObservedReceipts(source, self)

    def promotion(self, source: Any) -> Any:
        return None if source is None else _OperationalVerifier(source, self)

    def retain(
        self,
        outcome: CandidateEvaluation,
        *,
        operation: bytes,
        current: AcceptedProjectionCoordinate,
        actor: AuthenticatedActor,
        request: ProposalAdmissionRequest,
        limits: ProposalReceiveLimits,
        timestamp: str,
    ) -> None:
        if self.closed or self.ineligible or outcome.candidate is None or outcome.rebased:
            return
        from cruxible_core.proposals.proposals import CandidateEvaluation

        # Only immutable trees, detached result models and the derived state of
        # the candidate tree (immutable; a settlement in the same request
        # builds its generation from it) are retained.
        tree = (
            outcome.tree if isinstance(outcome.tree, SnapshotTree) else SnapshotTree(outcome.tree)
        )
        self.submission_tree = without_cards(tree)
        self._outcome = CandidateEvaluation(
            tree,
            copy.deepcopy(outcome.candidate),
            copy.deepcopy(outcome.diagnostics),
            False,
            state=outcome.state,
            claim_admission_accounts=copy.deepcopy(outcome.claim_admission_accounts),
        )
        self._binding = _binding(current, actor, request, limits, timestamp)
        self._operation = operation

    def handoff(self, operation: bytes) -> PreparedEvaluationScope | None:
        if self._operation != operation or self._outcome is None or self.ineligible or self.closed:
            return None
        return self

    def take(
        self,
        *,
        owner: PreparedEvaluationAdapter | None,
        current: AcceptedProjectionCoordinate,
        actor: AuthenticatedActor,
        request: ProposalAdmissionRequest,
        limits: ProposalReceiveLimits,
        timestamp: str,
        tree: Mapping[str, bytes],
        bodies: BodyVerifierProtocol,
    ) -> CandidateEvaluation | None:
        if self.consumed or self.closed:
            return None
        self.consumed = True
        if (
            owner is not self.owner
            or self.epoch != self.owner.epoch
            or self.ineligible
            or self._outcome is None
            or tree is not self.submission_tree
            or self._binding != _binding(current, actor, request, limits, timestamp)
        ):
            self.owner.count("invalidated")
            return None
        try:
            for observation in self._observations.values():
                actual = (
                    bodies.verify(observation.digest)
                    if observation.access is None
                    else cast(CaptureObjectStoreProtocol, bodies).read(
                        observation.digest,
                        access=BodyAccessContext.model_validate_json(observation.access),
                    )
                )
                if actual != observation.result:
                    self.owner.count("invalidated")
                    return None
        except Exception:
            # The ordinary evaluator reproduces its existing refusal/exception
            # boundary instead of freshness replay inventing a new error surface.
            self.owner.count("invalidated")
            return None
        if self.epoch != self.owner.epoch:
            self.owner.count("invalidated")
            return None
        self.owner.count("reused")
        from cruxible_core.proposals.proposals import CandidateEvaluation

        value = self._outcome
        return CandidateEvaluation(
            value.tree,
            copy.deepcopy(value.candidate),
            copy.deepcopy(value.diagnostics),
            False,
            state=value.state,
            claim_admission_accounts=copy.deepcopy(value.claim_admission_accounts),
        )

    def admitted(
        self,
        outcome: CandidateEvaluation,
        *,
        current: AcceptedProjectionCoordinate,
        actor_id: str,
        timestamp: str,
        mandate_digest: str | None,
        claim_type_expansions: tuple[ClaimTypeExpansionEvidence, ...],
    ) -> AdmittedEvaluation | None:
        """This scope's evaluation, as the same request's settlement may reuse it.

        None unless every input the evaluation consulted can be replayed: an
        ineligible or closed scope, a rebased or refused outcome, or one
        without its derived state is settled by a full re-evaluation.
        """
        if (
            self.closed
            or self.ineligible
            or outcome.rebased
            or outcome.candidate is None
            or outcome.diagnostics
            or outcome.state is None
            or not isinstance(outcome.tree, SnapshotTree)
        ):
            return None
        return AdmittedEvaluation(
            owner=self.owner,
            epoch=self.epoch,
            outcome=outcome,
            current=current,
            actor_id=actor_id,
            timestamp=timestamp,
            mandate_digest=mandate_digest,
            claim_type_expansions=claim_type_expansions,
            observations=tuple(self._observations.values()),
            facts=tuple(self._facts),
        )

    def close(self) -> None:
        self.closed = True
        self._observations.clear()
        self._facts.clear()
        self._outcome = None
        self.submission_tree = None


#: The writer-owned fingerprint of every mutable input an accepted query-facts
#: build reads besides the accepted tree; None when it cannot be read.
FactInputs = Callable[[], "str | None"]


@dataclass(frozen=True, slots=True)
class FactsObservation:
    """One accepted query-facts build an evaluation consumed, and what it was built on.

    At one accepted coordinate a fact row still varies with the body store
    (a cited Capture's replay availability) and the re-observation journal (a
    lineage's freshness, read when a condition evaluates). ``inputs`` is their
    writer-owned fingerprint (``verdict_input_fingerprint``: the body-store
    landing log, which moves on every evidence body landing or erase, and the
    re-observation journal's log), read before the build.
    """

    coordinate: AcceptedProjectionCoordinate
    predicates: tuple[str, ...] | None
    subject_kinds: tuple[str, ...] | None
    inputs: str
    result: Any


def observed_facts_build(
    source: Any,
    coordinate: AcceptedProjectionCoordinate,
    *,
    predicates: tuple[str, ...] | None,
    subject_kinds: tuple[str, ...] | None,
    inputs: FactInputs | None,
) -> tuple[Any, FactsObservation | None]:
    """One facts build, with its observation; None when no binding names its inputs."""
    seen = None if inputs is None else inputs()
    value = source(coordinate, predicates=predicates, subject_kinds=subject_kinds)
    if seen is None:
        return value, None
    return value, FactsObservation(coordinate, predicates, subject_kinds, seen, value)


def facts_replay(
    facts: tuple[FactsObservation, ...],
    *,
    query_facts_provider: Any,
    inputs: FactInputs | None,
) -> bool:
    """Whether every observed facts build stands: same inputs, and the same answer now.

    The binding is checked first, so a body landing or erase, or a recorded
    re-observation, since the build fails without rebuilding anything. On an
    unchanged binding the provider answers again (a request's pooled build
    when its body identities hold, else a fresh build) and must equal the
    observed one; that covers a body rewritten in place, which the
    writer-owned log does not see. Raising means not replayed.
    """
    if not facts:
        return True
    if query_facts_provider is None or inputs is None:
        return False
    try:
        now = inputs()
        for fact in facts:
            if now is None or fact.inputs != now:
                return False
            actual = query_facts_provider(
                fact.coordinate, predicates=fact.predicates, subject_kinds=fact.subject_kinds
            )
            if actual is not fact.result and actual != fact.result:
                return False
    except Exception:
        return False
    return True


def _replays(
    observations: tuple[_BodyObservation, ...],
    facts: tuple[FactsObservation, ...],
    *,
    bodies: BodyVerifierProtocol,
    query_facts_provider: Any,
    inputs: FactInputs | None,
) -> bool:
    """Whether every observed body read and fact build answers now exactly as it did."""
    try:
        for observation in observations:
            actual = (
                bodies.verify(observation.digest)
                if observation.access is None
                else cast(CaptureObjectStoreProtocol, bodies).read(
                    observation.digest,
                    access=BodyAccessContext.model_validate_json(observation.access),
                )
            )
            if actual != observation.result:
                return False
    except Exception:
        # The full evaluation reproduces its own refusal or exception boundary.
        return False
    return facts_replay(facts, query_facts_provider=query_facts_provider, inputs=inputs)


@dataclass(frozen=True)
class AdmittedEvaluation:
    """A submission's admission evaluation, for the same request's own settlement.

    Settlement re-derives a candidate from durable records because activation
    is, in general, a separate request. In the request that submitted it the
    evaluation is still in hand: ``reusable`` returns it instead when the base
    is the one it evaluated at, it was not rebased, the candidate and tree are
    exactly the ones it produced, the derived-state epoch is unchanged and every
    body read and fact build it observed answers the same. Anything else
    re-evaluates in full.
    """

    owner: PreparedEvaluationAdapter
    epoch: int
    outcome: CandidateEvaluation
    current: AcceptedProjectionCoordinate
    actor_id: str
    timestamp: str
    mandate_digest: str | None
    claim_type_expansions: tuple[ClaimTypeExpansionEvidence, ...]
    observations: tuple[_BodyObservation, ...]
    facts: tuple[FactsObservation, ...]

    def reusable(
        self,
        *,
        base: AcceptedProjectionCoordinate,
        candidate: CandidateRecordAnyVersion,
        candidate_tree: Mapping[str, bytes],
        actor_id: str,
        mandate_digest: str | None,
        claim_type_expansions: tuple[ClaimTypeExpansionEvidence, ...],
        bodies: BodyVerifierProtocol,
        query_facts_provider: Any,
    ) -> CandidateEvaluation | None:
        outcome = self.outcome
        assert outcome.candidate is not None  # admitted() holds only a candidate
        if (
            self.epoch != self.owner.epoch
            or outcome.rebased
            or outcome.state is None
            or base != self.current
            or candidate_tree is not outcome.tree
            or candidate.candidate_digest != outcome.candidate.candidate_digest
            or candidate.candidate.timestamp != self.timestamp
            or actor_id != self.actor_id
            or mandate_digest != self.mandate_digest
            or claim_type_expansions != self.claim_type_expansions
            or not _replays(
                self.observations,
                self.facts,
                bodies=bodies,
                query_facts_provider=query_facts_provider,
                inputs=self.owner.fact_inputs,
            )
            or self.epoch != self.owner.epoch
        ):
            self.owner.count("settlement_invalidated")
            return None
        self.owner.count("settlement_reused")
        return outcome


@dataclass(frozen=True)
class SubmittedProposal:
    """What one request's own submission holds in memory once it is durable."""

    #: The admission, evaluation and candidate records exactly as submit wrote them.
    result: ProposalResult
    #: The evaluated tree submit committed as the proposal's tree.
    tree: SnapshotTree
    #: The accepted coordinate the submission evaluated at and published against.
    current: AcceptedProjectionCoordinate
    #: Its admission evaluation, when settlement may reuse it (see ``AdmittedEvaluation``).
    evaluation: AdmittedEvaluation | None = None


class SettlementHandoff:
    """One request's own submission, held for that same request's activation.

    The caller that will activate (a settle terminal, a write) creates it and
    passes it explicitly to the submission and then to the activation; it is
    never stored on the instance, so an activation requested separately (a
    ``proposal activate``, an approval) never sees one. Single use: the first
    activation that takes it consumes it, whatever proposal it names.
    """

    def __init__(
        self,
        owner: PreparedEvaluationAdapter | None = None,
        *,
        stable_run: str | None = None,
    ) -> None:
        #: The instance's adapter: without one, admission is not observed for reuse.
        self.owner = owner
        #: A settle run's own id: its journal receipts are stable in this request.
        self.stable_run = stable_run
        self._submitted: SubmittedProposal | None = None
        self._taken = False
        #: The settle terminal's own condition check at the head it submits at,
        #: for the admission evaluation to use when its inputs are the same.
        self.settle_condition: DelegatedAuthorityVerdict | None = None

    def submitted(
        self,
        *,
        result: ProposalResult,
        tree: Mapping[str, bytes],
        current: AcceptedProjectionCoordinate,
        evaluation: AdmittedEvaluation | None = None,
    ) -> None:
        """Hold a durable submission; only a tree that stays immutable is held."""
        if self._taken or self._submitted is not None or result.candidate is None:
            return
        if not isinstance(tree, SnapshotTree):
            return
        if evaluation is not None and evaluation.outcome.tree is not tree:
            evaluation = None
        self._submitted = SubmittedProposal(
            result=result, tree=tree, current=current, evaluation=evaluation
        )

    def observer(self) -> PreparedEvaluationScope | None:
        """A fresh scope observing one admission evaluation, or None without an owner."""
        if self.owner is None or self._taken or self._submitted is not None:
            return None
        return PreparedEvaluationScope(self.owner, self.owner.epoch, stable_run=self.stable_run)

    def take(self, proposal_id: str) -> SubmittedProposal | None:
        """The submission of ``proposal_id``, once; None for any other proposal."""
        if self._taken:
            return None
        self._taken = True
        submitted, self._submitted = self._submitted, None
        if submitted is None or submitted.result.admission.proposal_id != proposal_id:
            return None
        return submitted


class PreparedEvaluationAdapter:
    """Lifecycle owner for call-local handoffs, not a persistent result cache."""

    def __init__(
        self,
        owner: DerivedState,
        *,
        max_bytes: int = 32 * 1024 * 1024,
        max_observations: int = 16384,
        fact_inputs: FactInputs | None = None,
    ) -> None:
        #: The instance's fact-input binding; without one no facts build is replayable.
        self.fact_inputs = fact_inputs
        self._lock = threading.Lock()
        self._epoch = 0
        self._counts = {
            "started": 0,
            "reused": 0,
            "invalidated": 0,
            "ineligible": 0,
            "settlement_reused": 0,
            "settlement_invalidated": 0,
        }
        self.max_bytes, self.max_observations = max_bytes, max_observations
        owner.register(
            IndexDefinition("prepared-evaluation", "candidate", "1", "same-call-observations-v1"),
            self,
        )

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def clear(self) -> None:
        with self._lock:
            self._epoch += 1

    def count(self, reason: str) -> None:
        with self._lock:
            self._counts[reason] += 1

    def status(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    @contextmanager
    def scope(self) -> Iterator[PreparedEvaluationScope]:
        scope = PreparedEvaluationScope(self, self.epoch)
        self.count("started")
        try:
            yield scope
        finally:
            if scope.ineligible:
                self.count("ineligible")
            scope.close()
