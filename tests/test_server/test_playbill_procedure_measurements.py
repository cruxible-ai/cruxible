"""The measurement doors reach the wire: real route, real service, typed refusals."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import Playbill
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.readings import (
    PlaybillProcedureMeasureResultV1,
    PlaybillProcedureReadingsResultV1,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.governance.keys import GeneratedKeyMaterial, generate_client_principal_key
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from tests.core_support._knowledge_loop_support import seed_claims_into
from tests.test_procedures.test_procedure_measurement_readings import _world_on as measured_world_on
from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
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


@pytest.fixture
def owned_playbill_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, Path]]:
    """The shared HTTP host, plus the ``owner`` principal the knowledge-loop seeds act as."""

    state = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    registered = get_registry().create_governed_instance_with_id("inst_playbill_measured")
    instance_id = registered.record.instance_id
    managed = Path(registered.record.location)
    # The knowledge-loop seeds sign with the bootstrap custody the local
    # fixtures keep beside the instance; the reviewer key is minted there.
    principals = [
        generate_client_principal_key(
            (
                managed.parent / f"client-custody-{principal_id}"
                if principal_id == "reviewer"
                else tmp_path / f"{principal_id}-custody"
            ),
            principal_id=principal_id,
            kind="ordinary",
            forbidden_roots=(managed,),
        )
        for principal_id in ("operator", "owner", "reviewer")
    ]
    with TestClient(create_app()) as client:
        initialized = client.post(
            f"/api/v1/{instance_id}/playbill/init",
            json={
                "principals": [item.principal.model_dump(mode="json") for item in principals],
                "seed": False,
            },
        )
        assert initialized.status_code == 200, initialized.text
        yield client, instance_id, principals[2].private_key_path
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def test_a_nonempty_loop_runs_measures_retries_and_pages_over_the_wire(
    owned_playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    """The customer loop, on real doors: run, measure, retry, page, from HTTP and the SDK."""

    client, instance_id, reviewer_key_path = owned_playbill_http
    instance = get_playbill_manager().get(instance_id)
    reviewer = instance._recovered.head.principals.require_active("reviewer")  # noqa: SLF001
    material = GeneratedKeyMaterial(
        principal=reviewer,
        private_key_path=reviewer_key_path,
        public_key_path=reviewer_key_path.with_suffix(".pub"),
    )
    seed_claims_into(instance, material)
    _instance, _owner, procedure = measured_world_on(instance, material)
    name = procedure.identity.name
    base = f"/api/v1/{instance_id}/playbill/procedures/{name}"

    def run(minute: int) -> str:
        response = client.post(
            f"{base}/runs",
            json={
                "tag": "playbill-procedure-run-request-v2",
                "evaluation_time": f"2026-08-24T16:{minute:02d}:00Z",
                "input": {},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "succeeded", response.text
        return str(response.json()["run_id"])

    def measure(run_id: str, minute: int) -> dict[str, object]:
        response = client.post(
            f"{base}/measurements",
            json={
                "tag": "playbill-procedure-measure-request-v1",
                "run_id": run_id,
                "measurement_names": ["hot-claim", "rows-present"],
                "evaluation_time": f"2026-08-24T16:{minute:02d}:00Z",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        return {row["measurement_name"]: row for row in body["rows"]}

    first_run = run(30)
    first = measure(first_run, 31)
    assert first["rows-present"]["reading_status"] == "recorded"  # type: ignore[index]
    assert first["hot-claim"]["reading_status"] == "recorded"  # type: ignore[index]
    assert first["hot-claim"]["resolution"]["value"] == "supported"  # type: ignore[index]
    # A second authenticated request mints fresh attribution; it replays.
    retried = measure(first_run, 32)
    assert retried["rows-present"]["reading_status"] == "replayed"  # type: ignore[index]
    assert (
        retried["rows-present"]["reading"]["reading_id"]  # type: ignore[index]
        == first["rows-present"]["reading"]["reading_id"]  # type: ignore[index]
    )
    second_run = run(33)
    assert measure(second_run, 34)["rows-present"]["reading_status"] == "recorded"  # type: ignore[index]

    # Paged readback over HTTP with a limit below the retained count.
    page = client.post(
        f"{base}/readings",
        json={"tag": "playbill-procedure-readings-request-v1", "limit": 3},
    )
    assert page.status_code == 200, page.text
    first_page = PlaybillProcedureReadingsResultV1.model_validate(page.json())
    assert len(first_page.readings) == 3 and first_page.truncated and first_page.cursor
    rest = client.post(
        f"{base}/readings",
        json={
            "tag": "playbill-procedure-readings-request-v1",
            "limit": 3,
            "cursor": first_page.cursor,
        },
    )
    assert rest.status_code == 200, rest.text
    second_page = PlaybillProcedureReadingsResultV1.model_validate(rest.json())
    assert len(second_page.readings) == 1 and not second_page.truncated
    assert second_page.observation_time == first_page.observation_time
    assert len({row.reading_id for row in (*first_page.readings, *second_page.readings)}) == 4

    # The SDK over the same wire, with the ordinary moving clock, continues
    # the same page and re-measures through its own door.
    ticks = iter(range(35, 60))
    sdk_client = CruxibleClient(base_url="http://testserver")
    sdk_client._client._client = client  # noqa: SLF001
    workspace = tmp_path / "sdk-workspace"
    workspace.mkdir()
    pb = Playbill._from_client(  # noqa: SLF001
        sdk_client,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 8, 24, 16, next(ticks), tzinfo=UTC),
    )
    handle = pb.accepted_procedure(name)
    sdk_first = handle.readings(limit=3)
    assert sdk_first.truncated and sdk_first.cursor
    sdk_rest = handle.readings(limit=3, cursor=sdk_first.cursor)
    assert len(sdk_rest.readings) == 1 and not sdk_rest.truncated
    assert sdk_rest.observation_time == sdk_first.observation_time
    batch = handle.measure(run=second_run, measurements=("rows-present",))
    assert batch["rows-present"].reading_status == "replayed"
