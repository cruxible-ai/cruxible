"""Runtime permission modes for Cruxible operations.

Controls which operations a runtime session can invoke, enforced via the
``CRUXIBLE_MODE`` environment variable. Four cumulative tiers:

- ``READ_ONLY``: query, inspect, validate, and plan workflows
- ``GOVERNED_WRITE``: execute governed operator actions such as feedback,
  proposals, snapshots, policy additions, and subscribed state pulls
- ``GRAPH_WRITE``: commit local governed state through direct graph writes,
  group resolution, trust updates, or canonical workflow apply
- ``ADMIN``: manage instance lifecycle, active config replacement, locks,
  clones, overlays, and published state trust boundaries

Default is ``ADMIN`` (backward compatible) when ``CRUXIBLE_MODE`` is unset.

Audit logging uses structlog to stderr so it never interferes with the
MCP stdio transport on stdout. A safe stderr default is configured at
module level (guarded by ``if not structlog.is_configured()``) so audit
logs work even without an explicit ``configure_structlog()`` call.
Production JSON formatting is set by ``server.main()``.
"""

from __future__ import annotations

import contextvars
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from enum import IntEnum
from pathlib import Path

import structlog

from cruxible_core.errors import ConfigError, InstanceScopeError, PermissionDeniedError

# ---------------------------------------------------------------------------
# Safe stderr default for structlog — never write to stdout (MCP stdio)
# ---------------------------------------------------------------------------
if not structlog.is_configured():
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )

_log = structlog.get_logger("cruxible.permissions")


# ---------------------------------------------------------------------------
# Permission mode enum
# ---------------------------------------------------------------------------


class PermissionMode(IntEnum):
    """Cumulative permission tiers: ADMIN ⊃ GRAPH_WRITE ⊃ GOVERNED_WRITE ⊃ READ_ONLY."""

    READ_ONLY = 1
    GOVERNED_WRITE = 2
    GRAPH_WRITE = 3
    ADMIN = 4


_MODE_NAMES: dict[str, PermissionMode] = {
    "read_only": PermissionMode.READ_ONLY,
    "governed_write": PermissionMode.GOVERNED_WRITE,
    "graph_write": PermissionMode.GRAPH_WRITE,
    "admin": PermissionMode.ADMIN,
}

# ---------------------------------------------------------------------------
# Tool → minimum permission tier
# ---------------------------------------------------------------------------

