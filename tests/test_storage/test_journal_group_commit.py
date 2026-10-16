"""A terminal's group preserves the chain, replay, leases and unavailable-body semantics."""

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import JournalConflictError
from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.storage import cas
from cruxible_core.storage.cas import ContentAddressedBodyStore
from cruxible_core.storage.material_reservations import (
    ProcedureMaterialReservationStore,
    make_run_reservation,
)
from tests.test_storage.test_journal_backends import _activate, _backend, _draft, _stream

PARTITION = "runs-2026-08"


@pytest.mark.parametrize("prefix", [0, 1, 3])
def test_group_chain_and_exact_prefix_replay(tmp_path: Path, prefix: int) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    head = backend.read_head(_stream(), PARTITION)
    drafts = tuple(_draft(str(index)) for index in range(3))
    if prefix:
        backend.append_many(drafts[:prefix], expected_head=head, fencing_token="writer-a")
    group = backend.append_many(drafts, expected_head=head, fencing_token="writer-a")
    assert tuple(item.record.sequence for item in group) == (1, 2, 3)
    assert group[0].record.previous_record_digest == head.record_digest
    assert all(b.record.previous_record_digest == a.record_digest for a, b in zip(group, group[1:]))
    assert backend.append_many(drafts, expected_head=head, fencing_token="writer-a") == group
    assert backend.select_records(_stream(), partition_id=PARTITION) == group
    with pytest.raises(JournalConflictError, match="stale or forked"):
        backend.append_many(
            (_draft("different"), *drafts[1:]), expected_head=head, fencing_token="writer-a"
        )
    with pytest.raises(JournalConflictError, match="fencing token"):
        backend.append_many(drafts, expected_head=head, fencing_token="wrong")


def test_group_is_one_log_write_and_does_not_flush_exhaust(tmp_path: Path, monkeypatch) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    # Warm the head; a regular group needs no frame readback.
    head = backend.read_head(_stream(), PARTITION)
    writes = []
    real_write = cas.os.write

    def write(fd, value):
        if bytes(value).startswith(b"\x00"):
            writes.append(len(value))
        return real_write(fd, value)

    def no_flush(fd):
        pytest.fail("exhaust performed a per-group/per-item fsync")

    def no_readback(*args, **kwargs):
        pytest.fail("the bound group was re-read from the log")

    monkeypatch.setattr(cas.os, "write", write)
    monkeypatch.setattr(cas.os, "fsync", no_flush)
    monkeypatch.setattr(LocalJournalBackend, "_read_frames", staticmethod(no_readback))
    group = backend.append_many(
        tuple(_draft(str(i)) for i in range(20)), expected_head=head, fencing_token="writer-a"
    )
    assert len(group) == 20
    assert len(writes) == 1


def test_head_memo_notices_another_backend_and_a_crash_tail(tmp_path: Path) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    head = backend.read_head(_stream(), PARTITION)
    first = backend.append(_draft("first"), expected_head=head, fencing_token="writer-a")
    other = LocalJournalBackend(backend.root)
    head = other.read_head(_stream(), PARTITION)
    second = other.append(_draft("second"), expected_head=head, fencing_token="writer-a")
    assert backend.read_head(_stream(), PARTITION).record_digest == second.record_digest
    path = backend._record_log_path_for_testing(_stream(), PARTITION)
    with path.open("ab") as handle:
        handle.write(b"\x00\x00")
    third = backend.append(
        _draft("third"),
        expected_head=backend.read_head(_stream(), PARTITION),
        fencing_token="writer-a",
    )
    assert backend.select_records(_stream(), partition_id=PARTITION) == (first, second, third)


@pytest.mark.parametrize("damage", ["missing", "truncated"])
def test_lost_unflushed_payload_is_unavailable_without_breaking_journal(
    tmp_path: Path,
    damage: str,
    monkeypatch,
) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    root = tmp_path / "cas"
    root.mkdir()
    bodies = ContentAddressedBodyStore(root)
    leases = ProcedureMaterialReservationStore(bodies.reservation_root)
    contents = [b'{"item":1}', b'{"item":2}']
    drafts = tuple(
        _draft(str(i)).model_copy(
            update={
                "payload_digest": bodies.digest_bytes(content).tagged,
            }
        )
        for i, content in enumerate(contents)
    )
    reservations = tuple(
        make_run_reservation(
            instance_id=draft.stream.instance_id,
            partition_id=PARTITION,
            event_kind=draft.event_kind,
            run_id=draft.run_id,
            admission_binding_digest=draft.admission_binding_digest,
            body_digest=draft.payload_digest,
        )
        for draft in drafts
    )
    with leases.locked():
        group_id = leases.reserve_many_locked(reservations)
        assert leases.active_locked() == reservations
        with monkeypatch.context() as patch:
            patch.setattr(cas.os, "fsync", lambda fd: pytest.fail("payload fsync"))
            bodies.store_many(contents, journal_payload=True)
        group = backend.append_many(
            drafts, expected_head=backend.read_head(_stream(), PARTITION), fencing_token="writer-a"
        )
        leases.release_locked(group_id)
    # Simulate the OS losing the last unflushed payload after a crash. The
    # journal remains authenticated; the missing/truncated body is unavailable.
    path = bodies._path(drafts[0].payload_digest)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(contents[0][:3])
    reopened = ContentAddressedBodyStore(root)
    assert reopened.availability(drafts[0].payload_digest) in {"missing", "corrupt"}
    assert reopened.availability(drafts[0].payload_digest) != "present"
    assert reopened.verify(drafts[1].payload_digest)
    assert (
        LocalJournalBackend(backend.root).select_records(_stream(), partition_id=PARTITION) == group
    )
    assert not leases.active()


