"""Why an accepted generation changed what it changed: one resolver for get and the floor.

An accepted generation publishes exactly one candidate, by exactly one actor.
The admission that accepted it is the one proposal that admitted that candidate
(``proposals_by_candidate``), by that actor, and was not withdrawn or rejected;
none, or more than one, and the generation has no attributed proposal: nothing
is picked by id order and nothing is invented. Its reason for one member is the
Procedure item's own reason for that path when a Procedure proposed it (read
through the admission's own item-reason link), else the admission's rationale
-- a write's ``because`` with its per-retire notes, or an authored change set's
rationale.

``get`` (``detail="evidence"``, ``"why"`` and ``"history"``) and the floor's
``changes/`` files read it here and nowhere else, so a rendered page and a read
always name the same reason. The change-set record and its generation facts are
never read: the resolver needs only the generation's history row.

Work, memoized per resolver (one per request or render): per distinct
generation one index lookup of the admissions sharing its candidate, and each
admission's file read, verified and parsed once per process (keyed by its
digest); per member path at most one item-reason file. Nothing grows with the
instance.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from cruxible_client.contracts.get_reads import (
    GET_REASON_MAX_CHARS,
    GetChangeSetReason,
    GetProposedReason,
    GetRevisionReason,
)
from cruxible_client.contracts.proposal_models import ProposalAdmissionRecord, ProposalItemReason
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.indexes.history.history_index import AcceptedGenerationLocation
from cruxible_core.runtime.instance import PlaybillInstance

#: Admission records by the digest of their bytes: immutable, so each is read,
#: verified and parsed once per process, whichever read or render asks.
_ADMISSIONS: OrderedDict[str, ProposalAdmissionRecord] = OrderedDict()
_ADMISSIONS_CAPACITY = 65536


class ChangeReasons:
    """Each accepted generation's accepting admission and its members' reasons, memoized."""

    def __init__(self, instance: PlaybillInstance) -> None:
        self._instance = instance
        self._admissions: dict[tuple[str, str], ProposalAdmissionRecord | None] = {}
        self._operations: dict[str, str | None] = {}
        self._items: dict[tuple[str, str], ProposalItemReason | None] = {}

    def prefetch(self, generations: Iterable[AcceptedGenerationLocation]) -> None:
        """Resolve many generations' admissions from one read of the proposal index.

        A render that names many generations (the floor's changes/) asks the
        index once, in chunks, instead of once per generation.
        """

        wanted = {
            (str(item.candidate_digest), str(item.actor_id))
            for item in generations
            if item.candidate_digest is not None and item.actor_id is not None
        } - set(self._admissions)
        if not wanted:
            return
        evidence = self._instance.proposal_evidence()
        if evidence.index is None:
            self._admissions.update(dict.fromkeys(wanted))
            return
        digests = sorted({candidate for candidate, _actor in wanted})
        rows: dict[str, list[dict[str, Any]]] = {digest: [] for digest in digests}
        for start in range(0, len(digests), 500):
            chunk = digests[start : start + 500]
            for row in evidence.index.rows(
                evidence,
                "candidate_digest IN (" + ",".join("?" for _ in chunk) + ")",
                tuple(chunk),
            ):
                rows[str(row["candidate_digest"])].append(row)
        for candidate, actor in wanted:
            self._admissions[(candidate, actor)] = self._matching(evidence, rows[candidate], actor)

    def admission(self, generation: AcceptedGenerationLocation) -> ProposalAdmissionRecord | None:
        """The one admission that accepted ``generation``'s candidate, or None."""

        if generation.candidate_digest is None or generation.actor_id is None:
            return None  # genesis: no proposal
        key = (generation.candidate_digest, generation.actor_id)
        if key not in self._admissions:
            self._admissions[key] = self._accepting(*key)
        return self._admissions[key]

    def _accepting(self, candidate_digest: str, actor_id: str) -> ProposalAdmissionRecord | None:
        evidence = self._instance.proposal_evidence()
        if evidence.index is None:
            return None
        rows = evidence.index.rows(evidence, "candidate_digest=?", (candidate_digest,))
        return self._matching(evidence, rows, actor_id)

    @staticmethod
    def _matching(
        evidence: Any, rows: Iterable[dict[str, Any]], actor_id: str
    ) -> ProposalAdmissionRecord | None:
        """The one non-withdrawn admission among ``rows`` by ``actor_id``, or None."""

        matches: dict[str, ProposalAdmissionRecord] = {}
        for row in rows:
            if row["withdrawal_path"] is not None or row["admission_digest"] is None:
                continue  # withdrawn, rejected or unadmitted: never the accepting admission
            digest = str(row["admission_digest"])
            admission = memo_get(_ADMISSIONS, digest)
            if admission is None:
                admission = evidence.read_admission(row["proposal_id"])
                memo_put(_ADMISSIONS, digest, admission, capacity=_ADMISSIONS_CAPACITY)
            if admission.actor_id == actor_id:
                matches[admission.proposal_id] = admission
        if len(matches) != 1:
            return None
        (admission,) = matches.values()
        return admission

    def item_reason(
        self, admission: ProposalAdmissionRecord, path: str
    ) -> ProposalItemReason | None:
        """The Procedure item's own reason for ``path`` in this admission, if one was kept."""

        evidence = self._instance.proposal_evidence()
        proposal = admission.proposal_id
        if proposal not in self._operations:
            self._operations[proposal] = evidence.item_reason_operation(admission)
        operation = self._operations[proposal]
        if operation is None:
            return None
        key = (operation, path)
        if key not in self._items:
            self._items[key] = evidence.read_item_reason(operation, path)
        return self._items[key]

    def reason(self, generation: AcceptedGenerationLocation, path: str) -> GetRevisionReason | None:
        """Why ``generation`` changed ``path``: its item's reason, else its change set's."""

        admission = self.admission(generation)
        if admission is None:
            return None
        item = self.item_reason(admission, path)
        if item is not None:
            return proposed_reason(admission, item)
        if not admission.rationale:
            return None
        text = admission.rationale
        cut = len(text) > GET_REASON_MAX_CHARS
        return GetChangeSetReason(
            text=text[: GET_REASON_MAX_CHARS - 1] + "…" if cut else text,
            truncated=cut,
            proposal=admission.proposal_id,
        )

    def rationale(self, generation: AcceptedGenerationLocation, path: str) -> str | None:
        """The same reason as text, whole: the item's reason, else the change set's rationale."""

        admission = self.admission(generation)
        if admission is None:
            return None
        item = self.item_reason(admission, path)
        return item.reason if item is not None else admission.rationale


def proposed_reason(
    admission: ProposalAdmissionRecord, item: ProposalItemReason
) -> GetProposedReason:
    return GetProposedReason(
        reason=item.reason,
        truncated=item.truncated,
        proposal=admission.proposal_id,
        procedure=item.procedure,
        run=item.run_id,
        terminal=item.terminal,
    )


__all__ = ["ChangeReasons", "proposed_reason"]
