"""Runbook reads (D-R48, D-R36 follow-up): the ``get Runbook:<name>`` card and
the ``orient(section="runbooks")`` rows.

The card's definition parts are read at the requested coordinate: one typed
source read for the Runbook, one per step for the Line it names (whether that
Line can run now), and one Trigger-index seek for the Triggers aimed at it
(``triggers_by_target``). Its recent runs are the Runbook's retained run
records, read live with one ``runs_by_runbook`` seek bounded by the card's
limit. Nothing scans the Runbooks, the Lines or the runs: the card's work is
its own steps plus constants, whatever else the instance holds.

The orient rows page the ``runbooks`` index by name (a keyset page: one seek
per page), one Trigger-index count for the page's Runbooks and one run-record
seek per row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from cruxible_client.contracts.operational_reads import (
    OPERATIONAL_CARD_LIST_LIMIT,
    RUNBOOK_CARD_RUNS,
    GetLineTrigger,
    GetRunbookCard,
    GetRunbookStep,
    OrientRunbook,
    RunbookStepAvailability,
)
from cruxible_client.contracts.procedures.line_specs import LineSpec
from cruxible_client.contracts.runbooks import Runbook, RunbookRunResult
from cruxible_client.contracts.triggers import INTERNAL_ACTIONS, ActionTarget, Trigger
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.operational import (
    RenderGet,
    aimed_trigger_page,
    schedule_summary,
)


def _revision(projection: Any, identity: str) -> int:
    row = projection.typed.envelope(identity)
    return 1 if row is None else int(row.revision)


def _step(projection: Any, index: int, target: Any) -> GetRunbookStep:
    if isinstance(target, ActionTarget):
        availability: RunbookStepAvailability = (
            "available" if target.action in INTERNAL_ACTIONS else "action_unregistered"
        )
        return GetRunbookStep(
            step=index, target=f"action:{target.action}", availability=availability
        )
    identity = target.line.target.qualified
    line = projection.typed.source(identity)
    availability = (
        "line_missing"
        if not isinstance(line, LineSpec)
        else "line_retired"
        if line.lifecycle.state != "live"
        else "line_takes_event"
        if line.trigger_input is not None
        else "available"
    )
    return GetRunbookStep(step=index, target=identity, availability=availability)


def recent_runbook_runs(
    instance: PlaybillInstance, name: str, *, limit: int
) -> tuple[tuple[RunbookRunResult, ...], bool]:
    """The Runbook's newest retained runs (at most ``limit``) and whether older ones exist.

    One ``runs_by_runbook`` seek reading ``limit + 1`` rows: constant in the
    Runbook's other runs and every other Runbook's.
    """

    import json

    from cruxible_core.service.runbooks import RUNBOOK_STATE

    with RUNBOOK_STATE.open(instance, create=False) as connection:
        if connection is None:
            return (), False
        rows = connection.execute(
            "SELECT payload FROM runs INDEXED BY runs_by_runbook WHERE runbook=? "
            "ORDER BY finished_at DESC LIMIT ?",
            (name, limit + 1),
        ).fetchall()
    runs = tuple(RunbookRunResult.model_validate(json.loads(row[0])) for row in rows[:limit])
    return runs, len(rows) > limit


def runbook_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    identity: str,
    *,
    render: RenderGet,
) -> GetRunbookCard:
    with instance.bind_accepted_projection(coordinate) as projection:
        runbook = cast(Runbook, projection.typed.source(identity))
        steps = tuple(
            _step(projection, index, target) for index, target in enumerate(runbook.steps, start=1)
        )
        aimed = aimed_trigger_page(
            projection, (identity,), limit=OPERATIONAL_CARD_LIST_LIMIT, target_kind="runbook"
        )[identity]
        triggers = tuple(
            GetLineTrigger(
                trigger=name,
                version=_revision(projection, name),
                fires=schedule_summary(cast(Trigger, projection.typed.source(name)).schedule),
            )
            for name in aimed.identities
        )
    runs, more = recent_runbook_runs(instance, runbook.identity.name, limit=RUNBOOK_CARD_RUNS)
    next_steps = [render(step.target, None) for step in steps if step.target.startswith("Line:")]
    next_steps.extend(render(item.trigger, None) for item in triggers)
    if runs:
        # The latest run's first Line run: where its outcome came from.
        latest_line_run = next((step.run_id for step in runs[0].steps if step.run_id), None)
        if latest_line_run is not None:
            next_steps.append(render(f"ProcedureRun:{latest_line_run}", None))
    next_steps.append(render(identity, "history"))
    return GetRunbookCard(
        runbook=identity,
        description=runbook.description,
        lifecycle=runbook.lifecycle.state,
        steps=steps,
        triggers=triggers,
        triggers_total=aimed.total,
        recent_runs=runs,
        runs_more=more,
        next=tuple(dict.fromkeys(next_steps)),
    )


def _latest_runs(
    instance: PlaybillInstance, names: Sequence[str]
) -> dict[str, tuple[str, datetime]]:
    """Per Runbook name, its latest run's status and finish: one seek per name."""

    from cruxible_core.service.runbooks import RUNBOOK_STATE

    if not names:
        return {}
    found: dict[str, tuple[str, datetime]] = {}
    with RUNBOOK_STATE.open(instance, create=False) as connection:
        if connection is None:
            return {}
        for name in names:
            row = connection.execute(
                "SELECT json_extract(payload,'$.status'), finished_at FROM runs "
                "INDEXED BY runs_by_runbook WHERE runbook=? ORDER BY finished_at DESC LIMIT 1",
                (name,),
            ).fetchone()
            if row is not None:
                found[name] = (str(row[0]), datetime.fromisoformat(str(row[1])))
    return found


def runbook_rows(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    limit: int,
    after: str | None,
) -> tuple[tuple[OrientRunbook, ...], str | None]:
    """One keyset page of accepted Runbooks by identity, and the key to continue after."""

    with instance.bind_accepted_projection(coordinate) as projection:
        identities = [
            str(row[0])
            for row in projection.typed.connection.execute(
                "SELECT identity FROM runbooks WHERE identity>? ORDER BY identity LIMIT ?",
                ("" if after is None else after, limit + 1),
            )
        ]
        page = identities[:limit]
        runbooks = [cast(Runbook, projection.typed.source(identity)) for identity in page]
        aimed = aimed_trigger_page(projection, tuple(page), limit=0, target_kind="runbook")
    latest = _latest_runs(instance, [runbook.identity.name for runbook in runbooks])
    rows = []
    for identity, runbook in zip(page, runbooks, strict=True):
        last = latest.get(runbook.identity.name)
        rows.append(
            OrientRunbook(
                runbook=identity,
                lifecycle=runbook.lifecycle.state,
                description=runbook.description,
                steps=len(runbook.steps),
                triggers_total=aimed[identity].total,
                last_run=None if last is None else last[0],  # type: ignore[arg-type]
                last_run_at=None if last is None else last[1],
            )
        )
    stop = page[-1] if len(identities) > limit and page else None
    return tuple(rows), stop


__all__ = ["recent_runbook_runs", "runbook_card", "runbook_rows"]
