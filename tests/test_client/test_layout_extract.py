"""The extract node that reads through its kind's layout, and its resolution to today's spec."""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.capture_layouts import CaptureLayout
from cruxible_client.contracts.procedures.extract import CompleteWhen, ExtractField, ExtractNode
from cruxible_client.contracts.procedures.layout_extract import (
    KeyTypeOverride,
    LayoutExtractNode,
    LayoutExtractRefusalCode,
    LayoutExtractRefused,
    LayoutExtractStep,
    resolve_layout_extract,
)
from cruxible_client.contracts.shapes import ContractRef

ROW = {"name": "acme.hit", "version": 1, "digest": "sha256:" + "1" * 64}
LAYOUT = CaptureLayout.model_validate(
    {
        "header": {
            "query": {"at": "/query", "type": "string{max_len: 256}"},
            "page": {"at": "/page", "parse": "int"},
        },
        "records": {
            "hits": {
                "at": "/hits/*",
                "key": {"url": {"at": "url", "type": "string{max_len: 2048}"}},
                "row": ROW,
                "fields": {"title": {"at": "title"}, "rank": {"at": "rank", "parse": "int"}},
                "count_at": "/count",
            },
            "notes": {"at": "/notes/*", "row": ROW, "fields": {"title": {"at": "text"}}},
            "tagged": {
                "at": "/tagged/*",
                "key": {"code": {"at": "code", "canonicalize": "claim_type"}},
                "row": ROW,
                "fields": {"title": {"at": "title"}},
            },
        },
    }
)


def _node(**changes: Any) -> LayoutExtractNode:
    data: dict[str, Any] = {
        "kind": "extract",
        "node_id": "hits",
        "capture": "$steps.search.results",
        "records": "hits",
        "header": ["query"],
        "as": "hits",
    }
    data.update(changes)
    return LayoutExtractNode.model_validate(data)


def test_the_node_dumps_only_what_it_states() -> None:
    node = _node(header=[])

    assert node.model_dump(mode="json", by_alias=True) == {
        "kind": "extract",
        "node_id": "hits",
        "capture": "$steps.search.results",
        "records": "hits",
        "duplicates": "refuse_key",
        "as": "hits",
        "next": None,
    }
    assert (node.capture_alias, node.capture_field) == ("search", "results")


