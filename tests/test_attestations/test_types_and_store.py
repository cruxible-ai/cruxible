"""Validation and append-only store coverage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cruxible_core.attestation.store import AttestationStore
from cruxible_core.attestation.types import AttestationDisposition, AttestationRecord
from tests.test_attestations.conftest import actor, evidence


def _record(**overrides: object) -> AttestationRecord:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    payload: dict[str, object] = {
        "relationship_type": "protected_by",
        "from_type": "Service",
        "from_id": "svc-1",
        "to_type": "Control",
        "to_id": "ctl-1",
        "claim_content_digest": "sha256:digest",
        "claim_state_at_record": "live",
        "stance": "support",
        "evidence_refs": [evidence()],
        "observed_at": now,
        "recorded_at": now,
        "actor_context": actor("observer"),
    }
    payload.update(overrides)
    return AttestationRecord.model_validate(payload)


def test_stance_evidence_and_observation_time_validation() -> None:
    with pytest.raises(ValidationError, match="requires at least one evidence ref"):
        _record(evidence_refs=[])
    unsure = _record(stance="unsure", evidence_refs=[])
    assert unsure.evidence_refs == []
    with pytest.raises(ValidationError, match="less than or equal"):
        _record(
            observed_at=datetime(2026, 7, 24, 12, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc),
        )


def test_store_has_only_append_and_read_surfaces() -> None:
    store = AttestationStore()
    try:
        record = _record()
        store.save_attestation(record)
        assert store.get_attestation(record.attestation_id) == record
        assert not hasattr(store, "update_attestation")
        assert not hasattr(store, "delete_attestation")
        assert not hasattr(store, "update_disposition")
        assert not hasattr(store, "delete_disposition")
    finally:
        store.close()


def test_latest_disposition_wins_deterministically() -> None:
    store = AttestationStore()
    try:
        record = _record()
        store.save_attestation(record)
        first = AttestationDisposition(
            attestation_id=record.attestation_id,
            verdict="invalidated",
            reviewer_actor_context=actor("reviewer-1"),
        )
        second = AttestationDisposition(
            attestation_id=record.attestation_id,
            verdict="upheld",
            reviewer_actor_context=actor("reviewer-2"),
            recorded_at=first.recorded_at + timedelta(microseconds=1),
        )
        store.save_disposition(first)
        store.save_disposition(second)
        latest = store.get_latest_dispositions([record.attestation_id])
        assert latest[record.attestation_id].disposition_id == second.disposition_id
        summary = store.summaries_for_claims({record.claim_key(): record.claim_content_digest})[
            record.claim_key()
        ]
        assert summary.support_count == 1
        assert summary.invalidated_count == 0
    finally:
        store.close()
