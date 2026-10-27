"""What just happened, newest first: one feed over the ledger and the operational journals.

``orient(section="activity")`` pages accepted generations, Line occurrences and
enablement changes (the Line dispatch journal), run starts, finishes and the
Captures runs land (the Procedure-run journal), inbox postings and decisions
(the Line inbox journal) and Capture erasures, merged newest first. Nothing
here is a new authority: each source is read through its own index, and each
row is decoded by its own concern's reader.

Cost. Every source is read newest first through an index in its own order --
``records_event`` for the Procedure-run and erasure journals, the dispatch and
inbox projections' ``activity_recent``, the ledger by sequence -- and a page
reads at most ``limit + 1`` index rows from each, then merges them. Only the
rows on the page read a payload, so a page costs its rows, never the history or
the instance's size.

Order and paging. A row's key is ``(at, source, partition, position,
ordinal)``, newest first. The cursor carries the last row's key: each
journal source continues strictly below it, so a row recorded between pages
(newer than the key) is never repeated, and nothing below the key is skipped.
Accepted generations are read by sequence, and their acceptance times need
not follow sequence (a candidate's timestamp is checked for spelling, not for
order). A generation is therefore placed by an effective key: its acceptance
time clamped to no later than the one above it in the walk, a running minimum
from the walk's first head down, which the cursor carries (with the next
sequence to read) so a later page continues the same keys. Its ``at`` stays
its true acceptance time. A generation accepted after the walk's first page
is placed by its true time and listed if that is below the page boundary,
like a journal record landing between pages; each later page reads those
generations (the ones accepted during the walk), never the history below.

Missing payloads follow the best-effort exhaust rule: the row still appears,
marked ``unavailable`` (``record_unavailable`` when what the record said is
unknown), and the read never refuses for it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from pydantic import TypeAdapter

from cruxible_client.contracts.operational_reads import capture_handle
from cruxible_client.contracts.orient import (
    OrientActivity,
    OrientActivityCaptureErased,
    OrientActivityCaptureLanded,
    OrientActivityGenerationAccepted,
    OrientActivityRunFinished,
    OrientActivityRunStarted,
)
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.evidence.capture_erasures import CaptureErasureStore, erasure_journal_present
from cruxible_core.exhaust.capture_selectors import landing_logical_source
from cruxible_core.exhaust.journal_index import EventLocator
from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root
from cruxible_core.exhaust.line_inbox import LineInboxStore, inbox_root
from cruxible_core.exhaust.payloads import UnavailablePayload
from cruxible_core.indexes.history.history_index import HistoryReader
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.change_reasons import ChangeReasons
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.generation_rows import (
    generation_accepted_at,
    generation_row,
)
from cruxible_core.service.discovery.runs import (
    final_status,
    procedure_run_journal,
    procedure_run_stream,
    run_identity,
    run_payload,
)
from cruxible_core.storage.cas import BodyAccessContext

_ROW: TypeAdapter[OrientActivity] = TypeAdapter(OrientActivity)
_ACCESS = BodyAccessContext(principal_id="playbill-activity-reads", can_read_body=True)

#: A row's place in the feed: (at, source, partition, position, ordinal), newest first.
ActivityKey = tuple[str, str, str, int, int]
#: The highest and lowest (partition, position, ordinal) at one instant.
_HIGH: tuple[str, int, int] = ("\U0010ffff", 2**62, 2**62)
_LOW: tuple[str, int, int] = ("", -1, -1)
#: The Procedure-run journal's event kinds the feed lists, by source.
_RUN_SOURCES: Mapping[str, str] = {
    "runs.admission": "admission_bound",
    "runs.capture": "produced_capture",
    "runs.final": "attempt_finalized",
}


@dataclass(frozen=True)
class ActivityPosition:
    """Where a page stopped: its last row's key, and where the ledger walk stands.

    ``ledger_next`` is the next generation to read (``-1`` once genesis was
    listed), ``ledger_clamp`` the effective acceptance time of the last one
    listed (``None`` before any), and ``ledger_top`` the head the walk began at.
    """

    key: ActivityKey
    ledger_next: int
    ledger_clamp: str | None
    ledger_top: int

    def encode(self) -> list[str]:
        return [
            *self.key[:3],
            str(self.key[3]),
            str(self.key[4]),
            str(self.ledger_next),
            self.ledger_clamp or "",
            str(self.ledger_top),
        ]

    @classmethod
    def decode(cls, values: tuple[str, ...]) -> ActivityPosition | None:
        """The position a cursor carries, or ``None`` when it is not one."""

        if len(values) != 8:
            return None
        at, source, partition, position, ordinal, ledger, clamp, top = values
        try:
            key = (at, source, partition, int(position), int(ordinal))
            return cls(
                key=key, ledger_next=int(ledger), ledger_clamp=clamp or None, ledger_top=int(top)
            )
        except ValueError:
            return None


@dataclass(frozen=True)
class _Candidate:
    key: ActivityKey
    build: Callable[[], OrientActivity]
    #: The generation a ledger row of the walk's own history lists (not a late one).
    generation: int | None = None


def _instant(text: str) -> datetime:
    value = parse_datetime(text)
    assert value is not None
    return value


def _bound(source: str, after: ActivityPosition | None) -> tuple[str, str, int, int] | None:
    """Where one source continues: strictly below the last row's key, in its own order.

    At the last row's own instant, a source ordered after it (a smaller name)
    still owes its rows there; one ordered before it listed them already.
    """

    if after is None:
        return None
    at, last, partition, position, ordinal = after.key
    if source == last:
        return at, partition, position, ordinal
    return (at, *(_HIGH if source < last else _LOW))


class _Page:
    """The reads one page shares: memoized run identities and contract names."""

    def __init__(self, instance: PlaybillInstance, stack: ExitStack) -> None:
        self.instance = instance
        self._stack = stack
        self._runs: dict[str, tuple[str, str | None] | None] = {}
        self._names: CaptureContractNames | None = None

    def run(
        self, run_id: str, admission_digest: str | None = None
    ) -> tuple[str, str | None] | None:
        """A run's (Procedure, Line), from its admission payload: read once per page."""

        if run_id not in self._runs:
            journal = procedure_run_journal(self.instance)
            digest = admission_digest
            if digest is None and journal is not None:
                digest = journal.index.run_admission_digest(
                    procedure_run_stream(self.instance), run_id
                )
            self._runs[run_id] = None if digest is None else run_identity(self.instance, digest)
        return self._runs[run_id]

    def contract(self, digest: str) -> str:
        if self._names is None:
            head = self.instance.accepted_coordinate()
            projection = self._stack.enter_context(self.instance.bind_accepted_projection(head))
            self._names = CaptureContractNames(
                self.instance, head, connection=projection.typed.connection
            )
        return self._names.name(digest, qualified=True)


