"""Historical resolution evidence remains replayable but not writable."""

from __future__ import annotations

import json
import sqlite3

from cruxible_core.storage.resolution_evidence import LegacyResolutionEvidenceReader


def _actor(actor_id: str) -> str:
    return json.dumps(
        {
            "actor_type": "human_user",
            "actor_id": actor_id,
            "org_id": "org-1",
            "operation_id": f"op-{actor_id}",
            "timestamp": "2026-08-01T12:00:00Z",
        }
    )


def test_reader_replays_legacy_rows_without_exposing_mutations() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE attestations (
            attestation_id TEXT PRIMARY KEY,
            relationship_type TEXT NOT NULL,
            from_type TEXT NOT NULL,
            from_id TEXT NOT NULL,
            to_type TEXT NOT NULL,
            to_id TEXT NOT NULL,
            edge_key INTEGER,
            claim_id TEXT,
            claim_content_digest TEXT NOT NULL,
            claim_state_at_record TEXT NOT NULL,
            stance TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            actor_context_json TEXT NOT NULL
        );
        CREATE TABLE attestation_dispositions (
            disposition_id TEXT PRIMARY KEY,
            attestation_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            reviewer_actor_context_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO attestations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "att-1",
            "protected_by",
            "Service",
            "svc-1",
            "Control",
            "ctl-1",
            None,
            "claim-1",
            "sha256:claim",
            "live",
            "support",
            '[{"source":"audit","source_record_id":"row-1"}]',
            "2026-08-01T11:00:00Z",
            "2026-08-01T12:00:00Z",
            _actor("observer"),
        ),
    )
    connection.executemany(
        "INSERT INTO attestation_dispositions VALUES (?, ?, ?, ?, ?)",
        [
            (
                "disp-1",
                "att-1",
                "invalidated",
                _actor("reviewer-1"),
                "2026-08-01T12:01:00Z",
            ),
            (
                "disp-2",
                "att-1",
                "upheld",
                _actor("reviewer-2"),
                "2026-08-01T12:02:00Z",
            ),
        ],
    )

    reader = LegacyResolutionEvidenceReader(connection=connection)
    record = reader.get_attestation("att-1")
    assert record is not None
    assert record.claim_id == "claim-1"
    assert record.evidence_refs[0].source_record_id == "row-1"
    latest = reader.get_latest_dispositions(["att-1"])
    assert latest["att-1"].disposition_id == "disp-2"
    assert not hasattr(reader, "save_attestation")
    assert not hasattr(reader, "save_disposition")
    reader.close()
    connection.execute("SELECT 1")
    connection.close()


def test_reader_treats_absent_legacy_tables_as_empty() -> None:
    reader = LegacyResolutionEvidenceReader()
    try:
        assert reader.get_attestation("missing") is None
        assert reader.get_latest_dispositions(["missing"]) == {}
    finally:
        reader.close()
