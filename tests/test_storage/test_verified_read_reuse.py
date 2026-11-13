"""Remembered verification never outlives the exact file and path binding it proved."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import CasError, FormatError
from cruxible_core.ledger import git as ledger_git
from cruxible_core.ledger.git import GitLedger
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage import cas, landing_log
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.storage.landing_log import LOG_NAME, PENDING_NAME, LandingLog

READ = BodyAccessContext(principal_id="owner", can_read_body=True)


def _store(tmp_path: Path) -> tuple[ContentAddressedBodyStore, str, Path]:
    root = tmp_path / "managed" / "cas"
    root.mkdir(parents=True)
    store = ContentAddressedBodyStore(root)
    digest = store.store(b"good").digest
    assert store.read(digest, access=READ) == b"good"  # warm the remembered proof
    return store, digest, store._path(digest)


def test_a_file_replaced_just_before_a_warm_read_is_refused(tmp_path, monkeypatch):
    store, digest, path = _store(tmp_path)
    opened = os.open

    def replace_then_open(target, flags, *args, **kwargs):
        if str(target) == path.name:
            evil = path.with_name("evil")
            evil.write_bytes(b"evil")
            os.replace(evil, path)
            monkeypatch.setattr(cas.os, "open", opened)
        return opened(target, flags, *args, **kwargs)

    monkeypatch.setattr(cas.os, "open", replace_then_open)
    with pytest.raises(CasError, match="do not match"):
        store.read(digest, access=READ)


def test_a_file_rewritten_in_place_after_warming_is_refused(tmp_path):
    store, digest, path = _store(tmp_path)
    path.chmod(0o600)
    with path.open("r+b") as handle:  # same inode, same size, new bytes
        handle.write(b"evil")
    with pytest.raises(CasError, match="do not match"):
        store.read(digest, access=READ)


@pytest.mark.parametrize("level", ["algorithm", "cas", "managed"])
def test_a_swapped_ancestor_never_redirects_a_retained_store(tmp_path, level):
    store, digest, _path = _store(tmp_path)
    ancestor = {
        "algorithm": store._algorithm_root,
        "cas": store._algorithm_root.parent,
        "managed": store._algorithm_root.parent.parent,
    }[level]
    outside = tmp_path / "outside"
    outside.mkdir()
    # Move the directory out of custody and put a symlink to an empty stand-in
    # at its path: the retained store must keep addressing what it validated.
    os.replace(ancestor, outside / "moved")
    stand_in = outside / "stand-in"
    stand_in.mkdir()
    ancestor.symlink_to(stand_in, target_is_directory=True)

    assert store.read(digest, access=READ) == b"good"
    written = store.store(b"written after the swap").digest
    assert store.read(written, access=READ) == b"written after the swap"
    assert list(stand_in.rglob("*")) == []


def test_a_symlinked_shard_or_object_is_refused(tmp_path):
    store, digest, path = _store(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / path.name).write_bytes(b"good")
    shard = path.parent
    os.replace(shard, tmp_path / "moved-shard")
    shard.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(CasError, match="not trustworthy"):
        store.read(digest, access=READ)


class _Layout:
    def model_dump(self) -> dict[str, str]:
        return {"nested": "a/b"}


def test_a_swapped_ancestor_of_a_warm_storage_path_is_refused(tmp_path):
    root = tmp_path / "instance"
    (root / "a" / "b").mkdir(parents=True)
    layout = _Layout()
    assert PlaybillInstance._validated_paths(root, layout)["nested"] == (root / "a" / "b")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.replace(root / "a", outside / "a")
    (root / "a").symlink_to(outside / "a", target_is_directory=True)
    with pytest.raises(FormatError, match="escapes"):
        PlaybillInstance._validated_paths(root, layout)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_child_forked_while_a_reader_is_busy_reads_without_deadlock(tmp_path):
    import threading

    ledger = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format="sha1",
        signing_key_path=tmp_path / "unused-key",
        allowed_signers_path=tmp_path / "unused-signers",
    )
    oid = ledger._git(["hash-object", "-w", "--stdin"], input_bytes=b"blob").decode().strip()
    assert ledger.read_blobs([oid]) == {oid: b"blob"}
    reader = ledger_git._batch_reader(ledger.path)
    # Another thread holds the reader while this one forks.
    held, release = threading.Event(), threading.Event()

    def busy() -> None:
        with reader._lock:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=busy)
    worker.start()
    held.wait(5)
    threading.Timer(0.2, release.set).start()
    pid = os.fork()  # waits for the busy reader, then forks
    if pid == 0:  # pragma: no cover - exercised in the child
        try:
            ok = ledger.read_blobs([oid]) == {oid: b"blob"}
        except BaseException:
            ok = False
        os._exit(0 if ok else 1)
    worker.join(5)
    deadline_status = None
    for _ in range(100):
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            deadline_status = status
            break
        threading.Event().wait(0.05)
    if deadline_status is None:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        pytest.fail("the forked child deadlocked on an inherited reader")
    assert os.waitstatus_to_exitcode(deadline_status) == 0
    # The parent's reader still answers after the fork.
    assert ledger.read_blobs([oid]) == {oid: b"blob"}


def test_availability_rehashes_what_a_remembered_proof_would_trust(tmp_path):
    store, digest, path = _store(tmp_path)
    assert store.availability(digest) == "present"
    path.chmod(0o600)
    with path.open("r+b") as handle:
        handle.write(b"rot!")
    # Rot that kept the file's identity: the remembered proof still vouches.
    with cas._VERIFIED_LOCK:
        cas._VERIFIED[store._memo_key(digest)] = cas._file_identity(path.stat())
    assert store.verify(digest) is True
    assert store.availability(digest) == "corrupt"
    path.unlink()
    assert store.availability(digest) == "missing"


def test_availability_refuses_a_fifo_without_waiting_for_a_writer(tmp_path):
    store, digest, path = _store(tmp_path)
    path.unlink()
    os.mkfifo(path)
    # Opening a FIFO for reading blocks until a writer appears; this must not.
    assert store.availability(digest) == "corrupt"


def test_scan_streams_a_bounded_prefix_including_a_dry_runs_held_bodies(
    tmp_path: Path,
) -> None:
    from cruxible_core.storage.cas import DryRunBodyStore

    root = tmp_path / "cas"
    root.mkdir()
    store = ContentAddressedBodyStore(root)
    stored = sorted(store.store(f"body {index}".encode()).digest for index in range(40))
    whole = store.scan("", budget=1000)
    assert whole.complete and whole.digests == tuple(stored) and whole.examined == 40
    first = stored[0].removeprefix("sha256:")
    one = store.scan(first[:12], budget=1000)
    assert one.complete and one.digests == (stored[0],)
    partial = store.scan("", budget=10)
    assert not partial.complete and partial.examined == 10 and len(partial.digests) <= 10
    absent = first[:2] + ("0" if first[2] != "0" else "1") * 10
    near = store.scan(absent, budget=1000, nearest=3)
    assert near.complete and near.digests == () and len(near.nearest) <= 3
    with pytest.raises(CasError):
        store.scan("A", budget=10)
    assert store.peek(stored[0], 4) == b"body"
    held: dict[str, bytes] = {}
    dry = DryRunBodyStore(store, held)
    extra = dry.store(b"held only").digest
    assert extra in dry.scan("", budget=1000).digests
    assert extra not in store.scan("", budget=1000).digests
    assert dry.peek(extra, 4) == b"held"


def test_only_evidence_bodies_and_erasure_are_logged_as_landings(tmp_path):
    """Journal-record payloads (run records, dispatch coverage, settlements) never
    move the verdict input fingerprint; an evidence body's arrival and any erase do,
    each logged before its object moves and again after."""

    store, digest, _path = _store(tmp_path)
    head = store.landings.head()
    assert head.identity and head.sequence == 2
    assert [(row.digest, row.kind) for row in store.landings.after(0)] == [
        (digest, "store"),
        (digest, "stored"),
    ]
    store.store(b"run record payload", journal_payload=True)
    assert store.landings.head() == head
    store.store(b"good")  # already present: nothing arrived
    assert store.landings.head() == head
    arrived = store.store(b"a new capture body").digest
    assert [(row.digest, row.kind) for row in store.landings.after(head.sequence)] == [
        (arrived, "store"),
        (arrived, "stored"),
    ]
    after_arrival = store.landings.head()
    assert store.erase(digest)
    assert [(row.digest, row.kind) for row in store.landings.after(after_arrival.sequence)] == [
        (digest, "erase"),
        (digest, "erased"),
    ]
    assert store.landings.head().identity == head.identity
    # A cursor reads exactly the range it asks for.
    assert [row.sequence for row in store.landings.after(1, through=3)] == [2, 3]


def test_a_body_damaged_in_place_is_recorded_once_and_cleared_by_its_repair(tmp_path):
    """A body damaged out of band moves no operation: the verification that finds it
    records the damage once, presence reads absent from that record on and present
    before it, and the body found whole again is recorded present."""

    store, digest, path = _store(tmp_path)
    landed = store.landings.head()
    os.chmod(path, 0o600)
    path.write_bytes(b"evil")
    # Nothing records an in-place rewrite until a verification hashes it.
    assert store.landings.head() == landed
    assert store.availability(digest) == "corrupt"
    damaged = store.landings.head()
    assert [(row.digest, row.kind) for row in store.landings.after(landed.sequence)] == [
        (digest, "corrupt")
    ]
    assert not store.landings.present(digest, identity=damaged.identity, sequence=damaged.sequence)
    assert store.landings.present(digest, identity=landed.identity, sequence=landed.sequence)
    # Found damaged again, by a read or deleted as well, it is not recorded again.
    with pytest.raises(CasError):
        store.read(digest, access=READ)
    path.unlink()
    assert store.availability(digest) == "missing"
    assert store.landings.head() == damaged
    # Repaired in place: the next verification records it present.
    path.write_bytes(b"good")
    assert store.availability(digest) == "present"
    assert [(row.digest, row.kind) for row in store.landings.after(damaged.sequence)] == [
        (digest, "stored")
    ]
    repaired = store.landings.head()
    assert store.landings.present(digest, identity=repaired.identity, sequence=repaired.sequence)
    assert store.availability(digest) == "present"
    assert store.landings.head() == repaired


def test_a_read_that_finds_a_landed_body_gone_records_it_and_a_store_restores_it(tmp_path):
    store, digest, path = _store(tmp_path)
    payload = store.store(b"run record payload", journal_payload=True).digest
    erased = store.store(b"erased body").digest
    assert store.erase(erased)
    landed = store.landings.head()
    # Bodies the log never had present, or erased, are no damage.
    store._path(payload).unlink()
    assert store.availability(payload) == "missing"
    assert store.availability(erased) == "missing"
    assert store.landings.head() == landed
    path.unlink()
    with pytest.raises(CasError):
        store.read(digest, access=READ)
    assert [(row.digest, row.kind) for row in store.landings.after(landed.sequence)] == [
        (digest, "missing")
    ]
    damaged = store.landings.head()
    assert not store.landings.present(digest, identity=damaged.identity, sequence=damaged.sequence)
    store.store(b"good")
    restored = store.landings.head()
    assert [row.kind for row in store.landings.after(damaged.sequence)] == ["store", "stored"]
    assert store.landings.present(digest, identity=restored.identity, sequence=restored.sequence)


def _present_at(store, digest, head) -> bool:  # type: ignore[no-untyped-def]
    return store.landings.present(digest, identity=head.identity, sequence=head.sequence)


def test_a_verify_that_finds_a_landed_body_gone_records_it_once(tmp_path):
    store, digest, path = _store(tmp_path)
    landed = store.landings.head()
    path.unlink()
    assert store.verify(digest) is False
    assert [(row.digest, row.kind) for row in store.landings.after(landed.sequence)] == [
        (digest, "missing")
    ]
    damaged = store.landings.head()
    assert not _present_at(store, digest, damaged)
    # A read positioned before the record still answers present.
    assert _present_at(store, digest, landed)
    assert store.verify(digest) is False
    assert store.landings.head() == damaged


def _damaged_then_restored(tmp_path):  # type: ignore[no-untyped-def]
    store, digest, path = _store(tmp_path)
    landed = store.landings.head()
    os.chmod(path, 0o600)
    path.write_bytes(b"evil")
    with pytest.raises(CasError):
        store.read(digest, access=READ)
    damaged = store.landings.head()
    assert [row.kind for row in store.landings.after(landed.sequence)] == ["corrupt"]
    path.write_bytes(b"good")
    return store, digest, landed, damaged


@pytest.mark.parametrize("finding", ("verify", "read"))
def test_a_verification_that_finds_a_damaged_body_whole_records_its_repair_once(
    tmp_path, finding: str
):
    store, digest, landed, damaged = _damaged_then_restored(tmp_path)
    if finding == "verify":
        assert store.verify(digest) is True
    else:
        assert store.read(digest, access=READ) == b"good"
    assert [(row.digest, row.kind) for row in store.landings.after(damaged.sequence)] == [
        (digest, "stored")
    ]
    repaired = store.landings.head()
    assert _present_at(store, digest, repaired)
    # Positions before the repair keep their answers.
    assert not _present_at(store, digest, damaged)
    assert _present_at(store, digest, landed)
    # Verified whole again, warm or cold, it is not recorded again.
    assert store.verify(digest) is True
    assert store.read(digest, access=READ) == b"good"
    assert store.landings.head() == repaired


def test_a_landing_logged_before_its_object_is_logged_again_once_it_is_final(tmp_path, monkeypatch):
    """A reader that consumes the first row while the write is in flight sees the
    digest again after the object lands, so nothing it derived then stays."""

    store, _digest, _path = _store(tmp_path)
    content = b"in flight"
    seen: list[tuple[str, bool]] = []
    original = LandingLog._append

    def append(self, kind, digest, *, sync):  # type: ignore[no-untyped-def]
        sequence = original(self, kind, digest, sync=sync)
        seen.append((kind, store.verify(digest)))
        return sequence

    monkeypatch.setattr(LandingLog, "_append", append)
    store.store(content)
    assert seen == [("store", False), ("stored", True)]


def test_a_missing_landing_log_reads_as_empty_and_is_not_created(tmp_path):
    root = tmp_path / "managed" / "cas"
    root.mkdir(parents=True)
    store = ContentAddressedBodyStore(root)
    assert (store.landings.head().identity, store.landings.head().sequence) == ("", 0)
    assert store.landings.after(0) == ()
    assert not (store._algorithm_root / LOG_NAME).exists()
    assert not (store._algorithm_root / PENDING_NAME).exists()


def test_presence_is_the_last_completion_at_or_before_a_position(tmp_path):
    """A read decides a body's presence from the log alone: its last completion at or
    before the read's position. An intent whose completion comes later changes nothing."""

    store, digest, path = _store(tmp_path)
    identity = store.landings.head().identity

    def present(sequence: int, which: str = digest) -> bool:
        return store.landings.present(which, identity=identity, sequence=sequence)

    assert store.erase(digest)
    store.store(b"good")
    assert [(row.sequence, row.kind) for row in store.landings.after(0)] == [
        (1, "store"),
        (2, "stored"),
        (3, "erase"),
        (4, "erased"),
        (5, "store"),
        (6, "stored"),
    ]
    assert [present(sequence) for sequence in range(7)] == [
        False,
        False,
        True,
        True,
        False,
        False,
        True,
    ]
    # The file is never asked: removed outside the store, the log still says present.
    path.unlink()
    assert present(6) and not present(6, "sha256:" + "0" * 64)
    with pytest.raises(CasError, match="replaced"):
        store.landings.present(digest, identity="landings_other", sequence=6)


