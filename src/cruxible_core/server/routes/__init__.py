"""FastAPI route modules for Cruxible server."""

from __future__ import annotations

from cruxible_core.errors import InstanceNotFoundError, InstanceScopeError
from cruxible_core.server.auth import get_current_auth_context
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry


def resolve_server_instance_id(instance_id: str) -> str:
    """Validate and return an opaque governed instance ID."""
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
    return instance_id


__all__ = ["resolve_server_instance_id"]
