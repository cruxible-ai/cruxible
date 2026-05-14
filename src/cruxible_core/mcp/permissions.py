"""Compatibility re-exports for runtime-owned permission policy.

Permission policy is owned by :mod:`cruxible_core.runtime.permissions`. MCP
imports from this module remain supported while callers migrate to the runtime
module directly.
"""

from cruxible_core.runtime.permissions import (
    TOOL_PERMISSIONS,
    PermissionMode,
    check_permission,
    get_current_mode,
    init_permissions,
    request_permission_scope,
    reset_permissions,
    validate_allowed_roots,
    validate_root_dir,
    validate_tool_permissions,
)

__all__ = [
    "PermissionMode",
    "TOOL_PERMISSIONS",
    "check_permission",
    "get_current_mode",
    "init_permissions",
    "request_permission_scope",
    "reset_permissions",
    "validate_allowed_roots",
    "validate_root_dir",
    "validate_tool_permissions",
]
