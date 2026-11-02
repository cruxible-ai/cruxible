"""Each node's run summary, from counters the executor already holds in memory.

Nothing here reads a store or recomputes a node: every summary is built from
the node's own outcome (an extract's, a transform's value, a guard's operand
trace) or from the counters the claims terminal and the completeness law keep
anyway (``ClaimsDiagnostics``, ``Completeness.downgraded``, the terminal's
changes and its delivery receipt). See
:mod:`cruxible_client.contracts.procedures.node_summary` for what each says.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from cruxible_client.contracts.claim_evidence import FindingKind
from cruxible_client.contracts.procedures.node_summary import (
    NODE_SUMMARY_CAPTURE_DIGESTS_MAX,
    NODE_SUMMARY_GUARD_OPERANDS_MAX,
    CallRunSummary,
    ClaimsDelivery,
    ClaimsRunSummary,
    ExtractRunSummary,
    GuardOperand,
    GuardRunSummary,
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


def extract_summary(outcome: ExtractOutcome) -> ExtractRunSummary:
    return ExtractRunSummary(
        records=len(outcome.held),
        invalid_records=outcome.invalid_records,
        duplicates=sum(1 for item in outcome.held if isinstance(item, DuplicateArm)),
        invalid_fields=sum(outcome.invalid_fields.values()),
        complete=outcome.complete,
        complete_when=outcome.complete_when_result,
    )


def transform_summary(
    transform_kind: str, inputs: Iterable[object], value: object
) -> TransformRunSummary:
    """``inputs`` are the transform's resolved record inputs; ``value`` its output."""

    counted = [count for count in map(row_count, inputs) if count is not None]
    return TransformRunSummary(
        transform_kind=transform_kind,
        rows_in=sum(counted) if counted else None,
        rows_out=row_count(value),
    )


def call_summary(capture_fields: Iterable[str], output: object) -> CallRunSummary:
    """A call's Captures: the digest each capture field of its output holds."""

    held = output if isinstance(output, Mapping) else {}
    digests = [digest for name in capture_fields if isinstance(digest := held.get(name), str)]
    return CallRunSummary(
        captures=len(digests),
        capture_digests=tuple(digests[:NODE_SUMMARY_CAPTURE_DIGESTS_MAX]),
    )


def _operand(value: object) -> int | bool | None:
    return value if isinstance(value, int) else None  # bool is an int


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
    if not isinstance(operator, str):
        return
    if "present" in trace:
        left = _operand(trace["present"])
        right = None
    else:
        left, right = _operand(trace.get("left")), _operand(trace.get("right"))
    if left is not None or right is not None:
        yield GuardOperand(operator=operator, left=left, right=right)


def guard_summary(verdict: bool, trace: object) -> GuardRunSummary:
    found = list(_comparisons(trace))
    return GuardRunSummary(
        branch="then" if verdict else "else",
        operands=tuple(found[:NODE_SUMMARY_GUARD_OPERANDS_MAX]),
        operands_truncated=max(len(found) - NODE_SUMMARY_GUARD_OPERANDS_MAX, 0),
    )


_RECEIPT_DELIVERIES: dict[str, ClaimsDelivery] = {
    "playbill-terminal-egress-receipt-v3": "proposed",
    "playbill-terminal-egress-unchanged-receipt-v1": "unchanged",
    "playbill-terminal-egress-still-current-receipt-v1": "still_current",
}


def claims_delivery(receipt: object) -> ClaimsDelivery:
    """How a claims terminal's delivered receipt landed its change."""

    tag = getattr(receipt, "tag", None)
    if tag == "playbill-terminal-egress-receipt-v4":
        outcome = getattr(receipt, "outcome", None)
        return "settled" if outcome == "settled" else "proposed"
    if isinstance(tag, str) and tag in _RECEIPT_DELIVERIES:
        return _RECEIPT_DELIVERIES[tag]
    raise ValueError(f"a claims terminal delivered an unexpected receipt {tag!r}")


def claims_summary(
    changes: Iterable[CoreClaimsChange],
    diagnostics: ClaimsDiagnostics | None,
    *,
    downgraded: int,
    delivery: ClaimsDelivery,
) -> ClaimsRunSummary:
    """A claims terminal's counts: its changes, and the counters its expansion kept.

    ``ClaimsDiagnostics.same`` counts every restatement, re-observed or not;
    each re-observation is also one change, so ``no_op`` is their difference.
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
    return ClaimsRunSummary(
        written=written,
        revised=revised,
        inverse_written=inverse,
        subjects_created=subjects,
        re_observed=observed,
        no_op=max(same - observed, 0),
        retired=retired,
        surfaced=surfaced,
        downgraded=downgraded,
        skipped={kind: count for kind, count in sorted(counts.items()) if count > 0},
        delivery=delivery,
    )


__all__ = [
    "call_summary",
    "claims_delivery",
    "claims_summary",
    "extract_summary",
    "guard_summary",
    "row_count",
    "transform_summary",
]
