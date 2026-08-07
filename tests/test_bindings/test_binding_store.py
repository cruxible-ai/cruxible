"""Storage-level guarantees of the binding ledger.

The one-active-binding-per-slot rule is a DATABASE guarantee, not a service
convention. These tests bypass the service and go at the schema directly,
because a service-level check that happens to hold in single-threaded tests
proves nothing about two writers on two connections.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cruxible_core.bindings.store import BindingStore
from cruxible_core.bindings.types import SlotBinding, SlotBindingRevision
from cruxible_core.storage.sqlite import (
    BINDING_LEDGER_MIGRATION,
    SQLiteStorageBackend,
)


def _binding(**overrides: object) -> SlotBinding:
    defaults: dict[str, object] = {
        "install_id": "inst-prod-1",
        "slot_name": "summarize",
        "provider_name": "summarizer-core",
        "contract_in": "doc.v1",
        "contract_out": "summary.v1",
        "billing_mode": "included",
    }
    defaults.update(overrides)
    return SlotBinding(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> BindingStore:
    return BindingStore(tmp_path / "state.db")


class TestOneActiveBindingInvariant:
    def test_the_database_refuses_a_second_active_row_for_one_slot(
        self, store: BindingStore
    ) -> None:
        store.save_binding(_binding())
        with pytest.raises(sqlite3.IntegrityError):
            store.save_binding(_binding(provider_name="summarizer-alt"))

    def test_retired_rows_do_not_occupy_the_slot(self, store: BindingStore) -> None:
        """The index is PARTIAL: history must not block the next binding."""
        store.save_binding(_binding(status="retired"))
        store.save_binding(_binding(status="retired", provider_name="summarizer-old"))
        store.save_binding(_binding(provider_name="summarizer-new"))

        active = store.get_active_binding(install_id="inst-prod-1", slot_name="summarize")
        assert active is not None
        assert active.provider_name == "summarizer-new"
        assert store.count_bindings(install_id="inst-prod-1") == 3

    def test_two_connections_cannot_both_bind_the_same_slot(self, tmp_path: Path) -> None:
        """The race the service's read-then-write check cannot close on its own."""
        db_path = tmp_path / "state.db"
        BindingStore(db_path).close()

        first = sqlite3.connect(db_path)
        second = sqlite3.connect(db_path)
        try:
            first.row_factory = sqlite3.Row
            second.row_factory = sqlite3.Row
            store_a = BindingStore(db_path, connection=first, initialize_schema=False)
            store_b = BindingStore(db_path, connection=second, initialize_schema=False)

            # Both read an unbound slot. Nothing in either read can prevent the
            # other from writing; only the index can.
            assert store_a.get_active_binding(install_id="inst-prod-1", slot_name="s") is None
            assert store_b.get_active_binding(install_id="inst-prod-1", slot_name="s") is None

            store_a.save_binding(_binding(slot_name="s", provider_name="a"))
            first.commit()

            with pytest.raises(sqlite3.IntegrityError):
                store_b.save_binding(_binding(slot_name="s", provider_name="b"))
                second.commit()
        finally:
            first.close()
            second.close()

        verify = BindingStore(db_path)
        try:
            assert verify.count_bindings(slot_name="s") == 1
        finally:
            verify.close()

    def test_slots_on_different_installs_do_not_collide(self, store: BindingStore) -> None:
        store.save_binding(_binding())
        store.save_binding(_binding(install_id="inst-staging-1"))
        assert store.count_bindings(slot_name="summarize", status="active") == 2


class TestPinnedInterfaceIsWriteOnce:
    """The pinned interface is immutable at the STORAGE layer, not by convention.

    The service refuses a rebind that redefines the interface, but the update
    statement is what makes the immutability true: even handed a model whose
    interface fields were altered, no update path can move them.
    """

    def test_the_pinned_interface_survives_the_round_trip(self, store: BindingStore) -> None:
        original = _binding(
            allowed_billing_modes=("included", "metered"),
            requires_third_party_consent=True,
        )
        store.save_binding(original)
        loaded = store.get_binding(original.binding_id)
        assert loaded is not None
        assert loaded.allowed_billing_modes == ("included", "metered")
        assert loaded.requires_third_party_consent is True

    def test_an_unconstrained_allowlist_stays_null_rather_than_empty(
        self, store: BindingStore
    ) -> None:
        """``None`` means "any mode"; ``()`` would mean "no mode". Not the same."""
        binding = _binding()
        store.save_binding(binding)
        row = store._conn.execute(
            "SELECT allowed_billing_modes FROM slot_bindings WHERE binding_id = ?",
            (binding.binding_id,),
        ).fetchone()
        assert row["allowed_billing_modes"] is None
        loaded = store.get_binding(binding.binding_id)
        assert loaded is not None
        assert loaded.allowed_billing_modes is None

    def test_update_binding_cannot_move_the_pinned_interface(self, store: BindingStore) -> None:
        original = _binding(
            allowed_billing_modes=("included",),
            requires_third_party_consent=True,
        )
        store.save_binding(original)

        tampered = original.model_copy(
            update={
                "provider_name": "summarizer-fast",
                "contract_in": "doc.v2",
                "contract_out": "summary.v2",
                "allowed_billing_modes": ("included", "byo_key"),
                "requires_third_party_consent": False,
                "revision": 2,
            }
        )
        store.update_binding(tampered)

        loaded = store.get_binding(original.binding_id)
        assert loaded is not None
        assert loaded.provider_name == "summarizer-fast"
        assert loaded.revision == 2
        assert loaded.contract_in == "doc.v1"
        assert loaded.contract_out == "summary.v1"
        assert loaded.allowed_billing_modes == ("included",)
        assert loaded.requires_third_party_consent is True


