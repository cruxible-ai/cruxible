"""Tests for same-identity instance backup and restore."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import service_add_entities
from cruxible_core.service.snapshots import (
    read_instance_backup_manifest,
    service_relocate_instance,
    service_restore_instance,
    service_snapshot_instance,
)

CONFIG_YAML = """\
version: "1.0"
name: backup_smoke

entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true
      title:
        type: string

relationships: []
"""


def _instance(root: Path) -> CruxibleInstance:
    root.mkdir()
    (root / "config.yaml").write_text(CONFIG_YAML)
    instance = CruxibleInstance.init(root, "config.yaml")
    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Thing",
            entity_id="T-root",
            properties={"thing_id": "T-root", "title": "Root"},
        )
    )
    instance.save_graph(graph)
    return instance


def test_instance_backup_restore_preserves_graph_receipts_and_config(tmp_path: Path) -> None:
    source = _instance(tmp_path / "source")
    write = service_add_entities(
        source,
        [
            EntityInstance(
                entity_type="Thing",
                entity_id="T-written",
                properties={"thing_id": "T-written", "title": "Written"},
            )
        ],
    )
    artifact = tmp_path / "backup.cruxible.zip"

    result = service_snapshot_instance(
        source,
        instance_id="inst_backup",
        artifact_path=artifact,
        label="pre-release",
    )
    restored = service_restore_instance(
        artifact_path=artifact,
        root_dir=tmp_path / "restored",
        instance_mode=CruxibleInstance.GOVERNED_MODE,
    )

    assert result.instance_id == "inst_backup"
    assert result.manifest.label == "pre-release"
    assert {"state.db", "config.yaml", "instance.json"} <= set(result.manifest.artifacts)
    assert restored.instance_id == "inst_backup"
    assert restored.instance.is_governed_mode()
    assert restored.instance.get_config_path() == restored.instance.root / "config.yaml"
    assert restored.instance.load_graph().get_entity("Thing", "T-written") is not None
    assert restored.instance.get_receipt_store().get_receipt(write.receipt_id or "") is not None
    assert (restored.instance.root / "config.yaml").read_text() == CONFIG_YAML


def test_instance_backup_uses_sqlite_backup_for_live_state(tmp_path: Path) -> None:
    source = _instance(tmp_path / "source")
    service_add_entities(
        source,
        [
            EntityInstance(
                entity_type="Thing",
                entity_id="T-wal",
                properties={"thing_id": "T-wal", "title": "Wal"},
            )
        ],
    )
    artifact = tmp_path / "backup.cruxible.zip"

    service_snapshot_instance(source, instance_id="inst_backup", artifact_path=artifact)

    with zipfile.ZipFile(artifact) as archive:
        db_bytes = archive.read("state.db")
    db_path = tmp_path / "state-copy.db"
    db_path.write_bytes(db_bytes)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT entity_id FROM graph_entities WHERE entity_id = 'T-wal'"
        ).fetchone()
    assert row is not None


def test_instance_restore_rejects_digest_mismatch(tmp_path: Path) -> None:
    source = _instance(tmp_path / "source")
    artifact = tmp_path / "backup.cruxible.zip"
    service_snapshot_instance(source, instance_id="inst_backup", artifact_path=artifact)
    broken = tmp_path / "broken.cruxible.zip"

    with zipfile.ZipFile(artifact) as archive, zipfile.ZipFile(broken, "w") as out:
        for name in archive.namelist():
            content = archive.read(name)
            if name == "config.yaml":
                content += b"\n# tampered\n"
            out.writestr(name, content)

    with pytest.raises(ConfigError, match="digest mismatch"):
        service_restore_instance(artifact_path=broken, root_dir=tmp_path / "restored")


def test_instance_restore_requires_required_manifest_artifact_digests(
    tmp_path: Path,
) -> None:
    source = _instance(tmp_path / "source")
    artifact = tmp_path / "backup.cruxible.zip"
    service_snapshot_instance(source, instance_id="inst_backup", artifact_path=artifact)
    broken = tmp_path / "missing-manifest-artifact.cruxible.zip"

    with zipfile.ZipFile(artifact) as archive, zipfile.ZipFile(broken, "w") as out:
        manifest = json.loads(archive.read("manifest.json"))
        manifest["artifacts"].pop("config.yaml")
        for name in archive.namelist():
            content = (
                json.dumps(manifest).encode("utf-8")
                if name == "manifest.json"
                else archive.read(name)
            )
            out.writestr(name, content)

    with pytest.raises(ConfigError, match="missing required artifact digest"):
        service_restore_instance(artifact_path=broken, root_dir=tmp_path / "restored")


def test_instance_restore_rejects_unsafe_zip_path(tmp_path: Path) -> None:
    artifact = tmp_path / "unsafe.cruxible.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("../state.db", b"bad")
        archive.writestr("manifest.json", json.dumps({}))

    with pytest.raises(ConfigError, match="Unsafe path"):
        read_instance_backup_manifest(artifact)


def test_instance_restore_rejects_existing_instance(tmp_path: Path) -> None:
    source = _instance(tmp_path / "source")
    target = _instance(tmp_path / "target")
    artifact = tmp_path / "backup.cruxible.zip"
    service_snapshot_instance(source, instance_id="inst_backup", artifact_path=artifact)

    with pytest.raises(ConfigError, match="Instance already exists"):
        service_restore_instance(artifact_path=artifact, root_dir=target.root)


def test_relocate_moves_instance_preserving_identity_and_graph(tmp_path: Path) -> None:
    source = _instance(tmp_path / "source")
    service_add_entities(
        source,
        [
            EntityInstance(
                entity_type="Thing",
                entity_id="T-moved",
                properties={"thing_id": "T-moved", "title": "Moved"},
            )
        ],
    )
    target = tmp_path / "moved"

    result = service_relocate_instance(
        source,
        instance_id="inst_relocated",
        to_dir=target,
    )

    assert result.instance_id == "inst_relocated"
    assert Path(result.from_dir) == source.root
    assert Path(result.to_dir) == target
    assert result.source_removed is False
    # Source is intentionally kept by default (orphaned, disk-only copy).
    assert (source.root / "config.yaml").exists()
    # Restored instance carries the same graph + identity.
    relocated = CruxibleInstance.load(target)
    assert relocated.is_governed_mode()
    assert relocated.load_graph().get_entity("Thing", "T-moved") is not None
    assert (target / "config.yaml").read_text() == CONFIG_YAML


def test_relocate_remove_source_deletes_old_dir(tmp_path: Path) -> None:
    source = _instance(tmp_path / "source")
    target = tmp_path / "moved"

    result = service_relocate_instance(
        source,
        instance_id="inst_relocated",
        to_dir=target,
        remove_source=True,
    )

    assert result.source_removed is True
    assert not source.root.exists()
    assert (target / "config.yaml").exists()


def test_relocate_rejects_same_location(tmp_path: Path) -> None:
    source = _instance(tmp_path / "source")

    with pytest.raises(ConfigError, match="current location"):
        service_relocate_instance(
            source,
            instance_id="inst_relocated",
            to_dir=source.root,
        )


def test_aborted_relocate_leaves_original_usable(tmp_path: Path) -> None:
    source = _instance(tmp_path / "source")
    service_add_entities(
        source,
        [
            EntityInstance(
                entity_type="Thing",
                entity_id="T-keep",
                properties={"thing_id": "T-keep", "title": "Keep"},
            )
        ],
    )
    # Pre-existing instance at the target makes restore refuse mid-relocate.
    occupied = _instance(tmp_path / "occupied")

    with pytest.raises(ConfigError):
        service_relocate_instance(
            source,
            instance_id="inst_relocated",
            to_dir=occupied.root,
            remove_source=True,
        )

    # Original instance is untouched: still on disk, still loadable, still queryable,
    # and never deleted despite remove_source=True.
    assert (source.root / "config.yaml").exists()
    reloaded = CruxibleInstance.load(source.root)
    assert reloaded.load_graph().get_entity("Thing", "T-keep") is not None
