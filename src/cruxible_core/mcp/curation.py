"""Advertised MCP curation for the Playbill-only tool set."""

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
    PROFILE_REVIEW: PROFILE_REVIEW,
}

_COMMON_READS = {
    "cruxible_version",
    "cruxible_server_info",
    "cruxible_playbill_inspect_proposal",
    "cruxible_playbill_inspect_refusal",
    "cruxible_playbill_review",
    "cruxible_playbill_prepare_approval",
    "cruxible_playbill_list_documents",
    "cruxible_playbill_get_document",
    "cruxible_playbill_dereference",
    "cruxible_playbill_history",
    "cruxible_playbill_explain",
    "cruxible_playbill_source_context",
    "cruxible_playbill_check_source_bundle",
    "cruxible_playbill_list_principals",
    "cruxible_playbill_list_subjects",
    "cruxible_playbill_get_subject",
    "cruxible_playbill_subject_history",
    "cruxible_playbill_list_claim_types",
    "cruxible_playbill_get_claim_type",
    "cruxible_playbill_list_claims",
    "cruxible_playbill_get_claim",
    "cruxible_playbill_claim_history",
    "cruxible_playbill_explain_claim",
    "cruxible_playbill_list_query_definitions",
    "cruxible_playbill_get_query_definition",
    "cruxible_playbill_run_query",
    "cruxible_playbill_discover",
    "cruxible_playbill_expand",
    "cruxible_playbill_export_floor",
    "cruxible_playbill_resolve_coverage",
    "cruxible_playbill_authoring_get",
    "cruxible_playbill_authoring_resume",
    "cruxible_playbill_authoring_list_pending",
    "cruxible_playbill_authoring_status",
}

_PROFILE_TOOLS: dict[str, frozenset[str] | None] = {
    PROFILE_FULL: None,
    PROFILE_STATE_AUTHORING: frozenset(
        _COMMON_READS
        | {
            "cruxible_playbill_store_body",
            "cruxible_playbill_propose_document",
            "cruxible_playbill_propose_source_bundle",
            "cruxible_playbill_propose_subject",
            "cruxible_playbill_propose_claim_type",
            "cruxible_playbill_propose_claim",
            "cruxible_playbill_propose_claims",
            "cruxible_playbill_propose_query_definition",
            "cruxible_playbill_authoring_create",
            "cruxible_playbill_authoring_compile",
            "cruxible_playbill_authoring_preflight",
            "cruxible_playbill_authoring_submit",
        }
    ),
    PROFILE_REVIEW: frozenset(
        _COMMON_READS
        | {
            "cruxible_playbill_submit_approval",
            "cruxible_playbill_activate",
        }
    ),
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
