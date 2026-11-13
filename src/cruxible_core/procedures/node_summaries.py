"""Each node's run summary, from counters the executor already holds in memory.

Nothing here reads a store or recomputes a node: every summary is built from
the node's own outcome (an extract's, a transform's value, a guard's operand
trace) or from the counters the claims terminal and the completeness law keep
anyway (``ClaimsDiagnostics``, ``Completeness.downgraded``, the terminal's
changes and its delivery receipt). See
:mod:`cruxible_client.contracts.procedures.node_summary` for what each says.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast, get_args

from cruxible_client.contracts.claim_evidence import CompleteWhenResult, FindingKind
from cruxible_client.contracts.procedures.node_summary import (
    NODE_SUMMARY_CAPTURE_DIGESTS_MAX,
    NODE_SUMMARY_GUARD_OPERANDS_MAX,
    NODE_SUMMARY_INT_MAX,
    NODE_SUMMARY_TEXT_MAX,
    CallRunSummary,
    ClaimsDelivery,
    ClaimsRunSummary,
    ExtractCompleteWhen,
    ExtractRunSummary,
    GuardOperand,
    GuardOperator,
    GuardRunSummary,
    HaltRunSummary,
    NodeRunSummary,
    ReobservationRefusalReason,
    TransformRunSummary,
)
from cruxible_client.contracts.shapes import DuplicateArm
from cruxible_core.procedures.claims_changes import (
    CoreClaimChange,
    CoreClaimsChange,
    CoreReobservationChange,
    CoreRetirementChange,
    CoreSubjectChange,
)
from cruxible_core.procedures.claims_diagnostics import ClaimsDiagnostics

if TYPE_CHECKING:
    from cruxible_core.procedures.extract import ExtractOutcome

#: The finding a held Claim the source no longer contains is surfaced as.
_SURFACED: FindingKind = "claim_not_in_source"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATORS = frozenset(
    member for part in get_args(GuardOperator) for member in (get_args(part) or (part,))
)


def record_summary(
    summaries: dict[str, NodeRunSummary], node_id: str, build: Callable[[], NodeRunSummary]
) -> None:
    """Keep one node's summary; a summary that cannot be built is left out, never raised.

    A summary only observes a node that already ran: whatever it meets, it
    must never turn a valid node, or a delivered terminal, into a failed one.
    """

    try:
        summaries[node_id] = build()
    except ValueError:  # pydantic's ValidationError included
        summaries.pop(node_id, None)


def _count(value: int) -> int:
    """An in-memory count, clamped to the summary bound."""

    return min(max(value, 0), NODE_SUMMARY_INT_MAX)


def _bounded(value: int | None) -> tuple[int | None, bool]:
    """A source integer within the summary bound, or ``None`` and ``True`` past it."""

    if value is None or -NODE_SUMMARY_INT_MAX <= value <= NODE_SUMMARY_INT_MAX:
        return value, False
    return None, True


def _text(value: str) -> tuple[str, bool]:
    """Authored text cut to the summary bound, and whether it was cut."""

    return value[:NODE_SUMMARY_TEXT_MAX], len(value) > NODE_SUMMARY_TEXT_MAX


def row_count(value: object) -> int | None:
    """How many records or rows a run value holds, or ``None`` when it is one value."""

    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        for name in ("records", "rows"):
            held = value.get(name)
            if isinstance(held, list):
                return len(held)
    return None


def complete_when_summary(result: CompleteWhenResult) -> ExtractCompleteWhen:
    count_at, cut = _text(result.count_at)
    expected, expected_out = _bounded(result.expected)
    observed, observed_out = _bounded(result.observed)
    return ExtractCompleteWhen(
        count_at=count_at,
        count_at_truncated=cut,
        expected=expected,
        observed=observed,
        out_of_range=expected_out or observed_out,
        held=result.held,
    )


def extract_summary(outcome: ExtractOutcome) -> ExtractRunSummary:
    check = outcome.complete_when_result
    return ExtractRunSummary(
        records=_count(len(outcome.held)),
        invalid_records=_count(outcome.invalid_records),
        duplicates=_count(sum(1 for item in outcome.held if isinstance(item, DuplicateArm))),
        invalid_fields=_count(sum(outcome.invalid_fields.values())),
        complete=outcome.complete,
        complete_when=None if check is None else complete_when_summary(check),
    )


def transform_summary(
    transform_kind: str, rows_in: Iterable[int | None], value: object
) -> TransformRunSummary:
    """``rows_in`` counts each record input as the transform itself resolved it."""

    counted = [count for count in rows_in if count is not None]
    rows_out = row_count(value)
    return TransformRunSummary(
        transform_kind=cast(Any, transform_kind),
        rows_in=_count(sum(counted)) if counted else None,
        rows_out=None if rows_out is None else _count(rows_out),
    )


def call_summary(capture_fields: Iterable[str], output: object) -> CallRunSummary:
    """A call's Captures: the digest each capture field of its output holds.

    A list field holds several, one per member, in order (D-R37 a1).
    """

    held = output if isinstance(output, Mapping) else {}

    def named(value: object) -> list[object]:
        return list(value) if isinstance(value, list) else [value]

    digests = [
        digest
        for name in capture_fields
        for digest in named(held.get(name))
        if isinstance(digest, str) and _DIGEST.fullmatch(digest)
    ]
    return CallRunSummary(
        captures=_count(len(digests)),
        capture_digests=tuple(digests[:NODE_SUMMARY_CAPTURE_DIGESTS_MAX]),
    )


def halt_summary(reason: str | None) -> HaltRunSummary:
    if reason is None:
        return HaltRunSummary()
    text, cut = _text(reason)
    return HaltRunSummary(reason=text, reason_truncated=cut)


def _operand(value: object) -> tuple[int | bool | None, bool]:
    """A numeric or bool operand within the bound, and whether one was left out past it."""

    if isinstance(value, bool):
        return value, False
    if isinstance(value, int):
        return _bounded(value)
    return None, False


def _comparisons(trace: object) -> Iterable[GuardOperand]:
    """Every comparison in a guard's operand trace with a numeric or bool operand."""

    if not isinstance(trace, Mapping):
        return
    if "not" in trace:
        yield from _comparisons(trace["not"])
        return
    for combinator in ("all", "any"):
        if combinator in trace:
            for child in trace[combinator] if isinstance(trace[combinator], list) else ():
                yield from _comparisons(child)
            return
    operator = trace.get("operator")
    if not isinstance(operator, str) or operator not in _OPERATORS:
        return
    if "present" in trace:
        (left, left_out), (right, right_out) = _operand(trace["present"]), (None, False)
    else:
        (left, left_out), (right, right_out) = (
            _operand(trace.get("left")),
            _operand(trace.get("right")),
        )
    if left is not None or right is not None or left_out or right_out:
        yield GuardOperand(
            operator=cast(GuardOperator, operator),
            left=left,
            right=right,
            out_of_range=left_out or right_out,
        )


