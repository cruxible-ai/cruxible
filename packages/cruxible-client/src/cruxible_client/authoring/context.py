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
    warnings: tuple[str, ...] = ()
    instance_transport_mismatch: str | None = None


def _normalized_transport(server_url: object, server_socket: object) -> str | None:
    if isinstance(server_url, str) and server_url:
        return server_url.rstrip("/")
    if isinstance(server_socket, str) and server_socket:
        return f"unix://{Path(server_socket).expanduser().resolve()}"
    return None


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


def _walk_roots(start: Path, *, home: Path) -> tuple[Path, ...]:
    device = next(
        (candidate.stat().st_dev for candidate in (start, *start.parents) if candidate.exists()),
        None,
    )
    if device is None:
        raise PlaybillContextResolutionError(f"workspace discovery cannot inspect {start}")
    if start.is_relative_to(home):
        ceiling = home
    else:
        ceiling = start.parent if start.parent.parent != start.parent else start
        for candidate in (start, *start.parents):
            if candidate.parent == candidate:
                break
            try:
                if (candidate / ".git").exists():
                    ceiling = candidate
                    break
            except OSError:
                break
    roots: list[Path] = []
    for root in (start, *start.parents):
        try:
            if root.stat().st_dev != device:
                break
        except OSError:
            break
        roots.append(root)
        if root == ceiling:
            break
    return tuple(roots)


def _source_catalog_path(root: Path) -> Path | None:
    return next(
        (
            path
            for path in (root / ".playbill" / "sources.yaml", root / "sources.yaml")
            if path.exists()
        ),
        None,
    )


def _binding_conflict(*, binding_path: Path | None, source_catalog_path: Path | None) -> None:
    if binding_path is None or source_catalog_path is None:
        return
    if binding_path.parent.parent == source_catalog_path.parent.parent:
        return
    raise PlaybillContextResolutionError(
        "workspace_binding_conflict: "
        f"coverage binding {binding_path} and source catalog {source_catalog_path} "
        "select different workspace roots; repair: move both files under the same "
        "<workspace>/.playbill directory"
    )


def _selected_workspace(
    *,
    explicit: str | Path | None,
    environ: Mapping[str, str],
    cwd: Path,
    no_workspace: bool,
    home: Path,
) -> tuple[
    Path,
    WorkspaceSource,
    PlaybillWorkspaceBinding | None,
    Path | None,
    tuple[str, ...],
]:
    start = cwd.expanduser().resolve()
    if no_workspace or environ.get("CRUXIBLE_NO_WORKSPACE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return start, "local", None, None, ()
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        binding, path = _workspace_binding(root)
        return root, "explicit", binding, path, ()
    if raw := environ.get("CRUXIBLE_PLAYBILL_WORKSPACE"):
        root = Path(raw).expanduser().resolve()
        binding, path = _workspace_binding(root)
        return root, "environment", binding, path, ()

    warnings: list[str] = []
    selected: tuple[Path, PlaybillWorkspaceBinding, Path] | None = None
    source_catalog_path: Path | None = None
    for root in _walk_roots(start, home=home):
        source_catalog_path = source_catalog_path or _source_catalog_path(root)
        try:
            binding, path = _workspace_binding(root)
        except PlaybillContextResolutionError as exc:
            if root == start:
                raise
            warnings.append(f"skipped invalid ancestor workspace binding: {exc}")
            continue
        if path is not None:
            assert binding is not None
            selected = (root, binding, path)
            break
    if selected is None:
        return start, "local", None, None, tuple(warnings)
    root, binding, path = selected
    if source_catalog_path is None:
        remaining = _walk_roots(root, home=home)
        source_catalog_path = next(
            (candidate for item in remaining if (candidate := _source_catalog_path(item))),
            None,
        )
    _binding_conflict(binding_path=path, source_catalog_path=source_catalog_path)
    return root, "workspace", binding, path, tuple(warnings)


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
    no_workspace: bool = False,
    home: Path | None = None,
) -> ResolvedPlaybillContext:
    """Resolve explicit > environment > workspace > remembered context.

    Transport and instance are selected independently. The workspace binding is
    eligible only when its coverage config names both one transport and an
    instance; incomplete coverage-only configs remain valid but do not retarget
    commands.
    """

    env = os.environ if environ is None else environ
    stored: Mapping[str, object] = {} if remembered is None else remembered
    root, workspace_source, binding, binding_path, resolution_warnings = _selected_workspace(
        explicit=workspace,
        environ=env,
        cwd=Path.cwd() if cwd is None else cwd,
        no_workspace=no_workspace,
        home=(Path.home() if home is None else home).expanduser().resolve(),
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
    instance_transport_mismatch = None
    for raw_instance, source in raw_instance_layers:
        if raw_instance is None:
            continue
        if source == "remembered":
            selected_transport_coordinate = _normalized_transport(resolved_url, resolved_socket)
            recorded_transport_coordinate = stored.get("instance_transport")
            if recorded_transport_coordinate is None:
                recorded_transport_coordinate = _normalized_transport(
                    stored.get("server_url"), stored.get("server_socket")
                )
            if recorded_transport_coordinate is not None and not isinstance(
                recorded_transport_coordinate, str
            ):
                raise PlaybillContextResolutionError(
                    "remembered instance transport must be a string"
                )
            if recorded_transport_coordinate != selected_transport_coordinate:
                instance_transport_mismatch = (
                    "context_instance_transport_mismatch: the remembered instance is bound to "
                    f"{recorded_transport_coordinate or '<local>'}, but the resolved "
                    f"transport is {selected_transport_coordinate or '<local>'}; repair: "
                    "pass --instance-id <id> or attach the workspace with "
                    ".playbill/coverage.json"
                )
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
        warnings=resolution_warnings,
        instance_transport_mismatch=instance_transport_mismatch,
    )


__all__ = [
    "PlaybillContextResolutionError",
    "PlaybillWorkspaceBinding",
    "ResolvedPlaybillContext",
    "TargetSource",
    "WorkspaceSource",
    "resolve_playbill_context",
]
