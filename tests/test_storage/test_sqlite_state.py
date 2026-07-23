"""Contract tests for the unified SQLite state backend."""

from __future__ import annotations

import ast
import inspect
import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.test_cli.conftest import CAR_PARTS_YAML

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.decision.store import DecisionStore
from cruxible_core.decision.types import DecisionRecord
from cruxible_core.feedback.store import FeedbackStore
from cruxible_core.feedback.types import OutcomeRecord
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import EvidenceRef, RelationshipEvidence
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, RelationshipMetadata
from cruxible_core.group.store import GroupStore
from cruxible_core.instance_protocol import (
    DecisionStoreProtocol,
    FeedbackStoreProtocol,
    GroupStoreProtocol,
)
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.store import SQLiteReceiptStore
from cruxible_core.storage.sqlite import (
    SNAPSHOT_SCHEMA_MIGRATION,
    SQLiteGraphRepository,
)


@pytest.fixture
def initialized_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CAR_PARTS_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _vehicle(entity_id: str = "V-1") -> EntityInstance:
    return EntityInstance(
        entity_type="Vehicle",
        entity_id=entity_id,
        properties={"vehicle_id": entity_id, "year": 2024, "make": "Honda", "model": "Civic"},
    )


def _part(entity_id: str = "BP-1") -> EntityInstance:
    return EntityInstance(
        entity_type="Part",
        entity_id=entity_id,
        properties={"part_number": entity_id, "name": "Pads", "category": "brakes"},
    )


def test_state_db_has_versioned_migration_marker(initialized_instance: CruxibleInstance) -> None:
    db_path = initialized_instance.instance_dir / "state.db"
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        migrations = {row[0] for row in conn.execute("SELECT migration_id FROM storage_migrations")}
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    assert "0001_unified_sqlite_state" in migrations
    assert SNAPSHOT_SCHEMA_MIGRATION in migrations
    assert journal_mode == "wal"


def test_new_instance_does_not_create_live_graph_json(
    initialized_instance: CruxibleInstance,
) -> None:
    assert (initialized_instance.instance_dir / "state.db").exists()
    assert not (initialized_instance.instance_dir / "graph.json").exists()


def test_pre_state_db_graph_json_is_not_imported(tmp_path: Path) -> None:
    instance_dir = tmp_path / ".cruxible"
    instance_dir.mkdir()
    (tmp_path / "config.yaml").write_text(CAR_PARTS_YAML)
    (instance_dir / "instance.json").write_text(
        json.dumps(
            {
                "config_path": "config.yaml",
                "data_dir": ".",
                "instance_mode": "dev",
            }
        )
    )
    old_graph = EntityGraph()
    old_graph.add_entity(_part())
    old_graph_path = instance_dir / "graph.json"
    old_graph_path.write_text(json.dumps(old_graph.to_dict(), indent=2))

    # Pre-state.db workspace import is intentionally unsupported in this branch.
    # state.db is the sole authoritative store for current instances.
    loaded = CruxibleInstance.load(tmp_path)
    assert loaded.load_graph().get_entity("Part", "BP-1") is None
    assert old_graph_path.exists()


def test_save_load_restart_preserves_relationship_state(
    initialized_instance: CruxibleInstance,
) -> None:
    graph = EntityGraph()
    graph.add_entity(_part())
    graph.add_entity(_vehicle())
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": True},
            metadata=RelationshipMetadata(
                provenance=RelationshipProvenance(source="ingest", source_ref="catalog.csv"),
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human"),
                    lifecycle=RelationshipLifecycleState(status="active"),
                    group_override=True,
                ),
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        EvidenceRef(
                            source="catalog",
                            source_record_id="row-1",
                            artifact_id="parts.csv",
                            row_index=7,
                        )
                    ],
                    rationale="catalog match",
                    source_receipt_ids=["RCP-source"],
                    source_trace_ids=["TRC-source"],
                    source_step_ids=["step-1"],
                ),
            ),
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": False},
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="rejected", source="agent"),
                    lifecycle=RelationshipLifecycleState(status="active"),
                )
            ),
        )
    )

    initialized_instance.save_graph(graph)
    restarted = CruxibleInstance.load(initialized_instance.root)
    loaded = restarted.load_graph()

    accepted = loaded.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits", edge_key=0)
    rejected = loaded.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits", edge_key=1)
    assert accepted is not None
    assert rejected is not None
    assert accepted.edge_key == 0
    assert rejected.edge_key == 1
    assert accepted.metadata.provenance is not None
    assert accepted.metadata.provenance.source == "ingest"
    assert accepted.metadata.evidence is not None
    assert accepted.metadata.evidence.evidence_refs[0].source_record_id == "row-1"
    assert accepted.metadata.assertion.review.status == "approved"
    assert accepted.metadata.assertion.group_override is True
    assert rejected.metadata.assertion.review.status == "rejected"
    assert loaded.has_live_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert len(list(loaded.iter_relationships("fits"))) == 2


