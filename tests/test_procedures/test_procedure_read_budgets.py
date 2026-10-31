"""A run's read budget: one cumulative ceiling its Line, Mandates and parent can only lower.

``max_read_rows`` is a hard cap a Procedure declares (authored ``read_rows``).
A run's reads, Query and Claim alike, draw on one budget: the Line's stated
``max_read_rows`` (else the cap), lowered by every live Mandate's resource
ceiling. An invoked child gets what its parent has left, so the parent and its
children never read past the parent's budget between them. These run on the
seeded work-item world: two Subjects, one status Claim each.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts.procedures.authored import ProcedureSpec
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    ProcedureRunRequest,
    _line_budget,
    service_run_playbill_line,
    service_run_playbill_procedure,
)
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    seed_claims,
)
from tests.support.operation_world import actor, author, author_procedure

NOW = datetime.fromisoformat(EVALUATION_TIME)


def _reading(name: str, subjects: tuple[str, ...], *, caps: dict[str, Any] | None = None) -> Any:
    steps: list[dict[str, Any]] = [
        {"read": f"read_{index}", "claim_type": PREDICATE, "subject": subject}
        for index, subject in enumerate(subjects)
    ]
    steps.append({"project": "out", "fields": {"count": len(subjects)}})
    return ProcedureSpec.model_validate(
        {
            "procedure": name,
            "input": {},
            "output": {"count": "integer"},
            "steps": steps,
            "returns": "out",
            **({} if caps is None else {"caps": caps, "budget": caps}),
        }
    )


def _run(instance: Any, name: str) -> Any:
    return service_run_playbill_procedure(
        instance,
        name=name,
        request=ProcedureRunRequest(input={}, evaluation_time=NOW),
        actor_context=actor().model_copy(update={"timestamp": NOW}),
    )


def _refusal(state: Any) -> str:
    return "" if state.terminal is None else str(state.terminal.model_dump(mode="json"))


@pytest.mark.parametrize(("read_rows", "status"), [(2, "succeeded"), (1, "admission_refused")])
def test_a_procedures_read_rows_cap_bounds_all_its_reads_together(
    tmp_path: Path, read_rows: int, status: str
) -> None:
    instance, _ = seed_claims(tmp_path)
    author_procedure(
        instance, _reading("test.two-reads", ("wi-42", "wi-43"), caps={"read_rows": read_rows})
    )

    state = _run(instance, "test.two-reads")

    assert state.status == status, _refusal(state)
    if status != "succeeded":
        assert "read_ceiling_exceeded" in _refusal(state)


@pytest.mark.parametrize(("read_rows", "refused"), [(2, False), (1, True)])
def test_an_invoked_child_reads_only_what_its_parent_has_left(
    tmp_path: Path, read_rows: int, refused: bool
) -> None:
    """F9: the parent reads one Claim; its child's read draws on the same budget."""

    instance, _ = seed_claims(tmp_path)
    author_procedure(instance, _reading("test.child-read", ("wi-43",)), name="child")
    parent = ProcedureSpec.model_validate(
        {
            "procedure": "test.parent-read",
            "input": {},
            "output": {"count": "integer"},
            "steps": [
                {"read": "mine", "claim_type": PREDICATE, "subject": "wi-42"},
                {"invoke": "test.child-read", "id": "child", "input": {}},
                {"project": "out", "fields": {"count": 2}},
            ],
            "returns": "out",
            "caps": {"read_rows": read_rows},
            "budget": {"read_rows": read_rows},
        }
    )
    author_procedure(instance, parent, name="parent")

    state = _run(instance, "test.parent-read")

    if refused:
        assert state.status != "succeeded"
        assert "read_ceiling_exceeded" in _refusal(state)
    else:
        assert state.status == "succeeded", _refusal(state)


def _line(instance: Any, *, max_read_rows: int | None) -> None:
    from cruxible_client.contracts.authoring.inputs import LineInput

    budgets: dict[str, int] = {
        "max_capture_bytes": 1048576,
        "max_items": 5000,
        "max_provider_calls": 100,
        "max_wall_clock_microseconds": 60_000_000,
    }
    if max_read_rows is not None:
        budgets["max_read_rows"] = max_read_rows
    author(
        instance,
        LineInput.model_validate(
            {
                "kind": "line",
                "name": "test.reading-line",
                "procedure_name": "test.two-reads",
                "parameters": {},
                "budgets": budgets,
                "max_authority": "observe",
            }
        ),
        name="reading-line",
    )


@pytest.mark.parametrize(("max_read_rows", "status"), [(None, "succeeded"), (1, "refused")])
def test_a_lines_read_budget_lowers_the_procedures_cap(
    tmp_path: Path, max_read_rows: int | None, status: str
) -> None:
    from tests.test_procedures.test_b2_operations import Clock

    instance, _ = seed_claims(tmp_path)
    author_procedure(instance, _reading("test.two-reads", ("wi-42", "wi-43")))
    _line(instance, max_read_rows=max_read_rows)

    state = service_run_playbill_line(
        instance,
        path_identity_digest="test.reading-line",
        request=LineRunRequest(line="test.reading-line"),
        actor_context=actor().model_copy(update={"timestamp": NOW}),
        caller_rung=2,
        daemon_clock=Clock(NOW),
    )

    if status == "succeeded":
        assert state.status == "succeeded", _refusal(state)
    else:
        assert state.status != "succeeded"
        assert "read_ceiling_exceeded" in _refusal(state)


def test_a_line_states_a_read_budget_no_higher_than_its_procedures_cap(tmp_path: Path) -> None:
    instance, _ = seed_claims(tmp_path)
    author_procedure(
        instance, _reading("test.two-reads", ("wi-42", "wi-43"), caps={"read_rows": 5})
    )
    with pytest.raises(AssertionError, match="max_read_rows"):
        _line(instance, max_read_rows=6)


def test_every_live_mandate_lowers_the_lines_read_budget() -> None:
    """A Mandate's resource ceiling caps the read rows of the Line runs it grants."""

    from cruxible_client.contracts.procedures.models import ProcedureBudget, ProcedureHardCaps

    caps = ProcedureHardCaps.model_validate(
        {
            "max_wall_clock": {"microseconds": 60_000_000},
            "max_provider_calls": 1,
            "max_capture_bytes": 0,
            "max_items": 10,
            "max_repeat_attempts": 1,
            "max_read_rows": 500,
        }
    )
    budget = ProcedureBudget.model_validate(
        {"wall_clock": {"microseconds": 1}, "max_provider_calls": 1, "max_capture_bytes": 0}
    )
    procedure = SimpleNamespace(
        procedure=SimpleNamespace(definition=SimpleNamespace(budget=budget, hard_caps=caps))
    )
    budgets = {
        "max_capture_bytes": 0,
        "max_items": 10,
        "max_provider_calls": 1,
        "max_wall_clock_microseconds": 1,
        "max_read_rows": 400,
    }
    line = SimpleNamespace(line=SimpleNamespace(budgets=budgets))

    def mandate(rows: int) -> Any:
        ceiling = caps.model_copy(update={"max_read_rows": rows})
        return ("sha256:" + "0" * 64, SimpleNamespace(resource_ceiling=ceiling))

    granted = (mandate(300), mandate(450))
    assert _line_budget(line, procedure).max_read_rows == 400  # type: ignore[arg-type]
    assert _line_budget(line, procedure, granted).max_read_rows == 300  # type: ignore[arg-type]
    unstated = SimpleNamespace(line=SimpleNamespace(budgets=None))
    assert _line_budget(unstated, procedure).max_read_rows == 500  # type: ignore[arg-type]
