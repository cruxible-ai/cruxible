"""The record-level fit rule: every row of the rule, Captures, recursion, the fast path, order."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

import pytest

from cruxible_client.contracts.capability_fit import (
    ABSENT,
    CaptureCovers,
    FitResult,
    Fits,
    FitSide,
    FitUnresolved,
    Unfit,
    record_fits,
    render_unfit,
)
from cruxible_client.contracts.capture_layouts import (
    DeclaredCaptureKind,
    LayoutGap,
    RowFit,
    TypeFit,
    layout_covers,
)
from cruxible_client.contracts.provider_operations import ContractLookup, closure_lookup
from cruxible_client.contracts.shapes import (
    Contract,
    ContractRef,
    contract_digest,
    parse_type,
    render_type,
    validate_contract,
)

SIDES: tuple[FitSide, ...] = ("request", "result")
KIND = "acme.hits"


def _contract(name: str, fields: dict[str, Any], version: int = 1) -> Contract:
    return validate_contract({"name": name, "version": version, "fields": fields})


def _ref(contract: Contract) -> ContractRef:
    return ContractRef(name=contract.name, version=contract.version)


def _pinned(contract: Contract) -> ContractRef:
    return ContractRef(
        name=contract.name, version=contract.version, digest=contract_digest(contract).tagged
    )


def _nested(contract: Contract) -> dict[str, Any]:
    return {"type": {"kind": "contract", "contract": _pinned(contract).model_dump(mode="json")}}


def _no_layouts(kind: str, type_fits: TypeFit, row_fits: RowFit) -> LayoutGap | None:
    raise AssertionError(f"the Capture seam was asked about {kind}")


def _fit(
    sent: dict[str, Any],
    received: dict[str, Any],
    *,
    side: FitSide = "request",
    sender_closure: Iterable[Contract] = (),
    receiver_closure: Iterable[Contract] = (),
    covers: CaptureCovers = _no_layouts,
) -> FitResult:
    """Fit a sender record against a receiver record, each resolved in its own closure."""

    sender = _contract("acme.sent", sent)
    receiver = _contract("acme.received", received)
    return record_fits(
        _ref(sender),
        _ref(receiver),
        closure_lookup((sender, *sender_closure)),
        closure_lookup((receiver, *receiver_closure)),
        side=side,
        capture_covers=covers,
    )


def _type_unfit(side: FitSide, path: str, sender: str, receiver: str) -> Unfit:
    return Unfit(side=side, path=path, problem="type", sender=sender, receiver=receiver)


STRUCTURAL = Fits(fit="structural")


# -- leaf types: every row of the table, both directions ------------------------------------

#: (case, sender type, receiver type, sender fits receiver, receiver fits sender)
LEAF_ROWS: list[tuple[str, str, str, bool, bool]] = [
    ("string_length_within", "string{max_len: 10}", "string{max_len: 20}", True, False),
    ("string_min_within", "string{min_len: 2}", "string{min_len: 1}", True, False),
    ("string_unbounded_receiver", "string{max_len: 10}", "string", True, False),
    ("string_pattern_absent_on_receiver", 'string{pattern: "^a"}', "string", True, False),
    ("string_pattern_equal", 'string{pattern: "^a"}', 'string{pattern: "^a"}', True, True),
    ("string_pattern_differs", 'string{pattern: "^a"}', 'string{pattern: "^b"}', False, False),
    ("int_range_within", "int{0..10}", "int{0..100}", True, False),
    ("int_unbounded_receiver", "int{0..10}", "int", True, False),
    ("decimal_range_within", "decimal{0..1}", "decimal{0..2}", True, False),
    ("number_range_within", "number{0..1.5}", "number{0..2}", True, False),
    ("int_is_not_number", "int", "number", False, False),
    ("bool_identical", "bool", "bool", True, True),
    ("date_identical", "date", "date", True, True),
    ("datetime_identical", "datetime", "datetime", True, True),
    ("duration_identical", "duration", "duration", True, True),
    ("date_is_not_datetime", "date", "datetime", False, False),
    ("bool_is_not_string", "bool", "string", False, False),
    ("enum_subset", 'enum["a"]', 'enum["a", "b"]', True, False),
    ("enum_into_string", 'enum["ab", "cd"]', "string{max_len: 2}", True, False),
    ("enum_into_string_too_long", 'enum["abc"]', "string{max_len: 2}", False, False),
    ("enum_of_same", "enum_of acme.thing.status", "enum_of acme.thing.status", True, True),
    ("enum_of_other", "enum_of acme.thing.status", "enum_of acme.thing.state", False, False),
    ("key_same", "key<acme.thing>", "key<acme.thing>", True, True),
    ("key_other", "key<acme.thing>", "key<acme.other>", False, False),
    ("key_part_same", "key_part<acme.thing:id>", "key_part<acme.thing:id>", True, True),
    ("key_part_other", "key_part<acme.thing:id>", "key_part<acme.thing:slug>", False, False),
    ("key_is_not_key_part", "key<acme.thing>", "key_part<acme.thing:id>", False, False),
    ("subject_subset", "subject<acme.a>", "subject<acme.a | acme.b>", True, False),
    ("list_bound_within", "list<int>{max_items: 10}", "list<int>{max_items: 20}", True, False),
    ("list_is_not_item", "list<int>{max_items: 10}", "int", False, False),
]


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize(
    ("sender", "receiver", "forward", "reverse"),
    [pytest.param(*row[1:], id=row[0]) for row in LEAF_ROWS],
)
def test_leaf_rows_both_directions(
    side: FitSide, sender: str, receiver: str, forward: bool, reverse: bool
) -> None:
    for sent, received, fits in ((sender, receiver, forward), (receiver, sender, reverse)):
        result = _fit({"value": sent}, {"value": received}, side=side)
        expected = (
            STRUCTURAL
            if fits
            else _type_unfit(
                side, "value", render_type(parse_type(sent)), render_type(parse_type(received))
            )
        )
        assert result == expected, (sent, received)


# -- the record rule: fields present, absent and optional ------------------------------------


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize(
    ("sent", "received", "expected"),
    [
        pytest.param({"a": "int"}, {"a": "int"}, None, id="same_fields"),
        pytest.param({}, {}, None, id="empty_records"),
        # Request: an extra request field the slot may send. Result: an extra result field
        # on the operation. Both are a sender field the receiver does not declare.
        pytest.param(
            {"a": "int", "b": "string?"},
            {"a": "int"},
            ("b", "undeclared", "string?", ABSENT),
            id="sender_field_undeclared",
        ),
        # Request: a slot-optional field must be operation-optional. Result: the operation
        # may omit a field the capability requires.
        pytest.param(
            {"a": "int?"},
            {"a": "int"},
            ("a", "optional", "int?", "int"),
            id="sender_optional_receiver_required",
        ),
        pytest.param({"a": "int"}, {"a": "int?"}, None, id="sender_required_receiver_optional"),
        # Request: an optional request field the slot never sends. Result: a result field
        # the capability marks optional and the operation leaves out.
        pytest.param({"a": "int"}, {"a": "int", "b": "int?"}, None, id="receiver_extra_optional"),
        pytest.param(
            {"a": "int"},
            {"a": "int", "b": "int"},
            ("b", "required", ABSENT, "int"),
            id="receiver_extra_required",
        ),
    ],
)
def test_record_rule(
    side: FitSide,
    sent: dict[str, str],
    received: dict[str, str],
    expected: tuple[str, str, str, str] | None,
) -> None:
    result = _fit(sent, received, side=side)
    if expected is None:
        assert result == STRUCTURAL
    else:
        path, problem, sender, receiver = expected
        assert result == Unfit(
            side=side, path=path, problem=problem, sender=sender, receiver=receiver
        )


# -- structure: lists, field results, nested Contracts and variants recurse -----------------


@pytest.mark.parametrize("side", SIDES)
def test_list_item_recurses(side: FitSide) -> None:
    narrow, wide = "list<int{0..10}>{max_items: 5}", "list<int{0..100}>{max_items: 5}"
    assert _fit({"value": narrow}, {"value": wide}, side=side) == STRUCTURAL
    assert _fit({"value": wide}, {"value": narrow}, side=side) == _type_unfit(
        side, "value[]", "int{0..100}", "int{0..10}"
    )


@pytest.mark.parametrize("side", SIDES)
def test_field_result_fits_by_its_value(side: FitSide) -> None:
    narrow, wide = "field_result<int{0..10}>", "field_result<int{0..100}>"
    assert _fit({"value": narrow}, {"value": wide}, side=side) == STRUCTURAL
    assert _fit({"value": wide}, {"value": narrow}, side=side) == _type_unfit(
        side, "value", "int{0..100}", "int{0..10}"
    )
    assert _fit({"value": "field_result<int>"}, {"value": "int"}, side=side) == _type_unfit(
        side, "value", "field_result<int>", "int"
    )


NARROW_ROW = _contract("acme.row", {"x": "int{0..10}"})
WIDE_ROW = _contract("acme.row", {"x": "int{0..100}"})


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("wrapper", ["{}", "record<{}>", "result<{}>", "refusal<{}>"])
def test_wrapped_contracts_fit_by_shape(side: FitSide, wrapper: str) -> None:
    """The same ``acme.row@1`` name means each side's own copy; the copies fit by shape."""

    spelling = wrapper.format("acme.row@1")
    assert (
        _fit(
            {"value": spelling},
            {"value": spelling},
            side=side,
            sender_closure=[NARROW_ROW],
            receiver_closure=[WIDE_ROW],
        )
        == STRUCTURAL
    )
    assert _fit(
        {"value": spelling},
        {"value": spelling},
        side=side,
        sender_closure=[WIDE_ROW],
        receiver_closure=[NARROW_ROW],
    ) == _type_unfit(side, "value.x", "int{0..100}", "int{0..10}")


