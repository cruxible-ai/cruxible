"""Count settled items that match earlier refusals, in post, from the run journal.

Settle delivery does nothing for this: the count is informational and is
computed after the fact. One step reads the next page of ``terminal_egress``
records from the journal index after the refusal index's persisted cursor (a
keyed read by event kind, never a scan), keeps the deliveries whose receipt
says ``settled``, takes each one's Line version from its run's admission, and
hands the page to the refusal index, which counts and advances the cursor in
one transaction. While retirements lag (a backlog is indexed one page per
step), a step spends itself on that page and counts nothing. The consumer
runner runs steps every tick, and `next` runs a bounded catch-up before it
reads the Line rows. A busy or unbuilt index skips
the step; the cursor makes skipping lossless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_core.exhaust.payloads import UnavailablePayload, read_payload
from cruxible_core.proposals.refusal_index import SettledCursor, SettledDelivery
from cruxible_core.storage.cas import BodyAccessContext

if TYPE_CHECKING:
    from cruxible_core.runtime.instance import PlaybillInstance

#: Journal records one step reads.
SETTLED_BATCH = 256
#: Steps `next` runs before reading the Line rows.
NEXT_CATCH_UP_STEPS = 4

_ACCESS = BodyAccessContext(principal_id="cruxible-settled-refusals", can_read_body=True)
_SETTLED_RECEIPT = "playbill-terminal-egress-receipt-v4"


@dataclass(frozen=True)
class SettledStep:
    """What one step did: records read, and whether more remain after them."""

    ran: bool
    records: int
    more: bool


def count_settled_refusals_step(
    instance: PlaybillInstance, *, wait: bool = False, limit: int = SETTLED_BATCH
) -> SettledStep:
    """Count one bounded page of settle deliveries after the persisted cursor."""

    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    index = instance.refusal_index()
    state = index.settled_state(instance, wait=wait)
    if state is None:
        return SettledStep(ran=False, records=0, more=False)
    cursor, coverage = state
    if coverage is not None:
        # Retirements are still being indexed (one page per step): count nothing
        # until the history a settlement's receipts depend on is in.
        return SettledStep(ran=True, records=0, more=True)
    journal, _root = _journal(instance)
    stream = _stream(instance)
    identity, page = journal.index.kind_after(
        stream, event_kind="terminal_egress", after=cursor.record_id, limit=limit
    )
    if cursor.journal_identity != identity and cursor.record_id:
        # Positions belong to one journal-index identity; a rebuilt journal
        # index starts over from its beginning (counting is idempotent).
        identity, page = journal.index.kind_after(
            stream, event_kind="terminal_egress", after=0, limit=limit
        )
    bodies = instance.body_store()
    deliveries = tuple(
        delivery
        for _position, stored in page
        if (delivery := _settled_delivery(journal, stream, stored, bodies)) is not None
    )
    through = SettledCursor(
        identity,
        page[-1][0] if page else (cursor.record_id if cursor.journal_identity == identity else 0),
    )
    if not page and through == cursor:
        return SettledStep(ran=True, records=0, more=False)
    advanced = index.advance_settled(
        instance, expected=cursor, through=through, deliveries=deliveries, wait=wait
    )
    return SettledStep(ran=advanced, records=len(page), more=advanced and len(page) == limit)


def _settled_delivery(journal, stream, stored, bodies) -> SettledDelivery | None:  # type: ignore[no-untyped-def]
    """The settled delivery one ``terminal_egress`` record reports, if it reports one."""

    payload = read_payload(bodies, stored.record.payload_digest, access=_ACCESS)
    if not isinstance(payload, dict):
        return None
    receipt = payload.get("receipt")
    if not isinstance(receipt, dict):
        return None
    proposal_id = receipt.get("proposal_id")
    if (
        receipt.get("tag") != _SETTLED_RECEIPT
        or receipt.get("outcome") != "settled"
        or not isinstance(proposal_id, str)
    ):
        return None
    run_id = stored.record.run_id
    if run_id is None:
        return None
    from cruxible_core.procedures.execution import parse_admission_payload

    admissions = journal.select_records(
        stream, run_id=run_id, event_kind="admission_bound", limit=1
    )
    if not admissions:
        return None
    payload = read_payload(bodies, admissions[0].record.payload_digest, access=_ACCESS)
    if isinstance(payload, UnavailablePayload):
        return None
    admission = parse_admission_payload(payload).admission
    line_identity = getattr(admission, "line_identity", None)
    line_spec_digest = getattr(admission, "line_spec_digest", None)
    if line_identity is None or line_spec_digest is None:
        return None
    return SettledDelivery(
        line_identity=line_identity.qualified,
        line_spec_digest=line_spec_digest,
        run_id=run_id,
        proposal_id=proposal_id,
        # The run's evaluation instant, which its settle request was made at.
        settled_at=canonical_candidate_timestamp(admission.admitted_at),
    )


def catch_up_settled_refusals(instance: PlaybillInstance, *, steps: int, wait: bool) -> None:
    """Run up to ``steps`` bounded steps, stopping when caught up or skipped."""

    for _step in range(steps):
        result = count_settled_refusals_step(instance, wait=wait)
        if not result.more:
            return


__all__ = [
    "NEXT_CATCH_UP_STEPS",
    "SETTLED_BATCH",
    "SettledStep",
    "catch_up_settled_refusals",
    "count_settled_refusals_step",
]
