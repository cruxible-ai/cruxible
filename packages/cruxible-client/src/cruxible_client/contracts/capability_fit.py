"""What a fit check returns, and the refusals binding by capability can earn.

Fit is one record-level rule applied both ways: on the request, the slot (its
capability's ``in``) is the sender and the operation's ``in`` the receiver; on
the result, the operation's ``out`` is the sender and the capability's ``out``
the receiver. A check returns :class:`Fits` (``identical`` when the two
Contracts are the same digest, ``structural`` otherwise) or the first failing
path as :class:`Unfit`, with both sides rendered. Binding a slot also needs
equal effects and a truncation promise the slot accepts:
:class:`EffectUnequal` and :class:`TruncationUnfit`.

The fit function itself lives with the binding rule; this module fixes what it
returns, how a failure reads, and the refusal codes and messages of binding by
capability (``cruxible.line.*``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.provider_operations import OperationEffect

#: How a fitting pair fits: the same Contract digests, or by shape.
FitKind = Literal["identical", "structural"]
#: Which record the failing path is in: what the slot sends, or what it gets back.
FitSide = Literal["request", "result"]
#: Which clause of the fit rule failed.
#: ``type``: the field's types do not fit; ``undeclared``: the receiver lacks a
#: field the sender may carry; ``optional``: the sender may omit a field the
#: receiver requires; ``required``: the receiver requires a field the sender
#: lacks; ``layout``: a Capture field's layouts do not cover.
FitProblem = Literal["type", "undeclared", "optional", "required", "layout"]
#: How an unfit side renders a field it does not have.
ABSENT = "absent"


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class Fits(_Shape):
    """The pair fits; ``fit`` is what run and provider receipts record."""

    verdict: Literal["fits"] = "fits"
    fit: FitKind


class Unfit(_Shape):
    """The first path where the sender does not fit the receiver.

    ``path`` is dotted from the record root (``time_range``, ``items[].name``,
    ``document.layout.records.affected``); ``sender`` and ``receiver`` are each
    side's rendered type there, or ``absent``.
    """

    verdict: Literal["unfit"] = "unfit"
    side: FitSide
    path: str = Field(min_length=1)
    problem: FitProblem
    sender: str
    receiver: str


class EffectUnequal(_Shape):
    """The operation's effect is not the capability's."""

    verdict: Literal["effect_unequal"] = "effect_unequal"
    capability: OperationEffect
    operation: OperationEffect


class TruncationUnfit(_Shape):
    """The operation may truncate its listing; the slot treats its result as complete."""

    verdict: Literal["truncation_unfit"] = "truncation_unfit"


#: The result of the record-level fit rule.
FitResult: TypeAlias = Annotated[Fits | Unfit, Field(discriminator="verdict")]
#: The result of checking one operation against one slot.
BindingFit: TypeAlias = Annotated[
    Fits | Unfit | EffectUnequal | TruncationUnfit, Field(discriminator="verdict")
]
#: A reason one operation that declares a slot's capability does not fit it.
BindingUnfitReason: TypeAlias = Unfit | EffectUnequal | TruncationUnfit

#: The clause each (side, problem) reads as; ``{sender}`` and ``{receiver}`` are rendered types.
UNFIT_CLAUSES: dict[tuple[FitSide, FitProblem], str] = {
    ("request", "type"): "the slot may send {sender}; the operation accepts {receiver}",
    ("request", "undeclared"): "the slot may send {sender}; the operation does not accept it",
    ("request", "optional"): "the slot may omit it; the operation requires {receiver}",
    ("request", "required"): "the operation requires {receiver}; the slot never sends it",
    ("request", "layout"): (
        "the instance's layout declares {sender}; the operation's copy expects {receiver}"
    ),
    ("result", "type"): "the operation returns {sender}; the capability expects {receiver}",
    ("result", "undeclared"): (
        "the operation returns {sender}; the capability does not declare it"
    ),
    ("result", "optional"): "the operation may omit it; the capability requires {receiver}",
    ("result", "required"): "the capability requires {receiver}; the operation never returns it",
    ("result", "layout"): (
        "the operation's copy declares {sender}; the instance's layout declares {receiver}"
    ),
}


def render_unfit(reason: BindingUnfitReason, *, slot: str) -> str:
    """One unfit reason as plain text, naming the first failing path and both sides."""

    if isinstance(reason, EffectUnequal):
        return f"effect: the capability is {reason.capability}; the operation is {reason.operation}"
    if isinstance(reason, TruncationUnfit):
        return (
            f"this operation may return a truncated listing; slot `{slot}` treats its result "
            "as complete"
        )
    clause = UNFIT_CLAUSES[(reason.side, reason.problem)].format(
        sender=reason.sender, receiver=reason.receiver
    )
    return f"{reason.side}.{reason.path}: {clause}"


# -- refusals of binding by capability -----------------------------------------------------

SLOT_UNFILLED: Final = "cruxible.line.slot_unfilled"
SLOT_AMBIGUOUS: Final = "cruxible.line.slot_ambiguous"
BINDING_UNFIT: Final = "cruxible.line.binding_unfit"
BINDING_UNDECLARED: Final = "cruxible.line.binding_undeclared"

#: Every code binding a slot by capability can refuse with.
LineBindingRefusalCode = Literal[
    "cruxible.line.slot_unfilled",
    "cruxible.line.slot_ambiguous",
    "cruxible.line.binding_unfit",
    "cruxible.line.binding_undeclared",
]


def _needs(capability: str, effect: OperationEffect) -> str:
    return f"{capability}, {effect}"


def slot_unfilled_message(
    slot: str,
    capability: str,
    effect: OperationEffect,
    *,
    unfit: Sequence[tuple[str, BindingUnfitReason]] = (),
    within: str | None = None,
) -> str:
    """``cruxible.line.slot_unfilled``: no live operation serves the slot and fits it.

    ``capability`` is the pin's spelling (``name@N``); ``unfit`` lists the
    operations (``build#operation``) that declare the capability but do not fit,
    each with its first failing reason; the caller bounds the list.
    """

    scope = f" in {within}" if within is not None else ""
    message = f"No live operation{scope} serves slot `{slot}` ({_needs(capability, effect)})."
    if unfit:
        message += " Declares it but does not fit: " + " ".join(
            f"`{operation}`: {render_unfit(reason, slot=slot)}." for operation, reason in unfit
        )
    return message


def slot_ambiguous_message(
    slot: str,
    capability: str,
    effect: OperationEffect,
    candidates: Sequence[str],
    *,
    more: bool = False,
    within: str | None = None,
) -> str:
    """``cruxible.line.slot_ambiguous``: several live operations serve and fit the slot."""

    scope = f" in {within}" if within is not None else ""
    return (
        f"Several live operations{scope} serve slot `{slot}` ({_needs(capability, effect)}): "
        + ", ".join(f"`{item}`" for item in candidates)
        + (", ..." if more else "")
        + "."
    )


def binding_unfit_message(
    slot: str, capability: str, operation: str, reason: BindingUnfitReason
) -> str:
    """``cruxible.line.binding_unfit``: the named operation declares the capability but does
    not fit the slot."""

    return (
        f"Slot `{slot}` needs {capability}. `{operation}` declares it but does not fit: "
        f"{render_unfit(reason, slot=slot)}."
    )


def binding_undeclared_message(
    slot: str, capability: str, operation: str, serves: Sequence[str]
) -> str:
    """``cruxible.line.binding_undeclared``: the named operation does not serve the slot's
    capability. ``capability`` is the pin's spelling; ``serves`` the operation's names."""

    name = capability.partition("@")[0]
    served = ", ".join(serves) if serves else "nothing"
    return f"Slot `{slot}` needs {capability}. `{operation}` serves {served}, not {name}."


__all__ = [
    "ABSENT",
    "BINDING_UNDECLARED",
    "BINDING_UNFIT",
    "SLOT_AMBIGUOUS",
    "SLOT_UNFILLED",
    "UNFIT_CLAUSES",
    "BindingFit",
    "BindingUnfitReason",
    "EffectUnequal",
    "FitKind",
    "FitProblem",
    "FitResult",
    "FitSide",
    "Fits",
    "LineBindingRefusalCode",
    "TruncationUnfit",
    "Unfit",
    "binding_undeclared_message",
    "binding_unfit_message",
    "render_unfit",
    "slot_ambiguous_message",
    "slot_unfilled_message",
]