@pytest.mark.parametrize("side", SIDES)
def test_nested_contracts_of_other_names_fit_by_shape(side: FitSide) -> None:
    mine = _contract("acme.mine", {"x": "int{0..10}"})
    theirs = _contract("acme.theirs", {"x": "int", "note": "string?"})
    assert (
        _fit(
            {"value": "acme.mine@1"},
            {"value": "acme.theirs@1"},
            side=side,
            sender_closure=[mine],
            receiver_closure=[theirs],
        )
        == STRUCTURAL
    )
    assert _fit(
        {"value": "acme.theirs@1"},
        {"value": "acme.mine@1"},
        side=side,
        sender_closure=[theirs],
        receiver_closure=[mine],
    ) == Unfit(
        side=side, path="value.note", problem="undeclared", sender="string?", receiver=ABSENT
    )


@pytest.mark.parametrize("side", SIDES)
def test_wrapper_kinds_must_match(side: FitSide) -> None:
    assert _fit(
        {"value": "record<acme.row@1>"},
        {"value": "result<acme.row@1>"},
        side=side,
        sender_closure=[NARROW_ROW],
        receiver_closure=[NARROW_ROW],
    ) == _type_unfit(side, "value", "record<acme.row@1>", "result<acme.row@1>")


@pytest.mark.parametrize("side", SIDES)
def test_nested_recursion_goes_deep(side: FitSide) -> None:
    leaf_s = _contract("acme.leaf", {"x": "int{0..10}"})
    leaf_r = _contract("acme.leaf", {"x": "int{0..5}"})
    mid_s = _contract("acme.mid", {"leaf": "acme.leaf@1"})
    mid_r = _contract("acme.mid", {"leaf": "acme.leaf@1", "note": "string?"})
    assert _fit(
        {"value": "list<acme.mid@1>{max_items: 3}"},
        {"value": "list<acme.mid@1>{max_items: 3}"},
        side=side,
        sender_closure=[mid_s, leaf_s],
        receiver_closure=[mid_r, leaf_r],
    ) == _type_unfit(side, "value[].leaf.x", "int{0..10}", "int{0..5}")