def test_save_load_restart_preserves_typed_entity_metadata(
    initialized_instance: CruxibleInstance,
) -> None:
    """The typed entity-metadata envelope round-trips through ``metadata_json``.

    Mirrors ``RelationshipMetadata`` persistence: the typed
    :class:`EntityMetadata` (a real field on ``EntityInstance``, not a dict)
    serializes to the flat ``metadata_json`` dict and a restart reloads it
    byte-for-byte into the typed model -- the typed ``lifecycle`` (incl. ``retired``)
    and the typed ``actor_context`` (the live dogfooding shape), plus a free-form
    key walled off in ``extra``.
    """
    from datetime import datetime, timezone

    from cruxible_core.governance.actors import GovernedActorContext
    from cruxible_core.graph.assertion_state import EntityLifecycleState
    from cruxible_core.graph.types import EntityMetadata

    graph = EntityGraph()
    graph.add_entity(_vehicle())
    actor = GovernedActorContext(
        actor_type="human_user",
        actor_id="robert",
        org_id="inst_test",
        operation_id="op_close",
        timestamp=datetime(2026, 6, 15, 0, 6, 36, tzinfo=timezone.utc),
    )
    retired_part = EntityInstance(
        entity_type="Part",
        entity_id="BP-1",
        properties={"part_number": "BP-1", "name": "Pads", "category": "brakes"},
        # The typed envelope is assigned directly -- no dict round-trip needed.
        metadata=EntityMetadata(
            lifecycle=EntityLifecycleState(status="retired", reason="rolled up"),
            actor_context=actor,
            extra={"note": "keep-me"},
        ),
    )
    # The runtime field is genuinely typed, exactly like the relationship side.
    assert isinstance(retired_part.metadata, EntityMetadata)
    graph.add_entity(retired_part)

    initialized_instance.save_graph(graph)
    restarted = CruxibleInstance.load(initialized_instance.root)
    loaded = restarted.load_graph()

    reloaded = loaded.get_entity("Part", "BP-1")
    assert reloaded is not None
    # Reloads straight into the typed model -- no free-form spelunking.
    envelope = reloaded.metadata
    assert isinstance(envelope, EntityMetadata)
    assert envelope.lifecycle is not None
    assert envelope.lifecycle.status == "retired"
    assert envelope.lifecycle.reason == "rolled up"
    assert envelope.lifecycle_status() == "retired"
    assert envelope.is_live() is False
    # Typed actor_context survives.
    assert envelope.actor_context is not None
    assert envelope.actor_context.actor_id == "robert"
    # Free-form sibling key survives the round-trip, walled off in `extra`.
    assert envelope.extra["note"] == "keep-me"


def test_save_graph_does_not_create_live_graph_json(
    initialized_instance: CruxibleInstance,
) -> None:
    graph = EntityGraph()
    graph.add_entity(_part())
    graph.add_entity(_vehicle())
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
        )
    )

    initialized_instance.save_graph(graph)
    assert not (initialized_instance.instance_dir / "graph.json").exists()

    reloaded = CruxibleInstance.load(initialized_instance.root).load_graph()

    assert reloaded.get_entity("Part", "BP-1") is not None
    assert reloaded.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits") is not None


def test_delta_write_does_not_create_live_graph_json(
    initialized_instance: CruxibleInstance,
) -> None:
    graph = initialized_instance.load_graph()
    entity = _part()
    graph.add_entity(entity)

    initialized_instance.save_graph_delta(graph, entities=[entity], relationships=[])

    assert not (initialized_instance.instance_dir / "graph.json").exists()
    restarted = CruxibleInstance.load(initialized_instance.root)
    assert restarted.load_graph().get_entity("Part", "BP-1") is not None


def test_snapshot_graph_json_export_remains_node_link_compatible(
    initialized_instance: CruxibleInstance,
) -> None:
    graph = EntityGraph()
    graph.add_entity(_part())
    graph.add_entity(_vehicle())
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
        )
    )

    initialized_instance.save_graph(graph)
    snapshot = initialized_instance.create_snapshot(label="portable-export")
    exported = initialized_instance.instance_dir / "snapshots" / snapshot.snapshot_id / "graph.json"
    reloaded = EntityGraph.from_dict(json.loads(exported.read_text()))

    assert reloaded.get_entity("Part", "BP-1") is not None
    assert reloaded.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits") is not None


