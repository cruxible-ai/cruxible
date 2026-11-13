"""Pair hops (S4): ``KIND{part: b, ...}`` and ``KIND{part: b} with FACT``.

Candidates come from the Subject key index (whole key) or the pair index (one end
with a fact); each candidate's eligibility is decided from its own visible Claims
by the evaluator, so the served read (indexes, reached Claims only) and the
reference read (every Claim, ``DirectClaimFactIndex``) answer byte for byte,
verdict advisory included. A hidden, retired or none-only fact never binds a
pair, though its pair index row may exist.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.query.authored import (
    QuerySpecError,
    QuerySpecInput,
    lower_query_spec,
    query_spec_from_definition,
)
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import QueryPairPart, QueryPairStep
from cruxible_core.service.discovery import query as served
from cruxible_core.service.discovery.authored_query import (
    AuthoredQueryResolver,
    candidate_query_vocabulary,
)
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._pair_real import accept_raw
from tests.core_support._query_reach_world import (
    AFFECTED_BY,
    COMPONENT,
    E_COMPONENT,
    EXPOSURE,
    FUTURE,
    INVENTORY,
    JUSTIFICATION,
    O_COMPONENT,
    O_INVENTORY,
    OCCURRENCE,
    PAST,
    SCOPE,
    VULN,
    WHEN,
    ReachWorld,
)
from tests.test_query.test_query_reach import definition, run
from tests.test_scale._counters import counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK


def sbom_spec(visible: str = "current", required: bool = True) -> dict[str, Any]:
    """The SBOM read: the occurrences of one inventory with a live scope."""

    bang = "!" if required else ""
    return {
        "query": "qr.sbom",
        "params": {"inventory": "subject"},
        "from": {"i": f"{INVENTORY} = $inventory"},
        "hops": [
            {"o": f"{OCCURRENCE}{{inventory: i}}{bang} with scope"},
            {"c": f"o.{O_COMPONENT}!: {COMPONENT}"},
        ],
        "fields": [{"occurrence": "o.subject_id"}, "o.scope", {"component": "c.subject_id"}],
        "visible": visible,
    }


def worklist_spec(visible: str = "current") -> dict[str, Any]:
    """The kit's urgent/backlog shape: link, partial pair, key link, whole-key pair."""

    return {
        "query": "qr.worklist",
        "params": {"vuln": "subject"},
        "from": {"v": f"{VULN} = $vuln"},
        "hops": [
            {"c": f"<v.{AFFECTED_BY}!: {COMPONENT}"},
            {"o": f"{OCCURRENCE}{{component: c}}! with scope"},
            {"i": f"o.{O_INVENTORY}!: {INVENTORY}"},
            {"e": f"{EXPOSURE}{{component: c, vulnerability: v}}"},
        ],
        "where": "not exists e.justification",
        "fields": [
            {"component": "c.subject_id"},
            {"inventory": "i.subject_id"},
            {"exposure": "e.subject_id"},
        ],
        "visible": visible,
    }


def judged_spec() -> dict[str, Any]:
    """A required whole-key hop from every component's vulnerabilities."""

    return {
        "query": "qr.judged",
        "from": {"c": COMPONENT},
        "hops": [
            {"v": f"c.{AFFECTED_BY}: {VULN}"},
            {"e": f"{EXPOSURE}{{vulnerability: v, component: c}}! where exists e.justification"},
        ],
        "fields": [{"exposure": "e.subject_id"}, "e.justification"],
        "visible": "held",
    }


def dumped(result: Any) -> bytes:
    return canonical_bytes(result.model_dump(mode="json"))


