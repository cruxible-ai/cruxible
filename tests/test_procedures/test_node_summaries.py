"""Node run summaries: built from in-memory counters, typed by kind, bounded in bytes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, get_args

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_evidence import (
    ClaimsDiagnostic,
    CompleteWhenResult,
    FindingKind,
)
from cruxible_client.contracts.procedures.node_summary import (
    NODE_RUN_SUMMARY_ADAPTER,
    NODE_SUMMARY_CAPTURE_DIGESTS_MAX,
    NODE_SUMMARY_GUARD_OPERANDS_MAX,
    NODE_SUMMARY_INT_MAX,
    NODE_SUMMARY_MAX_BYTES,
    NODE_SUMMARY_TEXT_MAX,
    CallRunSummary,
    ClaimsRunSummary,
    EgressRunSummary,
    ExtractRunSummary,
    GuardOperand,
    GuardRunSummary,
    ReadRunSummary,
    RepeatRunSummary,
    TransformRunSummary,
)
from cruxible_client.contracts.shapes import DuplicateArm
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
    extract_summary,
    guard_summary,
    halt_summary,
    record_summary,
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
    # A v1 receipt names only its disposition: settled, or a fallback proposal.
    assert claims_delivery(receipt("receipt-v1", disposition="settled")) == "settled"
    assert claims_delivery(receipt("receipt-v1", disposition="received")) == "proposed"
    # Anything else is still a delivery, never a failure.
    assert claims_delivery(receipt("receipt-v2", disposition="received")) == "delivered"
    assert claims_delivery(object()) == "delivered"


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
    shaped = transform_summary("join", [2, 1], {"records": [1, 2]})
    assert (shaped.rows_in, shaped.rows_out) == (3, 2)
    aggregated = transform_summary("aggregate", [2], {"value": {}, "result": 2})
    assert (aggregated.rows_in, aggregated.rows_out) == (2, None)
    assert transform_summary("derive", [], {"value": {}}).rows_in is None
    many = {f"f{n}": f"sha256:{n:064x}" for n in range(10)}
    call = call_summary(many, {**many, "other": "sha256:" + "b" * 64})
    assert call.captures == 10 and len(call.capture_digests) == NODE_SUMMARY_CAPTURE_DIGESTS_MAX
    assert call_summary(("capture",), None) == CallRunSummary(captures=0)
    # A capture field holding something other than a digest is not a Capture.
    assert call_summary(("capture",), {"capture": "x" * 5000}) == CallRunSummary(captures=0)


#: The widest legal text a summary could meet: 512 four-byte code points, plus
#: control characters, which canonical JSON escapes to six bytes each.
WIDE_TEXT = "/" + "\U0001f600" * 255 + "\u0001" * 256
HUGE = 10**3000


def _worst_cases() -> list[Any]:
    """Each kind at its bound, built through the same builders the executor uses."""

    kinds = get_args(FindingKind)
    big = NODE_SUMMARY_INT_MAX
    extract = SimpleNamespace(
        held=[DuplicateArm.model_construct()] * 3,
        invalid_records=HUGE,
        invalid_fields={"a": HUGE, "b": HUGE},
        complete=False,
        complete_when_result=CompleteWhenResult.model_construct(
            count_at=WIDE_TEXT, expected=HUGE, observed=-HUGE, held=False
        ),
    )
    many = {f"f{n}": DIGEST for n in range(100)}
    return [
        extract_summary(extract),  # type: ignore[arg-type]
        transform_summary("aggregate", [HUGE, HUGE], {"records": [1]}),
        ReadRunSummary(rows=big),
        call_summary(many, many),
        guard_summary(
            False,
            {"any": [{"left": -HUGE, "operator": "not_in", "right": HUGE} for _ in range(20)]},
        ),
        guard_summary(
            True,
            {"all": [{"left": -big, "operator": "ge", "right": big} for _ in range(20)]},
        ),
        claims_summary(
            [],
            SimpleNamespace(counts=dict.fromkeys(kinds, HUGE), same=HUGE),  # type: ignore[arg-type]
            downgraded=HUGE,
            delivery="still_current",
        ),
        EgressRunSummary(kind="post_inbox", emitted=big, unposted=big),
        halt_summary(WIDE_TEXT),
        RepeatRunSummary(attempts=big),
    ]


@pytest.mark.parametrize("summary", _worst_cases(), ids=lambda s: s.kind)
def test_every_summary_kind_is_bounded_and_round_trips(summary: Any) -> None:
    wire = summary.model_dump(mode="json")
    assert len(canonical_bytes(wire)) <= NODE_SUMMARY_MAX_BYTES
    assert NODE_RUN_SUMMARY_ADAPTER.validate_python(wire) == summary


def test_oversized_source_values_are_left_out_and_marked() -> None:
    extract, *_, guard, _, _, _, halt, _ = _worst_cases()
    check = extract.complete_when
    assert check.count_at == WIDE_TEXT[:NODE_SUMMARY_TEXT_MAX] and check.count_at_truncated
    assert check.expected is None and check.observed is None and check.out_of_range
    assert extract.invalid_records == NODE_SUMMARY_INT_MAX
    (first, *_) = guard.operands
    assert first == GuardOperand(operator="not_in", out_of_range=True)
    assert guard.operands_truncated == 20 - NODE_SUMMARY_GUARD_OPERANDS_MAX
    assert halt.reason_truncated and len(halt.reason) == NODE_SUMMARY_TEXT_MAX


def test_the_contract_refuses_unbounded_scalars() -> None:
    from pydantic import ValidationError

    for bad in (
        {"kind": "read", "rows": NODE_SUMMARY_INT_MAX + 1},
        {"kind": "call", "captures": 1, "capture_digests": ["not-a-digest"]},
        {"kind": "transform", "transform_kind": "x" * 100},
        {"kind": "guard", "branch": "then", "operands": [{"operator": "x" * 100}]},
        {"kind": "guard", "branch": "then", "operands": [{"operator": "gt", "left": HUGE}]},
        {"kind": "halt", "reason": "r" * (NODE_SUMMARY_TEXT_MAX + 1)},
    ):
        with pytest.raises(ValidationError):
            NODE_RUN_SUMMARY_ADAPTER.validate_python(bad)


def test_a_summary_that_cannot_be_built_is_left_out_not_raised() -> None:
    summaries: dict[str, Any] = {"n": ReadRunSummary(rows=1)}

    def broken() -> Any:
        return ReadRunSummary(rows=-1)

    record_summary(summaries, "n", broken)
    assert summaries == {}


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


@pytest.mark.parametrize("size", [5, 200])
def test_transform_rows_are_counted_from_the_inputs_the_transform_resolved(
    tmp_path: Any, monkeypatch: Any, size: int
) -> None:
    """The summary reads each input's length once: no second resolution, no record walk."""

    from cruxible_core.procedures import execution
    from tests.test_procedures.test_closed_transforms import _feed, _returned, _run, _status
    from tests.test_procedures.test_closed_transforms import _transform as transform

    resolved: list[tuple[str, object]] = []
    counted: list[object] = []
    summaries: list[Any] = []
    real_resolve, real_count, real_summary = (
        execution._resolve_node_template,
        execution.row_count,
        execution.transform_summary,
    )

    def resolve(reference: object, **kwargs: Any) -> object:
        value = real_resolve(reference, **kwargs)
        resolved.append((kwargs["node_id"], value))
        return value

    def count(value: object) -> int | None:
        counted.append(value)
        return real_count(value)

    def summary(*args: Any) -> Any:
        summaries.append(found := real_summary(*args))
        return found

    monkeypatch.setattr(execution, "_resolve_node_template", resolve)
    monkeypatch.setattr(execution, "row_count", count)
    monkeypatch.setattr(execution, "transform_summary", summary)
    records = tuple(_status("open") for _ in range(size))
    nodes = (
        _feed(*records),
        transform(
            "kept",
            "filter",
            {
                "input": "$steps.feed.rows",
                "where": {"op": "present", "of": {"$ref": "item", "path": ["fields", "status"]}},
            },
        ),
        _returned("kept"),
    )
    result, _sinks = _run(nodes, tmp_path)
    assert result.status == "succeeded", result
    (filtered,) = summaries
    assert (filtered.rows_in, filtered.rows_out) == (size, size)
    # The filter resolved its one input once, and the summary counted that very value.
    kept = [value for node_id, value in resolved if node_id == "kept"]
    assert len(kept) == 1
    assert any(value is kept[0] for value in counted)
    # Constant work whatever the size: the filter's and the result's one resolution
    # each, and one length read for the summary.
    assert len(resolved) == 2 and len(counted) == 1


