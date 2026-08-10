"""The MCP tools/call seam REFUSES the inputs the 0.4.0 removals retired.

Taking a parameter off a tool signature does not refuse it: FastMCP validates
``arguments`` against the signature and DISCARDS what it does not declare, so a
stale caller sending ``group_override=True`` got a successful call with its
input dropped. Each case below asserts the refusal AND that the handler never
ran -- a refused call must not be a half-executed one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_client import contracts
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server

_FEEDBACK_ARGS = {
    "instance_id": "inst-1",
    "action": "accept",
    "from_type": "Part",
    "from_id": "BP-1",
    "relationship_type": "fits",
    "to_type": "Vehicle",
    "to_id": "V-1",
}
_FROM_QUERY_ARGS = {
    "instance_id": "inst-1",
    "receipt_id": "RCP-1",
    "result_index": 0,
    "action": "accept",
}

# tool, arguments, retired key, the guidance the refusal must carry.
_RETIRED_TOOL_CALLS = [
    ("cruxible_feedback", {**_FEEDBACK_ARGS, "source": "agent"}, "source", "actor_context"),
    (
        "cruxible_feedback",
        {**_FEEDBACK_ARGS, "group_override": True},
        "group_override",
        "no public replacement",
    ),
    (
        "cruxible_feedback_from_query",
        {**_FROM_QUERY_ARGS, "source": "agent"},
        "source",
        "actor_context",
    ),
    (
        "cruxible_feedback_from_query",
        {**_FROM_QUERY_ARGS, "group_override": True},
        "group_override",
        "no public replacement",
    ),
    (
        "cruxible_outcome",
        {"instance_id": "inst-1", "outcome": "correct", "source": "agent"},
        "source",
        "actor_context",
    ),
    (
        "cruxible_propose_group",
        {
            "instance_id": "inst-1",
            "relationship_type": "fits",
            "members": [],
            "proposed_by": "agent",
        },
        "proposed_by",
        "actor_context",
    ),
    (
        "cruxible_resolve_group",
        {
            "instance_id": "inst-1",
            "group_id": "GRP-1",
            "action": "approve",
            "expected_pending_version": 1,
            "resolved_by": "agent",
        },
        "resolved_by",
        "actor_context",
    ),
    (
        "cruxible_create_decision_record",
        {"instance_id": "inst-1", "question": "Ship it?", "opened_by": "agent"},
        "opened_by",
        "actor_context",
    ),
]

_HANDLERS_THAT_MUST_NOT_RUN = (
    "handle_feedback",
    "handle_feedback_batch",
    "handle_feedback_from_query",
    "handle_outcome",
    "handle_propose_group",
    "handle_resolve_group",
    "handle_create_decision_record",
)


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Trip-wire every handler these tools reach, so execution is observable."""
    calls: list[str] = []

    def _tripwire(name: str) -> Any:
        def _record(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} executed for a refused call")

        return _record

    for name in _HANDLERS_THAT_MUST_NOT_RUN:
        monkeypatch.setattr(handlers, name, _tripwire(name))
    return calls


@pytest.mark.parametrize(("tool", "arguments", "key", "guidance"), _RETIRED_TOOL_CALLS)
def test_retired_tool_arguments_are_refused_before_the_tool_runs(
    executed: list[str],
    tool: str,
    arguments: dict[str, Any],
    key: str,
    guidance: str,
) -> None:
    server = create_server()

    with pytest.raises(ToolError) as raised:
        asyncio.run(server.call_tool(tool, arguments))

    assert f"'{key}' was removed in 0.4.0" in str(raised.value)
    assert guidance in str(raised.value)
    assert executed == []


def test_a_retired_key_inside_a_batch_item_is_refused_too(executed: list[str]) -> None:
    """Batch carries its retired keys on ``items``, refused during arg validation."""
    server = create_server()

    with pytest.raises(ToolError) as raised:
        asyncio.run(
            server.call_tool(
                "cruxible_feedback_batch",
                {
                    "instance_id": "inst-1",
                    "items": [
                        {
                            "receipt_id": "RCP-1",
                            "action": "accept",
                            "target": {
                                "from_type": "Part",
                                "from_id": "BP-1",
                                "relationship_type": "fits",
                                "to_type": "Vehicle",
                                "to_id": "V-1",
                            },
                            "source": "agent",
                        }
                    ],
                },
            )
        )

    assert "'source' was removed in 0.4.0" in str(raised.value)
    assert executed == []


def test_an_unrelated_unknown_argument_is_still_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam refuses retired keys by NAME; it is not an extras ban."""
    monkeypatch.setattr(
        handlers,
        "handle_feedback",
        lambda *_args, **_kwargs: contracts.FeedbackResult(
            feedback_id="FB-1",
            action="accept",
            applied=True,
        ),
    )
    server = create_server()

    result = asyncio.run(
        server.call_tool(
            "cruxible_feedback",
            {**_FEEDBACK_ARGS, "not_a_retired_input": "whatever"},
        )
    )

    assert result is not None