OK_NARROW = _contract("acme.ok", {"count": "int{0..10}"})
OK_WIDE = _contract("acme.ok", {"count": "int{0..100}"})
ERR = _contract("acme.err", {"reason": "string"})


@pytest.mark.parametrize("side", SIDES)
def test_variant_arms_subset_and_each_arm_fits(side: FitSide) -> None:
    one, two = "variant{ok: acme.ok@1}", "variant{ok: acme.ok@1, err: acme.err@1}"
    assert (
        _fit(
            {"value": one},
            {"value": two},
            side=side,
            sender_closure=[OK_NARROW],
            receiver_closure=[OK_WIDE, ERR],
        )
        == STRUCTURAL
    )
    assert _fit(
        {"value": two},
        {"value": one},
        side=side,
        sender_closure=[OK_NARROW, ERR],
        receiver_closure=[OK_WIDE],
    ) == _type_unfit(side, "value", render_type(parse_type(two)), render_type(parse_type(one)))
    assert _fit(
        {"value": one},
        {"value": two},
        side=side,
        sender_closure=[OK_WIDE],
        receiver_closure=[OK_NARROW, ERR],
    ) == _type_unfit(side, "value.ok.count", "int{0..100}", "int{0..10}")


# -- Captures (§2.3) ------------------------------------------------------------------------


