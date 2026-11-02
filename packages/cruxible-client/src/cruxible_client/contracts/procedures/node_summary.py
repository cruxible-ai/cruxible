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

from cruxible_client.contracts.claim_evidence import CompleteWhenResult, FindingKind

#: Capture digests a call's summary names, at most; ``captures`` counts them all.
NODE_SUMMARY_CAPTURE_DIGESTS_MAX = 8
#: Guard comparisons a guard's summary keeps, at most.
NODE_SUMMARY_GUARD_OPERANDS_MAX = 8
#: Canonical bytes of one summary, at most (the payload byte bound per node).
NODE_SUMMARY_MAX_BYTES = 2048


class _NodeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _omit_none(value: object) -> bool:
    return value is None


class ExtractRunSummary(_NodeSummary):
    """One extract: the records it read, and what its completeness rests on.

    ``records`` counts every record read, ``invalid_records`` and
    ``duplicates`` those among them that were invalid or a refused duplicate;
    ``invalid_fields`` counts the invalid field results of valid records.
    ``complete_when`` is the declared count check's result, when one is declared.
    """

    kind: Literal["extract"] = "extract"
    records: int = Field(ge=0)
    invalid_records: int = Field(ge=0)
    duplicates: int = Field(ge=0)
    invalid_fields: int = Field(ge=0)
    complete: bool
    complete_when: CompleteWhenResult | None = Field(default=None, exclude_if=_omit_none)


class TransformRunSummary(_NodeSummary):
    """One closed transform: the records it read and the records it produced.

    ``rows_in`` sums every records input (a join's left and right, a union's
    parts); ``rows_out`` is absent for a transform that produces one value
    (``aggregate``, ``derive``), and ``rows_in`` for one that reads none.
    """

    kind: Literal["transform"] = "transform"
    transform_kind: str
    rows_in: int | None = Field(default=None, ge=0, exclude_if=_omit_none)
    rows_out: int | None = Field(default=None, ge=0, exclude_if=_omit_none)


class ReadRunSummary(_NodeSummary):
    """One accepted-state read: the rows it admitted."""

    kind: Literal["read"] = "read"
    rows: int = Field(ge=0)


class CallRunSummary(_NodeSummary):
    """One call: how many Captures it made or was admitted, and the first of them."""

    kind: Literal["call"] = "call"
    captures: int = Field(ge=0)
    capture_digests: tuple[str, ...] = Field(
        default=(), max_length=NODE_SUMMARY_CAPTURE_DIGESTS_MAX, exclude_if=lambda value: not value
    )


#: A guard operand the summary may carry: a number or a boolean, never a string.
GuardOperandValue: TypeAlias = bool | int


class GuardOperand(_NodeSummary):
    """One comparison a guard evaluated: its operator, and its numeric or bool operands.

    An operand that is anything else (a string, a list, a record) is left out;
    so is the whole comparison when neither operand is a number or a boolean.
    ``present`` and ``absent`` carry whether the reference resolved as ``left``.
    """

    operator: str
    left: GuardOperandValue | None = Field(default=None, exclude_if=_omit_none)
    right: GuardOperandValue | None = Field(default=None, exclude_if=_omit_none)


class GuardRunSummary(_NodeSummary):
    """One guard: the branch it took, and the numeric and bool operands it compared."""

    kind: Literal["guard"] = "guard"
    branch: Literal["then", "else"]
    operands: tuple[GuardOperand, ...] = Field(
        default=(), max_length=NODE_SUMMARY_GUARD_OPERANDS_MAX, exclude_if=lambda value: not value
    )
    #: Comparisons with a numeric or bool operand past the bound.
    operands_truncated: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)


#: How a claims terminal's change was delivered: ``settled`` under its
#: mandate, ``proposed`` for review, ``still_current`` when it equals its
#: Line's open proposal, ``unchanged`` when every item restated accepted
#: state, ``empty`` when it had no item to emit.
ClaimsDelivery: TypeAlias = Literal["settled", "proposed", "still_current", "unchanged", "empty"]


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
    written: int = Field(ge=0)
    revised: int = Field(ge=0)
    inverse_written: int = Field(ge=0)
    subjects_created: int = Field(ge=0)
    re_observed: int = Field(ge=0)
    no_op: int = Field(ge=0)
    retired: int = Field(ge=0)
    surfaced: int = Field(ge=0)
    downgraded: int = Field(ge=0)
    skipped: dict[FindingKind, int] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    delivery: ClaimsDelivery


class EgressRunSummary(_NodeSummary):
    """One ``emit_capture`` or ``post_inbox`` terminal: the items it emitted.

    ``unposted`` counts the items a post's inbox cap left out.
    """

    kind: Literal["emit_capture", "post_inbox"]
    emitted: int = Field(ge=0)
    unposted: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)


class HaltRunSummary(_NodeSummary):
    """One halt: the reason code it declares, when it declares one."""

    kind: Literal["halt"] = "halt"
    reason: str | None = Field(default=None, max_length=128, exclude_if=_omit_none)


class RepeatRunSummary(_NodeSummary):
    """One repeat: the attempts it used."""

    kind: Literal["repeat"] = "repeat"
    attempts: int = Field(ge=1)


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
    "NODE_SUMMARY_MAX_BYTES",
    "CallRunSummary",
    "ClaimsDelivery",
    "ClaimsRunSummary",
    "EgressRunSummary",
    "ExtractRunSummary",
    "GuardOperand",
    "GuardOperandValue",
    "GuardRunSummary",
    "HaltRunSummary",
    "NodeRunSummary",
    "ReadRunSummary",
    "RepeatRunSummary",
    "TransformRunSummary",
]
