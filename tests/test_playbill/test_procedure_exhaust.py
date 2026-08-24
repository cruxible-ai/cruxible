"""Procedure-exhaust writer integration and CAS-binding laws."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    GenerationRoot,
    SemanticRoot,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillJournalError
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
    journal_payload_bytes,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-exhaust-test-v1",
        {"label": label},
    ).tagged


def _coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root=typed_digest(
            SemanticRoot,
            "playbill-procedure-exhaust-semantic-v1",
            {"value": "accepted"},
        ).tagged,
        generation_root=typed_digest(
            GenerationRoot,
            "playbill-procedure-exhaust-generation-v1",
            {"value": "accepted"},
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
        stream_id="procedure-readings",
    )


def _writer(tmp_path):
    journal_root = tmp_path / "journal"
    cas_root = tmp_path / "cas"
    journal_root.mkdir(mode=0o700)
    cas_root.mkdir(mode=0o700)
    journal = LocalJournalBackend(journal_root)
    bodies = ContentAddressedBodyStore(cas_root)
    stream = _stream()
    partition_id = "readings"
    journal.activate_writer(
        stream,
        partition_id,
        fencing_token="writer-a",
        expected_head=journal.read_head(stream, partition_id),
    )
    return (
        ProcedureExhaustWriter(
            journal=journal,
            bodies=bodies,
            fencing_token="writer-a",
        ),
        journal,
        bodies,
        stream,
        partition_id,
    )


def test_writer_binds_exact_coordinate_and_cas_payload(tmp_path) -> None:
    writer, journal, bodies, stream, partition_id = _writer(tmp_path)
    payload = {"verdict": "satisfied", "measurement": 7}

    stored = writer.append(
        stream=stream,
        partition_id=partition_id,
        event_kind="procedure_reading",
        accepted_coordinate=_coordinate(),
        procedure_artifact_digest=_digest("procedure"),
        definition_digest=_digest("definition"),
        actor_context=_actor(),
        recorded_at=NOW,
        payload=payload,
        run_id="run-a",
    )

    assert stored.record.sequence == 1
    assert stored.record.accepted_coordinate == _coordinate()
    expected_payload_digest = bodies.digest_bytes(journal_payload_bytes(payload)).tagged
    assert stored.record.payload_digest == expected_payload_digest
    assert bodies.verify(stored.record.payload_digest)
    assert journal.read_head(stream, partition_id).record_digest == stored.record_digest


def test_writer_refuses_inactive_fence_without_journal_record(tmp_path) -> None:
    writer, journal, _bodies, stream, partition_id = _writer(tmp_path)
    journal.fence_writer(stream, partition_id, expected_fencing_token="writer-a")

    with pytest.raises(PlaybillJournalError, match="active fencing token"):
        writer.append(
            stream=stream,
            partition_id=partition_id,
            event_kind="procedure_reading",
            accepted_coordinate=_coordinate(),
            procedure_artifact_digest=_digest("procedure"),
            definition_digest=_digest("definition"),
            actor_context=_actor(),
            recorded_at=NOW,
            payload={"verdict": "refused"},
            run_id="run-a",
        )

    assert journal.all_records(stream, partition_id) == ()
