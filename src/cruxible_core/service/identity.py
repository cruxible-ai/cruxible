"""Whether the principal a request acts as may act on one instance, and why not.

One place answers this for every door: the route guard that checks a configured
principal ID before any read or write, ``whoami``, and authoring create. Each
refusal carries its code, the runnable CLI command, and the same next step as a
served repair.
"""

from __future__ import annotations

from typing import Literal

from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_core.actor_vocabulary import LOCAL_OPERATOR_ACTOR_ID
from cruxible_core.errors import PrincipalRefusedError
from cruxible_core.runtime.instance import PlaybillInstance

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
            "playbill.identity.principal_unconfigured",
            "this process names no principal, and the local operator identity "
            f"'{LOCAL_OPERATOR_ACTOR_ID}' is not a registered principal on {instance_id}; "
            "repair: set CRUXIBLE_PRINCIPAL_ID (or pass --principal-id) to your "
            f"registered principal ID (active principals: {named})",
            repair=RepairOperationV1(
                operation="playbill.principal.list",
                arguments={"configure": "CRUXIBLE_PRINCIPAL_ID"},
            ),
        )
    if standing == "revoked":
        return PrincipalRefusedError(
            "playbill.identity.principal_revoked",
            f"principal {principal_id!r} was revoked on {instance_id}; repair: act as an "
            "active principal (`cruxible playbill principal list`)",
            repair=RepairOperationV1(
                operation="playbill.principal.list",
                arguments={"revoked_principal_id": principal_id},
            ),
        )
    return PrincipalRefusedError(
        "playbill.identity.principal_absent",
        f"principal {principal_id!r} is not registered on {instance_id}; repair: an "
        f"owner runs `cruxible playbill principal add {principal_id} --key-dir DIR`, "
        "then this process acts with the settings it writes",
        repair=RepairOperationV1(
            operation="playbill.principal.add",
            arguments={"principal_id": principal_id},
        ),
    )


def credential_unbound_refusal(
    *, credential_id: str | None, credential_label: str | None
) -> PrincipalRefusedError:
    """An unbound credential keeps transport authority but can never author."""

    return PrincipalRefusedError(
        "playbill.identity.credential_unbound",
        f"this bearer credential ({credential_label}) acts as no principal, so it cannot "
        "author or attribute governed work; repair: mint one bound to your principal with "
        "its key: `cruxible credential mint --principal-id ID --key-dir DIR --mode "
        "governed_write`, then revoke this one",
        repair=RepairOperationV1(
            operation="credential.mint",
            arguments={"unbound_credential_id": credential_id},
        ),
    )


__all__ = [
    "credential_unbound_refusal",
    "PrincipalStanding",
    "active_principal_ids",
    "principal_refusal",
    "principal_standing",
]