TOOL_PERMISSIONS: dict[str, PermissionMode] = {
    # READ_ONLY tools do not mutate graph/state. Some may still append
    # decision-event audit metadata when an explicit decision_record_id is supplied.
    "cruxible_version": PermissionMode.READ_ONLY,
    "cruxible_server_info": PermissionMode.READ_ONLY,
    "cruxible_init": PermissionMode.READ_ONLY,
    "cruxible_validate": PermissionMode.READ_ONLY,
    "cruxible_schema": PermissionMode.READ_ONLY,
    "cruxible_query": PermissionMode.READ_ONLY,
    "cruxible_query_inline": PermissionMode.READ_ONLY,
    "cruxible_list_queries": PermissionMode.READ_ONLY,
    "cruxible_describe_query": PermissionMode.READ_ONLY,
    "cruxible_receipt": PermissionMode.READ_ONLY,
    "cruxible_get_trace": PermissionMode.READ_ONLY,
    "cruxible_list_traces": PermissionMode.READ_ONLY,
    "cruxible_list": PermissionMode.READ_ONLY,
    "cruxible_sample": PermissionMode.READ_ONLY,
    "cruxible_evaluate": PermissionMode.READ_ONLY,
    "cruxible_stats": PermissionMode.READ_ONLY,
    "cruxible_lint": PermissionMode.READ_ONLY,
    "cruxible_get_entity": PermissionMode.READ_ONLY,
    "cruxible_get_relationship": PermissionMode.READ_ONLY,
    "cruxible_relationship_lineage": PermissionMode.READ_ONLY,
    "cruxible_inspect_entity": PermissionMode.READ_ONLY,
    "cruxible_inspect_entity_history": PermissionMode.READ_ONLY,
    "cruxible_inspect_ontology": PermissionMode.READ_ONLY,
    "cruxible_inspect_workflows": PermissionMode.READ_ONLY,
    "cruxible_inspect_queries": PermissionMode.READ_ONLY,
    "cruxible_inspect_governance": PermissionMode.READ_ONLY,
    "cruxible_inspect_overview": PermissionMode.READ_ONLY,
    "cruxible_render_wiki": PermissionMode.READ_ONLY,
    "cruxible_get_group": PermissionMode.READ_ONLY,
    "cruxible_group_status": PermissionMode.READ_ONLY,
    "cruxible_list_groups": PermissionMode.READ_ONLY,
    "cruxible_list_resolutions": PermissionMode.READ_ONLY,
    "cruxible_get_feedback_profile": PermissionMode.READ_ONLY,
    "cruxible_get_outcome_profile": PermissionMode.READ_ONLY,
    "cruxible_analyze_feedback": PermissionMode.READ_ONLY,
    "cruxible_analyze_outcomes": PermissionMode.READ_ONLY,
    "cruxible_get_decision_record": PermissionMode.READ_ONLY,
    "cruxible_list_decision_records": PermissionMode.READ_ONLY,
    "cruxible_list_decision_events": PermissionMode.READ_ONLY,
    "cruxible_state_status": PermissionMode.READ_ONLY,
    "cruxible_state_pull_preview": PermissionMode.READ_ONLY,
    "cruxible_list_snapshots": PermissionMode.READ_ONLY,
    "cruxible_dereference_source_evidence": PermissionMode.READ_ONLY,
    "cruxible_plan_workflow": PermissionMode.READ_ONLY,
    # GOVERNED_WRITE tools
    "cruxible_feedback": PermissionMode.GOVERNED_WRITE,
    "cruxible_feedback_batch": PermissionMode.GOVERNED_WRITE,
    "cruxible_feedback_from_query": PermissionMode.GOVERNED_WRITE,
    "cruxible_outcome": PermissionMode.GOVERNED_WRITE,
    "cruxible_run_workflow": PermissionMode.GOVERNED_WRITE,
    "cruxible_test_workflow": PermissionMode.GOVERNED_WRITE,
    "cruxible_propose_workflow": PermissionMode.GOVERNED_WRITE,
    "cruxible_propose_group": PermissionMode.GOVERNED_WRITE,
    "cruxible_create_decision_record": PermissionMode.GOVERNED_WRITE,
    "cruxible_finalize_decision_record": PermissionMode.GOVERNED_WRITE,
    "cruxible_abandon_decision_record": PermissionMode.GOVERNED_WRITE,
    "cruxible_add_constraint": PermissionMode.GOVERNED_WRITE,
    "cruxible_add_decision_policy": PermissionMode.GOVERNED_WRITE,
    "cruxible_create_snapshot": PermissionMode.GOVERNED_WRITE,
    "cruxible_state_pull_apply": PermissionMode.GOVERNED_WRITE,
    "cruxible_register_source_artifact": PermissionMode.GOVERNED_WRITE,
    # GRAPH_WRITE tools
    "cruxible_add_entity": PermissionMode.GRAPH_WRITE,
    "cruxible_add_relationship": PermissionMode.GRAPH_WRITE,
    "cruxible_batch_direct_write": PermissionMode.GRAPH_WRITE,
    "cruxible_apply_workflow": PermissionMode.GRAPH_WRITE,
    "cruxible_resolve_group": PermissionMode.GRAPH_WRITE,
    "cruxible_update_trust_status": PermissionMode.GRAPH_WRITE,
    # ADMIN tools
    "cruxible_lock_workflow": PermissionMode.ADMIN,
    "cruxible_reload_config": PermissionMode.ADMIN,
    "cruxible_clone_snapshot": PermissionMode.ADMIN,
    "cruxible_instance_snapshot": PermissionMode.ADMIN,
    "cruxible_instance_restore": PermissionMode.ADMIN,
    "cruxible_instance_relocate": PermissionMode.ADMIN,
    "cruxible_state_publish": PermissionMode.ADMIN,
    "cruxible_state_create_overlay": PermissionMode.ADMIN,
}

# Internal runtime operations that are not registered MCP tools but still need
# permission gates owned by this module.
RUNTIME_OPERATION_PERMISSIONS: dict[str, PermissionMode] = {
    "cruxible_governed_instance_lifecycle": PermissionMode.ADMIN,
    "cruxible_hosted_instance_init": PermissionMode.ADMIN,
    "cruxible_init_with_config": PermissionMode.ADMIN,
    "cruxible_runtime_credentials": PermissionMode.ADMIN,
    "cruxible_server_restart": PermissionMode.ADMIN,
}

PERMISSION_REQUIREMENTS: dict[str, PermissionMode] = {
    **TOOL_PERMISSIONS,
    **RUNTIME_OPERATION_PERMISSIONS,
}

