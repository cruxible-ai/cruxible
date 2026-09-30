"""Procedure runs as read rows: listed from the journal index, never fully replayed.

A run's authoritative account is its journal chain, rebuilt by
``service_get_playbill_procedure_run``. Listing many runs never replays them:
the journal index locates each run's admission and final records, and only a
page's rows read those two payloads (the Procedure and Line from the admission,
the terminal status from the final record). Rows list running runs first, then
the newest admissions first.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import get_args

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.operational_reads import PlaybillRunRowV1, PlaybillRunStatus
from cruxible_client.contracts.procedures.results import ProcedureOperationalFailureCodeV1
from cruxible_client.contracts.temporal import parse_datetime
from cruxible_core.exhaust import LocalJournalBackend
from cruxible_core.exhaust.journal_index import RunLocator, RunLocatorKey
from cruxible_core.exhaust.records import JournalStreamIdentityV1, parse_journal_payload
from cruxible_core.procedures.execution import parse_admission_payload, procedure_line_partition
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.cas import BodyAccessContext

_ACCESS = BodyAccessContext(principal_id="playbill-run-reads", can_read_body=True)
_OPERATIONAL_FAILURES = frozenset(get_args(ProcedureOperationalFailureCodeV1))


def procedure_run_journal(instance: PlaybillInstance) -> LocalJournalBackend | None:
    """The Procedure-run journal, or ``None`` when no run was ever journaled.

    A read never creates the journal directory.
    """

    root: Path = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    return LocalJournalBackend(root) if root.is_dir() else None


def _stream(instance: PlaybillInstance) -> JournalStreamIdentityV1:
    from cruxible_core.service.procedures.procedure_runs import _stream as run_stream

    return run_stream(instance)


def final_status(payload: object) -> PlaybillRunStatus:
    """The served run status an ``attempt_finalized`` payload records."""

    raw = payload.get("status") if isinstance(payload, Mapping) else None
    if raw == "succeeded":
        return "succeeded"
    if raw == "halted":
        return "halted"
    if raw in {"refused", "budget_exhausted"}:
        return "node_refused"
    if raw == "failed" and isinstance(payload, Mapping):
        if payload.get("failure_code") in _OPERATIONAL_FAILURES:
            return "operational_failed"
    return "internal_failed"


def run_row(instance: PlaybillInstance, locator: RunLocator) -> PlaybillRunRowV1:
    """One located run as a row, reading its admission and (if any) final payloads."""

    bodies = instance.body_store()
    admission = parse_admission_payload(
        parse_journal_payload(bodies.read(locator.admission_payload_digest, access=_ACCESS))
    ).admission
    status: PlaybillRunStatus = "running"
    if locator.final_payload_digest is not None:
        status = final_status(
            parse_journal_payload(bodies.read(locator.final_payload_digest, access=_ACCESS))
        )
    line = getattr(admission, "line_identity", None)
    started = parse_datetime(locator.admitted_at)
    assert started is not None
    return PlaybillRunRowV1(
        run=locator.run_id,
        procedure=admission.procedure_identity.qualified,
        status=status,
        started_at=started,
        line=None if line is None else line.qualified,
        nodes_done=locator.nodes_done,
    )


def run_rows(
    instance: PlaybillInstance,
    *,
    limit: int,
    line: ArtifactIdentity | None = None,
    after: RunLocatorKey | None = None,
) -> tuple[tuple[PlaybillRunRowV1, ...], RunLocatorKey | None]:
    """One page of runs (of one Line when ``line`` is its identity) and where it stopped.

    The key is ``None`` when the page is the last one.
    """

    journal = procedure_run_journal(instance)
    if journal is None:
        return (), None
    partition = None if line is None else procedure_line_partition(line)
    locators, more = journal.index.run_locators(
        _stream(instance), limit=limit, partition_id=partition, after=after
    )
    rows = tuple(run_row(instance, locator) for locator in locators)
    return rows, (locators[-1].key if more and locators else None)


def run_counts(
    instance: PlaybillInstance, *, line: ArtifactIdentity | None = None
) -> tuple[int, int]:
    """(admitted runs, of which still running), of one Line when ``line`` is given."""

    journal = procedure_run_journal(instance)
    if journal is None:
        return 0, 0
    partition = None if line is None else procedure_line_partition(line)
    return journal.index.run_counts(_stream(instance), partition_id=partition)


__all__ = [
    "final_status",
    "procedure_run_journal",
    "run_counts",
    "run_row",
    "run_rows",
]
