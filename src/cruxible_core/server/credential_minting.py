"""Mint a bearer credential in one principal's name, only with that principal's authority.

A credential acts as exactly one principal. The admin tier lets a caller manage
credentials at all; it never lets a caller mint one that acts as somebody else.
Minting as principal P therefore needs, in addition, P's own authority:

* the request already acts as P (a credential bound to P), or
* P's signed consent: a fresh, single-use mint statement signed with P's key
  as registered at the accepted head.

P must be an ordinary principal, registered and active. The label is a
description and decides nothing. Minting needs daemon auth: on an auth-off
daemon it is refused, never latched.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_client.contracts.runtime_credentials import (
    RUNTIME_CREDENTIAL_PROOF_MAX_SKEW_SECONDS,
    RuntimeCredentialPrincipalProof,
    verify_runtime_credential_proof,
)
from cruxible_client.contracts.temporal import parse_datetime, utc_now
from cruxible_core.errors import (
    PrincipalRefusedError,
    RuntimeCredentialNotFoundError,
)
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.auth import ResolvedAuthContext
from cruxible_core.server.config import is_server_auth_enabled
from cruxible_core.server.credentials import (
    CreatedRuntimeCredential,
    RuntimeCredentialRecord,
    StateObserver,
    _proof_replayed,
    get_runtime_credential_store,
)
from cruxible_core.service.identity import principal_refusal
from cruxible_core.storage.preview_fence import is_previewing


def _mint_command(principal_id: str, permission_mode: str) -> str:
    return (
        f"cruxible credential mint --principal-id {principal_id} --key-dir DIR "
        f"--mode {permission_mode}"
    )


def _authority_required(principal_id: str, permission_mode: str) -> PrincipalRefusedError:
    return PrincipalRefusedError(
        "runtime_credential.principal_authority_required",
        f"minting a credential that acts as {principal_id!r} needs that principal's "
        "authority, and an admin credential alone is not it; repair: sign the mint with "
        f"the principal's key: `{_mint_command(principal_id, permission_mode)}`",
        repair=RepairOperation(
            operation="credential.mint",
            arguments={"principal_id": principal_id, "permission_mode": permission_mode},
        ),
    )


def _proof_invalid(principal_id: str, permission_mode: str, reason: str) -> PrincipalRefusedError:
    return PrincipalRefusedError(
        "runtime_credential.principal_proof_invalid",
        f"the signed consent for {principal_id!r} is not valid: {reason}; repair: sign a "
        f"fresh one with `{_mint_command(principal_id, permission_mode)}`",
        repair=RepairOperation(
            operation="credential.mint",
            arguments={"principal_id": principal_id, "permission_mode": permission_mode},
        ),
    )


def _verified_proof_digest(
    proof: RuntimeCredentialPrincipalProof,
    *,
    instance_id: str,
    principal_id: str,
    public_key: str,
    permission_mode: str,
    label: str,
) -> str:
    statement = proof.statement
    if (
        statement.instance_id != instance_id
        or statement.principal_id != principal_id
        or statement.permission_mode != permission_mode
        or statement.label != label
    ):
        raise _proof_invalid(
            principal_id, permission_mode, "it names a different instance, principal, mode or label"
        )
    issued_at = parse_datetime(statement.issued_at)
    assert issued_at is not None  # the statement model refuses anything else
    if abs(utc_now() - issued_at) > timedelta(seconds=RUNTIME_CREDENTIAL_PROOF_MAX_SKEW_SECONDS):
        raise _proof_invalid(principal_id, permission_mode, "it was not signed just now")
    if not verify_runtime_credential_proof(proof, public_key=public_key):
        raise _proof_invalid(
            principal_id, permission_mode, "the signature is not the principal's registered key"
        )
    return _proof_digest(proof)


def _proof_digest(proof: RuntimeCredentialPrincipalProof) -> str:
    """The single-use identity of one signed consent."""

    return "sha256:" + hashlib.sha256(canonical_bytes(proof.model_dump(mode="json"))).hexdigest()


def _require_auth_on() -> None:
    if not is_server_auth_enabled():
        # A bearer credential authenticates nothing here, and storing one would
        # silently latch this state root into requiring auth on its next start.
        raise PrincipalRefusedError(
            "runtime_credential.auth_off",
            "this daemon runs with auth off, so a bearer credential would authenticate "
            "nothing; repair: restart the daemon with auth: `cruxible server start --auth`",
            repair=RepairOperation(operation="server.start", arguments={"auth": True}),
        )


def _principal_authority(
    *,
    instance_id: str,
    principal_id: str,
    mode_name: str,
    label: str,
    principal_proof: RuntimeCredentialPrincipalProof | None,
    auth_context: ResolvedAuthContext | None,
) -> str | None:
    """Require ``principal_id``'s authority for a credential in its name.

    Returns the digest of the consent this spends, or None when the request
    already acts as the principal. Refuses unless the principal is an active,
    ordinary principal and one of the two holds.
    """

    instance = get_playbill_manager().get(instance_id)
    refusal = principal_refusal(instance, principal_id, configured=True)
    if refusal is not None:
        raise refusal
    registered = next(
        item
        for item in instance.accepted_history()[-1].principals.principals
        if item.principal_id == principal_id
    )
    if registered.kind != "ordinary":
        # A recovery principal exists only to govern key replacement; it never
        # authors, so it never holds a bearer credential.
        raise PrincipalRefusedError(
            "runtime_credential.principal_not_ordinary",
            f"principal {principal_id!r} is a {registered.kind} principal, and only ordinary "
            "principals hold credentials; repair: mint for an ordinary principal "
            "(`cruxible playbill orient --section principals`)",
            repair=RepairOperation(
                operation="cruxible.orient", arguments={"section": "principals"}
            ),
        )
    if principal_proof is not None:
        return _verified_proof_digest(
            principal_proof,
            instance_id=instance_id,
            principal_id=principal_id,
            public_key=registered.public_key,
            permission_mode=mode_name,
            label=label,
        )
    if auth_context is not None and auth_context.principal_id == principal_id:
        return None
    raise _authority_required(principal_id, mode_name)


def mint_principal_credential(
    *,
    instance_id: str,
    principal_id: str,
    permission_mode: PermissionMode,
    label: str | None,
    principal_proof: RuntimeCredentialPrincipalProof | None,
    auth_context: ResolvedAuthContext | None,
    observe: StateObserver | None = None,
) -> CreatedRuntimeCredential:
    """Mint one credential acting as ``principal_id``, or refuse with the repair.

    ``observe`` sees the credentials the mint adds to (R12's state pin), in
    the minting transaction or, for a preview, as it reads them.
    """

    _require_auth_on()
    description = label or principal_id
    proof_digest = _principal_authority(
        instance_id=instance_id,
        principal_id=principal_id,
        mode_name=permission_mode.name.lower(),
        label=description,
        principal_proof=principal_proof,
        auth_context=auth_context,
    )
    store = get_runtime_credential_store()
    prepared = store.prepare_credential(
        instance_id=instance_id,
        label=description,
        permission_mode=permission_mode,
        created_by=None if auth_context is None else auth_context.credential_id,
        principal_id=principal_id,
    )
    if is_previewing():
        # R12: everything a mint checks has passed; nothing is stored, and the
        # prepared token is discarded by the caller.
        if proof_digest is not None and store.proof_spent(proof_digest):
            raise _proof_replayed()
        if observe is not None:
            observe(store.mint_state(prepared.record))
        return prepared
    return store.commit_prepared_credential(prepared, proof_digest=proof_digest, observe=observe)


def rotate_principal_credential(
    *,
    instance_id: str,
    credential_id: str,
    principal_proof: RuntimeCredentialPrincipalProof | None,
    auth_context: ResolvedAuthContext | None,
    observe: StateObserver | None = None,
) -> CreatedRuntimeCredential:
    """Replace one credential's token, never handing another principal's to the caller.

    The replacement acts as the same principal with the same tier and label,
    so rotating it needs exactly the authority minting it would: the request
    acts as that principal, or carries its signed consent to those terms. Any
    admin may revoke a credential; only its principal may receive a new one.
    An unbound operator credential names no principal, so the admin tier alone
    rotates it.
    """

    _require_auth_on()
    store = get_runtime_credential_store()
    existing = store.get(credential_id)
    if existing is None or existing.instance_id != instance_id or existing.revoked_at:
        raise RuntimeCredentialNotFoundError(credential_id)
    proof_digest: str | None = None
    if existing.principal_id is not None:
        proof_digest = _principal_authority(
            instance_id=instance_id,
            principal_id=existing.principal_id,
            mode_name=existing.permission_mode.name.lower(),
            label=existing.label,
            principal_proof=principal_proof,
            auth_context=auth_context,
        )
    created = store.prepare_rotated_credential(
        instance_id=instance_id,
        credential_id=credential_id,
        rotated_by=None if auth_context is None else auth_context.credential_id,
    )
    if is_previewing():
        if proof_digest is not None and store.proof_spent(proof_digest):
            raise _proof_replayed()
        if observe is not None:
            observe(store.credential_state(credential_id))
        return created
    return store.commit_prepared_rotation(
        created,
        instance_id=instance_id,
        credential_id=credential_id,
        proof_digest=proof_digest,
        observe=observe,
    )


def revoke_runtime_credential(
    *, instance_id: str, credential_id: str, observe: StateObserver | None = None
) -> RuntimeCredentialRecord:
    """Revoke one credential; a preview returns it unrevoked and changes nothing."""

    store = get_runtime_credential_store()
    if is_previewing():
        existing = store.get(credential_id)
        if existing is None or existing.instance_id != instance_id:
            raise RuntimeCredentialNotFoundError(credential_id)
        if observe is not None:
            observe(store.credential_state(credential_id))
        return existing
    return store.revoke_credential(
        instance_id=instance_id, credential_id=credential_id, observe=observe
    )


__all__ = [
    "mint_principal_credential",
    "revoke_runtime_credential",
    "rotate_principal_credential",
]