class TestRevisionHistory:
    def test_revisions_are_unique_per_binding(self, store: BindingStore) -> None:
        binding = _binding()
        store.save_binding(binding)
        revision = SlotBindingRevision(
            binding_id=binding.binding_id,
            revision=1,
            change_kind="bind",
            install_id=binding.install_id,
            slot_name=binding.slot_name,
            provider_name=binding.provider_name,
            contract_in=binding.contract_in,
            contract_out=binding.contract_out,
            billing_mode=binding.billing_mode,
            status="active",
        )
        store.save_revision(revision)
        with pytest.raises(sqlite3.IntegrityError):
            store.save_revision(revision)

    def test_status_and_change_kind_are_constrained_by_the_schema(
        self, store: BindingStore
    ) -> None:
        """The vocabulary is a CHECK constraint, not only a Python Literal."""
        connection = store._conn
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO slot_bindings (binding_id, install_id, slot_name, "
                "provider_name, contract_in, contract_out, billing_mode, status, "
                "bound_at, updated_at) VALUES "
                "('b1', 'i', 's', 'p', 'a', 'b', 'included', 'paused', 'x', 'x')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO slot_binding_revisions (binding_id, revision, "
                "change_kind, install_id, slot_name, provider_name, contract_in, "
                "contract_out, billing_mode, status, recorded_at) VALUES "
                "('b1', 1, 'swap', 'i', 's', 'p', 'a', 'b', 'included', 'active', 'x')"
            )


class TestPaginationAndRoundTrip:
    def test_pagination_is_stable_and_ordering_is_deterministic(self, store: BindingStore) -> None:
        for slot in ("alpha", "beta", "gamma"):
            store.save_binding(_binding(slot_name=slot))

        assert [row.slot_name for row in store.list_bindings()] == ["alpha", "beta", "gamma"]
        assert [row.slot_name for row in store.list_bindings(limit=2)] == ["alpha", "beta"]
        assert [row.slot_name for row in store.list_bindings(limit=2, offset=2)] == ["gamma"]
        assert [row.slot_name for row in store.list_bindings(offset=1)] == ["beta", "gamma"]

    def test_every_field_survives_the_round_trip(self, store: BindingStore) -> None:
        original = _binding(
            third_party_consent=True,
            consent_actor_id="agent-alpha",
            consent_org_id="org-acme",
            consent_at="2026-08-05T12:00:00+00:00",
            revision=4,
            receipt_id="RCP-abc",
        )
        store.save_binding(original)
        loaded = store.get_binding(original.binding_id)
        assert loaded is not None
        assert loaded.model_dump(mode="json") == original.model_dump(mode="json")


class TestMigration:
    def test_a_fresh_state_db_is_stamped_with_the_binding_migration(self, tmp_path: Path) -> None:
        backend = SQLiteStorageBackend(tmp_path / "state.db")
        backend.initialize()
        assert backend.has_migration(BINDING_LEDGER_MIGRATION)

    def test_a_pre_binding_database_gains_the_tables_on_open(self, tmp_path: Path) -> None:
        """Upgrade path: the tables and the stamp both arrive, once."""
        db_path = tmp_path / "state.db"
        backend = SQLiteStorageBackend(db_path)
        backend.initialize()

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP TABLE slot_bindings")
            conn.execute("DROP TABLE slot_binding_revisions")
            conn.execute(
                "DELETE FROM storage_migrations WHERE migration_id = ?",
                (BINDING_LEDGER_MIGRATION,),
            )
            conn.commit()
        finally:
            conn.close()

        SQLiteStorageBackend(db_path).initialize()
        assert SQLiteStorageBackend(db_path).has_migration(BINDING_LEDGER_MIGRATION)

        store = BindingStore(db_path, initialize_schema=False)
        try:
            assert store.count_bindings() == 0
        finally:
            store.close()

    def test_the_unit_of_work_exposes_the_binding_store(self, tmp_path: Path) -> None:
        backend = SQLiteStorageBackend(tmp_path / "state.db")
        with backend.unit_of_work() as uow:
            uow.bindings.save_binding(_binding())
        with backend.unit_of_work() as uow:
            assert uow.bindings.count_bindings() == 1
