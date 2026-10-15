"""Whole service operations cost the same over a small and a ten-times larger history.

Each test runs one real operation end to end -- a Line admission and run, a
prediction settlement on retained terminal evidence, a served query's facts --
over two sizes of the history that operation used to read, and counts its work:
journal frames verified, CAS bodies read, accepted blobs and Claims parsed, and
SQLite VM instructions across every connection it opens. Counts, never wall time.

Only the operation's own work is counted: the thread that runs it. Setup queues
background work (a workspace advertisement after each acceptance reads the
history index on its own thread and connections); on a loaded runner it overlaps
the measurement, so it is drained first and its reads are never counted.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.indexes.resident import close_idle_connections
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.cas import ContentAddressedBodyStore
from tests.core_support._subject_kind_support import fixture_claim_paths


@contextmanager
def _counted(
    monkeypatch: pytest.MonkeyPatch, *, instance: PlaybillInstance | None = None
) -> Iterator[Counter[str]]:
    """Count the work the calling thread does inside the block.

    With ``instance``, its queued workspace advertisement is drained first, so no
    background reader changes shared state (the history index) mid-measurement.
    """

    if instance is not None:
        instance.settled_workspace_advertisement()
    work: Counter[str] = Counter()
    measuring = threading.get_ident()

    def count(key: str, amount: int = 1) -> None:
        if threading.get_ident() == measuring:
            work[key] += amount

    frames = LocalJournalBackend._read_frames
    read = ContentAddressedBodyStore.read
    blobs = PlaybillInstance.blobs_at
    connect = sqlite3.connect
    partition_ids = LocalJournalBackend.partition_ids

    def counted_partition_ids(self: Any, stream: Any) -> Any:
        found = partition_ids(self, stream)
        count("partitions_enumerated", len(found))
        return found

    def counted_frames(*args: Any, **kwargs: Any) -> Iterator[Any]:
        for item in frames(*args, **kwargs):
            count("frames")
            yield item

    def counted_read(self: Any, digest: str, **kwargs: Any) -> bytes:
        count("cas_reads")
        return read(self, digest, **kwargs)

    def counted_blobs(self: Any, oid: str, paths: Any) -> Any:
        paths = tuple(paths)
        count("blobs", len(paths))
        return blobs(self, oid, paths)

    def counted_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, **kwargs)

        def tick() -> int:
            count("vm")
            return 0

        connection.set_progress_handler(tick, 1)
        return connection

    # Index connections and snapshot readers are resident: close the idle ones so
    # the window reconnects, counted.
    close_idle_connections()
    with monkeypatch.context() as patch:
        patch.setattr(LocalJournalBackend, "_read_frames", staticmethod(counted_frames))
        patch.setattr(LocalJournalBackend, "partition_ids", counted_partition_ids)
        patch.setattr(ContentAddressedBodyStore, "read", counted_read)
        patch.setattr(PlaybillInstance, "blobs_at", counted_blobs)
        patch.setattr(sqlite3, "connect", counted_connect)
        yield work


# -- a Line admission and run, over the Line's own past runs ------------------------


def test_a_line_admission_does_not_grow_with_the_lines_past_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One manual ``line run`` (admission, dedupe by occurrence, run) after 3 and 23 runs.

    The occurrence dedupe once walked the Line partition by sequence; every past
    run adds records there.
    """

    from tests.test_procedures.test_manual_line_runs import manual_run
    from tests.test_procedures.test_procedure_source_runs import NOW, _line_world

    instance, root, line = _line_world(tmp_path)
    clock = [NOW]

    def run_once() -> None:
        clock[0] += timedelta(seconds=1)
        state = manual_run(instance, line, at=clock[0], root=root)
        assert state.status == "succeeded", state

    measured = []
    done = 0
    for size in (3, 23):
        while done < size:
            run_once()
            done += 1
        with _counted(monkeypatch, instance=instance) as work:
            run_once()
            done += 1
        measured.append(dict(work))
    small, large = measured
    assert small["frames"] > 0 and small["vm"] > 0
    # Reads match exactly; SQLite may run a handful more instructions on a
    # larger file, while one extra row per past run would add hundreds.
    assert {k: v for k, v in small.items() if k != "vm"} == {
        k: v for k, v in large.items() if k != "vm"
    }, measured
    assert abs(small["vm"] - large["vm"]) <= 64, measured


