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

#: The everyday agent loop: map state, read it, write it, and settle. Agents read
#: state only through orient, query and get; the full grammar (query_spec, the
#: since change feed, authoring, Procedures, Lines, curation) is in the full
#: profile.
_DEFAULT_TOOLS = frozenset(
    {
        # the three read verbs and the work queue
        "cruxible_playbill_orient",
        "cruxible_playbill_query",
        "cruxible_playbill_get",
        "cruxible_playbill_next",
        # the write verbs; the authoring_* intent tools stay in the full profile
        "cruxible_playbill_set",
        "cruxible_playbill_retire",
        "cruxible_playbill_write",
        # proposals through activation
        "cruxible_playbill_proposal_list",
        "cruxible_playbill_review",
        "cruxible_playbill_approve",
        "cruxible_playbill_activate",
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
