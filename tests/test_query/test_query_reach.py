"""Query pushdown (C1): hops expand through link indexes, conjunctions seek their supported
part, Subject-valued equality seeks the object index -- all against the full-load oracle.

The served evaluation must commit to exactly the full load's result: rows, order,
refusals, truncation and (for hops, which never narrow entries) the verdict
advisory. An entry seek eliminates Subjects that cannot match whatever their
Claims' verdicts, so its advisory counts hidden Claims among the candidates only.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.query.authored import QuerySpecInput, lower_query_spec
from cruxible_core.service.discovery import query as served
from cruxible_core.service.discovery.authored_query import (
    AuthoredQueryResolver,
    candidate_query_vocabulary,
)
from cruxible_core.service.discovery.compact_query import _accepted
from cruxible_core.service.discovery.query_pushdown import select_query_facts
from tests.core_support._query_reach_world import (
    AFFECTED_BY,
    COMPONENT,
    FINDING,
    FOUND_IN,
    FUTURE,
    PAST,
    SEVERITY,
    STATUS,
    UNIT,
    VULN,
    WHEN,
    ReachWorld,
)
from tests.test_scale._counters import counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK

OPEN = '("blocked", "deferred")'


def definition(world: ReachWorld, spec: dict[str, Any]) -> Any:
    tree = world.instance.tree_at(world.instance.accepted_coordinate().git_oid)
    return _accepted(
        lower_query_spec(
            QuerySpecInput.model_validate(spec),
            AuthoredQueryResolver(candidate_query_vocabulary(tree)),
        )
    )


def plan_spec(visible: str = "current") -> dict[str, Any]:
    """The kit's coverage_plan shape: a seekable key equality beside an absence."""

    return {
        "query": "qr.plan",
        "params": {"repo": "string"},
        "from": {"u": UNIT},
        "where": f"u.repo == $repo and (not exists u.status or u.status in {OPEN})",
        "fields": ["u.subject_id", "u.surface", "u.status"],
        "visible": visible,
    }


def by_unit_spec(visible: str = "current", where: str | None = None) -> dict[str, Any]:
    """The kit's findings_by_unit shape: a reverse hop from the bound unit."""

    spec: dict[str, Any] = {
        "query": "qr.by_unit",
        "params": {"unit": "subject"},
        "from": {"u": f"{UNIT} = $unit"},
        "hops": [{"f": f"<u.{FOUND_IN}!: {FINDING}"}],
        "fields": [{"finding": "f.subject_id"}, "f.severity"],
        "visible": visible,
    }
    if where is not None:
        spec["where"] = where
    return spec


def found_in_spec(visible: str = "current") -> dict[str, Any]:
    """Subject-valued equality: the findings whose ``found_in`` names ``$unit``."""

    return {
        "query": "qr.found_in",
        "params": {"unit": "subject"},
        "from": {"f": FINDING},
        "where": "f.found_in == $unit",
        "fields": ["f.subject_id", "f.severity"],
        "visible": visible,
    }


def units_of_spec() -> dict[str, Any]:
    """A forward hop from one bound finding to its units, reading the target's fields."""

    return {
        "query": "qr.units_of",
        "params": {"finding": "subject"},
        "from": {"f": f"{FINDING} = $finding"},
        "hops": [{"u": f"f.{FOUND_IN}!: {UNIT}"}],
        "fields": [{"unit": "u.subject_id"}, "u.status", "f.severity"],
    }


def affected_spec() -> dict[str, Any]:
    """A forward hop from every component (no entry narrowing): the reach is the kind."""

    return {
        "query": "qr.affected",
        "from": {"c": COMPONENT},
        "hops": [{"v": f"c.{AFFECTED_BY}: {VULN}"}],
        "fields": [{"component": "c.subject_id"}, {"vuln": "v.subject_id"}],
        "visible": "held",
    }


def include_spec() -> dict[str, Any]:
    """A seek with an include: the include's reach loads for the candidates only."""

    return {
        "query": "qr.with_units",
        "params": {"severity": "string"},
        "from": {"f": FINDING},
        "where": "f.severity == $severity",
        "include": {"units": "f.found_in"},
        "fields": ["f.subject_id"],
    }


