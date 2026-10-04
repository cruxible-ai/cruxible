"""Whether the principal a request acts as may act on one instance, and why not.

One place answers this for every door: the route guard that checks a configured
principal ID before any read or write, ``whoami``, and authoring create. Each
refusal carries its code, the runnable CLI command, and the same next step as a
served repair.
"""

from __future__ import annotations

from typing import Literal, cast

from cruxible_client.contracts.principals import (
    AuthoringRefusal,
    AuthoringRefusalCode,
)
from cruxible_client.contracts.repairs import RepairOperation, hand_edit_repair
from cruxible_core.actor_vocabulary import LOCAL_OPERATOR_ACTOR_ID
from cruxible_core.errors import PrincipalRefusedError
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.permissions import PermissionMode

PrincipalStanding = Literal["active", "revoked", "absent"]


def principal_standing(instance: PlaybillInstance, principal_id: str) -> PrincipalStanding:
    """The principal's status in the accepted registry at the current head."""

    for principal in instance.accepted_history()[-1].principals.principals:
        if principal.principal_id == principal_id:
            return principal.status
    return "absent"


def active_principal_ids(instance: PlaybillInstance) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                principal.principal_id
                for principal in instance.accepted_history()[-1].principals.principals
                if principal.status == "active" and principal.kind == "ordinary"
            ),
            key=lambda item: item.encode("utf-8"),
        )
    )


def principal_refusal(
    instance: PlaybillInstance,
    principal_id: str,
    *,
    configured: bool,
) -> PrincipalRefusedError | None:
    """Why ``principal_id`` cannot act on this instance, or None when it can.

    ``configured`` is False for the implicit local operator identity, which the
    caller never chose: its repair is to configure a principal, not to register
    the placeholder name.
    """

    standing = principal_standing(instance, principal_id)
    if standing == "active":
        return None
    instance_id = instance.descriptor.instance_id
    if not configured and principal_id == LOCAL_OPERATOR_ACTOR_ID:
        active = active_principal_ids(instance)
        named = ", ".join(active) if active else "none yet"
        return PrincipalRefusedError(
            "cruxible.identity.principal_unconfigured",
            "this process names no principal, and the local operator identity "
            f"'{LOCAL_OPERATOR_ACTOR_ID}' is not a registered principal on {instance_id}; "
            "repair: set CRUXIBLE_PRINCIPAL_ID (or pass --principal-id) to your "
            f"registered principal ID (active principals: {named})",
            repair=RepairOperation(
                operation="cruxible.orient",
                arguments={"section": "principals", "configure": "CRUXIBLE_PRINCIPAL_ID"},
            ),
        )
    if standing == "revoked":
        return PrincipalRefusedError(
            "cruxible.identity.principal_revoked",
            f"principal {principal_id!r} was revoked on {instance_id}; repair: act as an "
            "active principal (`cruxible playbill orient --section principals`)",
            repair=RepairOperation(
                operation="cruxible.orient",
                arguments={"section": "principals", "revoked_principal_id": principal_id},
            ),
        )
    return PrincipalRefusedError(
        "cruxible.identity.principal_absent",
        f"principal {principal_id!r} is not registered on {instance_id}; repair: an "
        f"owner runs `cruxible playbill principal add {principal_id} --key-dir DIR`, "
        "then this process acts with the settings it writes",
        repair=RepairOperation(
            operation="cruxible.principal.add",
            arguments={"principal_id": principal_id},
        ),
    )


def credential_unbound_refusal(
    *, credential_id: str | None, credential_label: str | None
) -> PrincipalRefusedError:
    """An unbound credential keeps transport authority but can never author."""

    who = "request" if credential_label is None else f"bearer credential ({credential_label})"
    return PrincipalRefusedError(
        "cruxible.identity.credential_unbound",
        f"this {who} acts as no principal, so it cannot "
        "author or attribute governed work; repair: mint one bound to your principal with "
        "its key: `cruxible credential mint --principal-id ID --key-dir DIR --mode "
        "governed_write`, then revoke this one",
        repair=RepairOperation(
            operation="credential.mint",
            arguments=({} if credential_id is None else {"unbound_credential_id": credential_id}),
        ),
    )


def require_authoring_principal(instance: PlaybillInstance, actor_id: str) -> None:
    """Refuse an authoring draft whose actor is not an active principal, before any work.

    Proposal evaluation refuses the same actor later
    (``cruxible.proposal.creator_principal_invalid``); refusing at create saves
    the caller from building and preflighting a payload that can never land.
    """

    refusal = principal_refusal(instance, actor_id, configured=actor_id != LOCAL_OPERATOR_ACTOR_ID)
    if refusal is not None:
        raise refusal


def authoring_refusal(
    instance: PlaybillInstance,
    *,
    actor_id: str | None,
    configured: bool,
    credential_id: str | None,
    credential_label: str | None,
    permission_mode: PermissionMode,
) -> AuthoringRefusal | None:
    """Why this actor cannot author here, or None when it can.

    The first applicable reason wins, in the order a write meets them: a
    terminal instance, an actor with no principal, a principal that is not
    active, then a tier below ``governed_write``.
    """

    refusal: PrincipalRefusedError | None
    terminal = instance.descriptor.decommissioned
    if terminal is not None:
        return AuthoringRefusal(
            code="cruxible.instance.decommissioned",
            detail=(
                f"the instance was decommissioned at {terminal.decommissioned_at} "
                f"({terminal.reason}); every write is refused"
            ),
            repair=hand_edit_repair(
                "cruxible.instance.decommissioned",
                required_change=(
                    "author on another instance; a decommissioned one accepts no writes"
                ),
            ),
        )
    if actor_id is None:
        refusal = credential_unbound_refusal(
            credential_id=credential_id, credential_label=credential_label
        )
    else:
        refusal = principal_refusal(instance, actor_id, configured=configured)
    if refusal is None and permission_mode < PermissionMode.GOVERNED_WRITE:
        refusal = PrincipalRefusedError(
            "cruxible.identity.permission_insufficient",
            f"this request runs at {permission_mode.name.lower()}, and authoring needs "
            "governed_write; repair: mint a governed_write credential for your principal: "
            f"`cruxible credential mint --principal-id {actor_id} --key-dir DIR --mode "
            "governed_write`",
            repair=RepairOperation(
                operation="credential.mint",
                arguments={"principal_id": actor_id, "permission_mode": "governed_write"},
            ),
        )
    if refusal is None:
        return None
    assert refusal.repair is not None
    return AuthoringRefusal(
        code=cast(AuthoringRefusalCode, refusal.error_code),
        detail=str(refusal).removeprefix(f"{refusal.error_code}: "),
        repair=refusal.repair,
    )


__all__ = [
    "authoring_refusal",
    "credential_unbound_refusal",
    "require_authoring_principal",
    "PrincipalStanding",
    "active_principal_ids",
    "principal_refusal",
    "principal_standing",
]
