"""Graph-snapshot, clone, and same-identity instance backup service functions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypedDict
from zipfile import ZipInfo

from pydantic import ValidationError

from cruxible_core import __version__
from cruxible_core.config.loader import load_config_from_string
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service.lifecycle import refuse_auth_managed_without_server_auth
from cruxible_core.service.types import (
    CloneSnapshotResult,
    InstanceBackupResult,
    InstanceRelocateResult,
    InstanceRestoreResult,
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

    # Clone activates the snapshot's config in a new instance: refuse an
    # auth-managed config on an auth-off daemon BEFORE clone_from_snapshot writes
    # anything into the target root. Missing snapshots/artifacts fall through to
    # clone_from_snapshot's own errors.
    snapshot_config_bytes = instance._read_snapshot_artifacts(snapshot_id).get("config.yaml")
    if snapshot_config_bytes is not None:
        refuse_auth_managed_without_server_auth(
            load_config_from_string(snapshot_config_bytes.decode("utf-8")),
            instance_config_path=Path(root_dir) / "config.yaml",
        )

    cloned, snapshot = CruxibleInstance.clone_from_snapshot(
        instance,
        snapshot_id,
        root_dir,
        instance_mode=instance_mode,
    )
    return CloneSnapshotResult(instance=cloned, snapshot=snapshot)


def service_backup_instance(
    instance: InstanceProtocol,
    *,
    instance_id: str,
    artifact_path: str | Path,
    label: str | None = None,
) -> InstanceBackupResult:
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

    return InstanceBackupResult(
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

    # Restore activates the backed-up config on this daemon: refuse an
    # auth-managed config on an auth-off daemon BEFORE any file is staged or the
    # instance root is created.
    refuse_auth_managed_without_server_auth(
        load_config_from_string(contents[_INSTANCE_BACKUP_CONFIG].decode("utf-8")),
        instance_config_path=root / _INSTANCE_BACKUP_CONFIG,
    )

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
        (instance_dir / "instance.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
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


def service_relocate_instance(
    instance: InstanceProtocol,
    *,
    instance_id: str,
    to_dir: str | Path,
    instance_mode: str = CruxibleInstance.GOVERNED_MODE,
    registry_status: Literal["registered", "repaired", "unchanged"] = "registered",
) -> InstanceRelocateResult:
    """Move a healthy same-identity instance to *to_dir* by backup-then-restore.

    Steps, ordered so an abort never leaves the instance unreachable:
      1. Back up the (still-healthy) instance to a throwaway artifact. If this
         fails, the source is untouched.
      2. Restore the artifact into ``to_dir`` (atomic ``os.replace`` of a staging
         dir). If this fails, the source is still untouched and usable.

    This function never removes the source directory: removal is the caller's
    responsibility and must happen only after the registry has been repointed and
    the in-process manager slot has been swapped to the relocated instance. That
    ordering keeps the source as a usable fallback if any of those later steps
    fail, so ``source_removed`` is always ``False`` in the returned result.

    The source instance stays live and queryable throughout backup + restore;
    the caller atomically overwrites the manager slot with the relocated instance
    at the end (the source is never dropped from the manager mid-relocate).
    """
    if not isinstance(instance, CruxibleInstance):
        raise ConfigError("Instance relocate currently supports only local filesystem instances")

    source_root = instance.get_root_path()
    target_root = Path(to_dir).expanduser()
    resolved_source = source_root.resolve()
    resolved_target = target_root.resolve()
    if resolved_target == resolved_source:
        raise ConfigError(f"Relocate target is the current location: {target_root}")
    # Reject either direction of containment: a target nested inside the source
    # (or vice versa) means a later source removal would also delete the restored
    # instance, and overlapping trees make atomic restore impossible.
    overlap = paths_overlap(resolved_target, resolved_source)
    if overlap == "nested_inside":
        raise ConfigError(
            f"Relocate target {target_root} is nested inside the source {source_root}"
        )
    if overlap == "contains":
        raise ConfigError(f"Relocate target {target_root} contains the source {source_root}")

    # 1. Back up the healthy instance to a throwaway artifact. Keep it under the
    #    target's parent so the temp + restore staging share a filesystem.
    target_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="cruxible_instance_relocate_",
        dir=str(target_root.parent),
    ) as tmp:
        artifact = Path(tmp) / "relocate.cruxible.zip"
        backup = service_backup_instance(
            instance,
            instance_id=instance_id,
            artifact_path=artifact,
            label=f"relocate:{source_root}",
        )
        # 2. Restore into the new location. Refuses if non-empty / already an
        #    instance; the source remains untouched on any failure here.
        restored = service_restore_instance(
            artifact_path=artifact,
            root_dir=target_root,
            instance_mode=instance_mode,
            registry_status=registry_status,
        )

    return InstanceRelocateResult(
        instance=restored.instance,
        instance_id=restored.instance_id,
        from_dir=str(source_root),
        to_dir=restored.root_dir,
        manifest=backup.manifest,
        source_removed=False,
        registry_status=restored.registry_status,
    )


def paths_overlap(target: Path, other: Path) -> Literal["", "same", "nested_inside", "contains"]:
    """Classify how two already-resolved paths overlap on the filesystem tree.

    Returns ``"same"`` when the paths are identical, ``"nested_inside"`` when
    *target* is a descendant of *other*, ``"contains"`` when *target* is an
    ancestor of *other*, and ``""`` when the two trees are disjoint.
    """
    if target == other:
        return "same"
    if other in target.parents:
        return "nested_inside"
    if target in other.parents:
        return "contains"
    return ""


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


class _InstanceBackupBundle(TypedDict):
    manifest: InstanceBackupManifest
    contents: dict[str, bytes]


def _read_verified_instance_backup(
    artifact: Path,
) -> _InstanceBackupBundle:
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
            missing_required_artifacts = sorted(
                (_INSTANCE_BACKUP_REQUIRED - {_INSTANCE_BACKUP_MANIFEST}) - set(manifest.artifacts)
            )
            if missing_required_artifacts:
                raise ConfigError(
                    "Instance backup manifest is missing required artifact digest(s): "
                    + ", ".join(missing_required_artifacts)
                )
            contents = {name: archive.read(name) for name in manifest.artifacts if name in names}
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
