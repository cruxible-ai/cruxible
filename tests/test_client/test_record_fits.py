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
    _Fail,
    _Seg,
    _Walk,
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
    FieldPresent,
    check_value,
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
def test_nested_copies_fit_by_shape(side: FitSide) -> None:
    """The same ``acme.row@1`` name means each side's own copy; the copies fit by shape."""

    assert (
        _fit(
            {"value": "acme.row@1"},
            {"value": "acme.row@1"},
            side=side,
            sender_closure=[NARROW_ROW],
            receiver_closure=[WIDE_ROW],
        )
        == STRUCTURAL
    )
    assert _fit(
        {"value": "acme.row@1"},
        {"value": "acme.row@1"},
        side=side,
        sender_closure=[WIDE_ROW],
        receiver_closure=[NARROW_ROW],
    ) == _type_unfit(side, "value.x", "int{0..100}", "int{0..10}")


# -- wrappers follow their wire values: fit implies the receiver's validator accepts -------


def _wrapped_case(
    spelling: str, sent_row: Contract, received_row: Contract, value: object, side: FitSide
) -> tuple[FitResult, bool]:
    """Fit ``spelling`` over each side's row copy; check that ``value`` is valid for the sender
    (asserted) and return whether the receiver's existing validator accepts it."""

    sender = _contract("acme.sent", {"value": spelling})
    receiver = _contract("acme.received", {"value": spelling})
    resolve_sender = closure_lookup((sender, sent_row))
    resolve_receiver = closure_lookup((receiver, received_row))
    fit = record_fits(
        _ref(sender),
        _ref(receiver),
        resolve_sender,
        resolve_receiver,
        side=side,
        capture_covers=_no_layouts,
    )
    sent = check_value(value, sender.fields["value"].type, at="/value", resolve=resolve_sender)
    assert isinstance(sent, FieldPresent), sent
    received = check_value(
        value, receiver.fields["value"].type, at="/value", resolve=resolve_receiver
    )
    return fit, isinstance(received, FieldPresent)


def _envelope(spelling: str, fields: dict[str, object]) -> object:
    record = {"record": "/0", "key": {}, "fields": fields}
    return {"tag": "record", "record": record} if spelling.startswith("result") else record


SEVEN = {"tag": "present", "value": 7, "at": "/x"}
ROW_X = _contract("acme.row", {"x": "int"})
ROW_X_Y = _contract("acme.row", {"x": "int", "y": "string?"})
ROW_X_OPTIONAL = _contract("acme.row", {"x": "int?"})


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("spelling", ["record<acme.row@1>", "result<acme.row@1>"])
@pytest.mark.parametrize(
    ("sent_row", "received_row", "fields", "expected"),
    [
        pytest.param(NARROW_ROW, WIDE_ROW, {"x": SEVEN}, None, id="value_type_within"),
        pytest.param(
            WIDE_ROW,
            NARROW_ROW,
            {"x": {"tag": "present", "value": 50, "at": "/x"}},
            ("x", "type", "int{0..100}", "int{0..10}"),
            id="value_type_wider",
        ),
        # The envelope carries every row field: an optional receiver field the sender's row
        # lacks is missing from every sender value.
        pytest.param(
            ROW_X,
            ROW_X_Y,
            {"x": SEVEN},
            ("y", "required", ABSENT, "string?"),
            id="receiver_row_has_more_fields",
        ),
        pytest.param(
            ROW_X_Y,
            ROW_X,
            {"x": SEVEN, "y": {"tag": "absent"}},
            ("y", "undeclared", "string?", ABSENT),
            id="sender_row_has_more_fields",
        ),
        # Optionality is not compared: both envelopes carry field_result<int>.
        pytest.param(
            ROW_X_OPTIONAL, ROW_X, {"x": {"tag": "absent"}}, None, id="optional_into_required"
        ),
        pytest.param(ROW_X, ROW_X_OPTIONAL, {"x": SEVEN}, None, id="required_into_optional"),
    ],
)
def test_record_and_result_rows_follow_the_envelope(
    side: FitSide,
    spelling: str,
    sent_row: Contract,
    received_row: Contract,
    fields: dict[str, object],
    expected: tuple[str, str, str, str] | None,
) -> None:
    fit, accepted = _wrapped_case(
        spelling, sent_row, received_row, _envelope(spelling, fields), side
    )
    if expected is None:
        assert fit == STRUCTURAL
        assert accepted
    else:
        path, problem, sender, receiver = expected
        assert fit == Unfit(
            side=side, path=f"value.{path}", problem=problem, sender=sender, receiver=receiver
        )
        assert not accepted