class _V1ClaimsSink:
    """A sink that is no preparer: the claims terminal keeps its v1 request and receipt."""

    def __init__(self) -> None:
        self.receipts: list[Any] = []

    def deliver_terminal_egress(self, *, request: Any) -> Any:
        from cruxible_core.procedures.egress import (
            TerminalEgressChildReceiptV1,
            TerminalEgressReceiptV1,
        )

        receipt = TerminalEgressReceiptV1(
            kind=request.kind,
            run_id=request.run_id,
            node_id=request.node_id,
            disposition="received",
            children=tuple(
                TerminalEgressChildReceiptV1(
                    child_index=item.child_index,
                    item_key=item.item_key,
                    egress_digest="sha256:" + f"{item.child_index:064x}",
                )
                for item in request.items
            ),
        )
        self.receipts.append(receipt)
        return receipt


def test_a_v1_claims_delivery_is_summarised_not_failed(tmp_path: Any, monkeypatch: Any) -> None:
    """A sink that is no preparer gets a v1 claims request; its verified v1 receipt is a delivery.

    The executor's own account is checked: the served run state does not fold
    a delivered claims egress that names no proposal, which is not this path's
    concern.
    """

    from contextlib import suppress

    from pydantic import ValidationError

    from cruxible_core.exhaust.payloads import read_payload
    from cruxible_core.service.procedures import procedure_runs
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.test_pilot.support import pilot_world
    from tests.test_procedures.test_claims_terminal import run

    sink = _V1ClaimsSink()
    results: list[Any] = []
    real = procedure_runs.service_execute_direct_procedure

    def execute(*args: Any, **kwargs: Any) -> Any:
        results.append(found := real(*args, **{**kwargs, "egress_sink": sink}))
        return found

    monkeypatch.setattr(procedure_runs, "service_execute_direct_procedure", execute)
    instance, root = pilot_world(tmp_path, "kev.json")
    with suppress(ValidationError):
        run(instance, root)
    (receipt,) = sink.receipts
    assert receipt.tag == "playbill-terminal-egress-receipt-v1"
    (result,) = results
    assert result.status == "succeeded", result
    access = BodyAccessContext(principal_id="test", can_read_body=True)
    fired = [
        read_payload(instance.body_store(), stored.record.payload_digest, access=access)
        for stored in procedure_runs._records_for_run(instance, result.run_id)
        if stored.record.event_kind == "node_fired"
    ]
    (claims,) = (payload for payload in fired if payload["kind"] == "claims")
    assert claims["verdict"] == "succeeded"
    assert claims["summary"]["delivery"] == "proposed" and claims["summary"]["written"] == 8