@pytest.mark.parametrize(
    "changes",
    [
        {"capture": "$steps.search"},
        {"records": "/hits/*"},
        {"header": ["query", "page"]},
        {"header": ["query", "query"]},
        {"key": {"url": {"type": "string"}}},
        {"key": {"url": {"type": "key_part<acme.page:url>", "at": "link"}}},
        {"fields": {"title": {"at": "title"}}},
    ],
)
def test_the_node_refuses_what_is_the_kind_s_to_declare(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _node(**changes)


def test_an_accepted_node_carries_its_row_pin() -> None:
    """Ruling L1: acceptance adds the record set's row pin; the author never writes it."""

    node = _node(header=[], row="acme.hit@1")

    assert node.row is not None and (node.row.name, node.row.version) == ("acme.hit", 1)
    assert node.model_dump(mode="json", by_alias=True)["row"] == {
        "name": "acme.hit",
        "version": 1,
    }


def test_resolution_builds_today_s_full_spec() -> None:
    resolved = resolve_layout_extract(_node(header=["page", "query"]), LAYOUT)

    assert resolved == ExtractNode.model_validate(
        {
            "kind": "extract",
            "node_id": "hits",
            "capture": "$steps.search.results",
            "records": "/hits/*",
            "complete_when": {"count_at": "/count"},
            "key": {"url": {"at": "url", "type": "string{max_len: 2048}"}},
            "row": ROW,
            "fields": {"title": {"at": "title"}, "rank": {"at": "rank", "parse": "int"}},
            "header": {
                "page": {"at": "/page", "parse": "int"},
                "query": {"at": "/query", "type": "string{max_len: 256}"},
            },
            "as": "hits",
        }
    )
    assert resolved.complete_when == CompleteWhen(count_at="/count")
    assert resolved.row == ContractRef.model_validate(ROW)


def test_resolution_keeps_the_node_s_read_policy() -> None:
    resolved = resolve_layout_extract(
        _node(header=[], duplicates="first", only_keys=["https://a.example/"], next="write"),
        LAYOUT,
    )

    assert resolved.duplicates == "first"
    assert resolved.only_keys == ["https://a.example/"]
    assert resolved.next == "write"
    assert resolved.header == {}
    unkeyed = resolve_layout_extract(_node(records="notes", header=[]), LAYOUT)
    assert unkeyed.key == {} and unkeyed.complete_when is None


def test_a_plain_string_key_part_may_take_a_subject_key_type() -> None:
    resolved = resolve_layout_extract(
        _node(key={"url": {"type": "key_part<acme.page:url>"}}), LAYOUT
    )

    assert resolved.key == {"url": ExtractField(at="url", type="key_part<acme.page:url>")}
    assert KeyTypeOverride(type="key<acme.page>").type == "key<acme.page>"


def test_resolution_codes_are_a_closed_vocabulary() -> None:
    assert set(get_args(LayoutExtractRefusalCode)) == {
        "cruxible.procedure.extract_layout_undeclared",
        "cruxible.procedure.extract_records_unknown",
        "cruxible.procedure.extract_header_unknown",
        "cruxible.procedure.extract_key_override_invalid",
        "cruxible.procedure.extract_records_unkeyed",
        "cruxible.procedure.extract_format_unsupported",
        "cruxible.procedure.extract_row_unavailable",
    }


@pytest.mark.parametrize(
    ("node", "layout", "code", "reads_list"),
    [
        (_node(), None, "cruxible.procedure.extract_layout_undeclared", False),
        (_node(records="misses"), LAYOUT, "cruxible.procedure.extract_records_unknown", False),
        (_node(header=["total"]), LAYOUT, "cruxible.procedure.extract_header_unknown", False),
        (
            _node(key={"link": {"type": "key_part<acme.page:url>"}}),
            LAYOUT,
            "cruxible.procedure.extract_key_override_invalid",
            False,
        ),
        (
            _node(records="tagged", header=[], key={"code": {"type": "key_part<acme.tag:code>"}}),
            LAYOUT,
            "cruxible.procedure.extract_key_override_invalid",
            False,
        ),
        (
            _node(records="notes", header=[], only_keys=["x"]),
            LAYOUT,
            "cruxible.procedure.extract_records_unkeyed",
            False,
        ),
        (_node(header=[]), LAYOUT, "cruxible.procedure.extract_format_unsupported", True),
        (_node(records="notes"), LAYOUT, "cruxible.procedure.extract_format_unsupported", True),
    ],
)
def test_resolution_refuses_with_a_typed_code(
    node: LayoutExtractNode, layout: CaptureLayout | None, code: str, reads_list: bool
) -> None:
    with pytest.raises(LayoutExtractRefused) as caught:
        resolve_layout_extract(node, layout, reads_list=reads_list)

    assert caught.value.code == code


def test_a_list_read_may_select_a_record_set_with_no_count_and_no_header() -> None:
    resolved = resolve_layout_extract(_node(records="notes", header=[]), LAYOUT, reads_list=True)

    assert resolved.records == "/notes/*"


def test_the_authored_step_names_a_record_set_and_header_fields() -> None:
    step = LayoutExtractStep.model_validate(
        {
            "extract": "hits",
            "capture": {"$ref": "step", "step": "search", "path": ["results"]},
            "records": "hits",
            "header": ["query"],
        }
    )

    assert step.header == ("query",)
    assert step.duplicates == "refuse_key"
    with pytest.raises(ValidationError):
        LayoutExtractStep.model_validate(
            {
                "extract": "hits",
                "capture": {"$ref": "step", "step": "search", "path": ["results"]},
                "records": "hits",
                "fields": {"title": {"at": "title"}},
            }
        )