def test_snapshot_persists_db_rows_artifacts_and_head_state(
    initialized_instance: CruxibleInstance,
) -> None:
    graph = EntityGraph()
    graph.add_entity(_part())
    initialized_instance.save_graph(graph)

    snapshot = initialized_instance.create_snapshot(label="db-authority")

    conn = sqlite3.connect(initialized_instance.instance_dir / "state.db")
    try:
        snapshot_row = conn.execute(
            "SELECT snapshot_json FROM snapshots WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        ).fetchone()
        artifact_rows = conn.execute(
            "SELECT artifact_name, content FROM snapshot_artifacts WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        ).fetchall()
        head_value = conn.execute(
            "SELECT value_json FROM instance_state WHERE key = 'head_snapshot_id'",
        ).fetchone()
    finally:
        conn.close()

    assert snapshot_row is not None
    artifacts = {row[0]: bytes(row[1]) for row in artifact_rows}
    assert {"config.yaml", "graph.json", "snapshot.json"} <= set(artifacts)
    assert artifacts["snapshot.json"] == snapshot_row[0].encode("utf-8")
    assert json.loads(head_value[0]) == snapshot.snapshot_id


def test_instance_state_missing_and_json_null_return_none(
    initialized_instance: CruxibleInstance,
) -> None:
    with initialized_instance.write_transaction() as uow:
        assert uow.snapshots.get_instance_state("missing_state") is None
        uow.snapshots.set_instance_state("explicit_null", None)

    with initialized_instance._storage_backend().snapshot_repository() as snapshots:
        assert snapshots.get_instance_state("missing_state") is None
        assert snapshots.get_instance_state("explicit_null") is None


def test_head_snapshot_id_comes_from_db_not_stale_instance_json(
    initialized_instance: CruxibleInstance,
) -> None:
    snapshot = initialized_instance.create_snapshot(label="db-head")
    metadata_path = initialized_instance.instance_dir / "instance.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["head_snapshot_id"] = "snap_stale"
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True))

    reloaded = CruxibleInstance.load(initialized_instance.root)

    assert reloaded.metadata.head_snapshot_id == "snap_stale"
    assert reloaded.get_head_snapshot_id() == snapshot.snapshot_id


def test_missing_snapshot_files_can_be_exported_and_cloned_from_db_artifacts(
    initialized_instance: CruxibleInstance,
    tmp_path: Path,
) -> None:
    graph = EntityGraph()
    graph.add_entity(_part())
    initialized_instance.save_graph(graph)
    snapshot = initialized_instance.create_snapshot(label="recoverable")
    snapshot_dir = initialized_instance.instance_dir / "snapshots" / snapshot.snapshot_id
    shutil.rmtree(snapshot_dir)

    reloaded = CruxibleInstance.load(initialized_instance.root)

    assert reloaded.get_snapshot(snapshot.snapshot_id) == snapshot
    assert not snapshot_dir.exists()
    exported = reloaded._export_snapshot_artifacts(snapshot.snapshot_id)
    assert (exported / "graph.json").exists()

    shutil.rmtree(exported)
    clone, _ = CruxibleInstance.clone_from_snapshot(
        reloaded,
        snapshot.snapshot_id,
        tmp_path / "clone-from-db",
    )

    assert clone.load_graph().get_entity("Part", "BP-1") is not None
    assert clone.get_head_snapshot_id() == snapshot.snapshot_id


def test_clone_from_snapshot_imports_snapshot_export_to_sql(
    initialized_instance: CruxibleInstance,
    tmp_path: Path,
) -> None:
    graph = EntityGraph()
    graph.add_entity(_part())
    initialized_instance.save_graph(graph)
    snapshot = initialized_instance.create_snapshot(label="clone-source")

    clone, _ = CruxibleInstance.clone_from_snapshot(
        initialized_instance,
        snapshot.snapshot_id,
        tmp_path / "clone",
    )

    assert not (clone.instance_dir / "graph.json").exists()
    assert clone.load_graph().get_entity("Part", "BP-1") is not None


def _edge_with_receipt(receipt_id: str) -> RelationshipInstance:
    return RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
        metadata=RelationshipMetadata(
            provenance=RelationshipProvenance(
                source="workflow_apply",
                source_ref="workflow:canonical-fitment",
                receipt_id=receipt_id,
            )
        ),
    )


