"""Durable-prefix, byte and concurrency parity for bounded consumption writes."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import CanonicalEncodingError, canonical_bytes
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.curation.review_operational import (
    REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT,
    ReviewOperationalStore,
    ReviewOperationalStoreError,
)
from cruxible_core.exhaust.consumption import (
    CONSUMPTION_EPOCH_PARTITION_ID,
    ConsumptionContextV1,
    ConsumptionEpochV1,
    build_consumption_receipt,
    consumption_aggregate,
    ensure_consumption_epoch,
    record_consumption,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from tests.core_support._support import initialize_local


@pytest.fixture(autouse=True)
def _consumption_receipts_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests exercise recorded receipts, which a local daemon leaves off.
    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "on")


NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)
COORDINATE = AcceptedCoordinate(
    git_oid="a" * 40,
    semantic_root="sha256:" + "b" * 64,
    generation_root="sha256:" + "c" * 64,
    compiler_digest="sha256:" + "d" * 64,
)
ACTOR = GovernedActorContext(
    actor_type="service_account",
    actor_id="reader",
    org_id="org-test",
    operation_id="batch-test",
    timestamp=NOW,
)


def _store(path: Path, **kwargs):  # type: ignore[no-untyped-def]
    path.mkdir(exist_ok=True)
    return ReviewOperationalStore(path, instance_id="batch-instance", **kwargs)


def _options():  # type: ignore[no-untyped-def]
    return dict(
        family="consumption",
        coordinate=COORDINATE,
        generation=0,
        actor_context=ACTOR,
        recorded_at=NOW,
    )


def _entry(name: str, **extra):  # type: ignore[no-untyped-def]
    return name, {"tag": "batch-test-v1", "event_id": name, **extra}


def _bytes(store: ReviewOperationalStore) -> dict[str, bytes]:
    return {
        str(path.relative_to(store.root)): path.read_bytes()
        for path in sorted(store.root.rglob("*"))
        if path.is_file()
    }


def _scalar(store, key, payload):  # type: ignore[no-untyped-def]
    return store.append(event_id=key, payload=payload, partition_id=f"event:{key}", **_options())


def test_batch_matches_singleton_bytes_and_returns_prior_and_in_batch_duplicates(tmp_path: Path):
    scalar = _store(tmp_path / "scalar")
    batch = _store(tmp_path / "batch")
    entries = (_entry("old"), _entry("new"), _entry("new"), _entry("third"))
    expected = tuple(_scalar(scalar, key, value) for key, value in entries)
    assert batch.append_batch(entries=entries, **_options()) == expected
    assert _bytes(batch) == _bytes(scalar)
    assert {event.sequence for event in expected} == {0}
    assert len({event.partition_id for event in expected}) == 3


def test_conflicting_later_item_preserves_exact_durable_prefix(tmp_path: Path):
    scalar = _store(tmp_path / "scalar")
    batch = _store(tmp_path / "batch")
    entries = (_entry("one"), _entry("two"), _entry("one", conflict=True), _entry("three"))
    with pytest.raises(ReviewOperationalStoreError, match="conflicting payload"):
        for key, payload in entries:
            _scalar(scalar, key, payload)
    with pytest.raises(ReviewOperationalStoreError, match="conflicting payload"):
        batch.append_batch(entries=entries, **_options())
    assert _bytes(batch) == _bytes(scalar)
    assert len(batch.events()) == 2


@pytest.mark.parametrize(
    "boundary", ["after_payload_sync", "after_event_sync", "after_partition_rename"]
)
def test_mid_batch_crash_reopens_and_retries_to_exact_scalar_bytes(tmp_path: Path, boundary: str):
    reference = _store(tmp_path / "reference")
    entries = tuple(_entry(str(n)) for n in range(4))
    expected = tuple(_scalar(reference, key, payload) for key, payload in entries)
    hits = 0

    def crash(observed: str) -> None:
        nonlocal hits
        if observed == boundary:
            hits += 1
            if hits == 2:
                raise RuntimeError("crash")

    failed = _store(tmp_path / "failed", crash_hook=crash)
    with pytest.raises(RuntimeError, match="crash"):
        failed.append_batch(entries=entries, **_options())
    # Unpublished staging is not an empty or malformed committed partition.
    assert len(_store(tmp_path / "failed").events()) == (
        2 if boundary == "after_partition_rename" else 1
    )
    recovered = _store(tmp_path / "failed")
    assert recovered.append_batch(entries=entries, **_options()) == expected
    assert _bytes(recovered) == _bytes(reference)


def test_two_store_objects_serialize_concurrent_duplicate_batches(tmp_path: Path):
    path = tmp_path / "shared"
    a, b = _store(path), _store(path)
    entries = tuple(_entry(str(n)) for n in range(12))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(lambda store: store.append_batch(entries=entries, **_options()), (a, b))
        )
    assert results[0] == results[1]
    assert len(a.events()) == len(entries)


@pytest.mark.parametrize("corruption", ["payload", "event", "gap", "symlink"])
def test_new_batch_rechecks_current_bytes_before_idempotent_return(tmp_path: Path, corruption: str):
    store = _store(tmp_path / "store")
    entries = (_entry("one"), _entry("two"))
    store.append_batch(entries=entries, **_options())
    events = sorted(store.root.glob("partitions/consumption/*/events/*.json"))
    if corruption == "payload":
        payload = next(store.root.glob("partitions/consumption/*/payloads/*.json"))
        body = json.loads(payload.read_bytes())
        body["tampered"] = True
        payload.write_bytes(canonical_bytes(body) + b"\n")
    elif corruption == "event":
        event = json.loads(events[0].read_bytes())
        event["accepted_generation"] += 1
        events[0].write_bytes(canonical_bytes(event) + b"\n")
    elif corruption == "gap":
        events[0].unlink()
    else:
        outside = tmp_path / "outside"
        events[0].rename(outside)
        events[0].symlink_to(outside)
    with pytest.raises(ReviewOperationalStoreError):
        store.append_batch(entries=entries, **_options())


def test_append_and_retry_never_read_unrelated_history(tmp_path: Path, monkeypatch):
    store = _store(tmp_path / "store")
    store.append_batch(entries=(_entry("old"),), **_options())
    original = ReviewOperationalStore._load_partition
    loads = []

    def counted(self, family, partition_id):
        result = original(self, family, partition_id)
        loads.append(len(result))
        return result

    monkeypatch.setattr(ReviewOperationalStore, "_load_partition", counted)
    entries = tuple(_entry(str(n)) for n in range(32))
    store.append_batch(entries=entries, **_options())
    assert loads == []

    store.append_batch(entries=entries, **_options())
    assert loads == [1] * len(entries)
    loads.clear()
    # Starting another independent event never verifies the growing history.
    store.append_batch(entries=(_entry("latest"),), **_options())
    assert loads == []


def test_unrelated_corruption_is_checked_by_readers_not_new_receipt_appends(tmp_path: Path):
    store = _store(tmp_path / "store")
    store.append_batch(entries=(_entry("old"),), **_options())
    payload = next(store.root.glob("partitions/consumption/*/payloads/*.json"))
    payload.write_bytes(b"broken")
    assert store.append_batch(entries=(_entry("new"),), **_options())[0].sequence == 0
    with pytest.raises(ReviewOperationalStoreError):
        store.events()
    with pytest.raises(ReviewOperationalStoreError):
        store.append_batch(entries=(_entry("old"),), **_options())


def test_independent_retry_compares_complete_payload_including_format_tag(tmp_path: Path):
    store = _store(tmp_path / "store")
    store.append_batch(entries=(_entry("one"),), **_options())
    with pytest.raises(ReviewOperationalStoreError, match="conflicting payload"):
        store.append_batch(
            entries=(("one", {"event_id": "one", "tag": "different-format"}),), **_options()
        )


@pytest.mark.parametrize(
    "boundary", ["after_payload_sync", "after_event_sync", "after_partition_rename"]
)
def test_epoch_initialization_is_atomic_and_retryable(tmp_path: Path, boundary: str):
    def crash(observed):
        if observed == boundary:
            raise RuntimeError("crash")

    store = _store(tmp_path / "store", crash_hook=crash)
    epoch = ConsumptionEpochV1(consumption_epoch_generation=0, accepted_coordinate=COORDINATE)
    options = dict(partition_id=CONSUMPTION_EPOCH_PARTITION_ID, payload=epoch, **_options())
    with pytest.raises(RuntimeError, match="crash"):
        store.ensure_first(**options)
    reopened = _store(tmp_path / "store")
    assert len(reopened.events()) == (1 if boundary == "after_partition_rename" else 0)
    event, payload = reopened.ensure_first(**options)
    assert payload == epoch.model_dump(mode="json")
    assert reopened.ensure_first(**options) == (event, payload)
    assert len(reopened.events()) == 1


def test_concurrent_epoch_initializers_keep_the_first_bound_coordinate(tmp_path: Path):
    stores = (_store(tmp_path / "shared"), _store(tmp_path / "shared"))

    def initialize(item):
        n, store = item
        return store.ensure_first(
            partition_id=CONSUMPTION_EPOCH_PARTITION_ID,
            payload=ConsumptionEpochV1(
                consumption_epoch_generation=n, accepted_coordinate=COORDINATE
            ),
            **_options(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(initialize, enumerate(stores)))
    assert results[0] == results[1]
    assert len(stores[0].events()) == 1


def test_receipts_and_epoch_ignore_history_growth_and_replay_old_sources(
    tmp_path: Path, monkeypatch
):
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    context = ConsumptionContextV1(actor_context=ACTOR, access_profile_id="read")
    store = instance.review_operational_store()
    artifact = (ArtifactIdentity(kind="ClaimType", name="kind"), "sha256:" + "a" * 64)
    old_receipt = build_consumption_receipt(
        context=context,
        operation="playbill.claim_type.get",
        coordinate=coordinate,
        artifact_identity=artifact[0],
        artifact_digest=artifact[1],
    )
    # Frozen chained history remains authoritative and byte-identical. Only
    # the first event is needed to recover its epoch on a hot write.
    epoch = ConsumptionEpochV1(consumption_epoch_generation=0, accepted_coordinate=coordinate)
    for payload in (epoch, old_receipt):
        store.append(
            family="consumption",
            partition_id=CONSUMPTION_EPOCH_PARTITION_ID,
            event_id=payload.event_id,
            payload=payload,
            coordinate=coordinate,
            generation=0,
            actor_context=ACTOR,
            recorded_at=NOW,
        )
    retained = _bytes(store)
    loads = []
    original = ReviewOperationalStore._load_event

    def counted(self, *args):
        value = original(self, *args)
        loads.append(value[0].partition_id)
        return value

    def forbid(*args, **kwargs):
        raise AssertionError("receipt append must not scan history")

    # Warm the shared history source before checking the served lookup path.
    with instance.accepted_history_reader():
        pass
    with monkeypatch.context() as patch:
        patch.setattr(instance, "accepted_history", forbid)
        patch.setattr(ReviewOperationalStore, "events", forbid)
        patch.setattr(ReviewOperationalStore, "_load_event", counted)
        args = dict(
            context=context,
            operation="playbill.claim_type.get",
            coordinate=coordinate,
            artifacts=(artifact,),
        )
        assert record_consumption(instance, **args) == (old_receipt,)
        assert loads == [CONSUMPTION_EPOCH_PARTITION_ID]
        loads.clear()
        assert record_consumption(instance, **args) == (old_receipt,)
        assert loads == [CONSUMPTION_EPOCH_PARTITION_ID, f"event:{old_receipt.receipt_id}"]
        loads.clear()
        assert (
            ensure_consumption_epoch(
                instance, coordinate=coordinate, generation=100, actor_context=ACTOR
            )
            == epoch
        )
        assert loads == [CONSUMPTION_EPOCH_PARTITION_ID]
    for name, raw in retained.items():
        assert (store.root / name).read_bytes() == raw
    assert consumption_aggregate(instance).artifacts[0].total_touch_count == 1


def test_lost_response_retries_in_a_fresh_process(tmp_path: Path):
    import subprocess
    import sys

    root = tmp_path / "store"
    child = """
