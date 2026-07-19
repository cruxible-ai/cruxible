"""CLI layer of the agent-local working set: opt-in gate + capture hook.

The record format, normalization, instance keys, writer, and reader live in
the transport-neutral core, :mod:`cruxible_core.working_set` (shared with the
MCP capture hook in :mod:`cruxible_core.mcp.working_set`); this module adds
only what is CLI-specific:

- the opt-in gate (``CRUXIBLE_WORKING_SET=1`` env / per-command ``--ws``
  flag) — when neither gate is open every function here is a no-op: no file
  is created and read commands behave byte-for-byte identically;
- click-context resolution of the capture identity (server vs local mode,
  the same instance binding continuation tokens use); and
- :func:`capture_json_read`, the hook the ``--json`` read commands call after
  printing their payload.

See the core module's docstring for the cache contract — including the
tamper-honesty caveat: the cache files are same-user-writable by design, and
the permission/symlink hygiene reduces accidents, not adversaries. The core
names are re-exported here so existing CLI-side imports keep working.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import click

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.workflow.compiler import compute_lock_config_digest
from cruxible_core.working_set import (
    COMPACT_THRESHOLD_BYTES,
    HEADER_LINE,
    RecordReadResult,
    WorkingSetPathError,
    append_records,
    capture_read_payload,
    credential_scope,
    extract_read_records,
    iter_record_lines,
    local_instance_key,
    normalize_edge_record,
    normalize_entity_record,
    read_records,
    read_records_detailed,
    record_identity,
    record_wins,
    records_path,
    refuse_symlink_write,
    server_instance_key,
    validate_record,
    working_set_dir,
    working_set_warning,
    write_records,
)

WORKING_SET_ENV = "CRUXIBLE_WORKING_SET"

_ENV_TRUE = {"1", "true", "yes", "on"}

# Re-exported so the CLI surface keeps one import point for capture + verbs.
__all__ = [
    "COMPACT_THRESHOLD_BYTES",
    "HEADER_LINE",
    "WORKING_SET_ENV",
    "CaptureContext",
    "RecordReadResult",
    "WorkingSetPathError",
    "append_records",
    "capture_json_read",
    "capture_read_payload",
    "credential_scope",
    "extract_read_records",
    "iter_record_lines",
    "local_instance_key",
    "normalize_edge_record",
    "normalize_entity_record",
    "read_records",
    "read_records_detailed",
    "record_identity",
    "record_wins",
    "records_path",
    "refuse_symlink_write",
    "resolve_active_config_digest",
    "resolve_capture_context",
    "server_instance_key",
    "validate_record",
    "working_set_dir",
    "working_set_enabled",
    "working_set_warning",
    "write_records",
]


def working_set_enabled(ws_flag: bool = False) -> bool:
    """Return whether working-set capture is enabled for this invocation."""
    if ws_flag:
        return True
    return os.environ.get(WORKING_SET_ENV, "").strip().lower() in _ENV_TRUE


def _cli_root_obj() -> dict[str, Any]:
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return {}
    root = ctx.find_root()
    root.ensure_object(dict)
    obj = root.obj
    return obj if isinstance(obj, dict) else {}


@dataclass
class CaptureContext:
    """Resolved identity for one capture: records key + digest/revision sources."""

    instance_key: str
    local_instance: CruxibleInstance | None = None
    server_instance_id: str | None = None


def resolve_capture_context() -> CaptureContext | None:
    """Resolve the capture context for the current CLI invocation.

    Server mode returns the credential-scoped daemon instance key with no
    local instance; local mode loads the on-disk instance (cheap: one
    metadata file read). Returns ``None`` when no instance can be resolved —
    capture then no-ops.
    """
    obj = _cli_root_obj()
    if obj.get("server_url") or obj.get("server_socket"):
        instance_id = obj.get("instance_id")
        if not instance_id:
            return None
        return CaptureContext(
            instance_key=server_instance_key(str(instance_id)),
            server_instance_id=str(instance_id),
        )
    try:
        instance = CruxibleInstance.load()
    except Exception:
        return None
    return CaptureContext(
        instance_key=local_instance_key(instance.get_root_path()),
        local_instance=instance,
    )


def resolve_active_config_digest(context: CaptureContext) -> str | None:
    """Resolve the active config digest for stamping/verifying records.

    Local mode computes the same lock digest continuation tokens bind to;
    server mode reads the daemon's recorded active config digest via the
    config-status endpoint (one extra read per capture — acceptable for an
    opt-in cache). ``None`` when unresolvable; such records verify as
    ``unknown`` on the config axis.
    """
    if context.local_instance is not None:
        try:
            return compute_lock_config_digest(context.local_instance.load_config())
        except Exception:
            return None
    if context.server_instance_id is None:
        return None
    try:
        # Imported lazily: _common pulls in the full command surface.
        from cruxible_core.cli.commands._common import _get_client

        client = _get_client()
        if client is None:
            return None
        provenance = client.config_status(context.server_instance_id).provenance
        return provenance.active_config_digest if provenance is not None else None
    except Exception:
        return None


def capture_json_read(
    payload: Any,
    *,
    source_cmd: str,
    ws_flag: bool = False,
    read_revision: int | None = None,
) -> None:
    """Capture entity/edge-shaped data from an emitted ``--json`` read payload.

    A pure side effect: it runs AFTER the payload has been printed, never
    filters or mutates it, and swallows its own failures (warning on stderr).
    ``read_revision`` resolution order: the payload envelope's
    ``read_revision``, then the explicit argument (threaded from the parsed
    result object), then the local instance's current revision; ``None`` when
    all three are unavailable (recorded as-is; ``ws verify`` reports such
    records as ``unknown``).
    """
    if not working_set_enabled(ws_flag):
        return
    try:
        # Cheap pre-check so empty payloads never trigger context/digest
        # resolution (server mode resolves the digest with an extra read).
        if not extract_read_records(payload):
            return
        context = resolve_capture_context()
        if context is None:
            return
        fallback_revision: int | None = None
        if context.local_instance is not None:
            fallback_revision = context.local_instance.get_read_revision()
        capture_read_payload(
            payload,
            instance_key=context.instance_key,
            source_cmd=source_cmd,
            read_revision=read_revision,
            config_digest=resolve_active_config_digest(context),
            fallback_revision=fallback_revision,
        )
    except Exception as exc:  # pragma: no cover - capture must never break a read
        working_set_warning(f"working-set capture failed ({exc.__class__.__name__}: {exc})")
