"""Guardrails on what we CLAIM about permission tiers and read side effects.

Two claims went stale and were quietly load-bearing:

* the MCP ``BASE_INSTRUCTIONS`` told agents ``READ_ONLY`` performs no
  persistence (query and gate-check both write receipt rows) and attributed
  config mutation to ``ADMIN`` alone (constraints and decision policies are
  governed-write config additions);
* the docs presented ``CRUXIBLE_MODE`` as gating every session, when it is a
  boundary only on the daemon and MCP surfaces — the local CLI reads the
  operator's own environment and is an operator console by design;
* the state-resolution tier table filed both halves of a state pull under
  ``governed_write`` after ``state_pull_apply`` rose to ``ADMIN`` (its preview
  is ``READ_ONLY``);
* the mutation CLI's help text still told operators to WRITE terminal lifecycle
  statuses that add/update now refuse.

These pin the corrected statements so the next edit has to keep them true.
"""

from __future__ import annotations

import re
from pathlib import Path

from cruxible_core.cli.commands import mutations as mutations_module
from cruxible_core.graph.assertion_state import (
    TERMINAL_ENTITY_LIFECYCLE_STATUSES,
    TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES,
)
from cruxible_core.mcp.server import BASE_INSTRUCTIONS
from cruxible_core.runtime import permissions as permissions_module

REPO_ROOT = Path(__file__).resolve().parents[2]

_OPERATOR_CONSOLE_CLAIM = "operator console at operator tier by design"

_STATE_RESOLUTION_DOC = REPO_ROOT / "docs" / "state-resolution-and-maintenance.md"


def _flat(text: str) -> str:
    """Lowercase with runs of whitespace collapsed, so line wrapping is invisible."""
    return re.sub(r"\s+", " ", text).lower()


def _tier_section(name: str) -> str:
    """The BASE_INSTRUCTIONS bullet describing one permission tier."""
    bullets = BASE_INSTRUCTIONS.split("\n- `")
    for bullet in bullets[1:]:
        if bullet.startswith(name):
            return _flat(bullet)
    raise AssertionError(f"BASE_INSTRUCTIONS has no bullet for tier {name}")


def test_base_instructions_admit_that_reads_persist_receipts() -> None:
    read_only = _tier_section("READ_ONLY")
    assert "no graph or config mutations" in read_only
    assert "receipt" in read_only


def test_base_instructions_do_not_reserve_config_mutation_to_admin() -> None:
    governed = _tier_section("GOVERNED_WRITE")
    assert "cruxible_add_constraint" in governed
    assert "cruxible_add_decision_policy" in governed

    # ADMIN owns replacing the ACTIVE config, not "config mutation" generally.
    admin = _tier_section("ADMIN")
    assert "active config" in admin
    assert "cruxible_reload_config" in admin


def test_permissions_module_states_where_the_tier_is_a_boundary() -> None:
    doc = _flat(permissions_module.__doc__ or "")
    assert "local cli" in doc
    assert _OPERATOR_CONSOLE_CLAIM in doc
    assert "never get a shell on the state host" in doc


def test_docs_publish_the_local_operator_console_posture() -> None:
    auth_doc = _flat((REPO_ROOT / "docs" / "runtime-auth-and-agent-roles.md").read_text())
    assert "where permission tiers are enforced" in auth_doc
    assert _OPERATOR_CONSOLE_CLAIM in auth_doc

    for name in ("state-resolution-and-maintenance.md", "for-ai-agents.md"):
        text = _flat((REPO_ROOT / "docs" / name).read_text())
        assert _OPERATOR_CONSOLE_CLAIM in text, name


def test_auth_doc_scopes_reviewer_independence_to_auth_on_daemons() -> None:
    auth_doc = _flat((REPO_ROOT / "docs" / "runtime-auth-and-agent-roles.md").read_text())
    assert "reviewer-independence guarantees are provable only on auth-on daemons" in auth_doc
    assert "auth-off surfaces resolve all actors to the local operator" in auth_doc


