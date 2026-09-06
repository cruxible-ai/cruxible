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
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.consumption import (
    ConsumptionContextV1,
    record_consumption,
)
from cruxible_core.playbill.review_operational import (
    REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT,
    ReviewOperationalStore,
    ReviewOperationalStoreError,
)
from tests.test_playbill._support import initialize_local

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
        partition_id="receipts",
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


def test_batch_matches_scalar_bytes_and_returns_prior_and_in_batch_duplicates(tmp_path: Path):
    scalar = _store(tmp_path / "scalar")
    batch = _store(tmp_path / "batch")
    first = _entry("old")
    for store in (scalar, batch):
        store.append(event_id=first[0], payload=first[1], **_options())
    # Include payload identities that intentionally differ from the append
    # lookup identity; existing append keys lookup from persisted payloads.
    entries = (
        first,
        _entry("new"),
        _entry("new"),
        ("lookup", {"event_id": "payload", "value": [1, 2]}),
        ("lookup", {"event_id": "payload", "value": [1, 2]}),
        ("payload", {"event_id": "payload", "value": [1, 2]}),
        ("no-id", {"value": 3}),
        ("no-id", {"value": 3}),
    )
    expected = tuple(
        scalar.append(event_id=key, payload=value, **_options()) for key, value in entries
    )
    assert batch.append_batch(entries=entries, **_options()) == expected
    assert _bytes(batch) == _bytes(scalar)
    # The ordinary idempotency check still precedes head-CAS checking.
    assert (
        batch.append(
            event_id="old", payload=first[1], expected_latest_event_digest=None, **_options()
        )
        == expected[0]
    )


def test_conflicting_later_item_preserves_exact_durable_prefix(tmp_path: Path):
    scalar = _store(tmp_path / "scalar")
    batch = _store(tmp_path / "batch")
    entries = (_entry("one"), _entry("two"), _entry("one", conflict=True), _entry("three"))
    with pytest.raises(ReviewOperationalStoreError, match="conflicting payload"):
        for key, payload in entries:
            scalar.append(event_id=key, payload=payload, **_options())
    with pytest.raises(ReviewOperationalStoreError, match="conflicting payload"):
        batch.append_batch(entries=entries, **_options())
    assert _bytes(batch) == _bytes(scalar)
    assert len(batch.events()) == 2


@pytest.mark.parametrize("boundary", ["after_payload_sync", "after_event_sync"])
def test_mid_batch_crash_reopens_and_retries_to_exact_scalar_bytes(tmp_path: Path, boundary: str):
    reference = _store(tmp_path / "reference")
    entries = tuple(_entry(str(n)) for n in range(4))
    expected = tuple(
        reference.append(event_id=key, payload=payload, **_options()) for key, payload in entries
    )
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


def test_one_partition_replay_per_bounded_batch_and_no_hidden_state(tmp_path: Path, monkeypatch):
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
    assert loads == [1]
    store.append_batch(entries=entries, **_options())
    assert loads == [1, 33]


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
        "cruxible_core.playbill.consumption.REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT", 3
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
