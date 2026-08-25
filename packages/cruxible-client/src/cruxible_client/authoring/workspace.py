"""Client-owned Playbill floor verification and workspace replacement.

The daemon returns inert bytes. This module is the shared CLI/MCP adapter that
verifies those bytes and writes them locally without ever sending a path to the
daemon.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import shutil
import tempfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from cruxible_client import contracts
from cruxible_client.authoring.selectors import WorkspaceSources

_CONFIG_PATH = PurePosixPath(".playbill/coverage.json")
_FLOOR_DOMAIN = "playbill-floor-export-v2"


class PlaybillWorkspaceError(ValueError):
    """A client workspace or exported floor failed deterministic validation."""


class _FloorClient(Protocol):
    def activate_playbill_proposal(
        self, instance_id: str, proposal_id: str
    ) -> contracts.PlaybillActivationReceipt: ...

    def export_playbill_floor(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillFloorExport: ...


def _canonical_json(value: object) -> bytes:
    def normalize(item: object) -> object:
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            raise PlaybillWorkspaceError("floor manifest contains a floating-point value")
        if isinstance(item, str):
            normalized = unicodedata.normalize("NFC", item)
            if normalized != item:
                raise PlaybillWorkspaceError("floor manifest text is not NFC-normalized")
            return item
        if isinstance(item, list):
            return [normalize(value) for value in item]
        if isinstance(item, Mapping):
            normalized_map: dict[str, object] = {}
            for key, value in item.items():
                if not isinstance(key, str):
                    raise PlaybillWorkspaceError("floor manifest keys must be strings")
                normalized_key = unicodedata.normalize("NFC", key)
                if normalized_key in normalized_map:
                    raise PlaybillWorkspaceError("floor manifest keys collide after NFC")
                normalized_map[normalized_key] = normalize(value)
            return normalized_map
        raise PlaybillWorkspaceError(f"floor manifest contains unsupported {type(item).__name__}")

    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _typed_digest(domain: str, payload: Mapping[str, object]) -> str:
    if "tag" in payload:
        raise PlaybillWorkspaceError("floor digest payload may not supply tag")
    digest = hashlib.sha256(_canonical_json({"tag": domain, **payload})).hexdigest()
    return f"sha256:{digest}"


def _safe_export_path(value: object) -> str:
    if not isinstance(value, str):
        raise PlaybillWorkspaceError("floor export path is not text")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise PlaybillWorkspaceError(f"floor export path escapes its root: {value}")
    return value


def verified_floor_files(export: contracts.PlaybillFloorExport) -> dict[str, bytes]:
    """Verify the v2 envelope, manifest, inventory, and bytes."""

    if export.tag != _FLOOR_DOMAIN:
        raise PlaybillWorkspaceError("configured floor refresh requires playbill-floor-export-v2")
    manifest = export.manifest
    if manifest.get("tag") != "playbill-floor-manifest-v2":
        raise PlaybillWorkspaceError("floor export manifest has an unsupported tag")
    if manifest.get("format") != _FLOOR_DOMAIN:
        raise PlaybillWorkspaceError("floor export manifest has an unsupported format")
    coordinate = manifest.get("coordinate")
    if coordinate != export.coordinate.model_dump(mode="json"):
        raise PlaybillWorkspaceError("floor export envelope and manifest coordinates differ")
    inventory = manifest.get("files")
    if not isinstance(inventory, list):
        raise PlaybillWorkspaceError("floor export manifest inventory is not a list")

    decoded: dict[str, bytes] = {}
    for exported_file in export.files:
        path = _safe_export_path(exported_file.path)
        if path in decoded:
            raise PlaybillWorkspaceError(f"floor export repeats path: {path}")
        try:
            decoded[path] = base64.b64decode(exported_file.content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise PlaybillWorkspaceError("floor export contains invalid base64 bytes") from exc

    expected_paths = {"manifest.json"}
    for raw_item in inventory:
        if not isinstance(raw_item, Mapping):
            raise PlaybillWorkspaceError("floor manifest inventory entry is not an object")
        expected_paths.add(_safe_export_path(raw_item.get("path")))
    if set(decoded) != expected_paths:
        raise PlaybillWorkspaceError("floor export files differ from the manifest inventory")
    try:
        decoded_manifest = json.loads(decoded["manifest.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaybillWorkspaceError("floor export manifest bytes are invalid") from exc
    if decoded_manifest != manifest:
        raise PlaybillWorkspaceError("floor export manifest bytes differ from the envelope")

    for raw_item in inventory:
        assert isinstance(raw_item, Mapping)
        path = _safe_export_path(raw_item.get("path"))
        content = decoded[path]
        byte_length = raw_item.get("byte_length")
        content_digest = raw_item.get("content_digest")
        if not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length < 0:
            raise PlaybillWorkspaceError(f"floor export byte length is invalid for {path}")
        if len(content) != byte_length:
            raise PlaybillWorkspaceError(f"floor export byte length differs for {path}")
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if digest != content_digest:
            raise PlaybillWorkspaceError(f"floor export content digest differs for {path}")
    expected_floor_digest = _typed_digest(_FLOOR_DOMAIN, {"files": inventory})
    if manifest.get("floor_digest") != expected_floor_digest:
        raise PlaybillWorkspaceError("floor export root digest differs from its inventory")
    return decoded


def _workspace_root(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve()


def _relative_destination(workspace: Path, relative_path: str) -> Path:
    path = PurePosixPath(relative_path)
    if (
        not relative_path
        or path.is_absolute()
        or path.as_posix() != relative_path
        or ".." in path.parts
        or not path.parts
        or path.parts[0] == ".playbill"
    ):
        raise PlaybillWorkspaceError(
            "floor output path must be a normalized workspace-relative directory"
        )
    destination = workspace / relative_path
    try:
        resolved = destination.resolve()
    except OSError as exc:
        raise PlaybillWorkspaceError(f"could not resolve configured floor output: {exc}") from exc
    if not resolved.is_relative_to(workspace):
        raise PlaybillWorkspaceError("configured floor output escapes the workspace root")
    return destination


def configured_floor_path(workspace: str | Path) -> str | None:
    """Return the declared v2 floor path, or ``None`` when absent/unconfigured."""

    root = _workspace_root(workspace)
    config_path = root / _CONFIG_PATH
    if not config_path.exists():
        return None
    try:
        config: Any = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaybillWorkspaceError(f"coverage config is invalid: {exc}") from exc
    if not isinstance(config, Mapping):
        raise PlaybillWorkspaceError("coverage config is not an object")
    if config.get("tag") != "playbill-coverage-workspace-config-v2":
        return None
    output = config.get("floor_output")
    if output is None:
        return None
    if not isinstance(output, Mapping):
        raise PlaybillWorkspaceError("coverage floor_output is not an object")
    if output.get("tag") != "playbill-floor-output-v1" or output.get("format") != _FLOOR_DOMAIN:
        raise PlaybillWorkspaceError("coverage floor_output has an unsupported profile")
    value = output.get("path")
    if not isinstance(value, str):
        raise PlaybillWorkspaceError("coverage floor_output path is not text")
    _relative_destination(root, value)
    return value


def _replace_exact(destination: Path, files: Mapping[str, bytes], *, root: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.parent.resolve().is_relative_to(root):
        raise PlaybillWorkspaceError("configured floor output parent escapes the workspace root")
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.playbill-floor-", dir=destination.parent)
    )
    backup = destination.parent / f".{destination.name}.playbill-backup-{secrets.token_hex(8)}"
    moved_old = False
    installed = False
    try:
        stage_root = stage.resolve()
        for path, content in files.items():
            target = (stage / path).resolve()
            if not target.is_relative_to(stage_root):  # pragma: no cover - prevalidated
                raise PlaybillWorkspaceError(f"floor export path escapes its stage: {path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if destination.exists() or destination.is_symlink():
            destination.rename(backup)
            moved_old = True
        stage.rename(destination)
        installed = True
    except Exception:
        if moved_old and not (destination.exists() or destination.is_symlink()):
            backup.rename(destination)
            moved_old = False
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if installed and moved_old and backup.exists():
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()


def materialize_playbill_floor(
    workspace: str | Path,
    *,
    relative_path: str,
    export: contracts.PlaybillFloorExport,
    force: bool = True,
) -> contracts.PlaybillWorkspaceFloorWriteResult:
    """Verify and exactly replace one workspace-relative floor directory."""

    root = _workspace_root(workspace)
    destination = _relative_destination(root, relative_path)
    if destination.exists() and any(destination.iterdir()) and not force:
        raise PlaybillWorkspaceError(
            f"refusing to write the floor into a non-empty directory: {destination}"
        )
    files = verified_floor_files(export)
    _replace_exact(destination, files, root=root)
    return contracts.PlaybillWorkspaceFloorWriteResult(
        path=relative_path,
        destination=str(destination),
        floor_digest=str(export.manifest["floor_digest"]),
        coordinate=export.coordinate,
        file_count=len(export.files),
    )


def inspect_workspace_floor(
    workspace: str | Path,
    *,
    current_coordinate: contracts.PlaybillAcceptedCoordinate | None,
) -> contracts.PlaybillWorkspaceFloorStatus:
    """Compare the installed configured floor with a daemon coordinate."""

    root = _workspace_root(workspace)
    try:
        relative_path = configured_floor_path(root)
    except PlaybillWorkspaceError as exc:
        return contracts.PlaybillWorkspaceFloorStatus(status="invalid", message=str(exc))
    if relative_path is None:
        return contracts.PlaybillWorkspaceFloorStatus(
            status="not_configured", current_coordinate=current_coordinate
        )
    destination = _relative_destination(root, relative_path)
    manifest_path = destination / "manifest.json"
    if not manifest_path.is_file():
        return contracts.PlaybillWorkspaceFloorStatus(
            status="missing",
            path=relative_path,
            destination=str(destination),
            current_coordinate=current_coordinate,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        installed = contracts.PlaybillAcceptedCoordinate.model_validate(manifest["coordinate"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return contracts.PlaybillWorkspaceFloorStatus(
            status="invalid",
            path=relative_path,
            destination=str(destination),
            current_coordinate=current_coordinate,
            message=str(exc),
        )
    status: Literal["current", "stale"]
    if current_coordinate is not None and installed == current_coordinate:
        status = "current"
    else:
        status = "stale"
    return contracts.PlaybillWorkspaceFloorStatus(
        status=status,
        path=relative_path,
        destination=str(destination),
        installed_coordinate=installed,
        current_coordinate=current_coordinate,
    )


def observe_playbill_next_workspace(workspace: str | Path) -> dict[str, object]:
    """Observe the configured floor and every resolvable installed catalog source.

    The daemon compares ``installed_coordinate`` with its resolved coordinate.  Therefore
    the local ``stale`` spelling produced without a daemon coordinate is only a transport
    hint; it cannot manufacture a stale or current queue item. Invalid catalogs leave
    sources unobserved; individual unavailable sources are omitted from an otherwise
    valid observation so the daemon can explain each accepted citation separately.
    """

    root = _workspace_root(workspace)
    floor = inspect_workspace_floor(workspace, current_coordinate=None)
    observation: dict[str, object] = {
        "tag": "playbill-next-workspace-observation-v1",
        "floor_status": floor.status,
        "installed_coordinate": (
            None
            if floor.installed_coordinate is None
            else floor.installed_coordinate.model_dump(mode="json")
        ),
        "drift_observations": None,
    }
    try:
        candidates = (
            root / ".playbill" / "sources.yaml",
            root / "sources.yaml",
        )
        existing = tuple(path for path in candidates if path.is_file())
        if not existing or any(not path.resolve().is_relative_to(root) for path in existing):
            return observation
        overlay_path = root / ".playbill" / "sources.local.yaml"
        if overlay_path.is_file() and not overlay_path.resolve().is_relative_to(root):
            return observation
        sources = WorkspaceSources(root)
    except (OSError, ValueError):
        return observation
    source_observations: list[dict[str, str]] = []
    for entry in sources.catalog.entries:
        try:
            path = sources.path_for_source(entry.name)
            content = path.read_bytes()
        except (OSError, ValueError):
            continue
        source_observations.append(
            {
                "source_id": entry.name,
                "observed_source_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
    observation["source_observations"] = source_observations
    return observation


def activate_with_workspace_refresh(
    client: _FloorClient,
    instance_id: str,
    proposal_id: str,
    *,
    workspace: str | Path,
) -> contracts.PlaybillWorkspaceActivationResult:
    """Activate once, then independently refresh the current configured floor."""

    activation = client.activate_playbill_proposal(instance_id, proposal_id)
    try:
        relative_path = configured_floor_path(workspace)
        if relative_path is None:
            refresh = contracts.PlaybillFloorRefreshResult(status="not_configured")
        else:
            export = client.export_playbill_floor(instance_id)
            written = materialize_playbill_floor(
                workspace,
                relative_path=relative_path,
                export=export,
            )
            refresh = contracts.PlaybillFloorRefreshResult(
                status="refreshed",
                path=relative_path,
                destination=written.destination,
                floor_digest=written.floor_digest,
            )
    except Exception as exc:  # report activation and refresh truth together
        refresh = contracts.PlaybillFloorRefreshResult(status="failed", message=str(exc))
    return contracts.PlaybillWorkspaceActivationResult(
        **activation.model_dump(mode="json"),
        floor_refresh=refresh,
    )


__all__ = [
    "PlaybillWorkspaceError",
    "activate_with_workspace_refresh",
    "configured_floor_path",
    "inspect_workspace_floor",
    "observe_playbill_next_workspace",
    "materialize_playbill_floor",
    "verified_floor_files",
]