def compare(world: ReachWorld, query: Any, parameters: Any, *, whole: Any) -> Any:
    actual = run(world, query, parameters)
    expected = run(world, query, parameters, facts=lambda: whole)
    assert dumped(actual) == dumped(expected)
    return actual


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> ReachWorld:
    built = ReachWorld(tmp_path_factory.mktemp("pairs"))
    inventories = [built.subject(INVENTORY, f"i{index}") for index in range(2)]
    vulns = [built.subject(VULN, f"v{index}") for index in range(2)]
    retire: list[str] = []
    for index in range(7):
        component = built.subject(COMPONENT, f"c{index}")
        for vuln in vulns[: 1 + index % 2]:
            built.claim(AFFECTED_BY, vuln, component)
        first = built.subject(OCCURRENCE, inventories[0], component)
        if index == 0:
            built.claim(SCOPE, "required", first)
        elif index == 1:
            built.claim(SCOPE, "required", first, effective_until=PAST)  # stale
        elif index == 2:
            built.claim(SCOPE, "optional", first, effective_from=FUTURE)  # uncovered
        elif index == 3:
            retire.append(built.claim(SCOPE, "excluded", first))  # a historical shell
        elif index == 4:
            # One visible and one hidden scope: the pair binds through the visible one.
            built.claim(SCOPE, "required", first)
            built.claim(SCOPE, "optional", first, effective_until=PAST)
        elif index == 5:
            # Only an inverse: the slot holds nothing, so the pair never binds.
            built.claim(SCOPE, None, first)
        second = built.subject(OCCURRENCE, inventories[1], component)
        built.claim(SCOPE, "required", second)
        if index % 3 == 0:
            exposure = built.subject(EXPOSURE, component, vulns[0])
            if index:
                built.claim(
                    JUSTIFICATION,
                    "vulnerable_code_not_present",
                    exposure,
                    effective_until=PAST if index == 6 else None,
                )
    built.accept()
    for claim in retire:
        built.retire(claim)
    built.accept()
    return built


def test_randomized_pair_reads_match_the_reference(world: ReachWorld) -> None:
    whole = served.build_accepted_query_facts(
        world.instance, coordinate=world.instance.accepted_coordinate()
    )
    queries = [
        (definition(world, sbom_spec(visible, required)), "inventory", INVENTORY, "i")
        for visible in ("current", "held")
        for required in (True, False)
    ]
    queries += [
        (definition(world, worklist_spec(visible)), "vuln", VULN, "v")
        for visible in ("current", "held")
    ]
    rng = random.Random(1107)
    rows = 0
    for _ in range(3):
        for query, name, kind, prefix in queries:
            parameters = {name: f"{kind}/{prefix}{rng.choice(['0', '1', 'x'])}"}
            rows += len(compare(world, query, parameters, whole=whole).rows)
    rows += len(compare(world, definition(world, judged_spec()), None, whole=whole).rows)
    assert rows > 0


def test_a_hidden_retired_or_missing_fact_never_binds_its_pair(world: ReachWorld) -> None:
    result = run(world, definition(world, sbom_spec()), {"inventory": f"{INVENTORY}/i0"})
    assert [(row.fields[1].value, row.fields[2].value) for row in result.rows] == [
        ("i0--c0", "required"),
        ("i0--c4", "required"),
    ]
    # c1 (stale), c2 (not yet effective) and c5 (a none only) still have their
    # pair index rows:
    # the index proposed them, and their own Claims decided.
    with world.instance.bind_accepted_projection(
        world.instance.accepted_coordinate()
    ) as projection:
        proposed = {
            str(row[0]).rsplit("/", 1)[1]
            for row in projection.typed.connection.execute(
                "SELECT pair_path FROM pair_units WHERE end_path=? AND predicate=?",
                ("subjects/qr.inventory/i0.json", SCOPE),
            )
        }
    assert proposed == {
        "i0--c0.json",
        "i0--c1.json",
        "i0--c2.json",
        "i0--c4.json",
        "i0--c5.json",
    }
    assert result.verdict_visibility is not None
    assert result.verdict_visibility.excluded_claim_count == 3
    held = run(world, definition(world, sbom_spec("held")), {"inventory": f"{INVENTORY}/i0"})
    assert [row.fields[1].value for row in held.rows] == ["i0--c0", "i0--c1", "i0--c4"]


def test_the_worklist_reads_the_whole_key_pair_per_found_pair(world: ReachWorld) -> None:
    result = run(world, definition(world, worklist_spec()), {"vuln": f"{VULN}/v0"})
    found = sorted(
        (row.fields[0].value, row.fields[1].value, row.fields[2].value) for row in result.rows
    )
    # c3's exposure carries a justification; c6's is stale (hidden), so it stays.
    assert ("c3", "c3--v0", "i1") not in found
    assert ("c0", "c0--v0", "i0") in found and ("c6", "c6--v0", "i1") in found
    assert ("c1", None, "i1") in found and ("c1", None, "i0") not in found


