"""Regression (review P2-3): run paging never repeats or skips a run whose status changes."""

from __future__ import annotations

from pathlib import Path


def _index_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_core.exhaust import LocalJournalBackend
    from cruxible_core.exhaust.records import JournalStreamIdentityV1

    backend = LocalJournalBackend(tmp_path)
    stream = JournalStreamIdentityV1(
        instance_id="inst", journal_family="procedure-exhaust-v1", stream_id="procedures"
    )
    key = canonical_bytes(stream.model_dump(mode="json")).decode()
    sequence = iter(range(1, 100))

    def record(run_id: str, kind: str, at: str) -> None:
        with backend.index.connection() as conn:
            conn.execute(
                "INSERT INTO records(stream,partition_id,sequence,offset,size,digest,previous,"
                "event_kind,run_id,occurrence_id,payload_digest,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, "p", next(sequence), 0, 0, "d", "p", kind, run_id, None, "sha256:0", at),
            )

    return backend, stream, record


def test_run_pages_neither_repeat_nor_skip_a_run_that_finishes_between_pages(
    tmp_path: Path,
) -> None:
    backend, stream, record = _index_world(tmp_path)
    record("RUN-a", "admission_bound", "2026-09-01T00:00:00Z")
    record("RUN-a", "attempt_finalized", "2026-09-01T00:00:00Z")
    record("RUN-b", "admission_bound", "2026-09-02T00:00:00Z")

    walked: list[str] = []
    page, more = backend.index.run_locators(stream, limit=1)
    walked.extend(item.run_id for item in page)
    record("RUN-b", "attempt_finalized", "2026-09-02T00:00:00Z")
    while more:
        page, more = backend.index.run_locators(stream, limit=1, after=page[-1].key)
        walked.extend(item.run_id for item in page)

    assert sorted(walked) == ["RUN-a", "RUN-b"] and len(walked) == 2


def test_the_running_filter_pages_only_running_runs(tmp_path: Path) -> None:
    backend, stream, record = _index_world(tmp_path)
    record("RUN-a", "admission_bound", "2026-09-01T00:00:00Z")
    record("RUN-b", "admission_bound", "2026-09-02T00:00:00Z")
    record("RUN-b", "attempt_finalized", "2026-09-02T00:00:00Z")
    record("RUN-c", "admission_bound", "2026-09-03T00:00:00Z")

    page, more = backend.index.run_locators(stream, limit=5, running_only=True)

    assert [item.run_id for item in page] == ["RUN-c", "RUN-a"] and not more
