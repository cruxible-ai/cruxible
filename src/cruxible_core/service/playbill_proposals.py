"""Deterministic operational reads over proposal evidence and caller identity."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, model_validator

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.errors import (
    ProposalAdmissionError,
    ProposalIntegrityError,
    ProposalNotFoundError,
    ProposalReadmitRequiresResubmission,
    ProposalSelectorAmbiguousError,
)
from cruxible_core.playbill.id_prefixes import AmbiguousIdPrefix, resolve_id_prefix
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalResult,
    ProposalWithdrawalRecordV1,
)
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)
from cruxible_core.runtime.permissions import PermissionMode

ProposalInventoryStatus = Literal["open", "settled"]
ProposalTerminalReason = Literal["accepted", "refused", "stale", "withdrawn"]
WhoAmIActorIdSource = Literal["runtime_credential_label", "local_operator"]
PrincipalRegistrationStatus = Literal["active", "revoked", "absent"]
CredentialPermissionMode = Literal["read_only", "governed_write", "graph_write", "admin"]


class _StrictOperationalReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillProposalListEntryV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-list-entry-v1"] = "playbill-proposal-list-entry-v1"
    proposal_id: str
    actor_id: str
    target_ref: str
    admitted_at: str
    verdict: Literal["candidate", "refused"]
    candidate_digest: str | None = None
    status: ProposalInventoryStatus
    terminal_reason: ProposalTerminalReason | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> "PlaybillProposalListEntryV1":
        if (self.status == "open") != (self.terminal_reason is None):
            raise ValueError("open proposal status and terminal reason disagree")
        if self.verdict == "refused" and self.terminal_reason != "refused":
            raise ValueError("refused evaluation must be a settled refusal")
        return self


class PlaybillProposalListV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-list-v1"] = "playbill-proposal-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    status_filter: ProposalInventoryStatus | None = None
    entries: tuple[PlaybillProposalListEntryV1, ...]


class PlaybillProposalReadmitResultV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-readmit-result-v1"] = "playbill-proposal-readmit-result-v1"
    source_proposal_id: str
    operation_digest: str
    proposal: PlaybillProposalInspection


class PlaybillProposalWithdrawResultV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-withdraw-result-v1"] = "playbill-proposal-withdraw-result-v1"
    proposal_id: str
    actor_id: str
    reason: str
    withdrawn_at: str
    already_withdrawn: bool = False


class PlaybillProposalSelectorResultV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-selector-result-v1"] = "playbill-proposal-selector-result-v1"
    selector: str
    proposal_id: str


class PlaybillWhoAmIV1(_StrictOperationalReadModel):
    tag: Literal["playbill-whoami-v1"] = "playbill-whoami-v1"
    actor_id: str
    credential_label: str
    actor_id_source: WhoAmIActorIdSource
    credential_permission_mode: CredentialPermissionMode
    principal_registration_status: PrincipalRegistrationStatus
    active_principal_ids: tuple[str, ...]
    coordinate: PlaybillAcceptedCoordinate

    @model_validator(mode="after")
    def _credential_binding(self) -> "PlaybillWhoAmIV1":
        if (
            self.actor_id_source == "runtime_credential_label"
            and self.credential_label != self.actor_id
        ):
            raise ValueError("runtime credential label must equal the governed actor id")
        return self


def service_list_playbill_proposals(
    instance: PlaybillInstance,
    *,
    status: ProposalInventoryStatus | None = None,
) -> PlaybillProposalListV1:
    """Reduce immutable proposal evidence against the current accepted coordinate."""

    coordinate = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    evidence = instance.proposal_evidence()
    accepted_candidates = {
        generation.record.candidate_digest
        for generation in instance.accepted_history()
        if generation.record is not None
    }
    entries: list[PlaybillProposalListEntryV1] = []
    withdrawn = evidence.withdrawn_proposal_ids()
    for admission in evidence.list_admissions():
        evaluation = evidence.read_evaluation(admission.proposal_id)
        candidate_digest = evaluation.candidate_digest
        if evaluation.verdict == "refused":
            entry_status: ProposalInventoryStatus = "settled"
            terminal_reason: ProposalTerminalReason | None = "refused"
        elif candidate_digest in accepted_candidates:
            entry_status = "settled"
            terminal_reason = "accepted"
        else:
            assert candidate_digest is not None
            candidate = evidence.read_candidate(candidate_digest)
            if admission.proposal_id in withdrawn:
                # A withdrawal outranks staleness: an actor who said this
                # proposal will never be settled has answered the question
                # `readmit` would otherwise keep open.
                entry_status = "settled"
                terminal_reason = "withdrawn"
            elif candidate.candidate.parent_semantic_root == coordinate.semantic_root:
                entry_status = "open"
                terminal_reason = None
            else:
                entry_status = "settled"
                terminal_reason = "stale"
        entry = PlaybillProposalListEntryV1(
            proposal_id=admission.proposal_id,
            actor_id=admission.actor_id,
            target_ref=admission.target_ref,
            admitted_at=admission.admitted_at,
            verdict=evaluation.verdict,
            candidate_digest=candidate_digest,
            status=entry_status,
            terminal_reason=terminal_reason,
        )
        if status is None or status == entry.status:
            entries.append(entry)
    entries.sort(key=lambda item: (item.admitted_at.encode("utf-8"), item.proposal_id.encode()))
    return PlaybillProposalListV1(
        coordinate=coordinate,
        status_filter=status,
        entries=tuple(entries),
    )


def service_resolve_playbill_proposal_selector(
    instance: PlaybillInstance,
    *,
    selector: str,
) -> PlaybillProposalSelectorResultV1:
    """Resolve a user selector once to immutable proposal admission evidence."""

    admissions = tuple(instance.proposal_evidence().list_admissions())
    proposal_ids = tuple(item.proposal_id for item in admissions)
    try:
        resolved = resolve_id_prefix(
            selector,
            proposal_ids,
            marker="sha256:",
            label="proposal",
        )
    except AmbiguousIdPrefix as exc:
        matches = tuple(
            sorted(
                {proposal_id for proposal_id in proposal_ids if proposal_id.startswith(selector)},
                key=lambda item: item.encode("utf-8"),
            )
        )
        raise ProposalSelectorAmbiguousError(selector, matches) from exc
    if resolved in proposal_ids:
        return PlaybillProposalSelectorResultV1(selector=selector, proposal_id=resolved)

    matching_ref_admissions = tuple(item for item in admissions if item.target_ref == selector)
    if matching_ref_admissions:
        target_oid = instance.proposal_ref_target(selector)
        current_candidates = tuple(
            sorted(
                {
                    item.proposal_id
                    for item in matching_ref_admissions
                    if item.candidate_commit_oid == target_oid
                },
                key=lambda item: item.encode("utf-8"),
            )
        )
        if len(current_candidates) == 1:
            return PlaybillProposalSelectorResultV1(
                selector=selector,
                proposal_id=current_candidates[0],
            )
        historical_candidates = tuple(
            sorted(
                {item.proposal_id for item in matching_ref_admissions},
                key=lambda item: item.encode("utf-8"),
            )
        )
        raise ProposalSelectorAmbiguousError(selector, historical_candidates)
    raise ProposalNotFoundError(selector)


def _proposal_result(instance: PlaybillInstance, proposal_id: str) -> ProposalResult:
    evidence = instance.proposal_evidence()
    admission = evidence.read_admission(proposal_id)
    evaluation = evidence.read_evaluation(proposal_id)
    candidate = (
        None
        if evaluation.candidate_digest is None
        else evidence.read_candidate(evaluation.candidate_digest)
    )
    return ProposalResult(admission=admission, evaluation=evaluation, candidate=candidate)


def service_readmit_playbill_proposal(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    actor_id: str,
) -> PlaybillProposalReadmitResultV1:
    """Replay one stale authored tree through the current ProposalService rebase."""

    source = _proposal_result(instance, proposal_id)
    if source.admission.actor_id != actor_id:
        raise ProposalAdmissionError("only the source proposal actor may readmit it")
    # A stale proposal may be withdrawn instead of readmitted, and that is the
    # actor saying this tree will never be settled. Readmitting it would settle
    # exactly that tree, under a new proposal id, so it refuses here.
    instance.proposal_evidence().refuse_withdrawn(source.admission.proposal_id)
    source_status = next(
        (
            entry
            for entry in service_list_playbill_proposals(instance, status="settled").entries
            if entry.proposal_id == proposal_id
        ),
        None,
    )
    if source_status is None or source_status.terminal_reason != "stale":
        raise ProposalAdmissionError("only a settled stale proposal may be readmitted")
    if (
        source.candidate is not None
        and len(source.candidate.members) > 1
        and any(member.artifact_kind == "claim-type" for member in source.candidate.members)
    ):
        raise ProposalReadmitRequiresResubmission()
    coordinate = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    operation_digest = typed_digest(
        Sha256Value,
        "playbill-proposal-readmit-v1",
        {
            "source_proposal_id": proposal_id,
            "current_accepted_coordinate": coordinate.model_dump(mode="json"),
        },
    ).tagged
    matching = tuple(
        admission
        for admission in instance.proposal_evidence().list_admissions()
        if admission.source_compilation_digest == operation_digest
    )
    if len(matching) > 1:
        raise ProposalIntegrityError("readmission operation digest names multiple admissions")
    if matching:
        result = _proposal_result(instance, matching[0].proposal_id)
    else:
        generation = instance.accepted_history()[-1]
        if generation.record is None:  # pragma: no cover - stale source requires a successor
            raise ProposalIntegrityError("readmission requires an accepted candidate timestamp")
        result = instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id=actor_id),
            request=ProposalAdmissionRequest(
                target_ref=(
                    f"refs/proposals/{actor_id}/readmit-"
                    f"{operation_digest.removeprefix('sha256:')[:24]}"
                ),
                proposed_base_oid=source.admission.proposed_base_oid,
                source_compilation_digest=operation_digest,
                claim_type_expansions=source.admission.claim_type_expansions,
            ),
            candidate_tree=instance.proposal_tree(source.admission.candidate_tree_oid),
            timestamp=generation.record.candidate.timestamp,
        )
    return PlaybillProposalReadmitResultV1(
        source_proposal_id=proposal_id,
        operation_digest=operation_digest,
        proposal=PlaybillProposalInspection(
            proposal=result,
            workspace_advertisement=result.workspace_advertisement,
            accepted_coordinate=coordinate,
        ),
    )


def service_withdraw_playbill_proposal(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    actor_id: str,
    reason: str,
    withdrawn_at: str,
    unscoped_operator: bool = False,
) -> PlaybillProposalWithdrawResultV1:
    """Record one actor's terminal statement that a proposal will not be settled.

    A proposal whose activation a hard limit refuses -- the ledger's change-set
    record ceiling is the case this exists for -- is admitted, evaluated and
    permanently unactivatable, and nothing removed it from the open inventory.
    Withdrawal is the missing terminal transition: it touches no accepted state
    and leaves every byte of the candidate readable, it moves the proposal out
    of `proposal list --status open` where an actor reads their work, and every
    settlement door refuses a proposal that carries one.

    Only an OPEN proposal is withdrawable. A settled one already has its terminal
    reason -- accepted, refused, or stale -- and overwriting that with a
    statement of intent would lose the outcome; a stale proposal that should not
    be readmitted is the one exception, since staleness is not an ending.

    WHO may withdraw. Ordinarily the proposal's own actor, matched on
    `admission.actor_id` -- which is the runtime credential's LABEL, because a
    label is the only identity a proposal carries and the only one a later
    request can present. That is deliberately not a tier check: an ADMIN of the
    same instance is still not the author of somebody else's proposal, and
    withdrawal is a statement of intent, which only its author has.

    A label is not durable, though. Rotating, revoking or re-minting a
    credential under a different label would leave that actor's open proposals
    withdrawable by nobody and permanently in the inventory -- card 110's
    graveyard, back through the door this verb exists to close. So an UNSCOPED
    operator, holding a daemon-wide credential rather than an instance-bound
    one, may withdraw any withdrawable proposal. It is the authority that
    already allocates and stops hosts, and withdrawal touches no accepted state,
    so it is a strictly smaller lever than the ones it holds.
    """

    admission = instance.proposal_evidence().read_admission(proposal_id)
    if admission.actor_id != actor_id and not unscoped_operator:
        raise ProposalAdmissionError(
            "only the source proposal actor, or a daemon-wide operator, may withdraw it"
        )
    existing = instance.proposal_evidence().read_withdrawal(admission.proposal_id)
    if existing is not None:
        return PlaybillProposalWithdrawResultV1(
            proposal_id=existing.proposal_id,
            actor_id=existing.actor_id,
            reason=existing.reason,
            withdrawn_at=existing.withdrawn_at,
            already_withdrawn=True,
        )
    entry = next(
        (
            item
            for item in service_list_playbill_proposals(instance).entries
            if item.proposal_id == admission.proposal_id
        ),
        None,
    )
    if entry is None:  # pragma: no cover - a read admission always lists
        raise ProposalNotFoundError(proposal_id)
    if entry.status != "open" and entry.terminal_reason != "stale":
        raise ProposalAdmissionError(
            f"only an open or stale proposal may be withdrawn; this one is {entry.terminal_reason}"
        )
    record = ProposalWithdrawalRecordV1(
        proposal_id=admission.proposal_id,
        actor_id=actor_id,
        reason=reason,
        withdrawn_at=withdrawn_at,
    )
    instance.proposal_evidence().write_withdrawal(record)
    # A withdrawal settles the proposal, so the mirror loses its branch and
    # gains the archived ref in the same publication.
    instance.request_ledger_mirror()
    return PlaybillProposalWithdrawResultV1(
        proposal_id=record.proposal_id,
        actor_id=record.actor_id,
        reason=record.reason,
        withdrawn_at=record.withdrawn_at,
    )


def service_playbill_whoami(
    instance: PlaybillInstance,
    *,
    actor_id: str,
    credential_label: str,
    actor_id_source: WhoAmIActorIdSource,
    permission_mode: PermissionMode,
) -> PlaybillWhoAmIV1:
    """Explain the transport-derived actor and its accepted principal status."""

    generation = instance.accepted_history()[-1]
    principals = generation.principals.principals
    active = tuple(
        sorted(
            (item.principal_id for item in principals if item.status == "active"),
            key=lambda item: item.encode("utf-8"),
        )
    )
    matched = next((item for item in principals if item.principal_id == actor_id), None)
    registration: PrincipalRegistrationStatus = "absent" if matched is None else matched.status
    return PlaybillWhoAmIV1(
        actor_id=actor_id,
        credential_label=credential_label,
        actor_id_source=actor_id_source,
        credential_permission_mode=cast(CredentialPermissionMode, permission_mode.name.lower()),
        principal_registration_status=registration,
        active_principal_ids=active,
        coordinate=PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )


__all__ = [
    "PlaybillProposalListEntryV1",
    "PlaybillProposalListV1",
    "PlaybillProposalReadmitResultV1",
    "PlaybillProposalWithdrawResultV1",
    "PlaybillWhoAmIV1",
    "CredentialPermissionMode",
    "PrincipalRegistrationStatus",
    "ProposalInventoryStatus",
    "ProposalTerminalReason",
    "WhoAmIActorIdSource",
    "service_list_playbill_proposals",
    "service_readmit_playbill_proposal",
    "service_withdraw_playbill_proposal",
    "service_playbill_whoami",
]