def test_group_lease_recovery_releases_only_after_the_complete_scan(tmp_path: Path) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    bodies = ContentAddressedBodyStore(root)
    leases = ProcedureMaterialReservationStore(bodies.reservation_root)
    reservations = tuple(
        make_run_reservation(
            instance_id="instance-a",
            partition_id=PARTITION,
            event_kind="node_fired",
            run_id="run-a",
            admission_binding_digest=_draft("x").admission_binding_digest,
            body_digest=bodies.digest_bytes(str(index).encode()).tagged,
        )
        for index in range(3)
    )
    with leases.locked():
        leases.reserve_many_locked(reservations)
    assert leases.recover_run_material((), bodies=bodies) == tuple(
        item.reservation_id for item in reservations
    )
    assert not leases.active()


def test_exhaust_body_promoted_to_evidence_is_flushed(tmp_path: Path, monkeypatch) -> None:
    bodies = ContentAddressedBodyStore(tmp_path)
    value = b"a payload later used as evidence"
    bodies.store(value, journal_payload=True)
    flushed = []
    real_fsync = cas.os.fsync

    def fsync(fd):
        flushed.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(cas.os, "fsync", fsync)
    bodies.store(value)
    assert len(flushed) >= 2  # the body and its containing directory


def test_a_dry_run_batch_keeps_new_bodies_only_in_memory(tmp_path: Path) -> None:
    from cruxible_core.storage.cas import BodyAccessContext, DryRunBodyStore

    base = ContentAddressedBodyStore(tmp_path)
    held: dict[str, bytes] = {}
    preview = DryRunBodyStore(base, held)
    values = [b"first dry-run payload", b"second dry-run payload"]
    group = preview.store_many(values, journal_payload=True)
    for item, value in zip(group, values):
        assert held[item.digest] == value
        assert (
            preview.read(
                item.digest,
                access=BodyAccessContext(
                    principal_id="test",
                    can_read_body=True,
                ),
            )
            == value
        )
        assert not base._path(item.digest).exists()


def test_a_failed_member_scan_retains_the_whole_group_lease(tmp_path: Path) -> None:
    bodies = ContentAddressedBodyStore(tmp_path)
    leases = ProcedureMaterialReservationStore(bodies.reservation_root)
    reservations = tuple(
        make_run_reservation(
            instance_id="instance-a",
            partition_id=PARTITION,
            event_kind="node_fired",
            run_id="run-a",
            admission_binding_digest=_draft("x").admission_binding_digest,
            body_digest=bodies.digest_bytes(str(index).encode()).tagged,
        )
        for index in range(2)
    )
    with leases.locked():
        leases.reserve_many_locked(reservations)

    def incomplete_scan(reservation):
        if reservation.reservation_id == reservations[-1].reservation_id:
            raise RuntimeError("incomplete scan")
        return ()

    with pytest.raises(RuntimeError, match="incomplete scan"):
        leases.recover_run_material(incomplete_scan, bodies=bodies)
    assert leases.active() == reservations


def test_bound_record_carries_immutable_contracts_but_detaches_mutable_attribution():
    from cruxible_core.exhaust.records import ProcedureJournalRecordV1, journal_genesis_digest

    draft = _draft("owned-attribution")
    previous = journal_genesis_digest(draft.stream, draft.partition_id)
    record = ProcedureJournalRecordV1.bind(draft, sequence=1, previous_record_digest=previous)
    fresh = ProcedureJournalRecordV1.model_validate(
        {
            **draft.model_dump(mode="python", exclude={"tag"}),
            "sequence": 1,
            "previous_record_digest": previous,
        }
    )
    assert record == fresh
    assert record.stream is draft.stream
    assert record.accepted_coordinate is draft.accepted_coordinate
    assert record.actor_context is not draft.actor_context
    draft.actor_context.actor_id = "changed-draft"
    assert record.actor_context.actor_id == "operator"
    record.actor_context.actor_id = "changed-record"
    assert draft.actor_context.actor_id == "changed-draft"
