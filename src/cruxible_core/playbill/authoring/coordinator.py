"""Daemon-owned AuthoringIntent lifecycle before compilation and submission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cruxible_core.playbill.authoring.models import (
    AuthoringIntentListV1,
    AuthoringIntentV1,
    AuthoringIntentViewV1,
    AuthoringPayloadV1,
    CandidateStatusV1,
    ClaimAuthoringPayloadV1,
    ProcedureAuthoringPayloadV1,
    authoring_create_fingerprint,
    authoring_payload_digest,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.claims import new_claim_id
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor


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
        return AuthoringIntentViewV1(intent=self.store.get(intent_id, actor_id=actor.actor_id))

    def resume(self, intent_id: str, *, actor: AuthenticatedActor) -> AuthoringIntentViewV1:
        return self.get(intent_id, actor=actor)

    def list_pending(self, *, actor: AuthenticatedActor) -> AuthoringIntentListV1:
        return AuthoringIntentListV1(intents=self.store.list_pending(actor_id=actor.actor_id))

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


__all__ = ["AuthoringIntentCoordinator"]