def run(world: ReachWorld, query: Any, parameters: Any, *, facts: Any = None) -> Any:
    instance = world.instance
    return served.evaluate_accepted_query(
        instance,
        query,
        coordinate=instance.accepted_coordinate(),
        evaluation_time=WHEN,
        parameters=parameters,
        facts=facts,
    )


def dumped(result: Any, *, advisory: bool = True) -> bytes:
    payload = result.model_dump(mode="json")
    if not advisory:
        payload.pop("verdict_visibility")
    return canonical_bytes(payload)


def compare(world: ReachWorld, query: Any, parameters: Any, *, whole: Any) -> Any:
    """Served against the full load; the advisory too unless the entry was narrowed."""

    actual = run(world, query, parameters)
    expected = run(world, query, parameters, facts=lambda: whole)
    narrowed_entry = not query.query.traversal and query.query.where is not None
    assert dumped(actual, advisory=not narrowed_entry) == dumped(
        expected, advisory=not narrowed_entry
    )
    if narrowed_entry and actual.verdict_visibility is not None:
        assert expected.verdict_visibility is not None
        assert (
            actual.verdict_visibility.excluded_claim_count
            <= expected.verdict_visibility.excluded_claim_count
        )
    return actual


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> ReachWorld:
    built = ReachWorld(tmp_path_factory.mktemp("reach"))
    hub = built.subject(UNIT, "acme", "hub")
    side = built.subject(UNIT, "acme", "side")
    built.claim(STATUS, "checked", hub)
    built.claim(STATUS, "checked", side)
    for index in range(6):
        unit = built.subject(UNIT, "acme", f"a{index}")
        if index == 1:
            built.claim(STATUS, "deferred", unit)
        elif index == 2:
            # Stale and future statuses are hidden from a current read.
            built.claim(STATUS, "deferred", unit, effective_until=PAST)
        elif index == 3:
            built.claim(STATUS, "blocked", unit, effective_from=FUTURE)
        elif index == 4:
            built.claim(STATUS, "checked", unit)
        other = built.subject(UNIT, "other", f"o{index}")
        if index % 2:
            built.claim(STATUS, "deferred", other, effective_until=PAST)
    for index in range(5):
        finding = built.subject(FINDING, f"f{index}")
        built.claim(FOUND_IN, hub, finding)
        if index == 4:
            built.claim(FOUND_IN, side, finding)
        built.claim(
            SEVERITY,
            "high" if index % 2 else "low",
            finding,
            effective_until=PAST if index == 3 else None,
        )
        stray = built.subject(FINDING, f"g{index}")
        built.claim(FOUND_IN, side, stray, effective_until=PAST if index == 2 else None)
        built.claim(SEVERITY, "high", stray)
    vulns = [built.subject(VULN, f"v{index}") for index in range(3)]
    for index in range(4):
        component = built.subject(COMPONENT, f"c{index}")
        for vuln in vulns[: index % 3 + 1]:
            built.claim(AFFECTED_BY, vuln, component, effective_until=PAST if index == 2 else None)
    built.accept()
    return built


def _reference(world: ReachWorld, kind: str, identifier: str) -> str:
    return f"{kind}/{identifier}"


def test_randomized_reads_match_the_full_load(world: ReachWorld) -> None:
    whole = served.build_accepted_query_facts(
        world.instance, coordinate=world.instance.accepted_coordinate()
    )
    units = ["acme--hub", "acme--side", "acme--a1", "other--o1", "acme--absent"]
    findings = ["f0", "f3", "f4", "g2", "absent"]
    queries = [
        (
            definition(world, plan_spec(visible)),
            lambda r: {"repo": r.choice(["acme", "other", "x"])},
        )
        for visible in ("current", "held")
    ]
    for visible in ("current", "held"):
        queries.append(
            (
                definition(world, by_unit_spec(visible)),
                lambda r: {"unit": _reference(world, UNIT, r.choice(units))},
            )
        )
        queries.append(
            (
                definition(world, found_in_spec(visible)),
                lambda r: {"unit": _reference(world, UNIT, r.choice(units))},
            )
        )
    queries += [
        (
            definition(world, by_unit_spec(where='f.severity == "high"')),
            lambda r: {"unit": _reference(world, UNIT, r.choice(units))},
        ),
        (
            definition(world, units_of_spec()),
            lambda r: {"finding": _reference(world, FINDING, r.choice(findings))},
        ),
        (definition(world, affected_spec()), lambda r: None),
        (
            definition(world, include_spec()),
            lambda r: {"severity": r.choice(["high", "low", "none"])},
        ),
    ]
    rng = random.Random(1106)
    seen_rows = 0
    for _ in range(3):
        for query, parameters in queries:
            seen_rows += len(compare(world, query, parameters(rng), whole=whole).rows)
    assert seen_rows > 0


