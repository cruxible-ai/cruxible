"""A Procedure read runs to the end: one raised ceiling, and a loud refusal past it.

The interactive budgets of a Query do not bound a ``read`` node. For that one
call the result, path, path-per-result and include limits are lifted to the
run's read ceiling (its remaining ``max_read_rows``); the declared traversal
depth is kept. Anything the ceiling still clips refuses
``read_ceiling_exceeded``, and the ceiling is cumulative across the run's
reads. Interactive callers keep the Query's own budgets.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.procedures.models import (
    ProcedureDefinition,
    ProjectNode,
    ReadNode,
)
from cruxible_client.contracts.procedures.reads import ProcedureReadCeiling
from cruxible_client.contracts.query.grammar import QueryBudgets
from cruxible_core.procedures.execution import (
    ProcedureBoundaryRefused,
    ReadCeilingExceeded,
    StateTapReadResultV1,
    bind_accepted_state_materials,
)
from cruxible_core.query.engine import evaluate_claim_query
from tests.support.procedures import named
from tests.test_claims.test_claim_query_engine import (
    NOW,
    all_items_query,
    facts,
    reviewer_claim,
    run,
    status_claim,
)
from tests.test_query.test_query_definitions import accepted_query, active_work_query


def _items(count: int) -> Any:
    from cruxible_core.query.backends import ClaimQueryFactsV1
    from tests.test_claims.test_claim_query_engine import coordinate, subject

    names = [f"wi-{index:05d}" for index in range(1, count + 1)]
    return ClaimQueryFactsV1(
        coordinate=coordinate(),
        subjects=tuple(
            sorted(
                (subject("project.work_item", name) for name in names),
                key=lambda item: item.path.encode("utf-8"),
            )
        ),
        claims=tuple(
            sorted(
                (status_claim(index, name, "ready") for index, name in enumerate(names, 1)),
                key=lambda item: item.accepted.path.encode("utf-8"),
            )
        ),
    )


def _read(query: Any, fact_rows: Any, ceiling: int, **kwargs: Any) -> Any:
    return evaluate_claim_query(
        accepted_query(query),
        facts=fact_rows,
        coordinate=fact_rows.coordinate,
        evaluation_time=NOW,
        procedure_read=ProcedureReadCeiling(rows=ceiling),
        **kwargs,
    )


def test_a_flat_query_returns_every_row_to_a_read_past_its_interactive_maximum() -> None:
    """(a) 1,500 rows under a 1,000-result maximum: a read gets all of them."""

    query = all_items_query(
        maximum_budgets=QueryBudgets(max_results=1000, max_traversal_depth=0),
        default_budgets=QueryBudgets(max_results=1000, max_traversal_depth=0),
    )
    fact_rows = _items(1500)

    result = _read(query, fact_rows, 10_000)

    assert result.verdict == "completed"
    assert len(result.rows) == 1500
    assert result.truncation.clipped_budgets == ()


def test_an_interactive_query_on_the_same_definition_still_clips() -> None:
    """(f) Interactive callers keep today's budgets."""

    query = all_items_query(
        maximum_budgets=QueryBudgets(max_results=1000, max_traversal_depth=0),
        default_budgets=QueryBudgets(max_results=1000, max_traversal_depth=0),
    )
    result = run(query, _items(1500))

    assert len(result.rows) == 1000
    assert result.truncation.clipped_budgets == ("max_results",)


def _paths() -> Any:
    return facts(
        (
            status_claim(1, "wi-1", "ready"),
            reviewer_claim(3, "wi-1", "ada"),
            reviewer_claim(4, "wi-1", "grace"),
        )
    )


def test_a_path_query_returns_every_path_under_the_ceiling_and_clips_over_it() -> None:
    """(b) A traversal whose max_paths would clip interactively."""

    query = active_work_query(includes=())
    interactive = run(
        query,
        _paths(),
        parameters={"status": "ready"},
        budgets=QueryBudgets(
            max_results=50, max_traversal_depth=2, max_paths=1, max_paths_per_result=1
        ),
    )
    under = _read(query, _paths(), 100, parameters={"status": "ready"})
    over = _read(query, _paths(), 1, parameters={"status": "ready"})

    assert interactive.truncation.clipped_budgets == ("max_paths",)
    assert under.truncation.clipped_budgets == () and len(under.rows) == 2
    assert over.truncation.clipped_budgets == ("max_paths",)


def test_paths_per_result_are_lifted_to_the_ceiling() -> None:
    """(d) A path result whose max_paths_per_result would clip interactively."""

    query = active_work_query(result_binding="item", result_shape="path", includes=())
    interactive = run(
        query,
        _paths(),
        parameters={"status": "ready"},
        budgets=QueryBudgets(
            max_results=50, max_traversal_depth=2, max_paths=200, max_paths_per_result=1
        ),
    )
    under = _read(query, _paths(), 100, parameters={"status": "ready"})
    over = _read(query, _paths(), 1, parameters={"status": "ready"})

    assert interactive.truncation.clipped_budgets == ("max_paths_per_result",)
    assert under.truncation.clipped_budgets == ()
    assert under.truncation.retained_path_count == 2
    assert over.truncation.clipped_budgets  # past the ceiling the read is clipped: it refuses


