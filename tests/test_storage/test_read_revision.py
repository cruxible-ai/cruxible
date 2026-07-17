"""Monotonic read_revision: bumped once per mutation commit, never on reads.

The revision advances inside the SAME SQLite transaction as every
state-mutating commit (graph, snapshots, groups, feedback, decisions, source
artifacts) and NEVER for audit-only writes (query receipts, traces, decision
events) — so read paths that persist proof records keep the revision
unchanged. Receipts prove computation, never freshness.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.test_cli.conftest import CAR_PARTS_YAML

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.service import (
    service_add_entities,
    service_add_relationships,
    service_apply_workflow,
    service_backup_instance,
    service_batch_direct_write,
    service_create_snapshot,
    service_feedback_input,
    service_inspect_entity,
    service_list,
    service_lock,
    service_propose_group_inputs,
    service_query_surface,
    service_resolve_group,
    service_restore_instance,
    service_run,
    service_sample,
    service_stats,
)
from cruxible_core.service.types import (
    BatchDirectWriteInput,
    EntityWriteInput,
    FeedbackItemInput,
    GroupMemberInput,
    RelationshipTargetInput,
)
from cruxible_core.storage.sqlite import (
    READ_REVISION_MIGRATION,
    READ_REVISION_STATE_KEY,
)


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CAR_PARTS_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _seed_entities(instance: CruxibleInstance) -> None:
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-1",
                properties={"vehicle_id": "V-1", "year": 2024, "make": "Honda", "model": "Civic"},
            ),
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1",
                properties={"part_number": "BP-1", "name": "Pads", "category": "brakes"},
            ),
        ],
    )


def _fits_edge() -> RelationshipInstance:
    return RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
        properties={"verified": True},
    )


def _fits_target() -> RelationshipTargetInput:
    return RelationshipTargetInput(
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
    )


# ---------------------------------------------------------------------------
# (a) exactly once per mutation commit, across every write path
# ---------------------------------------------------------------------------


class TestRevisionIncrementsOncePerMutationCommit:
    def test_direct_entity_write(self, instance: CruxibleInstance) -> None:
        before = instance.get_read_revision()
        _seed_entities(instance)
        assert instance.get_read_revision() == before + 1

    def test_relationship_add(self, instance: CruxibleInstance) -> None:
        _seed_entities(instance)
        before = instance.get_read_revision()
        service_add_relationships(instance, [_fits_edge()], "direct", "add_relationship")
        assert instance.get_read_revision() == before + 1

    def test_batch_direct_write(self, instance: CruxibleInstance) -> None:
        before = instance.get_read_revision()
        service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Vehicle",
                        entity_id="V-2",
                        properties={
                            "vehicle_id": "V-2",
                            "year": 2025,
                            "make": "Honda",
                            "model": "Accord",
                        },
                    )
                ],
            ),
        )
        assert instance.get_read_revision() == before + 1

    def test_workflow_apply(self, canonical_workflow_instance: CruxibleInstance) -> None:
        service_lock(canonical_workflow_instance)
        preview = service_run(canonical_workflow_instance, "build_reference", {})
        before = canonical_workflow_instance.get_read_revision()
        service_apply_workflow(
            canonical_workflow_instance,
            "build_reference",
            {},
            expected_apply_digest=preview.apply_digest or "",
            expected_head_snapshot_id=preview.head_snapshot_id,
        )
        assert canonical_workflow_instance.get_read_revision() == before + 1

    def test_group_propose_and_resolve(self, instance: CruxibleInstance) -> None:
        _seed_entities(instance)
        before = instance.get_read_revision()
        proposal = service_propose_group_inputs(
            instance,
            "fits",
            [
                GroupMemberInput(
                    from_type="Part",
                    from_id="BP-1",
                    to_type="Vehicle",
                    to_id="V-1",
                    relationship_type="fits",
                    properties={"verified": True},
                )
            ],
            thesis_facts={"k": "v"},
        )
        assert instance.get_read_revision() == before + 1
        service_resolve_group(instance, proposal.group_id, "approve", expected_pending_version=1)
        assert instance.get_read_revision() == before + 2

    def test_feedback(self, instance: CruxibleInstance) -> None:
        _seed_entities(instance)
        service_add_relationships(instance, [_fits_edge()], "direct", "add_relationship")
        before = instance.get_read_revision()
        service_feedback_input(
            instance,
            FeedbackItemInput(action="approve", target=_fits_target()),
            source="human",
        )
        assert instance.get_read_revision() == before + 1

    def test_snapshot_create(self, instance: CruxibleInstance) -> None:
        _seed_entities(instance)
        before = instance.get_read_revision()
        service_create_snapshot(instance)
        assert instance.get_read_revision() == before + 1

    def test_snapshot_restore_bumps_and_never_resets(
        self, instance: CruxibleInstance, tmp_path: Path
    ) -> None:
        _seed_entities(instance)
        snapshot = service_create_snapshot(instance).snapshot
        # More mutations after the snapshot: restoring the older snapshot into
        # a clone must still move ITS revision forward, never backwards.
        service_add_relationships(instance, [_fits_edge()], "direct", "add_relationship")

        clone, _ = CruxibleInstance.clone_from_snapshot(
            instance, snapshot.snapshot_id, tmp_path / "clone"
        )
        # Fresh state DB: init commit + snapshot-materialization commit.
        assert clone.get_read_revision() >= 1
        before = clone.get_read_revision()
        service_add_entities(
            clone,
            [
                EntityInstance(
                    entity_type="Vehicle",
                    entity_id="V-9",
                    properties={
                        "vehicle_id": "V-9",
                        "year": 2020,
                        "make": "Honda",
                        "model": "Fit",
                    },
                )
            ],
        )
        assert clone.get_read_revision() == before + 1

    def test_backup_restore_preserves_monotonic_revision(
        self, instance: CruxibleInstance, tmp_path: Path
    ) -> None:
        _seed_entities(instance)
        original = instance.get_read_revision()
        service_backup_instance(
            instance, instance_id="inst-rev", artifact_path=tmp_path / "backup.zip"
        )
        restored = service_restore_instance(
            artifact_path=tmp_path / "backup.zip",
            root_dir=tmp_path / "restored",
            instance_mode="dev",
        )
        # The restored state DB carries its history forward — never a reset.
        assert restored.instance.get_read_revision() >= original


# ---------------------------------------------------------------------------
# (a) never on reads — even reads that persist audit records
# ---------------------------------------------------------------------------


def test_reads_never_advance_revision(instance: CruxibleInstance) -> None:
    _seed_entities(instance)
    service_add_relationships(instance, [_fits_edge()], "direct", "add_relationship")
    before = instance.get_read_revision()

    # Plain reads.
    service_list(instance, "entities", entity_type="Vehicle")
    service_list(instance, "edges")
    service_stats(instance)
    service_sample(instance, "Vehicle")
    service_inspect_entity(instance, "Vehicle", "V-1", depth=2)
    # A query persists its receipt (an audit record) — still not a mutation.
    result = service_query_surface(instance, "parts_for_vehicle", {"vehicle_id": "V-1"})
    assert result.receipt_id is not None

    assert instance.get_read_revision() == before


# ---------------------------------------------------------------------------
# (b) persistence across restart
# ---------------------------------------------------------------------------


def test_revision_survives_reload(instance: CruxibleInstance) -> None:
    _seed_entities(instance)
    revision = instance.get_read_revision()
    assert revision >= 1

    reloaded = CruxibleInstance.load(instance.get_root_path())
    assert reloaded.get_read_revision() == revision

    service_add_relationships(reloaded, [_fits_edge()], "direct", "add_relationship")
    assert reloaded.get_read_revision() == revision + 1


# ---------------------------------------------------------------------------
# (c) migration: opening a pre-revision state DB
# ---------------------------------------------------------------------------


def test_pre_revision_state_db_initializes_from_snapshot_count(
    instance: CruxibleInstance,
) -> None:
    _seed_entities(instance)
    service_create_snapshot(instance)
    service_create_snapshot(instance)
    db_path = instance.get_instance_dir() / "state.db"

    # Simulate a pre-revision state DB: drop the counter and its migration row.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM instance_state WHERE key = ?",
            (READ_REVISION_STATE_KEY,),
        )
        conn.execute(
            "DELETE FROM storage_migrations WHERE migration_id = ?",
            (READ_REVISION_MIGRATION,),
        )
        conn.commit()
        snapshot_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        conn.close()
    assert snapshot_count == 2

    reopened = CruxibleInstance.load(instance.get_root_path())
    # Backfill seeds the counter from the snapshot count.
    assert reopened.get_read_revision() == snapshot_count

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value_json FROM instance_state WHERE key = ?",
            (READ_REVISION_STATE_KEY,),
        ).fetchone()
        migrated = conn.execute(
            "SELECT migration_id FROM storage_migrations WHERE migration_id = ?",
            (READ_REVISION_MIGRATION,),
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row[0]) == snapshot_count
    assert migrated is not None

    # And it keeps counting from there.
    service_add_relationships(reopened, [_fits_edge()], "direct", "add_relationship")
    assert reopened.get_read_revision() == snapshot_count + 1


def test_migration_keeps_existing_revision_value(instance: CruxibleInstance) -> None:
    """INSERT OR IGNORE: a present counter is never overwritten by the backfill."""
    _seed_entities(instance)
    revision = instance.get_read_revision()
    db_path = instance.get_instance_dir() / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM storage_migrations WHERE migration_id = ?",
            (READ_REVISION_MIGRATION,),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = CruxibleInstance.load(instance.get_root_path())
    assert reopened.get_read_revision() == revision
