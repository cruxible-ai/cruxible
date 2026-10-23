"""Deterministic operational reads over proposal evidence and caller identity."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts.candidates import CandidateRecordAnyVersion
from cruxible_client.contracts.canonical import ProposalDigest
from cruxible_client.contracts.errors import (
    ProposalAdmissionError,
    ProposalIntegrityError,
    ProposalNotFoundError,
    ProposalReadmitAlreadyAccepted,
    ProposalReadmitNotStale,
    ProposalReadmitRefused,
    ProposalReadmitRequiresResubmission,
    ProposalSelectorAmbiguousError,
    ProposalStale,
)
from cruxible_client.contracts.principals import AuthoringRefusal
from cruxible_client.contracts.proposal_models import ProposalReadmissionLink
from cruxible_core.authoring.id_prefixes import AmbiguousIdPrefix, resolve_id_prefix
from cruxible_core.indexes.proposals.proposal_index import timestamp
from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalResult,
    ProposalWithdrawalRecord,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.service.authoring.documents import (
    AcceptedCoordinate,
    ProposalInspection,
)
from cruxible_core.service.change_preview import ChangeMode, change_scope
from cruxible_core.service.identity import authoring_refusal, principal_standing
from cruxible_core.service.list_pages import (
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)

#: ``awaiting_readmit``: a candidate evaluated against a state accepted head has
#: since moved past. Nothing settled it: it waits for its author to readmit it
#: (recompile at the head) or withdraw it, and no approval can settle it as it
#: stands. ``open`` candidates sit on the head and wait for review or activation.
ProposalInventoryStatus = Literal["open", "awaiting_readmit", "settled", "incomplete"]
ProposalIncompleteReason = Literal["missing_admission", "missing_evaluation", "missing_candidate"]
ProposalTerminalReason = Literal["accepted", "refused", "withdrawn", "rejected"]
_UNSETTLED_STATUSES = frozenset({"open", "awaiting_readmit"})
#: Where the actor ID came from. ``runtime_credential`` is the principal the
#: bearer credential is bound to; ``unbound_credential`` is a credential that
#: acts as no principal (no actor ID); ``principal_claim`` is an auth-off
#: daemon's configured principal ID, a claim of identity, not authentication.
WhoAmIActorIdSource = Literal[
    "runtime_credential", "unbound_credential", "principal_claim", "local_operator"
]
_CREDENTIAL_SOURCES = frozenset({"runtime_credential", "unbound_credential"})
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
        if (self.status in _UNSETTLED_STATUSES) != (self.terminal_reason is None):
            raise ValueError("unsettled proposal status and terminal reason disagree")
        if self.verdict == "refused" and self.terminal_reason != "refused":
            raise ValueError("refused evaluation must be a settled refusal")
        return self


class PlaybillProposalListV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-list-v1"] = "playbill-proposal-list-v1"
    coordinate: AcceptedCoordinate
    status_filter: ProposalInventoryStatus | None = None
    entries: tuple[PlaybillProposalListEntryV1, ...]
    truncated: bool = False
    next_cursor: str | None = None


class PlaybillProposalReadmitResultV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-readmit-result-v1"] = "playbill-proposal-readmit-result-v1"
    #: ``readmitted``: ``proposal`` is the readmission (or its preview).
    #: ``already_live``: every change the stale proposal carried is live at the
    #: head, so nothing was proposed: the source is closed as answered (a
    #: preview closes nothing) and ``proposal`` is absent.
    status: Literal["readmitted", "already_live"] = Field(
        default="readmitted", exclude_if=lambda value: value == "readmitted"
    )
    source_proposal_id: str
    operation_digest: str
    proposal: ProposalInspection | None
    #: The accepted coordinate after the call (``already_live`` only; a
    #: readmission's own inspection carries it otherwise).
    accepted_coordinate: AcceptedCoordinate | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _outcome_shape(self) -> "PlaybillProposalReadmitResultV1":
        answered = self.status == "already_live"
        if answered != (self.proposal is None) or answered != (
            self.accepted_coordinate is not None
        ):
            raise ValueError("a readmission names its proposal exactly when one was proposed")
        return self


class PlaybillProposalWithdrawResultV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-withdraw-result-v1"] = "playbill-proposal-withdraw-result-v1"
    #: ``would_withdraw`` answers a preview, which recorded nothing (R12).
    status: Literal["withdrawn", "would_withdraw"] = "withdrawn"
    proposal_id: str
    actor_id: str
    reason: str
    withdrawn_at: str
    already_withdrawn: bool = False
    #: The accepted coordinate the withdrawal was checked at; ``at`` pins a commit.
    coordinate: AcceptedCoordinate | None = None


class PlaybillProposalRejectResultV1(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-reject-result-v1"] = "playbill-proposal-reject-result-v1"
    #: ``would_reject`` answers a preview, which recorded nothing (R12).
    status: Literal["rejected", "would_reject"] = "rejected"
    proposal_id: str
    actor_id: str
    reason: str
    rejected_at: str
    already_rejected: bool = False
    #: The accepted coordinate the rejection was checked at; ``at`` pins a commit.
    coordinate: AcceptedCoordinate | None = None


class ProposalSelectorResult(_StrictOperationalReadModel):
    tag: Literal["playbill-proposal-selector-result-v1"] = "playbill-proposal-selector-result-v1"
    selector: str
    proposal_id: str


class PlaybillWhoAmIV1(_StrictOperationalReadModel):
    tag: Literal["playbill-whoami-v1"] = "playbill-whoami-v1"
    # None only for an unbound credential, which acts as no principal.
    actor_id: str | None
    # The credential's description; never the source of the actor ID.
    credential_label: str | None
    actor_id_source: WhoAmIActorIdSource
    # False when no bearer credential backs the identity: an auth-off daemon
    # trusts every process of its OS user equally, so the actor ID is a claim.
    authenticated: bool
    credential_permission_mode: CredentialPermissionMode
    # None when the request names no principal (an unbound credential).
    principal_registration_status: PrincipalRegistrationStatus | None
    active_principal_ids: tuple[str, ...]
    coordinate: AcceptedCoordinate
    # Whether authoring compile would accept this actor, and the refusal it
    # would return otherwise: the same code, detail and repair.
    can_author: bool
    authoring_refusal: AuthoringRefusal | None

    @model_validator(mode="after")
    def _credential_binding(self) -> "PlaybillWhoAmIV1":
        if (self.actor_id_source in _CREDENTIAL_SOURCES) != self.authenticated:
            raise ValueError("only a runtime credential authenticates the actor")
        if (self.actor_id_source == "unbound_credential") != (self.actor_id is None):
            raise ValueError("exactly an unbound credential names no actor")
        if (self.actor_id is None) != (self.principal_registration_status is None):
            raise ValueError("a registration status belongs to a named actor")
        if self.can_author != (self.authoring_refusal is None):
            raise ValueError("an actor that cannot author carries exactly its refusal")
        return self


def service_list_playbill_proposals(
    instance: PlaybillInstance,
    *,
    status: ProposalInventoryStatus | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> PlaybillProposalListV1:
    """Reduce immutable proposal evidence against the current accepted coordinate.

    ``limit`` bounds the page; ``None`` is the unbounded internal read. A
    cursor continues its first page at that page's accepted coordinate, so
    statuses do not shift between pages.
    """

    selection = {"status": status}
    continuation = (
        None
        if cursor is None
        else decode_list_cursor(cursor, list_name=_PROPOSAL_LIST, selection=selection)
    )
    coordinate = (
        AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        if continuation is None
        else AcceptedCoordinate.model_validate(continuation.coordinate)
    )
    entries = tuple(
        entry
        for entry in _proposal_entries(instance, coordinate)
        if status is None or status == entry.status
    )
    # The inventory is operational evidence, not only accepted state: a
    # withdrawal or admission between pages changes the listing, so the cursor
    # pins a digest of every entry and a changed listing is refused as stale.
    snapshot = list_snapshot([entry.model_dump(mode="json") for entry in entries])
    page, truncated = page_after_boundary(
        entries,
        keys=tuple((entry.proposal_id,) for entry in entries),
        snapshot=snapshot,
        continuation=continuation,
        limit=len(entries) if limit is None else limit,
        list_name=_PROPOSAL_LIST,
    )
    return PlaybillProposalListV1(
        coordinate=coordinate,
        status_filter=status,
        entries=page,
        truncated=truncated,
        next_cursor=(
            encode_list_cursor(
                list_name=_PROPOSAL_LIST,
                coordinate=coordinate.model_dump(mode="json"),
                selection=selection,
                snapshot=snapshot,
                last_key=(page[-1].proposal_id,),
            )
            if truncated and page
            else None
        ),
    )


_PROPOSAL_LIST = "proposal"


def service_playbill_proposal_status(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
) -> PlaybillProposalListEntryV1:
    """The one proposal's list entry at the current accepted coordinate, read by ID.

    It is the list's own entry for that ID, so retained partial evidence answers
    ``incomplete`` with its reasons rather than refusing; whatever evidence is
    present is still read and authenticated.
    """

    evidence = instance.proposal_evidence()
    resolved = evidence.resolve_proposal_id(proposal_id)
    try:
        ProposalDigest.from_tagged(resolved)
    except ValueError as exc:
        raise ProposalNotFoundError(proposal_id) from exc
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    entry = next(
        (
            item
            for item in _proposal_entries(instance, coordinate, resolved, require_evidence=False)
            if item.proposal_id == resolved
        ),
        None,
    )
    if entry is None:
        raise ProposalNotFoundError(proposal_id)
    missing = set(entry.incomplete_reasons)
    if "missing_admission" not in missing:
        evidence.read_admission(resolved)
    if "missing_evaluation" not in missing:
        evaluation = evidence.read_evaluation(resolved)
        if evaluation.candidate_digest is not None and "missing_candidate" not in missing:
            evidence.read_candidate(evaluation.candidate_digest)
    if entry.withdrawal_present:
        evidence.read_withdrawal(resolved)
    return entry


def open_proposal_count(instance: PlaybillInstance) -> int:
    """How many proposals ``proposal list --status open`` shows, counted on the open locators.

    Open is a complete candidate (admission retained), neither refused,
    withdrawn nor accepted by the coordinate's own history sequence, whose
    parent is the coordinate's semantic root. The two locators of
    ``_open_candidates`` keep the listing's snapshot: a proposal accepted after
    the bound sequence is still open at it. Settled history is never visited.
    """

    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    with _bound_inventory(instance, coordinate) as bound:
        if bound is None:
            return 0
        connection, sequence = bound
        return len(
            _open_candidates(
                connection,
                sequence,
                columns="proposal_id,admitted_at_us",
                where="candidate_parent_semantic_root=?",
                parameters=(coordinate.semantic_root,),
            )
        )


def open_proposal_at_target(
    instance: PlaybillInstance, target_ref: str
) -> PlaybillProposalListEntryV1 | None:
    """The open proposal under one exact target ref, if any: a dedupe by locator.

    The same entry ``proposal list --status open`` would show for it, read
    through the target-ref locator rather than the whole inventory.
    """

    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    return next(
        (
            entry
            for entry in _proposal_entries(instance, coordinate, target_ref=target_ref)
            if entry.status == "open"
        ),
        None,
    )


def _proposal_entries(
    instance: PlaybillInstance,
    coordinate: AcceptedCoordinate,
    proposal_id: str | None = None,
    *,
    require_evidence: bool = True,
    target_ref: str | None = None,
) -> tuple[PlaybillProposalListEntryV1, ...]:
    """List entries, or one ID's, or one target ref's; ``require_evidence`` makes that
    ID's records mandatory."""
    evidence = instance.proposal_evidence()
    if proposal_id is not None and require_evidence:
        # A selected status is an evidence read, not an inventory-only answer.
        evidence.read_admission(proposal_id)
        evaluation = evidence.read_evaluation(proposal_id)
        if evaluation.candidate_digest is not None:
            evidence.read_candidate(evaluation.candidate_digest)
        evidence.read_withdrawal(proposal_id)
    with _bound_inventory(instance, coordinate) as bound:
        if bound is None:
            return ()
        connection, sequence = bound
        rows = connection.execute(
            "SELECT p.*, CASE WHEN evaluation_status='refused' THEN 'refused' "
            "WHEN accepted_sequence<=? THEN 'accepted' "
            "WHEN withdrawal_path IS NOT NULL THEN 'withdrawn' "
            "WHEN candidate_parent_semantic_root=? THEN NULL ELSE 'stale' END AS reason "
            "FROM proposals p "
            + (
                "WHERE proposal_id=? "
                if proposal_id is not None
                else "INDEXED BY proposals_by_target WHERE target_ref=? "
                if target_ref is not None
                else ""
            )
            + "ORDER BY admitted_at_us,proposal_id",
            (sequence, coordinate.semantic_root)
            + (
                (proposal_id,)
                if proposal_id is not None
                else (target_ref,)
                if target_ref is not None
                else ()
            ),
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
        reason = row["reason"]
        # A moved base settles nothing: the candidate awaits its author's readmit.
        awaiting_readmit = reason == "stale"
        if awaiting_readmit:
            reason = None
        if reason == "withdrawn":
            # One closing record carries both endings; its kind tells them apart.
            closed = evidence.read_withdrawal(row["proposal_id"])
            if closed is not None and closed.kind == "rejected":
                reason = "rejected"
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
                status=(
                    "incomplete"
                    if missing
                    else "awaiting_readmit"
                    if awaiting_readmit
                    else "open"
                    if reason is None
                    else "settled"
                ),
                terminal_reason=None if missing else reason,
                incomplete_reasons=tuple(missing),
                withdrawal_present=row["withdrawal_path"] is not None,
            )
        )
    return tuple(entries)


