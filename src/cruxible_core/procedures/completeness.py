"""The completeness law: every runtime value is complete or not, and sinks can tell.

A value is complete when everything it was derived from was complete: its data
lineage (the aliases it reads) and its control lineage (the guards on the path
that reached it). Incompleteness arises where data was not fully read, never
from a guess:

- an ``extract`` whose ``complete_when`` did not hold, that met an invalid
  record, or a duplicate under ``refuse_key``;
- a ``filter``, ``aggregate`` or ``derive`` that evaluated a predicate to
  ``unknown``;
- any node reading an incomplete alias (``shape``, ``join``, ``dedupe``,
  ``union``, ``project``, ``select``, ``call``, ``invoke``);
- a ``guard`` over an incomplete operand, for every node on its branch;
- an ``invoke`` whose child returned an incomplete value;
- a read of an extract's ``rows`` view that reaches an invalid field.

An extract's (and a closed transform's) ``rows`` view holds each record's
present field values only, so a
field the extractor found invalid reads there as plain absence. The view keeps
which fields it dropped; any read through it that reaches one (``rows``, a
row, or that row's field) is incomplete, so an unreadable value can never pass
as an ordinary absence. ``records`` and ``results`` keep every field's result
and need no such rule: a closed transform reading them evaluates an invalid
field ``unknown``.

The real sinks (a negative downgraded to the ClaimType's unknown member, a
destructive change falling back to ``surface``) belong to the Claims terminal;
here a sink only learns whether what it receives is complete, through a
:class:`CompletenessProbe`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

#: One read of a run value: the alias, and the path read beneath it. ``()``
#: reads the whole value; ``None`` reads no field content (a count, say).
Read = tuple[str, tuple[str, ...] | None]


class CompletenessProbe(Protocol):
    """Told, at each sink, whether the value it receives is complete."""

    def __call__(self, node_id: str, complete: bool) -> None: ...


@dataclass
class Completeness:
    """Which aliases hold incomplete values, and whether the control lineage is complete."""

    incomplete: set[str] = field(default_factory=set)
    control_complete: bool = True
    #: Per alias, each lossy ``rows`` view it holds, by the path to the value
    #: that has it (``()`` for the alias itself; ``("final", "<body>")`` for a
    #: repeat's body output): row index -> the names of its invalid fields.
    #: Only rows that dropped one are held.
    dropped: dict[str, dict[tuple[str, ...], dict[int, frozenset[str]]]] = field(
        default_factory=dict
    )

    def complete(self, aliases: Iterable[str]) -> bool:
        """Whether every alias read is complete (data lineage only)."""

        return not any(alias in self.incomplete for alias in aliases)

    def drop(
        self, alias: str, rows: Mapping[int, frozenset[str]], *, at: tuple[str, ...] = ()
    ) -> None:
        """Record the invalid fields a ``rows`` view leaves out, at ``at`` within ``alias``."""

        views = self.dropped.setdefault(alias, {})
        if rows:
            views[at] = dict(rows)
        else:
            views.pop(at, None)
        if not views:
            self.dropped.pop(alias, None)

    def reads_complete(self, reads: Iterable[Read], *, records_kept: bool = False) -> bool:
        """Whether every read is of a complete value and reaches no dropped field.

        ``records_kept`` is set by a reader that consumes a records value whole
        through its ``records`` (a closed transform), where every field keeps
        its result. A read of a value that contains a lossy view (a repeat's
        ``final``, say) reaches it, and is incomplete.
        """

        for alias, path in reads:
            if alias in self.incomplete:
                return False
            views = self.dropped.get(alias)
            if views is None or path is None:
                continue
            for at, rows in views.items():
                if path[: len(at)] != at:
                    if at[: len(path)] == path:
                        return False  # the read holds the lossy view inside it
                    continue
                if not _view_read_complete(path[len(at) :], rows, records_kept=records_kept):
                    return False
        return True

    def mark(self, alias: str, complete: bool) -> None:
        if complete:
            self.incomplete.discard(alias)
        else:
            self.incomplete.add(alias)

    def at_sink(self, reads: Iterable[Read]) -> bool:
        """A sink's value counts as complete only if its data and control lineage are."""

        return self.control_complete and self.reads_complete(reads)

    def guard(self, reads: Iterable[Read]) -> None:
        """A guard over an incomplete operand taints the branch it selects."""

        if not self.reads_complete(reads):
            self.control_complete = False


def _view_read_complete(
    path: tuple[str, ...], rows: Mapping[int, frozenset[str]], *, records_kept: bool
) -> bool:
    """Whether a read at ``path`` within a lossy records value meets no dropped field."""

    if not path:
        return records_kept
    if path[0] != "rows":
        return True
    if len(path) == 1 or not path[1].isdigit():
        return False
    names = rows.get(int(path[1]))
    return names is None or (len(path) > 2 and path[2] not in names)


def dropped_rows(value: object) -> dict[int, frozenset[str]]:
    """A records value's ``rows`` entries that leave out an invalid field: index -> fields.

    A value whose records carry field results (an extract's, a closed
    transform's) shows them in ``rows`` as present values only.
    """

    records = value.get("records") if isinstance(value, Mapping) else None
    if not isinstance(records, list):
        return {}
    dropped: dict[int, frozenset[str]] = {}
    for index, record in enumerate(records):
        fields = record.get("fields") if isinstance(record, Mapping) else None
        if not isinstance(fields, Mapping):
            continue
        invalid = frozenset(
            name
            for name, result in fields.items()
            if isinstance(result, Mapping) and result.get("tag") == "invalid"
        )
        if invalid:
            dropped[index] = invalid
    return dropped


__all__ = ["Completeness", "CompletenessProbe", "Read", "dropped_rows"]
