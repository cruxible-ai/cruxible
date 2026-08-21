"""Daemon-owned AuthoringIntent lifecycle before compilation and submission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cruxible_core.playbill.authoring.models import (
    AcceptanceConditionV1,
    AuthoringIntentListV1,
    AuthoringIntentV1,
    AuthoringIntentViewV1,
    AuthoringPayloadV1,
    AuthoringSubmitResultV1,
    CandidateStatusState,
    CandidateStatusV1,
    ClaimAuthoringPayloadV1,
    PreflightResultV1,
    ProcedureAuthoringPayloadV1,
    authoring_create_fingerprint,
    authoring_payload_digest,
)
from cruxible_core.playbill.authoring.preflight import ComputedPreflight, compute_preflight
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.claims import new_claim_id
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
)


@dataclass(frozen=True)
class AuthoringIntentCoordinator:
    instance: PlaybillInstance
    store: AuthoringIntentStore
    claim_id_factory: Callable[[], str] = new_claim_id

    @classmethod
    def for_instance(cls, instance: PlaybillInstance) -> "AuthoringIntentCoordinator":
        exhaust = instance.root / instance.descriptor.storage.exhaust
        return cls(instance=instance, store=AuthoringIntentStore(exhaust))

    def create(
        self,
        *,
        actor: AuthenticatedActor,
        payload: AuthoringPayloadV1,
        canonical_timestamp: str,
    ) -> AuthoringIntentViewV1:
        at = AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())
        intent_id = self.store.mint_intent_id()
        semantic_identity = self._mint_semantic_identity(payload)
        status = CandidateStatusV1(
            state="draft",
            current_accepted_coordinate=at,
        )
        intent = AuthoringIntentV1(
            intent_id=intent_id,
            instance_id=self.instance.descriptor.instance_id,
            actor_id=actor.actor_id,
            canonical_timestamp=canonical_timestamp,
            base_coordinate=at,
            semantic_identity=semantic_identity,
            payload=payload,
            payload_digest=authoring_payload_digest(payload),
            create_fingerprint=authoring_create_fingerprint(
                instance_id=self.instance.descriptor.instance_id,
                actor_id=actor.actor_id,
                payload=payload,
            ),
            candidate_status=status,
        )
        operation_key = typed_digest(
            Sha256Value,
            "playbill-authoring-create-v1",
            {
                "actor_id": actor.actor_id,
                "create_fingerprint": intent.create_fingerprint,
                "instance_id": intent.instance_id,
            },
        ).tagged
        stored = self.store.create(intent, operation_key=operation_key)
        return AuthoringIntentViewV1(intent=stored)

    def get(self, intent_id: str, *, actor: AuthenticatedActor) -> AuthoringIntentViewV1:
        intent = self.store.get(intent_id, actor_id=actor.actor_id)
        return AuthoringIntentViewV1(
            intent=intent.model_copy(update={"candidate_status": self._reduce_status(intent)})
        )

    def resume(self, intent_id: str, *, actor: AuthenticatedActor) -> AuthoringIntentViewV1:
        return self.get(intent_id, actor=actor)

    def list_pending(self, *, actor: AuthenticatedActor) -> AuthoringIntentListV1:
        reduced = tuple(
            intent.model_copy(update={"candidate_status": self._reduce_status(intent)})
            for intent in self.store.list_pending(actor_id=actor.actor_id)
        )
        return AuthoringIntentListV1(
            intents=tuple(
                intent
                for intent in reduced
                if intent.candidate_status.state not in {"accepted", "superseded", "terminal"}
            )
        )

    def preflight(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
    ) -> PreflightResultV1:
        _computed, updated = self._compute_and_bind_preflight(intent_id, actor=actor)
        if updated.last_preflight is None:  # pragma: no cover - transition invariant
            raise RuntimeError("preflight transition omitted its result")
        return updated.last_preflight

    def _compute_and_bind_preflight(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
    ) -> tuple[ComputedPreflight, AuthoringIntentV1]:
        current = self.store.get(intent_id, actor_id=actor.actor_id)
        computed = compute_preflight(self.instance, intent=current, actor=actor)
        operation_key = computed.result.certificate.certificate_digest

        def bind_preflight(intent: AuthoringIntentV1) -> AuthoringIntentV1:
            if intent.payload_digest != current.payload_digest:
                raise ValueError("AuthoringIntent payload changed during preflight")
            return intent.model_copy(
                update={
                    "last_preflight": computed.result,
                    "candidate_status": computed.status,
                }
            )

        updated = self.store.transition(
            intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=bind_preflight,
        )
        return computed, updated

    def compile(
        self,
        *,
        actor: AuthenticatedActor,
        payload: AuthoringPayloadV1,
        canonical_timestamp: str,
        intent_id: str | None = None,
    ) -> PreflightResultV1:
        view = (
            self.create(
                actor=actor,
                payload=payload,
                canonical_timestamp=canonical_timestamp,
            )
            if intent_id is None
            else self.replace_payload(intent_id, actor=actor, payload=payload)
        )
        return self.preflight(view.intent.intent_id, actor=actor)

    def submit(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
    ) -> AuthoringSubmitResultV1:
        current = self.store.get(intent_id, actor_id=actor.actor_id)
        reduced = self._reduce_status(current)
        if reduced.state == "accepted":
            return AuthoringSubmitResultV1(
                intent=current.model_copy(update={"candidate_status": reduced}),
                status=reduced,
            )
        if current.candidate_status.proposal_id is not None:
            candidate = self.instance.proposal_evidence().read_candidate(
                current.candidate_status.candidate_digest or ""
            )
            if (
                candidate.candidate.parent_semantic_root
                == self.instance.accepted_coordinate().semantic_root
            ):
                return AuthoringSubmitResultV1(
                    intent=current.model_copy(update={"candidate_status": reduced}),
                    status=reduced,
                )

        computed, preflighted = self._compute_and_bind_preflight(intent_id, actor=actor)
        if computed.result.verdict == "refused":
            status = computed.status
            if current.candidate_status.proposal_id is not None:
                status = status.model_copy(update={"state": "conflicted_after_rebase"})
            return AuthoringSubmitResultV1(
                intent=preflighted.model_copy(update={"candidate_status": status}),
                status=status,
            )
        if computed.evaluation is None or computed.evaluation.candidate is None:
            raise RuntimeError("passing preflight omitted its evaluated candidate")

        certificate = computed.result.certificate
        result = self.instance.proposal_service().submit(
            actor=actor,
            request=ProposalAdmissionRequest(
                target_ref=certificate.proposal_ref,
                proposed_base_oid=certificate.accepted_coordinate.git_oid,
            ),
            candidate_tree=computed.evaluated_tree,
            timestamp=current.canonical_timestamp,
        )
        if result.candidate is None:
            latest = AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())
            if latest == certificate.accepted_coordinate:
                raise RuntimeError("submit broke its unchanged-coordinate preflight binding")
            status = CandidateStatusV1(
                state="conflicted_after_rebase",
                current_accepted_coordinate=latest,
                path_to_acceptance=(
                    AcceptanceConditionV1(
                        condition="repreflight_after_concurrent_acceptance",
                        owner="daemon",
                        action="Retry submit; the coordinator will rebase and preflight.",
                        satisfied=False,
                    ),
                ),
            )
            return AuthoringSubmitResultV1(
                intent=preflighted.model_copy(update={"candidate_status": status}),
                status=status,
            )
        if result.candidate.candidate_digest != computed.evaluation.candidate.candidate_digest:
            raise RuntimeError("submit candidate differs from its binding preflight")

        operation_key = typed_digest(
            Sha256Value,
            "playbill-authoring-submit-v1",
            {
                "certificate_digest": certificate.certificate_digest,
                "proposal_id": result.admission.proposal_id,
            },
        ).tagged
        submitted_status = self._candidate_status(
            proposal_id=result.admission.proposal_id,
            candidate_digest=result.candidate.candidate_digest,
        )

        def bind_submit(intent: AuthoringIntentV1) -> AuthoringIntentV1:
            if (
                intent.last_preflight is None
                or intent.last_preflight.certificate.certificate_digest
                != certificate.certificate_digest
            ):
                raise ValueError("AuthoringIntent preflight changed during submit")
            return intent.model_copy(update={"candidate_status": submitted_status})

        submitted = self.store.transition(
            intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=bind_submit,
        )
        return AuthoringSubmitResultV1(
            intent=submitted,
            status=submitted.candidate_status,
        )

    def status(self, intent_id: str, *, actor: AuthenticatedActor) -> CandidateStatusV1:
        intent = self.store.get(intent_id, actor_id=actor.actor_id)
        return self._reduce_status(intent)

    def replace_payload(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
        payload: AuthoringPayloadV1,
    ) -> AuthoringIntentViewV1:
        payload_digest = authoring_payload_digest(payload)
        operation_key = typed_digest(
            Sha256Value,
            "playbill-authoring-replace-payload-v1",
            {
                "actor_id": actor.actor_id,
                "intent_id": intent_id,
                "payload_digest": payload_digest,
            },
        ).tagged

        def replace(current: AuthoringIntentV1) -> AuthoringIntentV1:
            if current.candidate_status.state not in {
                "draft",
                "preflight_refused",
                "ready_to_submit",
            }:
                raise ValueError("submitted AuthoringIntent payload is immutable")
            semantic_identity = current.semantic_identity
            if isinstance(current.payload, ProcedureAuthoringPayloadV1) or isinstance(
                payload, ProcedureAuthoringPayloadV1
            ):
                if not isinstance(current.payload, ProcedureAuthoringPayloadV1) or not isinstance(
                    payload, ProcedureAuthoringPayloadV1
                ):
                    raise ValueError("AuthoringIntent payload kind cannot change")
                semantic_identity = f"Procedure:{payload.definition['name']}"
            elif not isinstance(payload, ClaimAuthoringPayloadV1):  # pragma: no cover
                raise ValueError("unsupported AuthoringIntent payload kind")
            at = AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())
            return current.model_copy(
                update={
                    "payload": payload,
                    "payload_digest": payload_digest,
                    "create_fingerprint": authoring_create_fingerprint(
                        instance_id=current.instance_id,
                        actor_id=current.actor_id,
                        payload=payload,
                    ),
                    "semantic_identity": semantic_identity,
                    "intent_revision": current.intent_revision + 1,
                    "last_preflight": None,
                    "candidate_status": CandidateStatusV1(
                        state="draft",
                        current_accepted_coordinate=at,
                    ),
                }
            )

        updated = self.store.transition(
            intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=replace,
        )
        return AuthoringIntentViewV1(intent=updated)

    def _mint_semantic_identity(self, payload: AuthoringPayloadV1) -> str:
        if isinstance(payload, ClaimAuthoringPayloadV1):
            return payload.claim_ref or self.claim_id_factory()
        return f"Procedure:{payload.definition['name']}"

    def _reduce_status(self, intent: AuthoringIntentV1) -> CandidateStatusV1:
        status = intent.candidate_status
        if status.proposal_id is None or status.candidate_digest is None:
            return status.model_copy(
                update={
                    "current_accepted_coordinate": AcceptedCoordinate.from_internal(
                        self.instance.accepted_coordinate()
                    )
                }
            )
        for generation in self.instance.accepted_history():
            if generation.record is not None and (
                generation.record.candidate_digest == status.candidate_digest
            ):
                accepted = self.instance.coordinate_for_oid(generation.oid)
                return CandidateStatusV1(
                    state="accepted",
                    proposal_id=status.proposal_id,
                    candidate_digest=status.candidate_digest,
                    current_accepted_coordinate=AcceptedCoordinate.from_internal(
                        self.instance.accepted_coordinate()
                    ),
                    accepted_generation=AcceptedCoordinate.from_internal(accepted),
                )
        candidate = self.instance.proposal_evidence().read_candidate(status.candidate_digest)
        if (
            candidate.candidate.parent_semantic_root
            != self.instance.accepted_coordinate().semantic_root
        ):
            approvals = self.instance.proposal_evidence().read_approvals(status.candidate_digest)
            state: CandidateStatusState = (
                "approval_invalid" if approvals else "conflicted_after_rebase"
            )
            return CandidateStatusV1(
                state=state,
                proposal_id=status.proposal_id,
                candidate_digest=status.candidate_digest,
                current_accepted_coordinate=AcceptedCoordinate.from_internal(
                    self.instance.accepted_coordinate()
                ),
                path_to_acceptance=(
                    AcceptanceConditionV1(
                        condition="candidate_rebase",
                        owner="daemon",
                        action="Retry submit to preflight and rebase the unchanged authoring.",
                        satisfied=False,
                    ),
                ),
            )
        return self._candidate_status(
            proposal_id=status.proposal_id,
            candidate_digest=status.candidate_digest,
        )

    def _candidate_status(
        self,
        *,
        proposal_id: str,
        candidate_digest: str,
    ) -> CandidateStatusV1:
        evidence = self.instance.proposal_evidence()
        candidate = evidence.read_candidate(candidate_digest)
        approvals = evidence.read_approvals(candidate_digest)
        generation = self.instance.generation_for_semantic_root(
            candidate.candidate.parent_semantic_root
        )
        signer_roles = {
            submission.attestation.signer_id: set(
                generation.principals.require_active(
                    submission.attestation.signer_id
                ).authority_roles
            )
            for submission in approvals
        }
        conditions: list[AcceptanceConditionV1] = []
        approvals_complete = True
        for requirement in candidate.approval_requirements:
            count = sum(requirement.role in roles for roles in signer_roles.values())
            satisfied = count >= requirement.minimum_distinct_signers
            approvals_complete = approvals_complete and satisfied
            conditions.append(
                AcceptanceConditionV1(
                    condition=(
                        f"approval:{requirement.role}:{requirement.minimum_distinct_signers}"
                    ),
                    owner="approver",
                    action=(
                        "Wait for independently submitted approval attestations "
                        f"from {requirement.minimum_distinct_signers} distinct "
                        f"{requirement.role} signer(s)."
                    ),
                    satisfied=satisfied,
                )
            )
        conditions.append(
            AcceptanceConditionV1(
                condition="activation",
                owner="daemon",
                action=(
                    "Activate the fully approved candidate through the existing settlement path."
                ),
                satisfied=False,
            )
        )
        return CandidateStatusV1(
            state=("ready_to_activate" if approvals_complete else "awaiting_external_approval"),
            proposal_id=proposal_id,
            candidate_digest=candidate_digest,
            current_accepted_coordinate=AcceptedCoordinate.from_internal(
                self.instance.accepted_coordinate()
            ),
            path_to_acceptance=tuple(conditions),
        )


__all__ = ["AuthoringIntentCoordinator"]