@contextmanager
def _bound_inventory(
    instance: PlaybillInstance,
    coordinate: AcceptedCoordinate,
) -> Iterator[tuple[sqlite3.Connection, int] | None]:
    """One proposal-index read bound to the history sequence of `coordinate`.

    Yields None for an instance that has never admitted a proposal.
    """

    evidence = instance.proposal_evidence()
    assert evidence.index is not None
    with evidence.index.read(evidence) as connection:
        if connection.execute("SELECT 1 FROM proposals LIMIT 1").fetchone() is None:
            yield None
            return
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
            yield connection, history.sequence


def _open_candidates(
    connection: sqlite3.Connection,
    sequence: int,
    *,
    columns: str,
    where: str,
    parameters: tuple[str, ...],
) -> list[sqlite3.Row]:
    """Admitted, unwithdrawn candidates that history at `sequence` had not settled.

    Two locators, so settled candidates are never enumerated: the open locator
    holds candidates no generation has settled, and the acceptance locator adds
    those settled only after `sequence` -- empty at the head, and bounded by the
    history after an older coordinate rather than by all of it.
    """

    open_rows = (
        f"SELECT {columns} FROM proposals WHERE evaluation_status='candidate' "
        "AND withdrawal_path IS NULL AND admission_path IS NOT NULL "
        f"AND {{settled}} AND {where}"
    )
    return connection.execute(
        open_rows.format(settled="accepted_sequence IS NULL")
        + " UNION ALL "
        + open_rows.format(settled="accepted_sequence>?")
        + " ORDER BY admitted_at_us,proposal_id",
        (*parameters, sequence, *parameters),
    ).fetchall()


