"""CaptureKind layouts: shape, law, grow-only succession, and coverage."""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.capability_fit import FitResult, Fits, Unfit
from cruxible_client.contracts.capture_layouts import (
    ABSENT,
    CaptureLayout,
    DeclaredCaptureKind,
    LayoutGap,
    LayoutLawCode,
    declared_capture_kind_digest,
    evaluate_layout_law,
    layout_covers,
    layout_succession_message,
    layout_successor_break,
)
from cruxible_client.contracts.captures import CaptureDecoding
from cruxible_client.contracts.provider_operations import closure_lookup
from cruxible_client.contracts.shapes import (
    Contract,
    ContractRef,
    FieldType,
    contract_digest,
    edge_refusal,
    render_type,
    validate_contract,
)

ROW = validate_contract(
    {"name": "acme.hit", "version": 1, "fields": {"title": "string{max_len: 64}", "rank": "int"}}
)
ROW_V2 = validate_contract(
    {
        "name": "acme.hit",
        "version": 2,
        "predecessor_digest": contract_digest(ROW).tagged,
        "fields": {"title": "string{max_len: 64}", "rank": "int", "note": "string?"},
    }
)
ROW_V2_REQUIRED = validate_contract(
    {
        "name": "acme.hit",
        "version": 3,
        "predecessor_digest": contract_digest(ROW_V2).tagged,
        "fields": {"title": "string{max_len: 64}", "rank": "int", "note": "string"},
    }
)
OTHER = validate_contract({"name": "acme.miss", "version": 1, "fields": {"reason": "string"}})
RESOLVE = closure_lookup((ROW, ROW_V2, ROW_V2_REQUIRED, OTHER))
JSON = CaptureDecoding.model_validate(
    {"format": "json", "limits": {"decoded_bytes": 1024, "depth": 8, "records": 100}}
)
CSV = CaptureDecoding.model_validate(
    {"format": "csv", "csv": {}, "limits": {"decoded_bytes": 1024, "depth": 8, "records": 100}}
)


def _pin(contract: Contract) -> dict[str, object]:
    return {
        "name": contract.name,
        "version": contract.version,
        "digest": contract_digest(contract).tagged,
    }


def _hits(**changes: Any) -> dict[str, Any]:
    record_set: dict[str, Any] = {
        "at": "/hits/*",
        "key": {"url": {"at": "url", "type": "string{max_len: 2048}"}},
        "row": _pin(ROW),
        "fields": {"title": {"at": "title"}, "rank": {"at": "rank", "parse": "int"}},
        "count_at": "/count",
    }
    record_set.update(changes)
    return record_set


def _layout(**changes: Any) -> CaptureLayout:
    data: dict[str, Any] = {
        "header": {"query": {"at": "/query", "type": "string{max_len: 256}"}},
        "records": {"hits": _hits()},
    }
    data.update(changes)
    return CaptureLayout.model_validate(data)


def _kind(layout: CaptureLayout | None = None, **changes: Any) -> DeclaredCaptureKind:
    data: dict[str, Any] = {
        "name": "acme.hits",
        "decoding": JSON,
        "layout": _layout() if layout is None else layout,
    }
    data.update(changes)
    return DeclaredCaptureKind.model_validate(data)


def _type_fits(sender: FieldType, receiver: FieldType) -> FitResult:
    """Today's edge rule, standing in for the fit function another slice owns."""

    if edge_refusal(sender, receiver) is None:
        return Fits(fit="structural")
    return Unfit(
        side="result",
        path="value",
        problem="type",
        sender=render_type(sender),
        receiver=render_type(receiver),
    )


def _row_fits(sender: ContractRef, receiver: ContractRef) -> FitResult:
    """Equal digests fit; anything else reports its first field (the fit slice decides)."""

    if sender == receiver:
        return Fits(fit="identical")
    return Unfit(side="result", path="title", problem="type", sender="int", receiver="string")


