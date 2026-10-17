"""Why an accepted generation changed what it changed: one resolver for get and the floor.

Acceptance records the admission id and digest in the generation's signed
header. Reasons are read by that identity, even when a later admission shares
the same candidate and actor. Pre-succession generations without that header
retain the unique non-withdrawn candidate/actor resolution.

``get`` (``detail="evidence"``, ``"why"`` and ``"history"``) and the floor's
``changes/`` files share this resolver. A Procedure item's own reason for the
path wins, else the admission's rationale (a write's ``because``).

Per generation, one immutable header and one admission are parsed once per
process, keyed by OID and digest. New generations require only an id lookup,
never a scan of admissions. Legacy prefetch batches candidate index lookups.
Per member path at most one item-reason file is read.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from cruxible_client.contracts.errors import ProposalNotFoundError, SettlementIntegrityError
from cruxible_client.contracts.get_reads import (
    GET_REASON_MAX_CHARS,
    GetChangeSetReason,
    GetProposedReason,
    GetRevisionReason,
)
from cruxible_client.contracts.proposal_models import ProposalAdmissionRecord, ProposalItemReason
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.indexes.history.history_index import AcceptedGenerationLocation
from cruxible_core.ledger.generation_facts import AcceptingAdmission
from cruxible_core.runtime.instance import PlaybillInstance

#: Admission records by the digest of their bytes: immutable, so each is read,
#: verified and parsed once per process, whichever read or render asks.
_ADMISSIONS: OrderedDict[str, ProposalAdmissionRecord] = OrderedDict()
_ADMISSIONS_CAPACITY = 65536


class ChangeReasons:
    """Each accepted generation's accepting admission and its members' reasons, memoized."""

    def __init__(self, instance: PlaybillInstance) -> None:
        self._instance = instance
        self._admissions: dict[str, ProposalAdmissionRecord | None] = {}
        self._legacy: dict[tuple[str, str], ProposalAdmissionRecord | None] = {}
        self._operations: dict[str, str | None] = {}
        self._items: dict[tuple[str, str], ProposalItemReason | None] = {}

    def prefetch(self, generations: Iterable[AcceptedGenerationLocation]) -> None:
        """Resolve many generations' admissions from one read of the proposal index.

        New generations use recorded ids; headerless legacy generations share
        batched candidate index lookups (the floor's changes/ renders many).
        """

        legacy = []
        for generation in generations:
            if self._instance.accepting_admission(generation.git_oid) is None:
                legacy.append(generation)
            else:
                self.admission(generation)
        wanted = {
            (str(item.candidate_digest), str(item.actor_id))
            for item in legacy
            if item.candidate_digest is not None and item.actor_id is not None
        } - set(self._legacy)
        if not wanted:
            return
        evidence = self._instance.proposal_evidence()
        if evidence.index is None:
            self._legacy.update(dict.fromkeys(wanted))
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
            self._legacy[(candidate, actor)] = self._matching(evidence, rows[candidate], actor)

    def admission(self, generation: AcceptedGenerationLocation) -> ProposalAdmissionRecord | None:
        """The one admission that accepted ``generation``'s candidate, or None."""

        if generation.candidate_digest is None or generation.actor_id is None:
            return None  # genesis: no proposal
        oid = generation.git_oid
        if oid not in self._admissions:
            identity = self._instance.accepting_admission(oid)
            if identity is not None:
                admission = memo_get(_ADMISSIONS, identity.admission_digest)
                if admission is None:
                    try:
                        admission = self._instance.proposal_evidence().read_admission(
                            identity.proposal_id
                        )
                    except ProposalNotFoundError:
                        self._admissions[oid] = None
                        return None
                    if AcceptingAdmission.of(admission) != identity:
                        raise SettlementIntegrityError(
                            "accepting admission digest differs from generation"
                        )
                    memo_put(
                        _ADMISSIONS,
                        identity.admission_digest,
                        admission,
                        capacity=_ADMISSIONS_CAPACITY,
                    )
                if admission.proposal_id != identity.proposal_id:
                    raise SettlementIntegrityError("accepting admission id differs from generation")
                if admission.actor_id != generation.actor_id:
                    raise SettlementIntegrityError(
                        "accepting admission actor differs from generation"
                    )
                self._admissions[oid] = admission
            else:
                key = (generation.candidate_digest, generation.actor_id)
                if key not in self._legacy:
                    self._legacy[key] = self._accepting(*key)
                self._admissions[oid] = self._legacy[key]
        return self._admissions[oid]

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