@dataclass(frozen=True)
class StaleProposal:
    """A candidate evaluated against a state accepted head has since moved past."""

    proposal_id: str
    actor_id: str
    target_ref: str
    admitted_at: str
    candidate_parent_semantic_root: str
    candidate_digest: str


def readmission_operation_digest(proposal_id: str, coordinate: AcceptedCoordinate) -> str:
    """The one readmission of `proposal_id` that `coordinate` admits."""

    return ProposalReadmissionLink(
        source_proposal_id=proposal_id, coordinate=coordinate
    ).operation_digest


_READMISSION_REF = "refs/proposals/{actor_id}/readmit-"


def _readmission_target_ref(actor_id: str, operation_digest: str) -> str:
    return (
        _READMISSION_REF.format(actor_id=actor_id) + operation_digest.removeprefix("sha256:")[:24]
    )


@dataclass(frozen=True)
class ProposalReadmission:
    """A proposal that readmitted another: the supersession link, as its admission records it.

    ``accepted``: history at the read coordinate settled it, so the source's
    change is accepted under this proposal id. Otherwise it is a candidate
    nobody withdrew, which still carries the change as an open or a stale
    proposal of its own.
    """

    proposal_id: str
    accepted: bool


@dataclass(frozen=True)
class _LinkedReadmission:
    proposal_id: str
    link: ProposalReadmissionLink
    evaluation_status: str
    withdrawn: bool
    accepted_sequence: int | None


def _readmissions_by_source(
    instance: PlaybillInstance, connection: sqlite3.Connection, actor_id: str
) -> dict[str, tuple[_LinkedReadmission, ...]]:
    """The author's readmissions, keyed by the stale proposal each re-admits.

    The readmission ref is only where to look. What links a readmission to
    its source is the ``readmits`` record on its own immutable admission,
    which only the readmit service writes and which is bound to the
    admission's identity through ``source_compilation_digest``. A proposal
    that merely spells a readmission ref links nothing, and the link does not
    depend on the head its evaluation happened to read.
    """

    prefix = _READMISSION_REF.format(actor_id=actor_id)
    rows = connection.execute(
        "SELECT proposal_id,evaluation_status,withdrawal_path,accepted_sequence FROM proposals "
        "WHERE target_ref>=? AND target_ref<? AND actor_id=? AND admission_path IS NOT NULL "
        "ORDER BY admitted_at_us,proposal_id",
        (prefix, prefix + "\uffff", actor_id),
    ).fetchall()
    evidence = instance.proposal_evidence()
    found: dict[str, list[_LinkedReadmission]] = {}
    for row in rows:
        link = evidence.read_admission(row["proposal_id"]).readmits
        if link is None:
            continue
        found.setdefault(link.source_proposal_id, []).append(
            _LinkedReadmission(
                proposal_id=row["proposal_id"],
                link=link,
                evaluation_status=row["evaluation_status"],
                withdrawn=row["withdrawal_path"] is not None,
                accepted_sequence=row["accepted_sequence"],
            )
        )
    return {source: tuple(items) for source, items in found.items()}


def _carrying_readmission(
    readmissions: Sequence[_LinkedReadmission], sequence: int
) -> ProposalReadmission | None:
    """The readmission that now carries a source's change, at history `sequence`.

    An accepted one wins, then a live one (a candidate nobody withdrew). A
    readmission that was refused or withdrawn carries nothing.
    """

    live: ProposalReadmission | None = None
    for item in readmissions:
        if item.accepted_sequence is not None and item.accepted_sequence <= sequence:
            return ProposalReadmission(item.proposal_id, accepted=True)
        if live is None and item.evaluation_status == "candidate" and not item.withdrawn:
            live = ProposalReadmission(item.proposal_id, accepted=False)
    return live


