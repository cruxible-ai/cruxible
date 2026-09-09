"""Instance-owned, single-submit handoff of a pure proposal evaluation.

No token crosses a transport, survives its call scope, or authorizes a write.
Submission still validates ingress and current writable/actor/base/ref bindings.
CAS observations are freshly replayed; operational query/receipt/promotion reads
and CAS writes make an evaluation ineligible until they have revision bindings.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from cruxible_client.contracts.canonical import canonical_bytes, is_candidate_card_path
from cruxible_client.contracts.captures import CaptureObjectStoreProtocol
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.documents import BodyVerifierProtocol
from cruxible_client.contracts.proposal_models import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalReceiveLimits,
)
from cruxible_core.playbill.derived_state import DerivedState, IndexDefinition, SnapshotTree
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate

if TYPE_CHECKING:
    from cruxible_core.playbill.proposals import CandidateEvaluation


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

    def store(self, content: bytes):  # type: ignore[no-untyped-def]
        self.scope.ineligible = True
        return self.source.store(content)


class _OperationalVerifier:
    def __init__(self, source: Any, scope: PreparedEvaluationScope) -> None:
        self.source, self.scope = source, scope

    def verify_promotion(self, promotion):  # type: ignore[no-untyped-def]
        self.scope.ineligible = True
        return self.source.verify_promotion(promotion)


class PreparedEvaluationScope:
    """Private capability held by exactly one synchronous coordinator submit."""

    def __init__(self, owner: PreparedEvaluationAdapter, epoch: int) -> None:
        self.owner, self.epoch = owner, epoch
        self.closed = False
        self.consumed = False
        self.ineligible = False
        self._observations: dict[tuple[str, str, bytes | None], _BodyObservation] = {}
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
        from cruxible_core.playbill.proposals import CandidateEvaluation

        # Only immutable trees and detached result models are retained. Derived
        # evaluation state is not consumed by ProposalService and is not copied.
        tree = (
            outcome.tree if isinstance(outcome.tree, SnapshotTree) else SnapshotTree(outcome.tree)
        )
        submission = tree.fork()
        for path in tree:
            if is_candidate_card_path(path):
                del submission[path]
        self.submission_tree = submission.snapshot()
        self._outcome = CandidateEvaluation(
            tree,
            copy.deepcopy(outcome.candidate),
            copy.deepcopy(outcome.diagnostics),
            False,
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
        from cruxible_core.playbill.proposals import CandidateEvaluation

        value = self._outcome
        return CandidateEvaluation(
            value.tree,
            copy.deepcopy(value.candidate),
            copy.deepcopy(value.diagnostics),
            False,
            claim_admission_accounts=copy.deepcopy(value.claim_admission_accounts),
        )

    def close(self) -> None:
        self.closed = True
        self._observations.clear()
        self._outcome = None
        self.submission_tree = None


class PreparedEvaluationAdapter:
    """Lifecycle owner for call-local handoffs, not a persistent result cache."""

    def __init__(
        self,
        owner: DerivedState,
        *,
        max_bytes: int = 32 * 1024 * 1024,
        max_observations: int = 16384,
    ) -> None:
        self._lock = threading.Lock()
        self._epoch = 0
        self._counts = {"started": 0, "reused": 0, "invalidated": 0, "ineligible": 0}
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