def _clone_has_no_dangling_receipt(clone: CruxibleInstance) -> None:
    """Assert the audit invariant: every edge receipt_id resolves or is null."""
    store = clone.get_receipt_store()
    try:
        for rel in clone.load_graph().iter_relationships():
            provenance = rel.metadata.provenance
            if provenance is None or provenance.receipt_id is None:
                continue
            assert store.get_receipt(provenance.receipt_id) is not None, (
                f"dangling receipt_id {provenance.receipt_id} on {rel.relationship_label()}"
            )
    finally:
        store.close()


def test_clone_from_snapshot_clears_dangling_receipt_and_stamps_clone_origin(
    initialized_instance: CruxibleInstance,
    tmp_path: Path,
) -> None:
    receipt = ReceiptBuilder(operation_type="add_relationship", parameters={"n": 1}).build()
    graph = EntityGraph()
    graph.add_entity(_vehicle())
    graph.add_entity(_part())
    graph.add_relationship(_edge_with_receipt(receipt.receipt_id))
    with initialized_instance.write_transaction() as uow:
        uow.receipts.save_receipt(receipt)
        uow.graph.save_graph(graph)

    # The source edge resolves to a real local receipt before cloning.
    _clone_has_no_dangling_receipt(initialized_instance)
    source_edge = initialized_instance.load_graph().get_relationship(
        "Part", "BP-1", "Vehicle", "V-1", "fits"
    )
    assert source_edge is not None
    assert source_edge.metadata.provenance is not None
    assert source_edge.metadata.provenance.receipt_id == receipt.receipt_id

    snapshot = initialized_instance.create_snapshot(label="clone-source")
    clone, _ = CruxibleInstance.clone_from_snapshot(
        initialized_instance,
        snapshot.snapshot_id,
        tmp_path / "clone",
    )

    cloned_edge = clone.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert cloned_edge is not None
    provenance = cloned_edge.metadata.provenance
    assert provenance is not None
    # Dangling receipt pointer is cleared; clone origin is stamped honestly.
    assert provenance.receipt_id is None
    assert provenance.clone_origin == "upstream-snapshot"
    # Original authoring history is preserved, including the cleared receipt id.
    assert provenance.source == "workflow_apply"
    assert provenance.source_ref == "workflow:canonical-fitment"
    assert getattr(provenance, "cloned_receipt_id", None) == receipt.receipt_id
    # The receipt itself was NOT shipped in the bundle.
    clone_store = clone.get_receipt_store()
    try:
        assert clone_store.get_receipt(receipt.receipt_id) is None
    finally:
        clone_store.close()
    # Invariant: no edge in the clone references a receipt that is not present.
    _clone_has_no_dangling_receipt(clone)


def test_after_commit_failure_does_not_run_rollback_callbacks_after_db_commit(
    initialized_instance: CruxibleInstance,
) -> None:
    rollback_calls: list[str] = []

    def fail_after_commit() -> None:
        raise RuntimeError("after commit callback failed")

    with pytest.raises(RuntimeError, match="after commit callback failed"):
        with initialized_instance.write_transaction() as uow:
            uow.graph.upsert_entities([_part("BP-AFTER-COMMIT")])
            uow.register_after_rollback(lambda: rollback_calls.append("rollback"))
            uow.register_after_commit(fail_after_commit)

    assert rollback_calls == []
    restarted = CruxibleInstance.load(initialized_instance.root)
    assert restarted.load_graph().get_entity("Part", "BP-AFTER-COMMIT") is not None


def test_receipt_then_graph_failure_rolls_back_both(
    initialized_instance: CruxibleInstance,
) -> None:
    graph = EntityGraph()
    graph.add_entity(_part())
    receipt = ReceiptBuilder(operation_type="add_entity", parameters={"count": 1}).build()

    def fail_save_graph(self: SQLiteGraphRepository, candidate: EntityGraph) -> None:
        raise RuntimeError("graph commit failed")

    with (
        patch.object(SQLiteGraphRepository, "save_graph", fail_save_graph),
        pytest.raises(RuntimeError, match="graph commit failed"),
    ):
        with initialized_instance.write_transaction() as uow:
            uow.receipts.save_receipt(receipt)
            uow.graph.save_graph(graph)

    store = initialized_instance.get_receipt_store()
    try:
        assert store.get_receipt(receipt.receipt_id) is None
    finally:
        store.close()
    assert initialized_instance.load_graph().get_entity("Part", "BP-1") is None


