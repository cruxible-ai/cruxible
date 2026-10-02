"""Runtime credential management routes."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_client.contracts.change_control import ChangeControlRequestV1
from cruxible_core.runtime.permissions import PermissionMode, check_permission
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.auth import get_current_auth_context
from cruxible_core.server.credential_minting import (
    mint_principal_credential,
    revoke_runtime_credential,
    rotate_principal_credential,
)
from cruxible_core.server.credentials import (
    RuntimeCredentialRecord,
    get_runtime_credential_store,
)
from cruxible_core.server.request_models import (
    RuntimeCredentialCreateRequest,
    RuntimeCredentialRotateRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id
from cruxible_core.service.change_preview import change_scope, full_coordinate

router = APIRouter(prefix="/api/v1", tags=["runtime-credentials"])


def _authorize_runtime_credentials(instance_id: str) -> str:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    check_permission("cruxible_runtime_credentials", instance_id=resolved_instance_id)
    return resolved_instance_id


def _credential_permission_mode(
    permission_mode: PermissionMode,
) -> contracts.RuntimeCredentialPermissionMode:
    return cast(contracts.RuntimeCredentialPermissionMode, permission_mode.name.lower())


def _record_to_contract(
    record: RuntimeCredentialRecord,
) -> contracts.RuntimeCredentialMetadata:
    return contracts.RuntimeCredentialMetadata(
        credential_id=record.credential_id,
        instance_id=record.instance_id,
        principal_id=record.principal_id,
        label=record.label,
        permission_mode=_credential_permission_mode(record.permission_mode),
        created_at=record.created_at,
        created_by=record.created_by,
        revoked_at=record.revoked_at,
    )


def _coordinate(instance_id: str) -> contracts.PlaybillAcceptedCoordinate | None:
    instance = get_playbill_manager().initialized(instance_id)
    return None if instance is None else full_coordinate(instance)


@router.post(
    "/{instance_id}/runtime/credentials",
    response_model=contracts.RuntimeCredentialResult,
)
def create_runtime_credential(
    instance_id: str,
    req: RuntimeCredentialCreateRequest,
) -> contracts.RuntimeCredentialResult:
    resolved_instance_id = _authorize_runtime_credentials(instance_id)
    with change_scope(
        get_playbill_manager().initialized(resolved_instance_id),
        dry_run=req.dry_run,
        at=req.at,
        kind="direct",
        operation="credential.mint",
        describe=f"minting a credential for {req.principal_id}",
    ) as mode:
        created = mint_principal_credential(
            instance_id=resolved_instance_id,
            principal_id=req.principal_id,
            permission_mode=PermissionMode[req.permission_mode.upper()],
            label=req.label,
            principal_proof=req.principal_proof,
            auth_context=get_current_auth_context(),
        )
    return contracts.RuntimeCredentialResult(
        status="would_mint" if mode.previewing else "minted",
        credential=_record_to_contract(created.record),
        token=None if mode.previewing else created.token,
        coordinate=_coordinate(resolved_instance_id),
    )


@router.get(
    "/{instance_id}/runtime/credentials",
    response_model=contracts.RuntimeCredentialListResult,
)
def list_runtime_credentials(
    instance_id: str,
) -> contracts.RuntimeCredentialListResult:
    resolved_instance_id = _authorize_runtime_credentials(instance_id)
    records = get_runtime_credential_store().list_for_instance(resolved_instance_id)
    return contracts.RuntimeCredentialListResult(
        credentials=[_record_to_contract(record) for record in records],
    )


@router.post(
    "/{instance_id}/runtime/credentials/{credential_id}/revoke",
    response_model=contracts.RuntimeCredentialResult,
)
def revoke_runtime_credential_route(
    instance_id: str,
    credential_id: str,
    req: ChangeControlRequestV1 | None = None,
) -> contracts.RuntimeCredentialResult:
    """Revoke one credential. It cannot be undone: previews unless committed with ``at``."""

    resolved_instance_id = _authorize_runtime_credentials(instance_id)
    control = req or ChangeControlRequestV1()
    with change_scope(
        get_playbill_manager().initialized(resolved_instance_id),
        dry_run=control.dry_run,
        at=control.at,
        kind="irreversible",
        operation="credential.revoke",
        describe=f"revoking credential {credential_id}",
    ) as mode:
        record = revoke_runtime_credential(
            instance_id=resolved_instance_id, credential_id=credential_id
        )
    return contracts.RuntimeCredentialResult(
        status="would_revoke" if mode.previewing else "revoked",
        credential=_record_to_contract(record),
        coordinate=_coordinate(resolved_instance_id),
    )


@router.post(
    "/{instance_id}/runtime/credentials/{credential_id}/rotate",
    response_model=contracts.RuntimeCredentialResult,
)
def rotate_runtime_credential(
    instance_id: str,
    credential_id: str,
    req: RuntimeCredentialRotateRequest | None = None,
) -> contracts.RuntimeCredentialResult:
    """Replace one credential's token; the old one is revoked, which cannot be undone."""

    resolved_instance_id = _authorize_runtime_credentials(instance_id)
    request = req or RuntimeCredentialRotateRequest()
    with change_scope(
        get_playbill_manager().initialized(resolved_instance_id),
        dry_run=request.dry_run,
        at=request.at,
        kind="irreversible",
        operation="credential.rotate",
        describe=f"rotating credential {credential_id}",
    ) as mode:
        created = rotate_principal_credential(
            instance_id=resolved_instance_id,
            credential_id=credential_id,
            principal_proof=request.principal_proof,
            auth_context=get_current_auth_context(),
        )
    return contracts.RuntimeCredentialResult(
        status="would_rotate" if mode.previewing else "rotated",
        credential=_record_to_contract(created.record),
        token=None if mode.previewing else created.token,
        coordinate=_coordinate(resolved_instance_id),
    )
