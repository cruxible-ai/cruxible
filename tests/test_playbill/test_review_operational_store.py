"""Durability laws for the local review operational store."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.review_operational import (
    PlaybillReviewOperationalEventV1,
    ReviewOperationalConcurrentChangeError,
    ReviewOperationalPartitionHeadV1,
    ReviewOperationalStore,
    ReviewOperationalStoreError,
    build_review_operational_head,
    review_operational_event_digest,
    review_operational_partition_genesis_digest,
)
from tests.test_playbill._support import initialize_local

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
    assert errors[0].code == "playbill.curation.concurrent_change"
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
