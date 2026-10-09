"""A store written by the full-map code opens, appends deltas, and keeps every head.

``tests/fixtures/claim_attestation_store_v1`` was written by the code before the
head-map delta (base 9c5943085): four appends over three Claims, full head-map
nodes, and its mutable accelerator file. The format change preserves every
published-root digest, so heads recorded in readings and continuation tokens
keep resolving; the only migration step is collecting the accelerator files.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.claim_attestation_store import (
    ClaimAttestationHeadMapDelta,
    ClaimAttestationHeadMapNode,
)
from cruxible_core.evidence.claim_attestation_store import ClaimAttestationEvidenceStore
from tests.test_claims.test_claim_attestation_store import NOW, _account, _attestation

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "claim_attestation_store_v1"
# Published heads the old code returned, in append order.
LEGACY_HEADS = (
    "sha256:1ec98478f6c69cc9a9f6643b0b72be370c5df0471fb4f2a26800b3f531eaa3cb",
    "sha256:04eb6f4d0c3aa68588ba7f76b7dce83f45cfe8595b85393d062aa3a3197d8698",
    "sha256:aaed5cfb28580d0f797ea032394da7290a924c967a3a5d6569fd7605c3515982",
    "sha256:4e60505b5f8ee60e0fba023c2592f82a17e3a820592eca130702c24ba2434d0a",
)


def _legacy(tmp_path: Path) -> ClaimAttestationEvidenceStore:
    shutil.copytree(FIXTURE / "exhaust", tmp_path / "exhaust")
    return ClaimAttestationEvidenceStore(tmp_path / "exhaust", instance_id="inst_test")


def _maps(store: ClaimAttestationEvidenceStore) -> list[object]:
    chain = store._validated_chain(store._load_pointer().root_digest)
    return [store._load_map(root.partition_map_digest) for root in chain.roots]


def test_a_legacy_store_opens_with_every_head_and_frontier_intact(tmp_path: Path) -> None:
    store = _legacy(tmp_path)
    assert (store.root / "accelerators" / "current.json").is_file()

    assert store.head() == LEGACY_HEADS[-1]
    for count, head in enumerate(LEGACY_HEADS, start=1):
        assert len(store.events(at_head=head)) == count
    assert len(store.fold_events()) == 4
    assert len(store.fold_events(at_head=LEGACY_HEADS[1])) == 2
    # The first Claim's partition holds two of the four events.
    first = store.events()[0][0].partition_digest
    assert [pair[0].sequence for pair in store.events(partition_digest=first)] == [1, 2]
    assert len(store.events(at_head=LEGACY_HEADS[0], partition_digest=first)) == 1
    # Migration: the accelerator cache is collected; nothing else is rewritten.
    assert not (store.root / "accelerators").exists()
    assert all(isinstance(node, ClaimAttestationHeadMapNode) for node in _maps(store))


def test_appends_after_migration_write_deltas_and_old_heads_still_resolve(
    tmp_path: Path,
) -> None:
    store = _legacy(tmp_path)
    duplicate = _attestation()  # the fixture's first append, byte for byte
    receipt = store.append(
        attestation=duplicate, verification_account=_account(duplicate), note=None
    )
    assert receipt.recorded_head == LEGACY_HEADS[0]
    assert receipt.current_head == LEGACY_HEADS[-1]

    fresh = _attestation(stance="unsure", attested_at=NOW + timedelta(seconds=10))
    appended = store.append(attestation=fresh, verification_account=_account(fresh), note=None)
    assert appended.partition_sequence == 3

    maps = _maps(store)
    assert all(isinstance(node, ClaimAttestationHeadMapNode) for node in maps[:5])
    assert isinstance(maps[5], ClaimAttestationHeadMapDelta)

    # A cold process validates the mixed chain: full maps, then the delta.
    reopened = ClaimAttestationEvidenceStore(tmp_path / "exhaust", instance_id="inst_test")
    assert reopened.head() == appended.current_head
    assert len(reopened.events()) == 5
    for count, head in enumerate(LEGACY_HEADS, start=1):
        assert len(reopened.events(at_head=head)) == count
    assert reopened.append(
        attestation=fresh, verification_account=_account(fresh), note=None
    ) == appended.model_copy(update={"current_head": appended.current_head})


def test_an_interrupted_migration_completes_on_the_next_open(tmp_path: Path) -> None:
    """Collecting the cache is idempotent: a crash part-way leaves files the next open takes."""

    store = _legacy(tmp_path)
    (store.root / "accelerators" / f"{'9' * 64}.json").write_bytes(b"legacy per-root cache\n")
    (store.root / "accelerators" / "current.json").unlink()  # crashed after one unlink
    assert store.head() == LEGACY_HEADS[-1]
    assert not (store.root / "accelerators").exists()
    # Opening an already migrated store changes nothing.
    again = ClaimAttestationEvidenceStore(tmp_path / "exhaust", instance_id="inst_test")
    assert again.head() == LEGACY_HEADS[-1]
    assert sorted(path.name for path in store.root.iterdir()) == [
        "manifest.json",
        "objects",
        "partitions",
        "published.json",
    ]


def test_a_delta_chained_to_another_map_is_refused(tmp_path: Path) -> None:
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.claim_attestation_store import (
        ClaimAttestationPublishedPointer,
    )
    from cruxible_core.evidence.claim_attestation_store import ClaimAttestationStoreError

    store = _legacy(tmp_path)
    fresh = _attestation(stance="unsure", attested_at=NOW + timedelta(seconds=10))
    store.append(attestation=fresh, verification_account=_account(fresh), note=None)
    tip = store._validated_chain(store._load_pointer().root_digest).tip
    delta = store._load_map(tip.partition_map_digest)
    assert isinstance(delta, ClaimAttestationHeadMapDelta)
    # The right head, chained to a map other than its predecessor's.
    genesis = store._validated_chain(tip.root_digest).roots[0]
    forged = store._head_map_delta(genesis.partition_map_digest, delta.head)
    store._write_object("map-node", forged.map_digest, forged)
    replaced = store._published_root(
        sequence=tip.sequence,
        previous=tip.previous_published_root_digest,
        event_digest=tip.event_digest,
        partition_map_digest=forged.map_digest,
    )
    store._object_path("published-root", tip.root_digest).unlink()
    store._write_object("published-root", replaced.root_digest, replaced)
    pointer = ClaimAttestationPublishedPointer(root_digest=replaced.root_digest)
    (store.root / "published.json").write_bytes(
        canonical_bytes(pointer.model_dump(mode="json")) + b"\n"
    )
    with pytest.raises(ClaimAttestationStoreError, match="map transition differs"):
        ClaimAttestationEvidenceStore(tmp_path / "exhaust", instance_id="inst_test").head()


@pytest.mark.parametrize("damage", ["delete_payload", "rewrite_account", "delete_event"])
def test_warm_reads_refuse_lost_or_rewritten_evidence_as_cold_reads_do(
    tmp_path: Path, damage: str
) -> None:
    """A long-lived store never serves an event whose backing objects are gone or changed."""

    from cruxible_core.evidence.claim_attestation_store import ClaimAttestationStoreError

    store = _legacy(tmp_path)
    (first, payload), *_rest = store.events()
    assert len(store.fold_events()) == 4  # every pair is now held warm
    target = {
        "delete_payload": store._object_path("payload", first.payload_digest),
        "rewrite_account": store._object_path(
            "verification-account", payload.verification_account_digest
        ),
        "delete_event": store._object_path("event", first.event_digest),
    }[damage]
    if damage.startswith("delete"):
        target.unlink()
    else:
        target.chmod(0o600)
        target.write_bytes(target.read_bytes().replace(b'"owner"', b'"other"', 1))
    cold = ClaimAttestationEvidenceStore(tmp_path / "exhaust", instance_id="inst_test")
    for reader in (store, cold):
        with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
            reader.events()
        with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
            reader.events(partition_digest=first.partition_digest)


@pytest.mark.parametrize("damage", ["delete", "rewrite_same_size"])
def test_an_account_changed_while_it_is_read_refuses_now_and_on_every_later_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """Damage between the account's authentication and its identity capture is caught."""

    from cruxible_core.evidence.claim_attestation_store import ClaimAttestationStoreError

    store = _legacy(tmp_path)
    store.head()
    store._event_pair_cache.clear()
    load = ClaimAttestationEvidenceStore._load_object
    damaged: list[Path] = []

    def load_then_damage(self, kind, digest, model):  # type: ignore[no-untyped-def]
        value = load(self, kind, digest, model)
        if kind == "verification-account" and not damaged:
            path = self._object_path(kind, digest)
            damaged.append(path)
            if damage == "delete":
                path.unlink()
            else:
                raw = path.read_bytes()
                path.chmod(0o600)
                path.write_bytes(raw.replace(b'"owner"', b'"ownex"', 1))
        return value

    with monkeypatch.context() as patch:
        patch.setattr(ClaimAttestationEvidenceStore, "_load_object", load_then_damage)
        with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
            store.events()
    assert damaged
    for _ in range(2):
        with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
            store.events()
    with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
        ClaimAttestationEvidenceStore(tmp_path / "exhaust", instance_id="inst_test").events()
