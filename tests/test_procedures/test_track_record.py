"""Procedure read-surface track-record coverage."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.procedure.store import (
    _MAX_ID_PARAMETERS_PER_STATEMENT,
    ProcedureStore,
)
from cruxible_core.procedure.types import (
    ProcedureBudgetSpent,
    ProcedureRun,
    ProcedureTrackRecord,
)
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import (
    service_get_procedure,
    service_list_procedures,
    service_propose_procedure,
    service_run_procedure,
)
from tests.test_procedures.conftest import actor, provider_definition
from tests.test_procedures.test_execution import _accept, _run, _stub_provider


def _finalized_run(
    *,
    run_id: str,
    procedure_id: str,
    definition_digest: str,
    verdict: str,
    finalized_at: str,
    refusal_reason: str | None = None,
) -> ProcedureRun:
    return ProcedureRun.model_validate(
        {
            "run_id": run_id,
            "procedure_id": procedure_id,
            "definition_digest": definition_digest,
            "status": "finalized",
            "verdict": verdict,
            "started_at": finalized_at,
            "finalized_at": finalized_at,
            "refusal_reason": refusal_reason,
        }
    )


def test_procedure_reads_attach_grouped_run_track_records(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked = service_propose_procedure(
        procedure_instance,
        provider_definition("tracked_procedure"),
        actor_context=actor("track-proposer"),
    ).procedure
    idle = service_propose_procedure(
        procedure_instance,
        provider_definition("idle_procedure"),
        actor_context=actor("idle-proposer"),
    ).procedure
    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_run(
            ProcedureRun(
                run_id="PRN-track-started",
                procedure_id=tracked.procedure_id,
                definition_digest=tracked.definition_digest,
            )
        )
        for run_id, verdict, finalized_at in (
            ("PRN-track-success", "succeeded", "2026-07-22T13:00:00Z"),
            ("PRN-track-failed", "failed", "2026-07-22T13:01:00Z"),
            ("PRN-track-refused", "refused", "2026-07-22T13:02:00Z"),
            ("PRN-track-budget", "budget_exceeded", "2026-07-22T13:04:00Z"),
            ("PRN-track-success2", "succeeded", "2026-07-22T13:03:00Z"),
        ):
            uow.procedures.save_run(
                _finalized_run(
                    run_id=run_id,
                    procedure_id=tracked.procedure_id,
                    definition_digest=tracked.definition_digest,
                    verdict=verdict,
                    finalized_at=finalized_at,
                )
            )

    aggregation_calls: list[tuple[str, ...]] = []
    original = ProcedureStore.get_run_track_records

    def record_aggregation(
        store: ProcedureStore,
        procedure_ids: Sequence[str],
    ) -> dict[str, ProcedureTrackRecord]:
        aggregation_calls.append(tuple(procedure_ids))
        return original(store, procedure_ids)

    monkeypatch.setattr(ProcedureStore, "get_run_track_records", record_aggregation)

    listed = service_list_procedures(procedure_instance)

    assert len(aggregation_calls) == 1
    assert set(aggregation_calls[0]) == {tracked.procedure_id, idle.procedure_id}
    by_id = {item.procedure_id: item.track_record for item in listed.items}
    assert by_id[idle.procedure_id] == ProcedureTrackRecord()
    assert by_id[tracked.procedure_id].model_dump(mode="json") == {
        "runs": 6,
        "succeeded": 2,
        "failed": 1,
        "refused": 1,
        "budget_exceeded": 1,
        "in_flight": 1,
        # The newest SUCCESS, not the newest run: the budget_exceeded row is
        # newer and must not be read as a healthy completion.
        "last_succeeded_at": "2026-07-22T13:03:00Z",
        "top_refusal_reason": None,
        "linked_outcomes": None,
    }

    shown = service_get_procedure(procedure_instance, tracked.procedure_id)

    assert aggregation_calls[1] == (tracked.procedure_id,)
    assert shown.track_record == by_id[tracked.procedure_id]


def test_verdict_buckets_account_for_every_run(
    procedure_instance: CruxibleInstance,
) -> None:
    """A procedure that only ever blows its budget must not read as busy.

    The dead-versus-busy confusion is the whole reason the buckets are
    exhaustive, so it is pinned as an invariant over the aggregate rather than
    as four independent count assertions.
    """
    dead = service_propose_procedure(
        procedure_instance,
        provider_definition("always_over_budget"),
        actor_context=actor("dead-proposer"),
    ).procedure
    busy = service_propose_procedure(
        procedure_instance,
        provider_definition("still_running"),
        actor_context=actor("busy-proposer"),
    ).procedure
    with procedure_instance.write_transaction() as uow:
        for index in range(3):
            uow.procedures.save_run(
                _finalized_run(
                    run_id=f"PRN-dead-{index}",
                    procedure_id=dead.procedure_id,
                    definition_digest=dead.definition_digest,
                    verdict="budget_exceeded",
                    finalized_at=f"2026-07-22T14:0{index}:00Z",
                )
            )
            uow.procedures.save_run(
                ProcedureRun(
                    run_id=f"PRN-busy-{index}",
                    procedure_id=busy.procedure_id,
                    definition_digest=busy.definition_digest,
                )
            )

    store = procedure_instance.get_procedure_store()
    try:
        records = store.get_run_track_records([dead.procedure_id, busy.procedure_id])
    finally:
        store.close()

    for record in records.values():
        assert record.runs == (
            record.succeeded
            + record.failed
            + record.refused
            + record.budget_exceeded
            + record.in_flight
        )
    assert records[dead.procedure_id].budget_exceeded == 3
    assert records[dead.procedure_id].in_flight == 0
    assert records[busy.procedure_id].in_flight == 3
    assert records[busy.procedure_id].budget_exceeded == 0
    assert records[dead.procedure_id] != records[busy.procedure_id]


def test_track_records_do_not_cross_contaminate_between_procedures(
    procedure_instance: CruxibleInstance,
) -> None:
    """Two procedures that BOTH have runs keep their own counts.

    The single-populated-procedure case cannot fail this way: a grouped query
    that dropped its ``GROUP BY`` would still look right when only one id has
    rows at all.
    """
    left = service_propose_procedure(
        procedure_instance,
        provider_definition("left_procedure"),
        actor_context=actor("left-proposer"),
    ).procedure
    right = service_propose_procedure(
        procedure_instance,
        provider_definition("right_procedure"),
        actor_context=actor("right-proposer"),
    ).procedure
    with procedure_instance.write_transaction() as uow:
        for index in range(2):
            uow.procedures.save_run(
                _finalized_run(
                    run_id=f"PRN-left-{index}",
                    procedure_id=left.procedure_id,
                    definition_digest=left.definition_digest,
                    verdict="succeeded",
                    finalized_at=f"2026-07-22T15:0{index}:00Z",
                )
            )
        for index in range(3):
            uow.procedures.save_run(
                _finalized_run(
                    run_id=f"PRN-right-{index}",
                    procedure_id=right.procedure_id,
                    definition_digest=right.definition_digest,
                    verdict="refused",
                    finalized_at=f"2026-07-22T16:0{index}:00Z",
                    refusal_reason="precondition_unsatisfied",
                )
            )

    store = procedure_instance.get_procedure_store()
    try:
        records = store.get_run_track_records([left.procedure_id, right.procedure_id])
    finally:
        store.close()

    assert records[left.procedure_id].model_dump(mode="json") == {
        "runs": 2,
        "succeeded": 2,
        "failed": 0,
        "refused": 0,
        "budget_exceeded": 0,
        "in_flight": 0,
        "last_succeeded_at": "2026-07-22T15:01:00Z",
        "top_refusal_reason": None,
        "linked_outcomes": None,
    }
    assert records[right.procedure_id].model_dump(mode="json") == {
        "runs": 3,
        "succeeded": 0,
        "failed": 0,
        "refused": 3,
        "budget_exceeded": 0,
        "in_flight": 0,
        "last_succeeded_at": None,
        "top_refusal_reason": "precondition_unsatisfied",
        "linked_outcomes": None,
    }


def test_top_refusal_reason_is_the_mode_and_ignores_unclassified_history(
    procedure_instance: CruxibleInstance,
) -> None:
    """Pre-migration rows carry no bucket and must not outvote classified ones."""
    procedure = service_propose_procedure(
        procedure_instance,
        provider_definition("refusing_procedure"),
        actor_context=actor("refuse-proposer"),
    ).procedure
    reasons = [
        "precondition_unsatisfied",
        "precondition_unsatisfied",
        "procedure_not_live",
        # Five historical refusals with no recorded bucket: the majority of the
        # rows, and still not an answer.
        None,
        None,
        None,
        None,
        None,
    ]
    with procedure_instance.write_transaction() as uow:
        for index, reason in enumerate(reasons):
            uow.procedures.save_run(
                _finalized_run(
                    run_id=f"PRN-refuse-{index}",
                    procedure_id=procedure.procedure_id,
                    definition_digest=procedure.definition_digest,
                    verdict="refused",
                    finalized_at=f"2026-07-22T17:{index:02d}:00Z",
                    refusal_reason=reason,
                )
            )

    store = procedure_instance.get_procedure_store()
    try:
        records = store.get_run_track_records([procedure.procedure_id])
    finally:
        store.close()

    assert records[procedure.procedure_id].refused == 8
    assert records[procedure.procedure_id].top_refusal_reason == "precondition_unsatisfied"


def test_unrecognized_refusal_bucket_degrades_instead_of_failing_the_read(
    procedure_instance: CruxibleInstance,
) -> None:
    """A bucket written by a newer version must not break the procedure listing."""
    procedure = service_propose_procedure(
        procedure_instance,
        provider_definition("foreign_bucket_procedure"),
        actor_context=actor("foreign-proposer"),
    ).procedure
    with procedure_instance.write_transaction() as uow:
        for index in range(4):
            uow.procedures.save_run(
                _finalized_run(
                    run_id=f"PRN-foreign-{index}",
                    procedure_id=procedure.procedure_id,
                    definition_digest=procedure.definition_digest,
                    verdict="refused",
                    finalized_at=f"2026-07-22T19:0{index}:00Z",
                    refusal_reason="procedure_not_live",
                )
            )
    # A bucket only a later version knows how to name, and the MAJORITY one, so
    # skipping it is what produces the answer rather than tie-breaking around
    # it. Written straight to the column: the model validator would reject it.
    conn = sqlite3.connect(procedure_instance.instance_dir / "state.db")
    try:
        conn.execute(
            "UPDATE procedure_runs SET refusal_reason = 'reason_from_the_future' "
            "WHERE run_id IN ('PRN-foreign-0', 'PRN-foreign-1', 'PRN-foreign-2')"
        )
        conn.commit()
    finally:
        conn.close()

    store = procedure_instance.get_procedure_store()
    try:
        records = store.get_run_track_records([procedure.procedure_id])
    finally:
        store.close()

    assert records[procedure.procedure_id].refused == 4
    assert records[procedure.procedure_id].top_refusal_reason == "procedure_not_live"


def test_run_refusal_reason_is_recorded_at_finalization(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bucket comes from the branch that refused, end to end."""
    definition = provider_definition(
        "requires_ready_task_for_track_record",
        precondition={"entity_type": "Task", "condition": {"status": "ready"}},
    )
    procedure_id = _accept(procedure_instance, definition)
    _stub_provider(monkeypatch, lambda payload: payload)

    with pytest.raises(ConfigError, match="precondition was unsatisfied") as exc_info:
        service_run_procedure(procedure_instance, procedure_id, {"value": 1}, actor("runner"))

    run = _run(procedure_instance, getattr(exc_info.value, "procedure_run_id"))
    assert run.verdict == "refused"
    assert run.refusal_reason == "precondition_unsatisfied"

    shown = service_get_procedure(procedure_instance, procedure_id)
    assert shown.track_record.refused == 1
    assert shown.track_record.top_refusal_reason == "precondition_unsatisfied"