# -- sources ------------------------------------------------------------------------------


def _generation_candidate(
    instance: PlaybillInstance,
    history: HistoryReader,
    records: Any,
    reasons: ChangeReasons,
    sequence: int,
) -> tuple[str, Callable[[], OrientActivity]]:
    """One generation's true acceptance time (a row key) and the row it lists."""

    location = history.generation(sequence)
    at = str(format_datetime(generation_accepted_at(instance, history, records, location)))

    def build() -> OrientActivity:
        row = generation_row(instance, history, records, reasons, location)
        return OrientActivityGenerationAccepted(
            at=row.accepted_at,
            ref=row.proposal,
            sequence=row.sequence,
            git_oid=row.git_oid,
            proposal=row.proposal,
            proposer=row.proposer,
            approvals=row.approvals,
            because=row.because,
            because_truncated=row.because_truncated,
            members=row.members,
        )

    return at, build


def _ledger(
    instance: PlaybillInstance,
    history: HistoryReader,
    *,
    top: int,
    clamp: str | None,
    limit: int,
) -> list[_Candidate]:
    """Accepted generations from ``top`` down, keyed by a running minimum of their times.

    Each key is the generation's acceptance time, clamped to no later than the
    key above it (``clamp`` for the first): sorted by sequence, the keys are
    sorted newest first however the acceptance times run.
    """

    if top < 0:
        return []
    records = instance.retained_record_reader()
    reasons = ChangeReasons(instance)
    rows: list[_Candidate] = []
    for sequence in range(top, max(top - limit - 1, -1), -1):
        at, build = _generation_candidate(instance, history, records, reasons, sequence)
        clamp = at if clamp is None or at < clamp else clamp
        rows.append(_Candidate((clamp, "ledger", "", sequence, 0), build, generation=sequence))
    return rows


