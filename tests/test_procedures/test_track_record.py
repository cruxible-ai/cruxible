"""Procedure read-surface track-record coverage."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from cruxible_core.procedure.store import ProcedureStore
from cruxible_core.procedure.types import ProcedureRun, ProcedureTrackRecord
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import (
    service_get_procedure,
    service_list_procedures,
    service_propose_procedure,
)
from tests.test_procedures.conftest import actor, provider_definition


def _finalized_run(
    *,
    run_id: str,
    procedure_id: str,
    definition_digest: str,
    verdict: str,
    finalized_at: str,
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
        "runs": 5,
        "succeeded": 2,
        "failed": 1,
        "refused": 1,
        "last_succeeded_at": "2026-07-22T13:03:00Z",
        "top_refusal_reason": None,
        "linked_outcomes": None,
    }

    shown = service_get_procedure(procedure_instance, tracked.procedure_id)

    assert aggregation_calls[1] == (tracked.procedure_id,)
    assert shown.track_record == by_id[tracked.procedure_id]
