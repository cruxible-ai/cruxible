"""Portable Procedure-journal identity, chain, recovery, and fencing laws."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    GenerationRoot,
    SemanticRoot,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillJournalError
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalHeadVectorV1,
    JournalPartitionHeadV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureJournalRecordDraftV1,
    build_journal_head_manifest,
    journal_genesis_digest,
    payload_digest,
    verify_journal_head_manifest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-journal-test-v1",
        {"label": label},
    ).tagged


def _coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root=typed_digest(
            SemanticRoot, "playbill-journal-test-semantic-v1", {"value": "accepted"}
        ).tagged,
        generation_root=typed_digest(
            GenerationRoot, "playbill-journal-test-generation-v1", {"value": "accepted"}
        ).tagged,
        compiler_digest=_digest("compiler"),
    )


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="operator",
        org_id="instance-a",
        operation_id="operation-a",
        timestamp=NOW,
    )


def _stream() -> JournalStreamIdentityV1:
    return JournalStreamIdentityV1(
        instance_id="instance-a",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )


def _draft(
    label: str,
    *,
    stream: JournalStreamIdentityV1 | None = None,
    recorded_at: datetime = NOW,
) -> ProcedureJournalRecordDraftV1:
    return ProcedureJournalRecordDraftV1(
        stream=stream or _stream(),
        partition_id="runs-2026-08",
        event_kind="node_fired",
        accepted_coordinate=_coordinate(),
        procedure_artifact_digest=_digest("procedure"),
        definition_digest=_digest("definition"),
        run_id="run-a",
        admission_binding_digest=_digest("admission"),
        payload_digest=payload_digest({"label": label}),
        actor_context=_actor(),
        recorded_at=recorded_at,
    )


@dataclass(frozen=True)
class _HeadSigner:
    private_key: Ed25519PrivateKey
    signer_id: str = "journal-home"
    signing_key_id: str = "head-key-1"

    def sign_journal_head(self, message: bytes) -> str:
        return self.private_key.sign(message).hex()


def _backend(tmp_path, name: str) -> LocalJournalBackend:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return LocalJournalBackend(root)


def _activate(backend: LocalJournalBackend, token: str = "writer-a") -> None:
    stream = _stream()
    head = backend.read_head(stream, "runs-2026-08")
    backend.activate_writer(
        stream,
        "runs-2026-08",
        fencing_token=token,
        expected_head=head,
    )


def _append(
    backend: LocalJournalBackend,
    label: str,
    *,
    token: str = "writer-a",
    recorded_at: datetime = NOW,
):
    stream = _stream()
    head = backend.read_head(stream, "runs-2026-08")
    return backend.append(
        _draft(label, recorded_at=recorded_at),
        expected_head=head,
        fencing_token=token,
    )


def test_journal_family_is_registered_and_genesis_is_domain_separated() -> None:
    stream = _stream()
    assert journal_genesis_digest(stream, "a") != journal_genesis_digest(stream, "b")
    with pytest.raises(ValidationError, match="unknown journal family"):
        JournalStreamIdentityV1(
            instance_id="instance-a",
            journal_family="invented",
            stream_id="procedures",
        )
    with pytest.raises(ValidationError, match="genesis"):
        JournalPartitionHeadV1(
            stream=stream,
            partition_id="a",
            sequence=0,
            record_digest=_digest("not-genesis"),
        )


def test_expected_head_fence_chain_and_idempotent_append(tmp_path) -> None:
    backend = _backend(tmp_path, "journal")
    stream = _stream()
    genesis = backend.read_head(stream, "runs-2026-08")
    _activate(backend)
    draft = _draft("first")
    first = backend.append(draft, expected_head=genesis, fencing_token="writer-a")
    retry = backend.append(draft, expected_head=genesis, fencing_token="writer-a")
    assert retry == first
    assert first.record.sequence == 1
    assert first.record.previous_record_digest == genesis.record_digest

    with pytest.raises(PlaybillJournalError, match="fencing token"):
        backend.append(_draft("second"), expected_head=genesis, fencing_token="writer-b")
    with pytest.raises(PlaybillJournalError, match="stale or forked"):
        backend.append(_draft("second"), expected_head=genesis, fencing_token="writer-a")


def test_recovery_discards_only_an_incomplete_final_frame(tmp_path) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    first = _append(backend, "first")
    path = backend._record_log_path_for_testing(_stream(), "runs-2026-08")
    original_size = path.stat().st_size
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(descriptor, (100).to_bytes(8, "big") + b"partial")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    recovered = backend.recover_partition(_stream(), "runs-2026-08")
    assert recovered.sequence == 1
    assert recovered.record_digest == first.record_digest
    assert path.stat().st_size == original_size


def test_complete_chain_tamper_is_corruption_not_crash_recovery(tmp_path) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    _append(backend, "first")
    path = backend._record_log_path_for_testing(_stream(), "runs-2026-08")
    content = bytearray(path.read_bytes())
    content[-5] ^= 1
    path.write_bytes(content)
    with pytest.raises(PlaybillJournalError, match="malformed|digest|canonical"):
        backend.recover_partition(_stream(), "runs-2026-08")


def test_signed_head_authenticates_assertion_but_not_witness_role(tmp_path) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    _append(backend, "first")
    vector = backend.read_head_vector(((_stream(), "runs-2026-08"),))
    signer = _HeadSigner(Ed25519PrivateKey.generate())
    manifest = build_journal_head_manifest(vector, asserted_at=NOW, signer=signer)
    verify_journal_head_manifest(
        manifest,
        expected_public_key=signer.private_key.public_key().public_bytes_raw().hex(),
    )
    assert manifest.statement.signing_role == "journal_head"

    changed = manifest.model_dump(mode="json")
    changed["statement"]["head_vector"]["partitions"][0]["sequence"] = 2
    tampered = type(manifest).model_validate(changed)
    with pytest.raises(PlaybillJournalError, match="signature"):
        verify_journal_head_manifest(
            tampered,
            expected_public_key=signer.private_key.public_key().public_bytes_raw().hex(),
        )


def test_head_vector_refuses_duplicate_or_unsorted_partitions(tmp_path) -> None:
    backend = _backend(tmp_path, "journal")
    head_a = backend.read_head(_stream(), "a")
    head_b = backend.read_head(_stream(), "b")
    with pytest.raises(ValidationError, match="sorted and unique"):
        JournalHeadVectorV1(partitions=(head_b, head_a))
    with pytest.raises(ValidationError, match="sorted and unique"):
        JournalHeadVectorV1(partitions=(head_a, head_a))


def test_exact_range_rejects_truncation_substitution_and_discontinuity(tmp_path) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    first = _append(backend, "first")
    second = _append(backend, "second", recorded_at=NOW + timedelta(seconds=1))
    journal_range = backend.range_from_sequences(
        _stream(), "runs-2026-08", first_sequence=1, last_sequence=2
    )
    assert backend.read_exact_range(journal_range) == (first, second)

    truncated = journal_range.model_dump(mode="json")
    truncated["last_sequence"] = 1
    truncated["expected_head_digest"] = second.record_digest
    with pytest.raises(PlaybillJournalError, match="expected head"):
        backend.read_exact_range(type(journal_range).model_validate(truncated))
