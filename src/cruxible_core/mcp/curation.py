"""Advertised MCP curation for the Playbill-only tool set."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from cruxible_core.errors import ConfigError
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode

PROFILE_FULL = "full"
PROFILE_DEFAULT = "default"

_PROFILES = (PROFILE_DEFAULT, PROFILE_FULL)

#: The everyday agent loop: orient and pick work, read Claims, write, and settle.
_DEFAULT_TOOLS = frozenset(
    {
        # orient (search mode=orient), next, search, and expand
        "cruxible_playbill_search",
        "cruxible_playbill_next",
        "cruxible_playbill_expand",
        # Claim, ClaimType, and Subject reads
        "cruxible_playbill_claim_values",
        "cruxible_playbill_list_claims",
        "cruxible_playbill_get_claim",
        "cruxible_playbill_get",
        "cruxible_playbill_explain_claim",
        "cruxible_playbill_list_claim_types",
        "cruxible_playbill_get_claim_type",
        "cruxible_playbill_list_subjects",
        "cruxible_playbill_get_subject",
        "cruxible_playbill_run_query",
        # the query read verb
        "cruxible_playbill_query",
        # the write verbs; the authoring_* intent tools stay in the full profile
        "cruxible_playbill_set",
        "cruxible_playbill_retire",
        "cruxible_playbill_write",
        # proposals through activation
        "cruxible_playbill_proposal_list",
        "cruxible_playbill_review",
        "cruxible_playbill_approve",
        "cruxible_playbill_activate",
        # the orient map
        "cruxible_playbill_orient",
        # identity and versions
        "cruxible_playbill_whoami",
        "cruxible_server_info",
    }
)

_PROFILE_TOOLS: dict[str, frozenset[str] | None] = {
    PROFILE_FULL: None,
    PROFILE_DEFAULT: _DEFAULT_TOOLS,
}


@dataclass(frozen=True)
class ToolCuration:
    profile: str
    allowlist: frozenset[str] | None = None

    @property
    def active(self) -> bool:
        return self.profile != PROFILE_FULL or self.allowlist is not None


def _parse_tool_list(raw: str | None) -> frozenset[str] | None:
    if raw is None:
        return None
    names = frozenset(name.strip() for name in raw.split(",") if name.strip())
    if not names:
        raise ConfigError("CRUXIBLE_MCP_TOOLS is set but empty")
    return names


def resolve_tool_curation(
    environ: Mapping[str, str] | None = None,
) -> ToolCuration:
    env = environ or os.environ
    profile = env.get("CRUXIBLE_MCP_PROFILE", PROFILE_DEFAULT).strip().lower()
    if profile not in _PROFILES:
        valid = ", ".join(_PROFILES)
        raise ConfigError(f"Invalid CRUXIBLE_MCP_PROFILE='{profile}'. Valid values: {valid}")
    allowlist = _parse_tool_list(
        env.get("CRUXIBLE_MCP_TOOLS") or env.get("CRUXIBLE_MCP_TOOL_ALLOWLIST")
    )
    return ToolCuration(profile=profile, allowlist=allowlist)


def advertised_tool_names(
    *,
    mode: PermissionMode,
    registered_tools: set[str],
    curation: ToolCuration,
) -> set[str]:
    permitted = {name for name in registered_tools if mode >= TOOL_PERMISSIONS[name]}
    profile_tools = _PROFILE_TOOLS[curation.profile]
    if profile_tools is not None:
        permitted &= set(profile_tools)
    if curation.allowlist is not None:
        unknown = set(curation.allowlist) - registered_tools
        if unknown:
            raise ConfigError(f"Unknown MCP tools in allowlist: {sorted(unknown)}")
        permitted &= set(curation.allowlist)
    return permitted


def session_tool_names() -> frozenset[str]:
    """The tools this MCP process advertises to its caller, for surface-aware repairs."""

    from cruxible_core.runtime.permissions import get_current_mode

    return frozenset(
        advertised_tool_names(
            mode=get_current_mode(),
            registered_tools=set(TOOL_PERMISSIONS),
            curation=resolve_tool_curation(),
        )
    )
