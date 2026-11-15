"""Help text, refusals and docstrings use the current nouns.

The contract batch removed ProviderInterfaces and slot signatures: a slot
names a capability, a provider build declares operations, and a Line binds
each slot to one operation. These surfaces described the old forms after the
batch landed; each is pinned here.
"""

from __future__ import annotations

import re

from click.testing import CliRunner

from cruxible_client.contracts.procedures.authored import ProcedureSpec
from cruxible_client.contracts.procedures.models import ProcedureDefinition
from cruxible_client.contracts.procedures.nodes import CallNode
from cruxible_core.cli.main import cli
from cruxible_core.mcp.tool_prompts import TOOL_DESCRIPTIONS
from cruxible_core.proposals.proposals import REMOVED_NOUNS

_REMOVED = re.compile(r"\binterfaces?\b|\bsignature\b|\{in, out, effect\}", re.IGNORECASE)


def test_slot_and_provider_help_names_capabilities_and_operations() -> None:
    texts = {
        "ProcedureSpec.slots": ProcedureSpec.model_fields["slots"].description or "",
        "ProcedureDefinition": ProcedureDefinition.__doc__ or "",
        "CallNode": CallNode.__doc__ or "",
        "cruxible_provider_list": TOOL_DESCRIPTIONS["cruxible_provider_list"],
    }
    help_text = CliRunner().invoke(cli, ["kit", "build", "--help"])
    assert help_text.exit_code == 0, help_text.output
    texts["kit build --help"] = help_text.output
    assert {name: text for name, text in texts.items() if _REMOVED.search(text)} == {}


def test_the_removed_interface_path_names_what_replaced_it() -> None:
    message = REMOVED_NOUNS["provider-interfaces/"]
    assert "signature" not in message
    assert "a Procedure slot names the capability it needs" in message
