"""Failed Line runs, followed from the run journal within the next consumer.

Each step reads the next page of ``attempt_finalized`` records after the
persisted cursor (a keyed read by event kind, never a scan). A record naming a
Line spec costs one final-payload read and, when it failed, succeeded or
halted, one admission read. A failed run of a Trigger's occurrence (a matched
or dispatched one, never a manual ``line run``: its caller saw the failure)
records the Line's latest failure; a later succeeded or halted top-level run
of the Line clears it. The cursor and rows commit together; a rebuilt journal
index starts over from its beginning, which replays the same rows. Reads query
the rows only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cruxible_core.consumers.protocol import ConsumerHealth, ConsumerWork
from cruxible_core.consumers.state import DisposableState
from cruxible_core.exhaust.payloads import UnavailablePayload, read_payload
from cruxible_core.storage.cas import BodyAccessContext

#: Journal records one step reads.
BATCH = 256
#: Steps one unit of work, or one ``next`` read, runs before yielding.
STEPS = 4

_ACCESS = BodyAccessContext(principal_id="cruxible-next-run-failures", can_read_body=True)
_STATE = DisposableState(
    "next/run-failures",
    """
CREATE TABLE progress (singleton INTEGER PRIMARY KEY, identity TEXT NOT NULL,
 record_id INTEGER NOT NULL) STRICT;
INSERT INTO progress VALUES(1,'',0);
CREATE TABLE failed (line TEXT PRIMARY KEY, run_id TEXT NOT NULL, finalized_at TEXT NOT NULL,
 failure_code TEXT, cause TEXT) STRICT;
""",
)


@dataclass(frozen=True)
class LineRunFailure:
    """A Line's latest failed run of a Trigger's occurrence, not yet followed by a success."""

    line: str
    run_id: str
    finalized_at: str
    failure_code: str | None
    cause: dict[str, Any] | None


def _page(instance: Any, after: int) -> tuple[str, tuple[tuple[int, Any], ...]]:
    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    journal, _root = _journal(instance)
    return journal.index.kind_after(
        _stream(instance), event_kind="attempt_finalized", after=after, limit=BATCH
    )


def _behind(instance: Any) -> bool:
    with _STATE.open(instance, create=False) as connection:
        cursor = (
            ("", 0)
            if connection is None
            else connection.execute("SELECT identity,record_id FROM progress").fetchone()
        )
    identity, page = _page(instance, int(cursor[1]))
    return bool(page) or (identity != cursor[0] and int(cursor[1]) > 0)


def _line_of(instance: Any, journal: Any, stream: Any, run_id: str) -> tuple[str, bool] | None:
    """The run's Line identity and whether a Trigger's occurrence admitted it, top-level only."""

    from cruxible_core.procedures.execution import parse_admission_payload

    admissions = journal.select_records(
        stream, run_id=run_id, event_kind="admission_bound", limit=1
    )
    if not admissions:
        return None
    payload = read_payload(
        instance.body_store(), admissions[0].record.payload_digest, access=_ACCESS
    )
    if isinstance(payload, UnavailablePayload):
        return None
    admission = parse_admission_payload(payload).admission
    line = admission.line_identity
    if line is None or admission.parent_binding is not None:
        return None
    return line.qualified, admission.trigger_binding is not None


def catch_up(instance: Any, *, steps: int = STEPS) -> None:
    """Fold up to ``steps`` pages of finished runs after the persisted cursor."""

    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    journal, _root = _journal(instance)
    stream = _stream(instance)
    for _step in range(steps):
        with _STATE.open(instance) as connection:
            assert connection is not None
            known, after = connection.execute("SELECT identity,record_id FROM progress").fetchone()
            identity, page = _page(instance, int(after))
            if identity != known and after:
                # Positions belong to one index identity: start over, from nothing.
                connection.execute("DELETE FROM failed")
                identity, page = _page(instance, 0)
            for _position, stored in page:
                record = stored.record
                if record.line_spec_digest is None or record.run_id is None:
                    continue
                final = read_payload(instance.body_store(), record.payload_digest, access=_ACCESS)
                status = final.get("status") if isinstance(final, dict) else None
                if status not in {"failed", "succeeded", "halted"}:
                    continue
                found = _line_of(instance, journal, stream, record.run_id)
                if found is None:
                    continue
                line, triggered = found
                if status != "failed":
                    connection.execute("DELETE FROM failed WHERE line=?", (line,))
                elif triggered:
                    assert isinstance(final, dict)
                    cause = final.get("failure_cause")
                    connection.execute(
                        "INSERT OR REPLACE INTO failed VALUES(?,?,?,?,?)",
                        (
                            line,
                            record.run_id,
                            record.recorded_at.isoformat(),
                            final.get("failure_code"),
                            None if cause is None else json.dumps(cause, sort_keys=True),
                        ),
                    )
            through = page[-1][0] if page else (after if identity == known else 0)
            connection.execute(
                "UPDATE progress SET identity=?,record_id=?", (identity, int(through))
            )
        if len(page) < BATCH:
            return


def line_run_failures(instance: Any) -> tuple[LineRunFailure, ...]:
    """Each Line's latest failed Trigger-occurrence run, after a bounded catch-up."""

    catch_up(instance)
    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            return ()
        rows = connection.execute(
            "SELECT line,run_id,finalized_at,failure_code,cause FROM failed ORDER BY line"
        ).fetchall()
    return tuple(
        LineRunFailure(
            line=str(line),
            run_id=str(run_id),
            finalized_at=str(finalized_at),
            failure_code=None if code is None else str(code),
            cause=None if cause is None else json.loads(cause),
        )
        for line, run_id, finalized_at, code, cause in rows
    )


class RunFailuresPart:
    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        # The journal index is the subscription; the cursor lives with the rows.
        return None

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        return (ConsumerWork(key="follow", item="follow"),) if _behind(instance) else ()

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        catch_up(manager.get(instance_id))

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        behind = _behind(instance)
        return (
            ConsumerHealth(
                kind="next",
                consumer_id="consumer:next",
                state="lagging" if behind else "running",
                detail={"run_failures_pending": behind},
            ),
        )


_PART = RunFailuresPart()
