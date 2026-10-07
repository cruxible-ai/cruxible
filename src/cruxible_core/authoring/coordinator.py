"""Daemon-owned AuthoringIntent lifecycle before compilation and submission."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, cast

from cruxible_client.contracts.attestations import (
    VerifiedApproval,
    approval_requirements_satisfied,
    verify_approval,
)
from cruxible_client.contracts.authoring.inputs import AuthoringInput, lower_authoring_input
from cruxible_client.contracts.authoring.models import (
    AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
    AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    AUTHORING_SDK_VERSION,
    AcceptanceCondition,
    AuthoringExpectation,
    AuthoringIntent,
    AuthoringIntentList,
    AuthoringIntentV1,
    AuthoringIntentView,
    AuthoringPayload,
    AuthoringProgramStamp,
    AuthoringReferenceExpectation,
    AuthoringSlotExpectation,
    AuthoringSubmitMember,
    AuthoringSubmitResult,
    CandidateStatus,
    CandidateStatusState,
    ChangeSetAuthoringPayload,
    ChangeSetClaimIdentity,
    ClaimAuthoringPayloadV1,
    ExistingCaptureCitationSource,
    PreflightResult,
    ProcedureAuthoringPayload,
    authoring_change_set_membership,
    authoring_create_fingerprint,
    authoring_member_identity,
    authoring_payload_digest,
    reference_expectations_digest,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import (
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claims import (
    claim_path,
    new_claim_id,
)
from cruxible_client.contracts.errors import ApprovalIntegrityError, CruxibleError
from cruxible_client.contracts.temporal import utc_now
from cruxible_core.authoring.preflight import (
    ComputedPreflight,
    authoring_operation,
    compute_preflight,
)
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.compiler.projection_artifacts import projected_revision
from cruxible_core.indexes.history.history_index import detached_history_reads
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.proposals.candidate_cards import is_candidate_card_path
from cruxible_core.proposals.prepared_evaluation import PreparedEvaluationScope
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalHeadMovedError,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.identity import require_authoring_principal
from cruxible_core.storage.cas import dry_run_bodies

AUTHORING_REBASE_DOMAIN = "playbill-authoring-rebase-v1"


class AuthoringIntentRebaseError(CruxibleError):
    code = "cruxible.authoring.intent_rebase_not_allowed"


class AuthoringIntentRebaseSubmitted(AuthoringIntentRebaseError):
    code = "cruxible.authoring.intent_rebase_submitted"


class AuthoringProgramStampError(CruxibleError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _validate_program_stamp(program_stamp: AuthoringProgramStamp) -> None:
    if program_stamp.sdk_contract_snapshot_digest != AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST:
        raise AuthoringProgramStampError(
            "cruxible.authoring.program_stamp_contract_mismatch",
            "the SDK contract snapshot is not the daemon's exact frozen snapshot",
        )
    if program_stamp.sdk_version != AUTHORING_SDK_VERSION:
        raise AuthoringProgramStampError(
            "cruxible.authoring.program_stamp_version_incompatible",
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
        payload: AuthoringPayload,
        canonical_timestamp: str,
        base_coordinate: AcceptedCoordinate | None = None,
        reference_expectations: tuple[AuthoringExpectation, ...] | None = None,
        program_stamp: AuthoringProgramStamp | None = None,
    ) -> AuthoringIntentView:
        """Open one authoring draft against the accepted coordinate.

        A draft is a durable write to the exhaust, not a read, so it takes the
        same terminal-state gate as every other governed-write door: an instance
        that has stopped accepting writes must not accumulate new intents that
        can never be submitted.
        """

        self.instance.require_writable()
        # Refuse an actor that can never land a proposal here before any
        # payload is resolved, compiled or preflighted.
        require_authoring_principal(self.instance, actor.actor_id)
        at = base_coordinate or AcceptedCoordinate.from_internal(
            self.instance.accepted_coordinate()
        )
        if isinstance(payload, ClaimAuthoringPayloadV1) and isinstance(
            payload.source,
            ExistingCaptureCitationSource,
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
                    "cruxible.authoring.program_stamp_contract_mismatch",
                    "a v3 program stamp requires the v2 reference-assertion envelope",
                )
        intent = self._draft_intent(
            actor=actor,
            payload=payload,
            canonical_timestamp=canonical_timestamp,
            at=at,
            reference_expectations=reference_expectations,
            intent_id=self.store.mint_intent_id(),
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
        self.finalize_completed()
        stored = self.store.create(intent, operation_key=operation_key)
        if reference_expectations is not None and (
            not isinstance(stored, AuthoringIntent)
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
        return AuthoringIntentView(intent=stored)

    def _draft_intent(
        self,
        *,
        actor: AuthenticatedActor,
        payload: AuthoringPayload,
        canonical_timestamp: str,
        at: AcceptedCoordinate,
        reference_expectations: tuple[AuthoringExpectation, ...] | None,
        intent_id: str,
    ) -> AuthoringIntentV1:
        """Build one draft intent in memory, minting its identities; nothing is stored."""

        semantic_identity = self._mint_semantic_identity(payload)
        status = CandidateStatus(
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
            "change_set_claim_identities": self._mint_change_set_claim_identities(payload),
        }
        return (
            AuthoringIntentV1.model_validate(intent_values)
            if reference_expectations is None
            else AuthoringIntent.model_validate(
                {
                    **intent_values,
                    "reference_expectations": reference_expectations,
                }
            )
        )

    def preview(
        self,
        *,
        actor: AuthenticatedActor,
        payload: AuthoringPayload,
        canonical_timestamp: str,
        reference_expectations: tuple[AuthoringExpectation, ...] | None = None,
    ) -> tuple[AuthoringIntentV1, ComputedPreflight]:
        """Preflight one payload exactly as submit would, and write nothing.

        This is a dry run: the draft is built in memory under a fresh intent ID
        instead of being stored, and the bodies lowering stores are held in
        memory (``dry_run_bodies``) and history reads never catch the derived
        index up on disk (``detached_history_reads``), so the same lowering and
        evaluation run as for a submit, against the current accepted coordinate,
        without a write.
        """

        self.instance.require_writable()
        # A preview refuses exactly the actor a create would.
        require_authoring_principal(self.instance, actor.actor_id)
        with dry_run_bodies(), detached_history_reads():
            intent = self._draft_intent(
                actor=actor,
                payload=payload,
                canonical_timestamp=canonical_timestamp,
                at=AcceptedCoordinate.from_internal(self.instance.accepted_coordinate()),
                reference_expectations=reference_expectations,
                intent_id=f"AIT-{secrets.token_hex(16)}",
            )
            return intent, compute_preflight(self.instance, intent=intent, actor=actor)

    def preview_input(
        self,
        *,
        actor: AuthenticatedActor,
        input: AuthoringInput,
        canonical_timestamp: str,
    ) -> tuple[AuthoringIntentV1, ComputedPreflight]:
        """Preflight one tagless input as ``submit_input`` would, and save no intent.

        The input lowers and binds its existing-capture expectations exactly as
        a create would, so the preview refuses everything the submit would.
        """

        # Refused at the same doors, and before lowering, exactly as create refuses.
        self.instance.require_writable()
        require_authoring_principal(self.instance, actor.actor_id)
        payload = lower_authoring_input(input)
        return self.preview(
            actor=actor,
            payload=payload,
            canonical_timestamp=canonical_timestamp,
            reference_expectations=self._existing_capture_reference_expectations(
                payload,
                coordinate=self.instance.accepted_coordinate(),
            ),
        )

    def create_input(
        self,
        *,
        actor: AuthenticatedActor,
        input: AuthoringInput,
        canonical_timestamp: str,
    ) -> AuthoringIntentView:
        """Atomically bind friendly IDs to one accepted base, then persist the intent."""

        self.instance.require_writable()
        require_authoring_principal(self.instance, actor.actor_id)
        base = self.instance.accepted_coordinate()
        coordinate = AcceptedCoordinate.from_internal(base)
        payload = lower_authoring_input(input)
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

    def get(self, intent_id: str, *, actor: AuthenticatedActor) -> AuthoringIntentView:
        intent = self._refreshed(self.store.get(intent_id, actor_id=actor.actor_id))
        return AuthoringIntentView(intent=intent)

    def list_pending(self, *, actor: AuthenticatedActor) -> AuthoringIntentList:
        reduced = tuple(
            self._refreshed(intent) for intent in self.store.list_pending(actor_id=actor.actor_id)
        )
        return AuthoringIntentList(
            intents=tuple(
                intent
                for intent in reduced
                if intent.candidate_status.state not in {"accepted", "superseded", "terminal"}
            )
        )

    def rebase(self, intent_id: str, *, actor: AuthenticatedActor) -> AuthoringIntentView:
        """Advance one refused, unsubmitted intent to the current accepted coordinate."""

        self.instance.require_writable()
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
                return AuthoringIntentView(intent=latest.intent)
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
            return AuthoringIntentView(intent=current)
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
                    "candidate_status": CandidateStatus(
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
        return AuthoringIntentView(intent=updated)

    def preflight(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
    ) -> PreflightResult:
        self.instance.require_writable()
        _computed, updated = self._compute_and_bind_preflight(intent_id, actor=actor)
        if updated.last_preflight is None:  # pragma: no cover - transition invariant
            raise RuntimeError("preflight transition omitted its result")
        return updated.last_preflight

    def _compute_and_bind_preflight(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
        prepared: PreparedEvaluationScope | None = None,
    ) -> tuple[ComputedPreflight, AuthoringIntentV1]:
        current = self.store.get(intent_id, actor_id=actor.actor_id)
        computed = compute_preflight(self.instance, intent=current, actor=actor, prepared=prepared)
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
        payload: AuthoringPayload,
        canonical_timestamp: str,
        intent_id: str | None = None,
        reference_expectations: tuple[AuthoringExpectation, ...] | None = None,
        program_stamp: AuthoringProgramStamp | None = None,
    ) -> PreflightResult:
        self.instance.require_writable()
        view = self._compose(
            actor=actor,
            payload=payload,
            canonical_timestamp=canonical_timestamp,
            intent_id=intent_id,
            reference_expectations=reference_expectations,
            program_stamp=program_stamp,
        )
        return self.preflight(view.intent.intent_id, actor=actor)

    def compile_and_submit(
        self,
        *,
        actor: AuthenticatedActor,
        payload: AuthoringPayload,
        canonical_timestamp: str,
        intent_id: str | None = None,
        reference_expectations: tuple[AuthoringExpectation, ...] | None = None,
        program_stamp: AuthoringProgramStamp | None = None,
    ) -> AuthoringSubmitResult:
        """Create or replace the intent and submit it in one call.

        Submit always computes and binds its own preflight, so a separate compile
        preflight first would only repeat it. A refused preflight returns the
        unsubmitted intent with that preflight bound, as compile would have.
        """

        self.instance.require_writable()
        view = self._compose(
            actor=actor,
            payload=payload,
            canonical_timestamp=canonical_timestamp,
            intent_id=intent_id,
            reference_expectations=reference_expectations,
            program_stamp=program_stamp,
        )
        return self.submit(view.intent.intent_id, actor=actor)

    def _compose(
        self,
        *,
        actor: AuthenticatedActor,
        payload: AuthoringPayload,
        canonical_timestamp: str,
        intent_id: str | None,
        reference_expectations: tuple[AuthoringExpectation, ...] | None,
        program_stamp: AuthoringProgramStamp | None,
    ) -> AuthoringIntentView:
        if intent_id is None:
            return self.create(
                actor=actor,
                payload=payload,
                canonical_timestamp=canonical_timestamp,
                reference_expectations=reference_expectations,
                program_stamp=program_stamp,
            )
        return self.replace_payload(
            intent_id,
            actor=actor,
            payload=payload,
            reference_expectations=reference_expectations,
            program_stamp=program_stamp,
        )

    def compile_input(
        self,
        *,
        actor: AuthenticatedActor,
        input: AuthoringInput,
        canonical_timestamp: str,
        intent_id: str | None = None,
    ) -> PreflightResult:
        self.instance.require_writable()
        view = self._compose_input(
            actor=actor,
            input=input,
            canonical_timestamp=canonical_timestamp,
            intent_id=intent_id,
        )
        return self.preflight(view.intent.intent_id, actor=actor)

    def submit_input(
        self,
        *,
        actor: AuthenticatedActor,
        input: AuthoringInput,
        canonical_timestamp: str,
        intent_id: str | None = None,
    ) -> AuthoringSubmitResult:
        """Compile one tagless input and submit it in one call.

        Without ``intent_id`` the input becomes a new intent; with one, it
        replaces that staged intent's payload first. Submit computes and binds
        its own preflight, so a refused input returns the unsubmitted intent
        with that preflight bound, as compile would have.
        """

        self.instance.require_writable()
        view = self._compose_input(
            actor=actor,
            input=input,
            canonical_timestamp=canonical_timestamp,
            intent_id=intent_id,
        )
        return self.submit(view.intent.intent_id, actor=actor)

    def _compose_input(
        self,
        *,
        actor: AuthenticatedActor,
        input: AuthoringInput,
        canonical_timestamp: str,
        intent_id: str | None,
    ) -> AuthoringIntentView:
        if intent_id is None:
            return self.create_input(
                actor=actor,
                input=input,
                canonical_timestamp=canonical_timestamp,
            )
        current = self.store.get(intent_id, actor_id=actor.actor_id)
        payload = lower_authoring_input(input)
        base = self.instance.resolve_accepted_coordinate(
            git_oid=current.base_coordinate.git_oid,
            semantic_root=current.base_coordinate.semantic_root,
            generation_root=current.base_coordinate.generation_root,
            compiler_digest=current.base_coordinate.compiler_digest,
        )
        return self.replace_payload(
            intent_id,
            actor=actor,
            payload=payload,
            reference_expectations=self._existing_capture_reference_expectations(
                payload,
                coordinate=base,
            ),
        )

    def _existing_capture_reference_expectations(
        self,
        payload: AuthoringPayload,
        *,
        coordinate: AcceptedProjectionCoordinate,
    ) -> tuple[AuthoringExpectation, ...] | None:
        """Assert the exact accepted contract behind a decision-input Capture ref."""

        if not isinstance(payload, ClaimAuthoringPayloadV1) or not isinstance(
            payload.source,
            ExistingCaptureCitationSource,
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
                path = projection.citations.capture_contract_path(envelope.capture_contract_digest)
            if not isinstance(path, str):
                return ()
        except (CruxibleError, ValueError):
            return ()
        return (
            AuthoringReferenceExpectation(
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
        return True, self._projected_claim_revision(
            claim_id=preflighted.semantic_identity,
            artifact_digest=artifact_digest,
        )

    def _projected_claim_revision(self, *, claim_id: str, artifact_digest: str) -> int:
        path = claim_path(claim_id)
        # The history index answers without reading any record; a member whose
        # history predates stored digests falls back to the records.
        indexed = self.instance.projected_member_revision(path, artifact_digest)
        if indexed is not None:
            return indexed
        # Only the records that touched this Claim count toward its revision.
        records = self.instance.member_record_history((path,))
        return projected_revision(
            records,
            path=path,
            input_digest=artifact_digest,
            artifact_digest=artifact_digest,
        )

    def _submit_members(
        self,
        computed: ComputedPreflight,
        preflighted: AuthoringIntentV1,
    ) -> tuple[AuthoringSubmitMember, ...]:
        """Say what every submitted member became, one row per member.

        One intent is one changeset, so the amend-in-place answer the singular
        fields give for a Claim intent is owed once per member for a set --
        otherwise a caller who submitted eighty members has to re-read eighty
        artifacts to learn which of them reused a lineage.
        """

        lowered = computed.lowered
        if lowered is None:
            return ()
        if isinstance(preflighted.payload, ClaimAuthoringPayloadV1):
            artifact_digest = lowered.resolved_authoring.get("artifact_digest")
            if not isinstance(artifact_digest, str):
                return ()
            identity_stable, claim_revision = self._revision_marker(computed, preflighted)
            predecessor = lowered.resolved_authoring.get("predecessor_digest")
            return (
                AuthoringSubmitMember(
                    identity=f"Claim:{preflighted.semantic_identity}",
                    artifact_digest=artifact_digest,
                    predecessor_digest=predecessor if isinstance(predecessor, str) else None,
                    identity_stable=identity_stable,
                    claim_revision=claim_revision,
                ),
            )
        raw_members = lowered.resolved_authoring.get("members")
        if not isinstance(raw_members, list):
            return ()
        members: list[AuthoringSubmitMember] = []
        for raw in raw_members:
            if not isinstance(raw, dict):  # pragma: no cover - lowering invariant
                continue
            identity = raw.get("identity")
            artifact_digest = raw.get("artifact_digest")
            if not isinstance(identity, str) or not isinstance(artifact_digest, str):
                continue
            predecessor = raw.get("predecessor_digest")
            claim_id = raw.get("claim_id")
            amends = isinstance(predecessor, str) and isinstance(claim_id, str)
            members.append(
                AuthoringSubmitMember(
                    identity=identity,
                    artifact_digest=artifact_digest,
                    predecessor_digest=predecessor if isinstance(predecessor, str) else None,
                    identity_stable=amends,
                    claim_revision=(
                        self._projected_claim_revision(
                            claim_id=cast(str, claim_id),
                            artifact_digest=artifact_digest,
                        )
                        if amends
                        else None
                    ),
                )
            )
        return tuple(sorted(members, key=lambda item: item.identity.encode("utf-8")))

    def submit(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
    ) -> AuthoringSubmitResult:
        self.instance.require_writable()
        with self.instance.prepared_evaluations.scope() as prepared:
            current = self._refreshed(self.store.get(intent_id, actor_id=actor.actor_id))
            reduced = current.candidate_status
            if reduced.state == "accepted":
                idempotent_existing = reduced.proposal_id is None
                revision: int | None = None
                if idempotent_existing and isinstance(current.payload, ClaimAuthoringPayloadV1):
                    coordinate = self.instance.accepted_coordinate()
                    with self.instance.bind_accepted_projection(coordinate) as projection:
                        projected = projection.claim(f"Claim:{current.semantic_identity}")
                    revision = None if projected is None else projected.envelope.revision
                return AuthoringSubmitResult(
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
                    return AuthoringSubmitResult(
                        intent=current.model_copy(update={"candidate_status": reduced}),
                        status=reduced,
                        workspace_advertisement=self.instance.advertise_workspace(),
                    )

            computed, preflighted = self._compute_and_bind_preflight(
                intent_id, actor=actor, prepared=prepared
            )
            if computed.result.verdict == "refused":
                status = computed.status
                if current.candidate_status.proposal_id is not None:
                    status = status.model_copy(update={"state": "conflicted_after_rebase"})
                return AuthoringSubmitResult(
                    intent=preflighted.model_copy(update={"candidate_status": status}),
                    status=status,
                    workspace_advertisement=self.instance.advertise_workspace(),
                )
            if computed.lowered is not None and computed.lowered.idempotent:
                accepted = AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())
                status = CandidateStatus(
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
                claim_revision = None
                if isinstance(preflighted.payload, ClaimAuthoringPayloadV1):
                    with self.instance.bind_accepted_projection(
                        self.instance.accepted_coordinate()
                    ) as projection:
                        projected = projection.claim(f"Claim:{preflighted.semantic_identity}")
                    claim_revision = None if projected is None else projected.envelope.revision
                return AuthoringSubmitResult(
                    intent=accepted_intent,
                    status=accepted_intent.candidate_status,
                    workspace_advertisement=self.instance.advertise_workspace(),
                    identity_stable=True,
                    claim_revision=claim_revision,
                )
            if computed.evaluation is None or computed.evaluation.candidate is None:
                raise RuntimeError("passing preflight omitted its evaluated candidate")

            certificate = computed.result.certificate
            handoff = prepared.handoff(authoring_operation(self.instance, preflighted))
            bound = certificate.accepted_coordinate

            # Slot membership is checked by preflight, at the certificate
            # coordinate, and nowhere else: a fresh evaluation at a moved head
            # would admit the change over a slot it never saw. Only an intent
            # that pins a slot is held to that head; any other re-evaluates
            # and publishes at whatever head admission finds.
            pins_slots = isinstance(preflighted, AuthoringIntent) and any(
                isinstance(item, AuthoringSlotExpectation)
                for item in preflighted.reference_expectations
            )

            def at_certificate(
                evaluated_at: AcceptedProjectionCoordinate, _tree: Mapping[str, bytes]
            ) -> None:
                # Refused before evaluation; the service then holds this head
                # unchanged through publication.
                if AcceptedCoordinate.from_internal(evaluated_at) != bound:
                    raise ProposalHeadMovedError(
                        "accepted main moved after preflight; preflight again at the current head"
                    )
                return None

            def moved_on() -> AuthoringSubmitResult:
                latest = AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())
                status = CandidateStatus(
                    state="conflicted_after_rebase",
                    current_accepted_coordinate=latest,
                    path_to_acceptance=(
                        AcceptanceCondition(
                            condition="repreflight_after_concurrent_acceptance",
                            owner="daemon",
                            action="Retry submit; the coordinator will rebase and preflight.",
                            satisfied=False,
                        ),
                    ),
                )
                return AuthoringSubmitResult(
                    intent=preflighted.model_copy(update={"candidate_status": status}),
                    status=status,
                    workspace_advertisement=self.instance.advertise_workspace(),
                )

            try:
                result = self.instance.proposal_service().submit(
                    actor=actor,
                    request=ProposalAdmissionRequest(
                        target_ref=certificate.proposal_ref,
                        proposed_base_oid=certificate.accepted_coordinate.git_oid,
                        # The one door that carries prose today. Every other submit call
                        # site authors on the author's behalf -- a migration, a seed, a
                        # retirement -- and has no sentence of theirs to pass on, so it
                        # keeps the derived subject.
                        rationale=(
                            preflighted.payload.rationale
                            if isinstance(preflighted.payload, ChangeSetAuthoringPayload)
                            else None
                        ),
                    ),
                    candidate_tree=handoff.submission_tree
                    if handoff is not None and handoff.submission_tree is not None
                    else {
                        path: content
                        for path, content in computed.evaluated_tree.items()
                        if not is_candidate_card_path(path)
                    },
                    timestamp=current.canonical_timestamp,
                    prepared=handoff,
                    # At the certificate head the evaluation must reproduce the
                    # preflighted candidate; that is refused before publication.
                    expected_candidate=(
                        bound.git_oid,
                        computed.evaluation.candidate.candidate_digest,
                    ),
                    authorize=at_certificate if pins_slots else None,
                )
            except ProposalHeadMovedError:
                return moved_on()
            if result.candidate is None:
                if AcceptedCoordinate.from_internal(self.instance.accepted_coordinate()) == bound:
                    raise RuntimeError("submit broke its unchanged-coordinate preflight binding")
                # The fresh evaluation at the moved head refused the rebase.
                return moved_on()

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
            identity_stable, claim_revision = self._revision_marker(computed, preflighted)
            return AuthoringSubmitResult(
                intent=submitted,
                status=submitted.candidate_status,
                workspace_advertisement=result.workspace_advertisement,
                identity_stable=identity_stable,
                claim_revision=claim_revision,
                members=self._submit_members(computed, preflighted),
            )

    def status(self, intent_id: str, *, actor: AuthenticatedActor) -> CandidateStatus:
        intent = self._refreshed(self.store.get(intent_id, actor_id=actor.actor_id))
        return intent.candidate_status

    def replace_payload(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
        payload: AuthoringPayload,
        reference_expectations: tuple[AuthoringExpectation, ...] | None = None,
        program_stamp: AuthoringProgramStamp | None = None,
    ) -> AuthoringIntentView:
        self.instance.require_writable()
        if program_stamp is not None:
            _validate_program_stamp(program_stamp)
            if reference_expectations is None:
                raise AuthoringProgramStampError(
                    "cruxible.authoring.program_stamp_contract_mismatch",
                    "a v3 program stamp requires the v2 reference-assertion envelope",
                )
        payload_digest = authoring_payload_digest(payload)
        # The idempotency key names the CREATE FINGERPRINT, never the payload
        # digest: a change set's payload digest deliberately drops its
        # rationale, so two replacements that keep the members and rewrite the
        # prose would mint one key, and the store would read the second as a
        # replay and return the first without applying it -- a success view
        # carrying the prose the author had just replaced. Nothing in this
        # preimage is revision-scoped, so the collision would be permanent for
        # the life of the intent. The fingerprint digests the whole payload, so
        # a genuine retry of one request still mints one key.
        replacement_fingerprint = authoring_create_fingerprint(
            instance_id=self.instance.descriptor.instance_id,
            actor_id=actor.actor_id,
            payload=payload,
        )
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
                "create_fingerprint": replacement_fingerprint,
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
            if isinstance(current.payload, ChangeSetAuthoringPayload) or isinstance(
                payload, ChangeSetAuthoringPayload
            ):
                if not isinstance(current.payload, ChangeSetAuthoringPayload) or not isinstance(
                    payload, ChangeSetAuthoringPayload
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
                        ProcedureAuthoringPayload,
                    )
                    else type(current.payload).__name__
                )
                payload_family = (
                    "Procedure"
                    if isinstance(payload, ProcedureAuthoringPayload)
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
                "candidate_status": CandidateStatus(
                    state="draft",
                    current_accepted_coordinate=at,
                ),
            }
            if reference_expectations is None:
                return current.model_copy(update=updates)
            return AuthoringIntent.model_validate(
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
        return AuthoringIntentView(intent=updated)

    def _replace_reference_expectations(
        self,
        current: AuthoringIntentV1,
        *,
        actor: AuthenticatedActor,
        reference_expectations: tuple[AuthoringExpectation, ...],
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
            if isinstance(intent, AuthoringIntent) and (
                intent.reference_expectations == reference_expectations
            ):
                return intent
            return AuthoringIntent.model_validate(
                {
                    **intent.model_dump(mode="json"),
                    "tag": "playbill-authoring-intent-v2",
                    "reference_expectations": [
                        item.model_dump(mode="json") for item in reference_expectations
                    ],
                    "intent_revision": intent.intent_revision + 1,
                    "last_preflight": None,
                    "candidate_status": CandidateStatus(
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

    def _mint_change_set_claim_identities(
        self,
        payload: AuthoringPayload,
    ) -> tuple[ChangeSetClaimIdentity, ...]:
        """Mint one Claim ID per new Claim member, once, at create.

        A singular Claim intent has minted its ID into `semantic_identity` since
        PC-G1b. A change set has no single identity to mint into, and its members
        still need durable IDs that survive preflight, rebase and replay, so each
        Claim member's ID is frozen here beside the payload-derived member
        identity it belongs to.
        """

        if not isinstance(payload, ChangeSetAuthoringPayload):
            return ()
        minted = tuple(
            ChangeSetClaimIdentity(
                member_identity=authoring_member_identity(member),
                claim_id=member.revises or self.claim_id_factory(),
            )
            for member in payload.members
            if isinstance(member, ClaimAuthoringPayloadV1)
        )
        return tuple(sorted(minted, key=lambda item: item.member_identity.encode("utf-8")))

    def _mint_semantic_identity(self, payload: AuthoringPayload) -> str:
        if isinstance(payload, ClaimAuthoringPayloadV1):
            return payload.revises or self.claim_id_factory()
        if isinstance(payload, ChangeSetAuthoringPayload):
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

    def _refreshed(self, intent: AuthoringIntentV1) -> AuthoringIntentV1:
        """The intent with its candidate status reduced against accepted state."""

        return intent.model_copy(update={"candidate_status": self._reduce_status(intent)})

    def finalize_completed(self) -> None:
        """Record acceptance on submitted intents whose candidate was accepted.

        An intent accepted through its proposal is otherwise only reported as
        accepted on a status read; persisting it lets a local instance compact
        it to its receipt. Runs after an activation and before each create.
        """
        if not self.store.finishes_completed:
            return
        candidates = self.store.submitted_candidates()
        if not candidates:
            return
        # Only a candidate the history index names as accepted is loaded; the
        # rest cost one index lookup, not a read of their candidate record.
        with self.instance.accepted_history_reader() as history:
            accepted = [
                (intent_id, actor_id)
                for intent_id, actor_id, digest in candidates
                if history.generation_for_candidate(digest) is not None
            ]
        for intent_id, actor_id in accepted:
            intent = self.store.get(intent_id, actor_id=actor_id)
            if intent.candidate_status.state == "accepted":
                continue  # another caller already finished it
            reduced = self._reduce_status(intent)
            if reduced.state != "accepted":
                continue
            key = typed_digest(
                Sha256Value,
                "playbill-authoring-accepted-v1",
                {"intent_id": intent.intent_id, "candidate_digest": reduced.candidate_digest},
            ).tagged
            self.store.complete(
                intent.intent_id,
                actor_id=intent.actor_id,
                operation_key=key,
                status=reduced,
            )

    def _reduce_status(self, intent: AuthoringIntentV1) -> CandidateStatus:
        status = intent.candidate_status
        if status.proposal_id is None or status.candidate_digest is None:
            return status.model_copy(
                update={
                    "current_accepted_coordinate": AcceptedCoordinate.from_internal(
                        self.instance.accepted_coordinate()
                    )
                }
            )
        # The history index names the generation that published this candidate,
        # so no change-set record is read.
        with self.instance.accepted_history_reader() as history:
            location = history.generation_for_candidate(status.candidate_digest)
        if location is not None:
            accepted = self.instance.coordinate_for_oid(location.git_oid)
            return CandidateStatus(
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
            return CandidateStatus(
                state=state,
                proposal_id=status.proposal_id,
                candidate_digest=status.candidate_digest,
                current_accepted_coordinate=AcceptedCoordinate.from_internal(
                    self.instance.accepted_coordinate()
                ),
                path_to_acceptance=(
                    AcceptanceCondition(
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
    ) -> CandidateStatus:
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
        conditions: list[AcceptanceCondition] = []
        approvals_complete = approval_requirements_satisfied(
            candidate,
            verified_approvals,
            principals=generation.principals,
            creator_principal_id=admission.actor_id,
        )
        if candidate.approval_requirements:
            conditions.append(
                AcceptanceCondition(
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
                AcceptanceCondition(
                    condition="principal-lifecycle-actor-binding",
                    owner="approver",
                    action="The lifecycle actor must sign the exact candidate.",
                    satisfied=actor_binding_satisfied,
                )
            )
        conditions.append(
            AcceptanceCondition(
                condition="activation",
                owner="daemon",
                action="Activate the candidate through the existing settlement path.",
                satisfied=False,
            )
        )
        return CandidateStatus(
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