# -- a prediction settlement on retained terminal evidence --------------------------


def _unrelated_history(instance: PlaybillInstance, *, count: int, run_partition: str) -> None:
    """Finished work the settlement never names, where a scan would meet it first.

    Other runs' records inside the settled run's own partition (before its
    terminal record), partitions that sort before it, and other contracts'
    settlement partitions in the resolution journal.
    """

    from cruxible_core.exhaust import ProcedureExhaustWriter
    from cruxible_core.service.procedures import predictions, procedure_runs
    from tests.test_procedures.p2b5.test_served_predictions import RECORDED_AT, _actor

    journal, _root = procedure_runs._journal_for_write(instance)
    resolutions, resolution_stream = predictions._journal(instance)
    stream = procedure_runs._stream(instance)
    coordinate = procedure_runs.AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    def append(backend, stream, partition, token, kind, ordinal, run):  # type: ignore[no-untyped-def]
        ProcedureExhaustWriter(
            journal=backend, bodies=instance.body_store(), fencing_token=token
        ).append(
            stream=stream,
            partition_id=partition,
            event_kind=kind,
            accepted_coordinate=coordinate,
            procedure_artifact_digest="sha256:" + "1" * 64 if run else None,
            definition_digest="sha256:" + "2" * 64,
            run_id=f"RUN-unrelated-{partition}-{ordinal}" if run else None,
            admission_binding_digest="sha256:" + "3" * 64 if run else None,
            actor_context=_actor(),
            recorded_at=RECORDED_AT,
            payload={"unrelated": ordinal},
        )

    procedure_runs._activate_writer(journal, stream, run_partition)
    for ordinal in range(count):
        append(
            journal,
            stream,
            run_partition,
            procedure_runs.PROCEDURE_RUN_FENCING_TOKEN,
            "node_fired",
            ordinal,
            True,
        )
    for backend, journal_stream, kind, run, prefix in (
        (journal, stream, "node_fired", True, "0unrelated"),
        (resolutions, resolution_stream, "resolution_disposition", False, "unrelated"),
    ):
        for ordinal in range(count):
            partition = f"{prefix}-{ordinal}"
            backend.activate_writer(
                journal_stream,
                partition,
                fencing_token="unrelated",
                expected_head=backend.read_head(journal_stream, partition),
            )
            append(backend, journal_stream, partition, "unrelated", kind, ordinal, run)