class _Seam:
    """A Capture seam that records each question and answers from a fixed table."""

    def __init__(self, gaps: dict[str, LayoutGap | None] | None = None) -> None:
        self.gaps = gaps or {}
        self.asked: list[str] = []

    def __call__(self, kind: str, type_fits: TypeFit, row_fits: RowFit) -> LayoutGap | None:
        self.asked.append(kind)
        return self.gaps.get(kind)


GAP = LayoutGap(entry="records.hits", producer=ABSENT, reader="at /hits/*")


@pytest.mark.parametrize(
    ("side", "sent", "received", "asks", "expected"),
    [
        # Request rows: the capability (slot) sends, the operation accepts.
        pytest.param(
            "request", "capture<acme.hits>", "capture<*>", False, None, id="req_k_into_any"
        ),
        pytest.param(
            "request", "capture<acme.hits>", "capture<acme.hits>", True, None, id="req_k_covered"
        ),
        pytest.param(
            "request", "capture<*>", "capture<acme.hits>", False, "type", id="req_any_into_k"
        ),
        # Result rows: the operation returns, the capability expects.
        pytest.param("result", "capture<*>", "capture<*>", False, None, id="res_any_into_any"),
        pytest.param(
            "result", "capture<acme.hits>", "capture<*>", False, None, id="res_k_into_any"
        ),
        pytest.param(
            "result", "capture<acme.hits>", "capture<acme.hits>", True, None, id="res_k_covered"
        ),
        pytest.param(
            "result", "capture<*>", "capture<acme.hits>", False, "type", id="res_any_into_k"
        ),
        pytest.param(
            "result", "capture<acme.other>", "capture<acme.hits>", False, "type", id="res_j_into_k"
        ),
    ],
)
def test_capture_rows(
    side: FitSide, sent: str, received: str, asks: bool, expected: str | None
) -> None:
    seam = _Seam()
    result = _fit({"doc": sent}, {"doc": received}, side=side, covers=seam)
    assert seam.asked == ([KIND] if asks else [])
    if expected is None:
        assert result == STRUCTURAL
    else:
        assert result == _type_unfit(side, "doc", sent, received)


@pytest.mark.parametrize("side", SIDES)
def test_capture_layout_gap_is_the_failing_path(side: FitSide) -> None:
    seam = _Seam({KIND: GAP})
    result = _fit(
        {"doc": "capture<acme.hits>"}, {"doc": "capture<acme.hits>"}, side=side, covers=seam
    )
    assert result == Unfit(
        side=side,
        path="doc.layout.records.hits",
        problem="layout",
        sender=ABSENT,
        receiver="at /hits/*",
    )