def test_the_spelling_round_trips_and_refuses_what_the_key_does_not_allow(
    world: ReachWorld,
) -> None:
    query = definition(world, worklist_spec()).query
    pairs = [step for step in query.traversal if isinstance(step, QueryPairStep)]
    assert [(step.fact, step.required) for step in pairs] == [(SCOPE, True), (None, False)]
    assert [item.part for item in pairs[1].parts] == sorted(
        [E_COMPONENT, f"{EXPOSURE}.vulnerability"]
    )
    tree = world.instance.tree_at(world.instance.accepted_coordinate().git_oid)
    resolver = AuthoredQueryResolver(candidate_query_vocabulary(tree))
    again = lower_query_spec(query_spec_from_definition(query), resolver)
    assert canonical_bytes(again.model_dump(mode="json")) == canonical_bytes(
        query.model_dump(mode="json")
    )

    def refused(hop: str) -> str:
        spec = sbom_spec()
        spec["hops"] = [{"o": hop}]
        spec["fields"] = [{"occurrence": "o.subject_id"}]
        try:
            lower_query_spec(QuerySpecInput.model_validate(spec), resolver)
        except (QuerySpecError, ReadRefusalError) as exc:
            return str(exc.error_code)
        except ValidationError:
            return "invalid"
        raise AssertionError(hop)

    for hop in (
        f"{OCCURRENCE}{{inventory: i}}",  # a part of the key, without a fact
        f"{OCCURRENCE}{{inventory: i}} with component",  # the fact is a key part
        f"{INVENTORY}{{iid: i}} with scope",  # a literal part
        f"{OCCURRENCE}{{component: i}} with scope",  # the binding is not the end's kind
        f"{OCCURRENCE}{{inventory: nobody}} with scope",  # no such binding
        f"{OCCURRENCE}{{inventory i}}",  # not `part: binding`
    ):
        assert refused(hop) == "cruxible.query.pair_hop_invalid", hop


def test_the_grammar_and_the_acceptance_law_refuse_a_malformed_pair_hop(
    world: ReachWorld,
) -> None:
    with pytest.raises(ValidationError):
        QueryPairStep(
            binding="o",
            subject_kind=OCCURRENCE,
            parts=(
                QueryPairPart(part=O_COMPONENT, binding="c"),
                QueryPairPart(part=O_INVENTORY, binding="i"),
            ),
            fact=SCOPE,
        )
    with pytest.raises(ValidationError):
        QueryPairStep(
            binding="o",
            subject_kind=OCCURRENCE,
            parts=(QueryPairPart(part=O_INVENTORY, binding="i"),),
            fact=O_INVENTORY,
        )
    query = definition(world, sbom_spec()).query
    step = query.traversal[0]
    assert isinstance(step, QueryPairStep)
    whole_key_missing = query.model_copy(
        update={"traversal": (step.model_copy(update={"fact": None}), *query.traversal[1:])}
    )
    updates = {
        query_definition_path(whole_key_missing.identity.name): render_query_definition(
            whole_key_missing
        )
    }
    refused = accept_raw(world.instance, updates, name="pair-hop-incomplete")
    assert refused.candidate is None
    codes = [item.code for item in refused.evaluation.diagnostics]
    assert "cruxible.query.pair_hop_invalid" in codes, codes
    accepted = accept_raw(
        world.instance,
        {query_definition_path(query.identity.name): render_query_definition(query)},
        name="pair-hop-sbom",
    )
    assert accepted.candidate is not None, accepted.evaluation.diagnostics
    from cruxible_core.service.discovery.query import service_run_playbill_query

    ran = service_run_playbill_query(
        world.instance,
        name=query.identity.name,
        evaluation_time=WHEN,
        parameters={"inventory": f"{INVENTORY}/i1"},
    )
    assert ran.result.verdict == "completed" and len(ran.result.rows) == 7


# -- work counts: fixed output, growing unrelated pairs and shells -----------------------

OUTPUT = 10
SIZES = (100, 1000)
COUNTED = ("sql_statements", "sql_vm_steps", "claim_parses")


def _pair_world(root: Path, unrelated: int) -> ReachWorld:
    """``OUTPUT`` occurrences of ``i0`` with a scope and ``OUTPUT`` exposures of ``v0``,
    beside ``unrelated`` occurrences of other inventories, ``unrelated`` historical
    shells of ``i0`` (scope retired) and ``unrelated`` exposures of other vulnerabilities."""

    built = ReachWorld(root)
    inventory = built.subject(INVENTORY, "i0")
    other = built.subject(INVENTORY, "i1")
    vuln = built.subject(VULN, "v0")
    elsewhere = built.subject(VULN, "v1")
    retire = []
    for index in range(OUTPUT):
        component = built.subject(COMPONENT, f"c{index}")
        built.claim(AFFECTED_BY, vuln, component)
        built.claim(SCOPE, "required", built.subject(OCCURRENCE, inventory, component))
        built.claim(
            JUSTIFICATION,
            "vulnerable_code_not_present",
            built.subject(EXPOSURE, component, vuln),
        )
    for index in range(unrelated):
        component = built.subject(COMPONENT, f"u{index}")
        built.claim(SCOPE, "required", built.subject(OCCURRENCE, other, component))
        retire.append(
            built.claim(SCOPE, "required", built.subject(OCCURRENCE, inventory, component))
        )
        built.claim(AFFECTED_BY, elsewhere, component)
        built.claim(
            JUSTIFICATION,
            "vulnerable_code_not_present",
            built.subject(EXPOSURE, component, elsewhere),
        )
    built.accept()
    for claim in retire:
        built.retire(claim)
    built.accept()
    return built