def test_decision_then_outcome_failure_rolls_back_compound_write(
    initialized_instance: CruxibleInstance,
) -> None:
    decision = DecisionRecord(question="Approve this change?", opened_by="agent")
    outcome = OutcomeRecord(
        receipt_id="RCP-1",
        anchor_type="receipt",
        anchor_id="RCP-1",
        outcome="correct",
    )

    def fail_save_outcome(self: FeedbackStore, record: OutcomeRecord) -> str:
        raise RuntimeError("outcome write failed")

    with (
        patch.object(FeedbackStore, "save_outcome", fail_save_outcome),
        pytest.raises(RuntimeError, match="outcome write failed"),
    ):
        with initialized_instance.write_transaction() as uow:
            uow.decisions.save_record(decision)
            uow.feedback.save_outcome(outcome)

    store = initialized_instance.get_decision_store()
    try:
        assert store.get_record(decision.decision_record_id) is None
    finally:
        store.close()


def test_snapshot_exports_run_only_after_write_transaction_commit(
    initialized_instance: CruxibleInstance,
) -> None:
    graph = EntityGraph()
    graph.add_entity(_part("BP-SNAPSHOT"))
    initialized_instance.save_graph(graph)

    with initialized_instance.write_transaction():
        snapshot = initialized_instance.create_snapshot(label="post-commit-export")
        snapshot_dir = initialized_instance.instance_dir / "snapshots" / snapshot.snapshot_id
        assert not snapshot_dir.exists()

    assert snapshot_dir.exists()
    assert (snapshot_dir / "graph.json").exists()
    assert initialized_instance.get_head_snapshot_id() == snapshot.snapshot_id


def test_store_repositories_do_not_expose_transaction_ownership() -> None:
    removed_commit_flag = "auto" + "_commit"
    for protocol in (DecisionStoreProtocol, FeedbackStoreProtocol, GroupStoreProtocol):
        assert not hasattr(protocol, "transaction")

    for store_type in (SQLiteReceiptStore, FeedbackStore, GroupStore, DecisionStore):
        assert not hasattr(store_type, "transaction")
        assert removed_commit_flag not in inspect.signature(store_type).parameters


def test_instance_store_getters_are_not_used_for_direct_writes() -> None:
    getter_names = {
        "get_receipt_store",
        "get_feedback_store",
        "get_group_store",
        "get_decision_store",
    }
    write_methods = {
        "append_event",
        "confirm_resolution",
        "delete_group",
        "replace_members",
        "save_feedback",
        "save_feedback_batch",
        "save_group",
        "save_members",
        "save_outcome",
        "save_receipt",
        "save_record",
        "save_resolution",
        "save_trace",
        "update_group",
        "update_group_status",
        "update_record",
        "update_resolution_trust_status",
    }
    offenders: list[str] = []

    for root in (Path("src/cruxible_core"), Path("tests")):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for function in (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                store_names: set[str] = set()
                for node in ast.walk(function):
                    if (
                        isinstance(node, ast.Assign)
                        and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute)
                        and node.value.func.attr in getter_names
                    ):
                        store_names.update(
                            target.id for target in node.targets if isinstance(target, ast.Name)
                        )
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in write_methods
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in store_names
                    ):
                        offenders.append(
                            f"{path}:{node.lineno}:{node.func.value.id}.{node.func.attr}"
                        )

    assert offenders == []


def test_no_direct_sqlite_imports_outside_storage_implementation() -> None:
    allowed = {
        Path("src/cruxible_core/storage/sqlite.py"),
        Path("src/cruxible_core/receipt/store.py"),
        Path("src/cruxible_core/feedback/store.py"),
        Path("src/cruxible_core/group/store.py"),
        Path("src/cruxible_core/procedure/store.py"),
        Path("src/cruxible_core/decision/store.py"),
        Path("src/cruxible_core/server/registry.py"),
        Path("src/cruxible_core/server/credentials.py"),
    }
    offenders: list[str] = []
    for path in Path("src/cruxible_core").rglob("*.py"):
        tree = ast.parse(path.read_text())
        has_sqlite_import = any(
            (isinstance(node, ast.Import) and any(alias.name == "sqlite3" for alias in node.names))
            or (isinstance(node, ast.ImportFrom) and node.module == "sqlite3")
            for node in ast.walk(tree)
        )
        if has_sqlite_import and path not in allowed:
            offenders.append(str(path))

    assert offenders == []