def _settle_on_a_retained_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: int):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.claims import claim_path, parse_claim
    from cruxible_client.contracts.predictions import SettleRequest, TerminalSettlementEvidence
    from cruxible_core.exhaust import ProcedureExhaustWriter
    from cruxible_core.procedures.egress import (
        TerminalEgressChildReceiptV2,
        TerminalEgressReceiptV4,
    )
    from cruxible_core.service.procedures import procedure_runs
    from cruxible_core.service.procedures.predictions import service_settle_playbill_prediction
    from tests.test_procedures.p2b5.test_served_predictions import (
        OBSERVED_AT,
        RECORDED_AT,
        _accept_payload,
        _actor,
        _payload,
        _predict,
    )
    from tests.test_procedures.test_procedure_run_surface import _world as run_world

    instance, owner, procedure = run_world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = fixture_claim_paths(tree)[0]
    capture = parse_claim(tree[path], path=path).backing.capture_digests[0]
    contract = _predict(instance, owner, capture)
    observation = _accept_payload(
        instance,
        owner,
        _payload(capture, qualifier="prediction-outcome", value="ready"),
        "2026-09-02T12:02:00.000000Z",
    )
    run = procedure_runs.service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=procedure_runs.ProcedureRunRequest(
            input={}, evaluation_time=OBSERVED_AT, resolution_contract=contract
        ),
        actor_context=_actor(),
    )
    assert run.status == "succeeded"
    records = procedure_runs._records_for_run(instance, run.run_id)
    admission = next(r.record for r in records if r.record.event_kind == "admission_bound")
    journal, _ = procedure_runs._journal_for_write(instance)
    stream = procedure_runs._stream(instance)
    _unrelated_history(instance, count=history, run_partition=admission.partition_id)
    procedure_runs._activate_writer(journal, stream, admission.partition_id)
    target = claim_path(observation.identity.name)
    receipt = TerminalEgressReceiptV4(
        kind="settle_change_set",
        run_id=run.run_id,
        node_id="settle-observation",
        disposition="settled",
        operation_key="sha256:" + "a" * 64,
        children=(
            TerminalEgressChildReceiptV2(
                child_index=0,
                item_key=target,
                egress_digest=observation.artifact_digest,
                path=target,
            ),
        ),
        proposal_id="sha256:" + "b" * 64,
        candidate_digest="sha256:" + "c" * 64,
        target_paths=(target,),
        outcome="settled",
        procedure_mandate_digest="sha256:" + "d" * 64,
        accepted_git_oid="e" * 40,
        fallback_reason=None,
    )
    terminal = ProcedureExhaustWriter(
        journal=journal,
        bodies=instance.body_store(),
        fencing_token=procedure_runs.PROCEDURE_RUN_FENCING_TOKEN,
    ).append(
        stream=stream,
        partition_id=admission.partition_id,
        event_kind="terminal_egress",
        accepted_coordinate=admission.accepted_coordinate,
        procedure_artifact_digest=run.procedure_artifact_digest,
        definition_digest=admission.definition_digest,
        admission_binding_digest=admission.admission_binding_digest,
        line_spec_digest=admission.line_spec_digest,
        occurrence_id=admission.occurrence_id,
        attempt=admission.attempt,
        actor_context=admission.actor_context,
        recorded_at=RECORDED_AT,
        run_id=run.run_id,
        payload={
            "tag": "playbill-procedure-terminal-egress-v1",
            "node_id": receipt.node_id,
            "kind": receipt.kind,
            "verdict": "delivered",
            "receipt": receipt.model_dump(mode="json"),
        },
    )
    request = SettleRequest(
        contract=contract,
        evidence=TerminalSettlementEvidence(
            claim=observation, run_id=run.run_id, terminal_record_digest=terminal.record_digest
        ),
    )
    with _counted(monkeypatch, instance=instance) as work:
        result = service_settle_playbill_prediction(
            instance,
            prediction_id=contract.identity.name,
            request=request,
            actor_context=_actor(),
            recorded_at=RECORDED_AT,
        )
    assert result.relation is not None
    return dict(work)