def _covers(producer: DeclaredCaptureKind, reader: DeclaredCaptureKind) -> LayoutGap | None:
    return layout_covers(producer, reader, type_fits=_type_fits, row_fits=_row_fits)


# -- shape ---------------------------------------------------------------------------------


def test_a_layout_reads_authored_spellings_and_dumps_sorted() -> None:
    layout = _layout(records={"hits": _hits(row="acme.hit@1")})

    record_set = layout.records["hits"]
    assert record_set.row == ContractRef(name="acme.hit", version=1)
    assert list(record_set.fields) == ["rank", "title"]
    assert layout.row_refs() == (ContractRef(name="acme.hit", version=1),)
    dumped = CaptureLayout.model_validate({"records": {"hits": _hits(key={}, count_at=None)}})
    assert set(dumped.model_dump(mode="json")) == {"records"}
    assert set(dumped.model_dump(mode="json")["records"]["hits"]) == {"at", "row", "fields"}


@pytest.mark.parametrize(
    "data",
    [
        {"records": {}},
        {"records": {"Hits": _hits()}},
        {"records": {"hits": _hits(at="/a/*/b/*")}},
        {"records": {"hits": _hits(fields={})}},
        {"records": {"hits": _hits(key={"title": {"at": "title"}})}},
        {"records": {"hits": _hits(count_at="")}},
        {"records": {"hits": _hits()}, "header": {"Bad": {"at": "/x"}}},
        {"records": {"hits": _hits(fields={"title": {"at": "title", "parse": "money"}})}},
        {"records": {"hits": _hits()}, "unknown": {}},
    ],
)
def test_a_layout_refuses_bad_shapes(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CaptureLayout.model_validate(data)


def test_a_declared_kind_needs_a_json_or_csv_decoding_for_a_layout() -> None:
    raw = DeclaredCaptureKind.model_validate({"name": "acme.raw"})
    assert raw.model_dump(mode="json") == {"name": "acme.raw"}

    with pytest.raises(ValidationError, match="needs the kind's decoding"):
        DeclaredCaptureKind.model_validate({"name": "acme.hits", "layout": _layout()})
    text = JSON.model_copy(update={"format": "text"})
    with pytest.raises(ValidationError, match="json or csv"):
        _kind(decoding=text)
    with pytest.raises(ValidationError, match="no header fields"):
        _kind(decoding=CSV)
    csv_layout = CaptureLayout.model_validate({"records": {"rows": _hits(at="/*", count_at=None)}})
    assert _kind(csv_layout, decoding=CSV).layout == csv_layout
    with pytest.raises(ValidationError, match="at /\\*"):
        _kind(
            CaptureLayout.model_validate({"records": {"rows": _hits(count_at=None)}}), decoding=CSV
        )


def test_the_declared_kind_digest_covers_name_decoding_and_layout() -> None:
    kind = _kind()
    other_limits = _kind(
        decoding=JSON.model_copy(update={"limits": JSON.limits.model_copy(update={"depth": 9})})
    )

    assert declared_capture_kind_digest(kind) == declared_capture_kind_digest(_kind())
    assert declared_capture_kind_digest(kind) != declared_capture_kind_digest(other_limits)
    assert declared_capture_kind_digest(kind) != declared_capture_kind_digest(
        _kind(_layout(header={}))
    )


# -- the law -------------------------------------------------------------------------------


def test_the_law_codes_are_a_closed_vocabulary() -> None:
    assert set(get_args(LayoutLawCode)) == {
        "cruxible.capture_kind.layout_decoding_unsupported",
        "cruxible.capture_kind.layout_row_unresolved",
        "cruxible.capture_kind.layout_field_unknown",
        "cruxible.capture_kind.layout_field_missing",
        "cruxible.capture_kind.layout_ref_unresolved",
        "cruxible.capture_kind.layout_succession_break",
    }


def test_the_layout_law_accepts_a_sound_layout() -> None:
    assert evaluate_layout_law(_layout(), decoding=JSON, resolve=RESOLVE) == ()


def test_the_layout_law_names_each_problem() -> None:
    unknown_field = _layout(
        records={"hits": _hits(fields={**_hits()["fields"], "score": {"at": "score"}})}
    )
    problems = evaluate_layout_law(unknown_field, decoding=JSON, resolve=RESOLVE)
    assert [problem.code for problem in problems] == ["cruxible.capture_kind.layout_field_unknown"]
    assert "['score']" in problems[0].message

    stale = _layout(records={"hits": _hits(row={**_pin(ROW), "digest": "sha256:" + "0" * 64})})
    problems = evaluate_layout_law(stale, decoding=JSON, resolve=RESOLVE)
    assert [problem.code for problem in problems] == ["cruxible.capture_kind.layout_row_unresolved"]
    assert "pinned digest" in problems[0].message

    problems = evaluate_layout_law(_layout(), decoding=None, resolve=RESOLVE)
    assert [problem.code for problem in problems] == [
        "cruxible.capture_kind.layout_decoding_unsupported"
    ]


@pytest.mark.parametrize(
    ("row", "fields", "missing"),
    [
        # A required row field with no rule.
        (ROW, {"title": {"at": "title"}}, ["rank"]),
        # An optional row field still needs a rule: its field result is part of the record.
        (ROW_V2, {"title": {"at": "title"}, "rank": {"at": "rank", "parse": "int"}}, ["note"]),
        (ROW_V2, {"title": {"at": "title"}}, ["note", "rank"]),
    ],
)
def test_every_row_field_needs_a_field_rule(
    row: Contract, fields: dict[str, Any], missing: list[str]
) -> None:
    layout = _layout(records={"hits": _hits(row=_pin(row), fields=fields)})
    problems = evaluate_layout_law(layout, decoding=JSON, resolve=RESOLVE)

    assert [problem.code for problem in problems] == [
        "cruxible.capture_kind.layout_field_missing"
    ] * len(missing)
    assert [problem.message.split(":")[0] for problem in problems] == [
        f"records.hits.fields.{field}" for field in missing
    ]


def test_a_key_part_is_not_a_row_field() -> None:
    keyed = validate_contract({"name": "acme.keyed", "version": 1, "fields": {"title": "string"}})
    layout = _layout(
        records={
            "hits": _hits(
                row=_pin(keyed),
                key={"url": {"at": "url"}, "title": {"at": "title"}},
                fields={"other": {"at": "other"}},
            )
        }
    )
    problems = evaluate_layout_law(layout, decoding=JSON, resolve=closure_lookup((keyed,)))

    assert [problem.code for problem in problems] == [
        "cruxible.capture_kind.layout_field_unknown",
        "cruxible.capture_kind.layout_field_missing",
    ]


@pytest.mark.parametrize(
    ("row", "fields", "document"),
    [
        (
            ROW,
            {"title": {"at": "title"}, "rank": {"at": "rank", "parse": "int"}},
            {
                "query": "q",
                "hits": [{"url": "https://a.example", "title": "a", "rank": 7}],
                "count": 1,
            },
        ),
        (
            ROW_V2,
            {
                "title": {"at": "title"},
                "rank": {"at": "rank", "parse": "int"},
                "note": {"at": "note", "presence": "optional"},
            },
            {
                "query": "q",
                "hits": [{"url": "https://a.example", "title": "a", "rank": 7}],
                "count": 1,
            },
        ),
    ],
)
def test_an_accepted_layout_yields_whole_records_of_its_row(
    row: Contract, fields: dict[str, Any], document: dict[str, Any]
) -> None:
    import json

    from cruxible_client.contracts.procedures.layout_extract import (
        LayoutExtractNode,
        resolve_layout_extract,
    )
    from cruxible_client.contracts.shapes import FieldPresent, RecordType, check_value
    from cruxible_core.procedures.extract import run_extract

    layout = _layout(records={"hits": _hits(row=_pin(row), fields=fields)})
    assert evaluate_layout_law(layout, decoding=JSON, resolve=RESOLVE) == ()
    node = resolve_layout_extract(
        LayoutExtractNode.model_validate(
            {
                "node_id": "hits",
                "capture": "$steps.search.results",
                "records": "hits",
                "header": ["query"],
                "as": "hits",
            }
        ),
        layout,
    )
    outcome = run_extract(
        node,
        capture_digest="sha256:" + "0" * 64,
        content=json.dumps(document).encode("utf-8"),
        decoding=JSON,
        row=row,
        resolve=RESOLVE,
    )

    assert outcome.complete_when_held and len(outcome.records) == 1
    for record in outcome.records:
        checked = check_value(
            record.model_dump(mode="json"),
            RecordType(contract=ContractRef.model_validate(_pin(row))),
            at="",
            resolve=RESOLVE,
        )
        assert isinstance(checked, FieldPresent), checked


# -- succession ----------------------------------------------------------------------------


def test_a_layout_may_grow() -> None:
    old = _layout()
    grown = _layout(
        header={**old.model_dump()["header"], "page": {"at": "/page", "parse": "int"}},
        records={
            "hits": _hits(),
            "misses": {
                "at": "/misses/*",
                "row": _pin(OTHER),
                "fields": {"reason": {"at": "reason"}},
            },
        },
    )

    assert layout_successor_break(None, old, resolve=RESOLVE) is None
    assert layout_successor_break(old, old, resolve=RESOLVE) is None
    assert layout_successor_break(old, grown, resolve=RESOLVE) is None


def test_a_row_may_move_to_a_later_version_that_adds_optional_fields_with_rules() -> None:
    old = _layout()
    moved = _layout(
        records={
            "hits": _hits(
                row=_pin(ROW_V2),
                fields={**_hits()["fields"], "note": {"at": "note", "presence": "optional"}},
            )
        }
    )
    assert layout_successor_break(old, moved, resolve=RESOLVE) is None

    without_rule = _layout(records={"hits": _hits(row=_pin(ROW_V2))})
    assert layout_successor_break(old, without_rule, resolve=RESOLVE) == "records.hits.fields.note"

    required = _layout(
        records={
            "hits": _hits(
                row=_pin(ROW_V2_REQUIRED),
                fields={**_hits()["fields"], "note": {"at": "note"}},
            )
        }
    )
    assert layout_successor_break(old, required, resolve=RESOLVE) == "records.hits.row.note"


@pytest.mark.parametrize(
    ("successor", "entry"),
    [
        (None, "layout"),
        (_layout(header={}), "header.query"),
        (_layout(header={"query": {"at": "/q", "type": "string{max_len: 256}"}}), "header.query"),
        (_layout(records={"other": _hits()}), "records.hits"),
        (_layout(records={"hits": _hits(at="/results/*")}), "records.hits.at"),
        (_layout(records={"hits": _hits(key={})}), "records.hits.key"),
        (_layout(records={"hits": _hits(count_at="/total")}), "records.hits.count_at"),
        (_layout(records={"hits": _hits(count_at=None)}), "records.hits.count_at"),
        (
            _layout(
                records={"hits": _hits(fields={"title": {"at": "name"}, "rank": {"at": "rank"}})}
            ),
            "records.hits.fields.rank",
        ),
        (
            _layout(records={"hits": _hits(fields={**_hits()["fields"], "extra": {"at": "x"}})}),
            "records.hits.fields.extra",
        ),
        (
            _layout(records={"hits": _hits(row=_pin(OTHER), fields={"reason": {"at": "r"}})}),
            "records.hits.fields.rank",
        ),
    ],
)
def test_a_layout_may_not_change_what_it_declared(
    successor: CaptureLayout | None, entry: str
) -> None:
    found = layout_successor_break(_layout(), successor, resolve=RESOLVE)

    assert found == entry
    assert "declare a new kind" in layout_succession_message(found)


def test_a_row_moved_to_another_name_or_an_earlier_version_breaks_at_the_row() -> None:
    renamed = _layout(records={"hits": _hits(row=_pin(OTHER))})
    assert layout_successor_break(_layout(), renamed, resolve=RESOLVE) == "records.hits.row"

    newer = _layout(
        records={
            "hits": _hits(
                row=_pin(ROW_V2),
                fields={**_hits()["fields"], "note": {"at": "note", "presence": "optional"}},
            )
        }
    )
    assert layout_successor_break(newer, _layout(), resolve=RESOLVE) == "records.hits.fields.note"
    unresolved = closure_lookup((ROW,))
    assert layout_successor_break(_layout(), newer, resolve=unresolved) == "records.hits.row"


# -- coverage ------------------------------------------------------------------------------


def test_equal_declarations_cover_and_a_reader_without_a_layout_needs_nothing() -> None:
    assert _covers(_kind(), _kind()) is None
    raw = DeclaredCaptureKind.model_validate({"name": "acme.hits", "decoding": JSON})
    assert _covers(raw, raw) is None
    assert _covers(_kind(), raw) is None


def test_a_producer_may_declare_more_than_the_reader() -> None:
    producer = _kind(
        _layout(
            header={
                "query": {"at": "/query", "type": "string{max_len: 256}"},
                "page": {"at": "/p"},
            },
            records={
                "hits": _hits(),
                "misses": {
                    "at": "/misses/*",
                    "row": _pin(OTHER),
                    "fields": {"reason": {"at": "reason"}},
                },
            },
        )
    )

    assert _covers(producer, _kind()) is None


@pytest.mark.parametrize(
    ("producer", "gap"),
    [
        (
            DeclaredCaptureKind.model_validate({"name": "acme.hits", "decoding": JSON}),
            LayoutGap(entry="layout", producer=ABSENT, reader="record sets hits"),
        ),
        (
            _kind(name="acme.other"),
            LayoutGap(entry="name", producer="acme.other", reader="acme.hits"),
        ),
        (
            _kind(_layout(header={})),
            LayoutGap(
                entry="header.query",
                producer=ABSENT,
                reader="at /query, type string{max_len: 256}",
            ),
        ),
        (
            _kind(_layout(header={"query": {"at": "/query", "type": "string{max_len: 512}"}})),
            LayoutGap(
                entry="header.query.type",
                producer="string{max_len: 512}",
                reader="string{max_len: 256}",
            ),
        ),
        (
            _kind(_layout(records={"other": _hits()})),
            LayoutGap(entry="records.hits", producer=ABSENT, reader="at /hits/*"),
        ),
        (
            _kind(_layout(records={"hits": _hits(at="/results/*")})),
            LayoutGap(entry="records.hits.at", producer="/results/*", reader="/hits/*"),
        ),
        (
            _kind(_layout(records={"hits": _hits(key={"url": {"at": "link"}})})),
            LayoutGap(
                entry="records.hits.key.url",
                producer="at link",
                reader="at url, type string{max_len: 2048}",
            ),
        ),
        (
            _kind(_layout(records={"hits": _hits(count_at=None)})),
            LayoutGap(entry="records.hits.count_at", producer=ABSENT, reader="/count"),
        ),
        (
            _kind(_layout(records={"hits": _hits(row=_pin(ROW_V2))})),
            LayoutGap(entry="records.hits.row.title", producer="int", reader="string"),
        ),
        (
            _kind(_layout(records={"hits": _hits(fields={"title": {"at": "title"}})})),
            LayoutGap(
                entry="records.hits.fields.rank", producer=ABSENT, reader="at rank, parse int"
            ),
        ),
    ],
)
def test_coverage_names_the_first_gap(producer: DeclaredCaptureKind, gap: LayoutGap) -> None:
    assert _covers(producer, _kind()) == gap


def test_coverage_compares_decoding_formats() -> None:
    csv_layout = CaptureLayout.model_validate({"records": {"hits": _hits(at="/*", count_at=None)}})
    json_reader = _kind(csv_layout)

    assert _covers(_kind(csv_layout, decoding=CSV), json_reader) == LayoutGap(
        entry="decoding.format", producer="csv", reader="json"
    )


# -- references inside field-rule types (review F-002) ----------------------------------------

PART = validate_contract({"name": "acme.meta-part", "version": 1, "fields": {"name": "string"}})
META = validate_contract(
    {
        "name": "acme.meta",
        "version": 1,
        "fields": {"part": {"type": {"kind": "contract", "contract": _pin(PART)}}},
    }
)


def _with_header(spelling: str) -> CaptureLayout:
    return _layout(header={"meta": {"at": "/meta", "type": spelling, "presence": "required"}})


@pytest.mark.parametrize(
    "spelling",
    [
        "acme.meta@1",
        "list<acme.meta@1>{max_items: 4}",
        "variant{one: acme.meta@1}",
        "field_result<acme.meta@1>",
        "record<acme.meta@1>",
    ],
)
def test_the_layout_law_resolves_contracts_a_rule_type_names(spelling: str) -> None:
    layout = _with_header(spelling)
    problems = evaluate_layout_law(layout, decoding=JSON, resolve=RESOLVE)

    assert [problem.code for problem in problems] == ["cruxible.capture_kind.layout_ref_unresolved"]
    assert problems[0].message.startswith("header.meta: acme.meta@1")
    resolve = closure_lookup((ROW, META, PART))
    assert evaluate_layout_law(layout, decoding=JSON, resolve=resolve) == ()


def test_the_layout_law_resolves_transitively_from_rule_types_and_rows() -> None:
    problems = evaluate_layout_law(
        _with_header("acme.meta@1"), decoding=JSON, resolve=closure_lookup((ROW, META))
    )
    assert [problem.code for problem in problems] == ["cruxible.capture_kind.layout_ref_unresolved"]
    assert problems[0].message.startswith("header.meta via acme.meta@1: acme.meta-part@1")

    nested_row = validate_contract(
        {
            "name": "acme.hit",
            "version": 1,
            "fields": {
                "title": "string{max_len: 64}",
                "rank": "int",
                "meta": {"type": {"kind": "contract", "contract": _pin(META)}, "optional": True},
            },
        }
    )
    layout = _layout(
        records={
            "hits": _hits(
                row=_pin(nested_row),
                fields={**_hits()["fields"], "meta": {"at": "meta", "presence": "optional"}},
            )
        }
    )
    problems = evaluate_layout_law(layout, decoding=JSON, resolve=closure_lookup((nested_row,)))
    assert [problem.code for problem in problems] == ["cruxible.capture_kind.layout_ref_unresolved"]
    assert problems[0].message.startswith("records.hits.row via acme.hit@1: acme.meta@1")


def test_the_layout_law_checks_named_kinds_when_it_can_see_them() -> None:
    layout = _layout(header={"att": {"at": "/att", "type": "capture<acme.other>"}})

    assert evaluate_layout_law(layout, decoding=JSON, resolve=RESOLVE) == ()
    assert (
        evaluate_layout_law(
            layout, decoding=JSON, resolve=RESOLVE, capture_kind_exists=lambda name: True
        )
        == ()
    )
    problems = evaluate_layout_law(
        layout, decoding=JSON, resolve=RESOLVE, capture_kind_exists=lambda name: False
    )
    assert [problem.code for problem in problems] == ["cruxible.capture_kind.layout_ref_unresolved"]
    assert problems[0].message == "header.att: capture<acme.other> names no CaptureKind here."
