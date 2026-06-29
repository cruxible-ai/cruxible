"""Runtime credential management routes."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime.permissions import PermissionMode, check_permission
from cruxible_core.server.auth import get_current_auth_context
from cruxible_core.server.credentials import (
    RuntimeCredentialRecord,
    get_runtime_credential_store,
)
from cruxible_core.server.request_models import RuntimeCredentialCreateRequest
from cruxible_core.server.routes import resolve_server_instance_id

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
        label=record.label,
        permission_mode=_credential_permission_mode(record.permission_mode),
        created_at=record.created_at,
        created_by=record.created_by,
        revoked_at=record.revoked_at,
    )


@router.post(
    "/{instance_id}/runtime/credentials",
    response_model=contracts.RuntimeCredentialResult,
)
async def create_runtime_credential(
    instance_id: str,
    req: RuntimeCredentialCreateRequest,
) -> contracts.RuntimeCredentialResult:
    resolved_instance_id = _authorize_runtime_credentials(instance_id)
    auth_context = get_current_auth_context()
    created = get_runtime_credential_store().create_credential(
        instance_id=resolved_instance_id,
        label=req.label,
        permission_mode=PermissionMode[req.permission_mode.upper()],
        created_by=auth_context.principal_id if auth_context else None,
    )
    return contracts.RuntimeCredentialResult(
        credential=_record_to_contract(created.record),
        token=created.token,
    )


@router.get(
    "/{instance_id}/runtime/credentials",
    response_model=contracts.RuntimeCredentialListResult,
)
async def list_runtime_credentials(
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
async def revoke_runtime_credential(
    instance_id: str,
    credential_id: str,
) -> contracts.RuntimeCredentialResult:
    resolved_instance_id = _authorize_runtime_credentials(instance_id)
    record = get_runtime_credential_store().revoke_credential(
        instance_id=resolved_instance_id,
        credential_id=credential_id,
    )
    return contracts.RuntimeCredentialResult(
        credential=_record_to_contract(record),
    )


@router.post(
    "/{instance_id}/runtime/credentials/{credential_id}/rotate",
    response_model=contracts.RuntimeCredentialResult,
)
async def rotate_runtime_credential(
    instance_id: str,
    credential_id: str,
) -> contracts.RuntimeCredentialResult:
    resolved_instance_id = _authorize_runtime_credentials(instance_id)
    auth_context = get_current_auth_context()
    created = get_runtime_credential_store().rotate_credential(
        instance_id=resolved_instance_id,
        credential_id=credential_id,
        rotated_by=auth_context.principal_id if auth_context else None,
    )
    return contracts.RuntimeCredentialResult(
        credential=_record_to_contract(created.record),
        token=created.token,
    )
