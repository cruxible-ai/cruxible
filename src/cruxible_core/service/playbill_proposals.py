"""Deterministic operational reads over proposal evidence and caller identity."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, model_validator

from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.runtime.permissions import PermissionMode

ProposalInventoryStatus = Literal["open", "settled"]
ProposalTerminalReason = Literal["accepted", "refused", "stale"]
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
            if candidate.candidate.parent_semantic_root == coordinate.semantic_root:
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
    "PlaybillWhoAmIV1",
    "CredentialPermissionMode",
    "PrincipalRegistrationStatus",
    "ProposalInventoryStatus",
    "ProposalTerminalReason",
    "WhoAmIActorIdSource",
    "service_list_playbill_proposals",
    "service_playbill_whoami",
]