def test_capture_messages_read_as_the_design_note() -> None:
    result = _fit({"document": "capture<*>"}, {"document": "capture<acme.hits>"}, side="result")
    assert isinstance(result, Unfit)
    assert render_unfit(result, slot="match") == (
        "result.document: the operation returns capture<*>; the capability expects "
        "capture<acme.hits>"
    )
    sent = 'enum["day", "month", "year"]'
    received = 'enum["day", "month"]'
    result = _fit({"time_range": sent}, {"time_range": received}, side="request")
    assert isinstance(result, Unfit)
    assert render_unfit(result, slot="search") == (
        f"request.time_range: the slot may send {sent}; the operation accepts {received}"
    )


@pytest.mark.parametrize("side", SIDES)
def test_list_of_captures(side: FitSide) -> None:
    seam = _Seam()
    assert (
        _fit(
            {"docs": "list<capture<acme.hits>>{max_items: 4}"},
            {"docs": "list<capture<*>>{max_items: 8}"},
            side=side,
            covers=seam,
        )
        == STRUCTURAL
    )
    assert _fit(
        {"docs": "list<capture<*>>{max_items: 4}"},
        {"docs": "list<capture<acme.hits>>{max_items: 4}"},
        side=side,
        covers=seam,
    ) == _type_unfit(side, "docs[]", "capture<*>", "capture<acme.hits>")
    assert seam.asked == []


def test_the_seam_is_asked_once_per_kind() -> None:
    seam = _Seam()
    result = _fit(
        {"a": "capture<acme.hits>", "b": "list<capture<acme.hits>>{max_items: 2}"},
        {"a": "capture<acme.hits>", "b": "list<capture<acme.hits>>{max_items: 2}"},
        covers=seam,
    )
    assert result == STRUCTURAL
    assert seam.asked == [KIND]


# -- the seam wired to layout_covers ---------------------------------------------------------

JSON_DECODING = {"format": "json", "limits": {"decoded_bytes": 1024, "depth": 8, "records": 100}}
HIT_NARROW = _contract("acme.hit", {"title": "string{max_len: 64}", "rank": "int{0..10}"})
HIT_WIDE = _contract("acme.hit", {"title": "string{max_len: 64}", "rank": "int"})
HIT_EXTRA = _contract(
    "acme.hit", {"title": "string{max_len: 64}", "rank": "int", "note": "string?"}
)


def _declared(row: Contract, *, rank: str, query: str) -> DeclaredCaptureKind:
    return DeclaredCaptureKind.model_validate(
        {
            "name": KIND,
            "decoding": JSON_DECODING,
            "layout": {
                "header": {"query": {"at": "/query", "type": query}},
                "records": {
                    "hits": {
                        "at": "/hits/*",
                        "row": _pinned(row).model_dump(mode="json"),
                        "fields": {
                            name: {"at": name, **({"type": rank} if name == "rank" else {})}
                            for name in row.fields
                        },
                    }
                },
            },
        }
    )


def _wired(sent: DeclaredCaptureKind, received: DeclaredCaptureKind) -> CaptureCovers:
    def covers(kind: str, type_fits: TypeFit, row_fits: RowFit) -> LayoutGap | None:
        assert kind == KIND
        return layout_covers(sent, received, type_fits=type_fits, row_fits=row_fits)

    return covers


def _fit_layouts(
    producer: DeclaredCaptureKind,
    producer_row: Contract,
    reader: DeclaredCaptureKind,
    reader_row: Contract,
    side: FitSide,
) -> FitResult:
    return _fit(
        {"doc": "capture<acme.hits>"},
        {"doc": "capture<acme.hits>"},
        side=side,
        sender_closure=[producer_row],
        receiver_closure=[reader_row],
        covers=_wired(producer, reader),
    )


@pytest.mark.parametrize("side", SIDES)
def test_wired_layouts_cover_by_shape(side: FitSide) -> None:
    producer = _declared(HIT_NARROW, rank="int{0..10}", query="string{max_len: 128}")
    reader = _declared(HIT_WIDE, rank="int", query="string{max_len: 256}")
    assert _fit_layouts(producer, HIT_NARROW, reader, HIT_WIDE, side) == STRUCTURAL


