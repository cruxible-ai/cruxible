"""Format tags are internal: no MCP input schema offers one, and calls work without them.

The guardrail half pins ruling 3: a request-side ``playbill-*-vN`` tag in an
input schema is call grammar a model must copy, so the advertised schemas hide
every one and the server fills them in before the arguments validate.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field

from cruxible_core.mcp.server import create_server
from cruxible_core.mcp.wire_tags import (
    exposed_wire_tags,
    fill_tool_arguments,
    fill_wire_tags,
    hide_wire_tags,
)


@pytest.mark.parametrize("profile", ["default", "full"])
def test_no_mcp_input_schema_exposes_a_format_tag(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", profile)
    tools = asyncio.run(create_server().list_tools())

    exposed = {tool.name: exposed_wire_tags(tool.inputSchema) for tool in tools}
    assert {name: tags for name, tags in exposed.items() if tags} == {}


class _V1(BaseModel):
    tag: Literal["playbill-example-v1"]
    value: int


class _V2(BaseModel):
    tag: Literal["playbill-example-v2"] = "playbill-example-v2"
    value: int
    note: str


class _Holder(BaseModel):
    item: Annotated[_V1 | _V2, Field(discriminator="tag")]
    items: list[_V1] = []


def test_a_missing_tag_is_filled_from_the_model() -> None:
    filled = fill_wire_tags({"item": {"value": 1}, "items": [{"value": 2}]}, _Holder)

    assert filled == {
        "item": {"tag": "playbill-example-v1", "value": 1},
        "items": [{"tag": "playbill-example-v1", "value": 2}],
    }
    assert _Holder.model_validate(filled).item.tag == "playbill-example-v1"


def test_a_union_takes_the_newest_member_the_arguments_validate_as() -> None:
    filled = fill_wire_tags({"value": 1, "note": "n"}, _V1 | _V2)
    assert filled["tag"] == "playbill-example-v2"


def test_a_sent_tag_is_kept_and_checked() -> None:
    filled = fill_wire_tags({"item": {"tag": "playbill-example-v1", "value": 3}}, _Holder)
    assert filled["item"]["tag"] == "playbill-example-v1"


def test_the_schema_hides_tag_properties_and_their_discriminator() -> None:
    schema = _Holder.model_json_schema()
    hidden = hide_wire_tags(schema)

    assert exposed_wire_tags(schema)
    assert exposed_wire_tags(hidden) == []
    assert "discriminator" not in str(hidden)


def test_tool_arguments_fill_before_validation() -> None:
    class _Args(BaseModel):
        request: _Holder

    filled = fill_tool_arguments(_Args, {"request": {"item": {"value": 4}}})
    assert _Args.model_validate(filled).request.item.tag == "playbill-example-v1"