def proposal_readmission(
    instance: PlaybillInstance,
    coordinate: AcceptedCoordinate,
    proposal_id: str,
) -> ProposalReadmission | None:
    """The readmission carrying `proposal_id`'s change at `coordinate`, if any."""

    actor_id = instance.proposal_evidence().read_admission(proposal_id).actor_id
    with _bound_inventory(instance, coordinate) as bound:
        if bound is None:
            return None
        connection, sequence = bound
        linked = _readmissions_by_source(instance, connection, actor_id)
        return _carrying_readmission(linked.get(proposal_id, ()), sequence)


def stale_unreadmitted_proposals(
    instance: PlaybillInstance,
    coordinate: AcceptedCoordinate,
    *,
    actor_id: str | None = None,
) -> tuple[StaleProposal, ...]:
    """The inventory's stale proposals nobody has withdrawn or readmitted.

    Exactly `proposal list`'s `awaiting_readmit` status -- a candidate neither
    refused, accepted nor withdrawn whose parent semantic root is not the
    coordinate's -- read through the open-parent locator rather than the whole
    inventory; ``actor_id`` keeps only that author's. A proposal readmitted at
    this coordinate is answered. So is one whose readmission still carries its
    change: that readmission was accepted -- the change landed, under another
    proposal id -- or is a live proposal that answers for itself. Every link
    is the ``readmits`` record on the readmission's own admission
    (``_readmissions_by_source``), never a ref's spelling.
    """

    with _bound_inventory(instance, coordinate) as bound:
        if bound is None:
            return ()
        connection, sequence = bound
        where = "candidate_parent_semantic_root IS NOT NULL AND candidate_parent_semantic_root!=?"
        parameters: tuple[str, ...] = (coordinate.semantic_root,)
        if actor_id is not None:
            where += " AND actor_id=?"
            parameters += (actor_id,)
        rows = _open_candidates(
            connection,
            sequence,
            columns=(
                "proposal_id,actor_id,target_ref,admitted_at_us,"
                "candidate_parent_semantic_root,candidate_digest"
            ),
            where=where,
            parameters=parameters,
        )
        stale = []
        linked_by_actor: dict[str, dict[str, tuple[_LinkedReadmission, ...]]] = {}
        for row in rows:
            if row["actor_id"] not in linked_by_actor:
                linked_by_actor[row["actor_id"]] = _readmissions_by_source(
                    instance, connection, row["actor_id"]
                )
            linked = linked_by_actor[row["actor_id"]].get(row["proposal_id"], ())
            here = readmission_operation_digest(row["proposal_id"], coordinate)
            # Readmitted at this coordinate (whatever its verdict: readmit
            # answers that one again), or carried by a readmission elsewhere.
            if any(item.link.operation_digest == here for item in linked):
                continue
            if _carrying_readmission(linked, sequence):
                continue
            stale.append(
                StaleProposal(
                    proposal_id=row["proposal_id"],
                    actor_id=row["actor_id"],
                    target_ref=row["target_ref"],
                    admitted_at=timestamp(row["admitted_at_us"]),
                    candidate_parent_semantic_root=row["candidate_parent_semantic_root"],
                    candidate_digest=row["candidate_digest"],
                )
            )
    return tuple(stale)


@dataclass(frozen=True)
class ProposalAwaitingApproval:
    """An open candidate one more eligible signer's approval would advance."""

    proposal_id: str
    actor_id: str
    target_ref: str
    admitted_at: str
    candidate_digest: str
    minimum_distinct_signers: int
    eligible_approvals: int


def _signature_required_unsigned(
    candidate: CandidateRecordAnyVersion,
    signers: set[str],
    ordinary_principal_ids: frozenset[str],
) -> bool:
    """Whether settlement still needs a signature the approval policy does not ask for.

    A compiler upgrade settles only with at least one signed client approval,
    whatever the approval policy says (settlement refuses "compiler upgrade
    requires a signed client approval"). Under a policy with no independent
    requirement, the creator's own signature counts, as settlement counts it.
    """

    return any(member.artifact_kind == "compiler-upgrade" for member in candidate.members) and not (
        signers & ordinary_principal_ids
    )


def proposals_awaiting_approval(
    instance: PlaybillInstance,
    coordinate: AcceptedCoordinate,
    *,
    principal_id: str,
    ordinary_principal_ids: frozenset[str],
) -> tuple[ProposalAwaitingApproval, ...]:
    """Open candidates at `coordinate` whose approval `principal_id` could supply.

    Read through the open-parent locator: candidates neither refused, accepted
    nor withdrawn whose parent is the coordinate's semantic root, so approval
    is what stands between them and activation. A candidate qualifies while its
    approval requirement is unmet -- counted exactly as activation counts it,
    over distinct active ordinary signers other than its creator -- and this
    principal is such a signer who has not signed it yet. A compiler upgrade
    also qualifies while it carries no signature at all (settlement requires
    one even when the policy does not), and then its creator may give it.
    `ordinary_principal_ids` is the coordinate's active ordinary registry.
    """

    if principal_id not in ordinary_principal_ids:
        return ()
    evidence = instance.proposal_evidence()
    with _bound_inventory(instance, coordinate) as bound:
        if bound is None:
            return ()
        connection, sequence = bound
        rows = _open_candidates(
            connection,
            sequence,
            columns="proposal_id,actor_id,target_ref,admitted_at_us,candidate_digest",
            where="candidate_parent_semantic_root=?",
            parameters=(coordinate.semantic_root,),
        )
    awaiting = []
    for row in rows:
        counts = _approval_awaited(
            evidence,
            row["candidate_digest"],
            actor_id=row["actor_id"],
            principal_id=principal_id,
            ordinary_principal_ids=ordinary_principal_ids,
        )
        if counts is None:
            continue
        eligible, minimum = counts
        awaiting.append(
            ProposalAwaitingApproval(
                proposal_id=row["proposal_id"],
                actor_id=row["actor_id"],
                target_ref=row["target_ref"],
                admitted_at=timestamp(row["admitted_at_us"]),
                candidate_digest=row["candidate_digest"],
                minimum_distinct_signers=minimum,
                eligible_approvals=eligible,
            )
        )
    return tuple(awaiting)


