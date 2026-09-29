"""Mint a bearer credential in one principal's name, only with that principal's authority.

A credential acts as exactly one principal. The admin tier lets a caller manage
credentials at all; it never lets a caller mint one that acts as somebody else.
Minting as principal P therefore needs, in addition, P's own authority:

* the request already acts as P (a credential bound to P, or, with daemon auth
  off, a request claiming P), or
* P's signed consent: a fresh, single-use mint statement signed with P's key
  as registered at the accepted head.

P must be registered and active. The label is a description and decides nothing.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_client.contracts.runtime_credentials import (
    RUNTIME_CREDENTIAL_PROOF_MAX_SKEW_SECONDS,
    RuntimeCredentialPrincipalProofV1,
    verify_runtime_credential_proof,
)
from cruxible_client.contracts.temporal import parse_datetime, utc_now
from cruxible_core.errors import PrincipalRefusedError
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.auth import ResolvedAuthContext
from cruxible_core.server.credentials import (
    CreatedRuntimeCredential,
    get_runtime_credential_store,
)
from cruxible_core.service.identity import principal_refusal


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
        repair=RepairOperationV1(
            operation="credential.mint",
            arguments={"principal_id": principal_id, "permission_mode": permission_mode},
        ),
    )


def _proof_invalid(principal_id: str, permission_mode: str, reason: str) -> PrincipalRefusedError:
    return PrincipalRefusedError(
        "runtime_credential.principal_proof_invalid",
        f"the signed consent for {principal_id!r} is not valid: {reason}; repair: sign a "
        f"fresh one with `{_mint_command(principal_id, permission_mode)}`",
        repair=RepairOperationV1(
            operation="credential.mint",
            arguments={"principal_id": principal_id, "permission_mode": permission_mode},
        ),
    )


def _verified_proof_digest(
    proof: RuntimeCredentialPrincipalProofV1,
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


def _proof_digest(proof: RuntimeCredentialPrincipalProofV1) -> str:
    """The single-use identity of one signed consent."""

    return "sha256:" + hashlib.sha256(canonical_bytes(proof.model_dump(mode="json"))).hexdigest()


def mint_principal_credential(
    *,
    instance_id: str,
    principal_id: str,
    permission_mode: PermissionMode,
    label: str | None,
    principal_proof: RuntimeCredentialPrincipalProofV1 | None,
    auth_context: ResolvedAuthContext | None,
) -> CreatedRuntimeCredential:
    """Mint one credential acting as ``principal_id``, or refuse with the repair."""

    mode_name = permission_mode.name.lower()
    instance = get_playbill_manager().get(instance_id)
    refusal = principal_refusal(instance, principal_id, configured=True)
    if refusal is not None:
        raise refusal
    description = label or principal_id
    proof_digest: str | None = None
    acts_as_principal = auth_context is not None and auth_context.principal_id == principal_id
    if principal_proof is not None:
        registered = next(
            item
            for item in instance.accepted_history()[-1].principals.principals
            if item.principal_id == principal_id
        )
        proof_digest = _verified_proof_digest(
            principal_proof,
            instance_id=instance_id,
            principal_id=principal_id,
            public_key=registered.public_key,
            permission_mode=mode_name,
            label=description,
        )
    elif not acts_as_principal:
        raise _authority_required(principal_id, mode_name)
    return get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label=description,
        permission_mode=permission_mode,
        created_by=None if auth_context is None else auth_context.credential_id,
        principal_id=principal_id,
        proof_digest=proof_digest,
    )


__all__ = ["mint_principal_credential"]
