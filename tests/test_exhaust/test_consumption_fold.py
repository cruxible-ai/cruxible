"""Raw consumption receipts fold into counters after the retention window, then go."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.exhaust.consumption import (
    CONSUMPTION_EPOCH_PARTITION_ID,
    ConsumptionContextV1,
    build_consumption_receipt,
    consumption_aggregate,
    consumption_audit_touch_counts,
    fold_consumption_receipts,
    record_consumption,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._support import initialize_local

OLD = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
NEW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _consumption_receipts_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "on")


def _context(at: datetime, reader: str = "reader") -> ConsumptionContextV1:
    return ConsumptionContextV1(
        actor_context=GovernedActorContext(
            actor_type="service_account",
            actor_id=reader,
            org_id="org-test",
            operation_id="op-consume",
            timestamp=at,
        ),
        access_profile_id="cruxible.coverage.read",
    )


def _artifact(name: str) -> tuple[ArtifactIdentity, str]:
    return (
        ArtifactIdentity(kind="ClaimType", name=name),
        typed_digest(Sha256Value, "test-artifact-v1", {"name": name}).tagged,
    )


def _read(
    instance: PlaybillInstance,
    at: datetime,
    *names: str,
    reader: str = "reader",
    operation: str = "playbill.claim_type.get",
) -> None:
    record_consumption(
        instance,
        context=_context(at, reader),
        operation=operation,  # type: ignore[arg-type]
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        artifacts=tuple(_artifact(name) for name in names),
    )


def _partitions(instance: PlaybillInstance) -> set[str]:
    return {
        event.partition_id
        for event, _payload in instance.review_operational_store().events(family="consumption")
    }


def test_old_receipts_fold_into_counters_and_their_partitions_go(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    _read(instance, OLD, "status", "priority")
    _read(instance, OLD, "status", reader="other")
    # A receipt's identity carries no time: a newer read is another reader's.
    _read(instance, NEW, "status", reader="third")
    aggregate, audit = consumption_aggregate(instance), consumption_audit_touch_counts(instance)
    raw_before = {item for item in _partitions(instance) if item.startswith("event:")}
    assert len(raw_before) == 4

    result = fold_consumption_receipts(instance, now=NOW, days=14)

    assert (result.folded, result.removed_partitions) == (3, 3)
    assert consumption_aggregate(instance) == aggregate
    assert consumption_audit_touch_counts(instance) == audit
    partitions = _partitions(instance)
    assert len({item for item in partitions if item.startswith("event:")}) == 1
    assert "folds" in partitions
    # Nothing more is due: a second pass changes nothing.
    assert fold_consumption_receipts(instance, now=NOW, days=14) == type(result)()


def test_a_read_served_again_after_its_receipt_folded_is_not_counted_twice(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    _read(instance, OLD, "status")
    fold_consumption_receipts(instance, now=NOW, days=14)
    aggregate, audit = consumption_aggregate(instance), consumption_audit_touch_counts(instance)
    # The same read again (an --at read of the folded coordinate) writes the same
    # receipt identity; another verb by the same reader is the same audit key.
    _read(instance, NOW, "status")
    _read(instance, NOW, "status", operation="playbill.search.match")
    assert consumption_audit_touch_counts(instance) == audit
    after = consumption_aggregate(instance).artifacts[0]
    assert after.total_touch_count == aggregate.artifacts[0].total_touch_count + 1
    # The repeated receipt is spent: the next pass removes it without counting it.
    result = fold_consumption_receipts(instance, now=NOW, days=14)
    assert (result.folded, result.removed_partitions) == (0, 1)
    assert consumption_aggregate(instance).artifacts[0] == after


def test_the_legacy_chained_receipts_fold_down_to_the_epoch(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    _read(instance, OLD, "status")  # starts the epoch
    store = instance.review_operational_store()
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    legacy = []
    for reader in ("legacy-1", "legacy-2"):
        identity, digest = _artifact("status")
        receipt = build_consumption_receipt(
            context=_context(OLD, reader),
            operation="playbill.claim_type.get",
            coordinate=coordinate,
            artifact_identity=identity,
            artifact_digest=digest,
        )
        legacy.append(
            store.append(
                family="consumption",
                partition_id=CONSUMPTION_EPOCH_PARTITION_ID,
                event_id=receipt.receipt_id,
                payload=receipt,
                coordinate=coordinate,
                generation=0,
                actor_context=_context(OLD, reader).actor_context,
                recorded_at=OLD,
            )
        )
    aggregate, audit = consumption_aggregate(instance), consumption_audit_touch_counts(instance)
    assert aggregate.artifacts[0].total_touch_count == 3

    result = fold_consumption_receipts(instance, now=NOW, days=14)

    assert (result.folded, result.removed_legacy_events) == (3, 2)
    payloads = store.partition_payloads(
        family="consumption", partition_id=CONSUMPTION_EPOCH_PARTITION_ID
    )
    assert [payload["tag"] for payload in payloads] == ["playbill-consumption-epoch-v1"]
    assert consumption_aggregate(instance) == aggregate
    assert consumption_audit_touch_counts(instance) == audit


def test_receipts_inside_the_window_are_kept(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    _read(instance, NOW - timedelta(days=13), "status")
    assert fold_consumption_receipts(instance, now=NOW, days=14).folded == 0
    assert fold_consumption_receipts(instance, now=NOW + timedelta(days=2), days=14).folded == 1
