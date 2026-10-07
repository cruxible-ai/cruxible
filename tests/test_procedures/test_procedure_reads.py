"""get is the one Procedure read: runnability, retired Procedures, and open windows."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.get_reads import GetProcedureCard, GetRequest
from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
from cruxible_client.contracts.procedures.readings import ProcedureReadingsRequest
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.procedures.measurements import (
    service_list_playbill_procedure_readings,
)
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRetired,
    ProcedureRunRequest,
    service_run_playbill_procedure,
)
from tests.test_procedures.test_procedure_measurement_readings import (
    OBSERVE_AT,
    RUN_TIME,
    _measure,
    _run,
)
from tests.test_procedures.test_procedure_measurement_readings import _world as measured_world
from tests.test_procedures.test_procedure_owned_contracts import _activate_procedure
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor, _world

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


def _card(instance, name: str) -> GetProcedureCard:  # type: ignore[no-untyped-def]
    card = service_playbill_get(
        instance,
        request=GetRequest(ref=f"Procedure:{name}", evaluation_time=datetime.now(UTC)),
        access=_ACCESS,
    ).card
    assert isinstance(card, GetProcedureCard)
    return card


def test_the_procedure_card_answers_how_it_runs_and_says_how_to_run_it(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)

    card = _card(instance, procedure.identity.name)

    assert card.lifecycle == "live"
    assert card.runnable == "direct" and card.unsupported_nodes == ()
    assert (
        card.next[0] == f'cruxible_procedure_run(name="{procedure.identity.name}", input={{...}})'
    )


def test_a_retired_procedure_reads_through_get_but_nothing_runs_or_measures_it(
    tmp_path: Path,
) -> None:
    instance, owner, procedure = _world(tmp_path)
    retired = procedure.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=procedure_artifact_digest(procedure).tagged
            )
        }
    )
    _activate_procedure(
        instance, owner, retired, sequence=5, timestamp="2026-08-24T15:30:00.000000Z"
    )

    card = _card(instance, procedure.identity.name)
    assert card.lifecycle == "retired"
    # No run step is offered for a Procedure nothing may run.
    assert all("procedure_run" not in step for step in card.next)
    with pytest.raises(ProcedureRetired):
        service_run_playbill_procedure(
            instance,
            name=procedure.identity.name,
            request=ProcedureRunRequest(evaluation_time=READ_TIME, input={}),
            actor_context=_actor(instance),
        )


def test_readings_report_an_unresolved_open_window_as_open(tmp_path: Path) -> None:
    instance, _owner, procedure = measured_world(tmp_path)
    _run(instance, procedure, at=RUN_TIME)

    before = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=ProcedureReadingsRequest(),
        evaluation_time=OBSERVE_AT,
    )
    # Before this fix an open, unresolved window read "expired".
    statuses = {row.measurement_name: row.status for row in before.contracts}
    assert statuses["rows-present"] == "open"

    _measure(instance, procedure, names=("rows-present",))
    after = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=ProcedureReadingsRequest(measurement_names=("rows-present",)),
        evaluation_time=OBSERVE_AT,
    )
    assert {row.status for row in after.contracts} == {"resolved"}


def test_measure_and_readings_refuse_a_retired_procedure(tmp_path: Path) -> None:
    instance, owner, procedure = measured_world(tmp_path)
    retired = procedure.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=procedure_artifact_digest(procedure).tagged
            )
        }
    )
    _activate_procedure(
        instance, owner, retired, sequence=9, timestamp="2026-08-24T15:30:00.000000Z"
    )
    with pytest.raises(ProcedureRetired):
        _measure(instance, procedure)
    with pytest.raises(ProcedureRetired):
        service_list_playbill_procedure_readings(
            instance,
            name=procedure.identity.name,
            request=ProcedureReadingsRequest(),
            evaluation_time=OBSERVE_AT,
        )