def test_a_log_minted_over_held_objects_names_each_as_stored(tmp_path):
    """A store that already holds objects when its log begins (a store from before the
    log, or bodies first stored as journal payloads) records each as present first."""

    root = tmp_path / "managed" / "cas"
    root.mkdir(parents=True)
    store = ContentAddressedBodyStore(root)
    held = store.store(b"stored before any landing", journal_payload=True).digest
    assert store.landings.head().identity == ""
    reopened = ContentAddressedBodyStore(root)
    head = reopened.landings.head()
    assert [(row.digest, row.kind) for row in reopened.landings.after(0)] == [(held, "stored")]
    assert reopened.landings.present(held, identity=head.identity, sequence=head.sequence)


def test_an_interrupted_seed_publishes_nothing_and_the_next_open_seeds_again(tmp_path, monkeypatch):
    """A seed that stops part way (its header and one body written) is never the log:
    the next open seeds every pre-existing body, and a later open keeps that head."""

    root = tmp_path / "managed" / "cas"
    root.mkdir(parents=True)
    store = ContentAddressedBodyStore(root)
    held = [
        store.store(value, journal_payload=True).digest
        for value in (b"pre-log body A", b"pre-log body B", b"pre-log body C")
    ]
    assert store.landings.head().identity == ""
    real_write = landing_log.os.write
    writes: list[int] = []

    def crash(descriptor, data):
        if not writes:
            writes.append(1)
            return real_write(descriptor, bytes(data[: landing_log.RECORD * 2]))
        raise OSError("simulated process failure during seed")

    monkeypatch.setattr(landing_log.os, "write", crash)
    with pytest.raises(OSError, match="during seed"):
        ContentAddressedBodyStore(root)
    monkeypatch.setattr(landing_log.os, "write", real_write)
    assert writes
    assert store.landings.head().identity == ""  # the partial seed was never published

    recovered = ContentAddressedBodyStore(root)
    head = recovered.landings.head()
    assert head.identity and head.sequence == len(held)
    assert all(
        recovered.landings.present(digest, identity=head.identity, sequence=head.sequence)
        for digest in held
    )
    assert ContentAddressedBodyStore(root).landings.head() == head