# ---------------------------------------------------------------------------
# Cached state
# ---------------------------------------------------------------------------

_cached_mode: PermissionMode | None = None
_cached_allowed_roots: list[Path] | None | bool = False  # False = not yet parsed

# Per-request override (for cloud / multi-tenant use)
_request_mode: contextvars.ContextVar[PermissionMode | None] = contextvars.ContextVar(
    "cruxible_permission_mode", default=None
)
_request_instance_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cruxible_instance_scope", default=None
)


# ---------------------------------------------------------------------------
# Initialization and caching
# ---------------------------------------------------------------------------


def validate_allowed_roots() -> list[Path] | None:
    """Parse and validate ``CRUXIBLE_ALLOWED_ROOTS`` at startup.

    Returns ``None`` if the env var is unset.
    Raises :class:`ConfigError` for empty lists or relative paths.
    """
    raw = os.environ.get("CRUXIBLE_ALLOWED_ROOTS")
    if raw is None:
        return None
    paths = [p.strip() for p in raw.split(",") if p.strip()]
    if not paths:
        raise ConfigError("CRUXIBLE_ALLOWED_ROOTS is set but empty")
    result: list[Path] = []
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            raise ConfigError(f"CRUXIBLE_ALLOWED_ROOTS contains relative path: '{p}'")
        result.append(path.resolve())
    return result


def init_permissions(mode: PermissionMode | None = None) -> PermissionMode:
    """Read ``CRUXIBLE_MODE`` env var and cache the result.

    Args:
        mode: Override for testing. If provided, skips env var lookup.

    Returns:
        The resolved :class:`PermissionMode`.

    Raises:
        ConfigError: If the env var contains an invalid value.
    """
    global _cached_mode, _cached_allowed_roots

    if mode is not None:
        _cached_mode = mode
    else:
        raw = os.environ.get("CRUXIBLE_MODE")
        if raw is None:
            _cached_mode = PermissionMode.ADMIN
        else:
            resolved = _MODE_NAMES.get(raw.lower())
            if resolved is None:
                valid = ", ".join(sorted(_MODE_NAMES))
                raise ConfigError(f"Invalid CRUXIBLE_MODE='{raw}'. Valid values: {valid}")
            _cached_mode = resolved

    # Parse allowed roots (fail-fast on bad config)
    _cached_allowed_roots = validate_allowed_roots()

    return _cached_mode


def get_current_mode() -> PermissionMode:
    """Return the active permission mode.

    Checks (in order):
    1. Request-scoped contextvar (set via :func:`request_permission_scope`)
    2. Module-level cached mode (from env var / :func:`init_permissions`)
    """
    request = _request_mode.get()
    if request is not None:
        return request

    global _cached_mode
    if _cached_mode is None:
        init_permissions()
    assert _cached_mode is not None
    return _cached_mode


def reset_permissions() -> None:
    """Clear cached mode, allowed roots, and request scope. Used for test isolation."""
    global _cached_mode, _cached_allowed_roots
    _cached_mode = None
    _cached_allowed_roots = False
    _request_mode.set(None)
    _request_instance_scope.set(None)


@contextmanager
def request_permission_scope(mode: PermissionMode) -> Iterator[None]:
    """Temporarily override the permission mode for the current context.

    Uses token-based reset so nested scopes restore correctly.
    """
    token = _request_mode.set(mode)
    try:
        yield
    finally:
        _request_mode.reset(token)


@contextmanager
def request_instance_scope(instance_id: str | None) -> Iterator[None]:
    """Temporarily bind an instance scope for the current request."""
    token = _request_instance_scope.set(instance_id)
    try:
        yield
    finally:
        _request_instance_scope.reset(token)


# ---------------------------------------------------------------------------
# Permission check
# ---------------------------------------------------------------------------


