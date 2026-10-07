"""Deterministic operational reads over proposal evidence and caller identity."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, model_validator

from cruxible_client.contracts.canonical import ProposalDigest
from cruxible_client.contracts.errors import (
    ProposalAdmissionError,
    ProposalIntegrityError,
    ProposalNotFoundError,
    ProposalReadmitAlreadyAccepted,
    ProposalReadmitNotStale,
    ProposalReadmitRequiresResubmission,
    ProposalSelectorAmbiguousError,
)
from cruxible_client.contracts.principals import AuthoringRefusal
from cruxible_client.contracts.proposal_models import ProposalReadmissionLink
from cruxible_core.authoring.id_prefixes import AmbiguousIdPrefix, resolve_id_prefix
from cruxible_core.indexes.proposals.proposal_index import timestamp
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

ProposalInventoryStatus = Literal["open", "settled", "incomplete"]
ProposalIncompleteReason = Literal["missing_admission", "missing_evaluation", "missing_candidate"]
ProposalTerminalReason = Literal["accepted", "refused", "stale", "withdrawn"]
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
        if (self.status == "open") != (self.terminal_reason is None):
            raise ValueError("open proposal status and terminal reason disagree")
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
    source_proposal_id: str
    operation_digest: str
    proposal: ProposalInspection


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


def _proposal_entries(
    instance: PlaybillInstance,
    coordinate: AcceptedCoordinate,
    proposal_id: str | None = None,
    *,
    require_evidence: bool = True,
) -> tuple[PlaybillProposalListEntryV1, ...]:
    """List entries, or one ID's; ``require_evidence`` makes that ID's records mandatory."""
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
            + ("WHERE proposal_id=? " if proposal_id is not None else "")
            + "ORDER BY admitted_at_us,proposal_id",
            (sequence, coordinate.semantic_root)
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

    Exactly `proposal list`'s `stale` terminal reason -- a candidate neither
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
            columns="proposal_id,actor_id,target_ref,admitted_at_us,candidate_parent_semantic_root",
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
    principal is such a signer who has not signed it yet.
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
            where="candidate_parent_semantic_root=? AND actor_id!=?",
            parameters=(coordinate.semantic_root, principal_id),
        )
    awaiting = []
    for row in rows:
        candidate = evidence.read_candidate(row["candidate_digest"])
        if not candidate.approval_requirements:
            continue
        signers = {
            submission.attestation.signer_id
            for submission in evidence.read_approvals(row["candidate_digest"])
        }
        if principal_id in signers:
            continue
        eligible = len((signers - {row["actor_id"]}) & ordinary_principal_ids)
        minimum = candidate.approval_requirements[0].minimum_distinct_signers
        if eligible >= minimum:
            continue
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
    """Replay one stale authored tree through the current ProposalService rebase.

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
    # A stale proposal may be withdrawn instead of readmitted, and that is the
    # actor saying this tree will never be settled. Readmitting it would settle
    # exactly that tree, under a new proposal id, so it refuses here.
    instance.proposal_evidence().refuse_withdrawn(source.admission.proposal_id)
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
    if source_status.terminal_reason != "stale":
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
    if (
        source.candidate is not None
        and len(source.candidate.members) > 1
        and any(member.artifact_kind == "claim-type" for member in source.candidate.members)
    ):
        raise ProposalReadmitRequiresResubmission()
    if _pins_slots(instance, target_ref=source.admission.target_ref, actor_id=actor_id):
        raise ProposalReadmitRequiresResubmission(
            "this stale proposal was admitted against pinned slot membership, which a "
            "byte rebase would not check; run the write again at the current head"
        )
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    link = ProposalReadmissionLink(source_proposal_id=proposal_id, coordinate=coordinate)
    operation_digest = link.operation_digest
    matching = tuple(
        admission
        for admission in instance.proposal_evidence().list_admissions()
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
            proposed_base_oid=source.admission.proposed_base_oid,
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


def _pins_slots(instance: PlaybillInstance, *, target_ref: str, actor_id: str) -> bool:
    """Whether the proposal's authoring intent pinned any slot's live membership.

    Slot pins are checked by authoring preflight at the evaluated head. A
    readmission rebases the stored tree without that preflight, so it would
    admit the change over a slot it never saw.
    """

    from cruxible_client.contracts.authoring.models import (
        AuthoringIntent,
        AuthoringSlotExpectation,
    )
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.preflight import authoring_intent_id_for_proposal_ref

    intent_id = authoring_intent_id_for_proposal_ref(target_ref, actor_id=actor_id)
    if intent_id is None:
        return False
    intent = AuthoringIntentCoordinator.for_instance(instance).store.get(
        intent_id, actor_id=actor_id
    )
    return isinstance(intent, AuthoringIntent) and any(
        isinstance(item, AuthoringSlotExpectation) for item in intent.reference_expectations
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
                AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
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
    record = ProposalWithdrawalRecord(
        proposal_id=admission.proposal_id,
        actor_id=actor_id,
        reason=reason,
        withdrawn_at=withdrawn_at,
    )
    head = mode.head
    assert head is not None
    coordinate = AcceptedCoordinate.from_internal(head)
    if mode.previewing:
        return PlaybillProposalWithdrawResultV1(
            status="would_withdraw",
            proposal_id=record.proposal_id,
            actor_id=record.actor_id,
            reason=record.reason,
            withdrawn_at=record.withdrawn_at,
            coordinate=coordinate,
        )
    # A pinned withdrawal confirms the live accepted head and records while
    # holding it still (R12).
    with mode.committing():
        instance.proposal_evidence().write_withdrawal(record)
    # Release local closed-candidate roots even without a configured mirror.
    instance.advertise_workspace()
    instance.request_ledger_mirror()
    return PlaybillProposalWithdrawResultV1(
        proposal_id=record.proposal_id,
        actor_id=record.actor_id,
        reason=record.reason,
        withdrawn_at=record.withdrawn_at,
        coordinate=coordinate,
    )


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
    "proposals_awaiting_approval",
    "readmission_operation_digest",
    "service_list_playbill_proposals",
    "service_playbill_proposal_status",
    "service_readmit_playbill_proposal",
    "service_withdraw_playbill_proposal",
    "service_playbill_whoami",
    "stale_unreadmitted_proposals",
]
