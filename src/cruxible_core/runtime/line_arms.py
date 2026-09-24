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
from cruxible_core.service.procedures.line_dispatch import (
    LineArmAuthorityLost,
    LineArmSegmentEnded,
    require_active_segment,
    service_dispatch_line,
    service_stop_line_arm,
)

#: Arming lets the daemon dispatch on the caller's behalf, so it needs what an
#: explicit dispatch needs.
ARM_PERMISSION = TOOL_PERMISSIONS["cruxible_playbill_line_dispatch"]

#: Occurrences one automatic pass admits before yielding to other Lines.
AUTOMATIC_DISPATCH_LIMIT = 10


def current_arm_principal() -> LineArmPrincipalV1:
    """The credential this request would arm under; only its identifier is kept."""

    auth = get_current_auth_context()
    if auth is not None and auth.credential_type == "runtime_credential":
        return LineArmPrincipalV1(
            kind="runtime_credential",
            credential_id=auth.principal_id,
            label=auth.principal_label,
        )
    if auth is None and not is_server_auth_enabled():
        return LineArmPrincipalV1(kind="local_operator", label=LOCAL_OPERATOR_ACTOR_ID)
    raise AuthenticationError(
        "Arming a Line requires a runtime credential the daemon can recheck before each run"
    )


def arm_authority(
    instance_id: str, principal: LineArmPrincipalV1, *, now: datetime
) -> tuple[GovernedActorContext, int]:
    """The actor and caller rung the arm dispatches under, or why it no longer may."""

    if principal.kind == "local_operator":
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
        return local_operator_actor_context(), mode.value - 1
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
    actor = GovernedActorContext(
        actor_type="service_account",
        actor_id=record.label,
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
    principal = LineArmPrincipalV1.model_validate(arm["armed_by"])
    daemon = GovernedActorContext(
        actor_type="system",
        actor_id="line-listener",
        org_id=instance.descriptor.instance_id,
        operation_id=new_id("op", length=16, separator="_"),
        timestamp=now,
    )

    def recheck() -> None:
        require_active_segment(instance, arm["session_id"])
        arm_authority(instance_id, principal, now=now)

    try:
        actor, caller_rung = arm_authority(instance_id, principal, now=now)
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
