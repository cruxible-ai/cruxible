"""Whether a proposal whose base moved is stale: its change set against the changes since.

A proposal is a change set against a base. It is stale only when the paths its
change set touches intersect the paths accepted history changed in
``(base, head]``. Nothing is recorded on the proposal for this: its keys are
derived from its own immutable candidate (memoized by proposal id), and
the check is one keyed seek per key on the history index's path locator
(``HistoryReader.paths_changed_after``), never a walk of the history between.

A proposal's keys:

* every member path of its change set (the Claims it writes or retires, the
  Subjects it creates, any definition it carries, closure members);
* every path a member's law was judged against (``dependency_proof_refs``:
  the ClaimType, the policies and Subjects it pins);
* for each Claim member: its Subject, a Subject object, its ClaimType and its
  slot ``(subject, predicate, qualifier)``; a slot is read as the slot's Claims
  at the head (one keyed seek on ``claims_by_subject_predicate``);
* the approval policy and the compiler-upgrade member, which every
  candidate's approval requirement and law coordinates are judged under;
* a settle's Procedure mandate (``settle_submission.mandate_digest``), read
  as that version's path on the history index's digest locator.

Landing re-evaluates the change set at the head, so a boundary no key names
(a Subject id an alias took since, say) still refuses there rather than land.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Protocol

from cruxible_client.contracts.approval_policy import APPROVAL_POLICY_PATH
from cruxible_client.contracts.claim_types import claim_type_path
from cruxible_client.contracts.claims import SubjectClaimObject, parse_claim
from cruxible_client.contracts.compiler_upgrade import COMPILER_UPGRADE_PATH
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import ProposalResult
from cruxible_core.runtime.instance import PlaybillInstance

#: A Claim slot: (subject path, predicate, qualifier).
Slot = tuple[str, str, str | None]

_ALWAYS_JUDGED_UNDER = (APPROVAL_POLICY_PATH, COMPILER_UPGRADE_PATH)


@dataclass(frozen=True)
class ProposalChangeKeys:
    """What a proposal's change set touches and is judged under, from its own candidate."""

    paths: frozenset[str]
    slots: frozenset[Slot]
    #: The Procedure mandate a settle was submitted under, if any.
    mandate_digest: str | None = None


_KEYS_LIMIT = 2048
_keys_memo: OrderedDict[str, ProposalChangeKeys] = OrderedDict()
_keys_lock = threading.Lock()


def proposal_change_keys(
    instance: PlaybillInstance, *, proposal_id: str, candidate_digest: str
) -> ProposalChangeKeys:
    """The keys of one proposal: O(its members), once per process per (immutable) proposal."""

    with _keys_lock:
        found = _keys_memo.get(proposal_id)
        if found is not None:
            _keys_memo.move_to_end(proposal_id)
            return found
    evidence = instance.proposal_evidence()
    candidate = evidence.read_candidate(candidate_digest)
    paths: set[str] = set(_ALWAYS_JUDGED_UNDER)
    for member in candidate.members:
        paths.add(member.path)
        for ref in getattr(member, "dependency_proof_refs", ()):
            paths.add(ref.target_path)
    for evaluation in getattr(candidate, "law_evidence", ()):
        for ref in evaluation.dependency_proof_refs:
            paths.add(ref.target_path)
    slots: set[Slot] = set()
    claim_paths = [member.path for member in candidate.members if member.path.startswith("claims/")]
    if claim_paths:
        tree_oid = evidence.read_evaluation(proposal_id).evaluated_tree_oid
        blobs = (
            {}
            if tree_oid is None
            else instance.proposal_blobs(tree_oid, claim_paths, proposal_id=proposal_id)
        )
        for path, content in blobs.items():
            statement = parse_claim(content, path=path).statement
            subject = statement.subject.artifact_path
            slots.add((subject, statement.predicate, statement.qualifier))
            paths.add(subject)
            paths.add(claim_type_path(statement.predicate))
            if isinstance(statement.object, SubjectClaimObject):
                paths.add(statement.object.address.artifact_path)
    settle = evidence.read_admission(proposal_id).settle_submission
    keys = ProposalChangeKeys(
        paths=frozenset(paths),
        slots=frozenset(slots),
        mandate_digest=None if settle is None else settle.mandate_digest,
    )
    with _keys_lock:
        _keys_memo[proposal_id] = keys
        while len(_keys_memo) > _KEYS_LIMIT:
            _keys_memo.popitem(last=False)
    return keys


