from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_attestation_store import (
    ClaimAttestationEvent,
    ClaimAttestationHeadMapEntry,
    ClaimAttestationPartitionHead,
    ClaimAttestationPublishedPointer,
    claim_attestation_partition_head_digest,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationStatement,
    VerifiedClaimAttestation,
    claim_attestation_v2_envelope_digest,
    claim_attestation_v2_statement_digest,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.evidence.claim_attestation_store import (
    ClaimAttestationEvidenceStore,
    ClaimAttestationStoreError,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
CAPTURE = "sha256:" + "c" * 64
COORDINATE = AcceptedCoordinate(
    git_oid="1" * 40,
    semantic_root=SHA_A,
    generation_root=SHA_B,
    compiler_digest="sha256:" + "d" * 64,
)


def _attestation(
    *,
    stance: str = "support",
    claim_id: str = "CLM-0123456789abcdef0123456789abcdef",
    attested_at: datetime = NOW,
) -> ClaimAttestation:
    statement = ClaimAttestationStatement(
        instance_id="inst_test",
        referent_coordinate=COORDINATE,
        claim_identity=ArtifactIdentity(kind="Claim", name=claim_id),
        claim_artifact_digest=SHA_A,
        claim_statement_digest=SHA_B,
        subject_shell_digest="sha256:" + "e" * 64,
        attesting_principal_id="owner",
        signing_key_digest="sha256:" + "f" * 64,
        attestation_basis="new_capture",
        stance=stance,  # type: ignore[arg-type]
        cited_capture_digests=(CAPTURE,),
        attested_at=attested_at,
    )
    return ClaimAttestation(statement=statement, signature="01" * 64)


def _account(attestation: ClaimAttestation) -> VerifiedClaimAttestation:
    return VerifiedClaimAttestation(
        statement_digest=claim_attestation_v2_statement_digest(attestation.statement),
        envelope_digest=claim_attestation_v2_envelope_digest(attestation),
        statement=attestation.statement,
        referent_coordinate=COORDINATE,
        append_coordinate=COORDINATE,
        attesting_principal_id="owner",
        submitted_by="owner",
        current_at_append=True,
        admitted_capture_digests=(CAPTURE,),
        recorded_at=NOW,
    )


def _store(tmp_path: Path, *, crash_hook=None) -> ClaimAttestationEvidenceStore:
    exhaust = tmp_path / "exhaust"
    exhaust.mkdir(parents=True, exist_ok=True)
    return ClaimAttestationEvidenceStore(
        exhaust,
        instance_id="inst_test",
        crash_hook=crash_hook,
    )


def test_append_duplicate_and_historical_prefix_are_byte_stable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _attestation()
    receipt = store.append(attestation=first, verification_account=_account(first), note="seen")
    assert (
        store.append(
            attestation=first,
            verification_account=_account(first),
            note="a retry cannot rewrite the note",
        )
        == receipt
    )

    second = _attestation(stance="contradict")
    second_receipt = store.append(
        attestation=second,
        verification_account=_account(second),
        note=None,
    )
    assert second_receipt.partition_sequence == 2
    assert len(store.events(at_head=receipt.recorded_head)) == 1
    assert len(store.events(at_head=second_receipt.current_head)) == 2
    assert (
        store.append(
            attestation=first,
            verification_account=_account(first),
            note=None,
        ).recorded_head
        == receipt.recorded_head
    )


@pytest.mark.parametrize(
    ("boundary", "published"),
    [
        ("after_step1", False),
        ("after_step2", True),
        ("after_step3", True),
        ("after_step4", True),
    ],
)
def test_crash_windows_are_inert_or_roll_forward(
    tmp_path: Path,
    boundary: str,
    published: bool,
) -> None:
    def crash(at: str) -> None:
        if at == boundary:
            raise RuntimeError(boundary)

    attestation = _attestation()
    crashing = _store(tmp_path, crash_hook=crash)
    with pytest.raises(RuntimeError, match=boundary):
        crashing.append(
            attestation=attestation,
            verification_account=_account(attestation),
            note=None,
        )

    reopened = _store(tmp_path)
    if published:
        assert len(reopened.events()) == 1
        duplicate = reopened.append(
            attestation=attestation,
            verification_account=_account(attestation),
            note=None,
        )
        assert duplicate.partition_sequence == 1
    else:
        assert reopened.events() == ()
        assert (
            reopened.append(
                attestation=attestation,
                verification_account=_account(attestation),
                note=None,
            ).partition_sequence
            == 1
        )


def test_post_chain_exception_rolls_forward_on_the_next_access(tmp_path: Path) -> None:
    def crash(at: str) -> None:
        if at == "after_step2":
            raise RuntimeError(at)

    attestation = _attestation()
    store = _store(tmp_path, crash_hook=crash)
    with pytest.raises(RuntimeError, match="after_step2"):
        store.append(
            attestation=attestation,
            verification_account=_account(attestation),
            note=None,
        )
    store.crash_hook = None
    # The same process re-verifies and publishes the durable event itself.
    assert len(store.events()) == 1
    retried = store.append(
        attestation=attestation,
        verification_account=_account(attestation),
        note=None,
    )
    assert retried.partition_sequence == 1
    assert len(store.events()) == 1


def test_unknown_head_and_corrupt_marker_refuse_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attestation = _attestation()
    receipt = store.append(
        attestation=attestation,
        verification_account=_account(attestation),
        note=None,
    )
    with pytest.raises(ClaimAttestationStoreError, match="attestation_head_unknown"):
        store.events(at_head="sha256:" + "9" * 64)

    marker = next((store.root / "partitions").glob("*/00000000000000000001.json"))
    marker.write_bytes(marker.read_bytes().replace(b'"sequence":1', b'"sequence":2'))
    # The same (warm) store refuses, exactly as a new one does.
    with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
        store.events(at_head=receipt.recorded_head)
    with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
        _store(tmp_path).events(at_head=receipt.recorded_head)


def test_accelerator_is_verified_rebuilt_and_selects_reducer_frontier(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _attestation(stance="support")
    first_receipt = store.append(
        attestation=first,
        verification_account=_account(first),
        note=None,
    )
    second = _attestation(stance="contradict")
    second_receipt = store.append(
        attestation=second,
        verification_account=_account(second),
        note=None,
    )

    # Every new-capture event remains outstanding even though the same principal
    # produced a later event; this is distinct from examined-existing latest.
    assert [item[0].event_digest for item in store.fold_events()] == [
        first_receipt.event_digest,
        second_receipt.event_digest,
    ]
    # The frontier is per process: a cold reader folds the same frontier.
    assert not (store.root / "accelerators").exists()
    assert [item[0].event_digest for item in _store(tmp_path).fold_events()] == [
        first_receipt.event_digest,
        second_receipt.event_digest,
    ]


def _write_pointer(store: ClaimAttestationEvidenceStore, root_digest: str) -> None:
    pointer = ClaimAttestationPublishedPointer(root_digest=root_digest)
    (store.root / "published.json").write_bytes(
        canonical_bytes(pointer.model_dump(mode="json")) + b"\n"
    )


def _current_root_and_map(store: ClaimAttestationEvidenceStore):  # type: ignore[no-untyped-def]
    """The tip root, its map object, and the full head map it stands for."""

    chain = store._validated_chain(store._load_pointer().root_digest)
    entries = tuple(
        ClaimAttestationHeadMapEntry(partition_digest=key, head=value)
        for key, value in chain.heads.items()
    )
    return chain.tip, store._load_map(chain.tip.partition_map_digest), entries


@pytest.mark.parametrize("mutation", ["unchanged_map", "duplicate_event"])
def test_transition_replay_refuses_repeated_event_with_unchanged_map_typed(
    tmp_path: Path,
    mutation: str,
) -> None:
    assert mutation in {"unchanged_map", "duplicate_event"}
    store = _store(tmp_path)
    attestation = _attestation()
    receipt = store.append(
        attestation=attestation,
        verification_account=_account(attestation),
        note=None,
    )
    current, node, _entries = _current_root_and_map(store)
    forged = store._published_root(
        sequence=current.sequence + 1,
        previous=current.root_digest,
        event_digest=receipt.event_digest,
        partition_map_digest=node.map_digest,
    )
    store._write_object("published-root", forged.root_digest, forged)
    _write_pointer(store, forged.root_digest)

    with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
        _store(tmp_path).events()


def test_transition_replay_refuses_unrelated_partition_map_change_typed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _attestation()
    store.append(attestation=first, verification_account=_account(first), note=None)
    current, _node, entries = _current_root_and_map(store)
    second = _attestation(claim_id="CLM-1123456789abcdef0123456789abcdef")

    def crash(at: str) -> None:
        if at == "after_step2":
            raise RuntimeError(at)

    store.crash_hook = crash
    with pytest.raises(RuntimeError, match="after_step2"):
        store.append(attestation=second, verification_account=_account(second), note=None)
    second_marker = next(
        path
        for path in (store.root / "partitions").glob("*/00000000000000000001.json")
        if store._load_marker(path).partition_digest != entries[0].partition_digest
    )
    second_event = store._load_marker(second_marker)
    second_head = store._partition_head(second_event)
    unrelated_partition = "sha256:" + "9" * 64
    unrelated_draft = ClaimAttestationPartitionHead.model_construct(
        tag="playbill-claim-attestation-partition-head-v1",
        partition_digest=unrelated_partition,
        sequence=1,
        event_digest=second_event.event_digest,
        head_digest="sha256:" + "0" * 64,
    )
    unrelated = ClaimAttestationPartitionHead(
        partition_digest=unrelated_partition,
        sequence=1,
        event_digest=second_event.event_digest,
        head_digest=claim_attestation_partition_head_digest(unrelated_draft),
    )
    forged_map = store._head_map(
        (
            *entries,
            ClaimAttestationHeadMapEntry(
                partition_digest=second_head.partition_digest,
                head=second_head,
            ),
            ClaimAttestationHeadMapEntry(
                partition_digest=unrelated.partition_digest,
                head=unrelated,
            ),
        )
    )
    store._write_object("map-node", forged_map.map_digest, forged_map)
    forged = store._published_root(
        sequence=current.sequence + 1,
        previous=current.root_digest,
        event_digest=second_event.event_digest,
        partition_map_digest=forged_map.map_digest,
    )
    store._write_object("published-root", forged.root_digest, forged)
    _write_pointer(store, forged.root_digest)

    with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
        _store(tmp_path).events()


def test_transition_replay_refuses_true_published_root_fork_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _attestation()
    first_receipt = store.append(
        attestation=first,
        verification_account=_account(first),
        note=None,
    )
    second = _attestation(
        claim_id="CLM-1123456789abcdef0123456789abcdef",
    )
    store.append(attestation=second, verification_account=_account(second), note=None)
    third = _attestation(attested_at=NOW + timedelta(seconds=1))
    third_receipt = store.append(
        attestation=third,
        verification_account=_account(third),
        note=None,
    )
    full_chain = store._validated_chain(store._load_pointer().root_digest)
    first_root = full_chain.roots[1]
    first_delta = store._load_map(first_root.partition_map_digest)
    third_event = store._load_object(
        "event",
        third_receipt.event_digest,
        ClaimAttestationEvent,
    )
    assert isinstance(third_event, ClaimAttestationEvent)
    first_heads = {first_delta.head.partition_digest: first_delta.head}
    first_heads[third_event.partition_digest] = store._partition_head(third_event)
    fork_map = store._head_map(
        tuple(
            ClaimAttestationHeadMapEntry(partition_digest=key, head=value)
            for key, value in first_heads.items()
        )
    )
    store._write_object("map-node", fork_map.map_digest, fork_map)
    fork = store._published_root(
        sequence=first_root.sequence + 1,
        previous=first_root.root_digest,
        event_digest=third_receipt.event_digest,
        partition_map_digest=fork_map.map_digest,
    )
    store._write_object("published-root", fork.root_digest, fork)
    _write_pointer(store, fork.root_digest)

    assert first_receipt.recorded_head == first_root.root_digest
    with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
        _store(tmp_path).head()


def test_unpublished_root_object_is_not_a_historical_read_head(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attestation = _attestation()
    receipt = store.append(
        attestation=attestation,
        verification_account=_account(attestation),
        note=None,
    )
    current, node, _entries = _current_root_and_map(store)
    unpublished = store._published_root(
        sequence=current.sequence + 1,
        previous=current.root_digest,
        event_digest=receipt.event_digest,
        partition_map_digest=node.map_digest,
    )
    store._write_object("published-root", unpublished.root_digest, unpublished)

    with pytest.raises(ClaimAttestationStoreError, match="attestation_head_unknown"):
        _store(tmp_path).events(at_head=unpublished.root_digest)


def test_idempotency_payload_mismatch_refuses_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attestation = _attestation()
    store.append(attestation=attestation, verification_account=_account(attestation), note=None)
    altered = attestation.model_copy(update={"signature": "02" * 64})

    with pytest.raises(ClaimAttestationStoreError, match="idempotency_payload_mismatch"):
        store.duplicate(attestation=altered)


def test_partition_tip_removes_per_append_full_scan_and_cold_fold_loads_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    partition_scans = 0
    original_partition_events = store._partition_events

    def counted_partition_events(partition_digest: str):  # type: ignore[no-untyped-def]
        nonlocal partition_scans
        partition_scans += 1
        return original_partition_events(partition_digest)

    monkeypatch.setattr(store, "_partition_events", counted_partition_events)
    map_sizes: list[int] = []
    for ordinal in range(300):
        attestation = _attestation(
            attested_at=NOW + timedelta(seconds=ordinal),
            # A new Claim every other append: the head map keeps growing.
            claim_id=f"CLM-{ordinal // 2:032x}",
        )
        store.append(
            attestation=attestation,
            verification_account=_account(attestation),
            note=None,
        )
        tip = store._validated_chain(store._load_pointer().root_digest).tip
        map_sizes.append(store._object_path("map-node", tip.partition_map_digest).stat().st_size)
    assert partition_scans == 0
    # Each append writes one head, however many Claims are attested.
    assert max(map_sizes) == min(map_sizes)
    assert not (store.root / "accelerators").exists()

    reopened = _store(tmp_path)
    payload_loads = 0
    original_load = reopened._load_object

    def counted_payload(kind, digest, model):  # type: ignore[no-untyped-def]
        nonlocal payload_loads
        payload_loads += kind == "payload"
        return original_load(kind, digest, model)

    monkeypatch.setattr(reopened, "_load_object", counted_payload)
    assert len(reopened.fold_events()) == 300
    assert payload_loads == 300
    assert len(reopened.fold_events()) == 300
    assert payload_loads == 300


def test_corrupt_pointer_with_two_maximal_roots_refuses_recovery_ambiguous(
    tmp_path: Path,
) -> None:
    left = _store(tmp_path / "left")
    first = _attestation()
    left.append(attestation=first, verification_account=_account(first), note=None)
    right = _store(tmp_path / "right")
    second = _attestation(claim_id="CLM-1123456789abcdef0123456789abcdef")
    right.append(attestation=second, verification_account=_account(second), note=None)

    for relative in ("objects", "partitions"):
        shutil.copytree(right.root / relative, left.root / relative, dirs_exist_ok=True)
    (left.root / "published.json").write_bytes(b"not canonical\n")

    with pytest.raises(ClaimAttestationStoreError, match="recovery_ambiguous"):
        _store(tmp_path / "left").head()


def test_events_after_a_root_reads_only_what_was_published_since(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    empty = store.head()
    # Before the store exists its empty root is a valid cursor with nothing after it.
    assert store.events_after(empty) == (empty, ())
    assert store.events_after(None) == (empty, ())
    receipts = []
    for ordinal in range(3):
        attestation = _attestation(
            claim_id=f"CLM-{ordinal:032x}", attested_at=NOW + timedelta(seconds=ordinal)
        )
        receipts.append(
            store.append(
                attestation=attestation, verification_account=_account(attestation), note=None
            )
        )
    head = store.head()
    assert head == receipts[-1].current_head
    everything = store.events()

    after_empty = store.events_after(empty)
    assert store.events_after(None) == after_empty
    assert after_empty[0] == head
    assert [(position, event, payload) for position, event, payload in after_empty[1]] == [
        (index + 1, event, payload) for index, (event, payload) in enumerate(everything)
    ]
    # A cursor at the first event's root reads the two published after it, and
    # loads none of the earlier objects to do so.
    loaded: list[str] = []
    load = ClaimAttestationEvidenceStore._load_object

    def counting(self, kind, digest, model):  # type: ignore[no-untyped-def]
        loaded.append(kind)
        return load(self, kind, digest, model)

    monkeypatch.setattr(ClaimAttestationEvidenceStore, "_load_object", counting)
    store._event_pair_cache.clear()
    after_first = store.events_after(receipts[0].recorded_head)
    assert [event.event_digest for _position, event, _payload in after_first[1]] == [
        receipts[1].event_digest,
        receipts[2].event_digest,
    ]
    assert [position for position, *_rest in after_first[1]] == [2, 3]
    assert loaded.count("event") == 2
    assert store.events_after(head) == (head, ())

    with pytest.raises(ClaimAttestationStoreError, match="attestation_head_unknown"):
        store.events_after("sha256:" + "9" * 64)