def _approval_awaited(
    evidence: ProposalEvidenceStore,
    candidate_digest: str,
    *,
    actor_id: str,
    principal_id: str,
    ordinary_principal_ids: frozenset[str],
) -> tuple[int, int] | None:
    """(eligible approvals, minimum) when `principal_id`'s approval would advance it, else None.

    Counted exactly as activation counts it: distinct active ordinary signers
    other than the creator; a compiler upgrade with no signature at all also
    waits on one, which its creator may give.
    """

    candidate = evidence.read_candidate(candidate_digest)
    signers = {
        submission.attestation.signer_id for submission in evidence.read_approvals(candidate_digest)
    }
    if principal_id in signers:
        return None
    if candidate.approval_requirements:
        if actor_id == principal_id:
            return None  # The creator never counts toward an independent requirement.
        eligible = len((signers - {actor_id}) & ordinary_principal_ids)
        minimum = candidate.approval_requirements[0].minimum_distinct_signers
        return None if eligible >= minimum else (eligible, minimum)
    if _signature_required_unsigned(candidate, signers, ordinary_principal_ids):
        return 0, 1
    return None


def stale_proposals_awaiting_approval(
    instance: PlaybillInstance,
    coordinate: AcceptedCoordinate,
    *,
    principal_id: str,
    ordinary_principal_ids: frozenset[str],
) -> tuple[tuple[StaleProposal, int, int], ...]:
    """Stale proposals `principal_id` would approve, were they at the head: the approver's view.

    Exactly ``stale_unreadmitted_proposals`` (``proposal list``'s
    ``awaiting_readmit``), kept where this principal's approval is one the
    candidate still awaits, counted as ``proposals_awaiting_approval`` counts
    it. No approval can settle one as it stands: it awaits its author's
    readmit, and only then this principal's review. Each comes with its
    (eligible approvals, minimum). O(stale open candidates), as the listing.
    """

    if principal_id not in ordinary_principal_ids:
        return ()
    evidence = instance.proposal_evidence()
    found = []
    for proposal in stale_unreadmitted_proposals(instance, coordinate):
        counts = _approval_awaited(
            evidence,
            proposal.candidate_digest,
            actor_id=proposal.actor_id,
            principal_id=principal_id,
            ordinary_principal_ids=ordinary_principal_ids,
        )
        if counts is not None:
            found.append((proposal, *counts))
    return tuple(found)


@dataclass(frozen=True)
class ProposalAwaitingActivation:
    """One of a principal's own open candidates whose approval requirement is met."""

    proposal_id: str
    target_ref: str
    admitted_at: str
    candidate_digest: str
    approvals: int
    #: Whether its approval policy asked for any approval (met), or none at all.
    approval_required: bool


def proposals_awaiting_activation(
    instance: PlaybillInstance,
    coordinate: AcceptedCoordinate,
    *,
    principal_id: str,
    ordinary_principal_ids: frozenset[str],
) -> tuple[ProposalAwaitingActivation, ...]:
    """The principal's own open candidates at `coordinate` that activation is next for.

    Read through the open-parent locator, like ``proposals_awaiting_approval``:
    candidates neither refused, accepted nor withdrawn whose parent is the
    coordinate's semantic root. A candidate qualifies when it needs no approval
    or its requirement is met, counted exactly as activation counts it. A
    principal-lifecycle candidate is the actor's own signature to give first,
    and a compiler upgrade with no signature yet needs one before settlement,
    so neither is listed here until then.
    """

    evidence = instance.proposal_evidence()
    with _bound_inventory(instance, coordinate) as bound:
        if bound is None:
            return ()
        connection, sequence = bound
        rows = _open_candidates(
            connection,
            sequence,
            columns="proposal_id,actor_id,target_ref,admitted_at_us,candidate_digest",
            where="candidate_parent_semantic_root=? AND actor_id=?",
            parameters=(coordinate.semantic_root, principal_id),
        )
    ready = []
    for row in rows:
        candidate = evidence.read_candidate(row["candidate_digest"])
        if candidate.members and all(
            member.artifact_kind == "principal-lifecycle" for member in candidate.members
        ):
            continue
        signers = {
            submission.attestation.signer_id
            for submission in evidence.read_approvals(row["candidate_digest"])
        }
        eligible = len((signers - {row["actor_id"]}) & ordinary_principal_ids)
        if candidate.approval_requirements and (
            eligible < candidate.approval_requirements[0].minimum_distinct_signers
        ):
            continue
        if _signature_required_unsigned(candidate, signers, ordinary_principal_ids):
            # Settlement needs a signature first: the approval row offers it.
            continue
        ready.append(
            ProposalAwaitingActivation(
                proposal_id=row["proposal_id"],
                target_ref=row["target_ref"],
                admitted_at=timestamp(row["admitted_at_us"]),
                candidate_digest=row["candidate_digest"],
                approvals=eligible,
                approval_required=bool(candidate.approval_requirements),
            )
        )
    return tuple(ready)


def service_resolve_playbill_proposal_selector(
    instance: PlaybillInstance,
    *,
    selector: str,
) -> ProposalSelectorResult:
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
        return ProposalSelectorResult(selector=selector, proposal_id=resolved)
    target_oid = instance.proposal_ref_target(selector) if selector.startswith("refs/") else None
    if target_oid is not None:
        current = evidence.index.rows(
            evidence, "candidate_commit_oid=? AND target_ref=?", (target_oid, selector)
        )
        if len(current) == 1:
            resolved = current[0]["proposal_id"]
            evidence.read_admission(resolved)
            return ProposalSelectorResult(selector=selector, proposal_id=resolved)
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
    dry_run: bool | None = None,
    at: str | None = None,
) -> PlaybillProposalReadmitResultV1:
    """Recompile retained input, or replay an exact-byte origin, at the head.

    ``dry_run`` evaluates the readmission on the admission path and admits
    nothing (R12); ``at`` pins a commit to the head the preview saw.
    """

    with change_scope(
        instance,
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="cruxible.proposal.readmit",
        describe=f"readmitting proposal {proposal_id}",
    ) as mode:
        return _readmit(instance, mode, proposal_id=proposal_id, actor_id=actor_id)