def test_a_journal_payload_stored_again_as_evidence_is_logged_present(tmp_path):
    store, _digest, _path = _store(tmp_path)
    payload = store.store(b"a record payload, later cited", journal_payload=True).digest
    head = store.landings.head()
    assert not store.landings.present(payload, identity=head.identity, sequence=head.sequence)
    store.store(b"a record payload, later cited")
    assert _kinds(store, head.sequence) == [(payload, "store"), (payload, "stored")]
    head = store.landings.head()
    assert store.landings.present(payload, identity=head.identity, sequence=head.sequence)
    # Present already: storing it again logs nothing.
    store.store(b"a record payload, later cited")
    assert store.landings.head() == head


def _kinds(store, after: int = 0) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    return [(row.digest, row.kind) for row in store.landings.after(after)]


@pytest.mark.parametrize("failing", ("store", "stored"))
def test_a_store_interrupted_at_either_row_is_settled_by_its_retry(tmp_path, monkeypatch, failing):
    """However a landing is interrupted, its retry publishes the object's final state."""

    store, _digest, _path = _store(tmp_path)
    head = store.landings.head().sequence
    original = LandingLog._append
    content = b"interrupted body"
    digest = store.digest_bytes(content).tagged

    def append(self, kind, digest, *, sync):  # type: ignore[no-untyped-def]
        if kind == failing:
            raise CasError("the landing row could not be written")
        return original(self, kind, digest, sync=sync)

    with monkeypatch.context() as patched:
        patched.setattr(LandingLog, "_append", append)
        with pytest.raises(CasError, match="landing row"):
            store.store(content)
    assert store.landings.unfinished()
    # A failed intent never wrote the object; a failed completion did.
    assert store.verify(digest) is (failing == "stored")
    store.store(content)
    assert not store.landings.unfinished()
    rows = _kinds(store, head)
    assert rows[-1] == (digest, "stored")
    if failing == "stored":
        assert rows == [(digest, "store"), (digest, "stored")]