class OverlapCheck(Protocol):
    def __call__(
        self, *, proposal_id: str, candidate_digest: str, parent_semantic_root: str
    ) -> tuple[str, ...]: ...


_SLOT_CLAIMS_SQL = "SELECT path, qualifier FROM claims WHERE subject_path=? AND predicate=?"


@contextmanager
def overlap_check(
    instance: PlaybillInstance, coordinate: AcceptedCoordinate
) -> Iterator[OverlapCheck]:
    """Name the paths changed in ``(base, coordinate]`` that a moved proposal touches.

    One history reader bound to ``coordinate`` serves every check; the
    coordinate's typed projection is bound only when a proposal has a slot.
    An empty answer means nothing the proposal touches or is judged under
    changed since its base: it may land at the head as it stands.
    """

    with ExitStack() as stack:
        history = stack.enter_context(instance.accepted_history_reader(at=coordinate))
        connection = None

        def slot_paths(slot: Slot) -> list[str]:
            nonlocal connection
            if connection is None:
                internal = instance.coordinate_for_oid(coordinate.git_oid)
                connection = stack.enter_context(
                    instance.bind_accepted_projection(internal)
                ).typed.connection
            subject, predicate, qualifier = slot
            return [
                str(path)
                for path, held in connection.execute(_SLOT_CLAIMS_SQL, (subject, predicate))
                if held == qualifier
            ]

        def check(
            *, proposal_id: str, candidate_digest: str, parent_semantic_root: str
        ) -> tuple[str, ...]:
            base = history.generation_for_semantic_root(parent_semantic_root)
            if base is None:
                # Not an ancestor of this coordinate: nothing lands it here.
                return (parent_semantic_root,)
            keys = proposal_change_keys(
                instance, proposal_id=proposal_id, candidate_digest=candidate_digest
            )
            paths = set(keys.paths)
            if keys.mandate_digest is not None:
                mandate = history.artifact(keys.mandate_digest)
                if mandate is not None:
                    paths.add(mandate.path)
            changed = set(history.paths_changed_after(paths, base.sequence))
            for slot in sorted(keys.slots, key=lambda item: (item[0], item[1], item[2] or "")):
                changed |= history.paths_changed_after(slot_paths(slot), base.sequence)
            return tuple(sorted(changed))

        yield check


def stale_paths_at_head(instance: PlaybillInstance, proposal: ProposalResult) -> tuple[str, ...]:
    """Why one proposal is stale at the head; empty when it is on the head or may land there."""

    candidate = proposal.candidate
    if candidate is None:
        return ()
    head = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    parent = candidate.candidate.parent_semantic_root
    if parent == head.semantic_root:
        return ()
    if candidate_accepted(instance, candidate.candidate_digest):
        # Settled: its own acceptance changed its paths, and nothing is left to land.
        return ()
    with overlap_check(instance, head) as check:
        return check(
            proposal_id=proposal.admission.proposal_id,
            candidate_digest=candidate.candidate_digest,
            parent_semantic_root=parent,
        )


def candidate_accepted(instance: PlaybillInstance, candidate_digest: str) -> bool:
    """Whether accepted history settled this candidate: one keyed read."""

    with instance.accepted_history_reader() as history:
        return history.generation_for_candidate(candidate_digest) is not None


def render_stale_paths(paths: tuple[str, ...], *, limit: int = 5) -> str:
    """The stale paths as one clause: the first few, then how many more."""

    shown = ", ".join(paths[:limit])
    more = len(paths) - limit
    return shown + (f" and {more} more" if more > 0 else "")


__all__ = [
    "OverlapCheck",
    "ProposalChangeKeys",
    "candidate_accepted",
    "overlap_check",
    "proposal_change_keys",
    "render_stale_paths",
    "stale_paths_at_head",
]
