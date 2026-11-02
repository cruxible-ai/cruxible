"""Node run summaries: built from in-memory counters, typed by kind, bounded in bytes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_evidence import ClaimsDiagnostic, FindingKind
from cruxible_client.contracts.procedures.node_summary import (
    NODE_RUN_SUMMARY_ADAPTER,
    NODE_SUMMARY_CAPTURE_DIGESTS_MAX,
    NODE_SUMMARY_GUARD_OPERANDS_MAX,
    NODE_SUMMARY_MAX_BYTES,
    CallRunSummary,
    ClaimsRunSummary,
    EgressRunSummary,
    ExtractRunSummary,
    GuardOperand,
    GuardRunSummary,
    HaltRunSummary,
    ReadRunSummary,
    RepeatRunSummary,
    TransformRunSummary,
)
from cruxible_core.cli.commands.playbill import _run_node_line
from cruxible_core.procedures.claims_changes import (
    CoreClaimChange,
    CoreReobservationChange,
    CoreRetirementChange,
    CoreSubjectChange,
)
from cruxible_core.procedures.claims_diagnostics import ClaimsDiagnostics
from cruxible_core.procedures.node_summaries import (
    call_summary,
    claims_delivery,
    claims_summary,
    guard_summary,
    row_count,
    transform_summary,
)
from cruxible_core.service.procedures.procedure_runs import run_node

DIGEST = "sha256:" + "a" * 64


def _claim(kind: str = "literal", predecessor: str | None = None) -> Any:
    return CoreClaimChange.model_construct(
        statement=SimpleNamespace(object=SimpleNamespace(kind=kind)), predecessor=predecessor
    )


def _diagnostics(kinds: dict[FindingKind, int], same: int = 0) -> ClaimsDiagnostics:
    diagnostics = ClaimsDiagnostics()
    for kind, count in kinds.items():
        for _ in range(count):
            diagnostics.add(ClaimsDiagnostic.model_construct(kind=kind, target="t"))
    diagnostics.same = same
    return diagnostics


def test_claims_summary_counts_each_change_once() -> None:
    changes = [
        _claim(),
        _claim(),
        _claim(predecessor="c1"),
        _claim(kind="none"),
        _claim(kind="none", predecessor="c2"),
        CoreSubjectChange.model_construct(),
        CoreReobservationChange.model_construct(),
        CoreRetirementChange.model_construct(),
        CoreRetirementChange.model_construct(),
    ]
    summary = claims_summary(
        changes,
        _diagnostics(
            {"claim_not_in_source": 3, "incomplete_source": 2, "held_disagrees": 1}, same=4
        ),
        downgraded=1,
        delivery="settled",
    )
    assert summary == ClaimsRunSummary(
        written=2,
        revised=1,
        inverse_written=2,
        subjects_created=1,
        re_observed=1,
        no_op=3,
        retired=2,
        surfaced=3,
        downgraded=1,
        skipped={"held_disagrees": 1, "incomplete_source": 1},
        delivery="settled",
    )


def test_claims_delivery_reads_the_receipt_kind() -> None:
    def receipt(tag: str, **fields: object) -> object:
        return SimpleNamespace(tag=f"playbill-terminal-egress-{tag}", **fields)

    assert claims_delivery(receipt("receipt-v4", outcome="settled")) == "settled"
    assert claims_delivery(receipt("receipt-v4", outcome="proposed")) == "proposed"
    assert claims_delivery(receipt("receipt-v3")) == "proposed"
    assert claims_delivery(receipt("unchanged-receipt-v1")) == "unchanged"
    assert claims_delivery(receipt("still-current-receipt-v1")) == "still_current"
    with pytest.raises(ValueError):
        claims_delivery(receipt("receipt-v1"))


def test_guard_summary_keeps_numeric_and_bool_operands_only() -> None:
    trace = {
        "all": [
            {"left": 3, "operator": "gt", "right": 1},
            {"left": "secret", "operator": "eq", "right": "value"},
            {"left": "kev", "operator": "in", "right": ["kev", "epss"]},
            {"not": {"operator": "present", "ref": "$steps.x", "present": True}},
            {"any": [{"left": True, "operator": "eq", "right": "yes"}]},
        ]
    }
    summary = guard_summary(False, trace)
    assert summary.branch == "else"
    assert summary.operands == (
        GuardOperand(operator="gt", left=3, right=1),
        GuardOperand(operator="present", left=True),
        GuardOperand(operator="eq", left=True),
    )
    assert "secret" not in summary.model_dump_json() and "kev" not in summary.model_dump_json()
    wide = guard_summary(
        True, {"any": [{"left": n, "operator": "eq", "right": n} for n in range(11)]}
    )
    assert len(wide.operands) == NODE_SUMMARY_GUARD_OPERANDS_MAX
    assert wide.operands_truncated == 11 - NODE_SUMMARY_GUARD_OPERANDS_MAX


def test_counts_of_records_rows_and_captures() -> None:
    assert row_count([1, 2]) == 2
    assert row_count({"records": [1], "rows": [1], "count": 1}) == 1
    assert row_count({"rows": [1, 2, 3]}) == 3
    assert row_count({"value": {}, "result": 4}) is None
    shaped = transform_summary("join", [[1, 2], {"records": [1]}], {"records": [1, 2]})
    assert (shaped.rows_in, shaped.rows_out) == (3, 2)
    aggregated = transform_summary("aggregate", [[1, 2]], {"value": {}, "result": 2})
    assert (aggregated.rows_in, aggregated.rows_out) == (2, None)
    many = {f"f{n}": f"sha256:{n:064x}" for n in range(10)}
    call = call_summary(many, {**many, "other": "sha256:" + "b" * 64})
    assert call.captures == 10 and len(call.capture_digests) == NODE_SUMMARY_CAPTURE_DIGESTS_MAX
    assert call_summary(("capture",), None) == CallRunSummary(captures=0)


def _worst_cases() -> list[Any]:
    kinds: tuple[FindingKind, ...] = (
        "extract_refused",
        "duplicate_key",
        "incomplete_source",
        "held_disagrees",
        "slot_contested",
    )
    big = 10**12
    return [
        ExtractRunSummary(
            records=big,
            invalid_records=big,
            duplicates=big,
            invalid_fields=big,
            complete=False,
            complete_when={
                "count_at": "/" + "x" * 256,
                "expected": big,
                "observed": big,
                "held": False,
            },
        ),
        TransformRunSummary(transform_kind="aggregate", rows_in=big, rows_out=big),
        ReadRunSummary(rows=big),
        CallRunSummary(captures=big, capture_digests=(DIGEST,) * NODE_SUMMARY_CAPTURE_DIGESTS_MAX),
        GuardRunSummary(
            branch="else",
            operands=(GuardOperand(operator="not_in", left=-big, right=big),)
            * NODE_SUMMARY_GUARD_OPERANDS_MAX,
            operands_truncated=big,
        ),
        ClaimsRunSummary(
            written=big,
            revised=big,
            inverse_written=big,
            subjects_created=big,
            re_observed=big,
            no_op=big,
            retired=big,
            surfaced=big,
            downgraded=big,
            skipped=dict.fromkeys(kinds, big),
            delivery="still_current",
        ),
        EgressRunSummary(kind="post_inbox", emitted=big, unposted=big),
        HaltRunSummary(reason="r" * 128),
        RepeatRunSummary(attempts=big),
    ]


@pytest.mark.parametrize("summary", _worst_cases(), ids=lambda s: s.kind)
def test_every_summary_kind_is_bounded_and_round_trips(summary: Any) -> None:
    wire = summary.model_dump(mode="json")
    assert len(canonical_bytes(wire)) <= NODE_SUMMARY_MAX_BYTES
    assert NODE_RUN_SUMMARY_ADAPTER.validate_python(wire) == summary


def test_run_node_reads_a_payload_with_or_without_a_summary() -> None:
    old = run_node({"node_id": "n", "kind": "project", "verdict": "succeeded"}, sequence=3)
    assert old.summary is None and old.node == "n"
    read = run_node(
        {
            "node_id": "w",
            "kind": "read",
            "verdict": "succeeded",
            "summary": {"kind": "read", "rows": 4},
        },
        sequence=4,
    )
    assert read.summary == ReadRunSummary(rows=4)


@pytest.mark.parametrize(
    ("summary", "line"),
    [
        (
            ExtractRunSummary(
                records=1247,
                invalid_records=1,
                duplicates=0,
                invalid_fields=2,
                complete=False,
                complete_when={
                    "count_at": "/count",
                    "expected": 1247,
                    "observed": 1247,
                    "held": True,
                },
            ),
            "entries extract succeeded: 1247 records, 1 invalid record, 2 invalid fields, "
            "complete_when 1247/1247, incomplete",
        ),
        (
            TransformRunSummary(transform_kind="filter", rows_in=1247, rows_out=12),
            "entries transform succeeded: filter 1247 -> 12 rows",
        ),
        (
            GuardRunSummary(
                branch="then", operands=(GuardOperand(operator="gt", left=12, right=0),)
            ),
            "entries guard succeeded: took then (12 gt 0)",
        ),
        (
            ClaimsRunSummary(
                written=3,
                revised=0,
                inverse_written=0,
                subjects_created=0,
                re_observed=9,
                no_op=0,
                retired=0,
                surfaced=0,
                downgraded=0,
                skipped={"extract_refused": 1},
                delivery="proposed",
            ),
            "entries claims succeeded: 3 written, 9 re-observed, skipped extract_refused=1; "
            "proposed",
        ),
    ],
)
def test_cli_prints_one_compact_line_per_node(summary: Any, line: str) -> None:
    node = {
        "node": "entries",
        "kind": summary.kind,
        "verdict": "succeeded",
        "summary": summary.model_dump(mode="json"),
    }
    assert _run_node_line(node) == line


def test_run_detail_prints_a_node_line_per_finished_node(capsys: Any) -> None:
    from cruxible_client.contracts.operational_reads import GetRunNode
    from cruxible_core.cli.commands.playbill import _echo_source_observations

    nodes = [
        GetRunNode(
            node="watched",
            kind="read",
            verdict="succeeded",
            sequence=2,
            summary=ReadRunSummary(rows=12),
        ),
        GetRunNode(node="tidy", kind="project", verdict="succeeded", sequence=3),
    ]
    state = SimpleNamespace(source_observations=[], terminal_egress=[], nodes=nodes)
    _echo_source_observations(state)  # type: ignore[arg-type]
    assert capsys.readouterr().out == (
        "Node watched read succeeded: 12 rows\nNode tidy project succeeded\n"
    )
