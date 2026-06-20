"""Snapshot, clone, and same-identity instance backup service functions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from zipfile import ZipInfo

from pydantic import ValidationError

from cruxible_core import __version__
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service.types import (
    CloneSnapshotResult,
    InstanceRestoreResult,
    InstanceSnapshotResult,
    SnapshotCreateResult,
    SnapshotListResult,
)
from cruxible_core.snapshot.types import InstanceBackupManifest
from cruxible_core.storage.sqlite import backup_sqlite_database
from cruxible_core.temporal import utc_now
from cruxible_core.workflow.compiler import LOCK_FILE_NAME, resolve_lock_path

_INSTANCE_BACKUP_MANIFEST = "manifest.json"
_INSTANCE_BACKUP_STATE_DB = "state.db"
_INSTANCE_BACKUP_CONFIG = "config.yaml"
_INSTANCE_BACKUP_METADATA = "instance.json"
_INSTANCE_BACKUP_REQUIRED = {
    _INSTANCE_BACKUP_MANIFEST,
    _INSTANCE_BACKUP_STATE_DB,
    _INSTANCE_BACKUP_CONFIG,
    _INSTANCE_BACKUP_METADATA,
}


def service_create_snapshot(
    instance: InstanceProtocol,
    label: str | None = None,
    *,
    actor_context: GovernedActorContext | None = None,
) -> SnapshotCreateResult:
    """Create an immutable full snapshot for the current instance."""
    snapshot = instance.create_snapshot(label=label, actor_context=actor_context)
    return SnapshotCreateResult(snapshot=snapshot)


def service_list_snapshots(
    instance: InstanceProtocol,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> SnapshotListResult:
    """List snapshots for the current instance, newest first.

    Snapshots are ordered by created_at then snapshot_id (descending);
    ``total`` reflects the full count before limit/offset are applied.
    """
    snapshots = sorted(
        instance.list_snapshots(),
        key=lambda snapshot: (snapshot.created_at, snapshot.snapshot_id),
        reverse=True,
    )
    total = len(snapshots)
    end = None if limit is None else offset + limit
    return SnapshotListResult(items=snapshots[offset:end], total=total)


def service_clone_snapshot(
    instance: InstanceProtocol,
    snapshot_id: str,
    root_dir: str | Path,
    *,
    instance_mode: str = CruxibleInstance.DEV_MODE,
) -> CloneSnapshotResult:
    """Create a new local instance from a selected snapshot."""
    if not isinstance(instance, CruxibleInstance):
        raise ConfigError("Snapshot clone currently supports only local filesystem instances")

    cloned, snapshot = CruxibleInstance.clone_from_snapshot(
        instance,
        snapshot_id,
        root_dir,
        instance_mode=instance_mode,
    )
    return CloneSnapshotResult(instance=cloned, snapshot=snapshot)


def service_snapshot_instance(
    instance: InstanceProtocol,
    *,
    instance_id: str,
    artifact_path: str | Path,
    label: str | None = None,
) -> InstanceSnapshotResult:
    """Write a portable same-identity backup artifact for an instance."""
    if not isinstance(instance, CruxibleInstance):
        raise ConfigError("Instance backup currently supports only local filesystem instances")

    artifact = Path(artifact_path).expanduser()
    if artifact.exists() and artifact.is_dir():
        raise ConfigError(f"Backup artifact path is a directory: {artifact}")
    artifact.parent.mkdir(parents=True, exist_ok=True)

    config_path = instance.get_config_path()
    if not config_path.exists():
        raise ConfigError(f"Cannot back up instance because config is missing: {config_path}")
    metadata_path = instance.get_instance_dir() / "instance.json"
    if not metadata_path.exists():
        raise ConfigError(f"Cannot back up instance because metadata is missing: {metadata_path}")

    instance.load_graph()  # ensure state.db exists before using SQLite backup.
    with tempfile.TemporaryDirectory(prefix="cruxible_instance_backup_") as tmp:
        tmp_dir = Path(tmp)
        state_db_copy = tmp_dir / _INSTANCE_BACKUP_STATE_DB
        _backup_sqlite_db(instance.get_instance_dir() / "state.db", state_db_copy)

        artifacts: dict[str, bytes] = {
            _INSTANCE_BACKUP_STATE_DB: state_db_copy.read_bytes(),
            _INSTANCE_BACKUP_CONFIG: config_path.read_bytes(),
            _INSTANCE_BACKUP_METADATA: metadata_path.read_bytes(),
        }
        lock_path = resolve_lock_path(instance)
        if lock_path.exists():
            artifacts[LOCK_FILE_NAME] = lock_path.read_bytes()

        digests = {name: _sha256_bytes(content) for name, content in artifacts.items()}
        manifest = InstanceBackupManifest(
            instance_id=instance_id,
            created_at=utc_now(),
            cruxible_version=__version__,
            label=label,
            original_config_path=str(config_path),
            restored_config_path=_INSTANCE_BACKUP_CONFIG,
            instance_mode=instance.get_instance_mode(),
            artifacts=digests,
        )
        manifest_bytes = json.dumps(
            manifest.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

        temp_artifact = artifact.with_name(f".{artifact.name}.tmp")
        with zipfile.ZipFile(temp_artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            _write_zip_member(archive, _INSTANCE_BACKUP_MANIFEST, manifest_bytes)
            for name, content in artifacts.items():
                _write_zip_member(archive, name, content)
        os.replace(temp_artifact, artifact)

    return InstanceSnapshotResult(
        instance_id=instance_id,
        artifact_path=str(artifact),
        manifest=manifest,
    )


def service_restore_instance(
    *,
    artifact_path: str | Path,
    root_dir: str | Path,
    instance_mode: str = CruxibleInstance.GOVERNED_MODE,
    registry_status: Literal["registered", "repaired", "unchanged"] = "registered",
) -> InstanceRestoreResult:
    """Restore a same-identity instance backup artifact into *root_dir*."""
    CruxibleInstance._validate_instance_mode(instance_mode)
    artifact = Path(artifact_path).expanduser()
    root = Path(root_dir).expanduser()
    bundle = _read_verified_instance_backup(artifact)
    manifest = bundle["manifest"]
    contents = bundle["contents"]

    if (root / CruxibleInstance.INSTANCE_DIR / "instance.json").exists():
        raise ConfigError(f"Instance already exists at {root}")
    if (root / _INSTANCE_BACKUP_CONFIG).exists():
        raise ConfigError(f"Refusing to overwrite existing config.yaml at {root}")

    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="cruxible_instance_restore_",
        dir=str(root.parent),
    ) as tmp:
        staging = Path(tmp) / root.name
        instance_dir = staging / CruxibleInstance.INSTANCE_DIR
        instance_dir.mkdir(parents=True)
        staging.mkdir(parents=True, exist_ok=True)

        (staging / _INSTANCE_BACKUP_CONFIG).write_bytes(contents[_INSTANCE_BACKUP_CONFIG])
        metadata = _restored_instance_metadata(
            contents[_INSTANCE_BACKUP_METADATA],
            instance_mode=instance_mode,
        )
        (instance_dir / "instance.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True)
        )
        (instance_dir / "state.db").write_bytes(contents[_INSTANCE_BACKUP_STATE_DB])
        lock_bytes = contents.get(LOCK_FILE_NAME)
        if lock_bytes is not None:
            (instance_dir / LOCK_FILE_NAME).write_bytes(lock_bytes)

        if root.exists():
            if any(root.iterdir()):
                raise ConfigError(f"Restore target is not empty: {root}")
            root.rmdir()
        os.replace(staging, root)

    restored = CruxibleInstance.load(root)
    restored.load_graph()
    return InstanceRestoreResult(
        instance=restored,
        instance_id=manifest.instance_id,
        root_dir=str(root),
        manifest=manifest,
        registry_status=registry_status,
    )


def read_instance_backup_manifest(artifact_path: str | Path) -> InstanceBackupManifest:
    """Read and validate only the backup manifest from an artifact."""
    artifact = Path(artifact_path).expanduser()
    try:
        with zipfile.ZipFile(artifact) as archive:
            _validate_zip_names(archive)
            manifest_bytes = archive.read(_INSTANCE_BACKUP_MANIFEST)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ConfigError(f"Invalid instance backup artifact: {artifact}") from exc
    return _parse_manifest(manifest_bytes)


def _backup_sqlite_db(source: Path, target: Path) -> None:
    if not source.exists():
        raise ConfigError(f"State database not found: {source}")
    backup_sqlite_database(source, target)


def _read_verified_instance_backup(
    artifact: Path,
) -> dict[str, InstanceBackupManifest | dict[str, bytes]]:
    try:
        with zipfile.ZipFile(artifact) as archive:
            _validate_zip_names(archive)
            names = set(archive.namelist())
            missing = sorted(_INSTANCE_BACKUP_REQUIRED - names)
            if missing:
                raise ConfigError(
                    f"Instance backup artifact is missing required file(s): {', '.join(missing)}"
                )
            manifest = _parse_manifest(archive.read(_INSTANCE_BACKUP_MANIFEST))
            contents = {
                name: archive.read(name)
                for name in manifest.artifacts
                if name in names
            }
    except ConfigError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ConfigError(f"Invalid instance backup artifact: {artifact}") from exc

    missing_artifacts = sorted(set(manifest.artifacts) - set(contents))
    if missing_artifacts:
        raise ConfigError(
            "Instance backup artifact is missing manifest-listed file(s): "
            + ", ".join(missing_artifacts)
        )
    for name, expected in manifest.artifacts.items():
        actual = _sha256_bytes(contents[name])
        if actual != expected:
            raise ConfigError(
                f"Instance backup artifact digest mismatch for {name}: "
                f"expected {expected}, got {actual}"
            )
    return {"manifest": manifest, "contents": contents}


def _parse_manifest(content: bytes) -> InstanceBackupManifest:
    try:
        return InstanceBackupManifest.model_validate_json(content)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        raise ConfigError("Invalid instance backup manifest", errors=errors) from exc


def _validate_zip_names(archive: zipfile.ZipFile) -> None:
    for name in archive.namelist():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or name.endswith("/"):
            raise ConfigError(f"Unsafe path in instance backup artifact: {name}")


def _write_zip_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, content)


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _restored_instance_metadata(content: bytes, *, instance_mode: str) -> Mapping[str, object]:
    try:
        metadata = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError("Invalid backed-up instance metadata") from exc
    if not isinstance(metadata, dict):
        raise ConfigError("Invalid backed-up instance metadata")
    metadata["config_path"] = _INSTANCE_BACKUP_CONFIG
    metadata["instance_mode"] = instance_mode
    metadata["version"] = __version__
    return metadata
