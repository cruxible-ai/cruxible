from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
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


def _attestation(*, stance: str = "support") -> ClaimAttestationV2:
    statement = ClaimAttestationStatementV2(
        instance_id="inst_test",
        referent_coordinate=COORDINATE,
        claim_identity=ArtifactIdentity(kind="Claim", name="CLM-0123456789abcdef0123456789abcdef"),
        claim_artifact_digest=SHA_A,
        claim_statement_digest=SHA_B,
        subject_shell_digest="sha256:" + "e" * 64,
        attesting_principal_id="owner",
        signing_key_digest="sha256:" + "f" * 64,
        attestation_basis="new_capture",
        stance=stance,  # type: ignore[arg-type]
        cited_capture_digests=(CAPTURE,),
        attested_at=NOW,
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
