"""Durable side of edge identity: migration 0004, the guards, and immutability.

The migration is the risky part of this change, so the tests are about the
properties that make it safe rather than about the SQL:

* it is ATOMIC and lock-serialized -- a concurrent initializer sees wholly-old
  or wholly-new state;
* it deletes the derived ``relationship_id`` rather than leaving a second,
  contradictory identity behind;
* it never touches ARTIFACT BYTES: a materialized ``graph.json`` and the
  snapshot digests computed over it survive a backfill unchanged.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest
from tests.test_cli.conftest import CAR_PARTS_YAML

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    mint_claim_id,
)
from cruxible_core.service.snapshots import (
    service_backup_instance,
    service_restore_instance,
)
from cruxible_core.snapshot.types import (
    INSTANCE_BACKUP_FORMAT_VERSION,
    InstanceBackupManifest,
)
from cruxible_core.sqlite_ddl import split_schema_statements
from cruxible_core.storage.sqlite import (
    CLAIM_IDENTITY_MIGRATION,
    SQLiteStorageBackend,
)

_PRE_0004_SCHEMA = """\
CREATE TABLE IF NOT EXISTS storage_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_entities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    node_id TEXT NOT NULL UNIQUE,
    properties_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS graph_relationships (
    relationship_id TEXT PRIMARY KEY,
    edge_key INTEGER NOT NULL UNIQUE,
    from_type TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_type TEXT NOT NULL,
    to_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY (from_type, from_id) REFERENCES graph_entities(entity_type, entity_id),
    FOREIGN KEY (to_type, to_id) REFERENCES graph_entities(entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_relationships_from
    ON graph_relationships(from_type, from_id);
"""


def _write_pre_identity_db(db_path: Path, *, edges: int = 3) -> None:
    """Build a database in the PRE-0004 shape, keyed by the derived string."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_PRE_0004_SCHEMA)
        for entity_type, entity_id in (("Part", "BP-1"), ("Vehicle", "V-1")):
            conn.execute(
                "INSERT INTO graph_entities "
                "(entity_type, entity_id, node_id, properties_json, metadata_json) "
                "VALUES (?, ?, ?, '{}', '{}')",
                (entity_type, entity_id, f"{entity_type}:{entity_id}"),
            )
        for key in range(edges):
            conn.execute(
                "INSERT INTO graph_relationships "
                "(relationship_id, edge_key, from_type, from_id, to_type, to_id, "
                "relationship_type, properties_json, metadata_json) "
                "VALUES (?, ?, 'Part', 'BP-1', 'Vehicle', 'V-1', 'fits', ?, '{}')",
                (f"edge:{key}", key, json.dumps({"n": key})),
            )
        conn.commit()
    finally:
        conn.close()


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_migration_rebuilds_the_table_around_claim_id(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _write_pre_identity_db(db_path)

    SQLiteStorageBackend(db_path).initialize()

    columns = _columns(db_path, "graph_relationships")
    assert "claim_id" in columns
    # The derived identity is DELETED, not kept beside the minted one.
    assert "relationship_id" not in columns

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT claim_id, edge_key, properties_json FROM graph_relationships ORDER BY edge_key"
        ).fetchall()
        migrations = {row[0] for row in conn.execute("SELECT migration_id FROM storage_migrations")}
    finally:
        conn.close()

    assert CLAIM_IDENTITY_MIGRATION in migrations
    assert [row[1] for row in rows] == [0, 1, 2]
    assert [json.loads(row[2])["n"] for row in rows] == [0, 1, 2]
    minted = [row[0] for row in rows]
    assert all(value.startswith("CLM-") for value in minted)
    assert len(set(minted)) == 3  # one id per row, no reuse


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _write_pre_identity_db(db_path)
    backend = SQLiteStorageBackend(db_path)
    backend.initialize()

    conn = sqlite3.connect(db_path)
    try:
        first = [row[0] for row in conn.execute("SELECT claim_id FROM graph_relationships")]
    finally:
        conn.close()

    backend.initialize()

    conn = sqlite3.connect(db_path)
    try:
        second = [row[0] for row in conn.execute("SELECT claim_id FROM graph_relationships")]
    finally:
        conn.close()
    assert first == second


def _initialize_in_child(db_path: str) -> None:
    SQLiteStorageBackend(Path(db_path)).initialize()


def test_concurrent_initializers_do_not_race_the_migration(tmp_path: Path) -> None:
    """Two processes upgrading at once must produce ONE consistent result.

    Without the write lock taken before initialization, the two could interleave
    DDL and a reader could observe a half-upgraded table. With it, the loser
    blocks, then finds the migration row already recorded and does nothing.
    """
    db_path = tmp_path / "state.db"
    _write_pre_identity_db(db_path, edges=5)

    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_initialize_in_child, args=(str(db_path),)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    assert [worker.exitcode for worker in workers] == [0, 0]

    conn = sqlite3.connect(db_path)
    try:
        ids = [row[0] for row in conn.execute("SELECT claim_id FROM graph_relationships")]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()
    assert len(ids) == 5
    assert len(set(ids)) == 5
    # No rebuild scaffolding survived a concurrent upgrade.
    assert "graph_relationships_pre_0004" not in tables


def test_schema_scripts_never_use_executescript() -> None:
    """The migration lock only holds if nothing in init commits underneath it."""
    repo_root = Path(__file__).resolve().parents[2]
    offenders = [
        str(path.relative_to(repo_root))
        for path in (repo_root / "src" / "cruxible_core").rglob("*.py")
        if ".executescript(" in path.read_text()
    ]
    assert offenders == []


def test_split_schema_statements_drops_blanks() -> None:
    assert split_schema_statements("CREATE TABLE a (x TEXT);\n\n CREATE INDEX i ON a(x);\n") == [
        "CREATE TABLE a (x TEXT)",
        "CREATE INDEX i ON a(x)",
    ]


# ------------------------------------------------------- durable write guards


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CAR_PARTS_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _graph_with_edge(claim_id: str) -> tuple[EntityGraph, RelationshipInstance]:
    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1",
            properties={"part_number": "BP-1", "name": "Pads", "category": "brakes"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1", "year": 2024, "make": "Honda", "model": "Civic"},
        )
    )
    edge = RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
        claim_id=claim_id,
    )
    graph.add_relationship(edge)
    stored = graph.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert stored is not None
    return graph, stored


