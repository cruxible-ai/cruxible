"""Guardrails on what we CLAIM about permission tiers and read side effects.

Two claims went stale and were quietly load-bearing:

* the MCP ``BASE_INSTRUCTIONS`` told agents ``READ_ONLY`` performs no
  persistence (query and gate-check both write receipt rows) and attributed
  config mutation to ``ADMIN`` alone (constraints and decision policies are
  governed-write config additions);
* the docs presented ``CRUXIBLE_MODE`` as gating every session, when it is a
  boundary only on the daemon and MCP surfaces — the local CLI reads the
  operator's own environment and is an operator console by design.

These pin the corrected statements so the next edit has to keep them true.
"""

from __future__ import annotations

import re
from pathlib import Path

from cruxible_core.mcp.server import BASE_INSTRUCTIONS
from cruxible_core.runtime import permissions as permissions_module

REPO_ROOT = Path(__file__).resolve().parents[2]

_OPERATOR_CONSOLE_CLAIM = "operator console at operator tier by design"


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
