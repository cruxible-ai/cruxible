"""Daemon-owned AuthoringIntent lifecycle before compilation and submission."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from cruxible_client.contracts.attestations import (
    VerifiedApproval,
    approval_requirements_satisfied,
    verify_approval,
)
from cruxible_client.contracts.authoring.inputs import AuthoringInputV1, lower_authoring_input
from cruxible_client.contracts.authoring.models import (
    AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
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
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ExistingCaptureCitationSourceV1,
    InsertionAbandonResultV1,
    InsertionConfirmationObservationV2,
    InsertionConfirmResultV2,
    InsertionExpectationV2,
    InsertionPrepareResultV2,
    PreflightResultV1,
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
    PublicationPreparationV2,
    PublicationPrepareWarningV1,
    PublicationSourceObservationV2,
    SelfSourceBodyV1,
    authoring_change_set_membership,
    authoring_create_fingerprint,
    authoring_member_identity,
    authoring_payload_digest,
    insertion_confirm_operation_v2_key,
    insertion_prepare_operation_v2_key,
    insertion_prepare_terminal_operation_v2_key,
    reference_expectations_digest,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    capture_contract_digest,
    capture_is_coordinator_self_source,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV2,
    claim_citation_references,
    claim_path,
    claim_statement_digest,
    new_claim_id,
    parse_claim,
)
from cruxible_client.contracts.errors import ApprovalIntegrityError, PlaybillError
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    ExternalSourceReferenceV1,
)
from cruxible_client.contracts.temporal import ensure_utc, format_datetime, parse_datetime, utc_now
from cruxible_core.playbill.authoring.insertions import (
    PUBLICATION_EXPECTATION_EXPIRY,
    InsertionProtocolError,
    PublicationClaimNotAccepted,
    PublicationConfirmationMismatch,
    PublicationNotPrepared,
    PublicationTerminalStateRefused,
    build_publication_preparation,
    mark_publication_bound,
    mark_publication_claim_accepted,
    mark_publication_prepared,
    mark_publication_terminal,
    mint_insertion_expectation_v2,
    publication_confirmation_from_source,
    publication_confirmation_matches,
)
from cruxible_core.playbill.authoring.preflight import ComputedPreflight, compute_preflight
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.candidate_cards import is_candidate_card_path
from cruxible_core.playbill.citation_relations import (
    RELATION_CONTRACT_SCHEMA,
    capture_contract_relation_subject,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.projection_artifacts import projected_revision
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


def _rendered_publication_block(
    intent: AuthoringIntentV1,
    preparation: PublicationPreparationV2 | None,
) -> str | None:
    if preparation is None:
        return None
    payload = intent.payload
    if not isinstance(payload, ClaimAuthoringPayloadV1) or not isinstance(
        payload.source, SelfSourceBodyV1
    ):
        raise InsertionProtocolError("publication intent lost its Flow-B self-source")
    framed = build_publication_preparation(preparation, body=payload.source.content)
    return base64.b64encode(framed).decode("ascii")


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
        at = base_coordinate or AcceptedCoordinate.from_internal(
            self.instance.accepted_coordinate()
        )
        if isinstance(payload, ClaimAuthoringPayloadV1) and isinstance(
            payload.source,
            ExistingCaptureCitationSourceV1,
        ):
            bound = self.instance.resolve_accepted_coordinate(
                git_oid=at.git_oid,
                semantic_root=at.semantic_root,
                generation_root=at.generation_root,
                compiler_digest=at.compiler_digest,
            )
            generated = self._existing_capture_reference_expectations(
                payload,
                coordinate=bound,
            )
            supplied = reference_expectations or ()
            if not any(item.payload_path == "source" for item in supplied):
                supplied = (*supplied, *generated)
            reference_expectations = tuple(
                sorted(
                    supplied,
                    key=lambda item: (
                        item.payload_path.encode("utf-8"),
                        item.artifact_kind.encode("ascii"),
                        item.address.encode("utf-8"),
                    ),
                )
            )
        if program_stamp is not None:
            _validate_program_stamp(program_stamp)
            if reference_expectations is None:
                raise AuthoringProgramStampError(
                    "playbill.authoring.program_stamp_contract_mismatch",
                    "a v3 program stamp requires the v2 reference-assertion envelope",
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
        expectations = self._existing_capture_reference_expectations(
            payload,
            coordinate=base,
        )
        return self.create(
            actor=actor,
            payload=payload,
            canonical_timestamp=canonical_timestamp,
            base_coordinate=coordinate,
            reference_expectations=expectations,
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
                        in {"awaiting_claim_acceptance", "pending", "prepared", "confirming"}
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
            base = self.instance.resolve_accepted_coordinate(
                git_oid=current.base_coordinate.git_oid,
                semantic_root=current.base_coordinate.semantic_root,
                generation_root=current.base_coordinate.generation_root,
                compiler_digest=current.base_coordinate.compiler_digest,
            )
            view = self.replace_payload(
                intent_id,
                actor=actor,
                payload=payload,
                reference_expectations=self._existing_capture_reference_expectations(
                    payload,
                    coordinate=base,
                ),
            )
        return self.preflight(view.intent.intent_id, actor=actor)

    def _existing_capture_reference_expectations(
        self,
        payload: AuthoringPayloadV1,
        *,
        coordinate: AcceptedProjectionCoordinate,
    ) -> tuple[AuthoringReferenceExpectationV1, ...] | None:
        """Assert the exact accepted contract behind a decision-input Capture ref."""

        if not isinstance(payload, ClaimAuthoringPayloadV1) or not isinstance(
            payload.source,
            ExistingCaptureCitationSourceV1,
        ):
            return None
        try:
            envelope = parse_capture_envelope(
                self.instance.body_store().read(
                    payload.source.capture_digest,
                    access=BodyAccessContext(
                        principal_id="playbill-authoring",
                        can_read_body=True,
                    ),
                )
            )
            with self.instance.bind_accepted_projection(coordinate) as projection:
                facts = projection.semantic_facts(
                    RELATION_CONTRACT_SCHEMA,
                    subject_identity=capture_contract_relation_subject(
                        envelope.capture_contract_digest
                    ),
                )
            if len(facts) != 1 or not isinstance(facts[0].value, dict):
                return ()
            raw_path = facts[0].value.get("path")
            path = raw_path.get("$path") if isinstance(raw_path, dict) else None
            if not isinstance(path, str):
                return ()
        except (PlaybillError, ValueError):
            return ()
        return (
            AuthoringReferenceExpectationV1(
                payload_path="source",
                artifact_kind="Source",
                address=path,
                minted_coordinate=AcceptedCoordinate.from_internal(coordinate),
            ),
        )

    def _revision_marker(
        self,
        computed: ComputedPreflight,
        preflighted: AuthoringIntentV1,
    ) -> tuple[bool, int | None]:
        """Say whether this submit amends a Claim identity in place, and to which revision.

        `revises` reuses one Claim identity rather than adding a second Claim, and
        the submit result read exactly like an ordinary create -- the caller had to
        re-read the artifact to discover the identity was reused. The lowering
        already knows: a non-null predecessor_digest IS amend-in-place.

        The revision number is the projection's, computed by the projection's own
        function rather than recounted here. Counting members independently drifts
        the moment the two disagree, and they already would: `_projected_revision`
        returns the SAME revision when this exact artifact digest is already in the
        path's history, so a resubmitted identical candidate keeps its number
        instead of claiming a new one.
        """

        lowered = computed.lowered
        if lowered is None:
            return False, None
        # Claims only. Procedure lowering also records a predecessor_digest, and
        # claim_path() refuses a Procedure identity -- so without this guard every
        # Procedure revision raised on the terminal success path, AFTER the submit
        # and the store transition had already landed: the write happened and the
        # call reported failure.
        if not isinstance(preflighted.payload, ClaimAuthoringPayloadV1):
            return False, None
        predecessor = lowered.resolved_authoring.get("predecessor_digest")
        if not isinstance(predecessor, str):
            return False, None
        artifact_digest = lowered.resolved_authoring.get("artifact_digest")
        if not isinstance(artifact_digest, str):
            return False, None
        path = claim_path(preflighted.semantic_identity)
        records = tuple(
            (path, generation.record)
            for generation in self.instance.accepted_history()
            if generation.record is not None
        )
        return True, projected_revision(
            records,
            path=path,
            input_digest=artifact_digest,
            artifact_digest=artifact_digest,
        )

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
            idempotent_existing = (
                reduced.proposal_id is None
                and isinstance(current.payload, ClaimAuthoringPayloadV1)
                and isinstance(current.payload.source, ExistingCaptureCitationSourceV1)
            )
            revision: int | None = None
            if idempotent_existing:
                coordinate = self.instance.accepted_coordinate()
                with self.instance.bind_accepted_projection(coordinate) as projection:
                    projected = projection.claim(f"Claim:{current.semantic_identity}")
                revision = None if projected is None else projected.envelope.revision
            return AuthoringSubmitResultV1(
                intent=current.model_copy(update={"candidate_status": reduced}),
                status=reduced,
                workspace_advertisement=self.instance.advertise_workspace(),
                identity_stable=idempotent_existing,
                claim_revision=revision,
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
                    workspace_advertisement=self.instance.advertise_workspace(),
                )

        computed, preflighted = self._compute_and_bind_preflight(intent_id, actor=actor)
        if computed.result.verdict == "refused":
            status = computed.status
            if current.candidate_status.proposal_id is not None:
                status = status.model_copy(update={"state": "conflicted_after_rebase"})
            return AuthoringSubmitResultV1(
                intent=preflighted.model_copy(update={"candidate_status": status}),
                status=status,
                workspace_advertisement=self.instance.advertise_workspace(),
            )
        if computed.lowered is not None and computed.lowered.idempotent:
            accepted = AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())
            status = CandidateStatusV1(
                state="accepted",
                current_accepted_coordinate=accepted,
                accepted_generation=accepted,
            )
            operation_key = typed_digest(
                Sha256Value,
                "playbill-authoring-submit-existing-association-v1",
                {
                    "certificate_digest": computed.result.certificate.certificate_digest,
                    "intent_id": intent_id,
                },
            ).tagged

            def accept_existing(intent: AuthoringIntentV1) -> AuthoringIntentV1:
                return intent.model_copy(update={"candidate_status": status})

            accepted_intent = self.store.transition(
                intent_id,
                actor_id=actor.actor_id,
                operation_key=operation_key,
                transform=accept_existing,
            )
            with self.instance.bind_accepted_projection(
                self.instance.accepted_coordinate()
            ) as projection:
                projected = projection.claim(f"Claim:{preflighted.semantic_identity}")
            claim_revision = None if projected is None else projected.envelope.revision
            return AuthoringSubmitResultV1(
                intent=accepted_intent,
                status=accepted_intent.candidate_status,
                workspace_advertisement=self.instance.advertise_workspace(),
                identity_stable=True,
                claim_revision=claim_revision,
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
            candidate_tree={
                path: content
                for path, content in computed.evaluated_tree.items()
                if not is_candidate_card_path(path)
            },
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
                workspace_advertisement=result.workspace_advertisement,
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
            insertion_expectation = mint_insertion_expectation_v2(
                preflighted,
                original_claim_artifact_digest=artifact_digest,
                claim_statement_digest=statement_digest,
                expires_at=created_at + PUBLICATION_EXPECTATION_EXPIRY,
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
        identity_stable, claim_revision = self._revision_marker(computed, preflighted)
        return AuthoringSubmitResultV1(
            intent=submitted,
            status=submitted.candidate_status,
            workspace_advertisement=result.workspace_advertisement,
            identity_stable=identity_stable,
            claim_revision=claim_revision,
        )

    def status(self, intent_id: str, *, actor: AuthenticatedActor) -> CandidateStatusV1:
        intent = self._refresh_protocol(
            self.store.get(intent_id, actor_id=actor.actor_id),
            actor=actor,
        )
        return intent.candidate_status

    def prepare_publication(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
        observation: PublicationSourceObservationV2,
    ) -> InsertionPrepareResultV2:
        before = self.store.get(intent_id, actor_id=actor.actor_id)
        current = self._refresh_protocol(before, actor=actor)
        expectation = current.insertion_expectation
        if not isinstance(expectation, InsertionExpectationV2):
            raise InsertionProtocolError("AuthoringIntent has no publication v2 expectation")
        if expectation.state == "awaiting_claim_acceptance":
            raise PublicationClaimNotAccepted(
                f"{PublicationClaimNotAccepted.code}: the governed Claim is not accepted"
            )
        exact = publication_confirmation_from_source(
            intent_id=intent_id,
            expectation=expectation,
            observation=observation,
        )
        operation_key = insertion_prepare_operation_v2_key(
            expectation.expectation_id,
            observation,
            live_expectation_digest=expectation.expectation_digest,
        )
        terminal_operation_key = insertion_prepare_terminal_operation_v2_key(
            expectation.expectation_id,
            observation,
        )
        if expectation.state == "bound":
            if exact is None:
                raise PublicationTerminalStateRefused(
                    f"{PublicationTerminalStateRefused.code}: conflicting terminal preparation"
                )
            return InsertionPrepareResultV2(
                outcome="bound",
                intent=current,
                expectation=expectation,
                preparation=expectation.preparation,
                inserted_block_base64=_rendered_publication_block(
                    current,
                    expectation.preparation,
                ),
            )
        if expectation.state in {"expired", "claim_currency_changed", "abandoned"}:
            replayed = self.store.operation_result(
                intent_id,
                actor_id=actor.actor_id,
                operation_key=terminal_operation_key,
            )
            replayed_expectation = None if replayed is None else replayed.insertion_expectation
            before_expectation = before.insertion_expectation
            if (
                replayed is None
                and isinstance(before_expectation, InsertionExpectationV2)
                and before_expectation.state
                not in {"bound", "expired", "abandoned", "claim_currency_changed"}
                and expectation.state in {"expired", "claim_currency_changed"}
            ):
                replayed = self.store.transition(
                    intent_id,
                    actor_id=actor.actor_id,
                    operation_key=terminal_operation_key,
                    transform=lambda intent: intent,
                )
                replayed_expectation = replayed.insertion_expectation
            if (
                replayed is None
                or not isinstance(replayed_expectation, InsertionExpectationV2)
                or replayed_expectation.state not in {"expired", "claim_currency_changed"}
            ):
                raise PublicationTerminalStateRefused(
                    f"{PublicationTerminalStateRefused.code}: publication is already terminal"
                )
            terminal_outcome: Literal["expired", "claim_currency_changed"] = (
                "expired" if replayed_expectation.state == "expired" else "claim_currency_changed"
            )
            return InsertionPrepareResultV2(
                outcome=terminal_outcome,
                intent=replayed,
                expectation=replayed_expectation,
                preparation=replayed_expectation.preparation,
                inserted_block_base64=_rendered_publication_block(
                    replayed,
                    replayed_expectation.preparation,
                ),
            )
        evaluation_time = self.clock()
        if exact is not None:

            def bind_applied(intent: AuthoringIntentV1) -> AuthoringIntentV1:
                live = intent.insertion_expectation
                if not isinstance(live, InsertionExpectationV2):
                    raise InsertionProtocolError("publication expectation changed version")
                return intent.model_copy(
                    update={
                        "insertion_expectation": mark_publication_bound(
                            intent,
                            live,
                            observation=exact,
                            finalized_at=evaluation_time,
                        )
                    }
                )

            bound = self.store.transition(
                intent_id,
                actor_id=actor.actor_id,
                operation_key=operation_key,
                transform=bind_applied,
            )
            bound_expectation = bound.insertion_expectation
            assert isinstance(bound_expectation, InsertionExpectationV2)
            return InsertionPrepareResultV2(
                outcome="bound",
                intent=bound,
                expectation=bound_expectation,
                preparation=bound_expectation.preparation,
                inserted_block_base64=_rendered_publication_block(
                    bound,
                    bound_expectation.preparation,
                ),
            )

        terminal_state = self._publication_guard_state(
            current,
            expectation,
            evaluation_time=evaluation_time,
        )
        if terminal_state is not None:
            terminal = self._transition_publication_terminal(
                current,
                expectation=expectation,
                actor=actor,
                state=terminal_state,
                evaluation_time=evaluation_time,
                operation_key=terminal_operation_key,
            )
            terminal_expectation = terminal.insertion_expectation
            assert isinstance(terminal_expectation, InsertionExpectationV2)
            return InsertionPrepareResultV2(
                outcome=terminal_state,
                intent=terminal,
                expectation=terminal_expectation,
                preparation=terminal_expectation.preparation,
                inserted_block_base64=_rendered_publication_block(
                    terminal,
                    terminal_expectation.preparation,
                ),
            )
        current_claim = self._current_claim(current)
        if current_claim is None:
            raise InsertionProtocolError("accepted publication Claim is missing")
        body = self._publication_body(current, current_claim)
        coordinate = expectation.accepted_claim_coordinate
        if coordinate is None:  # pragma: no cover - expectation invariant
            raise RuntimeError("accepted publication omitted its Claim coordinate")
        preparation = build_publication_preparation(
            expectation,
            observation=observation,
            body=body,
            accepted_coordinate=coordinate,
            accepted_generation=self._accepted_sequence(coordinate),
        )
        was_prepared = expectation.preparation == preparation
        warnings = self._publication_prepare_warnings(
            source_id=observation.source_id,
            body=body,
        )

        def persist_preparation(intent: AuthoringIntentV1) -> AuthoringIntentV1:
            live = intent.insertion_expectation
            if not isinstance(live, InsertionExpectationV2):
                raise InsertionProtocolError("publication expectation changed version")
            if live.expectation_digest != expectation.expectation_digest:
                raise InsertionProtocolError("publication expectation changed during prepare")
            return intent.model_copy(
                update={
                    "insertion_expectation": mark_publication_prepared(
                        live,
                        preparation=preparation,
                    )
                }
            )

        prepared = self.store.transition(
            intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=persist_preparation,
        )
        prepared_expectation = prepared.insertion_expectation
        assert isinstance(prepared_expectation, InsertionExpectationV2)
        return InsertionPrepareResultV2(
            outcome="already_prepared" if was_prepared else "prepared",
            intent=prepared,
            expectation=prepared_expectation,
            preparation=prepared_expectation.preparation,
            inserted_block_base64=_rendered_publication_block(
                prepared,
                prepared_expectation.preparation,
            ),
            warnings=warnings,
        )

    def confirm_insertion(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
        observation: InsertionConfirmationObservationV2,
    ) -> InsertionConfirmResultV2:
        before = self.store.get(intent_id, actor_id=actor.actor_id)
        current = self._refresh_protocol(before, actor=actor)
        expectation = current.insertion_expectation
        if not isinstance(expectation, InsertionExpectationV2):
            raise InsertionProtocolError("AuthoringIntent has no publication v2 expectation")
        operation_key = insertion_confirm_operation_v2_key(
            expectation.expectation_id,
            observation,
        )
        if expectation.state == "bound":
            if not publication_confirmation_matches(
                expectation,
                observation,
                intent_id=intent_id,
            ):
                raise PublicationTerminalStateRefused(
                    f"{PublicationTerminalStateRefused.code}: conflicting terminal confirmation"
                )
            return InsertionConfirmResultV2(
                outcome="already_bound",
                intent=current,
                expectation=expectation,
            )
        if expectation.state in {"expired", "claim_currency_changed", "abandoned"}:
            replayed = self.store.operation_result(
                intent_id,
                actor_id=actor.actor_id,
                operation_key=operation_key,
            )
            replayed_expectation = None if replayed is None else replayed.insertion_expectation
            before_expectation = before.insertion_expectation
            if (
                replayed is None
                and isinstance(before_expectation, InsertionExpectationV2)
                and before_expectation.state
                not in {"bound", "expired", "abandoned", "claim_currency_changed"}
                and expectation.state in {"expired", "claim_currency_changed"}
            ):
                replayed = self.store.transition(
                    intent_id,
                    actor_id=actor.actor_id,
                    operation_key=operation_key,
                    transform=lambda intent: intent,
                )
                replayed_expectation = replayed.insertion_expectation
            if (
                replayed is not None
                and isinstance(replayed_expectation, InsertionExpectationV2)
                and replayed_expectation.state in {"expired", "claim_currency_changed"}
            ):
                replayed_outcome: Literal["expired", "claim_currency_changed"] = (
                    "expired"
                    if replayed_expectation.state == "expired"
                    else "claim_currency_changed"
                )
                return InsertionConfirmResultV2(
                    outcome=replayed_outcome,
                    intent=replayed,
                    expectation=replayed_expectation,
                )
            raise PublicationTerminalStateRefused(
                f"{PublicationTerminalStateRefused.code}: publication is already terminal"
            )
        if expectation.state != "prepared":
            raise PublicationNotPrepared(
                f"{PublicationNotPrepared.code}: publication has no durable preparation"
            )

        evaluation_time = self.clock()
        if publication_confirmation_matches(
            expectation,
            observation,
            intent_id=intent_id,
        ):

            def bind(intent: AuthoringIntentV1) -> AuthoringIntentV1:
                live = intent.insertion_expectation
                if not isinstance(live, InsertionExpectationV2):
                    raise InsertionProtocolError("publication expectation changed version")
                return intent.model_copy(
                    update={
                        "insertion_expectation": mark_publication_bound(
                            intent,
                            live,
                            observation=observation,
                            finalized_at=evaluation_time,
                        )
                    }
                )

            bound = self.store.transition(
                intent_id,
                actor_id=actor.actor_id,
                operation_key=operation_key,
                transform=bind,
            )
            bound_expectation = bound.insertion_expectation
            assert isinstance(bound_expectation, InsertionExpectationV2)
            return InsertionConfirmResultV2(
                outcome="bound",
                intent=bound,
                expectation=bound_expectation,
            )

        terminal_state = self._publication_guard_state(
            current,
            expectation,
            evaluation_time=evaluation_time,
        )
        if terminal_state is not None:
            terminal = self._transition_publication_terminal(
                current,
                expectation=expectation,
                actor=actor,
                state=terminal_state,
                evaluation_time=evaluation_time,
                operation_key=operation_key,
            )
            terminal_expectation = terminal.insertion_expectation
            assert isinstance(terminal_expectation, InsertionExpectationV2)
            return InsertionConfirmResultV2(
                outcome=terminal_state,
                intent=terminal,
                expectation=terminal_expectation,
            )
        raise PublicationConfirmationMismatch(
            f"{PublicationConfirmationMismatch.code}: observation differs from exact preparation"
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
        if expectation.state == "abandoned":
            return InsertionAbandonResultV1(intent=current, expectation=expectation)
        if expectation.state in {"bound", "expired", "claim_currency_changed"}:
            raise PublicationTerminalStateRefused(
                f"{PublicationTerminalStateRefused.code}: publication is already terminal"
            )

        def abandon_publication(intent: AuthoringIntentV1) -> AuthoringIntentV1:
            live = intent.insertion_expectation
            if live is None:
                raise InsertionProtocolError("publication expectation disappeared")
            return intent.model_copy(
                update={
                    "insertion_expectation": mark_publication_terminal(
                        intent,
                        live,
                        state="abandoned",
                        finalized_at=self.clock(),
                    )
                }
            )

        updated = self.store.transition(
            intent_id,
            actor_id=actor.actor_id,
            operation_key=operation_key,
            transform=abandon_publication,
        )
        updated_expectation = updated.insertion_expectation
        assert updated_expectation is not None
        return InsertionAbandonResultV1(intent=updated, expectation=updated_expectation)

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
            if isinstance(current.payload, ClaimAuthoringPayloadV1) != isinstance(
                payload, ClaimAuthoringPayloadV1
            ):
                raise ValueError("AuthoringIntent payload kind cannot change")
            if isinstance(current.payload, ChangeSetAuthoringPayloadV1) or isinstance(
                payload, ChangeSetAuthoringPayloadV1
            ):
                if not isinstance(current.payload, ChangeSetAuthoringPayloadV1) or not isinstance(
                    payload, ChangeSetAuthoringPayloadV1
                ):
                    raise ValueError("AuthoringIntent payload kind cannot change")
                if authoring_change_set_membership(
                    current.payload.members
                ) != authoring_change_set_membership(payload.members):
                    raise ValueError("change-set replacement cannot change member identity")
            elif not isinstance(payload, ClaimAuthoringPayloadV1):
                current_family = (
                    "Procedure"
                    if isinstance(
                        current.payload,
                        ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2,
                    )
                    else type(current.payload).__name__
                )
                payload_family = (
                    "Procedure"
                    if isinstance(
                        payload, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2
                    )
                    else type(payload).__name__
                )
                if current_family != payload_family:
                    raise ValueError("AuthoringIntent payload kind cannot change")
            semantic_identity = (
                current.semantic_identity
                if isinstance(payload, ClaimAuthoringPayloadV1)
                else self._mint_semantic_identity(payload)
            )
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
        if isinstance(payload, ChangeSetAuthoringPayloadV1):
            digest = typed_digest(
                Sha256Value,
                AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
                {
                    "members": [
                        {"kind": kind, "identity": identity}
                        for kind, identity in authoring_change_set_membership(payload.members)
                    ]
                },
            ).tagged.removeprefix("sha256:")
            return f"ChangeSet:{digest}"
        return authoring_member_identity(payload)

    def _current_claim(self, intent: AuthoringIntentV1) -> ClaimArtifactAny | None:
        path = claim_path(intent.semantic_identity)
        content = self.instance.tree_at(self.instance.accepted_coordinate().git_oid).get(path)
        return None if content is None else parse_claim(content, path=path)

    def _publication_body(
        self,
        intent: AuthoringIntentV1,
        claim: ClaimArtifactAny,
    ) -> bytes:
        payload = intent.payload
        if not isinstance(payload, ClaimAuthoringPayloadV1) or not isinstance(
            payload.source, SelfSourceBodyV1
        ):
            raise InsertionProtocolError("publication intent lost its Flow-B self-source")
        if not isinstance(claim, ClaimArtifactV2):
            raise InsertionProtocolError("publication Claim has no citation-backed retained body")
        expected_digest = "sha256:" + hashlib.sha256(payload.source.content).hexdigest()
        store = self.instance.body_store()
        access = BodyAccessContext(principal_id="playbill-publication", can_read_body=True)
        contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
        contract_digest_value = capture_contract_digest(contract).tagged
        bodies: list[bytes] = []
        for citation in claim.backing.citations:
            if citation.origin != "self_source":
                continue
            envelope = parse_capture_envelope(store.read(citation.capture_digest, access=access))
            if envelope.capture_contract_digest != contract_digest_value or not (
                capture_is_coordinator_self_source(
                    envelope,
                    contract=contract,
                    claim_id=claim.identity.name,
                )
            ):
                continue
            source = envelope.source
            if not isinstance(source, CasSourceReferenceV1):
                continue
            body = store.read(source.content_digest, access=access)
            if "sha256:" + hashlib.sha256(body).hexdigest() == expected_digest:
                bodies.append(body)
        if not bodies:
            raise InsertionProtocolError(
                "accepted Claim does not retain the publication body under its self-source backing"
            )
        if any(body != bodies[0] for body in bodies[1:]):  # pragma: no cover - digest invariant
            raise RuntimeError("one retained publication digest resolved to different bytes")
        return bodies[0]

    def _publication_prepare_warnings(
        self,
        *,
        source_id: str,
        body: bytes,
    ) -> tuple[PublicationPrepareWarningV1, ...]:
        """Report exact retained anchors that this body would duplicate in one source."""

        store = self.instance.body_store()
        access = BodyAccessContext(principal_id="playbill-publication", can_read_body=True)
        tree = self.instance.tree_at(self.instance.accepted_coordinate().git_oid)
        citation_ids: set[str] = set()
        for path in sorted(tree, key=lambda item: item.encode("utf-8")):
            if not path.startswith("claims/"):
                continue
            claim = parse_claim(tree[path], path=path)
            if claim.lifecycle.state != "live":
                continue
            for citation in claim_citation_references(claim):
                try:
                    envelope = parse_capture_envelope(
                        store.read(citation.capture_digest, access=access)
                    )
                    if (
                        not isinstance(envelope.source, ExternalSourceReferenceV1)
                        or envelope.source.source_identity != source_id
                        or envelope.commitment.materialization != "cas"
                    ):
                        continue
                    commitment = store.read(envelope.commitment.digest, access=access)
                except PlaybillError:
                    continue
                if commitment and commitment in body:
                    citation_ids.add(citation.citation_id)
        if not citation_ids:
            return ()
        return (
            PublicationPrepareWarningV1(
                source_id=source_id,
                citation_ids=tuple(sorted(citation_ids, key=lambda item: item.encode("ascii"))),
            ),
        )

    def _accepted_sequence(self, coordinate: AcceptedCoordinate) -> int:
        return next(
            generation.sequence
            for generation in self.instance.accepted_history()
            if generation.oid == coordinate.git_oid
        )

    def _publication_claim_current(self, expectation: InsertionExpectationV2) -> bool:
        current_claim = self._current_claim_by_identity(expectation.claim_identity)
        return (
            current_claim is not None
            and current_claim.lifecycle.state == "live"
            and claim_statement_digest(current_claim.statement).tagged
            == expectation.claim_statement_digest
        )

    def _current_claim_by_identity(self, claim_identity: str) -> ClaimArtifactAny | None:
        path = claim_path(claim_identity)
        content = self.instance.tree_at(self.instance.accepted_coordinate().git_oid).get(path)
        return None if content is None else parse_claim(content, path=path)

    def _publication_guard_state(
        self,
        intent: AuthoringIntentV1,
        expectation: InsertionExpectationV2,
        *,
        evaluation_time: datetime,
    ) -> Literal["expired", "claim_currency_changed"] | None:
        del intent
        if ensure_utc(evaluation_time) >= expectation.expires_at:
            return "expired"
        if not self._publication_claim_current(expectation):
            return "claim_currency_changed"
        return None

    def _transition_publication_terminal(
        self,
        intent: AuthoringIntentV1,
        *,
        expectation: InsertionExpectationV2,
        actor: AuthenticatedActor,
        state: Literal["expired", "claim_currency_changed"],
        evaluation_time: datetime,
        operation_key: str | None = None,
    ) -> AuthoringIntentV1:
        key = (
            operation_key
            or typed_digest(
                Sha256Value,
                "playbill-publication-terminal-v2",
                {
                    "expectation_id": expectation.expectation_id,
                    "state": state,
                    "evaluation_time": format_datetime(ensure_utc(evaluation_time)),
                },
            ).tagged
        )

        def terminalize(current: AuthoringIntentV1) -> AuthoringIntentV1:
            live = current.insertion_expectation
            if not isinstance(live, InsertionExpectationV2):
                raise InsertionProtocolError("publication expectation changed version")
            return current.model_copy(
                update={
                    "insertion_expectation": mark_publication_terminal(
                        current,
                        live,
                        state=state,
                        finalized_at=evaluation_time,
                    )
                }
            )

        return self.store.transition(
            intent.intent_id,
            actor_id=actor.actor_id,
            operation_key=key,
            transform=terminalize,
        )

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
        if expectation is None:
            return intent.model_copy(update={"candidate_status": reduced})
        return self._refresh_publication_v2(
            intent,
            expectation=expectation,
            reduced=reduced,
            actor=actor,
            evaluation_time=self.clock(),
        )

    def _refresh_publication_v2(
        self,
        intent: AuthoringIntentV1,
        *,
        expectation: InsertionExpectationV2,
        reduced: CandidateStatusV1,
        actor: AuthenticatedActor,
        evaluation_time: datetime,
    ) -> AuthoringIntentV1:
        if expectation.state in {"bound", "expired", "abandoned", "claim_currency_changed"}:
            return intent.model_copy(update={"candidate_status": reduced})
        # Pin 15: a passive read may never decide whether a prepared postimage
        # exists. Only prepare/confirm carry the observation needed to rescue or
        # terminalize it.
        if expectation.state == "prepared":
            return intent.model_copy(update={"candidate_status": reduced})
        if ensure_utc(evaluation_time) >= expectation.expires_at:
            return self._transition_publication_terminal(
                intent.model_copy(update={"candidate_status": reduced}),
                expectation=expectation,
                actor=actor,
                state="expired",
                evaluation_time=evaluation_time,
            ).model_copy(update={"candidate_status": reduced})

        next_expectation = expectation
        if expectation.state == "awaiting_claim_acceptance":
            if reduced.state != "accepted":
                if reduced.state in {"superseded", "terminal"}:
                    next_expectation = mark_publication_terminal(
                        intent,
                        expectation,
                        state="claim_currency_changed",
                        finalized_at=self._protocol_time(intent),
                    )
                else:
                    return intent.model_copy(update={"candidate_status": reduced})
            elif not self._publication_claim_current(expectation):
                next_expectation = mark_publication_terminal(
                    intent,
                    expectation,
                    state="claim_currency_changed",
                    finalized_at=self._protocol_time(intent),
                )
            else:
                accepted = reduced.accepted_generation
                if accepted is None:  # pragma: no cover - accepted status invariant
                    raise RuntimeError("accepted publication status omitted its coordinate")
                next_expectation = mark_publication_claim_accepted(
                    expectation,
                    accepted_coordinate=accepted,
                )
        elif not self._publication_claim_current(expectation):
            next_expectation = mark_publication_terminal(
                intent,
                expectation,
                state="claim_currency_changed",
                finalized_at=self._protocol_time(intent),
            )

        if next_expectation == expectation:
            return intent.model_copy(update={"candidate_status": reduced})
        operation_key = typed_digest(
            Sha256Value,
            "playbill-publication-refresh-v2",
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
        admission = evidence.read_admission(proposal_id)
        evaluation = evidence.read_evaluation(proposal_id)
        if evaluation.candidate_digest != candidate_digest:
            raise RuntimeError("candidate status proposal association is inconsistent")
        generation = self.instance.generation_for_semantic_root(
            candidate.candidate.parent_semantic_root
        )
        principal_lifecycle = all(
            member.artifact_kind == "principal-lifecycle" for member in candidate.members
        )
        invalid_approval = False
        verified_approvals: list[VerifiedApproval] = []
        for submission in approvals:
            try:
                verified = verify_approval(
                    submission,
                    candidate=candidate.candidate,
                    principals=generation.principals,
                    purpose=("principal-lifecycle" if principal_lifecycle else "ordinary-artifact"),
                )
            except ApprovalIntegrityError:
                invalid_approval = True
            else:
                if (
                    not principal_lifecycle
                    and candidate.approval_requirements
                    and verified.signer_id == admission.actor_id
                ):
                    invalid_approval = True
                verified_approvals.append(verified)
        conditions: list[AcceptanceConditionV1] = []
        approvals_complete = approval_requirements_satisfied(
            candidate,
            verified_approvals,
            principals=generation.principals,
            creator_principal_id=admission.actor_id,
        )
        if candidate.approval_requirements:
            conditions.append(
                AcceptanceConditionV1(
                    condition="external-approval",
                    owner="approver",
                    action=(
                        "independent_approval_required mode needs one active ordinary approver "
                        "other than the candidate creator."
                    ),
                    satisfied=approvals_complete,
                )
            )
        if principal_lifecycle:
            actor_binding_satisfied = any(
                approval.signer_id == admission.actor_id for approval in verified_approvals
            )
            approvals_complete = approvals_complete and actor_binding_satisfied
            conditions.append(
                AcceptanceConditionV1(
                    condition="principal-lifecycle-actor-binding",
                    owner="approver",
                    action="The lifecycle actor must sign the exact candidate.",
                    satisfied=actor_binding_satisfied,
                )
            )
        conditions.append(
            AcceptanceConditionV1(
                condition="activation",
                owner="daemon",
                action="Activate the candidate through the existing settlement path.",
                satisfied=False,
            )
        )
        return CandidateStatusV1(
            state=(
                "approval_invalid"
                if invalid_approval
                else ("ready_to_activate" if approvals_complete else "awaiting_external_approval")
            ),
            proposal_id=proposal_id,
            candidate_digest=candidate_digest,
            current_accepted_coordinate=AcceptedCoordinate.from_internal(
                self.instance.accepted_coordinate()
            ),
            path_to_acceptance=tuple(conditions),
        )


__all__ = ["AuthoringIntentCoordinator"]
