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

#: The distributed plugin uses full + a pinned 26-tool allowlist. The 2026-11-08
#: maintainer ruling adds kit/provider/Line/Runbook startup to the lean catalog;
#: the fresh-install walkthrough (2026-11-09) adds inbox_list (next's top repair)
#: and claim_attest (named by review-state); since (what changed after a named
#: generation, named by review-state) adds 843.5; cruxible_init and
#: cruxible_authoring_submit_file (a bare folder by tools alone, 2026-11-10) add
#: about 230 after redundancy-only trims elsewhere, measured at 18,287. The
#: 2026-11-10 maintainer ruling raises it to 18,400 so the plugin is complete on
#: its own: cruxible_console (the read-only console without the CLI) joins the
#: allowlist, measured at 18,387.75. The 2026-11-19 plugin release check adds
#: reviewer/independent-approval parameters to cruxible_init and no_open to
#: cruxible_console, measured at 18,544.25 (ceiling 18,600). Compiler revision 74
#: adds ``based_on`` to cruxible_set and to cruxible_write's set and add changes,
#: measured at 18,674.75 with every based_on description dropped from the MCP
#: schemas, the floor short of removing the parameter: ceiling 18,700, approved
#: by the maintainer (2026-11-23) for ``based_on`` across cruxible_set and
#: cruxible_write. ``provider install --move`` (0.6.0 slice D2) adds ``move`` and
#: ``move_only`` to cruxible_provider_install's request, measured at 18,715.25:
#: ceiling 18,750, approved by the maintainer (2026-11-23) for slice D2's ``move``
#: and ``move_only`` on cruxible_provider_install. 0.6.0 kit recipes and scripts
#: (``KitRecipeSpec``, ``KitRecipeParamSpec`` and ``KitScriptSpec`` in
#: cruxible_kit_add's inline ``bundle`` schema) add 736, and the get and set
#: description touch-ups 6.5, measured at 19,457.75: ceiling 19,460, raised for
#: the 0.6.0 kit manifest (needs the maintainer's ratification, as each earlier
#: raise had). The ceiling exists so each addition is a deliberate decision, not
#: drift.
#: This separate ceiling does not widen the existing default profile's budget.
PLUGIN_PROFILE_MODEL_VISIBLE_TOKENS = 19_460


def model_visible_tokens(tool: Any) -> float:
    """One tool's estimated model-visible cost: its input schema and description."""

    return (len(json.dumps(tool.inputSchema)) + len(tool.description or "")) / 4


def catalog_model_visible_tokens(tools: Iterable[Any]) -> float:
    return sum(model_visible_tokens(tool) for tool in tools)
