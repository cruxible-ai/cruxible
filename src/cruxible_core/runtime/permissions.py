"""Transport permission ceilings for the Playbill daemon and MCP surface.

The process fixes a ceiling from ``CRUXIBLE_MODE`` at startup. Request
credentials may narrow it but can never raise it. These tiers control endpoint
reachability only; Playbill principals and acceptance laws determine semantic
authority.

The names remain ``READ_ONLY``, ``GOVERNED_WRITE``, ``GRAPH_WRITE``, and
``ADMIN`` during the destructive pivot so existing daemon credential records
remain intelligible. The local CLI is still an operator process, not a sandbox
against the shell user. Audit logging goes to stderr so MCP stdio stays clean.
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

PERMISSION_MODE_NAMES: tuple[str, ...] = tuple(_MODE_NAMES)

# ---------------------------------------------------------------------------
# Tool → minimum permission tier
# ---------------------------------------------------------------------------

TOOL_PERMISSIONS: dict[str, PermissionMode] = {
    "cruxible_version": PermissionMode.READ_ONLY,
    "cruxible_server_info": PermissionMode.READ_ONLY,
    "cruxible_playbill_inspect_proposal": PermissionMode.READ_ONLY,
    "cruxible_playbill_inspect_refusal": PermissionMode.READ_ONLY,
    "cruxible_playbill_review": PermissionMode.READ_ONLY,
    "cruxible_playbill_prepare_approval": PermissionMode.READ_ONLY,
    "cruxible_playbill_explain": PermissionMode.READ_ONLY,
    "cruxible_playbill_list_documents": PermissionMode.READ_ONLY,
    "cruxible_playbill_get_document": PermissionMode.READ_ONLY,
    "cruxible_playbill_history": PermissionMode.READ_ONLY,
    "cruxible_playbill_source_context": PermissionMode.READ_ONLY,
    "cruxible_playbill_check_source_bundle": PermissionMode.READ_ONLY,
    "cruxible_playbill_list_principals": PermissionMode.READ_ONLY,
    "cruxible_playbill_list_subjects": PermissionMode.READ_ONLY,
    "cruxible_playbill_get_subject": PermissionMode.READ_ONLY,
    "cruxible_playbill_subject_history": PermissionMode.READ_ONLY,
    "cruxible_playbill_list_claim_types": PermissionMode.READ_ONLY,
    "cruxible_playbill_get_claim_type": PermissionMode.READ_ONLY,
    "cruxible_playbill_list_claims": PermissionMode.READ_ONLY,
    "cruxible_playbill_get_claim": PermissionMode.READ_ONLY,
    "cruxible_playbill_claim_history": PermissionMode.READ_ONLY,
    "cruxible_playbill_explain_claim": PermissionMode.READ_ONLY,
    "cruxible_playbill_list_query_definitions": PermissionMode.READ_ONLY,
    "cruxible_playbill_get_query_definition": PermissionMode.READ_ONLY,
    "cruxible_playbill_run_query": PermissionMode.READ_ONLY,
    "cruxible_playbill_discover": PermissionMode.READ_ONLY,
    "cruxible_playbill_expand": PermissionMode.READ_ONLY,
    "cruxible_playbill_export_floor": PermissionMode.READ_ONLY,
    "cruxible_playbill_resolve_coverage": PermissionMode.READ_ONLY,
    "cruxible_playbill_authoring_get": PermissionMode.READ_ONLY,
    "cruxible_playbill_authoring_resume": PermissionMode.READ_ONLY,
    "cruxible_playbill_authoring_list_pending": PermissionMode.READ_ONLY,
    "cruxible_playbill_authoring_status": PermissionMode.READ_ONLY,
    "cruxible_playbill_store_body": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_propose_document": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_propose_source_bundle": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_propose_subject": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_propose_claim_type": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_propose_claim": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_propose_claims": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_propose_query_definition": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_authoring_create": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_authoring_compile": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_authoring_preflight": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_authoring_submit": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_authoring_confirm_insertion": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_authoring_abandon_insertion": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_dereference": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_submit_approval": PermissionMode.GRAPH_WRITE,
    "cruxible_playbill_activate": PermissionMode.GRAPH_WRITE,
    "cruxible_playbill_host_create": PermissionMode.ADMIN,
    "cruxible_playbill_init": PermissionMode.ADMIN,
    "cruxible_playbill_propose_principal_change": PermissionMode.ADMIN,
}

# HTTP/CLI operations that share the same runtime boundary without being MCP tools.
RUNTIME_OPERATION_PERMISSIONS: dict[str, PermissionMode] = {
    "cruxible_playbill_inspect": PermissionMode.READ_ONLY,
    "cruxible_playbill_read": PermissionMode.READ_ONLY,
    "cruxible_playbill_propose": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_body_read": PermissionMode.GOVERNED_WRITE,
    "cruxible_playbill_principal_change": PermissionMode.ADMIN,
    "cruxible_runtime_credentials": PermissionMode.ADMIN,
    "cruxible_server_restart": PermissionMode.ADMIN,
}

# Unserved operations retained only for donor behavior and parity tests. Keeping
# these names separate from ``TOOL_PERMISSIONS`` and
# ``RUNTIME_OPERATION_PERMISSIONS`` makes their lack of a public transport
# registration explicit while preserving the service-layer permission law
# until the owning behavior is transplanted or removed.
DONOR_OPERATION_PERMISSIONS: dict[str, PermissionMode] = {
    "cruxible_feedback_adjudicate": PermissionMode.GRAPH_WRITE,
    "cruxible_resolve_group": PermissionMode.GRAPH_WRITE,
    "cruxible_supersede_claim": PermissionMode.GRAPH_WRITE,
    "cruxible_retract_claim": PermissionMode.GRAPH_WRITE,
    "cruxible_supersede_entity": PermissionMode.GRAPH_WRITE,
    "cruxible_retire_entity": PermissionMode.GRAPH_WRITE,
}

PERMISSION_REQUIREMENTS: dict[str, PermissionMode] = {
    **TOOL_PERMISSIONS,
    **RUNTIME_OPERATION_PERMISSIONS,
    **DONOR_OPERATION_PERMISSIONS,
}

# ---------------------------------------------------------------------------
# Feedback action → minimum permission tier (per-ACTION, not per-tool)
# ---------------------------------------------------------------------------
#
# The permission map above is per-TOOL, and ``cruxible_feedback`` (with its
# batch/from_query siblings) is not one operation: it multiplexes RECORDING an
# observation with ADJUDICATING a claim. Recording is a governed-operator act.
# Adjudication is not: ``accept``/``correct`` make a non-live edge LIVE and
# ``reject`` retracts one, which is the same authority a direct graph write or a
# group resolution carries. Leaving every action at the tool's GOVERNED_WRITE
# floor let a single GOVERNED_WRITE actor attest a pending edge and then
# accept their own proposal — a live approved claim on a proposal_only type
# with no reviewer above them (wi-feedback-approval-rail).
#
# So the adjudication verbs are gated at GRAPH_WRITE, the same tier that
# ``cruxible_resolve_group`` and the direct-write verbs require, matching the
# procedure-resolve/disposition precedent.
#
# ``flag`` was REMOVED in 2026-07 (dd-flag-superseded-by-attestation, brought
# forward from "retires later"): it moved an edge to ``pending`` while storing
# no annotation, so it destroyed the reviewer's signal. It was also the only
# feedback action sitting at GOVERNED_WRITE, so EVERY remaining feedback action
# is now an adjudication requiring GRAPH_WRITE. What still sits at the tool's
# GOVERNED_WRITE floor is the RECORDING half of an action — persisting the
# FeedbackRecord — which is what an adjudication refusal rolls back along with
# the transition. A GOVERNED_WRITE actor who wants to register a doubt uses
# ``cruxible_attest`` with stance ``contradict``, which records the observation,
# its evidence, and its actor and changes no status.
#
# Enforced in ``service/feedback.py`` (the single service chokepoint every
# surface funnels through), not here, because the requirement is a property of
# the payload's action rather than of the tool name.
FEEDBACK_ACTION_PERMISSIONS: dict[str, PermissionMode] = {
    "accept": PermissionMode.GRAPH_WRITE,
    "reject": PermissionMode.GRAPH_WRITE,
    "correct": PermissionMode.GRAPH_WRITE,
}

# Audited operation name the adjudication check reports under. It is a runtime
# operation rather than a registered MCP tool, so the denial message names the
# adjudication act instead of whichever feedback tool carried it.
FEEDBACK_ADJUDICATION_OPERATION = "cruxible_feedback_adjudicate"

# ---------------------------------------------------------------------------
# Group resolution — the same adjudication act, reached by a second door
# ---------------------------------------------------------------------------
#
# ``cruxible_resolve_group`` remains GRAPH_WRITE in the donor-operation map
# above even though its MCP/HTTP surface is gone. The exported
# ``service_resolve_group`` can still be called by donor parity code: a direct
# library caller holding only GOVERNED_WRITE could otherwise reach the
# transition (and with ``stamp_existing=True`` bless a pending edge) with no
# tier check at all. Since wi-feedback-approval-rail chose
# the SERVICE layer as the enforcement seam for adjudication, group resolution
# is made consistent with it: the transition re-asserts the requirement inside
# its own mutation-receipt scope, so the refusal is receipted and every door
# into the act is gated the same way.
GROUP_RESOLUTION_OPERATION = "cruxible_resolve_group"
GROUP_RESOLUTION_PERMISSION = PermissionMode.GRAPH_WRITE

# ---------------------------------------------------------------------------
# Cached state
# ---------------------------------------------------------------------------

_cached_mode: PermissionMode | None = None
_cached_allowed_roots: list[Path] | None | bool = False  # False = not yet parsed

# Per-request narrowing (for authenticated/cloud multi-tenant use)
_request_mode: contextvars.ContextVar[PermissionMode | None] = contextvars.ContextVar(
    "cruxible_permission_mode", default=None
)
_request_instance_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cruxible_instance_scope", default=None
)


# ---------------------------------------------------------------------------
# Initialization and caching
# ---------------------------------------------------------------------------


def _default_read_only_opt_in() -> bool:
    """Return whether the unauth default should be READ_ONLY instead of ADMIN.

    Opt-in via ``CRUXIBLE_DEFAULT_READ_ONLY`` (off by default to preserve the
    local-UX ADMIN default). Only consulted when ``CRUXIBLE_MODE`` is unset; an
    explicit ``CRUXIBLE_MODE`` always wins.
    """
    raw = os.environ.get("CRUXIBLE_DEFAULT_READ_ONLY")
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


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
    """Read ``CRUXIBLE_MODE`` once and cache the process capability ceiling.

    The first call fixes the ceiling for the process lifetime. Repeating the
    call with the same resolved mode is harmless; attempting to change it
    fails closed. ``reset_permissions`` exists only for isolated tests that
    model fresh processes.

    Args:
        mode: Override for testing. If provided, skips env var lookup.

    Returns:
        The resolved :class:`PermissionMode`.

    Raises:
        ConfigError: If the env var contains an invalid value.
    """
    global _cached_mode, _cached_allowed_roots

    if mode is not None:
        resolved_mode = mode
    else:
        raw = os.environ.get("CRUXIBLE_MODE")
        if raw is None:
            # Default when CRUXIBLE_MODE is unset.
            #
            # The unauthenticated default deliberately stays ADMIN (audit #4):
            # every local `cruxible` write, dogfooding session, and demo runs
            # auth-off and relies on ADMIN by default. Flipping the default to
            # READ_ONLY would break local writes out of the box — an adoption
            # regression the project is explicitly optimizing against.
            #
            # The real browser-originated threat (a malicious webpage / DNS
            # rebinding hitting the loopback daemon) is closed at the HTTP edge by
            # the Origin allowlist in server.auth, NOT by lowering this default;
            # programmatic CLI/SDK clients are unaffected by that gate.
            #
            # Operators who want a least-privilege default WITHOUT setting
            # CRUXIBLE_MODE explicitly can opt in via CRUXIBLE_DEFAULT_READ_ONLY;
            # it defaults off so the local-UX default remains ADMIN.
            if _default_read_only_opt_in():
                resolved_mode = PermissionMode.READ_ONLY
            else:
                resolved_mode = PermissionMode.ADMIN
        else:
            resolved = _MODE_NAMES.get(raw.lower())
            if resolved is None:
                valid = ", ".join(sorted(_MODE_NAMES))
                raise ConfigError(f"Invalid CRUXIBLE_MODE='{raw}'. Valid values: {valid}")
            resolved_mode = resolved

    if _cached_mode is not None:
        if resolved_mode != _cached_mode:
            raise ConfigError(
                "CRUXIBLE_MODE is immutable after permission initialization "
                f"(initialized={_cached_mode.name.lower()}, "
                f"requested={resolved_mode.name.lower()})"
            )
        return _cached_mode

    _cached_mode = resolved_mode

    # Parse allowed roots (fail-fast on bad config)
    _cached_allowed_roots = validate_allowed_roots()

    return _cached_mode


def get_capability_ceiling() -> PermissionMode:
    """Return the immutable process capability ceiling."""
    global _cached_mode
    if _cached_mode is None:
        init_permissions()
    assert _cached_mode is not None
    return _cached_mode


def clamp_to_capability_ceiling(mode: PermissionMode) -> PermissionMode:
    """Intersect a requested permission tier with the process ceiling."""
    return min(mode, get_capability_ceiling())


def get_current_mode() -> PermissionMode:
    """Return the active permission mode clamped to the process ceiling.

    Anonymous/local calls receive exactly the initialized process mode. A
    request-scoped credential or relayed mode can only narrow that mode.
    """
    ceiling = get_capability_ceiling()
    request = _request_mode.get()
    if request is not None:
        return clamp_to_capability_ceiling(request)
    return ceiling


def reset_permissions() -> None:
    """Clear cached mode, allowed roots, and request scope. Used for test isolation."""
    global _cached_mode, _cached_allowed_roots
    _cached_mode = None
    _cached_allowed_roots = False
    _request_mode.set(None)
    _request_instance_scope.set(None)


@contextmanager
def request_permission_scope(mode: PermissionMode) -> Iterator[None]:
    """Temporarily narrow the permission mode for the current context.

    ``get_current_mode`` clamps this value to the process ceiling. Uses
    token-based reset so nested scopes restore correctly.
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
    required_override: PermissionMode | None = None,
    audit_success: bool = True,
) -> None:
    """Check whether the current mode permits calling *tool_name*.

    Args:
        tool_name: The operation or tool being called.
        instance_id: Optional instance ID for audit logging and scope enforcement.
        enforce_instance_scope: Whether to reject when request credentials are scoped
            to a different instance.
        required_override: Replace the static tool->tier requirement for this call.
            Temporarily retained for unserved donor transitions whose reviewed
            adjudication act has a stricter tier than their recording path. The
            operation must still exist in the static permission map.
        audit_success: Emit the ``mutation_allowed`` audit record on success. Set
            False ONLY for a pre-gate whose caller immediately re-checks (and
            audits) the same tool at its final computed requirement, so each
            mutation yields exactly one authoritative success record. Denials
            and scope violations are always logged regardless.

    Raises:
        PermissionDeniedError: If the current mode is insufficient.
    """
    current = get_current_mode()
    ceiling = get_capability_ceiling()
    if tool_name not in PERMISSION_REQUIREMENTS:
        raise ConfigError(f"Tool '{tool_name}' has no entry in permission requirements")
    effective = (
        required_override if required_override is not None else PERMISSION_REQUIREMENTS[tool_name]
    )

    if current < effective:
        ceiling_mode = ceiling.name if ceiling < effective else None
        _log.warning(
            "permission_denied",
            tool=tool_name,
            mode=current.name,
            required=effective.name,
            capability_ceiling=ceiling.name,
            instance_id=instance_id,
        )
        raise PermissionDeniedError(
            tool_name,
            current.name,
            effective.name,
            ceiling_mode=ceiling_mode,
        )

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
    if audit_success and effective >= PermissionMode.GOVERNED_WRITE:
        _log.info(
            "mutation_allowed",
            tool=tool_name,
            mode=current.name,
            instance_id=instance_id,
        )


def require_unscoped_operator(operation: str) -> None:
    """Require an unscoped operator credential for a daemon-wide operation.

    Some operations act on the whole shared daemon rather than a single instance
    (re-exec/restart and global server metadata). On a shared multi-tenant daemon,
    an *instance-scoped* ADMIN
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
