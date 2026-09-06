"""The measurement doors reach the wire: real route, real service, typed refusals."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.readings import PlaybillProcedureMeasureResultV1
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.test_playbill.test_procedure_run_surface import _slotless_procedure
from tests.test_server.test_playbill_line_run_refusals import _accept_members


def test_measure_and_readings_routes_serve_a_real_run_and_typed_refusals(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, reviewer_key_path = playbill_http
    instance = get_playbill_manager().get(instance_id)
    accepted = _slotless_procedure("served-measured")
    _accept_members(
        instance,
        reviewer_key_path,
        {accepted.path: render_procedure(accepted.procedure)},
        timestamp="2026-09-03T09:00:00.000000Z",
    )
    name = accepted.procedure.identity.name

    run = client.post(
        f"/api/v1/{instance_id}/playbill/procedures/{name}/runs",
        json={
            "tag": "playbill-procedure-run-request-v2",
            "evaluation_time": "2026-09-03T10:00:00Z",
            "input": {"status": "ok"},
        },
    )
    assert run.status_code == 200, run.text
    run_id = run.json()["run_id"]
    assert run_id is not None

    # No declaration: the fast path answers with no rows and writes nothing.
    measured = client.post(
        f"/api/v1/{instance_id}/playbill/procedures/{name}/measurements",
        json={
            "tag": "playbill-procedure-measure-request-v1",
            "run_id": run_id,
            "evaluation_time": "2026-09-03T10:05:00Z",
        },
    )
    assert measured.status_code == 200, measured.text
    body = measured.json()
    assert body["run_id"] == run_id and body["rows"] == []
    parsed = PlaybillProcedureMeasureResultV1.model_validate(body)
    assert parsed.observation_time == datetime(2026, 9, 3, 10, 5, tzinfo=UTC)

    # An undeclared name is a typed request fault carrying a runnable repair.
    refused = client.post(
        f"/api/v1/{instance_id}/playbill/procedures/{name}/measurements",
        json={
            "tag": "playbill-procedure-measure-request-v1",
            "measurement_names": ["nope"],
        },
    )
    assert refused.status_code == 400, refused.text
    envelope = refused.json()
    assert envelope["error_code"] == "measurement_not_declared"
    assert envelope["repair"]["operation"] == "playbill.procedure.readings"

    listed = client.post(
        f"/api/v1/{instance_id}/playbill/procedures/{name}/readings",
        json={"tag": "playbill-procedure-readings-request-v1", "run_id": run_id, "limit": 5},
    )
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["contracts"] == [] and page["readings"] == []
    assert page["truncated"] is False and page["cursor"] is None

    absent = client.post(
        f"/api/v1/{instance_id}/playbill/procedures/absent-procedure/readings",
        json={"tag": "playbill-procedure-readings-request-v1"},
    )
    assert absent.status_code == 404, absent.text