def _readmit(
    instance: PlaybillInstance, mode: ChangeMode, *, proposal_id: str, actor_id: str
) -> PlaybillProposalReadmitResultV1:
    source = _proposal_result(instance, proposal_id)
    if source.admission.actor_id != actor_id:
        raise ProposalAdmissionError("only the source proposal actor may readmit it")
    from cruxible_core.service.proposals.recompile import recompiled_successor

    successor = recompiled_successor(instance, source)
    if successor.admission.proposal_id != source.admission.proposal_id:
        status = service_playbill_proposal_status(
            instance, proposal_id=successor.admission.proposal_id
        )
        if status.terminal_reason == "accepted":
            raise ProposalReadmitAlreadyAccepted(
                proposal_id, accepted_as=successor.admission.proposal_id
            )
        link = successor.admission.readmits
        assert link is not None
        return PlaybillProposalReadmitResultV1(
            source_proposal_id=proposal_id,
            operation_digest=link.operation_digest,
            proposal=ProposalInspection(proposal=successor, accepted_coordinate=link.coordinate),
        )
    # A stale proposal may be withdrawn instead of readmitted, and that is the
    # actor saying this tree will never be settled. Readmitting it would settle
    # exactly that tree, under a new proposal id, so it refuses here.
    instance.proposal_evidence().refuse_closed(source.admission.proposal_id)
    source_status = next(
        (
            entry
            for entry in _proposal_entries(
                instance,
                AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
                proposal_id,
            )
            if entry.proposal_id == proposal_id
        ),
        None,
    )
    if source_status is None:  # pragma: no cover - a read admission always lists
        raise ProposalNotFoundError(proposal_id)
    if source_status.terminal_reason == "accepted":
        raise ProposalReadmitAlreadyAccepted(proposal_id)
    if source_status.status != "awaiting_readmit":
        raise ProposalReadmitNotStale(
            proposal_id, status=source_status.terminal_reason or source_status.status
        )
    carried = proposal_readmission(
        instance,
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        proposal_id,
    )
    if carried is not None and carried.accepted:
        raise ProposalReadmitAlreadyAccepted(proposal_id, accepted_as=carried.proposal_id)
    from cruxible_core.service.proposals.recompile import recompile_proposal

    try:
        recompiled = recompile_proposal(
            instance, source, preview=mode.previewing, confirm_head=mode.confirm_head
        )
    except ProposalStale as exc:
        if exc.reason == "cruxible.authoring.slot_membership_changed":
            raise ProposalReadmitRequiresResubmission(
                f"Pinned slot membership changed ({exc.reason}); {exc.detail}. "
                "Run the write again at the current head."
            ) from exc
        raise readmit_refused(instance, source, exc) from exc
    if recompiled is not None:
        link = ProposalReadmissionLink(
            source_proposal_id=proposal_id, coordinate=recompiled.coordinate
        )
        if recompiled.proposal is None:
            return PlaybillProposalReadmitResultV1(
                status="already_live",
                source_proposal_id=proposal_id,
                operation_digest=link.operation_digest,
                proposal=None,
                accepted_coordinate=recompiled.coordinate,
            )
        return PlaybillProposalReadmitResultV1(
            source_proposal_id=proposal_id,
            operation_digest=link.operation_digest,
            proposal=ProposalInspection(
                status="would_propose" if mode.previewing else "admitted",
                proposal=recompiled.proposal,
                accepted_coordinate=recompiled.coordinate,
            ),
        )
    if (
        source.candidate is not None
        and len(source.candidate.members) > 1
        and any(member.artifact_kind == "claim-type" for member in source.candidate.members)
    ):
        raise ProposalReadmitRequiresResubmission()
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    link = ProposalReadmissionLink(source_proposal_id=proposal_id, coordinate=coordinate)
    operation_digest = link.operation_digest
    # Only the source's author may readmit, and the readmit service admits under
    # exactly this ref, so the link's admissions are found at it (the record's
    # `readmits` still decides): never by reading every admission retained.
    matching = tuple(
        admission
        for admission in instance.proposal_evidence().admissions_at_target(
            _readmission_target_ref(actor_id, operation_digest)
        )
        if admission.readmits == link
    )
    if len(matching) > 1:
        raise ProposalIntegrityError("readmission operation digest names multiple admissions")
    if matching:
        result = _proposal_result(instance, matching[0].proposal_id)
    else:
        generation = instance.accepted_history()[-1]
        if generation.record is None:  # pragma: no cover - stale source requires a successor
            raise ProposalIntegrityError("readmission requires an accepted candidate timestamp")
        actor = AuthenticatedActor(actor_id=actor_id)
        request = ProposalAdmissionRequest(
            target_ref=_readmission_target_ref(actor_id, operation_digest),
            # A successful admission retains its evaluated (possibly rebased)
            # tree, which carries the records of its evaluated base. Replaying
            # that tree against the original proposed base would treat those
            # daemon-written records as authored edits on the next readmission.
            # Refused admissions instead retain the original submitted tree.
            proposed_base_oid=(
                source.evaluation.evaluated_base_oid
                if source.evaluation.evaluated_tree_oid is not None
                else source.admission.proposed_base_oid
            ),
            source_compilation_digest=operation_digest,
            claim_type_expansions=source.admission.claim_type_expansions,
        )
        candidate_tree = instance.proposal_tree(
            source.admission.candidate_tree_oid, proposal_id=proposal_id
        )
        if mode.previewing:
            preview = instance.proposal_service().preview(
                actor=actor,
                request=request,
                candidate_tree=candidate_tree,
                timestamp=generation.record.candidate.timestamp,
                readmits=link,
            )
            return PlaybillProposalReadmitResultV1(
                source_proposal_id=proposal_id,
                operation_digest=operation_digest,
                proposal=ProposalInspection(
                    status="would_propose" if preview.candidate is not None else "would_block",
                    proposal=preview,
                    accepted_coordinate=coordinate,
                ),
            )
        result = instance.proposal_service().submit(
            actor=actor,
            request=request,
            candidate_tree=candidate_tree,
            timestamp=generation.record.candidate.timestamp,
            readmits=link,
            confirm_head=mode.confirm_head,
        )
    return PlaybillProposalReadmitResultV1(
        source_proposal_id=proposal_id,
        operation_digest=operation_digest,
        proposal=ProposalInspection(
            proposal=result,
            workspace_advertisement=result.workspace_advertisement,
            accepted_coordinate=coordinate,
        ),
    )


def readmit_refused(
    instance: PlaybillInstance,
    source: ProposalResult,
    refusal: ProposalStale,
    *,
    approver: bool = False,
) -> ProposalReadmitRefused:
    """A refused recompilation, with the real exit named by where the change came from.

    Readmitting again would refuse the same way, so the repair is never
    readmit: a Line's next run, the Procedure run again, the write made again,
    or (always) withdraw. ``approver``: the caller is reviewing another actor's
    proposal, whose exit is to reject it with a reason. One run's records are
    read to name the origin, on this refusal path only.
    """
    from cruxible_core.service.proposals.procedure_recompile import recompile_origin
    from cruxible_core.service.proposals.recompile import lowering_origin

    if isinstance(refusal, ProposalReadmitRefused):
        return refusal
    try:
        origin = lowering_origin(instance, source)
    except ProposalStale:
        origin = None
    kind, name = ("procedure", None) if origin is None else recompile_origin(instance, origin)
    return ProposalReadmitRefused(
        source.admission.proposal_id,
        reason=refusal.reason or "lowering_refused",
        detail=refusal.detail,
        origin=kind,
        origin_name=name,
        approver=approver,
    )


