"""Daemon-owned AuthoringIntent lifecycle before compilation and submission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Literal

from cruxible_client.contracts.artifacts import ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.authoring.inputs import AuthoringInputV1, lower_authoring_input
from cruxible_client.contracts.authoring.models import (
    AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    AUTHORING_SDK_VERSION,
    AcceptanceConditionV1,
    AuthoringIntentListV1,
    AuthoringIntentV1,
    AuthoringIntentV2,
    AuthoringIntentViewV1,
    AuthoringPayloadV1,
    AuthoringProgramStampV1,
    AuthoringReferenceExpectationV1,
    AuthoringSubmitResultV1,
    CandidateStatusState,
    CandidateStatusV1,
    ClaimAuthoringPayloadV1,
    InsertionAbandonResultV1,
    InsertionConfirmationObservationV1,
    InsertionConfirmResultV1,
    InsertionExpectationV1,
    PreflightResultV1,
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
    SelfSourceBodyV1,
    authoring_create_fingerprint,
    authoring_payload_digest,
    insertion_confirmation_operation_key,
    reference_expectations_digest,
    update_insertion_expectation,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import (
    build_working_selection_capture,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV2,
    ClaimBackingV2,
    build_claim_citation,
    claim_artifact_digest,
    claim_path,
    claim_statement_address,
    claim_statement_digest,
    merge_claim_citations,
    new_claim_id,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.semantic import ContentSpan, SourceMapping
from cruxible_client.contracts.temporal import format_datetime, parse_datetime, utc_now
from cruxible_core.playbill.authoring.insertions import (
    InsertionProtocolError,
    mark_abandoned,
    mark_bound,
    mark_claim_accepted,
    mark_claim_currency_changed,
    mark_confirming,
    mark_expired,
    mint_insertion_expectation,
)
from cruxible_core.playbill.authoring.preflight import ComputedPreflight, compute_preflight
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
)

AUTHORING_REBASE_DOMAIN = "playbill-authoring-rebase-v1"


class AuthoringIntentRebaseError(PlaybillError):
    code = "playbill.authoring.intent_rebase_not_allowed"


class AuthoringIntentRebaseSubmitted(AuthoringIntentRebaseError):
    code = "playbill.authoring.intent_rebase_submitted"


class AuthoringProgramStampError(PlaybillError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _validate_program_stamp(program_stamp: AuthoringProgramStampV1) -> None:
    if program_stamp.sdk_contract_snapshot_digest != AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST:
        raise AuthoringProgramStampError(
            "playbill.authoring.program_stamp_contract_mismatch",
            "the SDK contract snapshot is not the daemon's exact frozen snapshot",
        )
    if program_stamp.sdk_version != AUTHORING_SDK_VERSION:
        raise AuthoringProgramStampError(
            "playbill.authoring.program_stamp_version_incompatible",
            "the SDK version is not compatible with this daemon",
        )


def _rebase_operation_key(
    intent: AuthoringIntentV1,
    *,
    actor_id: str,
    next_coordinate: AcceptedCoordinate,
) -> str:
    return typed_digest(
        Sha256Value,
        AUTHORING_REBASE_DOMAIN,
        {
            "intent_id": intent.intent_id,
            "actor_id": actor_id,
            "prior_base_coordinate": intent.base_coordinate.model_dump(mode="json"),
            "next_base_coordinate": next_coordinate.model_dump(mode="json"),
        },
    ).tagged


@dataclass(frozen=True)
class AuthoringIntentCoordinator:
    instance: PlaybillInstance
    store: AuthoringIntentStore
    claim_id_factory: Callable[[], str] = new_claim_id
    clock: Callable[[], datetime] = utc_now

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
        base_coordinate: AcceptedCoordinate | None = None,
        reference_expectations: tuple[AuthoringReferenceExpectationV1, ...] | None = None,
        program_stamp: AuthoringProgramStampV1 | None = None,
    ) -> AuthoringIntentViewV1:
        if program_stamp is not None:
            _validate_program_stamp(program_stamp)
            if reference_expectations is None:
                raise AuthoringProgramStampError(
                    "playbill.authoring.program_stamp_contract_mismatch",
                    "a v3 program stamp requires the v2 reference-assertion envelope",
                )
        at = base_coordinate or AcceptedCoordinate.from_internal(
            self.instance.accepted_coordinate()
        )
        intent_id = self.store.mint_intent_id()
        semantic_identity = self._mint_semantic_identity(payload)
        status = CandidateStatusV1(
            state="draft",
            current_accepted_coordinate=at,
        )
        intent_values = {
            "intent_id": intent_id,
            "instance_id": self.instance.descriptor.instance_id,
            "actor_id": actor.actor_id,
            "canonical_timestamp": canonical_timestamp,
            "base_coordinate": at,
            "semantic_identity": semantic_identity,
            "payload": payload,
            "payload_digest": authoring_payload_digest(payload),
            "create_fingerprint": authoring_create_fingerprint(
                instance_id=self.instance.descriptor.instance_id,
                actor_id=actor.actor_id,
                payload=payload,
            ),
            "candidate_status": status,
        }
        intent = (
            AuthoringIntentV1.model_validate(intent_values)
            if reference_expectations is None
            else AuthoringIntentV2.model_validate(
                {
                    **intent_values,
                    "reference_expectations": reference_expectations,
                }
            )
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
        if reference_expectations is not None and (
            not isinstance(stored, AuthoringIntentV2)
            or stored.reference_expectations != reference_expectations
        ):
            stored = self._replace_reference_expectations(
                stored,
                actor=actor,
                reference_expectations=reference_expectations,
            )
        if program_stamp is not None:
            stored = self.store.record_program_stamp(
                stored.intent_id,
                actor_id=actor.actor_id,
                program_stamp=program_stamp,
            )
        return AuthoringIntentViewV1(intent=stored)

    def create_input(
        self,
        *,
        actor: AuthenticatedActor,
        input: AuthoringInputV1,
        canonical_timestamp: str,
    ) -> AuthoringIntentViewV1:
        """Atomically bind friendly IDs to one accepted base, then persist the intent."""

        base = self.instance.accepted_coordinate()
        coordinate = AcceptedCoordinate.from_internal(base)
        payload = lower_authoring_input(input, tree=self.instance.tree_at(base.git_oid))
        return self.create(
            actor=actor,
            payload=payload,
            canonical_timestamp=canonical_timestamp,
            base_coordinate=coordinate,
        )

    def get(self, intent_id: str, *, actor: AuthenticatedActor) -> AuthoringIntentViewV1:
        intent = self._refresh_protocol(
            self.store.get(intent_id, actor_id=actor.actor_id),
            actor=actor,
        )
        return AuthoringIntentViewV1(intent=intent)

    def resume(self, intent_id: str, *, actor: AuthenticatedActor) -> AuthoringIntentViewV1:
        return self.get(intent_id, actor=actor)

    def list_pending(self, *, actor: AuthenticatedActor) -> AuthoringIntentListV1:
        reduced = tuple(
            self._refresh_protocol(intent, actor=actor)
            for intent in self.store.list_pending(actor_id=actor.actor_id)
        )
        return AuthoringIntentListV1(
            intents=tuple(
                intent
                for intent in reduced
                if (
                    intent.candidate_status.state not in {"accepted", "superseded", "terminal"}
                    or (
                        intent.insertion_expectation is not None
                        and intent.insertion_expectation.state
                        in {"awaiting_claim_acceptance", "pending", "confirming"}
                    )
                )
            )
        )

    def rebase(self, intent_id: str, *, actor: AuthenticatedActor) -> AuthoringIntentViewV1:
        """Advance one refused, unsubmitted intent to the current accepted coordinate."""

        current = self.store.get(intent_id, actor_id=actor.actor_id)
        status = current.candidate_status
        next_coordinate = AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())
        if status.state == "draft" and current.base_coordinate == next_coordinate:
            predecessor, latest = self.store.latest_transition(
                intent_id,
                actor_id=actor.actor_id,
            )
            if (
                predecessor is not None
                and predecessor.candidate_status.state == "preflight_refused"
                and latest.operation_key
                == _rebase_operation_key(
                    predecessor,
                    actor_id=actor.actor_id,
                    next_coordinate=next_coordinate,
                )
            ):
                return AuthoringIntentViewV1(intent=latest.intent)
        if status.proposal_id is not None or status.state in {
            "awaiting_external_approval",
            "approval_invalid",
            "ready_to_activate",
            "conflicted_after_rebase",
            "superseded",
            "accepted",
            "terminal",
        }:
            raise AuthoringIntentRebaseSubmitted(
                f"{AuthoringIntentRebaseSubmitted.code}: submitted intent cannot be rebased"
            )
        if status.state != "preflight_refused":
            raise AuthoringIntentRebaseError(
                f"{AuthoringIntentRebaseError.code}: only preflight_refused may advance"
            )
        if current.base_coordinate == next_coordinate:
            return AuthoringIntentViewV1(intent=current)
        operation_key = _rebase_operation_key(
            current,
            actor_id=actor.actor_id,
            next_coordinate=next_coordinate,
        )

        def advance(intent: AuthoringIntentV1) -> AuthoringIntentV1:
            if intent != current:
                raise AuthoringIntentRebaseError(
                    f"{AuthoringIntentRebaseError.code}: intent changed during rebase"
                )
            return intent.model_copy(
                update={
                    "base_coordinate": next_coordinate,
                    "intent_revision": intent.intent_revision + 1,
                    "last_preflight": None,
                    "candidate_status": CandidateStatusV1(
                        state="draft",
                        current_accepted_coordinate=next_coordinate,
                    ),
                }
            )

        updated = self.store.transition(
            intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=advance,
            allow_rebase=True,
        )
        return AuthoringIntentViewV1(intent=updated)

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
        reference_expectations: tuple[AuthoringReferenceExpectationV1, ...] | None = None,
        program_stamp: AuthoringProgramStampV1 | None = None,
    ) -> PreflightResultV1:
        view = (
            self.create(
                actor=actor,
                payload=payload,
                canonical_timestamp=canonical_timestamp,
                reference_expectations=reference_expectations,
                program_stamp=program_stamp,
            )
            if intent_id is None
            else self.replace_payload(
                intent_id,
                actor=actor,
                payload=payload,
                reference_expectations=reference_expectations,
                program_stamp=program_stamp,
            )
        )
        return self.preflight(view.intent.intent_id, actor=actor)

    def compile_input(
        self,
        *,
        actor: AuthenticatedActor,
        input: AuthoringInputV1,
        canonical_timestamp: str,
        intent_id: str | None = None,
    ) -> PreflightResultV1:
        if intent_id is None:
            view = self.create_input(
                actor=actor,
                input=input,
                canonical_timestamp=canonical_timestamp,
            )
        else:
            current = self.store.get(intent_id, actor_id=actor.actor_id)
            payload = lower_authoring_input(
                input,
                tree=self.instance.tree_at(current.base_coordinate.git_oid),
            )
            view = self.replace_payload(intent_id, actor=actor, payload=payload)
        return self.preflight(view.intent.intent_id, actor=actor)

    def submit(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
    ) -> AuthoringSubmitResultV1:
        current = self._refresh_protocol(
            self.store.get(intent_id, actor_id=actor.actor_id),
            actor=actor,
        )
        reduced = current.candidate_status
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
        insertion_expectation = preflighted.insertion_expectation
        payload = preflighted.payload
        if (
            isinstance(payload, ClaimAuthoringPayloadV1)
            and payload.insertion_target is not None
            and insertion_expectation is None
        ):
            if computed.lowered is None:  # pragma: no cover - passing preflight invariant
                raise RuntimeError("passing insertion preflight omitted its lowered Claim")
            artifact_digest = computed.lowered.resolved_authoring.get("artifact_digest")
            lowered_path = claim_path(preflighted.semantic_identity)
            lowered_claim = parse_claim(
                computed.lowered.proposed_tree[lowered_path],
                path=lowered_path,
            )
            if not isinstance(artifact_digest, str):
                raise RuntimeError("lowered insertion Claim omitted its frozen identities")
            statement_digest = claim_statement_digest(lowered_claim.statement).tagged
            created_at = parse_datetime(preflighted.canonical_timestamp)
            if created_at is None:  # pragma: no cover - validated timestamp
                raise RuntimeError("AuthoringIntent timestamp did not parse")
            insertion_expectation = mint_insertion_expectation(
                preflighted,
                original_claim_artifact_digest=artifact_digest,
                claim_statement_digest=statement_digest,
                expires_at=created_at + timedelta(days=7),
            )

        def bind_submit(intent: AuthoringIntentV1) -> AuthoringIntentV1:
            if (
                intent.last_preflight is None
                or intent.last_preflight.certificate.certificate_digest
                != certificate.certificate_digest
            ):
                raise ValueError("AuthoringIntent preflight changed during submit")
            return intent.model_copy(
                update={
                    "candidate_status": submitted_status,
                    "insertion_expectation": insertion_expectation,
                }
            )

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
        intent = self._refresh_protocol(
            self.store.get(intent_id, actor_id=actor.actor_id),
            actor=actor,
        )
        return intent.candidate_status

    def confirm_insertion(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
        observation: InsertionConfirmationObservationV1,
    ) -> InsertionConfirmResultV1:
        before = self.store.get(intent_id, actor_id=actor.actor_id)
        was_bound = (
            before.insertion_expectation is not None
            and before.insertion_expectation.state == "bound"
        )
        current = self._refresh_protocol(before, actor=actor)
        expectation = current.insertion_expectation
        if expectation is None:
            raise InsertionProtocolError("AuthoringIntent has no insertion expectation")
        if expectation.state == "bound":
            return InsertionConfirmResultV1(
                outcome="already_bound" if was_bound else "bound",
                intent=current,
                expectation=expectation,
            )
        if expectation.state == "expired":
            return InsertionConfirmResultV1(
                outcome="expired",
                intent=current,
                expectation=expectation,
            )
        if expectation.state == "claim_currency_changed":
            return InsertionConfirmResultV1(
                outcome="claim_currency_changed",
                intent=current,
                expectation=expectation,
            )
        if expectation.state == "abandoned":
            raise InsertionProtocolError("abandoned insertion expectation cannot be confirmed")
        if expectation.state == "awaiting_claim_acceptance":
            raise InsertionProtocolError("Claim must be accepted before insertion confirmation")

        now = self.clock()
        if expectation.state == "pending" and now >= expectation.patch.expires_at:
            expired = self._transition_expired(current, actor=actor, evaluation_time=now)
            assert expired.insertion_expectation is not None
            return InsertionConfirmResultV1(
                outcome="expired",
                intent=expired,
                expectation=expired.insertion_expectation,
            )
        correspondence = self._confirmation_correspondence(expectation, observation)
        if correspondence is not None:
            return InsertionConfirmResultV1(
                outcome=correspondence,
                intent=current,
                expectation=expectation,
            )

        if expectation.state == "pending":
            operation_key = insertion_confirmation_operation_key(
                expectation.expectation_id,
                observation,
            )

            def begin_confirmation(intent: AuthoringIntentV1) -> AuthoringIntentV1:
                live = intent.insertion_expectation
                if live is None or live.state != "pending":
                    return intent
                next_expectation, _status = self._submit_insertion_successor(
                    intent,
                    live,
                    actor=actor,
                    observation=observation,
                )
                return intent.model_copy(update={"insertion_expectation": next_expectation})

            current = self.store.transition(
                intent_id,
                actor_id=actor.actor_id,
                operation_key=operation_key,
                transform=begin_confirmation,
            )
            expectation = current.insertion_expectation
            if expectation is None:  # pragma: no cover - transition invariant
                raise RuntimeError("confirmation transition lost its insertion expectation")

        current = self._refresh_protocol(current, actor=actor)
        expectation = current.insertion_expectation
        assert expectation is not None
        if expectation.state == "bound":
            return InsertionConfirmResultV1(
                outcome="bound",
                intent=current,
                expectation=expectation,
            )
        status = self._insertion_candidate_status(expectation)
        if status.state in {"approval_invalid", "conflicted_after_rebase"}:
            current = self._rebase_insertion_successor(
                current,
                actor=actor,
                observation=observation,
            )
            expectation = current.insertion_expectation
            assert expectation is not None
            status = self._insertion_candidate_status(expectation)
        refused = expectation.successor_candidate_digest is None
        return InsertionConfirmResultV1(
            outcome=("backing_candidate_refused" if refused else "backing_candidate_pending"),
            intent=current,
            expectation=expectation,
            successor_status=status,
        )

    def abandon_insertion(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
    ) -> InsertionAbandonResultV1:
        current = self._refresh_protocol(
            self.store.get(intent_id, actor_id=actor.actor_id),
            actor=actor,
        )
        expectation = current.insertion_expectation
        if expectation is None:
            raise InsertionProtocolError("AuthoringIntent has no insertion expectation")
        operation_key = typed_digest(
            Sha256Value,
            "playbill-insertion-abandon-v1",
            {
                "expectation_id": expectation.expectation_id,
                "intent_id": intent_id,
            },
        ).tagged

        def abandon(intent: AuthoringIntentV1) -> AuthoringIntentV1:
            live = intent.insertion_expectation
            if live is None:
                raise InsertionProtocolError("AuthoringIntent has no insertion expectation")
            abandoned = mark_abandoned(
                intent,
                live,
                finalized_at=self.clock(),
            )
            return intent.model_copy(update={"insertion_expectation": abandoned})

        updated = self.store.transition(
            intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=abandon,
        )
        assert updated.insertion_expectation is not None
        return InsertionAbandonResultV1(
            intent=updated,
            expectation=updated.insertion_expectation,
        )

    def replace_payload(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
        payload: AuthoringPayloadV1,
        reference_expectations: tuple[AuthoringReferenceExpectationV1, ...] | None = None,
        program_stamp: AuthoringProgramStampV1 | None = None,
    ) -> AuthoringIntentViewV1:
        if program_stamp is not None:
            _validate_program_stamp(program_stamp)
            if reference_expectations is None:
                raise AuthoringProgramStampError(
                    "playbill.authoring.program_stamp_contract_mismatch",
                    "a v3 program stamp requires the v2 reference-assertion envelope",
                )
        payload_digest = authoring_payload_digest(payload)
        expectations_digest = (
            None
            if reference_expectations is None
            else reference_expectations_digest(reference_expectations)
        )
        operation_key = typed_digest(
            Sha256Value,
            (
                "playbill-authoring-replace-payload-v1"
                if expectations_digest is None
                else "playbill-authoring-replace-payload-v2"
            ),
            {
                "actor_id": actor.actor_id,
                "intent_id": intent_id,
                "payload_digest": payload_digest,
                **(
                    {}
                    if expectations_digest is None
                    else {"reference_expectations_digest": expectations_digest}
                ),
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
            if isinstance(
                current.payload,
                ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2,
            ) or isinstance(
                payload,
                ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2,
            ):
                if not isinstance(
                    current.payload,
                    ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2,
                ) or not isinstance(
                    payload,
                    ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2,
                ):
                    raise ValueError("AuthoringIntent payload kind cannot change")
                semantic_identity = f"Procedure:{payload.definition['name']}"
            elif not isinstance(payload, ClaimAuthoringPayloadV1):  # pragma: no cover
                raise ValueError("unsupported AuthoringIntent payload kind")
            at = AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())
            updates = {
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
            if reference_expectations is None:
                return current.model_copy(update=updates)
            return AuthoringIntentV2.model_validate(
                {
                    **current.model_dump(mode="json"),
                    **updates,
                    "tag": "playbill-authoring-intent-v2",
                    "reference_expectations": [
                        item.model_dump(mode="json") for item in reference_expectations
                    ],
                }
            )

        updated = self.store.transition(
            intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=replace,
        )
        if program_stamp is not None:
            updated = self.store.record_program_stamp(
                updated.intent_id,
                actor_id=actor.actor_id,
                program_stamp=program_stamp,
            )
        return AuthoringIntentViewV1(intent=updated)

    def _replace_reference_expectations(
        self,
        current: AuthoringIntentV1,
        *,
        actor: AuthenticatedActor,
        reference_expectations: tuple[AuthoringReferenceExpectationV1, ...],
    ) -> AuthoringIntentV1:
        if current.candidate_status.state not in {
            "draft",
            "preflight_refused",
            "ready_to_submit",
        }:
            return current
        expectations_digest = reference_expectations_digest(reference_expectations)
        operation_key = typed_digest(
            Sha256Value,
            "playbill-authoring-replace-reference-expectations-v1",
            {
                "actor_id": actor.actor_id,
                "intent_id": current.intent_id,
                "reference_expectations_digest": expectations_digest,
            },
        ).tagged

        def replace(intent: AuthoringIntentV1) -> AuthoringIntentV1:
            if isinstance(intent, AuthoringIntentV2) and (
                intent.reference_expectations == reference_expectations
            ):
                return intent
            return AuthoringIntentV2.model_validate(
                {
                    **intent.model_dump(mode="json"),
                    "tag": "playbill-authoring-intent-v2",
                    "reference_expectations": [
                        item.model_dump(mode="json") for item in reference_expectations
                    ],
                    "intent_revision": intent.intent_revision + 1,
                    "last_preflight": None,
                    "candidate_status": CandidateStatusV1(
                        state="draft",
                        current_accepted_coordinate=AcceptedCoordinate.from_internal(
                            self.instance.accepted_coordinate()
                        ),
                    ).model_dump(mode="json"),
                }
            )

        return self.store.transition(
            current.intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=replace,
        )

    def _mint_semantic_identity(self, payload: AuthoringPayloadV1) -> str:
        if isinstance(payload, ClaimAuthoringPayloadV1):
            return payload.claim_ref or self.claim_id_factory()
        return f"Procedure:{payload.definition['name']}"

    @staticmethod
    def _confirmation_correspondence(
        expectation: InsertionExpectationV1,
        observation: InsertionConfirmationObservationV1,
    ) -> Literal["ambiguous", "stale_target"] | None:
        if observation.observed_occurrence_count != 1:
            return "ambiguous"
        patch = expectation.patch
        if (
            observation.expectation_id != expectation.expectation_id
            or observation.source_id != patch.source_id
            or observation.observed_content_digest != patch.postimage_digest
            or observation.coordinate.source_byte_length != patch.postimage_byte_length
            or observation.selected_end_byte - observation.selected_start_byte
            != patch.body_byte_length
            or observation.selected_bytes_digest != patch.body_digest
        ):
            return "stale_target"
        return None

    def _transition_expired(
        self,
        intent: AuthoringIntentV1,
        *,
        actor: AuthenticatedActor,
        evaluation_time: datetime,
    ) -> AuthoringIntentV1:
        expectation = intent.insertion_expectation
        if expectation is None:
            raise InsertionProtocolError("AuthoringIntent has no insertion expectation")
        operation_key = typed_digest(
            Sha256Value,
            "playbill-insertion-expire-v1",
            {
                "evaluation_time": format_datetime(evaluation_time),
                "expectation_id": expectation.expectation_id,
            },
        ).tagged

        def expire(current: AuthoringIntentV1) -> AuthoringIntentV1:
            live = current.insertion_expectation
            if live is None:
                raise InsertionProtocolError("AuthoringIntent has no insertion expectation")
            expired = mark_expired(current, live, evaluation_time=evaluation_time)
            return current.model_copy(update={"insertion_expectation": expired})

        return self.store.transition(
            intent.intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=expire,
        )

    def _successor_timestamp(self, intent: AuthoringIntentV1) -> str:
        parent_time = self._protocol_time(intent)
        return canonical_candidate_timestamp(parent_time + timedelta(microseconds=1))

    @staticmethod
    def _merged_pins(
        current: tuple[ArtifactPin, ...],
        added: ArtifactPin,
    ) -> tuple[ArtifactPin, ...]:
        by_key = {(item.role, item.target.qualified): item for item in (*current, added)}
        return tuple(
            by_key[key]
            for key in sorted(
                by_key,
                key=lambda item: (item[0].encode("utf-8"), item[1].encode("utf-8")),
            )
        )

    @staticmethod
    def _merged_mappings(
        current: tuple[SourceMapping, ...],
        added: SourceMapping,
    ) -> tuple[SourceMapping, ...]:
        by_wire = {
            canonical_bytes(item.model_dump(mode="json")): item for item in (*current, added)
        }
        return tuple(by_wire[key] for key in sorted(by_wire))

    def _submit_insertion_successor(
        self,
        intent: AuthoringIntentV1,
        expectation: InsertionExpectationV1,
        *,
        actor: AuthenticatedActor,
        observation: InsertionConfirmationObservationV1,
    ) -> tuple[InsertionExpectationV1, CandidateStatusV1]:
        payload = intent.payload
        if not isinstance(payload, ClaimAuthoringPayloadV1):
            raise InsertionProtocolError("insertion successor requires a Claim intent")
        if not isinstance(payload.source, SelfSourceBodyV1):
            raise InsertionProtocolError("insertion successor requires a self-source body")
        current_claim = self._current_claim(intent)
        if current_claim is None or current_claim.lifecycle.state != "live":
            raise InsertionProtocolError("accepted Claim lineage is unavailable")
        if (
            claim_statement_digest(current_claim.statement).tagged
            != expectation.claim_statement_digest
        ):
            raise InsertionProtocolError("Claim currency changed before confirmation")
        body = payload.source.content
        observed_at = parse_datetime(intent.canonical_timestamp)
        if observed_at is None:  # pragma: no cover - timestamp invariant
            raise RuntimeError("AuthoringIntent timestamp did not parse")
        accepted = self.instance.accepted_coordinate()
        public_accepted = AcceptedCoordinate.from_internal(accepted)
        built = build_working_selection_capture(
            store=self.instance.body_store(),
            actor_id=actor.actor_id,
            claim_id=intent.semantic_identity,
            rationale=payload.rationale,
            observed_at=observed_at,
            accepted_coordinate=public_accepted,
            source_id=observation.source_id,
            coordinate={
                **observation.coordinate.model_dump(mode="json"),
                "observed_content_digest": observation.observed_content_digest,
            },
            selector={
                "expectation_id": observation.expectation_id,
                "observed_occurrence_count": observation.observed_occurrence_count,
                "selected_end_byte": observation.selected_end_byte,
                "selected_start_byte": observation.selected_start_byte,
            },
            selected_content=body,
        )
        citation = build_claim_citation(
            current_claim.identity,
            capture_digest=built.capture_digest,
            role="copy",
            origin="self_published",
        )
        prior_citations = (
            current_claim.backing.citations if isinstance(current_claim, ClaimArtifactV2) else ()
        )
        source_mapping = SourceMapping(
            subject=claim_statement_address(claim_path(intent.semantic_identity)),
            spans=(
                ContentSpan(
                    content_digest=built.source_body_digest,
                    start_byte=0,
                    end_byte=len(body),
                ),
            ),
        )
        successor = ClaimArtifactV2(
            identity=current_claim.identity,
            statement=current_claim.statement,
            backing=ClaimBackingV2(
                referent_context=current_claim.backing.referent_context,
                capture_digests=tuple(
                    sorted(
                        {*current_claim.backing.capture_digests, built.capture_digest},
                        key=lambda item: item.encode("ascii"),
                    )
                ),
                citations=merge_claim_citations(prior_citations, (citation,)),
                attestation_digests=current_claim.backing.attestation_digests,
                input_claim_digests=current_claim.backing.input_claim_digests,
                reducer_digest=current_claim.backing.reducer_digest,
                source_mappings=self._merged_mappings(
                    current_claim.backing.source_mappings,
                    source_mapping,
                ),
            ),
            authority=current_claim.authority,
            pins=self._merged_pins(
                current_claim.pins,
                ArtifactPin(
                    role="capture-contract",
                    target=built.contract.identity,
                    artifact_digest=capture_contract_digest(built.contract).tagged,
                ),
            ),
            lifecycle=ArtifactLifecycle(
                predecessor_digest=claim_artifact_digest(current_claim).tagged
            ),
        )
        path = claim_path(intent.semantic_identity)
        tree = self.instance.tree_at(accepted.git_oid)
        tree[path] = render_claim(successor)
        tree[capture_contract_path(built.contract.identity.name)] = render_capture_contract(
            built.contract
        )
        candidate_ref = f"refs/proposals/{actor.actor_id}/intent-{intent.intent_id[4:]}-publication"
        result = self.instance.proposal_service().submit(
            actor=actor,
            request=ProposalAdmissionRequest(
                target_ref=candidate_ref,
                proposed_base_oid=accepted.git_oid,
            ),
            candidate_tree=tree,
            timestamp=self._successor_timestamp(intent),
        )
        candidate_digest = None if result.candidate is None else result.candidate.candidate_digest
        if expectation.state == "pending":
            updated = mark_confirming(
                expectation,
                observation=observation,
                citation_id=citation.citation_id,
                proposal_id=result.admission.proposal_id,
                candidate_ref=candidate_ref,
                candidate_digest=candidate_digest,
            )
        else:
            updated = update_insertion_expectation(
                expectation,
                confirmation_observation=observation,
                citation_id=citation.citation_id,
                successor_proposal_id=result.admission.proposal_id,
                successor_candidate_ref=candidate_ref,
                successor_candidate_digest=candidate_digest,
            )
        return updated, self._insertion_candidate_status(updated)

    def _insertion_candidate_status(
        self,
        expectation: InsertionExpectationV1,
    ) -> CandidateStatusV1:
        if expectation.successor_proposal_id is None:
            raise InsertionProtocolError("confirming insertion omitted its proposal")
        if expectation.successor_candidate_digest is not None:
            candidate = self.instance.proposal_evidence().read_candidate(
                expectation.successor_candidate_digest
            )
            if (
                candidate.candidate.parent_semantic_root
                != self.instance.accepted_coordinate().semantic_root
            ):
                approvals = self.instance.proposal_evidence().read_approvals(
                    expectation.successor_candidate_digest
                )
                return CandidateStatusV1(
                    state="approval_invalid" if approvals else "conflicted_after_rebase",
                    proposal_id=expectation.successor_proposal_id,
                    candidate_digest=expectation.successor_candidate_digest,
                    current_accepted_coordinate=AcceptedCoordinate.from_internal(
                        self.instance.accepted_coordinate()
                    ),
                    path_to_acceptance=(
                        AcceptanceConditionV1(
                            condition="candidate_rebase",
                            owner="daemon",
                            action=(
                                "Retry confirmation; the coordinator will union current "
                                "backing and mint the rebased successor."
                            ),
                            satisfied=False,
                        ),
                    ),
                )
            return self._candidate_status(
                proposal_id=expectation.successor_proposal_id,
                candidate_digest=expectation.successor_candidate_digest,
            )
        evaluation = self.instance.proposal_evidence().read_evaluation(
            expectation.successor_proposal_id
        )
        return CandidateStatusV1(
            state="terminal",
            proposal_id=expectation.successor_proposal_id,
            current_accepted_coordinate=AcceptedCoordinate.from_internal(
                self.instance.accepted_coordinate()
            ),
            path_to_acceptance=tuple(
                AcceptanceConditionV1(
                    condition=item.code,
                    owner="writer",
                    action=item.message,
                    satisfied=False,
                )
                for item in evaluation.diagnostics
            ),
        )

    def _rebase_insertion_successor(
        self,
        intent: AuthoringIntentV1,
        *,
        actor: AuthenticatedActor,
        observation: InsertionConfirmationObservationV1,
    ) -> AuthoringIntentV1:
        expectation = intent.insertion_expectation
        if expectation is None:
            raise InsertionProtocolError("AuthoringIntent has no insertion expectation")
        operation_key = typed_digest(
            Sha256Value,
            "playbill-insertion-rebase-v1",
            {
                "accepted_semantic_root": self.instance.accepted_coordinate().semantic_root,
                "expectation_id": expectation.expectation_id,
                "observation_digest": insertion_confirmation_operation_key(
                    expectation.expectation_id,
                    observation,
                ),
            },
        ).tagged

        def rebase(current: AuthoringIntentV1) -> AuthoringIntentV1:
            live = current.insertion_expectation
            if live is None or live.state != "confirming":
                return current
            updated, _status = self._submit_insertion_successor(
                current,
                live,
                actor=actor,
                observation=observation,
            )
            return current.model_copy(update={"insertion_expectation": updated})

        return self.store.transition(
            intent.intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=rebase,
        )

    def _current_claim(self, intent: AuthoringIntentV1) -> ClaimArtifactAny | None:
        path = claim_path(intent.semantic_identity)
        content = self.instance.tree_at(self.instance.accepted_coordinate().git_oid).get(path)
        return None if content is None else parse_claim(content, path=path)

    def _protocol_time(self, intent: AuthoringIntentV1) -> datetime:
        current = self.instance.accepted_history()[-1]
        value = (
            intent.canonical_timestamp
            if current.record is None
            else current.record.candidate.timestamp
        )
        parsed = parse_datetime(value)
        if parsed is None:  # pragma: no cover - accepted timestamp invariant
            raise RuntimeError("accepted protocol timestamp did not parse")
        return parsed

    def _refresh_protocol(
        self,
        intent: AuthoringIntentV1,
        *,
        actor: AuthenticatedActor,
    ) -> AuthoringIntentV1:
        reduced = self._reduce_status(intent)
        expectation = intent.insertion_expectation
        evaluation_time = self.clock()
        if (
            expectation is not None
            and expectation.state in {"awaiting_claim_acceptance", "pending"}
            and evaluation_time >= expectation.patch.expires_at
        ):
            expired = self._transition_expired(
                intent.model_copy(update={"candidate_status": reduced}),
                actor=actor,
                evaluation_time=evaluation_time,
            )
            return expired.model_copy(update={"candidate_status": reduced})
        if expectation is None or expectation.state in {
            "bound",
            "expired",
            "abandoned",
            "claim_currency_changed",
        }:
            return intent.model_copy(update={"candidate_status": reduced})

        current_claim = self._current_claim(intent)
        next_expectation = expectation
        if expectation.state == "awaiting_claim_acceptance":
            if reduced.state != "accepted":
                return intent.model_copy(update={"candidate_status": reduced})
            if current_claim is None or current_claim.lifecycle.state != "live":
                next_expectation = mark_claim_currency_changed(
                    intent,
                    expectation,
                    finalized_at=self._protocol_time(intent),
                )
            elif (
                claim_statement_digest(current_claim.statement).tagged
                != expectation.claim_statement_digest
            ):
                next_expectation = mark_claim_currency_changed(
                    intent,
                    expectation,
                    finalized_at=self._protocol_time(intent),
                )
            else:
                next_expectation = mark_claim_accepted(expectation)
        elif (
            current_claim is None
            or current_claim.lifecycle.state != "live"
            or (
                claim_statement_digest(current_claim.statement).tagged
                != expectation.claim_statement_digest
            )
        ):
            next_expectation = mark_claim_currency_changed(
                intent,
                expectation,
                finalized_at=self._protocol_time(intent),
            )
        elif expectation.state == "confirming" and isinstance(current_claim, ClaimArtifactV2):
            if expectation.citation_id is not None and any(
                item.citation_id == expectation.citation_id
                for item in current_claim.backing.citations
            ):
                next_expectation = mark_bound(
                    intent,
                    expectation,
                    finalized_at=self._protocol_time(intent),
                )

        if next_expectation == expectation:
            return intent.model_copy(update={"candidate_status": reduced})
        operation_key = typed_digest(
            Sha256Value,
            "playbill-insertion-refresh-v1",
            {
                "accepted_semantic_root": self.instance.accepted_coordinate().semantic_root,
                "expectation_digest": next_expectation.expectation_digest,
                "intent_id": intent.intent_id,
            },
        ).tagged
        return self.store.transition(
            intent.intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=lambda current: current.model_copy(
                update={
                    "candidate_status": self._reduce_status(current),
                    "insertion_expectation": next_expectation,
                }
            ),
        )

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
