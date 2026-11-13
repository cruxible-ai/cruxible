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
from typing import TYPE_CHECKING, Annotated, Final, Literal, NamedTuple, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.provider_operations import ContractLookup, OperationEffect
from cruxible_client.contracts.shapes import (
    ANY_CAPTURE_KIND,
    CaptureType,
    Contract,
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
#: receiver side's. :func:`record_fits` asks it for every ``capture<K>`` both sides
#: name, including those inside Contracts the two sides share by digest (a kind's
#: layout is not covered by a Contract's digest), once per kind per call. It passes
#: ``K`` and the fit rule for one field type and for two row Contracts (each over the
#: same resolvers and memo as the call itself), which is what
#: ``capture_layouts.layout_covers`` takes. The caller wires it over its two
#: declarations of ``K``, the sender side's as producer, and may answer ``None``
#: itself when the two declarations have equal digests:
#: ``lambda kind, type_fits, row_fits: layout_covers(sent[kind], received[kind],
#: type_fits=type_fits, row_fits=row_fits)``. On the request the sender side is the
#: instance's kind and the receiver the operation's copy; on the result the reverse.
#: ``None`` means it covers. A layout whose rules name its own kind (or a kind that
#: names it back) is judged on the assumption that the kind covers; any other gap in
#: the cycle is still reported.
CaptureCovers: TypeAlias = Callable[[str, "TypeFit", "RowFit"], "LayoutGap | None"]

#: The root a :data:`TypeFit` reports a failing path from (``value``, ``value[]``).
_TYPE_FIT_ROOT = "value"

#: How deep kinds may refer to kinds through their layouts within one fit call.
MAX_CAPTURE_KIND_NESTING = 32
_NESTED_TOO_DEEP = f"a capture kind nested more than {MAX_CAPTURE_KIND_NESTING} kinds deep"
_NESTING_ALLOWED = f"at most {MAX_CAPTURE_KIND_NESTING} nested capture kinds"

#: How two Contracts are compared. ``body``: a nested record, whose absent optional
#: fields are absent keys. ``envelope``: the row of a ``record<C>`` or ``result<C>``,
#: whose value carries every row field as a ``field_result``.
_Mode = Literal["body", "envelope"]
_Party = Literal["sender", "receiver"]
_Key = tuple[str, str] | tuple[str, str, str]


class FitUnresolved(LookupError):
    """A Contract pin one side names does not resolve in that side's closure.

    Fit judges two resolvable closures; the closure checks that run before it
    (Contract law, a registration's ``declares``) refuse an unresolved pin first.
    """

    def __init__(self, ref: ContractRef, *, party: _Party) -> None:
        super().__init__(f"the {party}'s pin {ref.spelling} does not resolve")
        self.ref = ref
        self.party = party


class _Seg:
    """One path segment and the path below it. Tails are shared, never copied."""

    __slots__ = ("head", "tail")

    def __init__(self, head: str, tail: _Seg | None = None) -> None:
        self.head = head
        self.tail = tail


class _Fail(NamedTuple):
    """A failure relative to the record it was found in; ``leaf`` ends a layout path."""

    path: _Seg
    problem: FitProblem
    sender: str
    receiver: str
    leaf: str = ""

    def under(self, segment: str | None) -> _Fail:
        return self if segment is None else self._replace(path=_Seg(segment, self.path))

    def rendered_path(self) -> str:
        parts: list[str] = []
        node: _Seg | None = self.path
        while node is not None:
            parts.append(node.head)
            node = node.tail
        if self.leaf:
            parts.append(self.leaf)
        return ".".join(parts)


class _Reject(NamedTuple):
    fail: _Fail


class _Covers(NamedTuple):
    segment: str
    kind: str


class _Same(NamedTuple):
    """Two Contracts with one digest: only the Captures they reach are asked."""

    segment: str
    ref: ContractRef


class _Pair(NamedTuple):
    segment: str | None
    mode: _Mode
    sender: ContractRef
    receiver: ContractRef


_Duty = _Reject | _Covers | _Same | _Pair


class _Active:
    """A pair or kind being judged: its order of entry and the earliest one it assumed."""

    __slots__ = ("key", "number", "low", "mark")

    def __init__(self, key: _Key, number: int, mark: int) -> None:
        self.key = key
        self.number = number
        self.low = number
        self.mark = mark


class _Frame:
    __slots__ = ("item", "duties", "index", "segment")

    def __init__(
        self, item: _Active | None, duties: list[_Duty], segment: str | None = None
    ) -> None:
        self.item = item
        self.duties = duties
        self.index = 0
        self.segment = segment


def _field_order(name: str) -> bytes:
    return name.encode("utf-8")


#: Kinds the walk judges itself; every other pair of types is a leaf the edge rule judges.
_STRUCTURED = frozenset(
    {"capture", "list", "field_result", "contract", "record", "result", "refusal", "variant"}
)


def _reached(field_type: FieldType, path: str, out: list[tuple[str, str | ContractRef]]) -> None:
    """Each named ``capture<K>`` and Contract pin one type reaches, with its path."""

    if isinstance(field_type, CaptureType):
        if field_type.capture_kind != ANY_CAPTURE_KIND:
            out.append((path, field_type.capture_kind))
    elif isinstance(field_type, ListType):
        _reached(field_type.item, f"{path}[]", out)
    elif isinstance(field_type, FieldResultType):
        _reached(field_type.value, path, out)
    elif isinstance(field_type, NestedType | RecordType | ResultType | RefusalType):
        out.append((path, field_type.contract))
    elif isinstance(field_type, VariantType):
        for tag in sorted(field_type.arms, key=_field_order):
            out.append((f"{path}.{tag}", field_type.arms[tag]))


class _Walk:
    """One fit call: both resolvers, the Capture seam, and memos shared by every nested walk.

    Contract pairs are walked with an explicit stack, so the depth of a closure
    never meets the interpreter's recursion limit; only a Capture seam re-enters,
    once per kind. A pair or kind met again while it is being judged is assumed to
    fit; a success that rested on such an assumption is kept only while that
    assumption stands, and every such success is dropped when anything fails.
    """

    def __init__(
        self,
        resolve_sender: ContractLookup,
        resolve_receiver: ContractLookup,
        side: FitSide,
        capture_covers: CaptureCovers,
    ) -> None:
        self.resolvers: dict[_Party, ContractLookup] = {
            "sender": resolve_sender,
            "receiver": resolve_receiver,
        }
        self.side = side
        self.capture_covers = capture_covers
        self.contracts: dict[tuple[_Party, str, int, str | None], Contract] = {}
        self.digests: dict[tuple[_Party, str, int], str] = {}
        #: Per Contract digest: each named CaptureKind it reaches, with its first path.
        self.kinds: dict[str, dict[str, _Seg]] = {}
        self.memo: dict[_Key, _Fail | LayoutGap | None] = {}
        self.provisional: dict[_Key, int] = {}
        self.provisional_log: list[_Key] = []
        self.active: dict[_Key, _Active] = {}
        self.active_numbers: set[int] = set()
        self.chain: list[_Active] = []
        self.settled_low: dict[int, int] = {}
        self.entered = 0
        #: Duties taken from the work stack: the walk's own work, for the cost tests.
        self.steps = 0
        #: Kinds being judged inside one another right now (each one a seam call).
        self.kind_depth = 0

    # -- entry points (record_fits, and the two callables the Capture seam receives)

    def record(self, sender: ContractRef, receiver: ContractRef) -> FitResult:
        fail = self._run([_Pair(None, "body", sender, receiver)])
        if fail is not None:
            return self._unfit(fail)
        same = self._digest(sender, "sender") == self._digest(receiver, "receiver")
        return Fits(fit="identical" if same else "structural")

    def type_fits(self, sender: FieldType, receiver: FieldType) -> FitResult:
        duties: list[_Duty] = []
        self._types(sender, receiver, _TYPE_FIT_ROOT, duties)
        fail = self._run(duties)
        if fail is not None:
            return self._unfit(fail)
        return Fits(fit="identical" if sender == receiver else "structural")

    def _unfit(self, fail: _Fail) -> Unfit:
        return Unfit(
            side=self.side,
            path=fail.rendered_path(),
            problem=fail.problem,
            sender=fail.sender,
            receiver=fail.receiver,
        )

    # -- resolution, cached per side

    def _resolve(self, ref: ContractRef, party: _Party) -> Contract:
        key = (party, ref.name, ref.version, ref.digest)
        found = self.contracts.get(key)
        if found is None:
            found = self.resolvers[party](ref)
            if found is None:
                raise FitUnresolved(ref, party=party)
            self.contracts[key] = found
        return found

    def _digest(self, ref: ContractRef, party: _Party) -> str:
        if ref.digest is not None:
            return ref.digest
        key = (party, ref.name, ref.version)
        digest = self.digests.get(key)
        if digest is None:
            digest = contract_digest(self._resolve(ref, party)).tagged
            self.digests[key] = digest
        return digest

    # -- memo with cycle-aware assumptions

    def _assume(self, low: int) -> None:
        while low not in self.active_numbers:
            low = self.settled_low[low]
        top = self.chain[-1]
        if low < top.low:
            top.low = low

    def _recall(self, key: _Key) -> tuple[bool, _Fail | LayoutGap | None]:
        item = self.active.get(key)
        if item is not None:
            self._assume(item.number)
            return True, None
        if key in self.memo:
            low = self.provisional.get(key)
            if low is not None:
                self._assume(low)
            return True, self.memo[key]
        return False, None

    def _begin(self, key: _Key) -> _Active:
        item = _Active(key, self.entered, len(self.provisional_log))
        self.entered += 1
        self.active[key] = item
        self.active_numbers.add(item.number)
        self.chain.append(item)
        return item

    def _settle(self, item: _Active, result: _Fail | LayoutGap | None) -> None:
        self.chain.pop()
        del self.active[item.key]
        self.active_numbers.discard(item.number)
        self.memo[item.key] = result
        if result is not None:
            # A failure holds under any assumption; a success that assumed may not.
            if self.provisional_log:
                for key in self.provisional_log:
                    if self.provisional.pop(key, None) is not None:
                        del self.memo[key]
                self.provisional_log.clear()
                for active in self.chain:
                    active.mark = 0
            return
        if item.low < item.number:
            self.provisional[item.key] = item.low
            self.provisional_log.append(item.key)
            self.settled_low[item.number] = item.low
            parent = self.chain[-1]
            if item.low < parent.low:
                parent.low = item.low
            return
        for key in self.provisional_log[item.mark :]:
            self.provisional.pop(key, None)
        del self.provisional_log[item.mark :]

    # -- the work stack

    def _run(self, duties: list[_Duty]) -> _Fail | None:
        stack = [_Frame(None, duties)]
        while True:
            frame = stack[-1]
            fail, child = self._advance(frame)
            if child is not None:
                stack.append(child)
                continue
            while True:
                stack.pop()
                if frame.item is not None:
                    self._settle(frame.item, fail)
                if not stack:
                    return fail
                if fail is None:
                    break
                fail = fail.under(frame.segment)
                frame = stack[-1]

    def _advance(self, frame: _Frame) -> tuple[_Fail | None, _Frame | None]:
        while frame.index < len(frame.duties):
            duty = frame.duties[frame.index]
            frame.index += 1
            self.steps += 1
            if isinstance(duty, _Reject):
                return duty.fail, None
            if isinstance(duty, _Covers):
                if self._too_deep(duty.kind):
                    return _nesting_fail(_Seg(duty.segment)), None
                gap = self._kind(duty.kind)
                if gap is not None:
                    return _layout_fail(_Seg(duty.segment), gap), None
                continue
            if isinstance(duty, _Same):
                fail = self._identical(duty.ref)
                if fail is not None:
                    return fail.under(duty.segment), None
                continue
            opened = self._open(duty.mode, duty.sender, duty.receiver)
            if isinstance(opened, _Frame):
                opened.segment = duty.segment
                return None, opened
            if opened is not None:
                return opened.under(duty.segment), None
        return None, None

    def _open(
        self, mode: _Mode, sender: ContractRef, receiver: ContractRef
    ) -> _Frame | _Fail | None:
        if sender.digest is not None and receiver.digest is not None:
            if sender.digest == receiver.digest:
                return self._identical(sender)
            hit, value = self._recall((mode, sender.digest, receiver.digest))
            if hit:
                return cast("_Fail | None", value)
        sent = self._resolve(sender, "sender")
        received = self._resolve(receiver, "receiver")
        sent_digest, received_digest = (
            self._digest(sender, "sender"),
            self._digest(receiver, "receiver"),
        )
        if sent_digest == received_digest:
            return self._identical(sender)
        key: _Key = (mode, sent_digest, received_digest)
        if sender.digest is None or receiver.digest is None:
            hit, value = self._recall(key)
            if hit:
                return cast("_Fail | None", value)
        item = self._begin(key)
        return _Frame(item, self._fields(sent, received, envelope=mode == "envelope"))

    # -- Captures

    def _kind(self, kind: str) -> LayoutGap | None:
        key: _Key = ("kind", kind)
        hit, value = self._recall(key)
        if hit:
            return cast("LayoutGap | None", value)
        item = self._begin(key)
        self.kind_depth += 1
        try:
            gap = self.capture_covers(kind, self.type_fits, self.record)
        finally:
            self.kind_depth -= 1
        self._settle(item, gap)
        return gap

    def _too_deep(self, kind: str) -> bool:
        """Whether judging ``kind`` now would nest more than the bound allows."""

        key: _Key = ("kind", kind)
        return (
            self.kind_depth >= MAX_CAPTURE_KIND_NESTING
            and key not in self.active
            and key not in self.memo
        )

    def _identical(self, ref: ContractRef) -> _Fail | None:
        for kind, path in self._kinds_of(ref).items():
            if self._too_deep(kind):
                return _nesting_fail(path)
            gap = self._kind(kind)
            if gap is not None:
                return _layout_fail(path, gap)
        return None

    def _kinds_of(self, ref: ContractRef) -> dict[str, _Seg]:
        """Each named CaptureKind one Contract reaches, in field order, once per digest."""

        root = self._digest(ref, "sender")
        found = self.kinds.get(root)
        if found is not None:
            return found
        expanding: set[str] = set()
        stack: list[tuple[str, list[tuple[str, str | ContractRef]] | None, ContractRef]] = [
            (root, None, ref)
        ]
        while stack:
            digest, entries, at = stack.pop()
            if digest in self.kinds:
                continue
            if entries is None:
                if digest in expanding:
                    continue
                expanding.add(digest)
                entries = []
                contract = self._resolve(at, "sender")
                for name in sorted(contract.fields, key=_field_order):
                    _reached(contract.fields[name].type, name, entries)
                stack.append((digest, entries, at))
                for _, item in entries:
                    if isinstance(item, ContractRef):
                        nested = self._digest(item, "sender")
                        if nested not in self.kinds and nested not in expanding:
                            self.steps += 1
                            stack.append((nested, None, item))
                continue
            kinds: dict[str, _Seg] = {}
            for path, item in entries:
                if isinstance(item, str):
                    kinds.setdefault(item, _Seg(path))
                    continue
                # A Contract cycle (only unpinned refs can form one) adds nothing new.
                for kind, below in self.kinds.get(self._digest(item, "sender"), {}).items():
                    if kind not in kinds:
                        kinds[kind] = _Seg(path, below)
            self.kinds[digest] = kinds
            expanding.discard(digest)
        return self.kinds[root]

    # -- records and types

    def _fields(self, sent: Contract, received: Contract, *, envelope: bool) -> list[_Duty]:
        duties: list[_Duty] = []
        for name in sorted(set(sent.fields) | set(received.fields), key=_field_order):
            offered, wanted = sent.fields.get(name), received.fields.get(name)
            if wanted is None:
                assert offered is not None
                duties.append(_Reject(_Fail(_Seg(name), "undeclared", offered.spelling, ABSENT)))
                break
            if offered is None:
                if envelope or not wanted.optional:
                    duties.append(_Reject(_Fail(_Seg(name), "required", ABSENT, wanted.spelling)))
                    break
                continue
            if not envelope and offered.optional and not wanted.optional:
                duties.append(
                    _Reject(_Fail(_Seg(name), "optional", offered.spelling, wanted.spelling))
                )
                break
            if self._types(offered.type, wanted.type, name, duties):
                break
        return duties

    def _types(
        self, sender: FieldType, receiver: FieldType, path: str, duties: list[_Duty]
    ) -> bool:
        """Append the duties that decide whether ``sender`` fits ``receiver``; ``True`` once
        a rejection is appended (nothing after it can matter)."""

        if isinstance(sender, CaptureType) and isinstance(receiver, CaptureType):
            if receiver.capture_kind == ANY_CAPTURE_KIND:
                return False
            if sender.capture_kind != receiver.capture_kind:
                return _reject(duties, sender, receiver, path)
            duties.append(_Covers(path, sender.capture_kind))
            return False
        if isinstance(sender, ListType) and isinstance(receiver, ListType):
            if sender.max_items > receiver.max_items:
                return _reject(duties, sender, receiver, path)
            return self._types(sender.item, receiver.item, f"{path}[]", duties)
        if isinstance(sender, FieldResultType) and isinstance(receiver, FieldResultType):
            return self._types(sender.value, receiver.value, path, duties)
        if isinstance(sender, NestedType) and isinstance(receiver, NestedType):
            duties.append(_Pair(path, "body", sender.contract, receiver.contract))
            return False
        if (isinstance(sender, RecordType) and isinstance(receiver, RecordType)) or (
            isinstance(sender, ResultType) and isinstance(receiver, ResultType)
        ):
            duties.append(_Pair(path, "envelope", sender.contract, receiver.contract))
            return False
        if isinstance(sender, RefusalType) and isinstance(receiver, RefusalType):
            # A refusal value carries its Contract's exact pin: the pins must be one.
            sent_digest = self._digest(sender.contract, "sender")
            received_digest = self._digest(receiver.contract, "receiver")
            sent_pin = (sender.contract.name, sender.contract.version, sent_digest)
            if sent_pin != (receiver.contract.name, receiver.contract.version, received_digest):
                sent_text, received_text = render_type(sender), render_type(receiver)
                if sent_text == received_text:
                    sent_text = f"{sent_text} at {sent_digest[:19]}"
                    received_text = f"{received_text} at {received_digest[:19]}"
                duties.append(_Reject(_Fail(_Seg(path), "type", sent_text, received_text)))
                return True
            duties.append(_Same(path, sender.contract))
            return False
        if isinstance(sender, VariantType) and isinstance(receiver, VariantType):
            if not set(sender.arms) <= set(receiver.arms):
                return _reject(duties, sender, receiver, path)
            for tag in sorted(sender.arms, key=_field_order):
                duties.append(_Pair(f"{path}.{tag}", "body", sender.arms[tag], receiver.arms[tag]))
            return False
        if _STRUCTURED.isdisjoint((sender.kind, receiver.kind)) and (
            edge_refusal(sender, receiver) is None
        ):
            return False
        return _reject(duties, sender, receiver, path)


def _reject(duties: list[_Duty], sender: FieldType, receiver: FieldType, path: str) -> bool:
    duties.append(_Reject(_Fail(_Seg(path), "type", render_type(sender), render_type(receiver))))
    return True


def _nesting_fail(path: _Seg) -> _Fail:
    return _Fail(path, "layout", _NESTED_TOO_DEEP, _NESTING_ALLOWED)


def _layout_fail(path: _Seg, gap: LayoutGap) -> _Fail:
    return _Fail(path, "layout", gap.producer, gap.reader, f"layout.{gap.entry}")


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
    fits; a nested Contract fits by this same rule; a variant's arms are a
    subset and each arm fits; a ``field_result<T>`` fits by its value. Wrappers
    follow their wire values: a ``record<C>`` or ``result<C>`` row declares the
    same field names on both sides (optional ones included, since the value
    carries every field) and each field's type fits, optionality aside; a
    ``refusal<C>`` names the same Contract pin (name, version, digest) on both
    sides. ``capture<K>`` fits ``capture<*>``; ``capture<*>`` fits only
    ``capture<*>``; ``capture<K>`` into ``capture<K>`` fits when
    ``capture_covers`` says the sender side's layout of ``K`` covers the
    receiver side's.

    Each side's pins resolve in that side's closure. Two Contracts with equal
    digests are not compared field by field, but every named ``capture<K>``
    they reach is still asked of ``capture_covers``; the result is
    ``identical`` when the two roots have one digest and every such kind
    covers, ``structural`` when they fit otherwise. The first failing path, in
    field-name order, is returned as :class:`Unfit` on ``side``. Within one
    call each (sender digest, receiver digest) pair is walked once, the kinds a
    digest reaches are collected once, and the seam is asked once per kind. An
    unresolved pin raises :class:`FitUnresolved`. Kinds that refer to kinds
    through their layouts more than :data:`MAX_CAPTURE_KIND_NESTING` deep are unfit.
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
    "MAX_CAPTURE_KIND_NESTING",
    "TruncationUnfit",
    "Unfit",
    "binding_undeclared_message",
    "binding_unfit_message",
    "record_fits",
    "render_unfit",
    "slot_ambiguous_message",
    "slot_unfilled_message",
]
