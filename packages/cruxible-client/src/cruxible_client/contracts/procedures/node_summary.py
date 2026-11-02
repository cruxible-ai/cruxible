"""What flowed through one Procedure node: a typed, constant-size run summary.

Each ``node_fired`` record of a node that succeeded carries one summary, by the
node's kind, so a run view can say "1,247 records -> 12 watched -> 3 changed"
and which way a guard went without reading the Procedure or any body. A
summary holds counts, booleans, Capture digests and reason codes only: never
values, rows or samples. Every list in it is bounded, so its bytes are bounded
whatever the run read.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from cruxible_client.contracts.claim_evidence import FindingKind
from cruxible_client.contracts.procedures.nodes import TransformKind
from cruxible_client.contracts.shapes import ComparisonOp

#: Capture digests a call's summary names, at most; ``captures`` counts them all.
NODE_SUMMARY_CAPTURE_DIGESTS_MAX = 8
#: Guard comparisons a guard's summary keeps, at most.
NODE_SUMMARY_GUARD_OPERANDS_MAX = 8
#: Every integer a summary carries is within this magnitude (exact in JSON
#: readers that use doubles); a source integer past it is left out, marked.
NODE_SUMMARY_INT_MAX = 2**53 - 1
#: Code points of authored text a summary carries (a ``count_at`` path, a halt
#: reason), at most; longer text is cut, marked.
NODE_SUMMARY_TEXT_MAX = 64
#: Canonical bytes of one summary, at most (the payload byte bound per node).
#: Every field above is bounded, so each kind's worst case is below this.
NODE_SUMMARY_MAX_BYTES = 2048

#: A count: a nonnegative integer within the summary bound.
SummaryCount: TypeAlias = Annotated[int, Field(ge=0, le=NODE_SUMMARY_INT_MAX)]
#: A signed integer within the summary bound.
SummaryInt: TypeAlias = Annotated[int, Field(ge=-NODE_SUMMARY_INT_MAX, le=NODE_SUMMARY_INT_MAX)]
#: Authored text, cut to the summary bound.
SummaryText: TypeAlias = Annotated[str, Field(max_length=NODE_SUMMARY_TEXT_MAX)]
#: A Capture digest.
CaptureDigest: TypeAlias = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class _NodeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _omit_none(value: object) -> bool:
    return value is None


class ExtractCompleteWhen(_NodeSummary):
    """An extract's declared count check, bounded for a summary.

    ``count_at`` is the declared path, cut to ``NODE_SUMMARY_TEXT_MAX`` code
    points (``count_at_truncated`` then says so). ``expected`` is absent when
    the source enumerated no count; a count past ``NODE_SUMMARY_INT_MAX`` is
    left out and ``out_of_range`` says so.
    """

    count_at: SummaryText
    count_at_truncated: bool = Field(default=False, exclude_if=lambda value: not value)
    expected: SummaryCount | None = Field(default=None, exclude_if=_omit_none)
    observed: SummaryCount | None = Field(default=None, exclude_if=_omit_none)
    out_of_range: bool = Field(default=False, exclude_if=lambda value: not value)
    held: bool


class ExtractRunSummary(_NodeSummary):
    """One extract: the records it read, and what its completeness rests on.

    ``records`` counts every record read, ``invalid_records`` and
    ``duplicates`` those among them that were invalid or a refused duplicate;
    ``invalid_fields`` counts the invalid field results of valid records.
    ``complete_when`` is the declared count check's result, when one is declared.
    """

    kind: Literal["extract"] = "extract"
    records: SummaryCount
    invalid_records: SummaryCount
    duplicates: SummaryCount
    invalid_fields: SummaryCount
    complete: bool
    complete_when: ExtractCompleteWhen | None = Field(default=None, exclude_if=_omit_none)


class TransformRunSummary(_NodeSummary):
    """One closed transform: the records it read and the records it produced.

    ``rows_in`` sums every records input (a join's left and right, a union's
    parts); ``rows_out`` is absent for a transform that produces one value
    (``aggregate``, ``derive``), and ``rows_in`` for one that reads none.
    """

    kind: Literal["transform"] = "transform"
    transform_kind: TransformKind
    rows_in: SummaryCount | None = Field(default=None, exclude_if=_omit_none)
    rows_out: SummaryCount | None = Field(default=None, exclude_if=_omit_none)


class ReadRunSummary(_NodeSummary):
    """One accepted-state read: the rows it admitted."""

    kind: Literal["read"] = "read"
    rows: SummaryCount


class CallRunSummary(_NodeSummary):
    """One call: how many Captures it made or was admitted, and the first of them."""

    kind: Literal["call"] = "call"
    captures: SummaryCount
    capture_digests: tuple[CaptureDigest, ...] = Field(
        default=(), max_length=NODE_SUMMARY_CAPTURE_DIGESTS_MAX, exclude_if=lambda value: not value
    )


#: A guard operand the summary may carry: a bounded number or a boolean, never a string.
GuardOperandValue: TypeAlias = bool | SummaryInt
#: Every operator a guard predicate compares with.
GuardOperator: TypeAlias = ComparisonOp | Literal["in", "not_in", "present", "absent"]


class GuardOperand(_NodeSummary):
    """One comparison a guard evaluated: its operator, and its numeric or bool operands.

    An operand that is anything else (a string, a list, a record) is left out;
    so is the whole comparison when neither operand is a number or a boolean.
    A number past ``NODE_SUMMARY_INT_MAX`` is left out too, and ``out_of_range``
    says so. ``present`` and ``absent`` carry whether the reference resolved as
    ``left``.
    """

    operator: GuardOperator
    left: GuardOperandValue | None = Field(default=None, exclude_if=_omit_none)
    right: GuardOperandValue | None = Field(default=None, exclude_if=_omit_none)
    out_of_range: bool = Field(default=False, exclude_if=lambda value: not value)


class GuardRunSummary(_NodeSummary):
    """One guard: the branch it took, and the numeric and bool operands it compared."""

    kind: Literal["guard"] = "guard"
    branch: Literal["then", "else"]
    operands: tuple[GuardOperand, ...] = Field(
        default=(), max_length=NODE_SUMMARY_GUARD_OPERANDS_MAX, exclude_if=lambda value: not value
    )
    #: Comparisons with a numeric or bool operand past the bound.
    operands_truncated: SummaryCount = Field(default=0, exclude_if=lambda value: value == 0)


#: How a claims terminal's change was delivered: ``settled`` under its
#: mandate, ``proposed`` for review, ``still_current`` when it equals its
#: Line's open proposal, ``unchanged`` when every item restated accepted
#: state, ``empty`` when it had no item to emit.
#: ``delivered`` when the receipt does not say which.
ClaimsDelivery: TypeAlias = Literal[
    "settled", "proposed", "still_current", "unchanged", "empty", "delivered"
]


class ClaimsRunSummary(_NodeSummary):
    """One claims terminal: what it wrote, kept, retired, surfaced and skipped.

    ``written`` and ``revised`` count new and revising Claims, ``inverse_written``
    the declared-inverse Claims (written or revising) apart from them, and
    ``subjects_created`` the Subjects created by key. ``re_observed`` counts
    held Claims a new Capture re-confirmed, ``no_op`` restatements that changed
    nothing at all. ``downgraded`` counts values written as the ClaimType's
    unknown member over incomplete input. ``surfaced`` counts held Claims a
    source no longer contains that were reported instead of retired;
    ``skipped`` counts the other findings, by kind (an item not written, or an
    incomplete source). ``delivery`` is how the change landed.
    """

    kind: Literal["claims"] = "claims"
    written: SummaryCount
    revised: SummaryCount
    inverse_written: SummaryCount
    subjects_created: SummaryCount
    re_observed: SummaryCount
    no_op: SummaryCount
    retired: SummaryCount
    surfaced: SummaryCount
    downgraded: SummaryCount
    skipped: dict[FindingKind, SummaryCount] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    delivery: ClaimsDelivery


class EgressRunSummary(_NodeSummary):
    """One ``emit_capture`` or ``post_inbox`` terminal: the items it emitted.

    ``unposted`` counts the items a post's inbox cap left out.
    """

    kind: Literal["emit_capture", "post_inbox"]
    emitted: SummaryCount
    unposted: SummaryCount = Field(default=0, exclude_if=lambda value: value == 0)


class HaltRunSummary(_NodeSummary):
    """One halt: the reason code it declares, when it declares one, cut to the text bound."""

    kind: Literal["halt"] = "halt"
    reason: SummaryText | None = Field(default=None, exclude_if=_omit_none)
    reason_truncated: bool = Field(default=False, exclude_if=lambda value: not value)


class RepeatRunSummary(_NodeSummary):
    """One repeat: the attempts it used."""

    kind: Literal["repeat"] = "repeat"
    attempts: SummaryCount


NodeRunSummary: TypeAlias = Annotated[
    ExtractRunSummary
    | TransformRunSummary
    | ReadRunSummary
    | CallRunSummary
    | GuardRunSummary
    | ClaimsRunSummary
    | EgressRunSummary
    | HaltRunSummary
    | RepeatRunSummary,
    Field(discriminator="kind"),
]

NODE_RUN_SUMMARY_ADAPTER: TypeAdapter[NodeRunSummary] = TypeAdapter(NodeRunSummary)


__all__ = [
    "NODE_RUN_SUMMARY_ADAPTER",
    "NODE_SUMMARY_CAPTURE_DIGESTS_MAX",
    "NODE_SUMMARY_GUARD_OPERANDS_MAX",
    "NODE_SUMMARY_INT_MAX",
    "NODE_SUMMARY_MAX_BYTES",
    "NODE_SUMMARY_TEXT_MAX",
    "CallRunSummary",
    "CaptureDigest",
    "ClaimsDelivery",
    "ClaimsRunSummary",
    "EgressRunSummary",
    "ExtractCompleteWhen",
    "ExtractRunSummary",
    "GuardOperand",
    "GuardOperandValue",
    "GuardOperator",
    "GuardRunSummary",
    "HaltRunSummary",
    "NodeRunSummary",
    "ReadRunSummary",
    "RepeatRunSummary",
    "SummaryCount",
    "SummaryInt",
    "SummaryText",
    "TransformRunSummary",
]