ROW_X_NAMED = _contract("acme.other", {"x": "int"})
ROW_X_DESCRIBED = validate_contract(
    {"name": "acme.row", "version": 1, "description": "Another wording.", "fields": {"x": "int"}}
)


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize(
    ("sent_row", "received_row", "fits"),
    [
        pytest.param(ROW_X, ROW_X, True, id="same_pin"),
        pytest.param(ROW_X, ROW_X_DESCRIBED, False, id="description_only_digest_differs"),
        pytest.param(NARROW_ROW, WIDE_ROW, False, id="narrower_body"),
    ],
)
def test_refusal_needs_the_same_pin(
    side: FitSide, sent_row: Contract, received_row: Contract, fits: bool
) -> None:
    value = {
        "code": "declined",
        "contract": _pinned(sent_row).model_dump(mode="json"),
        "body": {"x": 7},
    }
    fit, accepted = _wrapped_case("refusal<acme.row@1>", sent_row, received_row, value, side)
    assert accepted is fits
    if fits:
        assert fit == STRUCTURAL
        return
    assert isinstance(fit, Unfit)
    assert (fit.path, fit.problem) == ("value", "type")
    sent_digest = contract_digest(sent_row).tagged[:19]
    assert fit.sender == f"refusal<acme.row@1> at {sent_digest}"


@pytest.mark.parametrize("side", SIDES)
def test_refusal_of_another_name_with_the_same_body(side: FitSide) -> None:
    sender = _contract("acme.sent", {"value": "refusal<acme.row@1>"})
    receiver = _contract("acme.received", {"value": "refusal<acme.other@1>"})
    resolve_receiver = closure_lookup((receiver, ROW_X_NAMED))
    fit = record_fits(
        _ref(sender),
        _ref(receiver),
        closure_lookup((sender, ROW_X)),
        resolve_receiver,
        side=side,
        capture_covers=_no_layouts,
    )
    assert fit == _type_unfit(side, "value", "refusal<acme.row@1>", "refusal<acme.other@1>")
    value = {
        "code": "declined",
        "contract": _pinned(ROW_X).model_dump(mode="json"),
        "body": {"x": 7},
    }
    received = check_value(
        value, receiver.fields["value"].type, at="/value", resolve=resolve_receiver
    )
    assert not isinstance(received, FieldPresent)


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


def test_equal_digests_skip_the_receiver_and_the_walk() -> None:
    contract = _contract("acme.request", {"a": "int", "b": "list<string>{max_items: 3}"})
    calls: Counter[str] = Counter()
    result = record_fits(
        _pinned(contract),
        _pinned(contract),
        _counting([contract], calls, "sender"),
        _refusing(),
        side="request",
        capture_covers=_no_layouts,
    )
    assert result == Fits(fit="identical")
    assert calls == Counter({"sender:acme.request": 1})


def test_equal_digests_still_ask_every_reached_kind() -> None:
    inner = _contract("acme.inner", {"doc": "capture<acme.links>"})
    contract = _contract(
        "acme.request",
        {"doc": "capture<acme.hits>", "inner": _nested(inner), "any": "capture<*>"},
    )
    seam = _Seam()
    result = record_fits(
        _pinned(contract),
        _pinned(contract),
        closure_lookup([contract, inner]),
        _refusing(),
        side="result",
        capture_covers=seam,
    )
    assert result == Fits(fit="identical")
    assert seam.asked == ["acme.hits", "acme.links"]
    seam = _Seam({"acme.links": GAP})
    result = record_fits(
        _pinned(contract),
        _pinned(contract),
        closure_lookup([contract, inner]),
        _refusing(),
        side="result",
        capture_covers=seam,
    )
    assert result == Unfit(
        side="result",
        path="inner.doc.layout.records.hits",
        problem="layout",
        sender=ABSENT,
        receiver="at /hits/*",
    )


def test_equal_contracts_without_pins_still_ask_their_kinds() -> None:
    contract = _contract("acme.request", {"doc": "capture<acme.hits>"})
    seam = _Seam({KIND: GAP})
    result = record_fits(
        _ref(contract),
        _ref(contract),
        closure_lookup([contract]),
        closure_lookup([contract]),
        side="result",
        capture_covers=seam,
    )
    assert isinstance(result, Unfit)
    assert result.path == "doc.layout.records.hits"


