"""Index- and store-level guards: their counted work does not grow with history.

The whole service operations (a Line admission and run, a prediction
settlement, a served query's facts) are measured end to end in
``test_service_scaling.py``; these pin the primitives those operations use.

Each operation is measured by counted work (SQLite VM steps on the journal index,
journal frames read, store objects loaded and bytes written, accepted blobs and
CAS objects read), never wall time, once over a small history and once over a
ten times larger one, and the counts must not grow. The history is synthetic
where the operation only reads an index, real where it writes a store.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.evidence import claim_attestation_store as attestation_store
from cruxible_core.evidence.claim_attestation_store import ClaimAttestationEvidenceStore
from cruxible_core.exhaust import LocalJournalBackend
from cruxible_core.exhaust.records import JournalStreamIdentityV1
from tests.test_claims.test_claim_attestation_store import NOW, _account, _attestation

SMALL, LARGE = 20, 200


@contextmanager
def _vm_steps() -> Iterator[Counter[str]]:
    """Count the SQLite VM instructions every journal-index connection executes."""

    steps: Counter[str] = Counter()
    original = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original(*args, **kwargs)

        def tick() -> int:
            steps["vm"] += 1
            return 0

        connection.set_progress_handler(tick, 1)
        return connection

    sqlite3.connect = connect  # type: ignore[assignment]
    try:
        yield steps
    finally:
        sqlite3.connect = original


# -- the journal index: run admission, run count, settle lookups ------------------


def _journal_world(tmp_path: Path, runs: int):  # type: ignore[no-untyped-def]
    """``runs`` finished Line runs (admission, nodes, final) plus one still running."""

    tmp_path.mkdir(mode=0o700)
    backend = LocalJournalBackend(tmp_path)
    stream = JournalStreamIdentityV1(
        instance_id="inst", journal_family="procedure-exhaust-v1", stream_id="procedures"
    )
    key = canonical_bytes(stream.model_dump(mode="json")).decode()
    rows = []
    sequence = 0

    def record(run_id: str, kind: str, occurrence: str | None = None) -> None:
        nonlocal sequence
        sequence += 1
        rows.append(
            (
                key,
                "line:a",
                sequence,
                0,
                0,
                "d",
                "p",
                kind,
                run_id,
                occurrence,
                f"sha256:{sequence:064x}",
                "2026-09-01T00:00:00Z",
            )
        )

    for index in range(runs):
        run = f"RUN-{index:06d}"
        record(run, "admission_bound", f"occ-{index:06d}")
        for _node in range(3):
            record(run, "node_fired")
        record(run, "terminal_egress")
        record(run, "attempt_finalized")
    record("RUN-open", "admission_bound", "occ-open")
    record("RUN-open", "node_fired")
    with backend.index.connection() as conn:
        conn.executemany(
            "INSERT INTO records(stream,partition_id,sequence,offset,size,digest,previous,"
            "event_kind,run_id,occurrence_id,payload_digest,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return backend, stream


def _journal_operations(backend: LocalJournalBackend, stream: JournalStreamIdentityV1) -> None:
    index = backend.index
    # Line admission: the occurrence dedupe (a new occurrence: nothing to read).
    assert (
        index.select(
            stream,
            partition_id="line:a",
            event_kind="admission_bound",
            occurrence_id="occ-new",
            descending=True,
            limit=1,
        )
        == ()
    )
    # Run count and the running page (orient, `get Line`, startup recovery).
    assert index.run_counts(stream)[1] == 1
    assert index.run_counts(stream, partition_id="line:a")[1] == 1
    (running,), _more = index.run_locators(stream, limit=5, running_only=True)
    assert running.run_id == "RUN-open"
    assert [item.run_id for item in index.running(stream)] == ["RUN-open"]
    # Prediction settle: the run's terminal record and a lease's payload match.
    index.select(stream, run_id="RUN-absent", event_kind="terminal_egress")
    index.select(stream, event_kind="terminal_egress", payload_digest="sha256:" + "e" * 64)


def test_journal_index_operations_do_not_grow_with_runs(tmp_path: Path) -> None:
    counts = []
    for runs in (SMALL, LARGE):
        backend, stream = _journal_world(tmp_path / str(runs), runs)
        _journal_operations(backend, stream)  # warm the connection path once
        with _vm_steps() as steps:
            _journal_operations(backend, stream)
        counts.append(steps["vm"])
    assert counts[0] > 0
    assert counts[1] == counts[0], counts


# -- the attestation store: one append, and the reads every consumer tick makes ----


@contextmanager
def _store_work(monkeypatch: pytest.MonkeyPatch) -> Iterator[Counter[str]]:
    work: Counter[str] = Counter()
    exclusive, pointer = attestation_store._exclusive_write, attestation_store._replace_pointer
    load = ClaimAttestationEvidenceStore._load_object

    def exclusive_write(path: Path, content: bytes) -> None:
        work["files_written"] += 1
        work["bytes_written"] += len(content)
        exclusive(path, content)

    def replace_pointer(path: Path, content: bytes) -> None:
        work["files_written"] += 1
        work["bytes_written"] += len(content)
        pointer(path, content)

    def load_object(self, kind, digest, model):  # type: ignore[no-untyped-def]
        work["objects_loaded"] += 1
        return load(self, kind, digest, model)

    with monkeypatch.context() as patch:
        patch.setattr(attestation_store, "_exclusive_write", exclusive_write)
        patch.setattr(attestation_store, "_replace_pointer", replace_pointer)
        patch.setattr(ClaimAttestationEvidenceStore, "_load_object", load_object)
        yield work


def _append(store: ClaimAttestationEvidenceStore, ordinal: int) -> None:
    attestation = _attestation(
        attested_at=NOW + timedelta(seconds=ordinal),
        # A new Claim each time: the head map grows with every attested Claim.
        claim_id=f"CLM-{ordinal:032x}",
    )
    store.append(attestation=attestation, verification_account=_account(attestation), note=None)


def test_attestation_append_and_ticks_do_not_grow_with_attestations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exhaust = tmp_path / "exhaust"
    exhaust.mkdir()
    store = ClaimAttestationEvidenceStore(exhaust, instance_id="inst_test")
    measured: list[Counter[str]] = []
    appended = 0
    for size in (SMALL, LARGE):
        while appended < size:
            _append(store, appended)
            appended += 1
        # A consumer tick has already folded what was appended before it; the
        # fold returns every outstanding event (its result), served warm.
        store.fold_events()
        with _store_work(monkeypatch) as work:
            _append(store, 10_000 + size)
            appended += 1
            store.head()
            store.fold_events()
        measured.append(work)
    small, large = measured
    assert small["objects_loaded"] == large["objects_loaded"], measured
    assert small["files_written"] == large["files_written"], measured
    # Only a sequence number's extra digits may differ.
    assert large["bytes_written"] - small["bytes_written"] <= 16, measured


# -- a served read over accepted state, with door and evidence history around it -----


def test_a_query_does_not_grow_with_attestation_or_evidence_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.discovery import query
    from cruxible_core.storage.cas import ContentAddressedBodyStore
    from tests.core_support._knowledge_loop_support import (
        PREDICATE,
        SUBJECT_KIND,
        seed_claims,
    )

    instance, _owner = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    door = instance.claim_attestation_evidence_store()

    def read() -> Any:
        return query.build_accepted_query_facts(
            instance,
            coordinate=coordinate,
            predicates=(PREDICATE,),
            subject_kinds=(SUBJECT_KIND,),
        )

    def counted(work: Counter[str], name: str, original: Callable[..., Any]) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            work[name] += 1
            return original(*args, **kwargs)

        return wrapper

    def measure() -> Counter[str]:
        work: Counter[str] = Counter()
        blobs = PlaybillInstance.blobs_at

        def blobs_at(self, oid, paths):  # type: ignore[no-untyped-def]
            work["blobs"] += len(tuple(paths))
            return blobs(self, oid, paths)

        with monkeypatch.context() as patch:
            patch.setattr(PlaybillInstance, "blobs_at", blobs_at)
            patch.setattr(query, "parse_claim", counted(work, "claims_parsed", query.parse_claim))
            for name in ("read", "verify", "file_identity", "availability"):
                patch.setattr(
                    ContentAddressedBodyStore,
                    name,
                    counted(work, f"cas_{name}", getattr(ContentAddressedBodyStore, name)),
                )
            with _vm_steps() as steps:
                facts = read()
            work["vm"] = steps["vm"]
        assert facts.claims
        return work

    def grow(to: int) -> None:
        events = len(door.events())
        for ordinal in range(events, to):
            _append(door, ordinal)
            instance.body_store().store(f"an evidence body {ordinal}".encode())

    read()  # warm per-process reads once
    grow(SMALL)
    small = measure()
    grow(LARGE)
    large = measure()
    assert small == large, (small, large)


def test_the_open_proposal_count_reads_only_the_open_locator(tmp_path: Path) -> None:
    """Orient's open-proposal count: one range of the open-parent partial index."""

    from cruxible_core.indexes.proposals import proposal_index

    connection = sqlite3.connect(":memory:")
    connection.executescript(proposal_index._SCHEMA)
    plan = [
        row[3]
        for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT count(*) FROM proposals INDEXED BY proposals_by_open_parent "
            "WHERE evaluation_status='candidate' AND withdrawal_path IS NULL "
            "AND accepted_sequence IS NULL AND candidate_parent_semantic_root=? "
            "AND admission_path IS NOT NULL",
            ("sha256:" + "a" * 64,),
        )
    ]
    assert plan and all(line.startswith("SEARCH") for line in plan), plan
    assert "proposals_by_open_parent (candidate_parent_semantic_root=?)" in plan[0]
