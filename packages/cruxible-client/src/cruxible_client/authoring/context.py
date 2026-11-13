"""Shared workspace-aware target resolution for CLI and authoring SDK clients."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.file_catalog import refuse_legacy_file_catalogs
from cruxible_client.contracts.workspace_layout import (
    WORKSPACE_DIRECTORY,
    WorkspaceDirectoryConflict,
    ensure_workspace_directory,
    same_directory,
    workspace_directory_conflict,
)
from cruxible_client.contracts.workspace_root import (
    holds_workspace_directory,
    resolve_workspace_root,
)

TargetSource = Literal["explicit", "environment", "workspace", "remembered", "local"]
WorkspaceSource = Literal["explicit", "environment", "workspace", "local"]


class ContextResolutionError(ValueError):
    """A selected workspace or target layer is not a safe context source."""


class WorkspaceBinding(BaseModel):
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
    def _one_transport(self) -> WorkspaceBinding:
        if self.server_url is not None and self.server_socket is not None:
            raise ValueError("workspace binding cannot select both server URL and server socket")
        return self

    @property
    def attached(self) -> bool:
        return self.instance_id is not None and (
            self.server_url is not None or self.server_socket is not None
        )


@dataclass(frozen=True)
class ResolvedContext:
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
    #: Why the remembered instance was not used: it is bound to another
    #: transport, or it is a local instance and this workspace binds none.
    instance_transport_mismatch: str | None = None


def _normalized_transport(server_url: object, server_socket: object) -> str | None:
    if isinstance(server_url, str) and server_url:
        return server_url.rstrip("/")
    if isinstance(server_socket, str) and server_socket:
        return f"unix://{Path(server_socket).expanduser().resolve()}"
    return None


def _workspace_binding(root: Path) -> tuple[WorkspaceBinding | None, Path | None]:
    path = root / WORKSPACE_DIRECTORY / "coverage.json"
    if not path.exists():
        return None, None
    # A binding is about to be read: this root is the selected workspace.
    ensure_workspace_directory(root)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContextResolutionError(
            f"workspace binding selected from {path} cannot be resolved: {exc}"
        ) from exc
    if not resolved.is_relative_to(root):
        raise ContextResolutionError(
            f"workspace binding selected from {path} escapes workspace {root}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContextResolutionError(
            f"workspace binding selected from {path} cannot be read: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ContextResolutionError(
            f"workspace binding selected from {path} is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ContextResolutionError(
            f"workspace binding selected from {path} must contain one JSON object"
        )
    try:
        return WorkspaceBinding.model_validate(payload), path
    except ValueError as exc:
        raise ContextResolutionError(
            f"workspace binding selected from {path} is invalid: {exc}"
        ) from exc


def _file_catalog_path(root: Path) -> Path | None:
    refuse_legacy_file_catalogs(root)
    return next(
        (
            path
            for path in (root / WORKSPACE_DIRECTORY / "files.yaml", root / "files.yaml")
            if path.exists()
            and (
                path.parent.name != WORKSPACE_DIRECTORY
                or workspace_directory_conflict(root) is None
            )
        ),
        None,
    )


def _binding_conflict(*, binding_path: Path | None, file_catalog_path: Path | None) -> None:
    if binding_path is None or file_catalog_path is None:
        return
    catalog_root = (
        file_catalog_path.parent.parent
        if file_catalog_path.parent.name == WORKSPACE_DIRECTORY
        else file_catalog_path.parent
    )
    if binding_path.parent.parent == catalog_root:
        return
    raise ContextResolutionError(
        "workspace_binding_conflict: "
        f"coverage binding {binding_path} and file catalog {file_catalog_path} "
        "select different workspace roots; repair: move both files under the same "
        "<workspace>/.cruxible directory"
    )


@dataclass(frozen=True)
class _SelectedWorkspace:
    root: Path
    source: WorkspaceSource
    binding: WorkspaceBinding | None
    binding_path: Path | None
    warnings: tuple[str, ...] = ()
    #: The root holds a real ``.cruxible`` workspace directory (whether or not
    #: it binds an instance).
    marked: bool = False


def _selected_workspace(
    *,
    explicit: str | Path | None,
    environ: Mapping[str, str],
    cwd: Path,
    no_workspace: bool,
    home: Path,
) -> _SelectedWorkspace:
    """Choose the workspace boundary first, then read a binding only inside it.

    The boundary is the shared resolver's (`cruxible_client.contracts.
    workspace_root`), the same root the CLI and the MCP adapter act on. No
    binding beyond a nearer workspace directory is ever followed.
    """

    start = cwd.expanduser().resolve()
    if no_workspace or environ.get("CRUXIBLE_NO_WORKSPACE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return _SelectedWorkspace(start, "local", None, None)
    if explicit is not None:
        root = ensure_workspace_directory(Path(explicit).expanduser().resolve())
        binding, path = _workspace_binding(root)
        return _SelectedWorkspace(
            root, "explicit", binding, path, marked=holds_workspace_directory(root)
        )
    if raw := environ.get("CRUXIBLE_WORKSPACE"):
        root = ensure_workspace_directory(Path(raw).expanduser().resolve())
        binding, path = _workspace_binding(root)
        return _SelectedWorkspace(
            root, "environment", binding, path, marked=holds_workspace_directory(root)
        )

    try:
        boundary = resolve_workspace_root(start, home=home)
    except (OSError, RuntimeError):
        # A start that is gone selects no workspace.
        return _SelectedWorkspace(start, "local", None, None)
    except WorkspaceDirectoryConflict as conflict:
        if same_directory(Path(conflict.workspace), start):
            raise
        # The boundary is an ancestor that is no workspace (a 0.3 instance, say):
        # nothing here is a workspace, and nothing beyond it is searched.
        return _SelectedWorkspace(
            start, "local", None, None, (f"skipped a directory that is no workspace: {conflict}",)
        )
    assert boundary is not None
    root = boundary.root
    marked = boundary.source == "cruxible_directory"
    binding, path = _workspace_binding(root) if marked else (None, None)
    # A catalog nearer than the boundary names another root for the same files.
    for directory in (start, *start.parents):
        if not directory.is_relative_to(root):
            break
        catalog = _file_catalog_path(directory)
        if catalog is not None:
            _binding_conflict(binding_path=path, file_catalog_path=catalog)
            break
    if path is not None:
        return _SelectedWorkspace(root, "workspace", binding, path, marked=True)
    return _SelectedWorkspace(root, "local", None, None, marked=marked)


def _transport(
    *,
    server_url: object,
    server_socket: object,
    source: TargetSource,
) -> tuple[str | None, str | None, TargetSource] | None:
    if server_url is None and server_socket is None:
        return None
    if server_url is not None and not isinstance(server_url, str):
        raise ContextResolutionError(f"{source} server URL must be a string")
    if server_socket is not None and not isinstance(server_socket, str):
        raise ContextResolutionError(f"{source} server socket must be a string")
    if server_url and server_socket:
        raise ContextResolutionError(
            f"{source} target cannot select both server URL and server socket"
        )
    return server_url or None, server_socket or None, source


def _instance(value: object, *, source: TargetSource) -> tuple[str, TargetSource] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ContextResolutionError(f"{source} instance ID must be a nonempty string")
    return value, source


def _remembered_local_in_unbound_workspace(stored: Mapping[str, object], root: Path) -> str | None:
    """Why the remembered instance is not used in workspace ``root``, or None.

    The invariant: a remembered instance stands in only when discovery finished
    normally and found no workspace marker, or when it is remote. ``root`` holds
    a real ``.cruxible`` workspace directory that binds no instance, so a
    remembered LOCAL instance (a Unix socket on this machine, which could be
    another folder's) is refused here; a remembered remote host (a URL) still
    applies, as for a remote host with a local file catalog.
    """

    instance = stored.get("instance_id")
    if not isinstance(instance, str):
        return None
    recorded = stored.get("instance_transport")
    if not isinstance(recorded, str) or not recorded:
        recorded = _normalized_transport(stored.get("server_url"), stored.get("server_socket"))
    if isinstance(recorded, str) and recorded and not recorded.startswith("unix://"):
        return None
    return (
        f"workspace_unbound: {root} is a Cruxible workspace whose .cruxible/ binds no "
        f"instance, so the remembered local instance {instance} (another folder's, as far "
        "as this one knows) is not used here; repair: `cruxible init` here gives this "
        "folder its own instance, `cruxible workspace attach --instance-id ID` binds an "
        "existing one, or pass --instance-id ID"
    )


def resolve_context(
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
) -> ResolvedContext:
    """Resolve explicit > environment > workspace > remembered context.

    Transport and instance are selected independently. The workspace binding is
    eligible only when its coverage config names both one transport and an
    instance; incomplete coverage-only configs remain valid but do not retarget
    commands.

    A remembered instance is used only when discovery finished normally and
    found no ``.cruxible`` workspace marker, or when it is remote (a URL). In a
    workspace whose ``.cruxible`` binds no instance a remembered local instance
    is refused (the reason is in ``instance_transport_mismatch``); a marker that
    cannot be read, or one across an ownership or filesystem boundary, refuses
    the whole resolution (``ContextResolutionError``, ``WorkspaceBoundaryRefused``).
    """

    env = os.environ if environ is None else environ
    stored: Mapping[str, object] = {} if remembered is None else remembered
    selection = _selected_workspace(
        explicit=workspace,
        environ=env,
        cwd=Path.cwd() if cwd is None else cwd,
        no_workspace=no_workspace,
        home=(Path.home() if home is None else home).expanduser().resolve(),
    )
    root, workspace_source = selection.root, selection.source
    binding, binding_path = selection.binding, selection.binding_path
    resolution_warnings = selection.warnings
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
        if source == "remembered" and selection.marked and not attached:
            withheld = _remembered_local_in_unbound_workspace(stored, root)
            if withheld is not None:
                instance_transport_mismatch = withheld
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
                raise ContextResolutionError("remembered instance transport must be a string")
            if recorded_transport_coordinate is not None and (
                recorded_transport_coordinate.startswith("unix://")
            ):
                # Compare socket realpaths on both sides; an alias recorded
                # before normalization must not orphan the remembered instance.
                recorded_transport_coordinate = _normalized_transport(
                    None, recorded_transport_coordinate.removeprefix("unix://")
                )
            if recorded_transport_coordinate != selected_transport_coordinate:
                instance_transport_mismatch = (
                    "context_instance_transport_mismatch: the remembered instance is bound to "
                    f"{recorded_transport_coordinate or '<local>'}, but the resolved "
                    f"transport is {selected_transport_coordinate or '<local>'}; repair: "
                    "pass --instance-id <id>, re-run `cruxible context connect` with this "
                    "transport and --instance-id <id>, or attach the workspace with "
                    ".cruxible/coverage.json"
                )
                continue
        selected_instance = _instance(raw_instance, source=source)
        break
    instance_source: TargetSource
    if selected_instance is None:
        resolved_instance, instance_source = None, "local"
    else:
        resolved_instance, instance_source = selected_instance

    return ResolvedContext(
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
    "ContextResolutionError",
    "WorkspaceBinding",
    "ResolvedContext",
    "TargetSource",
    "WorkspaceSource",
    "resolve_context",
]
