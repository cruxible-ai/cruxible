"""The default MCP profile's model-visible budget, measured one way everywhere.

What an MCP host sends the model for each tool is its name, its description
and its input schema (the OpenAI Agents SDK converter and the official MCP
Anthropic client both pass exactly these). Output schemas are validated by the
host and are not sent as call grammar, so they are not part of this budget;
they are measured separately as a follow-up.

The estimate is the JSON text length over four, the same rule the read-cut
roadmap item used.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

#: Approved budget for the default profile's model-visible tool catalog, in
#: estimated tokens: every tool's input schema plus its description.
DEFAULT_PROFILE_MODEL_VISIBLE_TOKENS = 10_000


def model_visible_tokens(tool: Any) -> float:
    """One tool's estimated model-visible cost: its input schema and description."""

    return (len(json.dumps(tool.inputSchema)) + len(tool.description or "")) / 4


def catalog_model_visible_tokens(tools: Iterable[Any]) -> float:
    return sum(model_visible_tokens(tool) for tool in tools)
