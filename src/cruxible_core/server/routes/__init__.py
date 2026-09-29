"""FastAPI route modules for Cruxible server."""

from __future__ import annotations

from cruxible_client.contracts.errors import PlaybillBootstrapError
from cruxible_core.errors import InstanceNotFoundError, InstanceScopeError
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.auth import ResolvedAuthContext, get_current_auth_context
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry
from cruxible_core.service.identity import principal_refusal


def resolve_server_instance_id(instance_id: str, *, identity_read: bool = False) -> str:
    """Validate and return an opaque governed instance ID.

    A request that names a principal must name one registered and active on this
    instance before it reads or writes anything. ``identity_read`` exempts the
    reads that explain the caller's own standing (``whoami``, ``orient``): they
    are how a refused caller learns why and what to run.
    """
    record = get_registry().get(instance_id)
    if record is None or record.backend != GOVERNED_DAEMON_BACKEND:
        raise InstanceNotFoundError(instance_id)
    auth_context = get_current_auth_context()
    if (
        auth_context is not None
        and auth_context.instance_scope is not None
        and auth_context.instance_scope != instance_id
    ):
        raise InstanceScopeError(instance_id, auth_context.instance_scope)
    if auth_context is not None and not identity_read:
        _require_active_principal(instance_id, auth_context)
    return instance_id


def _require_active_principal(instance_id: str, auth_context: ResolvedAuthContext) -> None:
    if auth_context.credential_type != "principal_claim" or auth_context.principal_id is None:
        return
    try:
        instance = get_playbill_manager().get(instance_id)
    except PlaybillBootstrapError:
        # No registry exists before init; init itself checks the owner it names.
        return
    refusal = principal_refusal(instance, auth_context.principal_id, configured=True)
    if refusal is not None:
        raise refusal


__all__ = ["resolve_server_instance_id"]
