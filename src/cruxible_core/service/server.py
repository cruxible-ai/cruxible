"""Server-level service helpers that do not depend on a specific instance."""

from __future__ import annotations

from cruxible_core import __version__
from cruxible_core.server.config import (
    get_server_state_dir,
    is_server_auth_enabled,
    is_server_required,
)
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.registry import get_registry
from cruxible_core.service.types import ServerInfoServiceResult


def service_server_info() -> ServerInfoServiceResult:
    """Return live daemon metadata for local hardening and diagnostics."""
    credential_store = get_runtime_credential_store()
    return ServerInfoServiceResult(
        server_required=is_server_required(),
        state_dir=str(get_server_state_dir()),
        version=__version__,
        instance_count=get_registry().count_instances(),
        auth_enabled=is_server_auth_enabled(),
        auth_required=credential_store.is_auth_required(),
    )