def check_permission(
    tool_name: str,
    *,
    instance_id: str | None = None,
    enforce_instance_scope: bool = True,
) -> None:
    """Check whether the current mode permits calling *tool_name*.

    Args:
        tool_name: The operation or tool being called.
        instance_id: Optional instance ID for audit logging and scope enforcement.
        enforce_instance_scope: Whether to reject when request credentials are scoped
            to a different instance. Disable only for legacy root-dir lifecycle checks
            that authorize scope before calling the runtime facade.

    Raises:
        PermissionDeniedError: If the current mode is insufficient.
    """
    current = get_current_mode()
    if tool_name not in PERMISSION_REQUIREMENTS:
        raise ConfigError(f"Tool '{tool_name}' has no entry in permission requirements")
    effective = PERMISSION_REQUIREMENTS[tool_name]

    if current < effective:
        _log.warning(
            "permission_denied",
            tool=tool_name,
            mode=current.name,
            required=effective.name,
            instance_id=instance_id,
        )
        raise PermissionDeniedError(tool_name, current.name, effective.name)

    credential_scope = _request_instance_scope.get()
    if (
        enforce_instance_scope
        and credential_scope is not None
        and instance_id is not None
        and instance_id != credential_scope
    ):
        _log.warning(
            "instance_scope_denied",
            tool=tool_name,
            instance_id=instance_id,
            credential_scope=credential_scope,
        )
        raise InstanceScopeError(instance_id, credential_scope)

    # Audit log for mutations
    if effective >= PermissionMode.GOVERNED_WRITE:
        _log.info(
            "mutation_allowed",
            tool=tool_name,
            mode=current.name,
            instance_id=instance_id,
        )


def require_unscoped_operator(operation: str) -> None:
    """Require an unscoped operator credential for a daemon-wide operation.

    Some operations act on the whole shared daemon rather than a single instance
    (re-exec/restart, global server metadata, restore before the target instance
    is known). On a shared multi-tenant daemon, an *instance-scoped* ADMIN
    credential — one bound to a single tenant's instance — must not be able to
    perform these daemon-wide operations: that is a cross-tenant escalation
    (e.g. one tenant restarting the daemon hosting every tenant, a DoS).

    Authorization rule, expressed against the request-scoped credential binding:

    * No bound scope (``None``) → ALLOW. This covers two legitimate cases that
      are indistinguishable to the runtime and both safe here:
        - auth-off / single-tenant local daemon (no credential context at all);
        - an unscoped operator / bootstrap credential (the bootstrap secret
          presents with ``instance_scope=None``).
    * A bound instance scope (any non-``None`` value) → REJECT. Every persisted
      runtime credential is bound to exactly one instance, so a non-``None`` scope
      is always an instance-scoped credential reaching for a daemon-wide lever.

    This is intentionally *additive* to :func:`check_permission`: callers still run
    the ADMIN tier check; this adds the scope-boundary gate that the tier check
    cannot express because these operations carry no ``instance_id`` to compare.

    Args:
        operation: Operation label used for the audit log and the denial message.

    Raises:
        InstanceScopeError: If the request presents an instance-scoped credential.
    """
    credential_scope = _request_instance_scope.get()
    if credential_scope is not None:
        _log.warning(
            "daemon_operation_scope_denied",
            operation=operation,
            credential_scope=credential_scope,
        )
        raise InstanceScopeError(operation, credential_scope)


# ---------------------------------------------------------------------------
# Root directory sandboxing
# ---------------------------------------------------------------------------


def validate_root_dir(root_dir: str) -> None:
    """Validate *root_dir* against ``CRUXIBLE_ALLOWED_ROOTS`` if set."""
    global _cached_allowed_roots
    # Ensure allowed roots are parsed
    if _cached_allowed_roots is False:
        _cached_allowed_roots = validate_allowed_roots()

    allowed = _cached_allowed_roots
    if allowed is None:
        return  # No restriction — backward compatible
    if not isinstance(allowed, list):
        return  # Not yet parsed — should not happen after init

    resolved = Path(root_dir).resolve()
    if not any(resolved == a or a in resolved.parents for a in allowed):
        _log.warning(
            "root_dir_denied",
            root_dir=root_dir,
            allowed_roots=[str(a) for a in allowed],
        )
        raise ConfigError(f"root_dir '{root_dir}' is not under any allowed root")


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def validate_tool_permissions(registered_tools: list[str]) -> None:
    """Enforce exact set equality between registered tools and permission map.

    Args:
        registered_tools: Tool names registered on the FastMCP server.

    Raises:
        ConfigError: If there are ungated tools or stale permission entries.
    """
    registered = set(registered_tools)
    permitted = set(TOOL_PERMISSIONS.keys())

    ungated = registered - permitted
    stale = permitted - registered

    errors: list[str] = []
    if ungated:
        errors.append(f"Tools registered without permission entry: {sorted(ungated)}")
    if stale:
        errors.append(f"Permission entries without registered tool: {sorted(stale)}")

    if errors:
        raise ConfigError("Tool permission validation failed", errors=errors)
