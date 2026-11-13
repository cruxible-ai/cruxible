"""Refused records (D-R40): a bad record is refused alone, at extract, and nothing retires.

A record that is not an object, or whose key part is out of its key ClaimType's
bounds, goes through extract's refused-record path with a typed reason
(``not_an_object``, ``key_part_out_of_bounds``). Its siblings still write; the
read is incomplete, so absence judges nothing and nothing retires; the run
reports the refusal. It is never a whole-run failure and never a refusal at
publication.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_e2e.test_skeleton_capture_lists import install
from tests.test_e2e.test_skeleton_sbom_late_read import (
    APP,
    LINE,
    Sbom,
    activate,
    memberships,
    run,
    sbom_spec,
    sbom_world,
    write_sbom,
)

A, B, C = "pkg:pypi/a@1", "pkg:pypi/b@1", "pkg:pypi/c@1"
#: Over the bounded purl key's 64 characters.
LONG = "pkg:pypi/" + "x" * 80 + "@1"
BOUNDED = "string{max_len: 64}"


def _changes(state: Any) -> list[str]:
    (egress,) = state.terminal_egress
    return sorted(child.change_kind for child in egress.children if child.change_kind)


def _refusals(state: Any) -> list[tuple[tuple[str, ...], str | None]]:
    """The run's record refusals at the extract: (record locators, typed reason)."""

    return [
        (item.record_locators, item.problem)
        for item in state.findings
        if item.kind == "extract_refused" and item.target == "listed"
    ]


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("not a record", "not_an_object"),
        (["a", "list"], "not_an_object"),
        (LONG, "key_part_out_of_bounds"),
        # An unparseable key (kit G1): refused alone, never a whole-run failure.
        ({"purl": {"not": "a purl"}}, "key_unparseable"),
    ],
)
def test_a_bad_record_is_refused_alone_its_siblings_write_and_nothing_retires(
    tmp_path: Path, bad: object, reason: str | None
) -> None:
    instance, root = sbom_world(tmp_path, purl_type=BOUNDED)
    install(instance, sbom_spec(), line=LINE)
    source = Sbom()
    write_sbom(root, APP, [A, B, C])
    activate(instance, run(instance, root, source))
    assert memberships(instance) == {APP: {A, B, C}}

    # B leaves the SBOM, and one member is bad: the read is incomplete.
    write_sbom(root, APP, [A, bad, C, "pkg:pypi/d@1"])
    state = run(instance, root, source, minute=1)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    # D is new and writes; A and C are re-observed; B does NOT retire.
    assert _changes(state) == ["claim", "reobservation", "reobservation", "subject"]
    assert state.completeness is not None and not state.completeness.complete
    assert {item.kind for item in state.completeness.reasons} >= {"invalid_record"}
    diagnostics = [d for egress in state.terminal_egress for d in egress.claims_diagnostics]
    assert any(d.kind == "incomplete_source" for d in diagnostics), diagnostics
    # The refusal is on the run, typed.
    (refused,) = _refusals(state)
    assert refused == (("/components/1",), reason)
    activate(instance, state)
    assert memberships(instance) == {APP: {A, B, C, "pkg:pypi/d@1"}}


# -- work counts: a refused record costs its one check, no state work ----------------


def _counted(tmp_path: Path, size: int, *, bad: int) -> int:
    from tests.test_scale._counters import counting, trace_sqlite

    good = [f"pkg:pypi/c{index}@1" for index in range(size)]
    with trace_sqlite(tmp_path):
        instance, root = sbom_world(tmp_path, purl_type=BOUNDED)
        install(instance, sbom_spec(), line=LINE)
        source = Sbom()
        write_sbom(root, APP, good)
        activate(instance, run(instance, root, source))
        members: list[object] = list(good)
        for index in range(bad):
            members.insert(index * 3, ["not", "a", "record", index])
        write_sbom(root, APP, members)
        with instance.bind_accepted_projection(instance.accepted_coordinate()):
            pass
        with counting(tmp_path) as window:
            state = run(instance, root, source, minute=1)
    assert state.status == "succeeded", state.terminal
    assert not window.unexplained, window.unexplained
    return int(window.counts["sql_statements"])


def test_refused_records_among_n_siblings_cost_no_state_work(tmp_path: Path) -> None:
    """With N siblings re-observed, refusing N/5 bad records adds no SQL at any N."""

    measured: dict[tuple[int, int], int] = {}
    for size in (10, 40):
        for bad in (0, size // 5):
            path = tmp_path / f"n{size}-b{bad}"
            path.mkdir()
            measured[(size, bad)] = _counted(path, size, bad=bad)
    added_small = measured[(10, 2)] - measured[(10, 0)]
    added_large = measured[(40, 8)] - measured[(40, 0)]
    assert added_small == added_large, measured
    report = __import__("os").environ.get("REFUSED_RECORD_COUNTS")
    if report:
        with open(report, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({str(key): value for key, value in measured.items()}) + "\n")