def test_a_store_whose_object_write_fails_publishes_its_absence(tmp_path, monkeypatch):
    store, _digest, _path = _store(tmp_path)
    head = store.landings.head().sequence
    content = b"never written"
    digest = store.digest_bytes(content).tagged

    def failed(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise CasError("CAS body could not be stored durably")

    monkeypatch.setattr(store, "_publish", failed)
    with pytest.raises(CasError, match="durably"):
        store.store(content)
    assert not store.landings.unfinished()
    assert _kinds(store, head) == [(digest, "store"), (digest, "erased")]


def test_an_erase_interrupted_before_its_completion_is_settled_by_retry_or_reopen(
    tmp_path, monkeypatch
):
    store, digest, _path = _store(tmp_path)
    head = store.landings.head().sequence
    original = LandingLog._append

    def append(self, kind, digest, *, sync):  # type: ignore[no-untyped-def]
        if kind == "erased":
            raise CasError("the landing row could not be written")
        return original(self, kind, digest, sync=sync)

    with monkeypatch.context() as patched:
        patched.setattr(LandingLog, "_append", append)
        with pytest.raises(CasError, match="landing row"):
            store.erase(digest)
    assert store.landings.unfinished() and not store.verify(digest)
    # Reopening the store settles what any writer left unfinished.
    reopened = ContentAddressedBodyStore(store.root)
    assert not reopened.landings.unfinished()
    assert _kinds(reopened, head) == [(digest, "erase"), (digest, "erased")]
    # A retry of the same erase is idempotent and publishes nothing more.
    assert reopened.erase(digest) is False
    assert _kinds(reopened, head) == [(digest, "erase"), (digest, "erased")]


def test_a_live_writers_landing_is_not_settled_under_it(tmp_path):
    """Reconciliation skips a marker another live writer holds: that writer publishes."""

    store, _digest, _path = _store(tmp_path)
    content = b"slow writer"
    digest = store.digest_bytes(content).tagged
    with store.landings.operation(digest, "store"):
        other = ContentAddressedBodyStore(store.root)
        assert other.landings.reconcile() == 0
        assert store.landings.unfinished()
    assert not store.landings.unfinished()


@pytest.mark.parametrize("level", ["algorithm", "cas", "managed"])
def test_a_retained_landing_log_follows_the_held_directory_through_a_swap(tmp_path, level):
    """The log is reached through the held directory, never a pathname: a swapped
    ancestor redirects neither its writes nor its reads."""

    store, digest, _path = _store(tmp_path)
    log = store.landings
    head = log.head()
    ancestor = {
        "algorithm": store._algorithm_root,
        "cas": store._algorithm_root.parent,
        "managed": store._algorithm_root.parent.parent,
    }[level]
    outside = tmp_path / "outside"
    outside.mkdir()
    os.replace(ancestor, outside / "moved")
    stand_in = outside / "stand-in"
    stand_in.mkdir()
    ancestor.symlink_to(stand_in, target_is_directory=True)

    with log.operation(digest, "erase"):
        pass
    assert log.head().identity == head.identity
    assert log.head().sequence == head.sequence + 2
    assert [row.kind for row in log.after(head.sequence)] == ["erase", "stored"]
    assert log.reconcile() == 0 and not log.unfinished()
    assert list(stand_in.rglob("*")) == []
