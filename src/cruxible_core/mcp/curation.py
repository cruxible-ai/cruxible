"""Advertised MCP tool-surface curation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from cruxible_core.errors import ConfigError
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode

PROFILE_FULL = "full"
PROFILE_STATE_AUTHORING = "state_authoring"
PROFILE_REVIEW = "review"

_PROFILE_ALIASES = {
    "all": PROFILE_FULL,
    "default": PROFILE_FULL,
    PROFILE_FULL: PROFILE_FULL,
    "state-authoring": PROFILE_STATE_AUTHORING,
    PROFILE_STATE_AUTHORING: PROFILE_STATE_AUTHORING,
    "review": PROFILE_REVIEW,
}

_PROFILE_TOOLS: dict[str, frozenset[str] | None] = {
    PROFILE_FULL: None,
    PROFILE_STATE_AUTHORING: frozenset(
        {
            "cruxible_version",
            "cruxible_server_info",
            "cruxible_init",
            "cruxible_validate",
            "cruxible_schema",
            "cruxible_query",
            "cruxible_query_inline",
            "cruxible_list_queries",
            "cruxible_describe_query",
            "cruxible_receipt",
            "cruxible_get_trace",
            "cruxible_list_traces",
            "cruxible_list",
            "cruxible_sample",
            "cruxible_evaluate",
            "cruxible_stats",
            "cruxible_lint",
            "cruxible_get_entity",
            "cruxible_get_relationship",
            "cruxible_relationship_lineage",
            "cruxible_inspect_entity",
            "cruxible_inspect_entity_history",
            "cruxible_inspect_ontology",
            "cruxible_inspect_workflows",
            "cruxible_inspect_queries",
            "cruxible_inspect_governance",
            "cruxible_inspect_overview",
            "cruxible_plan_workflow",
            "cruxible_run_workflow",
            "cruxible_test_workflow",
            "cruxible_add_entity",
            "cruxible_add_relationship",
            "cruxible_batch_direct_write",
            "cruxible_apply_workflow",
            "cruxible_lock_workflow",
            "cruxible_reload_config",
        }
    ),
    PROFILE_REVIEW: frozenset(
        {
            "cruxible_version",
            "cruxible_server_info",
            "cruxible_schema",
            "cruxible_query",
            "cruxible_query_inline",
            "cruxible_list_queries",
            "cruxible_describe_query",
            "cruxible_receipt",
            "cruxible_get_entity",
            "cruxible_get_relationship",
            "cruxible_relationship_lineage",
            "cruxible_inspect_entity",
            "cruxible_inspect_entity_history",
            "cruxible_inspect_governance",
            "cruxible_get_group",
            "cruxible_group_status",
            "cruxible_list_groups",
            "cruxible_list_resolutions",
            "cruxible_get_feedback_profile",
            "cruxible_get_outcome_profile",
            "cruxible_analyze_feedback",
            "cruxible_analyze_outcomes",
            "cruxible_feedback",
            "cruxible_feedback_batch",
            "cruxible_feedback_from_query",
            "cruxible_outcome",
            "cruxible_propose_group",
            "cruxible_resolve_group",
            "cruxible_update_trust_status",
        }
    ),
}


@dataclass(frozen=True)
class ToolCuration:
    """Resolved advertised tool-surface curation."""

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
    """Resolve MCP list-surface curation from environment."""
    env = environ or os.environ
    raw_profile = env.get("CRUXIBLE_MCP_PROFILE", PROFILE_FULL).strip().lower()
    profile = _PROFILE_ALIASES.get(raw_profile)
    if profile is None:
        valid = ", ".join(sorted(_PROFILE_ALIASES))
        raise ConfigError(f"Invalid CRUXIBLE_MCP_PROFILE='{raw_profile}'. Valid values: {valid}")
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
    """Return tool names that should appear in MCP tools/list."""
    unknown_registered = registered_tools - set(TOOL_PERMISSIONS)
    if unknown_registered:
        raise ConfigError(
            f"Registered tools without permission entry: {sorted(unknown_registered)}"
        )

    visible = {name for name in registered_tools if mode >= TOOL_PERMISSIONS[name]}

    profile_tools = _PROFILE_TOOLS[curation.profile]
    if profile_tools is not None:
        unknown_profile_tools = profile_tools - set(TOOL_PERMISSIONS)
        if unknown_profile_tools:
            raise ConfigError(
                f"MCP profile '{curation.profile}' references unknown tools: "
                f"{sorted(unknown_profile_tools)}"
            )
        visible &= profile_tools

    if curation.allowlist is not None:
        unknown_allowlist_tools = curation.allowlist - registered_tools
        if unknown_allowlist_tools:
            raise ConfigError(
                f"CRUXIBLE_MCP_TOOLS references unknown tools: {sorted(unknown_allowlist_tools)}"
            )
        visible &= curation.allowlist

    return visible


__all__ = [
    "PROFILE_FULL",
    "PROFILE_REVIEW",
    "PROFILE_STATE_AUTHORING",
    "ToolCuration",
    "advertised_tool_names",
    "resolve_tool_curation",
]
