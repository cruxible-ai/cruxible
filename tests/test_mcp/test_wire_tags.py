"""Format tags are internal: no MCP input schema offers one, and calls work without them.

The guardrail half pins ruling 3: a request-side ``playbill-*-vN`` tag in an
input schema is call grammar a model must copy, so the advertised schemas hide
every one and the server fills them in before the arguments validate.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field, TypeAdapter

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


def _walk(node: object):  # type: ignore[no-untyped-def]
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


@pytest.mark.parametrize("profile", ["default", "full"])
def test_advertised_schemas_are_valid_and_keep_no_tagless_one_of(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    """A oneOf over branches that lost a hidden tag would reject valid calls."""

    from jsonschema import Draft202012Validator

    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", profile)
    server = create_server()
    manager = server._tool_manager
    for tool in asyncio.run(server.list_tools()):
        Draft202012Validator.check_schema(tool.inputSchema)
        original = manager.get_tool(tool.name).parameters.get("$defs", {})
        hidden = tool.inputSchema.get("$defs", {})
        lost = {
            name
            for name, definition in original.items()
            if set(definition.get("properties", {}))
            != set(hidden.get(name, {}).get("properties", {}))
        }
        for node in _walk(tool.inputSchema):
            for branch in node.get("oneOf", ()):
                assert branch.get("$ref", "").rsplit("/", 1)[-1] not in lost, tool.name


def _stamp_arguments(**extra: object) -> dict[str, object]:
    return {
        "source_id": "corpus.runbook",
        "block_id": "summary",
        "declared_generation": 1,
        "declared_coordinate": {
            "git_oid": "4" * 64,
            "semantic_root": "sha256:" + "1" * 64,
            "generation_root": "sha256:" + "2" * 64,
            "compiler_digest": "sha256:" + "3" * 64,
        },
        "backing": [
            {
                "identity": {"kind": "Claim", "name": "CLM-" + "a" * 32},
                "statement_digest": "sha256:" + "8" * 64,
            }
        ],
        "body_digest": "sha256:" + "9" * 64,
        **extra,
    }


def test_a_tagless_projection_stamp_validates_against_its_advertised_schema() -> None:
    """Both stamp versions accept the common fields once their tags are hidden."""

    from jsonschema import validate

    from cruxible_client.contracts.declared_blocks import (
        ProjectionBlockStamp,
        ProjectionBlockStampAny,
    )

    adapter: TypeAdapter[object] = TypeAdapter(ProjectionBlockStampAny)
    schema = hide_wire_tags(adapter.json_schema())
    arguments = _stamp_arguments()
    validate(arguments, schema)
    filled = adapter.validate_python(fill_wire_tags(arguments, ProjectionBlockStampAny))
    assert isinstance(filled, ProjectionBlockStamp)  # the newest member that accepts it


def test_a_shared_tag_does_not_pick_the_older_stamp() -> None:
    """The marker-grammar tag both versions carry narrows nothing; currency_policy is V2's."""

    from jsonschema import validate

    from cruxible_client.contracts.declared_blocks import (
        ProjectionBlockStamp,
        ProjectionBlockStampAny,
    )

    adapter: TypeAdapter[object] = TypeAdapter(ProjectionBlockStampAny)
    arguments = _stamp_arguments(
        grammar_version="playbill-projection-marker-grammar-v1",
        currency_policy="require_current",
    )
    validate(
        {k: v for k, v in arguments.items() if k != "grammar_version"},
        hide_wire_tags(adapter.json_schema()),
    )
    filled = adapter.validate_python(fill_wire_tags(arguments, ProjectionBlockStampAny))
    assert isinstance(filled, ProjectionBlockStamp)
    assert filled.currency_policy == "require_current"


def test_default_profile_calls_without_tags_match_the_schema_and_validate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jsonschema import validate

    monkeypatch.delenv("CRUXIBLE_MCP_PROFILE", raising=False)
    server = create_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    manager = server._tool_manager
    calls = {
        "cruxible_query": {
            "name": "work.items",
            "budgets": {"max_results": 5, "max_traversal_depth": 0},
        },
        "cruxible_next": {
            "access_profile": {"profile_id": "reviewer"},
            "expiring_within": {"microseconds": 3_600_000_000},
        },
    }
    for name, arguments in calls.items():
        validate(arguments, tools[name].inputSchema)
        arg_model = manager.get_tool(name).fn_metadata.arg_model
        arg_model.model_validate(fill_tool_arguments(arg_model, arguments))