def _tier_table_rows() -> dict[str, str]:
    """The ``| tier | can do |`` table in the state-resolution doc, as {tier: cell}."""
    rows: dict[str, str] = {}
    for line in _STATE_RESOLUTION_DOC.read_text().splitlines():
        match = re.match(r"^\|\s*`(read_only|governed_write|graph_write|admin)`\s*\|(.*)\|$", line)
        if match:
            rows[match.group(1)] = _flat(match.group(2))
    assert set(rows) == {"read_only", "governed_write", "graph_write", "admin"}, (
        "state-resolution-and-maintenance.md no longer has a four-tier table"
    )
    return rows


def test_state_resolution_tier_table_matches_the_declared_state_pull_tiers() -> None:
    """Both halves of a state pull are filed under their real declared tier.

    ``TOOL_PERMISSIONS`` is the single source of truth; the doc row is derived
    from it here rather than restated, so raising or lowering either tier in the
    permission table fails this test until the doc follows.
    """
    rows = _tier_table_rows()
    for tool, command in (
        ("cruxible_state_pull_preview", "state pull-preview"),
        ("cruxible_state_pull_apply", "state pull-apply"),
    ):
        declared = permissions_module.TOOL_PERMISSIONS[tool].name.lower()
        naming = sorted(tier for tier, cell in rows.items() if f"`{command}`" in cell)
        assert naming == [declared], (
            f"{command} is declared {declared} in TOOL_PERMISSIONS but the "
            f"state-resolution tier table lists it under {naming or 'no tier'}"
        )

    # The stale phrasing lumped both halves together under one tier.
    assert "state pulls" not in rows["governed_write"]


_LIFECYCLE_OPTION_HELP = {
    "add_entity_cmd": (TERMINAL_ENTITY_LIFECYCLE_STATUSES, ("live",)),
    "update_entity_cmd": (TERMINAL_ENTITY_LIFECYCLE_STATUSES, ("live",)),
    "add_relationship_cmd": (TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES, ("active", "inactive")),
    "update_relationship_cmd": (TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES, ("active", "inactive")),
}


def _lifecycle_status_help(command_name: str) -> str:
    command = getattr(mutations_module, command_name)
    for param in command.params:
        if "--lifecycle-status" in param.opts:
            return _flat(param.help or "")
    raise AssertionError(f"{command_name} has no --lifecycle-status option")


def test_mutation_cli_help_does_not_instruct_writing_terminal_lifecycle_statuses() -> None:
    """``--lifecycle-status`` help names what IS writable and calls the rest refused.

    The terminal statuses may still be *mentioned* (the option's click.Choice
    still accepts them syntactically, and the refusal is worth explaining), but
    the help must never read as an instruction to set one.
    """
    for command_name, (terminal, writable) in _LIFECYCLE_OPTION_HELP.items():
        help_text = _lifecycle_status_help(command_name)
        assert "refused" in help_text, command_name
        assert "wi-lifecycle-verbs" in help_text, command_name
        for status in writable:
            assert status in help_text, (command_name, status)
        for status in sorted(terminal):
            assert status in help_text, (command_name, status)

    source = mutations_module.__file__
    assert source is not None
    text = Path(source).read_text()
    instructions = re.findall(r"--lifecycle-status[`` ]+(\w+)", text)
    offenders = sorted(
        {
            status
            for status in instructions
            if status
            in TERMINAL_ENTITY_LIFECYCLE_STATUSES | TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES
        }
    )
    assert offenders == [], (
        "mutations.py help/docstrings still instruct writing terminal lifecycle "
        f"statuses: {offenders}"
    )


def test_mutation_cli_points_at_the_verbs_that_replace_terminal_writes() -> None:
    """Refusing without a route forward is a dead end; the docstrings name one."""
    for command_name in ("update_entity_cmd", "update_relationship_cmd"):
        doc = _flat(getattr(mutations_module, command_name).callback.__doc__ or "")
        assert "refused" in doc, command_name
        assert "wi-lifecycle-verbs" in doc, command_name
        assert "cruxible attest record" in doc, command_name
        assert "cruxible feedback" in doc, command_name
