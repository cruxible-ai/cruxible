"""What a fit check returns, and the refusals binding by capability can earn.

Fit is one record-level rule applied both ways: on the request, the slot (its
capability's ``in``) is the sender and the operation's ``in`` the receiver; on
the result, the operation's ``out`` is the sender and the capability's ``out``
the receiver. A check returns :class:`Fits` (``identical`` when the two
Contracts are the same digest, ``structural`` otherwise) or the first failing
path as :class:`Unfit`, with both sides rendered. Binding a slot also needs
equal effects and a truncation promise the slot accepts:
:class:`EffectUnequal` and :class:`TruncationUnfit`.

This module holds the fit function (:func:`record_fits`), what it returns, how
a failure reads, and the refusal codes and messages of binding by capability
(``cruxible.line.*``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Annotated, Final, Literal, NamedTuple, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.provider_operations import ContractLookup, OperationEffect
from cruxible_client.contracts.shapes import (
    ANY_CAPTURE_KIND,
    CaptureType,
    Contract,
    ContractField,
    ContractRef,
    FieldResultType,
    FieldType,
    ListType,
    NestedType,
    RecordType,
    RefusalType,
    ResultType,
    VariantType,
    contract_digest,
    edge_refusal,
    render_type,
)

if TYPE_CHECKING:
    from cruxible_client.contracts.capture_layouts import LayoutGap, RowFit, TypeFit

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


# -- the fit rule --------------------------------------------------------------------------

#: The Capture seam: whether the sender side's layout of one CaptureKind covers the
#: receiver side's. :func:`record_fits` asks it only where both sides name the same
#: ``capture<K>``, passing ``K`` and the fit rule for one field type and for two row
#: Contracts (each over the same resolvers and memo as the call itself), which is
#: what ``capture_layouts.layout_covers`` takes. The caller wires it over its two
#: declarations of ``K``, the sender side's as producer:
#: ``lambda kind, type_fits, row_fits: layout_covers(sent[kind], received[kind],
#: type_fits=type_fits, row_fits=row_fits)``. On the request the sender side is the
#: instance's kind and the receiver the operation's copy; on the result the reverse.
#: ``None`` means it covers.
CaptureCovers: TypeAlias = Callable[[str, "TypeFit", "RowFit"], "LayoutGap | None"]

#: The root a :data:`TypeFit` reports a failing path from (``value``, ``value[]``).
_TYPE_FIT_ROOT = "value"


class FitUnresolved(LookupError):
    """A Contract pin one side names does not resolve in that side's closure.

    Fit judges two resolvable closures; the closure checks that run before it
    (Contract law, a registration's ``declares``) refuse an unresolved pin first.
    """

    def __init__(self, ref: ContractRef, *, party: Literal["sender", "receiver"]) -> None:
        super().__init__(f"the {party}'s pin {ref.spelling} does not resolve")
        self.ref = ref
        self.party = party


class _Fail(NamedTuple):
    """A failing path relative to the record being walked."""

    path: str
    problem: FitProblem
    sender: str
    receiver: str

    def under(self, prefix: str) -> _Fail:
        return self._replace(path=f"{prefix}.{self.path}")


def _field_order(name: str) -> bytes:
    return name.encode("utf-8")


class _Walk:
    """One fit call: both resolvers, the Capture seam, and memos shared by every nested walk."""

    def __init__(
        self,
        resolve_sender: ContractLookup,
        resolve_receiver: ContractLookup,
        side: FitSide,
        capture_covers: CaptureCovers,
    ) -> None:
        self.resolve_sender = resolve_sender
        self.resolve_receiver = resolve_receiver
        self.side = side
        self.capture_covers = capture_covers
        self.records: dict[tuple[str, str], _Fail | None] = {}
        self.walking: set[tuple[str, str]] = set()
        self.layouts: dict[str, LayoutGap | None] = {}

    # -- entry points (record_fits, and the two callables the Capture seam receives)

    def record(self, sender: ContractRef, receiver: ContractRef) -> FitResult:
        if sender.digest is not None and sender.digest == receiver.digest:
            return Fits(fit="identical")
        sent, received, key = self._pair(sender, receiver)
        if key[0] == key[1]:
            return Fits(fit="identical")
        fail = self._walk_pair(sent, received, key)
        return Fits(fit="structural") if fail is None else self._unfit(fail)

    def type_fits(self, sender: FieldType, receiver: FieldType) -> FitResult:
        fail = self._types(sender, receiver, _TYPE_FIT_ROOT)
        if fail is not None:
            return self._unfit(fail)
        return Fits(fit="identical" if sender == receiver else "structural")

    # -- records

    def _unfit(self, fail: _Fail) -> Unfit:
        return Unfit(
            side=self.side,
            path=fail.path,
            problem=fail.problem,
            sender=fail.sender,
            receiver=fail.receiver,
        )

    def _pair(
        self, sender: ContractRef, receiver: ContractRef
    ) -> tuple[Contract, Contract, tuple[str, str]]:
        sent = self.resolve_sender(sender)
        if sent is None:
            raise FitUnresolved(sender, party="sender")
        received = self.resolve_receiver(receiver)
        if received is None:
            raise FitUnresolved(receiver, party="receiver")
        key = (
            sender.digest or contract_digest(sent).tagged,
            receiver.digest or contract_digest(received).tagged,
        )
        return sent, received, key

    def _contract(self, sender: ContractRef, receiver: ContractRef) -> _Fail | None:
        if sender.digest is not None and receiver.digest is not None:
            if sender.digest == receiver.digest:
                return None
            pinned = (sender.digest, receiver.digest)
            if pinned in self.records:
                return self.records[pinned]
        sent, received, key = self._pair(sender, receiver)
        if key[0] == key[1]:
            return None
        return self._walk_pair(sent, received, key)

    def _walk_pair(self, sent: Contract, received: Contract, key: tuple[str, str]) -> _Fail | None:
        if key in self.records:
            return self.records[key]
        if key in self.walking:
            # A pair already being walked higher up fits unless something else fails.
            return None
        self.walking.add(key)
        try:
            fail = self._fields(sent.fields, received.fields)
        finally:
            self.walking.discard(key)
        self.records[key] = fail
        return fail

    def _fields(
        self, sent: dict[str, ContractField], received: dict[str, ContractField]
    ) -> _Fail | None:
        for name in sorted(set(sent) | set(received), key=_field_order):
            offered, wanted = sent.get(name), received.get(name)
            if wanted is None:
                assert offered is not None
                return _Fail(name, "undeclared", offered.spelling, ABSENT)
            if offered is None:
                if not wanted.optional:
                    return _Fail(name, "required", ABSENT, wanted.spelling)
                continue
            if offered.optional and not wanted.optional:
                return _Fail(name, "optional", offered.spelling, wanted.spelling)
            fail = self._types(offered.type, wanted.type, name)
            if fail is not None:
                return fail
        return None

    # -- types

    def _types(self, sender: FieldType, receiver: FieldType, path: str) -> _Fail | None:
        if isinstance(sender, CaptureType) and isinstance(receiver, CaptureType):
            return self._capture(sender, receiver, path)
        if isinstance(sender, ListType) and isinstance(receiver, ListType):
            if sender.max_items > receiver.max_items:
                return self._type_fail(sender, receiver, path)
            return self._types(sender.item, receiver.item, f"{path}[]")
        if isinstance(sender, FieldResultType) and isinstance(receiver, FieldResultType):
            return self._types(sender.value, receiver.value, path)
        if (
            isinstance(sender, NestedType | RecordType | ResultType | RefusalType)
            and isinstance(receiver, NestedType | RecordType | ResultType | RefusalType)
            and sender.kind == receiver.kind
        ):
            fail = self._contract(sender.contract, receiver.contract)
            return None if fail is None else fail.under(path)
        if isinstance(sender, VariantType) and isinstance(receiver, VariantType):
            if not set(sender.arms) <= set(receiver.arms):
                return self._type_fail(sender, receiver, path)
            for tag in sorted(sender.arms, key=_field_order):
                fail = self._contract(sender.arms[tag], receiver.arms[tag])
                if fail is not None:
                    return fail.under(f"{path}.{tag}")
            return None
        if _STRUCTURED.isdisjoint((sender.kind, receiver.kind)) and (
            edge_refusal(sender, receiver) is None
        ):
            return None
        return self._type_fail(sender, receiver, path)

    def _capture(self, sender: CaptureType, receiver: CaptureType, path: str) -> _Fail | None:
        if receiver.capture_kind == ANY_CAPTURE_KIND:
            return None
        if sender.capture_kind != receiver.capture_kind:
            return self._type_fail(sender, receiver, path)
        kind = sender.capture_kind
        if kind not in self.layouts:
            self.layouts[kind] = self.capture_covers(kind, self.type_fits, self.record)
        gap = self.layouts[kind]
        if gap is None:
            return None
        return _Fail(f"{path}.layout.{gap.entry}", "layout", gap.producer, gap.reader)

    @staticmethod
    def _type_fail(sender: FieldType, receiver: FieldType, path: str) -> _Fail:
        return _Fail(path, "type", render_type(sender), render_type(receiver))


#: Kinds the walk judges itself; every other pair of types is a leaf the edge rule judges.
_STRUCTURED = frozenset(
    {"capture", "list", "field_result", "contract", "record", "result", "refusal", "variant"}
)


def record_fits(
    sender: ContractRef,
    receiver: ContractRef,
    resolve_sender: ContractLookup,
    resolve_receiver: ContractLookup,
    *,
    side: FitSide,
    capture_covers: CaptureCovers,
) -> FitResult:
    """Whether the ``sender`` record fits the ``receiver`` record: the one rule of binding.

    Every sender field exists in the receiver and its type fits; a field
    optional in the sender is optional in the receiver; every receiver field
    the sender lacks is optional. Types fit by the edge rule's refinements
    (ranges and lengths within, a pattern equal or absent on the receiver, enum
    members a subset, Subject kinds a subset; everything else identical), and
    structurally where they wrap others: a list's bound is within and its item
    fits; a nested Contract, a ``record<C>``, ``result<C>`` or ``refusal<C>``
    fits by this same rule; a variant's arms are a subset and each arm fits; a
    ``field_result<T>`` fits by its value. ``capture<K>`` fits ``capture<*>``;
    ``capture<*>`` fits only ``capture<*>``; ``capture<K>`` into ``capture<K>``
    fits when ``capture_covers`` says the sender side's layout of ``K`` covers
    the receiver side's.

    Each side's pins resolve in that side's closure. Two pins with equal digests
    fit without being read (``identical``); otherwise the first failing path, in
    field-name order, is returned as :class:`Unfit` on ``side``. Within one call
    each (sender digest, receiver digest) pair is walked once and the seam is
    asked once per kind. An unresolved pin raises :class:`FitUnresolved`.
    """

    return _Walk(resolve_sender, resolve_receiver, side, capture_covers).record(sender, receiver)


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
    "CaptureCovers",
    "EffectUnequal",
    "FitKind",
    "FitProblem",
    "FitResult",
    "FitSide",
    "FitUnresolved",
    "Fits",
    "LineBindingRefusalCode",
    "TruncationUnfit",
    "Unfit",
    "binding_undeclared_message",
    "binding_unfit_message",
    "record_fits",
    "render_unfit",
    "slot_ambiguous_message",
    "slot_unfilled_message",
]