def test_a_prediction_settlement_does_not_grow_with_unrelated_runs_or_settlements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settling on a retained terminal reads the run's terminal records and the one
    contract's partition; it once enumerated and verified every partition of both
    journals, meeting other runs' records in the run's own partition first."""

    (tmp_path / "small").mkdir()
    (tmp_path / "large").mkdir()
    small = _settle_on_a_retained_terminal(tmp_path / "small", monkeypatch, 4)
    large = _settle_on_a_retained_terminal(tmp_path / "large", monkeypatch, 40)
    assert small["frames"] > 0
    # Two separate worlds: reads must match exactly; SQLite's instruction count
    # may differ by a few between two databases of the same shape, while one
    # extra row visited per unrelated record would add hundreds.
    assert {k: v for k, v in small.items() if k != "vm"} == {
        k: v for k, v in large.items() if k != "vm"
    }, (small, large)
    assert abs(small["vm"] - large["vm"]) <= 64, (small, large)


# -- a served query's facts, over retired Claims and other kinds' Subjects ----------


def test_a_scoped_query_does_not_grow_with_retired_claims_or_other_subject_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live facts never read retired Claims; scoped facts never list other kinds' Subjects."""

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.claims import parse_claim
    from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
    from cruxible_core.service.discovery import query
    from tests.core_support._candidate_support import submit_member_candidate
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.core_support._knowledge_loop_support import (
        PREDICATE,
        SUBJECT_KIND,
        TIMESTAMP,
        accept_proposal,
        activate,
        authoring,
        seed_claims,
    )
    from tests.core_support._retirement_support import retire_claim

    instance, owner = seed_claims(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    subject_id = next(
        parse_claim(tree[path], path=path).statement.subject.artifact_path.rsplit("/", 1)[1]
        for path in fixture_claim_paths(tree)
    ).removesuffix(".json")

    def grow(retired: int, subjects: int, start: int) -> None:
        members = {
            subject_path("project.unrelated", f"u-{start + ordinal}"): render_subject(
                SubjectShell(
                    identity=ArtifactIdentity(
                        kind="Subject", name=f"project.unrelated/u-{start + ordinal}"
                    ),
                    subject_kind="project.unrelated",
                    subject_id=f"u-{start + ordinal}",
                )
            )
            for ordinal in range(subjects)
        }
        accept_proposal(
            instance,
            owner,
            submit_member_candidate(
                instance,
                members=members,
                actor_id="owner",
                proposal_name=f"unrelated-{start}",
                proposal_family="subject",
                timestamp=TIMESTAMP,
            ),
        )
        for ordinal in range(retired):
            before = set(instance.paths_at(instance.accepted_coordinate().git_oid))
            proposed = service_propose_playbill_claim(
                instance,
                authoring=authoring(
                    subject_id, ("blocked", "done")[ordinal % 2], with_claim_type=False
                ),
                actor_id="owner",
                proposal_name=f"retired-{start + ordinal}",
                timestamp=TIMESTAMP,
            )
            activate(instance, owner, proposed)
            (added,) = {
                path
                for path in instance.paths_at(instance.accepted_coordinate().git_oid)
                if path.startswith("claims/")
            } - before
            retire_claim(instance, owner, added.rsplit("/", 1)[1].removesuffix(".json"))

    def measure() -> dict[str, int]:
        coordinate = instance.accepted_coordinate()
        parsed: Counter[str] = Counter()
        original = query.parse_claim

        def counted_parse(content: bytes, *, path: str, **kwargs: Any) -> Any:
            parsed["claims_parsed"] += 1
            return original(content, path=path, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(query, "parse_claim", counted_parse)
            with _counted(monkeypatch, instance=instance) as work:
                facts = query.build_accepted_query_facts(
                    instance,
                    coordinate=coordinate,
                    predicates=(PREDICATE,),
                    subject_kinds=(SUBJECT_KIND,),
                )
        assert facts.claims
        return {**work, **parsed}

    def warm() -> dict[str, int]:
        """The cheapest of three warm measurements of one coordinate.

        The first call does the coordinate's per-process reads once. A bounded
        retry on the measuring thread (an index acquisition that met a directory
        change) only ever adds work, while growth with history would raise every
        repetition, so the minimum is the query's own steady cost.
        """

        measure()
        runs = [measure() for _ in range(3)]
        return min(runs, key=lambda run: run["vm"])

    grow(retired=1, subjects=5, start=0)
    small = warm()
    grow(retired=3, subjects=45, start=100)
    large = warm()
    assert small["claims_parsed"] > 0
    assert {k: v for k, v in small.items() if k != "vm"} == {
        k: v for k, v in large.items() if k != "vm"
    }, (small, large)
    assert abs(small["vm"] - large["vm"]) <= 64, (small, large)
