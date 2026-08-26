"""Durability laws for the local review operational store."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.review_operational import (
    ReviewOperationalStore,
    ReviewOperationalStoreError,
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


def _append(store: ReviewOperationalStore, coordinate: AcceptedCoordinate):  # type: ignore[no-untyped-def]
    return store.append(
        family="consumption",
        partition_id="receipts",
        event_id="receipt-1",
        payload={
            "tag": "playbill-consumption-receipt-v1",
            "event_id": "receipt-1",
            "artifact_identity": {"kind": "ClaimType", "name": "status"},
        },
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
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