def guard_summary(verdict: bool, trace: object) -> GuardRunSummary:
    found = list(_comparisons(trace))
    return GuardRunSummary(
        branch="then" if verdict else "else",
        operands=tuple(found[:NODE_SUMMARY_GUARD_OPERANDS_MAX]),
        operands_truncated=_count(len(found) - NODE_SUMMARY_GUARD_OPERANDS_MAX),
    )


_RECEIPT_DELIVERIES: dict[str, ClaimsDelivery] = {
    "playbill-terminal-egress-receipt-v3": "proposed",
    "playbill-terminal-egress-unchanged-receipt-v1": "unchanged",
    "playbill-terminal-egress-still-current-receipt-v1": "still_current",
}


def claims_delivery(receipt: object) -> ClaimsDelivery:
    """How a claims terminal's delivered receipt landed its change.

    Every receipt the verifier accepts maps to its delivery; any other reads
    ``delivered``. The summary only observes a delivery that already happened.
    """

    tag = getattr(receipt, "tag", None)
    if tag == "playbill-terminal-egress-receipt-v4":
        outcome = getattr(receipt, "outcome", None)
        return "settled" if outcome == "settled" else "proposed"
    if isinstance(tag, str) and tag in _RECEIPT_DELIVERIES:
        return _RECEIPT_DELIVERIES[tag]
    return "delivered"


def claims_summary(
    changes: Iterable[CoreClaimsChange],
    diagnostics: ClaimsDiagnostics | None,
    *,
    downgraded: int,
    delivery: ClaimsDelivery,
    reobservations_refused: Mapping[ReobservationRefusalReason, int] | None = None,
) -> ClaimsRunSummary:
    """A claims terminal's counts: its changes, and the counters its expansion kept.

    ``ClaimsDiagnostics.same`` counts every restatement, re-observed or not;
    each re-observation is also one change, so ``no_op`` is their difference.
    ``reobservations_refused`` counts, by reason, the re-observations delivery
    could not record: those are never counted ``re_observed``.
    Each downgrade was reported ``incomplete_source`` and still written, so it
    is counted in ``downgraded`` and not as skipped.
    """

    written = revised = inverse = subjects = observed = retired = 0
    for change in changes:
        if isinstance(change, CoreClaimChange):
            if change.statement.object.kind == "none":
                inverse += 1
            elif change.predecessor is None:
                written += 1
            else:
                revised += 1
        elif isinstance(change, CoreSubjectChange):
            subjects += 1
        elif isinstance(change, CoreReobservationChange):
            observed += 1
        elif isinstance(change, CoreRetirementChange):
            retired += 1
    counts: dict[FindingKind, int] = {} if diagnostics is None else dict(diagnostics.counts)
    surfaced = counts.pop(_SURFACED, 0)
    if downgraded:
        counts["incomplete_source"] = counts.get("incomplete_source", 0) - downgraded
    same = 0 if diagnostics is None else diagnostics.same
    refused = {
        reason: count for reason, count in sorted((reobservations_refused or {}).items()) if count
    }
    return ClaimsRunSummary(
        written=_count(written),
        revised=_count(revised),
        inverse_written=_count(inverse),
        subjects_created=_count(subjects),
        re_observed=_count(observed - sum(refused.values())),
        reobservations_refused={reason: _count(count) for reason, count in refused.items()},
        no_op=_count(same - observed),
        retired=_count(retired),
        surfaced=_count(surfaced),
        downgraded=_count(downgraded),
        skipped={kind: _count(count) for kind, count in sorted(counts.items()) if count > 0},
        delivery=delivery,
    )


__all__ = [
    "call_summary",
    "claims_delivery",
    "claims_summary",
    "complete_when_summary",
    "extract_summary",
    "guard_summary",
    "halt_summary",
    "record_summary",
    "row_count",
    "transform_summary",
]
