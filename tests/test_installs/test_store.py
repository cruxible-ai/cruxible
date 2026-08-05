"""Store-level guarantees the service layer relies on but does not itself impose."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.installs.store import InstallLedgerStore
from cruxible_core.installs.types import (
    InstallPhaseEvent,
    InstallRecord,
    ObjectReference,
    OwnedObject,
)
from cruxible_core.storage.sqlite import INSTALL_LEDGER_MIGRATION, SQLiteStorageBackend
from tests.test_installs.conftest import artifact_ref, create_install

NOW = "2026-08-05T00:00:00+00:00"


@pytest.fixture
def store(tmp_path: Path) -> InstallLedgerStore:
    connection = sqlite3.connect(tmp_path / "state.db")
    connection.execute("PRAGMA foreign_keys = ON")
    return InstallLedgerStore(tmp_path / "state.db", connection=connection)


def _install(store: InstallLedgerStore, install_id: str, phase: str = "preparing") -> None:
    store.save_install(
        InstallRecord(
            install_id=install_id,
            artifact=artifact_ref(artifact_id=install_id),
            phase=phase,  # type: ignore[arg-type]
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _owned(install_id: str, name: str = "pub.a.q", kind: str = "named_query") -> OwnedObject:
    return OwnedObject(
        install_id=install_id,
        object_kind=kind,  # type: ignore[arg-type]
        object_name=name,
        installed_digest="sha256:x",
        recorded_at=NOW,
    )


# ---------------------------------------------------------------------------
# Database-enforced invariants
# ---------------------------------------------------------------------------


def test_one_live_owner_per_object_is_a_database_guarantee(
    store: InstallLedgerStore,
) -> None:
    """The partial unique index, not the service, is what makes this atomic."""
    _install(store, "inst-a")
    _install(store, "inst-b")
    store.save_owned_object(_owned("inst-a"))

    with pytest.raises(sqlite3.IntegrityError):
        store.save_owned_object(_owned("inst-b"))


@pytest.mark.parametrize("phase", ["failed", "rolling_back"])
def test_a_claim_under_cleanup_still_blocks_at_the_database_level(
    store: InstallLedgerStore,
    phase: str,
) -> None:
    """The index, not the service, is what stops a claim while cleanup is owed."""
    _install(store, "inst-a")
    _install(store, "inst-b")
    store.save_owned_object(_owned("inst-a"))
    store.set_install_phase(
        "inst-a",
        phase=phase,  # type: ignore[arg-type]
        updated_at=NOW,
        failure_reason="x",
        receipt_id=None,
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.save_owned_object(_owned("inst-b"))


def test_a_released_claim_frees_the_name_at_the_database_level(
    store: InstallLedgerStore,
) -> None:
    _install(store, "inst-a")
    _install(store, "inst-b")
    store.save_owned_object(_owned("inst-a"))
    for phase in ("failed", "rolling_back", "rolled_back"):
        store.set_install_phase(
            "inst-a",
            phase=phase,  # type: ignore[arg-type]
            updated_at=NOW,
            failure_reason="x",
            receipt_id=None,
        )

    store.save_owned_object(_owned("inst-b"))
    owner = store.find_live_owner(object_kind="named_query", object_name="pub.a.q")
    assert owner is not None
    assert owner.owning_install_id == "inst-b"


def test_owned_objects_require_an_existing_install(store: InstallLedgerStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.save_owned_object(_owned("inst-ghost"))


def test_phase_event_sequences_are_unique_per_install(store: InstallLedgerStore) -> None:
    _install(store, "inst-a")
    event = InstallPhaseEvent(
        event_id="evt-1",
        install_id="inst-a",
        sequence=1,
        from_phase=None,
        to_phase="preparing",
        occurred_at=NOW,
    )
    store.append_phase_event(event)

    with pytest.raises(sqlite3.IntegrityError):
        store.append_phase_event(event.model_copy(update={"event_id": "evt-2"}))


def test_an_unknown_phase_is_rejected_by_the_check_constraint(
    store: InstallLedgerStore,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(  # noqa: SLF001 - asserting the DDL constraint itself
            "INSERT INTO installs (install_id, artifact_kind, artifact_id, "
            "artifact_version, artifact_digest, phase, created_at, updated_at) "
            "VALUES ('i', 'blueprint', 'a', '1', 'sha256:x', 'nonsense', ?, ?)",
            (NOW, NOW),
        )


def test_an_unknown_object_kind_is_rejected_by_the_check_constraint(
    store: InstallLedgerStore,
) -> None:
    _install(store, "inst-a")
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(  # noqa: SLF001 - asserting the DDL constraint itself
            "INSERT INTO install_owned_objects (install_id, object_kind, object_name, "
            "installed_digest, recorded_at) VALUES ('inst-a', 'workflow', 'x', 'd', ?)",
            (NOW,),
        )


# ---------------------------------------------------------------------------
# Sequencing and round-trips
# ---------------------------------------------------------------------------


def test_next_phase_sequence_starts_at_one_and_increments(
    store: InstallLedgerStore,
) -> None:
    _install(store, "inst-a")
    assert store.next_phase_sequence("inst-a") == 1
    store.append_phase_event(
        InstallPhaseEvent(
            event_id="evt-1",
            install_id="inst-a",
            sequence=1,
            from_phase=None,
            to_phase="preparing",
            occurred_at=NOW,
        )
    )
    assert store.next_phase_sequence("inst-a") == 2


def test_references_round_trip_through_json(store: InstallLedgerStore) -> None:
    _install(store, "inst-a")
    owned = _owned("inst-a").model_copy(
        update={
            "references": [ObjectReference(object_kind="contract", object_name="pub.a.Request")]
        }
    )
    store.save_owned_object(owned)

    loaded = store.list_owned_objects("inst-a")[0]
    assert loaded.references == owned.references


def test_install_round_trips_its_artifact_reference(store: InstallLedgerStore) -> None:
    _install(store, "inst-a")
    loaded = store.get_install("inst-a")
    assert loaded is not None
    assert loaded.artifact.artifact_kind == "blueprint"
    assert loaded.artifact.artifact_digest == "sha256:blueprint-a"


def test_counting_and_filtering_agree(store: InstallLedgerStore) -> None:
    _install(store, "inst-a", phase="active")
    _install(store, "inst-b")

    assert store.count_installs() == 2
    assert store.count_installs(phase="active") == 1
    assert [record.install_id for record in store.list_installs(phase="active")] == ["inst-a"]


def test_missing_install_reads_as_none(store: InstallLedgerStore) -> None:
    assert store.get_install("inst-nope") is None
    assert store.list_owned_objects("inst-nope") == []
    assert store.list_phase_events("inst-nope") == []


# ---------------------------------------------------------------------------
# Migration wiring
# ---------------------------------------------------------------------------


def test_a_fresh_state_db_is_stamped_with_the_install_ledger_migration(
    tmp_path: Path,
) -> None:
    backend = SQLiteStorageBackend(tmp_path / "state.db")
    backend.initialize()
    assert backend.has_migration(INSTALL_LEDGER_MIGRATION)


def test_a_pre_ledger_database_is_upgraded_in_place(tmp_path: Path) -> None:
    """The 0005 stamp is additive: an existing database gains the tables."""
    db_path = tmp_path / "state.db"
    backend = SQLiteStorageBackend(db_path)
    backend.initialize()

    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE install_owned_objects")
    connection.execute("DROP TABLE install_phase_events")
    connection.execute("DROP TABLE installs")
    connection.execute(
        "DELETE FROM storage_migrations WHERE migration_id = ?",
        (INSTALL_LEDGER_MIGRATION,),
    )
    connection.commit()
    connection.close()

    SQLiteStorageBackend(db_path).initialize()

    reopened = sqlite3.connect(db_path)
    tables = {
        row[0] for row in reopened.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    reopened.close()
    assert {"installs", "install_phase_events", "install_owned_objects"} <= tables


def test_the_unit_of_work_exposes_the_ledger_slot(tmp_path: Path) -> None:
    backend = SQLiteStorageBackend(tmp_path / "state.db")
    backend.initialize()
    with backend.unit_of_work() as uow:
        assert isinstance(uow.installs, InstallLedgerStore)


def test_ledger_writes_advance_the_read_revision(instance: CruxibleInstance) -> None:
    """Install records are STATE, not audit records: a read of the instance
    returns different install rows after one, so the freshness marker must move
    or a cached listing could never be invalidated."""
    before = instance.get_read_revision()
    create_install(instance)
    assert instance.get_read_revision() > before
