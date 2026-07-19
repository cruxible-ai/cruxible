"""MCP layer of the agent-local working set: opt-in capture for read tools.

The MCP server process is a CLIENT of the daemon, co-located with the agent —
so working-set capture belongs here, and the daemon stays blind to it. The
record format, normalization, instance keys, and writer live in the
transport-neutral core, :mod:`cruxible_core.working_set` (shared with the CLI
capture hook in :mod:`cruxible_core.cli.working_set`).

Opt-in gate
    Capture happens only when ``CRUXIBLE_WORKING_SET_DIR`` is set to a
    non-empty directory path: entity/edge-shaped results flowing through the
    MCP read handlers (``cruxible_query``, ``cruxible_query_inline``,
    ``cruxible_get_entity``, ``cruxible_inspect_entity``, ``cruxible_list``,
    ``cruxible_sample``, ``cruxible_get_relationship``) are then ALSO
    captured as working-set records rooted at that directory. When the
    variable is unset, :func:`capture_tool_read` returns before doing ANY
    work — no serialization, no filesystem access, zero behavior or
    performance change.

Identity
    The hook sits at the handler dispatch seam, so both remote (HTTP client)
    and local modes are covered. Server mode derives the credential-scoped
    instance key from the server's bearer-token context (the same salted
    scope derivation the CLI uses); local mode keys on the resolved instance
    root. ``read_revision`` comes from the response payload envelope (all
    read surfaces carry it), with the local instance's current revision as
    the local-mode fallback.

Tamper honesty
    Same caveat as the core module: the cache files are same-user-writable
    by design; hygiene reduces accidents, not adversaries.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.config import resolve_server_settings
from cruxible_core.workflow.compiler import compute_lock_config_digest
from cruxible_core.working_set import (
    WORKING_SET_DIR_ENV,
    capture_read_payload,
    extract_read_records,
    local_instance_key,
    server_instance_key,
    working_set_warning,
)


def mcp_capture_enabled() -> bool:
    """Whether MCP working-set capture is opted in (env dir set, non-empty)."""
    return bool(os.environ.get(WORKING_SET_DIR_ENV, "").strip())


def capture_tool_read(result: Any, *, source_tool: str, instance_id: str) -> None:
    """Capture one MCP read-tool result into the working set (opt-in).

    Hard no-op when ``CRUXIBLE_WORKING_SET_DIR`` is unset: the gate is
    checked before any other work. When enabled, capture is a pure side
    effect after the handler produced its result — the result object is
    never mutated, and every capture failure is downgraded to a stderr
    warning so a read can never break on its own cache.
    """
    if not mcp_capture_enabled():
        return
    _capture(result, source_tool=source_tool, instance_id=instance_id)


def _capture(result: Any, *, source_tool: str, instance_id: str) -> None:
    try:
        payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
        # Cheap pre-check so record-free payloads never trigger digest
        # resolution (server mode resolves the digest with an extra read).
        if not extract_read_records(payload):
            return
        fallback_revision: int | None = None
        if resolve_server_settings().enabled:
            instance_key = server_instance_key(instance_id)
            config_digest = _server_config_digest(instance_id)
        else:
            instance = get_manager().get(instance_id)
            instance_key = local_instance_key(instance.get_root_path())
            try:
                fallback_revision = instance.get_read_revision()
            except Exception:
                fallback_revision = None
            try:
                config_digest = compute_lock_config_digest(instance.load_config())
            except Exception:
                config_digest = None
        capture_read_payload(
            payload,
            instance_key=instance_key,
            source_cmd=source_tool,
            fallback_revision=fallback_revision,
            config_digest=config_digest,
        )
    except Exception as exc:
        working_set_warning(f"working-set capture failed ({exc.__class__.__name__}: {exc})")


def _server_config_digest(instance_id: str) -> str | None:
    """Read the daemon's active config digest through the shared MCP client.

    Reuses the handler layer's cached client (imported lazily — handlers
    import this module). ``None`` when unresolvable; such records verify as
    ``unknown`` on the config axis.
    """
    try:
        from cruxible_core.mcp import handlers

        client = handlers._get_client()
        if client is None:
            return None
        provenance = client.config_status(instance_id).provenance
        return provenance.active_config_digest if provenance is not None else None
    except Exception:
        return None


__all__ = ["capture_tool_read", "mcp_capture_enabled"]