def test_a_negative_source_expectation_keeps_the_extract_summary() -> None:
    """A source may state any count, -1 too: the summary keeps it, and the check failed."""

    check = CompleteWhenResult(count_at="/count", expected=-1, observed=3, held=False)
    outcome = SimpleNamespace(
        held=[], invalid_records=0, invalid_fields={}, complete=False, complete_when_result=check
    )
    summaries: dict[str, Any] = {}
    record_summary(summaries, "entries", lambda: extract_summary(outcome))  # type: ignore[arg-type]
    summary = summaries["entries"]
    assert summary.complete_when.expected == -1 and summary.complete_when.observed == 3
    assert not summary.complete_when.held and not summary.complete_when.out_of_range


class _CountingInputs(tuple):  # type: ignore[type-arg]
    """A transform's record inputs that count how many of them the summary looks at."""

    seen = 0

    def __iter__(self) -> Any:
        for item in super().__iter__():
            type(self).seen += 1
            yield item


@pytest.mark.parametrize("fan_in", [4, 64])
def test_union_fan_in_is_matched_in_constant_time_per_input(
    tmp_path: Any, monkeypatch: Any, fan_in: int
) -> None:
    from cruxible_core.procedures import execution
    from tests.test_procedures.test_closed_transforms import _returned, _run, _status
    from tests.test_procedures.test_closed_transforms import _transform as transform

    real_inputs = execution._record_inputs
    _CountingInputs.seen = 0
    monkeypatch.setattr(
        execution, "_record_inputs", lambda spec: _CountingInputs(real_inputs(spec))
    )
    summaries: list[Any] = []
    real_summary = execution.transform_summary

    def summary(*args: Any) -> Any:
        summaries.append(found := real_summary(*args))
        return found

    monkeypatch.setattr(execution, "transform_summary", summary)
    from cruxible_client.contracts.procedures.models import ProjectNode

    # Distinct authored inputs: one feed per input, so no two references are one object.
    feeds = tuple(
        ProjectNode(
            node_id=f"feed{n}",
            literal={"rows": {"records": [_status("open"), _status("closed")], "complete": True}},
            as_=f"feed{n}",
        )
        for n in range(fan_in)
    )
    nodes = (
        *feeds,
        transform("merged", "union", {"inputs": [f"$steps.feed{n}.rows" for n in range(fan_in)]}),
        _returned("merged"),
    )
    result, _sinks = _run(nodes, tmp_path)
    assert result.status == "succeeded", result
    (merged,) = summaries
    assert (merged.rows_in, merged.rows_out) == (2 * fan_in, 2 * fan_in)
    # Each input is looked at once (to build the identity set), not once per resolution.
    assert _CountingInputs.seen == fan_in