def test_a_served_read_takes_its_reach_and_seeks(
    world: ReachWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[dict[str, Any]] = []
    build = served.build_accepted_query_facts

    def spy(*args: Any, **kwargs: Any) -> Any:
        observed.append(kwargs)
        return build(*args, **kwargs)

    monkeypatch.setattr(served, "build_accepted_query_facts", spy)
    hub = _reference(world, UNIT, "acme--hub")
    plan = run(world, definition(world, plan_spec()), {"repo": "acme"})
    # A hidden status (stale, not yet effective) reads as absent: those units are open.
    assert sorted(row.fields[1].value for row in plan.rows) == [
        "acme--a0",
        "acme--a1",
        "acme--a2",
        "acme--a3",
        "acme--a5",
    ]
    assert observed[-1]["entry_subject_paths"] is not None
    assert all(
        path.split("/")[2].startswith("acme--") for path in observed[-1]["entry_subject_paths"]
    )
    found = run(world, definition(world, by_unit_spec()), {"unit": hub})
    assert sorted(row.fields[0].value for row in found.rows) == ["f0", "f1", "f2", "f3", "f4"]
    reached = observed[-1]["claim_paths"]
    assert reached is not None and len(reached) == 5 + 5  # found_in into hub, each severity
    equal = run(world, definition(world, found_in_spec()), {"unit": hub})
    assert [row.result_subject_identity for row in equal.rows] == [
        f"Subject:{FINDING}/f{index}" for index in range(5)
    ]
    assert len(observed[-1]["entry_subject_paths"]) == 5


def test_an_unsafe_residual_keeps_the_full_load(world: ReachWorld) -> None:
    instance = world.instance
    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    plan = definition(world, plan_spec()).query
    found = definition(world, found_in_spec()).query

    class Refusing:
        def refuses(self, _filter: Any) -> bool:
            return True

        def values(self, *_args: Any) -> Any:
            from cruxible_client.contracts.query.results import QueryRefusal
            from cruxible_core.query.engine import _RefusalSignal

            raise _RefusalSignal(
                QueryRefusal(code="cruxible.query.value_type_mismatch", message="probe")
            )

    with instance.bind_accepted_projection(coordinate) as projection:

        def select(query: Any, parameters: Any, **kwargs: Any) -> Any:
            return select_query_facts(
                projection.typed.connection,
                query,
                parameters=parameters,
                budgets=None,
                source=tree,
                **kwargs,
            )

        # Without the evaluator's operand coercion a conjunction keeps every part.
        assert select(plan, {"repo": "acme"}) is None
        # An operand the evaluator would refuse is never pruned around.
        assert select(plan, {"repo": "acme"}, operands=Refusing()) is None
        assert select(found, {"unit": f"{UNIT}/acme--hub"}, operands=Refusing()) is None
        # A live-only read loses retired targets: a Subject-valued read could refuse.
        assert (
            select(
                found, {"unit": f"{UNIT}/acme--hub"}, live_subjects_only=True, operands=Refusing()
            )
            is None
        )


# -- work counts: fixed output, growing unrelated Subjects -------------------------------

OUTPUT = 10
SIZES = (100, 1000)
COUNTED = ("sql_statements", "sql_vm_steps", "claim_parses")


def _audit_world(root: Path, unrelated: int) -> tuple[ReachWorld, str]:
    """``OUTPUT`` open units in ``acme`` and findings in its hub, beside ``unrelated``
    other-repository units (with and without status) and findings in another unit."""

    built = ReachWorld(root)
    hub = built.subject(UNIT, "acme", "hub")
    built.claim(STATUS, "checked", hub)
    for index in range(OUTPUT):
        unit = built.subject(UNIT, "acme", f"a{index}")
        if index % 2:
            built.claim(STATUS, "deferred", unit)
        finding = built.subject(FINDING, f"f{index}")
        built.claim(FOUND_IN, hub, finding)
        built.claim(SEVERITY, "high", finding)
    side = built.subject(UNIT, "other", "side")
    for index in range(unrelated):
        unit = built.subject(UNIT, "other", f"o{index}")
        if index % 2:
            built.claim(STATUS, "deferred", unit)
        finding = built.subject(FINDING, f"g{index}")
        built.claim(FOUND_IN, side, finding)
        built.claim(SEVERITY, "high", finding)
    built.accept()
    return built, f"{UNIT}/acme--hub"


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> Iterator[list[dict[str, Any]]]:
    root = tmp_path_factory.mktemp("reach-counts")
    out: list[dict[str, Any]] = []
    with trace_sqlite(root):
        for size in SIZES:
            built, hub = _audit_world(root / f"u{size}", size)
            reads = {
                "conjunction_not_exists": (definition(built, plan_spec()), {"repo": "acme"}),
                "reverse_hop_bound_unit": (definition(built, by_unit_spec()), {"unit": hub}),
                "subject_valued_equality": (definition(built, found_in_spec()), {"unit": hub}),
            }
            counts: dict[str, Any] = {}
            for name, (query, parameters) in reads.items():
                run(built, query, parameters)  # warm: a steady-state read
                with counting(root) as window:
                    result = run(built, query, parameters)
                assert not window.unexplained, window.unexplained
                counts[name] = {
                    "rows": len(result.rows),
                    **{counter: window.counts[counter] for counter in COUNTED},
                }
            out.append(counts)
    yield out


@pytest.mark.parametrize(
    "read", ["conjunction_not_exists", "reverse_hop_bound_unit", "subject_valued_equality"]
)
def test_reads_are_flat_in_unrelated_subjects(measured: list[dict[str, Any]], read: str) -> None:
    small, large = (sized[read] for sized in measured)
    assert small["rows"] == large["rows"] == OUTPUT, (small, large)
    for counter in COUNTED:
        assert small[counter] > 0
        assert large[counter] <= small[counter] * RATIO + SLACK[counter], (read, counter, measured)


def _all_of(*filters: Any) -> Any:
    from cruxible_client.contracts.query.grammar import QueryConjunctionFilter

    return QueryConjunctionFilter(
        filters=tuple(
            sorted(filters, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
        )
    )


def test_a_row_dependent_operand_refusal_survives_a_seek(world: ReachWorld) -> None:
    """A Subject field compared as an integer refuses on every row the full load evaluates;
    a kind seek that selects nothing must not turn that refusal into an empty answer."""

    from cruxible_client.contracts.query.grammar import (
        QueryComparisonFilter,
        QueryLiteralRef,
        QuerySubjectFieldRef,
    )

    whole = served.build_accepted_query_facts(
        world.instance, coordinate=world.instance.accepted_coordinate()
    )
    base = definition(world, plan_spec())
    for operand in (
        QueryComparisonFilter(
            left=QuerySubjectFieldRef(binding="u", field="subject_id"),
            operator="eq",
            right=QueryLiteralRef(value=1),
            value_type="integer",
        ),
        QueryComparisonFilter(
            left=QuerySubjectFieldRef(binding="u", field="subject_id"),
            operator="eq",
            right=QueryLiteralRef(value=True),
            value_type="boolean",
        ),
    ):
        # Canonical order evaluates the subject_id part first, so the full load
        # refuses on the first unit before the kind part could short-circuit.
        where = _all_of(
            operand,
            QueryComparisonFilter(
                left=QuerySubjectFieldRef(binding="u", field="subject_kind"),
                operator="eq",
                right=QueryLiteralRef(value="qr.other"),
                value_type="string",
            ),
        )
        query = _accepted(base.query.model_copy(update={"where": where, "parameters": ()}))
        result = compare(world, query, None, whole=whole)
        assert result.verdict == "refused", result
        assert result.refusal is not None
        assert result.refusal.code == "cruxible.query.value_type_mismatch"
