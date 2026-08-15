"""Reviewed intent descriptions for the Playbill MCP surface."""

from __future__ import annotations

from cruxible_core.errors import ConfigError

TOOL_PROMPT_STYLE_RULE = (
    'Tool descriptions must start with "Use when", name the user intent first, '
    "and avoid implementation details that do not help a client choose a tool."
)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "cruxible_version": "Use when you need to confirm which cruxible build is running.",
    "cruxible_server_info": (
        "Use when you need live daemon version, state-directory, authentication, "
        "or instance-count information."
    ),
    "cruxible_playbill_host_create": (
        "Use when you need an empty daemon-owned host before Playbill bootstrap; "
        "this adopts no config or semantic state."
    ),
    "cruxible_playbill_init": (
        "Use when you need to bootstrap Playbill from client-generated public keys."
    ),
    "cruxible_playbill_store_body": (
        "Use when you need to store exact Document bytes inertly before proposing them."
    ),
    "cruxible_playbill_propose_document": (
        "Use when you need to propose a governed Document create or supersession."
    ),
    "cruxible_playbill_inspect_proposal": (
        "Use when you need immutable proposal evaluation and candidate evidence."
    ),
    "cruxible_playbill_inspect_refusal": (
        "Use when you need typed admission or acceptance-law diagnostics for a proposal."
    ),
    "cruxible_playbill_review": (
        "Use when you need a structured candidate review and permission-filtered diff."
    ),
    "cruxible_playbill_prepare_approval": (
        "Use when a client-held signer needs the exact immutable approval statement."
    ),
    "cruxible_playbill_submit_approval": (
        "Use when you have a public approval attestation produced outside the daemon."
    ),
    "cruxible_playbill_activate": ("Use when an approved Playbill candidate is ready to settle."),
    "cruxible_playbill_list_documents": (
        "Use when you need accepted Documents and their exact coordinate."
    ),
    "cruxible_playbill_get_document": (
        "Use when you need one accepted Document envelope and structured facts."
    ),
    "cruxible_playbill_dereference": (
        "Use when you need verified accepted body bytes and have body-read permission."
    ),
    "cruxible_playbill_history": (
        "Use when you need one Document's replay-verified accepted history."
    ),
    "cruxible_playbill_explain": (
        "Use when you need coordinate-bound governance, provenance, and attestation coverage."
    ),
    "cruxible_playbill_source_context": (
        "Use when a local client needs path-free accepted inputs before compiling sources."
    ),
    "cruxible_playbill_check_source_bundle": (
        "Use when you need to compare compiled source bytes with accepted state."
    ),
    "cruxible_playbill_propose_source_bundle": (
        "Use when you need to propose frozen source bytes without sending a local path."
    ),
    "cruxible_playbill_list_principals": (
        "Use when you need accepted public principal records and their coordinate."
    ),
    "cruxible_playbill_propose_principal_change": (
        "Use when you need a governed principal registration, rotation, revocation, or recovery."
    ),
}


def tool_description(tool_name: str) -> str:
    try:
        return TOOL_DESCRIPTIONS[tool_name]
    except KeyError as exc:
        raise ConfigError(f"MCP tool '{tool_name}' is missing a prompt description") from exc


__all__ = [
    "TOOL_DESCRIPTIONS",
    "TOOL_PROMPT_STYLE_RULE",
    "tool_description",
]