def _ledger_since(
    instance: PlaybillInstance, history: HistoryReader, *, after: ActivityPosition | None
) -> list[_Candidate]:
    """Generations accepted after the walk's first page that sort below its boundary.

    Keyed by their true acceptance times, as a journal record landing between
    pages is: one newer than the boundary is never listed by this walk.
    """

    if after is None or history.sequence <= after.ledger_top:
        return []
    records = instance.retained_record_reader()
    reasons = ChangeReasons(instance)
    rows = []
    for sequence in range(history.sequence, after.ledger_top, -1):
        at, build = _generation_candidate(instance, history, records, reasons, sequence)
        key: ActivityKey = (at, "ledger", "", sequence, 0)
        if key < after.key:
            rows.append(_Candidate(key, build))
    return sorted(rows, key=lambda item: item.key, reverse=True)


def _run_row(page: _Page, source: str, locator: EventLocator) -> OrientActivity:
    instance, at = page.instance, _instant(locator.recorded_at)
    run = str(locator.run_id)
    if source == "runs.admission":
        identity = page.run(run, locator.payload_digest)
        return OrientActivityRunStarted(
            at=at,
            ref=f"ProcedureRun:{run}",
            run=run,
            procedure=None if identity is None else identity[0],
            line=None if identity is None else identity[1],
            unavailable=identity is None,
        )
    identity = page.run(run)
    payload = run_payload(instance, locator.payload_digest)
    lost = isinstance(payload, UnavailablePayload)
    line = None if identity is None else identity[1]
    if source == "runs.final":
        return OrientActivityRunFinished(
            at=at,
            ref=f"ProcedureRun:{run}",
            run=run,
            procedure=None if identity is None else identity[0],
            line=line,
            status=final_status(payload),
            unavailable=lost or identity is None,
        )
    if lost or not isinstance(payload, Mapping):
        return OrientActivityCaptureLanded(at=at, run=run, line=line, unavailable=True)
    digest = payload.get("capture_digest")
    contract = payload.get("capture_contract_digest")
    source_name = landing_logical_source(instance.body_store(), payload, access=_ACCESS)
    return OrientActivityCaptureLanded(
        at=at,
        ref=None if not isinstance(digest, str) else f"Capture:{digest}",
        capture=None if not isinstance(digest, str) else capture_handle(digest),
        contract=None if not isinstance(contract, str) else page.contract(contract),
        logical_source=source_name if isinstance(source_name, str) else None,
        run=run,
        line=line,
        unavailable=isinstance(source_name, UnavailablePayload),
    )


def _runs(page: _Page, *, after: ActivityPosition | None, limit: int) -> Iterator[list[_Candidate]]:
    journal = procedure_run_journal(page.instance)
    if journal is None:
        return
    stream = procedure_run_stream(page.instance)
    for source, kind in _RUN_SOURCES.items():
        bound = _bound(source, after)
        locators = journal.index.newest_events(
            stream,
            event_kind=kind,
            limit=limit + 1,
            before=None if bound is None else bound[:3],
        )
        yield [
            _Candidate(
                (item.recorded_at, source, item.partition_id, item.position, 0),
                partial(_run_row, page, source, item),
            )
            for item in locators
        ]


def _dispatch(
    instance: PlaybillInstance, *, after: ActivityPosition | None, limit: int
) -> list[_Candidate]:
    if not dispatch_root(instance).is_dir():
        return []
    bound = _bound("dispatch", after)
    rows = LineDispatchStore(instance).activity(
        limit=limit + 1, before=None if bound is None else (bound[0], bound[2])
    )

    def build(item: Any) -> OrientActivity:
        fields: dict[str, Any] = {"event": item.event, "at": item.recorded_at, **item.fields}
        if item.event == "record_unavailable":
            fields["journal"] = "line_dispatch"
        else:
            fields.update(ref=item.line, line=item.line)
        return _ROW.validate_python(fields)

    return [
        _Candidate(
            (item.recorded_at, "dispatch", "dispatch", item.position, 0),
            partial(build, item),
        )
        for item in rows
    ]


