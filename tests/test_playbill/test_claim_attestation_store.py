from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_attestation_store import (
    ClaimAttestationEventV1,
    ClaimAttestationHeadMapEntryV1,
    ClaimAttestationPartitionHeadV1,
    ClaimAttestationPublishedPointerV1,
    claim_attestation_partition_head_digest,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationStatementV2,
    ClaimAttestationV2,
    VerifiedClaimAttestationV2,
    claim_attestation_v2_envelope_digest,
    claim_attestation_v2_statement_digest,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.claim_attestation_store import (
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
) -> ClaimAttestationV2:
    statement = ClaimAttestationStatementV2(
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
    return ClaimAttestationV2(statement=statement, signature="01" * 64)


def _account(attestation: ClaimAttestationV2) -> VerifiedClaimAttestationV2:
    return VerifiedClaimAttestationV2(
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
    exhaust.mkdir(exist_ok=True)
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


def test_post_chain_exception_poison_requires_recovery(tmp_path: Path) -> None:
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
    with pytest.raises(ClaimAttestationStoreError, match="store_poisoned"):
        store.head()
    store.crash_hook = None
    store.recover()
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
    with pytest.raises(ClaimAttestationStoreError, match="store_corrupt"):
        store.events(at_head=receipt.recorded_head)


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
    accelerator = next((store.root / "accelerators").glob("*.json"))
    accelerator.write_text("{}\n", encoding="utf-8")
    assert [item[0].event_digest for item in store.fold_events()] == [
        first_receipt.event_digest,
        second_receipt.event_digest,
    ]
    assert b"playbill-claim-attestation-accelerator-v1" in accelerator.read_bytes()


def _write_pointer(store: ClaimAttestationEvidenceStore, root_digest: str) -> None:
    pointer = ClaimAttestationPublishedPointerV1(root_digest=root_digest)
    (store.root / "published.json").write_bytes(
        canonical_bytes(pointer.model_dump(mode="json")) + b"\n"
    )


def _current_root_and_map(store: ClaimAttestationEvidenceStore):  # type: ignore[no-untyped-def]
    chain = store._validated_chain(store._load_pointer().root_digest)
    return chain[-1]


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
    current, node = _current_root_and_map(store)
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
    current, node = _current_root_and_map(store)
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
        if store._load_marker(path).partition_digest != node.entries[0].partition_digest
    )
    second_event = store._load_marker(second_marker)
    second_head = store._partition_head(second_event)
    unrelated_partition = "sha256:" + "9" * 64
    unrelated_draft = ClaimAttestationPartitionHeadV1.model_construct(
        tag="playbill-claim-attestation-partition-head-v1",
        partition_digest=unrelated_partition,
        sequence=1,
        event_digest=second_event.event_digest,
        head_digest="sha256:" + "0" * 64,
    )
    unrelated = ClaimAttestationPartitionHeadV1(
        partition_digest=unrelated_partition,
        sequence=1,
        event_digest=second_event.event_digest,
        head_digest=claim_attestation_partition_head_digest(unrelated_draft),
    )
    forged_map = store._head_map(
        (
            *node.entries,
            ClaimAttestationHeadMapEntryV1(
                partition_digest=second_head.partition_digest,
                head=second_head,
            ),
            ClaimAttestationHeadMapEntryV1(
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
    first_root, first_map = full_chain[1]
    third_event = store._load_object(
        "event",
        third_receipt.event_digest,
        ClaimAttestationEventV1,
    )
    assert isinstance(third_event, ClaimAttestationEventV1)
    first_heads = {item.partition_digest: item.head for item in first_map.entries}
    first_heads[third_event.partition_digest] = store._partition_head(third_event)
    fork_map = store._head_map(
        tuple(
            ClaimAttestationHeadMapEntryV1(partition_digest=key, head=value)
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
    current, node = _current_root_and_map(store)
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
    for ordinal in range(300):
        attestation = _attestation(attested_at=NOW + timedelta(seconds=ordinal))
        store.append(
            attestation=attestation,
            verification_account=_account(attestation),
            note=None,
        )
    assert partition_scans == 0

    reopened = _store(tmp_path)
    payload_loads = 0
    original_payload = reopened._payload_for_event

    def counted_payload(event):  # type: ignore[no-untyped-def]
        nonlocal payload_loads
        payload_loads += 1
        return original_payload(event)

    monkeypatch.setattr(reopened, "_payload_for_event", counted_payload)
    assert len(reopened.fold_events()) == 300
    assert payload_loads == 300
    assert len(reopened.fold_events()) == 300
    assert payload_loads == 300
