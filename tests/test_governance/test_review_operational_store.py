"""Durability laws for the local review operational store."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.curation.review_operational import (
    PlaybillReviewOperationalEventV1,
    ReviewOperationalConcurrentChangeError,
    ReviewOperationalPartitionHeadV1,
    ReviewOperationalStore,
    ReviewOperationalStoreError,
    build_review_operational_head,
    review_operational_event_digest,
    review_operational_partition_genesis_digest,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from tests.core_support._support import initialize_local

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _actor(operation_id: str = "op-review-1") -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="service_account",
        actor_id="reader",
        org_id="org-test",
        operation_id=operation_id,
        timestamp=NOW,
    )


def _append(
    store: ReviewOperationalStore,
    coordinate: AcceptedCoordinate,
    *,
    event_id: str = "receipt-1",
    expected_latest_event_digest: str | None | object = None,
    compare: bool = False,
):  # type: ignore[no-untyped-def]
    compare_options = (
        {"expected_latest_event_digest": expected_latest_event_digest} if compare else {}
    )
    return store.append(
        family="consumption",
        partition_id="receipts",
        event_id=event_id,
        payload={
            "tag": "playbill-consumption-receipt-v1",
            "event_id": event_id,
            "artifact_identity": {"kind": "ClaimType", "name": "status"},
        },
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
        **compare_options,
    )


def test_operational_digest_preimages_are_frozen() -> None:
    coordinate = AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root="sha256:" + "b" * 64,
        generation_root="sha256:" + "c" * 64,
        compiler_digest="sha256:" + "d" * 64,
    )
    genesis = review_operational_partition_genesis_digest(
        instance_id="inst-test",
        family="consumption",
        partition_id="receipts",
    )
    draft = PlaybillReviewOperationalEventV1.model_construct(
        tag="playbill-review-operational-event-v1",
        instance_id="inst-test",
        family="consumption",
        partition_id="receipts",
        sequence=0,
        previous_event_digest=genesis,
        accepted_coordinate=coordinate,
        accepted_generation=7,
        actor_context=GovernedActorContext(
            actor_type="service_account",
            actor_id="reader",
            org_id="org",
            operation_id="op",
            timestamp=NOW,
        ),
        payload_digest="sha256:" + "e" * 64,
        recorded_at=NOW,
        event_digest="sha256:" + "0" * 64,
    )
    event_digest = review_operational_event_digest(draft)
    head = build_review_operational_head(
        initialized_coordinate=coordinate,
        initialized_generation=7,
        partitions=(
            ReviewOperationalPartitionHeadV1(
                family="consumption",
                partition_id="receipts",
                sequence=0,
                event_digest=event_digest,
            ),
        ),
    )

    assert genesis == "sha256:e1e2b14562eeb21dded78b0797653c29c93e34dcae2a78f89eccc8b7c4ceb74e"
    assert event_digest == "sha256:40ed8710eee1081072b50afa6abfd5a64a731230a17259e3a4ba389700acd312"
    assert (
        head.head_digest
        == "sha256:ef385ba039492d835b854b34922538725f044332cb7cad4c6f403cfee4c89c8d"
    )


def test_store_is_absent_until_first_append_and_head_reproduces(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    semantic_before = instance.accepted_coordinate().semantic_root
    generation_before = instance.accepted_coordinate().generation_root
    store = instance.review_operational_store()

    empty = store.head()
    assert empty.initialized is False
    assert empty.partitions == ()
    event = _append(store, coordinate)
    head = store.head()

    assert head.initialized is True
    assert head.initialized_coordinate == coordinate
    assert head.initialized_generation == 0
    assert head.partitions[0].event_digest == event.event_digest
    assert event.previous_event_digest == review_operational_partition_genesis_digest(
        instance_id=instance.descriptor.instance_id,
        family="consumption",
        partition_id="receipts",
    )
    assert event.event_digest == review_operational_event_digest(event)
    assert instance.accepted_coordinate().semantic_root == semantic_before
    assert instance.accepted_coordinate().generation_root == generation_before


def test_identical_event_is_idempotent_and_conflicting_identity_refuses(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()

    first = _append(store, coordinate)
    assert _append(store, coordinate) == first
    with pytest.raises(ReviewOperationalStoreError, match="conflicting payload bytes"):
        store.append(
            family="consumption",
            partition_id="receipts",
            event_id="receipt-1",
            payload={"event_id": "receipt-1", "changed": True},
            coordinate=coordinate,
            generation=0,
            actor_context=_actor(),
            recorded_at=NOW,
        )
    assert len(store.events(family="consumption")) == 1


def test_payload_synced_before_crash_is_reused_on_retry(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    exhaust = Path(instance.inspect().storage_directories["exhaust"])

    def crash(boundary: str) -> None:
        if boundary == "after_payload_sync":
            raise RuntimeError("crash")

    crashing = ReviewOperationalStore(
        exhaust,
        instance_id=instance.descriptor.instance_id,
        crash_hook=crash,
    )
    with pytest.raises(RuntimeError, match="crash"):
        _append(crashing, coordinate)

    recovered = ReviewOperationalStore(exhaust, instance_id=instance.descriptor.instance_id)
    event = _append(recovered, coordinate)
    assert event.sequence == 0
    assert len(recovered.events()) == 1


def test_store_manifest_synced_before_crash_is_published_on_retry(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    exhaust = Path(instance.inspect().storage_directories["exhaust"])

    def crash(boundary: str) -> None:
        if boundary == "after_store_manifest_sync":
            raise RuntimeError("crash")

    crashing = ReviewOperationalStore(
        exhaust,
        instance_id=instance.descriptor.instance_id,
        crash_hook=crash,
    )
    with pytest.raises(RuntimeError, match="crash"):
        _append(crashing, coordinate)

    recovered = ReviewOperationalStore(exhaust, instance_id=instance.descriptor.instance_id)
    assert _append(recovered, coordinate).sequence == 0
    assert recovered.head().initialized_coordinate == coordinate


def test_event_synced_before_crash_is_idempotent_on_retry(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    exhaust = Path(instance.inspect().storage_directories["exhaust"])

    def crash(boundary: str) -> None:
        if boundary == "after_event_sync":
            raise RuntimeError("crash")

    crashing = ReviewOperationalStore(
        exhaust,
        instance_id=instance.descriptor.instance_id,
        crash_hook=crash,
    )
    with pytest.raises(RuntimeError, match="crash"):
        _append(crashing, coordinate)

    recovered = ReviewOperationalStore(exhaust, instance_id=instance.descriptor.instance_id)
    assert _append(recovered, coordinate).sequence == 0
    assert len(recovered.events()) == 1


def test_compare_and_append_allows_one_concurrent_writer(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()
    first = _append(store, coordinate)

    def append(event_id: str):  # type: ignore[no-untyped-def]
        try:
            return _append(
                store,
                coordinate,
                event_id=event_id,
                expected_latest_event_digest=first.event_digest,
                compare=True,
            )
        except ReviewOperationalConcurrentChangeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(append, ("receipt-2", "receipt-3")))

    events = tuple(result for result in results if not isinstance(result, Exception))
    errors = tuple(result for result in results if isinstance(result, Exception))
    assert len(events) == 1
    assert len(errors) == 1
    assert "changed concurrently" in str(errors[0])
    assert errors[0].code == "cruxible.curation.concurrent_change"
    assert len(store.events()) == 2


def test_partition_identity_is_hashed_and_cannot_escape_root(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()
    store.append(
        family="curation",
        partition_id="../../outside",
        event_id="path-test",
        payload={"event_id": "path-test"},
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
    )

    root = Path(instance.inspect().storage_directories["exhaust"]) / "review-operational-v1"
    assert not (root / "outside").exists()
    assert len(tuple((root / "partitions" / "curation").iterdir())) == 1


def test_truncated_or_noncanonical_chain_refuses_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()
    event = _append(store, coordinate)
    root = Path(instance.inspect().storage_directories["exhaust"]) / "review-operational-v1"
    event_path = next(root.glob("partitions/consumption/*/events/*.json"))
    payload = json.loads(event_path.read_bytes())
    payload["accepted_generation"] = 1
    event_path.write_bytes(canonical_bytes(payload) + b"\n")

    with pytest.raises(ReviewOperationalStoreError, match="malformed"):
        store.head()
    assert event.event_digest != payload["event_digest"] or payload["accepted_generation"] == 1


def test_partition_symlink_refuses(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()
    _append(store, coordinate)
    root = Path(instance.inspect().storage_directories["exhaust"]) / "review-operational-v1"
    partition = next((root / "partitions" / "consumption").iterdir())
    moved = tmp_path / "moved-partition"
    partition.rename(moved)
    partition.symlink_to(moved, target_is_directory=True)

    with pytest.raises(ReviewOperationalStoreError, match="partition root is invalid"):
        store.events()


def test_lock_symlink_refuses_without_following_target(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    exhaust = Path(instance.inspect().storage_directories["exhaust"])
    target = tmp_path / "outside-lock-target"
    target.write_text("untouched", encoding="utf-8")
    (exhaust / ".review-operational-v1.lock").symlink_to(target)

    with pytest.raises(ReviewOperationalStoreError, match="lock path is not trustworthy"):
        instance.review_operational_store().head()
    assert target.read_text(encoding="utf-8") == "untouched"


def _curation_append(store: ReviewOperationalStore, coordinate, partition: str, event_id: str):  # type: ignore[no-untyped-def]
    return store.append(
        family="curation",
        partition_id=partition,
        event_id=event_id,
        payload={"event_id": event_id},
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
    )


def test_a_reader_rereads_only_the_sequences_written_since_it_last_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.curation.review_operational import reset_review_operational_replays

    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()
    for partition in ("one", "two", "three"):
        for index in range(4):
            _curation_append(store, coordinate, partition, f"{partition}-{index}")
    _append(store, coordinate)
    store.head()
    assert len(store.events()) == 13
    loaded: list[int] = []
    load_event = ReviewOperationalStore._load_event

    def counted(self, directory, family, partition_id, sequence, previous):  # type: ignore[no-untyped-def]
        loaded.append(sequence)
        return load_event(self, directory, family, partition_id, sequence, previous)

    monkeypatch.setattr(ReviewOperationalStore, "_load_event", counted)
    # Nothing moved: the head and the events come from the replay.
    head = store.head()
    assert store.head() is head
    assert len(store.events(family="curation")) == 12
    assert loaded == []
    # An append reads back only the partition it extends: its last verified
    # event again (proving the prefix), then the new sequence.
    _curation_append(store, coordinate, "two", "two-4")
    moved = store.head()
    assert loaded == [3, 4]
    assert moved != head
    assert "two-4" in {payload["event_id"] for _event, payload in store.events()}
    assert len(store.events()) == 14 and loaded == [3, 4]
    # A fresh process (no replay) agrees with the warm one.
    monkeypatch.undo()
    reset_review_operational_replays()
    assert store.head() == moved
    assert len(store.events()) == 14


def _observed(
    store: ReviewOperationalStore, coordinate, partition: str, event_id: str, generation: int
):  # type: ignore[no-untyped-def]
    return store.append(
        family="block_observation",
        partition_id=partition,
        event_id=event_id,
        payload={"event_id": event_id},
        coordinate=coordinate,
        generation=generation,
        actor_context=_actor(),
        recorded_at=NOW,
    )


def _windowed(store: ReviewOperationalStore, lower: int, upper: int) -> list[str]:
    return [
        str(payload["event_id"])
        for _event, payload in store.events_in_generation_window(
            family="block_observation", lower=lower, upper=upper
        )
    ]


def _filtered(store: ReviewOperationalStore, lower: int, upper: int) -> list[str]:
    return [
        str(payload["event_id"])
        for event, payload in store.events(family="block_observation")
        if lower <= event.accepted_generation <= upper
    ]


def test_a_generation_window_read_matches_the_filtered_family_and_tracks_every_change(
    tmp_path: Path,
) -> None:
    from cruxible_core.curation.review_operational import reset_review_operational_replays

    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()
    assert _windowed(store, 0, 100) == []
    for partition in ("b", "a"):
        for generation in range(6):
            _observed(store, coordinate, partition, f"{partition}-{generation}", generation)
    assert _windowed(store, 2, 3) == ["a-2", "a-3", "b-2", "b-3"] == _filtered(store, 2, 3)
    assert _windowed(store, 3, 2) == []
    assert _windowed(store, 0, 10**9) == _filtered(store, 0, 10**9)
    # An append, a cut back and a removed partition each move the window read.
    _observed(store, coordinate, "a", "a-late", 2)
    assert _windowed(store, 2, 2) == ["a-2", "a-late", "b-2"] == _filtered(store, 2, 2)
    store.truncate_partition(family="block_observation", partition_id="a", keep=1)
    assert _windowed(store, 0, 5) == ["a-0", "b-0", "b-1", "b-2", "b-3", "b-4", "b-5"]
    assert _windowed(store, 0, 5) == _filtered(store, 0, 5)
    store.remove_partitions(family="block_observation", partition_ids=("b",))
    assert _windowed(store, 0, 5) == ["a-0"] == _filtered(store, 0, 5)
    _observed(store, coordinate, "b", "b-new", 4)
    # A fresh process builds the index once and agrees with the warm one.
    warm = _windowed(store, 0, 5)
    reset_review_operational_replays()
    assert _windowed(store, 0, 5) == warm == ["a-0", "b-new"]


def test_a_generation_window_read_does_not_grow_with_history_outside_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()
    loaded: list[int] = []
    load_event = ReviewOperationalStore._load_event

    def counted(self, directory, family, partition_id, sequence, previous):  # type: ignore[no-untyped-def]
        loaded.append(sequence)
        return load_event(self, directory, family, partition_id, sequence, previous)

    monkeypatch.setattr(ReviewOperationalStore, "_load_event", counted)
    window_sizes: list[int] = []
    for round_index, old_count in enumerate((10, 100)):
        for index in range(old_count):
            _observed(store, coordinate, "block", f"old-{round_index}-{index}", 0)
        _observed(store, coordinate, "block", f"new-{round_index}", 50)
        _windowed(store, 41, 50)  # absorbs this round's appends once
        loaded.clear()
        window_sizes.append(len(_windowed(store, 41, 50)))
        # Nothing moved: the pass reads no event and returns only the window.
        assert loaded == []
    assert window_sizes == [1, 2]


def test_has_events_answers_from_the_index_without_loading_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.curation.review_operational import reset_review_operational_replays

    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()
    assert store.has_events(family="block_observation") is False
    _curation_append(store, coordinate, "one", "one-0")
    assert store.has_events(family="block_observation") is False
    for index in range(20):
        _observed(store, coordinate, "block", f"old-{index}", 0)
    assert store.has_events(family="block_observation") is True
    assert store.has_events(family="curation") is True
    loaded: list[int] = []
    load_event = ReviewOperationalStore._load_event

    def counted(self, directory, family, partition_id, sequence, previous):  # type: ignore[no-untyped-def]
        loaded.append(sequence)
        return load_event(self, directory, family, partition_id, sequence, previous)

    monkeypatch.setattr(ReviewOperationalStore, "_load_event", counted)
    # Nothing moved: the answer loads no event, however long the history.
    assert store.has_events(family="block_observation") is True
    assert store.has_events(family="curation") is True
    assert loaded == []
    store.remove_partitions(family="block_observation", partition_ids=("block",))
    assert store.has_events(family="block_observation") is False
    assert loaded == []
    monkeypatch.undo()
    reset_review_operational_replays()
    assert store.has_events(family="block_observation") is False
    assert store.has_events(family="curation") is True


def test_a_replaced_touch_log_makes_the_next_reader_list_the_store_again(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    store = instance.review_operational_store()
    _curation_append(store, coordinate, "one", "one-0")
    assert len(store.events()) == 1
    root = Path(instance.inspect().storage_directories["exhaust"]) / "review-operational-v1"
    # Another writer's partition whose touch the log no longer names.
    other = ReviewOperationalStore(
        Path(instance.inspect().storage_directories["exhaust"]),
        instance_id=instance.descriptor.instance_id,
    )
    _curation_append(other, coordinate, "two", "two-0")
    (root / "partitions.log").unlink()
    assert {payload["event_id"] for _event, payload in store.events()} == {"one-0", "two-0"}