def test_equal_nested_pins_are_not_compared() -> None:
    inner = _contract("acme.inner", {"doc": "capture<acme.hits>"})
    sender = _contract("acme.sent", {"inner": _nested(inner), "a": "int{0..1}"})
    receiver = _contract("acme.received", {"inner": _nested(inner), "a": "int"})
    calls: Counter[str] = Counter()
    seam = _Seam()
    result = record_fits(
        _ref(sender),
        _ref(receiver),
        _counting([sender, inner], calls, "sender"),
        _counting([receiver], calls, "receiver"),
        side="request",
        capture_covers=seam,
    )
    assert result == STRUCTURAL
    assert calls["receiver:acme.inner"] == 0
    assert calls["sender:acme.inner"] == 1
    assert seam.asked == [KIND]


# The review's reproduction: one Contract on both sides, layouts that differ.
OTHER_SET_ROW = _contract("acme.hit", {"title": "string"})


def _set_layout(record_set: str, at: str) -> DeclaredCaptureKind:
    return DeclaredCaptureKind.model_validate(
        {
            "name": KIND,
            "decoding": JSON_DECODING,
            "layout": {
                "records": {
                    record_set: {
                        "at": at,
                        "row": _pinned(OTHER_SET_ROW).model_dump(mode="json"),
                        "fields": {"title": {"at": "title"}},
                    }
                }
            },
        }
    )


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("pinned", [True, False])
def test_shared_contract_with_unequal_layouts_is_unfit(side: FitSide, pinned: bool) -> None:
    shared = _contract("acme.root", {"doc": "capture<acme.hits>"})
    covers = _wired(_set_layout("other", "/other/*"), _set_layout("hits", "/hits/*"))
    pin = _pinned if pinned else _ref
    expected = Unfit(
        side=side,
        path="doc.layout.records.hits",
        problem="layout",
        sender=ABSENT,
        receiver="at /hits/*",
    )
    closure = [shared, OTHER_SET_ROW]
    assert (
        record_fits(
            pin(shared),
            pin(shared),
            closure_lookup(closure),
            closure_lookup(closure),
            side=side,
            capture_covers=covers,
        )
        == expected
    )
    # The same Contract nested under roots of different names.
    sender = _contract("acme.sent", {"body": _nested(shared)})
    receiver = _contract("acme.received", {"body": _nested(shared)})
    nested = record_fits(
        _ref(sender),
        _ref(receiver),
        closure_lookup([sender, *closure]),
        closure_lookup([receiver, *closure]),
        side=side,
        capture_covers=covers,
    )
    assert nested == expected.model_copy(update={"path": "body.doc.layout.records.hits"})


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


# -- layouts that name their own kind --------------------------------------------------------


def _linked(name: str, parent: str, query: str) -> DeclaredCaptureKind:
    """A kind whose header names ``capture<parent>``; ``parent`` may be the kind itself."""

    return DeclaredCaptureKind.model_validate(
        {
            "name": name,
            "decoding": JSON_DECODING,
            "layout": {
                "header": {
                    "parent": {"at": "/parent", "type": f"capture<{parent}>"},
                    "query": {"at": "/query", "type": query},
                },
                "records": {
                    "hits": {
                        "at": "/hits/*",
                        "row": _pinned(OTHER_SET_ROW).model_dump(mode="json"),
                        "fields": {"title": {"at": "title"}},
                    }
                },
            },
        }
    )


