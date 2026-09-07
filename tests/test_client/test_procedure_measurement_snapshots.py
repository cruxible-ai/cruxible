"""Measurement SDK coordinate composition, using only an in-memory transport spy."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_client import Playbill
from cruxible_client import contracts as api
from cruxible_client.authoring.sdk import ProcedureRun
from cruxible_client.authoring.sdk_types import ProcedureRef
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.projection import AcceptedCoordinate

from .test_playbill_sdk_snapshots import _LiveClient
from .test_playbill_sdk_world import _COORDINATE, _MOVED_COORDINATE

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
OLD = AcceptedCoordinate.model_validate(_COORDINATE.model_dump())
NEW = AcceptedCoordinate.model_validate(_MOVED_COORDINATE.model_dump())


class MeasurementClient(_LiveClient):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.wrong_coordinate = False
        self.first_page = None

    def _account(self, request):
        coordinate = request.at or AcceptedCoordinate.model_validate(self.coordinate.model_dump())
        return {
            "procedure_identity": ArtifactIdentity(kind="Procedure", name="daily-summary"),
            "procedure_artifact_digest": "sha256:" + "1" * 64,
            "activation_coordinate": OLD,
            "observation_coordinate": NEW if self.wrong_coordinate else coordinate,
            "observation_time": request.evaluation_time,
        }

    def measure_playbill_procedure(self, instance, name, *, request):
        self.requests.append(request)
        return api.PlaybillProcedureMeasureResultV1(
            **self._account(request), run_id=request.run_id, rows=()
        )

    def list_playbill_procedure_readings(self, instance, name, *, request):
        self.requests.append(request)
        if request.cursor:
            assert request.cursor == "first-page-cursor"
            return self.first_page.model_copy(update={"truncated": False, "cursor": None})
        self.first_page = api.PlaybillProcedureReadingsResultV1(
            **self._account(request),
            contracts=(),
            readings=(),
            truncated=True,
            cursor="first-page-cursor",
        )
        return self.first_page


@pytest.fixture
def connection(tmp_path: Path):
    client = MeasurementClient()
    clock = [NOW]
    pb = Playbill._from_client(
        client, instance_id="inst_world", workspace=tmp_path, clock=lambda: clock[0]
    )
    return pb, client, clock


@pytest.mark.parametrize("operation", ["measure", "readings"])
def test_live_calls_follow_head_and_remember_observation_without_extra_lookup(
    connection, operation
):
    pb, client, clock = connection
    searches = len(client.searches)
    procedure = pb.accepted_procedure("daily-summary")
    getattr(procedure, operation)(measurements=["b", "a", "b"])
    client.coordinate = _MOVED_COORDINATE
    clock[0] += timedelta(seconds=1)
    getattr(procedure, operation)()
    assert [request.at for request in client.requests] == [None, None]
    assert client.requests[0].measurement_names == ("a", "b")
    assert client.requests[1].evaluation_time == clock[0]
    assert pb.coordinate == NEW
    assert len(client.searches) == searches


@pytest.mark.parametrize("operation", ["measure", "readings"])
@pytest.mark.parametrize("pin", ["context", "reference", "argument"])
def test_explicit_snapshots_are_honored(connection, operation, pin):
    pb, client, _ = connection
    client.coordinate = _MOVED_COORDINATE
    context = pb.at(OLD) if pin == "context" else pb
    reference = ProcedureRef("daily-summary", OLD) if pin == "reference" else "daily-summary"
    procedure = context.accepted_procedure(reference)
    result = getattr(procedure, operation)(**({"at": OLD} if pin == "argument" else {}))
    assert client.requests[-1].at == OLD
    assert result.observation_coordinate == OLD
    assert context.coordinate == OLD


@pytest.mark.parametrize("operation", ["measure", "readings"])
def test_conflicting_pin_and_explicit_coordinate_refuse_before_transport(connection, operation):
    pb, client, _ = connection
    procedure = pb.accepted_procedure(ProcedureRef("daily-summary", OLD))
    with pytest.raises(ValueError, match="pinned Procedure"):
        getattr(procedure, operation)(at=NEW)
    assert client.requests == []


@pytest.mark.parametrize("operation", ["measure", "readings"])
def test_mismatched_response_does_not_move_context(connection, operation):
    pb, client, _ = connection
    client.wrong_coordinate = True
    with pytest.raises(ValueError, match="different requested coordinate"):
        getattr(pb.accepted_procedure("daily-summary"), operation)(at=OLD)
    assert pb.coordinate == OLD


def test_live_cursor_continuation_keeps_first_pages_selection(connection):
    pb, client, clock = connection
    procedure = pb.accepted_procedure("daily-summary")
    first = procedure.readings(limit=1)
    client.coordinate = _MOVED_COORDINATE
    clock[0] += timedelta(hours=1)
    second = procedure.readings(limit=1, cursor=first.cursor)
    assert [request.at for request in client.requests] == [None, None]
    assert client.requests[-1].cursor == first.cursor
    assert client.requests[-1].evaluation_time == clock[0]
    assert second.observation_time == first.observation_time == NOW
    assert second.observation_coordinate == pb.coordinate == OLD


def _run(context, run_id="RUN-test"):
    return ProcedureRun(
        context,
        api.PlaybillProcedureRunState(
            run_id=run_id,
            procedure_identity={"kind": "Procedure", "name": "daily-summary"},
            procedure_artifact_digest="sha256:" + "1" * 64,
            bound_coordinate=_COORDINATE,
            head_at_admission=_COORDINATE,
            lane="current",
            evaluation_time=NOW.isoformat(),
            status="succeeded",
            pending_inputs=[],
            outcomes=[],
            next_operation={},
        ),
    )


@pytest.mark.parametrize("pinned", [False, True])
def test_run_measure_keeps_observation_context_distinct_from_run_admission(connection, pinned):
    pb, client, _ = connection
    context = pb.at(OLD) if pinned else pb
    run = _run(context)
    client.coordinate = _MOVED_COORDINATE
    result = run.measure(measurements=["b", "a"])
    assert client.requests[-1].at == (OLD if pinned else None)
    assert client.requests[-1].run_id == run.run_id
    assert result.observation_coordinate == (OLD if pinned else NEW)
    assert context.coordinate == result.observation_coordinate
    assert run.coordinate == OLD


def test_admission_refused_run_cannot_create_measurement_request(connection):
    pb, client, _ = connection
    with pytest.raises(ValueError, match="without a run_id"):
        _run(pb, run_id=None).measure()
    assert client.requests == []
