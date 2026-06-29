"""Shared server route path constants and lightweight template matching."""

from __future__ import annotations

API_V1_PREFIX = "/api/v1"
HEALTH_PATH = "/health"
VERSION_PATH = "/version"
UI_PATH = "/ui"

RUNTIME_BOOTSTRAP_CLAIM_PATH = "/{instance_id}/runtime/bootstrap/claim"
HOSTED_INSTANCE_INIT_PATH = "/runtime/instances"

# Daemon-wide server-operation routes. These act on the whole shared daemon
# (global metadata, in-place re-exec, restore-before-target-known) rather than a
# single tenant's instance, so they are authorized for an unscoped operator
# credential (the runtime bootstrap secret) rather than an instance-scoped one.
SERVER_INFO_PATH = "/server/info"
SERVER_RESTART_PATH = "/server/restart"
INSTANCE_RESTORE_PATH = "/instances/restore"


def api_v1_path(path: str) -> str:
    """Return a full API v1 path from a router-relative path."""
    return f"{API_V1_PREFIX}{path}"


def is_ui_static_path(path: str) -> bool:
    """Return whether *path* targets the packaged browser UI."""
    return path == UI_PATH or path.startswith(f"{UI_PATH}/")


def route_template_matches(path: str, template: str) -> bool:
    """Return whether *path* matches a route template with `{param}` segments."""
    path_parts = _path_parts(path)
    template_parts = _path_parts(template)
    if len(path_parts) != len(template_parts):
        return False
    return all(
        _template_part_matches(path_part, template_part)
        for path_part, template_part in zip(path_parts, template_parts)
    )


def _path_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.strip("/").split("/") if part)


def _template_part_matches(path_part: str, template_part: str) -> bool:
    if template_part.startswith("{") and template_part.endswith("}"):
        return bool(path_part)
    return path_part == template_part
