"""Who may arm a Line, and whether that authority still holds when work is due.

An arm retains only the arming credential's identifier. Before every automatic
admission the daemon re-reads that credential: revoked, moved to another
instance, or no longer permitted to dispatch, and the arm stops with the reason
instead of running under authority it no longer has.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cruxible_client.contracts.line_dispatch import (
    LineArmPrincipalV1,
    LineDispatchRequestV1,
    LineDispatchResultV1,
    is_current_arm_principal_record,
)
from cruxible_client.contracts.primitives import new_id
from cruxible_core.documents.workspace_file import WorkspaceFileReadRefused
from cruxible_core.errors import AuthenticationError
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.runtime.permissions import (
    TOOL_PERMISSIONS,
    clamp_to_capability_ceiling,
    get_capability_ceiling,
)
from cruxible_core.server.actor_identity import (
    LOCAL_OPERATOR_ACTOR_ID,
    local_operator_actor_context,
)
from cruxible_core.server.auth import get_current_auth_context
from cruxible_core.server.config import is_server_auth_enabled
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.service.identity import credential_unbound_refusal, principal_refusal
from cruxible_core.service.procedures.line_dispatch import (
    ARM_REQUIRES_REARM,
    LineArmAuthorityLost,
    LineArmSegmentEnded,
    require_active_segment,
    service_dispatch_line,
    service_stop_line_arm,
)

#: Arming lets the daemon dispatch on the caller's behalf for as long as the arm
#: holds, so the arming credential must keep the tier arming itself needs.
ARM_PERMISSION = TOOL_PERMISSIONS["cruxible_playbill_line_arm"]

#: Occurrences one automatic pass admits before yielding to other Lines.
AUTOMATIC_DISPATCH_LIMIT = 10


def current_arm_principal() -> LineArmPrincipalV1:
    """The credential this request would arm under; only its identifier is kept."""

    auth = get_current_auth_context()
    if auth is not None and auth.credential_type == "runtime_credential":
        assert auth.credential_id is not None and auth.credential_label is not None
        if auth.principal_id is None:
            raise credential_unbound_refusal(
                credential_id=auth.credential_id, credential_label=auth.credential_label
            )
        return LineArmPrincipalV1(
            kind="runtime_credential",
            credential_id=auth.credential_id,
            label=auth.credential_label,
        )
    if not is_server_auth_enabled() and (auth is None or auth.credential_type == "principal_claim"):
        # Auth off: the arm records its provenance. A request that claims a
        # principal arms as that claim, rechecked before every admission; one
        # that claims none arms as the implicit local operator.
        if auth is None:
            return LineArmPrincipalV1(kind="local_operator", label=LOCAL_OPERATOR_ACTOR_ID)
        assert auth.principal_id is not None
        return LineArmPrincipalV1(kind="principal_claim", label=auth.principal_id)
    raise AuthenticationError(
        "Arming a Line requires a runtime credential the daemon can recheck before each run"
    )


def _require_active_principal(instance: Any, principal_id: str) -> None:
    """Stop the arm unless its principal is active at the accepted head, right now.

    Automatic dispatch never passes through the HTTP middleware, so it cannot
    rely on a credential having been revoked on use: the accepted registry is
    read here before every admission.
    """

    refusal = principal_refusal(instance, principal_id, configured=True)
    if refusal is not None:
        raise LineArmAuthorityLost(
            "principal_inactive",
            f"The arming principal {principal_id!r} is no longer an active principal "
            f"({refusal.error_code}); rearm as an active principal to resume.",
        )


def arm_authority(
    instance: Any, principal: LineArmPrincipalV1, *, now: datetime
) -> tuple[GovernedActorContext, int]:
    """The actor and caller rung the arm dispatches under, or why it no longer may.

    Rechecked before every automatic admission: the arming credential (revoked,
    moved, downgraded or unbound) and the accepted standing of the principal
    the arm acts as, for credential arms and auth-off claimed arms alike.
    """

    instance_id = instance.descriptor.instance_id

    if principal.kind in ("local_operator", "principal_claim"):
        if is_server_auth_enabled():
            raise LineArmAuthorityLost(
                "authentication_changed",
                "The daemon now requires authentication; rearm with a runtime credential.",
            )
        mode = get_capability_ceiling()
        if mode < ARM_PERMISSION:
            raise LineArmAuthorityLost(
                "permission_insufficient",
                f"The daemon's permission mode {mode.name} no longer permits dispatch.",
            )
        actor = local_operator_actor_context()
        if principal.kind == "principal_claim":
            # An auth-off arm made under a claimed principal acts as it only
            # while that principal stays active, whatever its name -- a
            # registered principal named "operator" included.
            _require_active_principal(instance, principal.label)
            actor = actor.model_copy(
                update={"actor_type": "service_account", "actor_id": principal.label}
            )
        # The implicit local operator (no principal claimed) is the OS user's own
        # authority; it never resolves to a registered principal's standing.
        return actor, mode.value - 1
    assert principal.credential_id is not None
    record = get_runtime_credential_store().get(principal.credential_id)
    if record is None or record.revoked_at is not None:
        raise LineArmAuthorityLost(
            "credential_revoked", "The arming credential was revoked; rearm to resume."
        )
    if record.instance_id != instance_id:
        raise LineArmAuthorityLost(
            "credential_scope_changed",
            "The arming credential no longer belongs to this instance; rearm to resume.",
        )
    mode = clamp_to_capability_ceiling(record.permission_mode)
    if mode < ARM_PERMISSION:
        raise LineArmAuthorityLost(
            "permission_insufficient",
            f"The arming credential's permission {mode.name} no longer permits dispatch.",
        )
    if record.principal_id is None:
        raise LineArmAuthorityLost(
            "credential_unbound",
            "The arming credential acts as no principal; rearm with a principal-bound one.",
        )
    try:
        _require_active_principal(instance, record.principal_id)
    except LineArmAuthorityLost:
        # Revoking a principal revokes its credentials, here as on use.
        get_runtime_credential_store().revoke_credentials_of_principal(
            instance_id=instance_id, principal_id=record.principal_id
        )
        raise
    actor = GovernedActorContext(
        actor_type="service_account",
        actor_id=record.principal_id,
        org_id=record.instance_id,
        operation_id=new_id("op", length=16, separator="_"),
        timestamp=now,
    )
    return actor, mode.value - 1


def dispatch_armed_line(
    manager: Any,
    instance_id: str,
    arm: dict[str, Any],
    *,
    now: datetime | None = None,
    limit: int = AUTOMATIC_DISPATCH_LIMIT,
) -> LineDispatchResultV1 | None:
    """Admit the due work one armed segment matched, under its rechecked authority.

    Returns None when the arm stopped because its authority no longer holds.
    """

    now = now or datetime.now(UTC)
    instance = manager.get(instance_id)
    daemon_early = GovernedActorContext(
        actor_type="system",
        actor_id="line-listener",
        org_id=instance.descriptor.instance_id,
        operation_id=new_id("op", length=16, separator="_"),
        timestamp=now,
    )
    if not is_current_arm_principal_record(arm.get("armed_by")):
        # An arm persisted before arms named their provenance: never resolve it
        # as the implicit operator; stop it and ask for a rearm.
        service_stop_line_arm(
            instance,
            arm["session_id"],
            reason="arm_requires_rearm",
            detail=ARM_REQUIRES_REARM,
            actor=daemon_early,
            now=now,
        )
        return None
    principal = LineArmPrincipalV1.model_validate(arm["armed_by"])
    daemon = GovernedActorContext(
        actor_type="system",
        actor_id="line-listener",
        org_id=instance.descriptor.instance_id,
        operation_id=new_id("op", length=16, separator="_"),
        timestamp=now,
    )

    def recheck() -> tuple[GovernedActorContext, int]:
        # Each admission runs under authority resolved for it, not the first one's.
        require_active_segment(instance, arm["session_id"])
        return arm_authority(instance, principal, now=now)

    try:
        actor, caller_rung = arm_authority(instance, principal, now=now)
        try:
            reader = manager.workspace_file_reader(instance_id)
        except WorkspaceFileReadRefused:
            reader = None
        return service_dispatch_line(
            instance,
            arm["line_id"],
            LineDispatchRequestV1(limit=limit),
            actor=actor,
            now=now,
            caller_rung=caller_rung,
            provider_runtime_operator=manager.provider_runtime_operator(),
            workspace_file_reader=reader,
            session_id=arm["session_id"],
            pinned_line_artifact_digest=arm["line_artifact_digest"],
            pinned_trigger_pins=arm.get("trigger_pins", {}),
            before_admission=recheck,
        )
    except LineArmSegmentEnded:
        # Disarmed or rearmed since this drain was scheduled: nothing more runs.
        return None
    except LineArmAuthorityLost as lost:
        service_stop_line_arm(
            instance,
            arm["session_id"],
            reason=lost.reason,
            detail=lost.detail,
            actor=daemon,
            now=now,
        )
        return None


__all__ = [
    "ARM_PERMISSION",
    "AUTOMATIC_DISPATCH_LIMIT",
    "arm_authority",
    "current_arm_principal",
    "dispatch_armed_line",
]