def test_upsert_refuses_to_retarget_an_existing_claim(instance: CruxibleInstance) -> None:
    """A miscopied id must not silently repoint an identity at new endpoints."""
    claim_id = mint_claim_id()
    graph, stored = _graph_with_edge(claim_id)
    instance.save_graph(graph)

    retarget = stored.model_copy(update={"from_id": "BP-OTHER"})
    with instance.write_transaction() as uow:
        with pytest.raises(ValueError, match="Refusing to retarget claim"):
            uow.graph.upsert_relationships([retarget])


def test_upsert_on_the_same_identity_updates_mutable_state_only(
    instance: CruxibleInstance,
) -> None:
    claim_id = mint_claim_id()
    graph, stored = _graph_with_edge(claim_id)
    instance.save_graph(graph)

    with instance.write_transaction() as uow:
        uow.graph.upsert_relationships([stored.model_copy(update={"properties": {"source": "x"}})])

    instance.invalidate_graph_cache()
    reloaded = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert reloaded is not None
    assert reloaded.claim_id == claim_id
    assert reloaded.properties["source"] == "x"
    assert reloaded.identity_tuple() == stored.identity_tuple()


def test_durable_write_without_a_claim_id_raises(instance: CruxibleInstance) -> None:
    graph, stored = _graph_with_edge(mint_claim_id())
    instance.save_graph(graph)
    with instance.write_transaction() as uow:
        with pytest.raises(ValueError, match="require a claim_id"):
            uow.graph.upsert_relationships([stored.model_copy(update={"claim_id": None})])


def test_save_graph_full_replace_rejects_duplicate_ids(instance: CruxibleInstance) -> None:
    """Full replace uses a plain INSERT: a duplicate id is an IntegrityError."""
    claim_id = mint_claim_id()
    graph, stored = _graph_with_edge(claim_id)
    # Slip a duplicate past the in-memory index by writing the raw edge data.
    graph._graph.add_edge(  # noqa: SLF001 - deliberately bypassing the guard
        "Part:BP-1",
        "Vehicle:V-1",
        key=99,
        relationship_type="fits",
        claim_id=claim_id,
        properties={},
        metadata={},
    )
    with pytest.raises(sqlite3.IntegrityError):
        instance.save_graph(graph)


# --------------------------------------------------- artifact byte immutability


