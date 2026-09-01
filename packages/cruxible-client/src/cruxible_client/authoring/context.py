"""Shared workspace-aware target resolution for CLI and authoring SDK clients."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

TargetSource = Literal["explicit", "environment", "workspace", "remembered", "local"]
WorkspaceSource = Literal["explicit", "environment", "workspace", "local"]


class PlaybillContextResolutionError(ValueError):
    """A selected workspace or target layer is not a safe context source."""


class PlaybillWorkspaceBinding(BaseModel):
    """The target-bearing subset of a workspace coverage configuration."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    tag: Literal[
        "playbill-coverage-workspace-config-v1",
        "playbill-coverage-workspace-config-v2",
    ] = "playbill-coverage-workspace-config-v1"
    server_url: str | None = None
    server_socket: str | None = None
    instance_id: str | None = None

    @field_validator("server_url", "server_socket", "instance_id")
    @classmethod
    def _nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("workspace target fields must be nonempty when set")
        return value

    @model_validator(mode="after")
    def _one_transport(self) -> PlaybillWorkspaceBinding:
        if self.server_url is not None and self.server_socket is not None:
            raise ValueError("workspace binding cannot select both server URL and server socket")
        return self

    @property
    def attached(self) -> bool:
        return self.instance_id is not None and (
            self.server_url is not None or self.server_socket is not None
        )


@dataclass(frozen=True)
class ResolvedPlaybillContext:
    """Resolved workspace and independently sourced target components."""

    server_url: str | None
    server_socket: str | None
    instance_id: str | None
    transport_source: TargetSource
    instance_source: TargetSource
    workspace: Path
    workspace_source: WorkspaceSource
    workspace_binding_path: Path | None
    workspace_attached: bool


def _workspace_binding(root: Path) -> tuple[PlaybillWorkspaceBinding | None, Path | None]:
    path = root / ".playbill" / "coverage.json"
    if not path.exists():
        return None, None
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PlaybillContextResolutionError(
            f"workspace binding selected from {path} cannot be resolved: {exc}"
        ) from exc
    if not resolved.is_relative_to(root):
        raise PlaybillContextResolutionError(
            f"workspace binding selected from {path} escapes workspace {root}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlaybillContextResolutionError(
            f"workspace binding selected from {path} cannot be read: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PlaybillContextResolutionError(
            f"workspace binding selected from {path} is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise PlaybillContextResolutionError(
            f"workspace binding selected from {path} must contain one JSON object"
        )
    try:
        return PlaybillWorkspaceBinding.model_validate(payload), path
    except ValueError as exc:
        raise PlaybillContextResolutionError(
            f"workspace binding selected from {path} is invalid: {exc}"
        ) from exc


def _selected_workspace(
    *,
    explicit: str | Path | None,
    environ: Mapping[str, str],
    cwd: Path,
) -> tuple[Path, WorkspaceSource, PlaybillWorkspaceBinding | None, Path | None]:
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        binding, path = _workspace_binding(root)
        return root, "explicit", binding, path
    if raw := environ.get("CRUXIBLE_PLAYBILL_WORKSPACE"):
        root = Path(raw).expanduser().resolve()
        binding, path = _workspace_binding(root)
        return root, "environment", binding, path
    start = cwd.expanduser().resolve()
    for root in (start, *start.parents):
        binding, path = _workspace_binding(root)
        if path is not None:
            return root, "workspace", binding, path
    return start, "local", None, None


def _transport(
    *,
    server_url: object,
    server_socket: object,
    source: TargetSource,
) -> tuple[str | None, str | None, TargetSource] | None:
    if server_url is None and server_socket is None:
        return None
    if server_url is not None and not isinstance(server_url, str):
        raise PlaybillContextResolutionError(f"{source} server URL must be a string")
    if server_socket is not None and not isinstance(server_socket, str):
        raise PlaybillContextResolutionError(f"{source} server socket must be a string")
    if server_url and server_socket:
        raise PlaybillContextResolutionError(
            f"{source} target cannot select both server URL and server socket"
        )
    return server_url or None, server_socket or None, source


def _instance(value: object, *, source: TargetSource) -> tuple[str, TargetSource] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PlaybillContextResolutionError(f"{source} instance ID must be a nonempty string")
    return value, source


def resolve_playbill_context(
    *,
    server_url: str | None = None,
    server_socket: str | None = None,
    instance_id: str | None = None,
    workspace: str | Path | None = None,
    remembered: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> ResolvedPlaybillContext:
    """Resolve explicit > environment > workspace > remembered context.

    Transport and instance are selected independently. The workspace binding is
    eligible only when its coverage config names both one transport and an
    instance; incomplete coverage-only configs remain valid but do not retarget
    commands.
    """

    env = os.environ if environ is None else environ
    stored: Mapping[str, object] = {} if remembered is None else remembered
    root, workspace_source, binding, binding_path = _selected_workspace(
        explicit=workspace,
        environ=env,
        cwd=Path.cwd() if cwd is None else cwd,
    )
    attached = binding is not None and binding.attached

    raw_transport_layers: tuple[tuple[object, object, TargetSource], ...] = (
        (server_url, server_socket, "explicit"),
        (
            env.get("CRUXIBLE_SERVER_URL"),
            env.get("CRUXIBLE_SERVER_SOCKET"),
            "environment",
        ),
        (
            binding.server_url if attached and binding is not None else None,
            binding.server_socket if attached and binding is not None else None,
            "workspace",
        ),
        (stored.get("server_url"), stored.get("server_socket"), "remembered"),
    )
    selected_transport = None
    for raw_url, raw_socket, source in raw_transport_layers:
        if raw_url is None and raw_socket is None:
            continue
        selected_transport = _transport(
            server_url=raw_url,
            server_socket=raw_socket,
            source=source,
        )
        break
    transport_source: TargetSource
    if selected_transport is None:
        resolved_url, resolved_socket, transport_source = None, None, "local"
    else:
        resolved_url, resolved_socket, transport_source = selected_transport

    raw_instance_layers: tuple[tuple[object, TargetSource], ...] = (
        (instance_id, "explicit"),
        (env.get("CRUXIBLE_INSTANCE_ID"), "environment"),
        (binding.instance_id if attached and binding is not None else None, "workspace"),
        (stored.get("instance_id"), "remembered"),
    )
    selected_instance = None
    for raw_instance, source in raw_instance_layers:
        if raw_instance is None:
            continue
        selected_instance = _instance(raw_instance, source=source)
        break
    instance_source: TargetSource
    if selected_instance is None:
        resolved_instance, instance_source = None, "local"
    else:
        resolved_instance, instance_source = selected_instance

    return ResolvedPlaybillContext(
        server_url=resolved_url,
        server_socket=resolved_socket,
        instance_id=resolved_instance,
        transport_source=transport_source,
        instance_source=instance_source,
        workspace=root,
        workspace_source=workspace_source,
        workspace_binding_path=binding_path,
        workspace_attached=attached,
    )


__all__ = [
    "PlaybillContextResolutionError",
    "PlaybillWorkspaceBinding",
    "ResolvedPlaybillContext",
    "TargetSource",
    "WorkspaceSource",
    "resolve_playbill_context",
]
