"""One exact-preview mechanism for every change, built once (rule R12).

Every change operation runs inside ``change_scope``. It decides from the
request's ``dry_run`` and the operation's ``ChangeKind`` whether this call
previews or commits, and pins the call to the accepted coordinate it runs at:

- a preview runs behind every guard there is -- bodies held in memory
  (``dry_run_bodies``), history reads detached from the on-disk index
  (``detached_history_reads``) and every fenced write door closed
  (``previewing``) -- so the change's own code runs up to its commit and stops
  there, writing nothing anywhere;
- a commit carrying ``at`` refuses ``playbill.preview.state_moved`` when the
  accepted head is no longer the one the preview saw, and a change that cannot
  be undone refuses ``playbill.preview.confirmation_required`` without ``at``.

The pin is checked twice: once on entry, to refuse early, and again where the
change commits, under that commit's own lock, so state that moves in between is
refused rather than committed. A proposal checks it at publication under the
activation lock (``admit_change_set`` hands ``ChangeMode.confirm_head`` to the
proposal service); an operation on an instance checks it under the lock its
write holds.

A change to state outside the accepted ledger -- a runtime credential, a host's
worktree binding -- is pinned instead to a ``PlaybillStateCoordinate``: a
digest of exactly the records it changes, read where it writes
(``state_change_scope`` and ``StateChange.observe``). The ledger can move
without changing them, and a host before genesis has no head at all.

Changes that become a proposal go through ``admit_change_set``: a preview
evaluates the candidate on the proposal service's own admission path
(``ProposalService.preview``) and a commit submits it, so both reach the same
verdict on the same tree.

A preview starts its guards before anything is opened: ``change_entry`` wraps
the instance load at the call's door, so a cold open runs behind the fence and
the recovery repairs an open would write are refused
(``playbill.preview.recovery_pending``) instead of written.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Literal

from cruxible_client import contracts
from cruxible_client.contracts.actor_types import TransportCapability
from cruxible_client.contracts.candidates import CandidateRecordAnyVersion
from cruxible_client.contracts.change_control import ChangeKind, PlaybillStateCoordinate
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.get_reads import PlaybillGetCoordinate
from cruxible_core.errors import ChangeRefusedError
from cruxible_core.indexes.history.history_index import detached_history_reads
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalPreview,
    ProposalResult,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.cas import dry_run_bodies
from cruxible_core.storage.preview_fence import is_previewing, previewing


class ChangeMode:
    """Whether this call previews, and the accepted coordinate it is pinned to.

    The head is read once, when first needed: at once for a preview or a pinned
    commit, and only if the change asks for it otherwise, so an unpinned commit
    that reports no coordinate reads nothing extra.
    """

    def __init__(
        self,
        instance: PlaybillInstance | None,
        *,
        previewing: bool,
        at: str | None = None,
        kind: ChangeKind = "direct",
        operation: str = "",
        describe: str = "",
    ) -> None:
        self.previewing = previewing
        self.at = at
        self._kind = kind
        self._operation = operation
        self._describe = describe
        self._instance = instance
        self._head: AcceptedProjectionCoordinate | None = None
        self._coordinate: PlaybillGetCoordinate | None = None

    @property
    def head(self) -> AcceptedProjectionCoordinate | None:
        if self._head is None and self._instance is not None:
            self._head = self._instance.accepted_coordinate()
        return self._head

    @property
    def coordinate(self) -> PlaybillGetCoordinate | None:
        head = self.head
        if self._coordinate is None and self._instance is not None and head is not None:
            self._coordinate = compact_coordinate(self._instance, head)
        return self._coordinate

    @contextmanager
    def committing(self) -> Iterator[None]:
        """Hold accepted state still across a pinned commit's write.

        For a commit carrying ``at``: takes the activation lock, confirms the
        live accepted head is the previewed one, and keeps the lock until the
        enclosed write is done, so an acceptance cannot land between the check
        and the write. A preview, or a commit with no ``at``, holds nothing.
        """

        if self.previewing or self.at is None or self._instance is None:
            yield
            return
        with self._instance.accepted_head_held() as head_oid:
            self.confirm_head(head_oid)
            yield

    def confirm_head(self, head_oid: str | None) -> None:
        """Refuse a pinned commit whose accepted head is not the one previewed.

        Called where the change commits, under the lock that keeps the head
        from moving until the commit is written; a preview, or a commit that
        carries no ``at``, passes.
        """

        if self.previewing or self.at is None:
            return
        _pin(
            head_oid=head_oid,
            at=self.at,
            kind=self._kind,
            operation=self._operation,
            describe=self._describe,
        )


class StateChange:
    """A change to operational state, pinned to the state it changes (R12).

    The write path calls ``observe`` with the subject's state, read inside the
    write's own transaction or lock: a preview records it as its coordinate, and
    a commit carrying ``at`` refuses ``playbill.preview.state_moved`` when it
    differs.
    """

    def __init__(self, *, previewing: bool, at: str | None, operation: str, describe: str) -> None:
        self.previewing = previewing
        self.at = at
        self._operation = operation
        self._describe = describe
        self.coordinate: PlaybillStateCoordinate | None = None

    def observe(self, coordinate: PlaybillStateCoordinate) -> None:
        self.coordinate = coordinate
        if self.previewing or self.at is None:
            return
        if not coordinate.digest.startswith(self.at):
            raise ChangeRefusedError(
                "playbill.preview.state_moved",
                f"{self._describe} was previewed at {self.at}, but {coordinate.subject} is "
                f"now at {coordinate.digest[:12]}; preview it again",
                operation=self._operation,
            )


def previews_by_default(kind: ChangeKind) -> bool:
    return kind != "direct"


def compact_coordinate(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> PlaybillGetCoordinate:
    """The 12-hex git oid and generation every outcome is pinned to."""

    public = AcceptedCoordinate.from_internal(coordinate)
    with instance.accepted_history_reader(at=public) as history:
        generation = int(history.sequence)
    return PlaybillGetCoordinate(git_oid=public.git_oid[:12], generation=generation)


def full_coordinate(instance: PlaybillInstance) -> contracts.PlaybillAcceptedCoordinate:
    """The whole accepted coordinate, for an outcome that reports it in full."""

    return contracts.PlaybillAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
    )


@contextmanager
def preview_guards(active: bool) -> Iterator[None]:
    """Every guard a preview runs behind, or nothing for a commit.

    Re-entrant: a preview already behind the guards (``change_entry`` at the
    call's door, then the change's own ``change_scope``) keeps the outer ones,
    so bodies stored on the way in stay readable inside.
    """

    with ExitStack() as stack:
        if active and not is_previewing():
            stack.enter_context(dry_run_bodies())
            stack.enter_context(detached_history_reads())
            stack.enter_context(previewing())
        yield


def previews(dry_run: bool | None, kind: ChangeKind) -> bool:
    """Whether a call with this ``dry_run`` previews, for an operation of ``kind``."""

    return previews_by_default(kind) if dry_run is None else dry_run


@contextmanager
def change_entry(dry_run: bool | None, kind: ChangeKind) -> Iterator[bool]:
    """Raise a preview's guards at the call's door, before its instance is opened.

    Wrap the instance load and the change together: opening an instance cold
    can run recovery, and a preview must not let that write either.
    """

    active = previews(dry_run, kind)
    with preview_guards(active):
        yield active


def _pin(
    *,
    head_oid: str | None,
    at: str | None,
    kind: ChangeKind,
    operation: str,
    describe: str,
) -> None:
    if at is None:
        if kind == "irreversible":
            raise ChangeRefusedError(
                "playbill.preview.confirmation_required",
                f"{describe} cannot be undone, so it commits only with the coordinate of "
                "its preview; preview it (dry_run), then commit with at=<that coordinate>",
                operation=operation,
            )
        return
    if head_oid is None or not head_oid.startswith(at):
        raise ChangeRefusedError(
            "playbill.preview.state_moved",
            f"{describe} was previewed at {at}, but accepted state is now at "
            f"{'nothing' if head_oid is None else head_oid[:12]}; preview it again",
            operation=operation,
        )


@contextmanager
def change_scope(
    instance: PlaybillInstance | None,
    *,
    dry_run: bool | None,
    at: str | None,
    kind: ChangeKind,
    operation: str,
    describe: str,
) -> Iterator[ChangeMode]:
    """Run one change as a preview or as a pinned commit; see the module docstring.

    ``instance`` is None for a change on a host with no instance yet: such a
    change has no accepted coordinate, and ``at`` cannot pin it.
    ``operation`` is the CLI leaf the refusal repairs name; ``describe`` names
    the change in refusal prose.
    """

    active = previews(dry_run, kind)
    with preview_guards(active):
        mode = ChangeMode(
            instance,
            previewing=active,
            at=at,
            kind=kind,
            operation=operation,
            describe=describe,
        )
        if active:
            mode.coordinate  # noqa: B018 - the preview pins to the head it starts at
        elif at is not None or kind == "irreversible":
            head = mode.head
            _pin(
                head_oid=None if head is None else head.git_oid,
                at=at,
                kind=kind,
                operation=operation,
                describe=describe,
            )
        yield mode


@contextmanager
def state_change_scope(
    *,
    dry_run: bool | None,
    at: str | None,
    kind: ChangeKind,
    operation: str,
    describe: str,
) -> Iterator[StateChange]:
    """Run one operational change as a preview or a pinned commit.

    Like ``change_scope``, but pinned to the state the change writes
    (``StateChange.observe``), which exists before genesis too. A change that
    cannot be undone refuses ``playbill.preview.confirmation_required`` on
    entry when it would commit without ``at``.
    """

    active = previews(dry_run, kind)
    if not active and at is None and kind == "irreversible":
        _pin(head_oid=None, at=None, kind=kind, operation=operation, describe=describe)
    with preview_guards(active):
        yield StateChange(previewing=active, at=at, operation=operation, describe=describe)


@dataclass(frozen=True)
class AdmittedChangeSet:
    """A change set admitted as a proposal, or evaluated as one by a preview."""

    previewed: bool
    candidate: CandidateRecordAnyVersion | None
    diagnostics: tuple[CompilerDiagnostic, ...]
    proposal_id: str | None
    result: ProposalResult | None
    preview: ProposalPreview | None = None

    @property
    def admitted(self) -> bool:
        return self.candidate is not None

    @property
    def candidate_digest(self) -> str | None:
        return None if self.candidate is None else self.candidate.candidate_digest

    @property
    def approval_required(self) -> bool:
        return self.candidate is not None and bool(self.candidate.approval_requirements)

    @property
    def status(self) -> Literal["proposed", "blocked", "would_propose", "would_block"]:
        if self.previewed:
            return "would_propose" if self.admitted else "would_block"
        return "proposed" if self.admitted else "blocked"

    def refusal_detail(self) -> str:
        return "Refused: " + "; ".join(item.code for item in self.diagnostics)


def admit_change_set(
    instance: PlaybillInstance,
    mode: ChangeMode,
    *,
    actor_id: str,
    request: ProposalAdmissionRequest,
    candidate_tree: Mapping[str, bytes],
    timestamp: str,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
) -> AdmittedChangeSet:
    """Submit one candidate tree, or (previewing) evaluate it on submit's own path."""

    service = instance.proposal_service()
    actor = AuthenticatedActor(actor_id=actor_id, capabilities=capabilities)
    if mode.previewing:
        preview = service.preview(
            actor=actor, request=request, candidate_tree=candidate_tree, timestamp=timestamp
        )
        return AdmittedChangeSet(
            previewed=True,
            candidate=preview.candidate,
            diagnostics=preview.evaluation.diagnostics,
            proposal_id=None,
            result=None,
            preview=preview,
        )
    submitted = service.submit(
        actor=actor,
        request=request,
        candidate_tree=candidate_tree,
        timestamp=timestamp,
        confirm_head=mode.confirm_head,
    )
    return AdmittedChangeSet(
        previewed=False,
        candidate=submitted.candidate,
        diagnostics=submitted.evaluation.diagnostics,
        proposal_id=submitted.admission.proposal_id,
        result=submitted,
    )


__all__ = [
    "AdmittedChangeSet",
    "ChangeMode",
    "StateChange",
    "admit_change_set",
    "change_entry",
    "change_scope",
    "compact_coordinate",
    "full_coordinate",
    "preview_guards",
    "previews",
    "previews_by_default",
    "state_change_scope",
]