@pytest.mark.parametrize("side", SIDES)
def test_wired_header_type_gap(side: FitSide) -> None:
    producer = _declared(HIT_WIDE, rank="int", query="string{max_len: 256}")
    reader = _declared(HIT_WIDE, rank="int", query="string{max_len: 128}")
    assert _fit_layouts(producer, HIT_WIDE, reader, HIT_WIDE, side) == Unfit(
        side=side,
        path="doc.layout.header.query.type",
        problem="layout",
        sender="string{max_len: 256}",
        receiver="string{max_len: 128}",
    )


@pytest.mark.parametrize("side", SIDES)
def test_wired_row_gap_names_the_row_field(side: FitSide) -> None:
    producer = _declared(HIT_EXTRA, rank="int", query="string{max_len: 128}")
    reader = _declared(HIT_WIDE, rank="int", query="string{max_len: 128}")
    assert _fit_layouts(producer, HIT_EXTRA, reader, HIT_WIDE, side) == Unfit(
        side=side,
        path="doc.layout.records.hits.row.note",
        problem="layout",
        sender="string?",
        receiver=ABSENT,
    )


# -- the fast path, memo, cycles, order -----------------------------------------------------


def _refusing() -> ContractLookup:
    def resolve(ref: ContractRef) -> Contract | None:
        raise AssertionError(f"resolved {ref.spelling} on the fast path")

    return resolve


def _counting(contracts: Iterable[Contract], calls: Counter[str], party: str) -> ContractLookup:
    resolve = closure_lookup(contracts)

    def lookup(ref: ContractRef) -> Contract | None:
        calls[f"{party}:{ref.name}"] += 1
        return resolve(ref)

    return lookup


def test_equal_digests_fit_identical_without_resolving() -> None:
    contract = _contract("acme.request", {"a": "int", "doc": "capture<acme.hits>"})
    result = record_fits(
        _pinned(contract),
        _pinned(contract),
        _refusing(),
        _refusing(),
        side="request",
        capture_covers=_no_layouts,
    )
    assert result == Fits(fit="identical")


def test_equal_contracts_without_pins_fit_identical_without_walking() -> None:
    contract = _contract("acme.request", {"doc": "capture<acme.hits>"})
    result = record_fits(
        _ref(contract),
        _ref(contract),
        closure_lookup([contract]),
        closure_lookup([contract]),
        side="result",
        capture_covers=_no_layouts,
    )
    assert result == Fits(fit="identical")


def test_equal_nested_pins_are_not_resolved() -> None:
    inner = _contract("acme.inner", {"doc": "capture<acme.hits>"})
    sender = _contract("acme.sent", {"inner": _nested(inner), "a": "int{0..1}"})
    receiver = _contract("acme.received", {"inner": _nested(inner), "a": "int"})
    calls: Counter[str] = Counter()
    result = record_fits(
        _ref(sender),
        _ref(receiver),
        _counting([sender], calls, "sender"),
        _counting([receiver], calls, "receiver"),
        side="request",
        capture_covers=_no_layouts,
    )
    assert result == STRUCTURAL
    assert calls["sender:acme.inner"] == calls["receiver:acme.inner"] == 0


