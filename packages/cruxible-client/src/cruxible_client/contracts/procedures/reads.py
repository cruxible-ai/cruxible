"""Procedure reads run to the end: one ceiling, and a loud refusal past it.

A ``read`` node evaluates its Query, or selects its ClaimType's Claims, under
the run's Procedure read ceiling (its remaining ``max_read_rows``), never under
the Query's interactive budgets. Any limit the ceiling clips (results, paths,
paths per result, include items, Claims) refuses the read with
``read_ceiling_exceeded``; a clipped result is never reported ``completed``.

The ceiling is cumulative across every read of a run and of the children it
invokes. A run's own budget is ``max_read_rows`` as its Line and Mandate
admit it (each can only lower the Procedure's hard cap), and an invoked child
gets what its parent has left.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from cruxible_client.contracts.procedures.model_base import _StrictProcedureModel

#: The run read ceiling a Procedure gets when its hard caps state none.
DEFAULT_MAX_READ_ROWS = 100_000
READ_CEILING_EXCEEDED = "read_ceiling_exceeded"


def effective_read_rows(budget_rows: int | None, cap_rows: int) -> int:
    """The rows a run's reads may return in all: its budget's, else its hard cap's."""

    return cap_rows if budget_rows is None else min(budget_rows, cap_rows)


class ProcedureReadCeiling(_StrictProcedureModel):
    """How many rows one Procedure read may return: the run's remaining read ceiling."""

    tag: Literal["cruxible-procedure-read-ceiling-v1"] = "cruxible-procedure-read-ceiling-v1"
    rows: int = Field(ge=1, le=2**31 - 1)


__all__ = [
    "DEFAULT_MAX_READ_ROWS",
    "READ_CEILING_EXCEEDED",
    "ProcedureReadCeiling",
    "effective_read_rows",
]
