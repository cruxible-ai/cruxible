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
#: estimated tokens: every tool's input schema plus its description. Raised from
#: 10,000 by the first-run ruling (Q1, 2026-10-21) for re-bind and Subject
#: retirement, new capabilities, once trims fell short.
DEFAULT_PROFILE_MODEL_VISIBLE_TOKENS = 10_400

#: The distributed plugin uses full + a pinned 22-tool allowlist. The 2026-11-08
#: maintainer ruling adds kit/provider/Line/Runbook startup to the lean catalog;
#: the fresh-install walkthrough (2026-11-09) adds inbox_list (next's top repair)
#: and claim_attest (named by review-state), measured at 17,206.
#: This separate ceiling does not widen the existing default profile's budget.
PLUGIN_PROFILE_MODEL_VISIBLE_TOKENS = 17_500


def model_visible_tokens(tool: Any) -> float:
    """One tool's estimated model-visible cost: its input schema and description."""

    return (len(json.dumps(tool.inputSchema)) + len(tool.description or "")) / 4


def catalog_model_visible_tokens(tools: Iterable[Any]) -> float:
    return sum(model_visible_tokens(tool) for tool in tools)