def test_aggregation_chunks_id_lists_past_the_sqlite_parameter_cap(
    procedure_instance: CruxibleInstance,
) -> None:
    """An id list larger than one statement's chunk still aggregates.

    SQLite's host-parameter cap is a hard error, not a slow path, so a page
    wider than one chunk has to survive as a normal read.
    """
    procedure = service_propose_procedure(
        procedure_instance,
        provider_definition("chunked_procedure"),
        actor_context=actor("chunk-proposer"),
    ).procedure
    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_run(
            _finalized_run(
                run_id="PRN-chunk-0",
                procedure_id=procedure.procedure_id,
                definition_digest=procedure.definition_digest,
                verdict="succeeded",
                finalized_at="2026-07-22T18:00:00Z",
            )
        )

    absent_ids = [
        f"PRC-absent-{index:05d}" for index in range(_MAX_ID_PARAMETERS_PER_STATEMENT * 2)
    ]
    store = procedure_instance.get_procedure_store()
    try:
        records = store.get_run_track_records([*absent_ids, procedure.procedure_id])
    finally:
        store.close()

    assert set(records) == {procedure.procedure_id}
    assert records[procedure.procedure_id].succeeded == 1


def test_track_record_rejects_buckets_that_lose_runs() -> None:
    with pytest.raises(ValueError, match="must cover every run"):
        ProcedureTrackRecord(runs=3, succeeded=1)