def judgments_spec() -> dict[str, Any]:
    return {
        "query": "qr.judgments",
        "params": {"vuln": "subject"},
        "from": {"v": f"{VULN} = $vuln"},
        "hops": [
            {"c": f"<v.{AFFECTED_BY}!: {COMPONENT}"},
            {"e": f"{EXPOSURE}{{component: c, vulnerability: v}}!"},
        ],
        "fields": [{"exposure": "e.subject_id"}, "e.justification"],
    }


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> Iterator[list[dict[str, Any]]]:
    root = tmp_path_factory.mktemp("pair-counts")
    out: list[dict[str, Any]] = []
    with trace_sqlite(root):
        for size in SIZES:
            built = _pair_world(root / f"u{size}", size)
            reads = {
                "pair_hop_with_scope": (
                    definition(built, sbom_spec()),
                    {"inventory": f"{INVENTORY}/i0"},
                ),
                "whole_key_pair_hop": (definition(built, judgments_spec()), {"vuln": f"{VULN}/v0"}),
            }
            counts: dict[str, Any] = {}
            for name, (query, parameters) in reads.items():
                run(built, query, parameters)
                with counting(root) as window:
                    result = run(built, query, parameters)
                assert not window.unexplained, window.unexplained
                counts[name] = {
                    "rows": len(result.rows),
                    **{counter: window.counts[counter] for counter in COUNTED},
                }
            out.append(counts)
    yield out


@pytest.mark.parametrize("read", ["pair_hop_with_scope", "whole_key_pair_hop"])
def test_pair_hops_are_flat_in_unrelated_pairs_and_shells(
    measured: list[dict[str, Any]], read: str
) -> None:
    small, large = (sized[read] for sized in measured)
    assert small["rows"] == large["rows"] == OUTPUT, (small, large)
    for counter in COUNTED:
        assert small[counter] > 0
        assert large[counter] <= small[counter] * RATIO + SLACK[counter], (read, counter, measured)


def test_a_contradicted_fact_never_binds_its_pair(
    world: ReachWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contradicted scope hides at the shared visibility boundary, on both paths."""

    from cruxible_client.contracts.claims import claim_statement_digest, parse_claim
    from cruxible_core.query import backends

    tree = world.instance.tree_at(world.instance.accepted_coordinate().git_oid)
    target = next(
        claim_statement_digest(claim.statement).tagged
        for path, content in tree.items()
        if path.startswith("claims/")
        for claim in (parse_claim(content, path=path),)
        if claim.statement.predicate == SCOPE
        and claim.statement.subject.artifact_path.endswith("/i0--c0.json")
        and claim.lifecycle.state == "live"
    )
    verdict = backends.evaluate_claim_verdict

    def contradicted(**kwargs: Any) -> Any:
        found = verdict(**kwargs)
        if kwargs["claim_statement_digest"] == target:
            return found.model_copy(update={"verdict": "contradicted"})
        return found

    monkeypatch.setattr(backends, "evaluate_claim_verdict", contradicted)
    whole = served.build_accepted_query_facts(
        world.instance, coordinate=world.instance.accepted_coordinate()
    )
    parameters = {"inventory": f"{INVENTORY}/i0"}
    current = compare(world, definition(world, sbom_spec()), parameters, whole=whole)
    assert [row.fields[1].value for row in current.rows] == ["i0--c4"]
    assert current.verdict_visibility is not None
    assert "contradicted" in {
        item.verdict for item in current.verdict_visibility.excluded_by_verdict
    }
    held = compare(world, definition(world, sbom_spec("held")), parameters, whole=whole)
    assert [row.fields[1].value for row in held.rows] == ["i0--c1", "i0--c4"]
