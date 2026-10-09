"""Count settled items matching earlier refusals in post, within the next consumer.

Settle delivery does nothing for `next`'s ``line_settled_prior_refusals`` row.
Each tick, when the run journal holds settle-terminal records past the refusal
index's persisted cursor, one bounded step counts them
(`count_settled_refusals_step`). A busy or unbuilt index skips the tick; the
cursor makes that lossless, and nothing is held in memory.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from cruxible_core.consumers.protocol import ConsumerHealth, ConsumerWork

#: Steps one unit of work runs before yielding to the next tick.
STEPS_PER_WORK = 4


def _behind(instance: Any) -> bool | None:
    """Whether settle records wait past the cursor; ``None`` when that is unknown now."""

    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    cursor = instance.refusal_index().settled_cursor(instance, wait=False)
    if cursor is None:
        return None
    journal, _root = _journal(instance)
    identity, page = journal.index.kind_after(
        _stream(instance), event_kind="terminal_egress", after=cursor.record_id, limit=1
    )
    return bool(page) or (identity != cursor.journal_identity and cursor.record_id > 0)


class SettledRefusalsPart:
    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        # The journal index is the subscription; the cursor lives with the counts.
        return None

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        # Unknown (the index is busy, or not built yet) is not due: a later tick,
        # or the read that builds the index, finds the records past the cursor.
        if _behind(instance) is True:
            return (ConsumerWork(key="count", item="count"),)
        return ()

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        from cruxible_core.service.procedures.settled_refusals import catch_up_settled_refusals

        catch_up_settled_refusals(manager.get(instance_id), steps=STEPS_PER_WORK, wait=False)

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        behind = _behind(instance) is True
        return (
            ConsumerHealth(
                kind="next",
                consumer_id="consumer:next",
                state="lagging" if behind else "running",
                detail={"settled_counts_pending": behind},
            ),
        )


_PART = SettledRefusalsPart()
