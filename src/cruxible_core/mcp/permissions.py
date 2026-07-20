"""Compatibility re-exports for runtime-owned permission policy.

Permission policy is owned by :mod:`cruxible_core.runtime.permissions`. MCP
imports from this module remain supported while callers migrate to the runtime
module directly.
"""

from cruxible_core.runtime.permissions import (
    PERMISSION_REQUIREMENTS,
    RUNTIME_OPERATION_PERMISSIONS,
    TOOL_PERMISSIONS,
    PermissionMode,
    check_permission,
    clamp_to_capability_ceiling,
    get_capability_ceiling,
    get_current_mode,
    init_permissions,
    request_instance_scope,
    request_permission_scope,
    reset_permissions,
    validate_allowed_roots,
    validate_root_dir,
    validate_tool_permissions,
)

__all__ = [
    "PermissionMode",
    "TOOL_PERMISSIONS",
    "RUNTIME_OPERATION_PERMISSIONS",
    "PERMISSION_REQUIREMENTS",
    "check_permission",
    "clamp_to_capability_ceiling",
    "get_capability_ceiling",
    "get_current_mode",
    "init_permissions",
    "request_instance_scope",
    "request_permission_scope",
    "reset_permissions",
    "validate_allowed_roots",
    "validate_root_dir",
    "validate_tool_permissions",
]
