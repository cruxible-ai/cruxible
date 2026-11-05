"""Pilot runs journal a bounded summary per node, served on the run state and the run card."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.node_summary import (
    NODE_SUMMARY_MAX_BYTES,
    ClaimsRunSummary,
    ExtractRunSummary,
    GuardRunSummary,
)
from tests.support.procedures import procedure_spec, refs
from tests.test_pilot.support import FIXTURES, kev_spec, pilot_world
from tests.test_procedures.test_claims_terminal import activate, candidate_claims, run

# Executor-only fixtures: these tests author Procedure step kinds the template
# closes at authoring (D-R37 h1) to cover their executor handlers (h3).
pytestmark = pytest.mark.executor_only


def _summaries(state: Any) -> dict[str, Any]:
    return {node.node: node.summary for node in state.nodes}


def _card_nodes(instance: Any, run_id: str) -> tuple[Any, ...]:
    from datetime import datetime, timezone

    from cruxible_core.service.discovery.runs import procedure_run_card

    card = procedure_run_card(
        instance,
        run_id,
        evaluation_time=datetime(2026, 11, 2, tzinfo=timezone.utc),
        render=lambda target, detail: target,
    )
    return card.nodes


def _fixture_records(name: str) -> int:
    return len(json.loads((FIXTURES / name).read_bytes())["vulnerabilities"])


def test_kev_run_summarises_every_node(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev.json")
    state = run(instance, root)
    summaries = _summaries(state)
    assert [node.kind for node in state.nodes] == ["call", "read", "extract", "claims"]
    assert all(node.verdict == "succeeded" for node in state.nodes)

    call = summaries["catalog"]
    assert call.captures == 1
    assert call.capture_digests == tuple(o.capture_digest for o in state.source_observations)
    assert summaries["watched"].rows == 2

    extract = summaries["entries"]
    assert isinstance(extract, ExtractRunSummary)
    assert extract.records == _fixture_records("kev.json")
    assert (extract.invalid_records, extract.duplicates, extract.invalid_fields) == (0, 0, 0)
    assert extract.complete and extract.complete_when is not None and extract.complete_when.held

    claims = summaries["write"]
    assert isinstance(claims, ClaimsRunSummary)
    (egress,) = state.terminal_egress
    assert claims.written == len(candidate_claims(instance, state)) == 8
    assert claims.written == sum(1 for child in egress.children if not child.unchanged)
    assert (claims.revised, claims.re_observed, claims.retired, claims.no_op) == (0, 0, 0, 0)
    assert claims.delivery == "proposed" and not claims.skipped

    # The run card serves the same summaries, read from the same records.
    assert {node.node: node.summary for node in _card_nodes(instance, state.run_id)} == summaries

    activate(instance, state)
    again = run(instance, root, minute=1)
    restated = _summaries(again)["write"]
    (egress,) = again.terminal_egress
    assert all(child.unchanged for child in egress.children)
    assert restated.written == 0 and restated.delivery == "unchanged"
    assert restated.re_observed == len(egress.children) == 8


def test_invalid_field_and_duplicates_are_counted_where_they_arise(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev-invalid.json")
    state = run(instance, root)
    summaries = _summaries(state)
    assert summaries["entries"].records == _fixture_records("kev-invalid.json")
    assert summaries["entries"].invalid_fields == 1
    claims = summaries["write"]
    assert claims.written == len(candidate_claims(instance, state)) == 7
    assert claims.skipped == {"extract_refused": len(state.terminal_egress[0].claims_diagnostics)}

    (tmp_path / "duplicates").mkdir()
    instance, root = pilot_world(tmp_path / "duplicates", "kev-duplicates.json")
    state = run(instance, root)
    extract = _summaries(state)["entries"]
    assert extract.records == _fixture_records("kev-duplicates.json")
    assert extract.duplicates == 2 and not extract.complete
    assert _summaries(state)["write"].written == len(candidate_claims(instance, state)) == 4


@pytest.mark.parametrize(("threshold", "branch"), [(0, "then"), (5, "else")])
def test_guard_summary_names_its_branch_and_numeric_operands(
    tmp_path: Path, threshold: int, branch: str
) -> None:
    base = kev_spec().model_dump(mode="python", by_alias=True)
    guard = refs(
        {
            "guard": {
                "op": "gt",
                "left": "$steps.entries.count",
                "right": {"literal": threshold},
            },
            "else": {"halt": "nothing_listed"},
        }
    )
    base["steps"] = [*base["steps"][:-1], guard, base["steps"][-1]]
    instance, root = pilot_world(tmp_path, "kev.json", spec=procedure_spec(base=base))
    state = run(instance, root)
    summaries = _summaries(state)
    (guard_summary,) = (s for s in summaries.values() if isinstance(s, GuardRunSummary))
    assert guard_summary.branch == branch
    (operand,) = guard_summary.operands
    assert (operand.operator, operand.left, operand.right) == ("gt", 2, threshold)
    if branch == "else":
        assert state.status == "halted"
        assert "write" not in summaries
        (halt,) = (n for n in state.nodes if n.kind == "halt")
        assert halt.summary is not None and halt.summary.kind == "halt"
    else:
        assert summaries["write"].written == 8


def test_node_fired_summaries_stay_within_the_byte_bound(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev-invalid.json")
    state = run(instance, root)
    for node in state.nodes:
        assert node.summary is not None
        size = len(canonical_bytes(node.summary.model_dump(mode="json")))
        assert size <= NODE_SUMMARY_MAX_BYTES, (node.node, size)