def service_withdraw_playbill_proposal(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    actor_id: str,
    reason: str,
    withdrawn_at: str,
    unscoped_operator: bool = False,
    dry_run: bool | None = None,
    at: str | None = None,
) -> PlaybillProposalWithdrawResultV1:
    """Record one actor's terminal statement that a proposal will not be settled.

    ``dry_run`` runs every check and records nothing (``would_withdraw``, R12).

    A proposal whose activation a hard limit refuses -- the ledger's change-set
    record ceiling is the case this exists for -- is admitted, evaluated and
    permanently unactivatable, and nothing removed it from the open inventory.
    Withdrawal is the missing terminal transition: it touches no accepted state
    and retains the outcome record while releasing candidate refs. It moves the proposal out
    of `proposal list --status open` where an actor reads their work, and every
    settlement door refuses a proposal that carries one.

    Only an OPEN proposal is withdrawable. A settled one already has its terminal
    reason -- accepted or refused -- and overwriting that with a statement of
    intent would lose the outcome. A proposal awaiting readmit (its base moved)
    has no ending yet, so it may be withdrawn instead of readmitted.

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

    with change_scope(
        instance,
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="cruxible.proposal.withdraw",
        describe=f"withdrawing proposal {proposal_id}",
    ) as mode:
        return _withdraw(
            instance,
            mode,
            proposal_id=proposal_id,
            actor_id=actor_id,
            reason=reason,
            withdrawn_at=withdrawn_at,
            unscoped_operator=unscoped_operator,
        )


def _withdraw(
    instance: PlaybillInstance,
    mode: ChangeMode,
    *,
    proposal_id: str,
    actor_id: str,
    reason: str,
    withdrawn_at: str,
    unscoped_operator: bool,
) -> PlaybillProposalWithdrawResultV1:
    admission = instance.proposal_evidence().read_admission(proposal_id)
    if admission.actor_id != actor_id and not unscoped_operator:
        raise ProposalAdmissionError(
            "only the source proposal actor, or a daemon-wide operator, may withdraw it"
        )
    record, already, coordinate = _close(
        instance,
        mode,
        ProposalWithdrawalRecord(
            proposal_id=admission.proposal_id,
            actor_id=actor_id,
            reason=reason,
            withdrawn_at=withdrawn_at,
        ),
    )
    return PlaybillProposalWithdrawResultV1(
        status="would_withdraw" if mode.previewing and not already else "withdrawn",
        proposal_id=record.proposal_id,
        actor_id=record.actor_id,
        reason=record.reason,
        withdrawn_at=record.withdrawn_at,
        already_withdrawn=already,
        coordinate=coordinate,
    )


def service_reject_playbill_proposal(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    actor_id: str,
    reason: str,
    rejected_at: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> PlaybillProposalRejectResultV1:
    """Record a reviewer's terminal statement that a proposal will not be accepted.

    ``dry_run`` runs every check and records nothing (``would_reject``, R12).

    Rejection is withdrawal's reviewer-side twin: the same durable, immutable
    closing record (``kind: rejected``) with a required reason, the same move
    out of the open inventory, and the same refusal at every settlement door.
    The proposal's own actor withdraws rather than rejects. Only an open or
    stale proposal may be rejected. The record keys each Claim the candidate
    proposed, so a later proposal of the same statement on the same evidence
    shows this rejection as a neutral prior refusal receipt; nothing refuses it.
    """

    with change_scope(
        instance,
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="cruxible.proposal.reject",
        describe=f"rejecting proposal {proposal_id}",
    ) as mode:
        admission = instance.proposal_evidence().read_admission(proposal_id)
        if admission.actor_id == actor_id:
            raise ProposalAdmissionError(
                "a proposal's own actor withdraws it rather than rejecting it: "
                f'run cruxible proposal withdraw {proposal_id} --reason "<reason>"'
            )
        record, already, coordinate = _close(
            instance,
            mode,
            ProposalWithdrawalRecord(
                proposal_id=admission.proposal_id,
                actor_id=actor_id,
                reason=reason,
                withdrawn_at=rejected_at,
                kind="rejected",
            ),
        )
        return PlaybillProposalRejectResultV1(
            status="would_reject" if mode.previewing and not already else "rejected",
            proposal_id=record.proposal_id,
            actor_id=record.actor_id,
            reason=record.reason,
            rejected_at=record.withdrawn_at,
            already_rejected=already,
            coordinate=coordinate,
        )


def _close(
    instance: PlaybillInstance,
    mode: ChangeMode,
    record: ProposalWithdrawalRecord,
) -> tuple[ProposalWithdrawalRecord, bool, AcceptedCoordinate | None]:
    """Write one closing record; return it, whether it already existed, and the head.

    A proposal closes once. The same kind of close again returns the existing
    record; the other kind refuses, since a withdrawn proposal cannot also be
    rejected (or the reverse). Closes of one proposal are serialized from the
    open check through the refusal-index rows, so the rows reflect the close
    that won.
    """

    with instance.refusal_index().closing(record.proposal_id):
        return _close_once(instance, mode, record)


def _close_once(
    instance: PlaybillInstance,
    mode: ChangeMode,
    record: ProposalWithdrawalRecord,
) -> tuple[ProposalWithdrawalRecord, bool, AcceptedCoordinate | None]:
    existing = instance.proposal_evidence().read_withdrawal(record.proposal_id)
    if existing is not None:
        if existing.kind != record.kind:
            raise ProposalAdmissionError(
                f"proposal {record.proposal_id} is already {existing.kind}; "
                "a closed proposal closes once"
            )
        return existing, True, None
    entry = next(
        (
            item
            for item in _proposal_entries(
                instance,
                AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
                record.proposal_id,
            )
            if item.proposal_id == record.proposal_id
        ),
        None,
    )
    if entry is None:  # pragma: no cover - a read admission always lists
        raise ProposalNotFoundError(record.proposal_id)
    verb = "withdrawn" if record.kind == "withdrawn" else "rejected"
    if entry.status not in _UNSETTLED_STATUSES:
        raise ProposalAdmissionError(
            f"only an open or stale proposal may be {verb}; this one is {entry.terminal_reason}"
        )
    head = mode.head
    assert head is not None
    coordinate = AcceptedCoordinate.from_internal(head)
    if mode.previewing:
        return record, False, coordinate
    if record.kind == "rejected":
        # Record the Claims the rejected candidate proposed (statement and cited
        # Captures) while it is still readable, so a later proposal of the same
        # statement on the same evidence resurfaces this rejection.
        from cruxible_core.proposals.refusal_index import candidate_refused_claims

        record = ProposalWithdrawalRecord.model_validate(
            {
                **record.model_dump(mode="json"),
                "refused_claims": [
                    item.model_dump(mode="json")
                    for item in candidate_refused_claims(instance, record.proposal_id) or ()
                ],
            }
        )
    try:
        if record.kind == "rejected":
            # The index row precedes the record (a crash between them leaves a
            # row a read ignores until a record says rejected) ...
            instance.refusal_index().record_rejection(instance, record)
        # A pinned close confirms the live accepted head and records while
        # holding it still (R12).
        with mode.committing():
            instance.proposal_evidence().write_withdrawal(record)
    finally:
        if record.kind == "rejected":
            # ... and once the close resolves, the rows are those of the record
            # that won: this one, a competing one, or none.
            instance.refusal_index().settle_close(instance, record.proposal_id)
    _end_closed_intent(instance, record.proposal_id)
    # Release local closed-candidate roots even without a configured mirror.
    instance.advertise_workspace()
    instance.request_ledger_mirror()
    return record, False, coordinate


def _end_closed_intent(instance: PlaybillInstance, proposal_id: str) -> None:
    """End the authoring intent a closed proposal came from, so re-authoring is new.

    The intent is read off the admission's proposal ref (one keyed read). A
    proposal no authoring intent submitted (a Procedure's, a Line's) has none.
    A crash before this leaves the intent pending; the next create of the same
    content ends it then (``AuthoringIntentCoordinator.create``).
    """

    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.preflight import authoring_intent_id_for_proposal_ref

    admission = instance.proposal_evidence().read_admission(proposal_id)
    intent_id = authoring_intent_id_for_proposal_ref(
        admission.target_ref, actor_id=admission.actor_id
    )
    if intent_id is None:
        return
    AuthoringIntentCoordinator.for_instance(instance).end_closed(
        intent_id, actor_id=admission.actor_id, proposal_id=proposal_id
    )


def proposal_still_open(instance: PlaybillInstance, proposal_id: str) -> bool:
    """Whether a proposal is open or awaiting readmit: not yet accepted, refused, or closed.

    One keyed inventory row; a stale proposal (its base moved) is still open
    work, which only a close or a readmission ends.
    """

    entry = next(
        (
            item
            for item in _proposal_entries(
                instance,
                AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
                proposal_id,
                require_evidence=False,
            )
            if item.proposal_id == proposal_id
        ),
        None,
    )
    return entry is not None and entry.status in _UNSETTLED_STATUSES


def service_supersede_line_proposal(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    successor_id: str,
    actor_id: str,
    withdrawn_at: str,
) -> bool:
    """Withdraw a Line's open proposal as superseded by the one its later run submitted.

    The Line's own run identity (``actor_id``) closes it, whichever actor's run
    submitted it: the Line owns its open proposal. Returns whether the proposal
    now stands withdrawn as superseded by ``successor_id`` -- by this call or an
    earlier, identical one (recovery redelivers). A proposal that settled first
    (accepted, refused, withdrawn or rejected otherwise) is left as it is, and
    nothing is withdrawn.
    """

    with change_scope(
        instance,
        dry_run=None,
        at=None,
        kind="direct",
        operation="cruxible.proposal.withdraw",
        describe=f"superseding proposal {proposal_id}",
    ) as mode:
        with instance.refusal_index().closing(proposal_id):
            existing = instance.proposal_evidence().read_withdrawal(proposal_id)
            if existing is not None:
                return existing.superseded_by == successor_id
            if not proposal_still_open(instance, proposal_id):
                return False
            _close_once(
                instance,
                mode,
                ProposalWithdrawalRecord(
                    proposal_id=proposal_id,
                    actor_id=actor_id,
                    reason=(
                        f"Superseded by {successor_id}: a later run of its Line proposed "
                        "a different change."
                    ),
                    withdrawn_at=withdrawn_at,
                    superseded_by=successor_id,
                ),
            )
            return True


def service_playbill_whoami(
    instance: PlaybillInstance,
    *,
    actor_id: str | None,
    credential_label: str | None,
    actor_id_source: WhoAmIActorIdSource,
    authenticated: bool,
    permission_mode: PermissionMode,
    credential_id: str | None = None,
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
    registration: PrincipalRegistrationStatus | None = (
        None if actor_id is None else principal_standing(instance, actor_id)
    )
    refusal = authoring_refusal(
        instance,
        actor_id=actor_id,
        configured=actor_id_source != "local_operator",
        credential_id=credential_id,
        credential_label=credential_label,
        permission_mode=permission_mode,
    )
    return PlaybillWhoAmIV1(
        actor_id=actor_id,
        credential_label=credential_label,
        actor_id_source=actor_id_source,
        authenticated=authenticated,
        can_author=refusal is None,
        authoring_refusal=refusal,
        credential_permission_mode=cast(CredentialPermissionMode, permission_mode.name.lower()),
        principal_registration_status=registration,
        active_principal_ids=active,
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )


__all__ = [
    "PlaybillProposalListEntryV1",
    "PlaybillProposalListV1",
    "PlaybillProposalReadmitResultV1",
    "PlaybillProposalRejectResultV1",
    "PlaybillProposalWithdrawResultV1",
    "PlaybillWhoAmIV1",
    "CredentialPermissionMode",
    "PrincipalRegistrationStatus",
    "ProposalAwaitingApproval",
    "ProposalInventoryStatus",
    "ProposalReadmission",
    "ProposalTerminalReason",
    "StaleProposal",
    "WhoAmIActorIdSource",
    "proposal_readmission",
    "proposal_still_open",
    "proposals_awaiting_activation",
    "proposals_awaiting_approval",
    "readmission_operation_digest",
    "service_list_playbill_proposals",
    "service_playbill_proposal_status",
    "service_readmit_playbill_proposal",
    "service_reject_playbill_proposal",
    "service_supersede_line_proposal",
    "service_withdraw_playbill_proposal",
    "service_playbill_whoami",
    "stale_unreadmitted_proposals",
]