def test_a_diamond_is_walked_once() -> None:
    leaf_s = _contract("acme.leaf", {"x": "int{0..10}"})
    leaf_r = _contract("acme.leaf", {"x": "int"})
    mid_s = _contract("acme.mid", {"leaf": _nested(leaf_s)})
    mid_r = _contract("acme.mid", {"leaf": _nested(leaf_r)})
    sender = _contract(
        "acme.sent", {"a": _nested(mid_s), "b": _nested(mid_s), "c": _nested(leaf_s)}
    )
    receiver = _contract(
        "acme.received", {"a": _nested(mid_r), "b": _nested(mid_r), "c": _nested(leaf_r)}
    )
    calls: Counter[str] = Counter()
    result = record_fits(
        _ref(sender),
        _ref(receiver),
        _counting([sender, mid_s, leaf_s], calls, "sender"),
        _counting([receiver, mid_r, leaf_r], calls, "receiver"),
        side="request",
        capture_covers=_no_layouts,
    )
    assert result == STRUCTURAL
    assert calls == Counter(
        {
            "sender:acme.sent": 1,
            "receiver:acme.received": 1,
            "sender:acme.mid": 1,
            "receiver:acme.mid": 1,
            "sender:acme.leaf": 1,
            "receiver:acme.leaf": 1,
        }
    )


def test_a_memoised_failure_is_reported_under_its_own_path() -> None:
    """The seam's row check walks a pair first; the main walk then meets it in the memo."""

    row_s = _contract("acme.hit", {"x": "int"})
    row_r = _contract("acme.hit", {"x": "int{0..1}"})

    def seam(kind: str, type_fits: TypeFit, row_fits: RowFit) -> LayoutGap | None:
        assert row_fits(_pinned(row_s), _pinned(row_r)) == _type_unfit(
            "result", "x", "int", "int{0..1}"
        )
        return None

    sender = _contract("acme.sent", {"doc": "capture<acme.hits>", "row": _nested(row_s)})
    receiver = _contract("acme.received", {"doc": "capture<acme.hits>", "row": _nested(row_r)})
    calls: Counter[str] = Counter()
    result = record_fits(
        _ref(sender),
        _ref(receiver),
        _counting([sender, row_s], calls, "sender"),
        _counting([receiver, row_r], calls, "receiver"),
        side="result",
        capture_covers=seam,
    )
    assert result == _type_unfit("result", "row.x", "int", "int{0..1}")
    assert calls["sender:acme.hit"] == 1
    assert calls["receiver:acme.hit"] == 1


@pytest.mark.parametrize("side", SIDES)
def test_a_self_referencing_contract_terminates(side: FitSide) -> None:
    node_s = _contract("acme.node", {"next": "acme.node@1?", "v": "int{0..10}"})
    node_r = _contract("acme.node", {"next": "acme.node@1?", "v": "int"})
    assert (
        _fit(
            {"value": "acme.node@1"},
            {"value": "acme.node@1"},
            side=side,
            sender_closure=[node_s],
            receiver_closure=[node_r],
        )
        == STRUCTURAL
    )
    assert _fit(
        {"value": "acme.node@1"},
        {"value": "acme.node@1"},
        side=side,
        sender_closure=[node_r],
        receiver_closure=[node_s],
    ) == _type_unfit(side, "value.v", "int", "int{0..10}")


def test_the_first_failing_path_is_stable() -> None:
    """Fields are walked in name order, whatever order either side declares them in."""

    failing = {"z": "int", "m": "int?", "a": "int{0..100}"}
    receiving = {"m": "int", "a": "int{0..10}"}
    expected = _type_unfit("request", "a", "int{0..100}", "int{0..10}")
    assert _fit(failing, receiving) == expected
    assert _fit(dict(reversed(failing.items())), dict(reversed(receiving.items()))) == expected
    assert _fit({"z": "int", "m": "int?"}, {"m": "int"}) == Unfit(
        side="request", path="m", problem="optional", sender="int?", receiver="int"
    )
    assert _fit({"z": "int"}, {}) == Unfit(
        side="request", path="z", problem="undeclared", sender="int", receiver=ABSENT
    )


def test_an_unresolved_pin_raises() -> None:
    with pytest.raises(FitUnresolved) as raised:
        _fit({"value": "acme.missing@1"}, {"value": "acme.missing@1"})
    assert raised.value.party == "sender"
    with pytest.raises(FitUnresolved) as raised:
        _fit({"value": "acme.row@1"}, {"value": "acme.row@1"}, sender_closure=[NARROW_ROW])
    assert raised.value.party == "receiver"
    assert raised.value.ref.spelling == "acme.row@1"