def _inbox(
    instance: PlaybillInstance, *, after: ActivityPosition | None, limit: int
) -> list[_Candidate]:
    if not inbox_root(instance).is_dir():
        return []
    bound = _bound("inbox", after)
    rows = LineInboxStore(instance).activity(
        limit=limit + 1, before=None if bound is None else (bound[0], bound[2], bound[3])
    )

    def build(item: Any) -> OrientActivity:
        fields: dict[str, Any] = {"event": item.event, "at": item.recorded_at, **item.fields}
        if item.event == "record_unavailable":
            fields["journal"] = "line_inbox"
        else:
            fields["ref"] = fields.pop("item")
        return _ROW.validate_python(fields)

    return [
        _Candidate(
            (item.recorded_at, "inbox", "inbox", item.position, item.ordinal),
            partial(build, item),
        )
        for item in rows
    ]


def _erasures(
    instance: PlaybillInstance, *, after: ActivityPosition | None, limit: int
) -> list[_Candidate]:
    if not erasure_journal_present(instance):
        return []
    store = CaptureErasureStore(instance)
    bound = _bound("erasures", after)
    locators = store.newest(limit=limit + 1, before=None if bound is None else bound[:3])

    def build(locator: EventLocator) -> OrientActivity:
        at = _instant(locator.recorded_at)
        record = store.read_record(locator)
        if isinstance(record, UnavailablePayload):
            return OrientActivityCaptureErased(at=at, unavailable=True)
        return OrientActivityCaptureErased(
            at=at,
            ref=f"Capture:{record.capture_digest}",
            capture=capture_handle(record.capture_digest),
            contract=record.capture_contract,
            by=record.erased_by,
        )

    return [
        _Candidate(
            (item.recorded_at, "erasures", item.partition_id, item.position, 0),
            partial(build, item),
        )
        for item in locators
    ]


# -- the page -------------------------------------------------------------------------------


def activity_page(
    instance: PlaybillInstance, *, limit: int, after: ActivityPosition | None
) -> tuple[tuple[OrientActivity, ...], ActivityPosition | None]:
    """One page of the feed, newest first, and where the next page continues.

    Each source contributes at most ``limit + 1`` index rows, merged by key;
    only the rows kept read their payloads. The position is ``None`` on the
    last page.
    """

    with ExitStack() as stack:
        page = _Page(instance, stack)
        built: dict[int, OrientActivity] = {}
        with instance.accepted_history_reader() as history:
            top = history.sequence if after is None else after.ledger_top
            ledger_next = top if after is None else after.ledger_next
            clamp = None if after is None else after.ledger_clamp
            sources = [
                _ledger(instance, history, top=ledger_next, clamp=clamp, limit=limit),
                _ledger_since(instance, history, after=after),
                *_runs(page, after=after, limit=limit),
                _dispatch(instance, after=after, limit=limit),
                _inbox(instance, after=after, limit=limit),
                _erasures(instance, after=after, limit=limit),
            ]
            kept, more = _merge(sources, limit)
            # A generation row reads the history it was located in; the rest
            # read their payloads once the history reader is released.
            for index, item in enumerate(kept):
                if item.key[1] == "ledger":
                    built[index] = item.build()
        rows = tuple(
            built[index] if index in built else item.build() for index, item in enumerate(kept)
        )
    if not more or not kept:
        return rows, None
    listed = [item for item in kept if item.generation is not None]
    return rows, ActivityPosition(
        key=kept[-1].key,
        ledger_next=listed[-1].key[3] - 1 if listed else ledger_next,
        ledger_clamp=listed[-1].key[0] if listed else clamp,
        ledger_top=top,
    )


def _merge(sources: list[list[_Candidate]], limit: int) -> tuple[list[_Candidate], bool]:
    """The newest ``limit`` candidates, each source in its own order; whether more remain."""

    heads = [0] * len(sources)
    kept: list[_Candidate] = []
    while len(kept) < limit:
        best: int | None = None
        for index, rows in enumerate(sources):
            if heads[index] < len(rows) and (
                best is None or rows[heads[index]].key > sources[best][heads[best]].key
            ):
                best = index
        if best is None:
            break
        kept.append(sources[best][heads[best]])
        heads[best] += 1
    return kept, any(heads[index] < len(rows) for index, rows in enumerate(sources))


__all__ = ["ActivityPosition", "activity_page"]