def _by_name(*kinds: DeclaredCaptureKind) -> CaptureCovers:
    sent = {kind.name: kind for kind in kinds[: len(kinds) // 2]}
    received = {kind.name: kind for kind in kinds[len(kinds) // 2 :]}

    def covers(kind: str, type_fits: TypeFit, row_fits: RowFit) -> LayoutGap | None:
        return layout_covers(sent[kind], received[kind], type_fits=type_fits, row_fits=row_fits)

    return covers


def _fit_linked(covers: CaptureCovers, side: FitSide) -> FitResult:
    return _fit(
        {"doc": "capture<acme.hits>"},
        {"doc": "capture<acme.hits>"},
        side=side,
        sender_closure=[OTHER_SET_ROW],
        receiver_closure=[OTHER_SET_ROW],
        covers=covers,
    )


NARROW_QUERY, WIDE_QUERY = "string{max_len: 10}", "string{max_len: 20}"


@pytest.mark.parametrize("side", SIDES)
def test_a_layout_naming_its_own_kind_terminates(side: FitSide) -> None:
    fits = _by_name(_linked(KIND, KIND, NARROW_QUERY), _linked(KIND, KIND, WIDE_QUERY))
    assert _fit_linked(fits, side) == STRUCTURAL
    unfit = _by_name(_linked(KIND, KIND, WIDE_QUERY), _linked(KIND, KIND, NARROW_QUERY))
    assert _fit_linked(unfit, side) == Unfit(
        side=side,
        path="doc.layout.header.query.type",
        problem="layout",
        sender=WIDE_QUERY,
        receiver=NARROW_QUERY,
    )


@pytest.mark.parametrize("side", SIDES)
def test_a_mutual_pair_of_layouts_terminates(side: FitSide) -> None:
    links = "acme.links"
    fits = _by_name(
        _linked(KIND, links, NARROW_QUERY),
        _linked(links, KIND, NARROW_QUERY),
        _linked(KIND, links, WIDE_QUERY),
        _linked(links, KIND, WIDE_QUERY),
    )
    assert _fit_linked(fits, side) == STRUCTURAL
    # The mismatch is in acme.links, reached through acme.hits' header; it is not hidden
    # by the back edge to acme.hits.
    unfit = _by_name(
        _linked(KIND, links, NARROW_QUERY),
        _linked(links, KIND, WIDE_QUERY),
        _linked(KIND, links, WIDE_QUERY),
        _linked(links, KIND, NARROW_QUERY),
    )
    assert _fit_linked(unfit, side) == Unfit(
        side=side,
        path="doc.layout.header.parent.type",
        problem="layout",
        sender=WIDE_QUERY,
        receiver=NARROW_QUERY,
    )


def test_a_success_that_assumed_a_failed_kind_is_dropped() -> None:
    links = "acme.links"
    covers = _by_name(
        _linked(KIND, links, WIDE_QUERY),
        _linked(links, KIND, NARROW_QUERY),
        _linked(KIND, links, NARROW_QUERY),
        _linked(links, KIND, WIDE_QUERY),
    )
    walk = _Walk(
        closure_lookup([OTHER_SET_ROW]), closure_lookup([OTHER_SET_ROW]), "request", covers
    )
    assert walk._kind(KIND) is not None
    # acme.links covered only on the assumption that acme.hits did; acme.hits did not.
    assert ("kind", links) not in walk.memo
    assert walk.provisional == {}


# -- deep closures: an explicit work stack, linear work and storage --------------------------


def _chain(prefix: str, depth: int, leaf: str) -> list[Contract]:
    contracts = [_contract(f"acme.{prefix}000", {"x": leaf})]
    for index in range(1, depth):
        contracts.append(_contract(f"acme.{prefix}{index:03d}", {"next": _nested(contracts[-1])}))
    return contracts


def _chain_walk(
    depth: int, sent_leaf: str, received_leaf: str
) -> tuple[_Walk, FitResult, Counter[str]]:
    sent, received = _chain("s", depth, sent_leaf), _chain("r", depth, received_leaf)
    calls: Counter[str] = Counter()
    walk = _Walk(
        _counting(sent, calls, "sender"),
        _counting(received, calls, "receiver"),
        "request",
        _no_layouts,
    )
    return walk, walk.record(_pinned(sent[-1]), _pinned(received[-1])), calls


def _retained_path(walk: _Walk) -> tuple[int, int]:
    """Distinct path segments the memo's failures hold, and their characters."""

    seen: dict[int, int] = {}
    for value in walk.memo.values():
        if not isinstance(value, _Fail):
            continue
        node: _Seg | None = value.path
        while node is not None and id(node) not in seen:
            seen[id(node)] = len(node.head)
            node = node.tail
    return len(seen), sum(seen.values())


@pytest.mark.parametrize("depth", [125, 250])
def test_a_deep_pinned_chain_fits(depth: int) -> None:
    walk, result, calls = _chain_walk(depth, "int{0..10}", "int")
    assert result == STRUCTURAL
    assert sum(calls.values()) == 2 * depth
    assert walk.steps == depth


@pytest.mark.parametrize("depth", [125, 250])
def test_a_deep_pinned_chain_fails_at_its_leaf(depth: int) -> None:
    walk, result, calls = _chain_walk(depth, "int", "int{0..10}")
    assert result == _type_unfit("request", "next." * (depth - 1) + "x", "int", "int{0..10}")
    assert sum(calls.values()) == 2 * depth
    assert walk.steps == depth + 1
    segments, characters = _retained_path(walk)
    # One shared segment per level: the leaf's ``x`` and one ``next`` per pair above it.
    assert segments == depth
    assert characters == 1 + 4 * (depth - 1)


def test_a_deep_shared_chain_still_asks_its_kind() -> None:
    contracts = _chain("s", 250, "capture<acme.hits>")
    seam = _Seam({KIND: GAP})
    result = record_fits(
        _pinned(contracts[-1]),
        _pinned(contracts[-1]),
        closure_lookup(contracts),
        _refusing(),
        side="result",
        capture_covers=seam,
    )
    assert isinstance(result, Unfit)
    assert result.path == "next." * 249 + "x.layout.records.hits"
    assert seam.asked == [KIND]