def test_an_include_item_cap_is_lifted_to_the_ceiling() -> None:
    """(c) The declared include cap (1) no longer clips; a ceiling below the items does."""

    fact_rows = facts(
        (
            status_claim(1, "wi-1", "ready"),
            status_claim(2, "wi-1", "ready"),
            reviewer_claim(3, "wi-1", "ada"),
        )
    )
    interactive = run(active_work_query(), fact_rows, parameters={"status": "ready"})
    lifted = _read(active_work_query(), fact_rows, 100, parameters={"status": "ready"})
    over = _read(active_work_query(), fact_rows, 1, parameters={"status": "ready"})

    assert interactive.truncation.clipped_budgets == ("include_max_items",)
    assert lifted.truncation.clipped_budgets == ()
    assert len(lifted.rows[0].includes[0].items) == 2
    assert "include_max_items" in over.truncation.clipped_budgets


# -- the run's reads share one ceiling, and a clip refuses ---------------------------


class _CeilingReader:
    """A state reader that serves ``rows`` rows per read and refuses past its ceiling."""

    def __init__(self, rows: int) -> None:
        self.rows = rows
        self.ceilings: list[int] = []

    def read_accepted_state(self, *, query, parameters, coordinate, ceiling):  # type: ignore[no-untyped-def]
        from cruxible_core.service.procedures.procedures import state_tap_value

        self.ceilings.append(ceiling)
        if self.rows > ceiling:
            raise ReadCeilingExceeded(
                clipped=("max_results",), candidates=self.rows, ceiling=ceiling
            )
        query_definition = all_items_query(
            maximum_budgets=QueryBudgets(max_results=1000, max_traversal_depth=0),
        )
        result = _read(query_definition, _items(self.rows), ceiling)
        return StateTapReadResultV1(
            value=state_tap_value(result),
            effective_budgets=result.budgets,
            rows=len(result.rows),
        )


def _two_reads(max_read_rows: int) -> Any:
    from tests.test_procedures.test_procedure_execution import (
        _budget,
        _hard_caps,
        _owned_accepted,
        _owned_contract,
        _owned_pin,
    )

    empty = _owned_contract("empty", {})
    rows = _owned_contract("rows", {})
    entry, out = _owned_pin("contract-in", empty), _owned_pin("contract-out", rows)
    from cruxible_client.contracts.artifacts import ArtifactPin

    query = ArtifactPin(
        role="query",
        target=ArtifactIdentity(kind="QueryDefinition", name="items"),
        artifact_digest="sha256:" + "1" * 64,
    )
    definition = ProcedureDefinition(
        name="two-reads",
        contract_in=named(entry),
        contract_out=named(out),
        nodes=(
            ReadNode(node_id="first", query=named(query), as_="first"),
            ReadNode(node_id="second", query=named(query), as_="second"),
            ProjectNode(node_id="result", literal={}, contract_out=named(out), as_="result"),
        ),
        returns="result",
        budget=_budget(),
        hard_caps=_hard_caps().model_copy(update={"max_read_rows": max_read_rows}),
        terminal_capability=1,
    )
    return _owned_accepted(definition, contracts=(empty, rows), pins=(entry, out, query))


def test_two_reads_share_the_runs_read_ceiling_and_the_second_refuses() -> None:
    """(e) Rows a read returns are subtracted from the ceiling the next read gets."""

    from tests.test_procedures.test_procedure_execution import _coordinate

    reader = _CeilingReader(rows=60)
    with pytest.raises(ProcedureBoundaryRefused) as caught:
        bind_accepted_state_materials(
            _two_reads(100),
            accepted_coordinate=_coordinate(),
            state_reader=reader,  # type: ignore[arg-type]
            bodies=_fixture_bodies(),
        )

    assert reader.ceilings == [100, 40]
    assert caught.value.code == "read_ceiling_exceeded"
    assert caught.value.details == {
        "node": "second",
        "clipped": ["max_results"],
        "candidates": 60,
        "ceiling": 40,
    }


def test_reads_within_the_ceiling_all_bind() -> None:
    from tests.test_procedures.test_procedure_execution import _coordinate

    reader = _CeilingReader(rows=50)
    materials = bind_accepted_state_materials(
        _two_reads(100),
        accepted_coordinate=_coordinate(),
        state_reader=reader,  # type: ignore[arg-type]
        bodies=_fixture_bodies(),
    )

    assert reader.ceilings == [100, 50]
    assert [item.input.input_name for item in materials] == ["first", "second"]


def _fixture_bodies() -> Any:
    import tempfile
    from pathlib import Path

    from cruxible_core.storage.cas import ContentAddressedBodyStore

    root = Path(tempfile.mkdtemp()) / "cas"
    root.mkdir(mode=0o700)
    return ContentAddressedBodyStore(root)