import os, sys
from pathlib import Path
from tests.test_exhaust.test_consumption_batch import _store, _entry, _options
def crash(boundary):
    if boundary == "after_partition_rename":
        os._exit(73)
store = _store(Path(sys.argv[1]), crash_hook=crash if sys.argv[2] == "crash" else None)
result = store.append_batch(entries=(_entry("one"), _entry("two")), **_options())
assert len(result) == 2 and len(store.events()) == 2
"""
    crashed = subprocess.run([sys.executable, "-c", child, str(root), "crash"], timeout=30)
    assert crashed.returncode == 73
    before = _store(root).events()[0]
    subprocess.run([sys.executable, "-c", child, str(root), "retry"], check=True, timeout=30)
    after = _store(root).events()
    assert before in after and len(after) == 2


def test_empty_and_oversized_batches_do_not_initialize_store(tmp_path: Path):
    store = _store(tmp_path / "store")
    assert store.append_batch(entries=(), **_options()) == ()
    with pytest.raises(ValueError, match="item limit"):
        store.append_batch(
            entries=(_entry("one"),) * (REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT + 1),
            **_options(),
        )
    assert not store.root.exists()


def test_consumption_chunks_larger_reads_and_preserves_retry_and_ledger(
    tmp_path: Path, monkeypatch
):
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    context = ConsumptionContextV1(actor_context=ACTOR, access_profile_id="read")
    artifacts = tuple(
        (ArtifactIdentity(kind="ClaimType", name=f"kind-{n}"), "sha256:" + f"{n:064x}")
        for n in range(7)
    )
    # Exercise the same production chunk path without a large disk fixture.
    monkeypatch.setattr(
        "cruxible_core.exhaust.consumption.REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT", 3
    )
    batches = []
    original = ReviewOperationalStore.append_batch

    def tracked(self, **kwargs):
        batches.append(len(kwargs["entries"]))
        return original(self, **kwargs)

    monkeypatch.setattr(ReviewOperationalStore, "append_batch", tracked)
    args = dict(
        context=context,
        operation="playbill.claim_type.get",
        coordinate=coordinate,
        artifacts=artifacts + artifacts[:2],
    )
    receipts = record_consumption(instance, **args)
    assert len(receipts) == 7
    assert batches == [3, 3, 1]
    assert record_consumption(instance, **args) == receipts
    assert len(instance.review_operational_store().events(family="consumption")) == 8
    assert AcceptedCoordinate.from_internal(instance.accepted_coordinate()) == coordinate


def test_batch_prevalidation_error_does_not_write_an_earlier_valid_item(tmp_path: Path):
    store = _store(tmp_path / "store")
    with pytest.raises(CanonicalEncodingError):
        store.append_batch(
            entries=(_entry("valid"), _entry("invalid", value=object())),
            **_options(),
        )
    assert not store.root.exists()
    assert not store._lock_path.exists()


def test_caller_mutation_after_prevalidation_cannot_change_written_payload(tmp_path: Path):
    payload = {"event_id": "one", "nested": {"values": [1, 2]}}

    def mutate(boundary: str) -> None:
        if boundary == "after_store_manifest_sync":
            payload["event_id"] = "changed"
            payload["nested"]["values"].append(3)

    store = _store(tmp_path / "store", crash_hook=mutate)
    written = store.append_batch(entries=(("one", payload),), **_options())
    assert len(written) == 1
    assert store.events()[0][1] == {"event_id": "one", "nested": {"values": [1, 2]}}
    assert (
        store.append_batch(
            entries=(("one", {"event_id": "one", "nested": {"values": [1, 2]}}),),
            **_options(),
        )
        == written
    )
