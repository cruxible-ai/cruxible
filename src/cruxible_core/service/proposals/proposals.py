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
from cruxible_core.authoring.id_prefixes import AmbiguousIdPrefix, resolve_id_prefix
from cruxible_core.indexes.proposals.proposal_index import timestamp
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalResult,
    ProposalWithdrawalRecordV1,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.service.authoring.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)

ProposalInventoryStatus = Literal["open", "settled", "incomplete"]
ProposalIncompleteReason = Literal["missing_admission", "missing_evaluation", "missing_candidate"]
ProposalTerminalReason = Literal["accepted", "refused", "stale", "withdrawn"]
WhoAmIActorIdSource = Literal["runtime_credential_label", "local_operator"]
PrincipalRegistrationStatus = Literal["active", "revoked", "absent"]
CredentialPermissionMode = Literal["read_only", "governed_write", "graph_write", "admin"]


class _StrictOperationalReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillProposalListEntryV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-list-entry-v1"] = "playbill-proposal-list-entry-v1"
    proposal_id: str
    actor_id: str | None
    target_ref: str | None
    admitted_at: str | None
    verdict: Literal["candidate", "refused"] | None
    candidate_digest: str | None = None
    status: ProposalInventoryStatus
    terminal_reason: ProposalTerminalReason | None = None
    incomplete_reasons: tuple[ProposalIncompleteReason, ...] = ()
    withdrawal_present: bool = False

    @model_validator(mode="after")
    def _status_shape(self) -> "PlaybillProposalListEntryV1":
        if self.status == "incomplete":
            if not self.incomplete_reasons or self.terminal_reason is not None:
                raise ValueError(
                    "incomplete proposal must name missing evidence without a terminal reason"
                )
            return self
        if self.incomplete_reasons or any(
            value is None
            for value in (self.actor_id, self.target_ref, self.admitted_at, self.verdict)
        ):
            raise ValueError("complete proposal requires admission and evaluation evidence")
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
    return PlaybillProposalListV1(
        coordinate=coordinate,
        status_filter=status,
        entries=tuple(
            entry
            for entry in _proposal_entries(instance, coordinate)
            if status is None or status == entry.status
        ),
    )


def _proposal_entries(
    instance: PlaybillInstance,
    coordinate: PlaybillAcceptedCoordinate,
    proposal_id: str | None = None,
) -> tuple[PlaybillProposalListEntryV1, ...]:
    evidence = instance.proposal_evidence()
    assert evidence.index is not None
    if proposal_id is not None:
        # A selected status is an evidence read, not an inventory-only answer.
        evidence.read_admission(proposal_id)
        evaluation = evidence.read_evaluation(proposal_id)
        if evaluation.candidate_digest is not None:
            evidence.read_candidate(evaluation.candidate_digest)
        evidence.read_withdrawal(proposal_id)
    with evidence.index.read(evidence) as connection:
        if connection.execute("SELECT 1 FROM proposals LIMIT 1").fetchone() is None:
            return ()
    with instance.accepted_history_reader(at=coordinate) as history:
        with evidence.index.read(evidence) as connection:
            generation = connection.execute(
                "SELECT git_oid,semantic_root,generation_root,compiler_digest "
                "FROM accepted_generations WHERE sequence=?",
                (history.sequence,),
            ).fetchone()
            if tuple(generation or ()) != (
                coordinate.git_oid,
                coordinate.semantic_root,
                coordinate.generation_root,
                coordinate.compiler_digest,
            ):
                raise ProposalIntegrityError("proposal inventory history binding differs")
            rows = connection.execute(
                "SELECT p.*, CASE WHEN evaluation_status='refused' THEN 'refused' "
                "WHEN EXISTS (SELECT 1 FROM accepted_generations g "
                "WHERE g.candidate_digest=p.candidate_digest AND g.sequence<=?) THEN 'accepted' "
                "WHEN withdrawal_path IS NOT NULL THEN 'withdrawn' "
                "WHEN candidate_parent_semantic_root=? THEN NULL ELSE 'stale' END AS reason "
                "FROM proposals p "
                + ("WHERE proposal_id=? " if proposal_id is not None else "")
                + "ORDER BY admitted_at_us,proposal_id",
                (history.sequence, coordinate.semantic_root)
                + ((proposal_id,) if proposal_id is not None else ()),
            ).fetchall()
    entries = []
    for row in rows:
        missing: list[ProposalIncompleteReason] = []
        if row["admission_path"] is None:
            missing.append("missing_admission")
        if row["evaluation_status"] == "missing":
            missing.append("missing_evaluation")
        elif (
            row["evaluation_status"] == "candidate"
            and row["candidate_parent_semantic_root"] is None
        ):
            missing.append("missing_candidate")
        entries.append(
            PlaybillProposalListEntryV1(
                proposal_id=row["proposal_id"],
                actor_id=row["actor_id"],
                target_ref=row["target_ref"],
                admitted_at=timestamp(row["admitted_at_us"])
                if row["admitted_at_us"] is not None
                else None,
                verdict=None if row["evaluation_status"] == "missing" else row["evaluation_status"],
                candidate_digest=row["candidate_digest"],
                status="incomplete" if missing else "open" if row["reason"] is None else "settled",
                terminal_reason=None if missing else row["reason"],
                incomplete_reasons=tuple(missing),
                withdrawal_present=row["withdrawal_path"] is not None,
            )
        )
    return tuple(entries)


def service_resolve_playbill_proposal_selector(
    instance: PlaybillInstance,
    *,
    selector: str,
) -> PlaybillProposalSelectorResultV1:
    """Resolve a user selector once to immutable proposal admission evidence."""

    evidence = instance.proposal_evidence()
    assert evidence.index is not None
    rows = evidence.index.rows(
        evidence, "proposal_id>=? AND proposal_id<?", (selector, selector + "\uffff")
    )
    proposal_ids = tuple(row["proposal_id"] for row in rows)
    try:
        resolved = resolve_id_prefix(selector, proposal_ids, marker="sha256:", label="proposal")
    except AmbiguousIdPrefix as exc:
        raise ProposalSelectorAmbiguousError(selector, proposal_ids) from exc
    if resolved in proposal_ids:
        evidence.read_admission(resolved)
        return PlaybillProposalSelectorResultV1(selector=selector, proposal_id=resolved)
    target_oid = instance.proposal_ref_target(selector) if selector.startswith("refs/") else None
    if target_oid is not None:
        current = evidence.index.rows(
            evidence, "candidate_commit_oid=? AND target_ref=?", (target_oid, selector)
        )
        if len(current) == 1:
            resolved = current[0]["proposal_id"]
            evidence.read_admission(resolved)
            return PlaybillProposalSelectorResultV1(selector=selector, proposal_id=resolved)
    # Only the historical ambiguity diagnostic needs every admission for a ref.
    rows = evidence.index.rows(evidence, "target_ref=?", (selector,))
    if rows:
        raise ProposalSelectorAmbiguousError(selector, tuple(row["proposal_id"] for row in rows))
    raise ProposalNotFoundError(selector)


def _proposal_result(instance: PlaybillInstance, proposal_id: str) -> ProposalResult:
    evidence = instance.proposal_evidence()
    admission = evidence.read_admission(proposal_id)
    evaluation = evidence.read_evaluation(admission.proposal_id)
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
            for entry in _proposal_entries(
                instance,
                PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
                proposal_id,
            )
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
            for item in _proposal_entries(
                instance,
                PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
                admission.proposal_id,
            )
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
