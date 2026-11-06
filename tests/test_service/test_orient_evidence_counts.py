"""W8: orient serves each ClaimType's live Claims by how they are backed.

``evidence_counts`` comes from per-predicate counters that Claim and citation
row changes maintain inside the publication transaction; a read seeks them
and never walks the Claims. The counters stay equal to a recount across
writes, revisions and retirements.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.write import WriteRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.orient import service_playbill_orient
from tests.core_support._write_support import KIND, caller, report_evidence, seed_write_surface
from tests.test_scale._counters import counting, trace_sqlite

STATUS = f"{KIND}.status"
MEASURED = f"{KIND}.measured"
LABELS = f"{KIND}.labels"


@pytest.fixture(autouse=True)
def traced_sqlite(tmp_path: Path):  # type: ignore[no-untyped-def]
    with trace_sqlite(tmp_path):
        yield


def _write(instance: PlaybillInstance, *changes: dict[str, Any]) -> Any:
    request = WriteRequest.model_validate({"because": "The report says so.", "changes": changes})
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return outcome


def _served(instance: PlaybillInstance) -> dict[str, tuple[int, int, int]]:
    result = service_playbill_orient(instance, section="claim_types", limit=100)
    return {
        row.predicate: (
            row.evidence_counts.admitted,
            row.evidence_counts.own_word,
            row.evidence_counts.unmet,
        )
        for row in result.claim_types or ()
        if row.evidence_counts is not None
    }


def _recount(instance: PlaybillInstance) -> dict[str, tuple[int, int]]:
    """Live Claims and those citing independent evidence, recounted from the rows."""

    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        connection = projection.typed.connection
        held = {
            str(predicate): (int(live), int(captured))
            for predicate, live, captured in connection.execute(
                "SELECT predicate, live, captured FROM claim_type_evidence_counts"
            )
        }
        counted = {
            str(predicate): (int(live), int(captured))
            for predicate, live, captured in connection.execute(
                "SELECT c.predicate, count(*), sum(EXISTS (SELECT 1 FROM citation_uses u "
                "WHERE u.owner_kind='Claim' AND u.owner_key=c.identity AND u.role='evidence' "
                "AND u.origin='independent')) FROM claims c WHERE c.lifecycle='live' "
                "GROUP BY c.predicate"
            )
        }
    assert held == counted
    return held


def test_counts_follow_writes_revisions_and_retirements(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    workspace = tmp_path / "workspace"
    _write(
        instance,
        {"op": "set", "subject": f"{KIND}/wi-1", "field": "status", "value": "ready"},
        {"op": "set", "subject": f"{KIND}/wi-2", "field": "status", "value": "blocked"},
        {
            "op": "set",
            "subject": f"{KIND}/wi-1",
            "field": "measured",
            "value": 3,
            "evidence": report_evidence(workspace, "three"),
        },
    )
    _recount(instance)
    served = _served(instance)
    assert served[STATUS] == (0, 2, 0)
    assert served[MEASURED] == (1, 0, 0)
    assert served[LABELS] == (0, 0, 0)
    detail = service_playbill_orient(instance, kind=KIND).kind_detail
    assert detail is not None
    by_kind = {row.predicate: row.evidence_counts for row in detail.predicates}
    assert by_kind[STATUS] is not None and by_kind[STATUS].own_word == 2
    # A revision keeps the Claim live and its backing moves with it.
    revised = _write(
        instance,
        {"op": "set", "subject": f"{KIND}/wi-1", "field": "status", "value": "done"},
        {
            "op": "set",
            "subject": f"{KIND}/wi-1",
            "field": "measured",
            "value": 4,
            "evidence": report_evidence(workspace, "four"),
        },
    )
    _recount(instance)
    assert _served(instance)[STATUS] == (0, 2, 0)
    assert _served(instance)[MEASURED] == (1, 0, 0)
    measured = next(
        change.claim for change in revised.changes if change.field in ("measured", MEASURED)
    )
    _write(instance, {"op": "retire", "target": measured, "reason": "superseded"})
    _recount(instance)
    assert _served(instance)[MEASURED] == (0, 0, 0)
    status = next(change.claim for change in revised.changes if change.field in ("status", STATUS))
    _write(instance, {"op": "retire", "target": status, "reason": "superseded"})
    _recount(instance)
    assert _served(instance)[STATUS] == (0, 1, 0)


def test_a_claim_without_captured_evidence_is_unmet_where_the_type_requires_one() -> None:
    import sqlite3

    from cruxible_client.contracts.orient import OrientPredicate
    from cruxible_core.service.discovery.orient import _with_evidence_counts

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE claim_type_evidence_counts (predicate, live, captured)")
    connection.execute("INSERT INTO claim_type_evidence_counts VALUES ('a.b', 5, 2)")
    rows = [
        OrientPredicate(name="b", predicate="a.b", cardinality="one", type="string", **extra)
        for extra in ({}, {"evidence_requirement": "captured"}, {"evidence_requirement": "none"})
    ]
    counts = [item.evidence_counts for item in _with_evidence_counts(connection, rows)]
    assert [(c.admitted, c.own_word, c.unmet) for c in counts if c is not None] == [
        (2, 3, 0),
        (2, 0, 3),
        (2, 3, 0),
    ]


def test_the_counts_read_is_flat_in_the_number_of_claims(tmp_path: Path) -> None:
    counts: dict[int, Any] = {}
    for size in (3, 30):
        root = tmp_path / str(size)
        root.mkdir()
        instance, _ = seed_write_surface(root)
        _write(
            instance,
            *(
                {"op": "set", "subject": f"{KIND}/n-{index}", "field": "status", "value": "ready"}
                for index in range(size)
            ),
        )
        assert _served(instance)[STATUS] == (0, size, 0)  # warm
        with counting(root) as window:
            assert _served(instance)[STATUS] == (0, size, 0)
        assert not window.unexplained, window.unexplained
        counts[size] = window.counts
    print("W8 orient claim_types work", {size: dict(c) for size, c in counts.items()})
    small, large = counts[3], counts[30]
    assert large["sql_statements"] == small["sql_statements"], (small, large)
    assert large["claim_parses"] == small["claim_parses"], (small, large)
    assert large["sql_vm_steps"] <= small["sql_vm_steps"] * 1.05 + 100, (small, large)
