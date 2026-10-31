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
- a ``guard`` over an incomplete operand, for every node on its branch.

The real sinks (a negative downgraded to the ClaimType's unknown member, a
destructive change falling back to ``surface``) belong to the Claims terminal;
here a sink only learns whether what it receives is complete, through a
:class:`CompletenessProbe`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


class CompletenessProbe(Protocol):
    """Told, at each sink, whether the value it receives is complete."""

    def __call__(self, node_id: str, complete: bool) -> None: ...


@dataclass
class Completeness:
    """Which aliases hold incomplete values, and whether the control lineage is complete."""

    incomplete: set[str] = field(default_factory=set)
    control_complete: bool = True

    def complete(self, aliases: Iterable[str]) -> bool:
        """Whether every alias read is complete (data lineage only)."""

        return not any(alias in self.incomplete for alias in aliases)

    def mark(self, alias: str, complete: bool) -> None:
        if complete:
            self.incomplete.discard(alias)
        else:
            self.incomplete.add(alias)

    def at_sink(self, aliases: Iterable[str]) -> bool:
        """A sink's value counts as complete only if its data and control lineage are."""

        return self.control_complete and self.complete(aliases)

    def guard(self, aliases: Iterable[str]) -> None:
        """A guard over an incomplete operand taints the branch it selects."""

        if not self.complete(aliases):
            self.control_complete = False


__all__ = ["Completeness", "CompletenessProbe"]