def test_snapshot_artifact_bytes_survive_a_legacy_backfill(instance: CruxibleInstance) -> None:
    """Backfill normalizes MEMORY and SQLite; it never rewrites artifact bytes.

    The digest-verification machinery (snapshot.graph_digest, members.json,
    same-release immutability) hashes exact bytes, so a backfill that touched
    graph.json would invalidate every one of them.
    """
    graph, _stored = _graph_with_edge(mint_claim_id())
    instance.save_graph(graph)
    snapshot = instance.create_snapshot(label="immutability")

    artifacts = instance._read_snapshot_artifacts(snapshot.snapshot_id)  # noqa: SLF001
    graph_json_before = artifacts["graph.json"]
    snapshot_json_before = artifacts["snapshot.json"]

    # Materialize the image the way clone/pull do, strip the ids to make it a
    # LEGACY image, and backfill it.
    payload = json.loads(graph_json_before.decode("utf-8"))
    for edge in payload["edges"]:
        edge.pop("claim_id", None)
    legacy = EntityGraph.from_dict(payload)
    minted = legacy.backfill_missing_claim_ids()
    assert minted, "the stripped image must have needed a backfill"

    artifacts_after = instance._read_snapshot_artifacts(snapshot.snapshot_id)  # noqa: SLF001
    assert artifacts_after["graph.json"] == graph_json_before
    assert artifacts_after["snapshot.json"] == snapshot_json_before

    # And the recorded digests still verify against the stored bytes.
    stored_snapshot = instance.get_snapshot(snapshot.snapshot_id)
    assert stored_snapshot is not None
    recomputed = f"sha256:{hashlib.sha256(artifacts_after['graph.json']).hexdigest()}"
    assert recomputed == stored_snapshot.graph_digest


def test_clone_backfills_a_legacy_snapshot_without_rewriting_its_artifacts(
    instance: CruxibleInstance,
    tmp_path: Path,
) -> None:
    graph, _stored = _graph_with_edge(mint_claim_id())
    instance.save_graph(graph)
    snapshot = instance.create_snapshot(label="clone-source")
    source_bytes = instance._read_snapshot_artifacts(snapshot.snapshot_id)["graph.json"]  # noqa: SLF001

    clone, _ = CruxibleInstance.clone_from_snapshot(
        instance,
        snapshot.snapshot_id,
        tmp_path / "clone",
    )
    cloned_edge = clone.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert cloned_edge is not None
    assert cloned_edge.claim_id is not None

    # The clone re-saved the SAME artifact bytes; nothing was rewritten.
    cloned_bytes = clone._read_snapshot_artifacts(snapshot.snapshot_id)["graph.json"]  # noqa: SLF001
    assert cloned_bytes == source_bytes


# ------------------------------------------------------------ backup downgrade


def test_restore_refuses_a_backup_that_needs_a_newer_reader(
    instance: CruxibleInstance,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "backup.zip"
    service_backup_instance(instance, instance_id="inst-1", artifact_path=artifact)

    # Rewrite the manifest to declare a future minimum reader, exactly as a
    # later storage migration would.
    import zipfile

    with zipfile.ZipFile(artifact) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = InstanceBackupManifest.model_validate_json(contents["manifest.json"])
    bumped = manifest.model_copy(
        update={"min_reader_format_version": INSTANCE_BACKUP_FORMAT_VERSION + 1}
    )
    contents["manifest.json"] = json.dumps(bumped.model_dump(mode="json")).encode("utf-8")
    with zipfile.ZipFile(artifact, "w") as archive:
        for name, payload in contents.items():
            archive.writestr(name, payload)

    with pytest.raises(ConfigError, match="requires a newer Cruxible"):
        service_restore_instance(artifact_path=artifact, root_dir=tmp_path / "restored")
    # Refused BEFORE installation: nothing was written to the target.
    assert not (tmp_path / "restored").exists()


def test_backup_manifest_declares_the_bumped_format(instance: CruxibleInstance, tmp_path: Path):
    result = service_backup_instance(
        instance,
        instance_id="inst-1",
        artifact_path=tmp_path / "backup.zip",
    )
    assert result.manifest.format_version == INSTANCE_BACKUP_FORMAT_VERSION
    assert result.manifest.min_reader_format_version == INSTANCE_BACKUP_FORMAT_VERSION
    assert INSTANCE_BACKUP_FORMAT_VERSION >= 2