def test_run_ledger_writes_advance_the_read_revision_once_each(
    procedure_instance: CruxibleInstance,
) -> None:
    """Starting and finalizing a run each move the freshness counter exactly once.

    The run ledger used to be classified audit-only in ``_AUDIT_ONLY_TABLES``,
    which was defensible while runs were only readable through their own
    dedicated listing. It is not defensible now that ``procedure list``/``get``
    derive visible ``track_record`` buckets from the same rows: a revision-silent
    run write would let a page be read at revision N, a run land, and the next
    page's continuation token still validate against an unchanged counter --
    a paginated read spanning two different states with nothing to detect it,
    and a working-set record that reads fresh while its buckets are stale.
    """
    procedure = service_propose_procedure(
        procedure_instance,
        provider_definition("revision_tracked_procedure"),
        actor_context=actor("revision-proposer"),
    ).procedure

    before = procedure_instance.get_read_revision()

    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_run(
            ProcedureRun(
                run_id="PRN-revision-0",
                procedure_id=procedure.procedure_id,
                definition_digest=procedure.definition_digest,
            )
        )
    started_revision = procedure_instance.get_read_revision()
    assert started_revision == before + 1

    started_record = service_get_procedure(procedure_instance, procedure.procedure_id).track_record
    assert started_record.model_dump(mode="json") == {
        "runs": 1,
        "succeeded": 0,
        "failed": 0,
        "refused": 0,
        "budget_exceeded": 0,
        "in_flight": 1,
        "last_succeeded_at": None,
        "top_refusal_reason": None,
        "linked_outcomes": None,
    }

    with procedure_instance.write_transaction() as uow:
        assert uow.procedures.finalize_run(
            run_id="PRN-revision-0",
            verdict="succeeded",
            budget_spent=ProcedureBudgetSpent(wall_clock_s=0.5, provider_calls=1),
            receipt_id="RCP-revision-0",
            finalized_at="2026-07-23T09:00:00Z",
        )
    finalized_revision = procedure_instance.get_read_revision()
    assert finalized_revision == started_revision + 1

    finalized = service_get_procedure(procedure_instance, procedure.procedure_id)
    # The counter moved because the VISIBLE buckets moved, not merely because a
    # row changed: pin both halves together or the assertion above degenerates
    # into "some write happened".
    assert finalized.track_record.model_dump(mode="json") == {
        "runs": 1,
        "succeeded": 1,
        "failed": 0,
        "refused": 0,
        "budget_exceeded": 0,
        "in_flight": 0,
        "last_succeeded_at": "2026-07-23T09:00:00Z",
        "top_refusal_reason": None,
        "linked_outcomes": None,
    }

    # And the reads that display the track record are still reads.
    listed = service_list_procedures(procedure_instance)
    assert listed.read_revision == finalized_revision
    service_get_procedure(procedure_instance, procedure.procedure_id)
    assert procedure_instance.get_read_revision() == finalized_revision


def test_a_refused_invocation_advances_the_revision_for_start_and_finalize(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end, through the service, not by writing run rows by hand.

    A precondition refusal writes the ledger twice -- the crash-visible started
    run, then the finalize inside the authorization transaction -- and changes
    nothing else, so it pins the count exactly rather than as an inequality.
    """
    definition = provider_definition(
        "revision_refusing_procedure",
        precondition={"entity_type": "Task", "condition": {"status": "ready"}},
    )
    procedure_id = _accept(procedure_instance, definition)
    _stub_provider(monkeypatch, lambda payload: payload)

    before = procedure_instance.get_read_revision()

    with pytest.raises(ConfigError, match="precondition was unsatisfied"):
        service_run_procedure(procedure_instance, procedure_id, {"value": 1}, actor("runner"))

    assert procedure_instance.get_read_revision() == before + 2
    shown = service_get_procedure(procedure_instance, procedure_id)
    assert shown.track_record.runs == 1
    assert shown.track_record.refused == 1
    assert shown.track_record.in_flight == 0
