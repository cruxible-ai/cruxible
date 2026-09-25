"""The prediction worker owes a settlement exactly for closed, unanswered bound windows."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidenceV2,
    PlaybillSettleRequestV2,
    PredictionEqualityRuleV1,
    PredictionObservationSelectorV1,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelectorV1,
    CaptureEventWindowV1,
    TriggerEventReferenceV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.resolution_contracts import (
    ResolutionContractReferenceV1,
    ResolutionContractV1,
    render_resolution_contract,
    resolution_contract_digest,
    resolution_contract_path,
)
from cruxible_core.consumers import predictions
from cruxible_core.consumers.predictions import PREDICTION_SETTLEMENT as WORKER
from cruxible_core.consumers.predictions import (
    UNBINDABLE_RETRY,
    settleable_windows,
    unbindable_anchors,
)
from tests.core_support._knowledge_loop_support import subject_address
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.p2b5 import test_served_predictions as served

SELECTOR = CaptureEventSelectorV1(
    capture_contract_identity=ArtifactIdentity(kind="CaptureContract", name="anchor"),
    capture_contract_digest="sha256:" + "a" * 64,
)
#: The fixed prediction's window: [12:01, 13:01] on the served test's day.
FIXED_CLOSES = served.PREDICTED_AT + timedelta(hours=1)


def drain(instance, *, now: datetime) -> None:  # type: ignore[no-untyped-def]
    """Match, then run every unit of due work until none is left."""

    manager = SimpleNamespace(get=lambda _id: instance)
    for _pass in range(16):
        WORKER.match(instance, now=now, daemon_id="daemon")
        work = tuple(WORKER.due(instance, now=now))
        if not work:
            return
        for item in work:
            WORKER.run(manager, "instance", item, now=now)
    raise AssertionError("the worker never ran out of due work")


def fixed_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    """An accepted hypothesis and a fixed-window contract testing it."""

    instance, owner, capture = served._world(tmp_path)
    contract = served._predict(instance, owner, capture)
    return instance, owner, capture, contract


def observe(instance, owner, capture, *, at: str):  # type: ignore[no-untyped-def]
    return served._accept_payload(
        instance,
        owner,
        served._payload(capture, qualifier="prediction-outcome", value="ready"),
        at,
    )


def settle(instance, contract, observation, *, event=None):  # type: ignore[no-untyped-def]
    return served.service_settle_playbill_prediction(
        instance,
        prediction_id=contract.identity.name,
        request=PlaybillSettleRequestV2(
            contract=contract,
            trigger_event=event,
            evidence=ObservationSettlementEvidenceV2(claim=observation),
        ),
        actor_context=served._actor(),
        recorded_at=served.RECORDED_AT + timedelta(days=1),
    )


def overturn(instance, result) -> None:  # type: ignore[no-untyped-def]
    from cruxible_core.exhaust.writer import ProcedureExhaustWriter
    from cruxible_core.procedures.resolution import (
        ProcedureResolutionV3,
        ResolutionContractActivationV3,
        append_resolution_disposition,
        build_resolution_disposition,
        resolution_contract_partition_id,
    )

    activation = ResolutionContractActivationV3.model_validate(result.activation)
    resolution = ProcedureResolutionV3.model_validate(result.resolution)
    journal, stream = served._journal(instance)
    partition = resolution_contract_partition_id(activation)
    journal.activate_writer(
        stream,
        partition,
        fencing_token="overturn",
        expected_head=journal.read_head(stream, partition),
    )
    append_resolution_disposition(
        ProcedureExhaustWriter(
            journal=journal, bodies=instance.body_store(), fencing_token="overturn"
        ),
        activation=activation,
        resolution=resolution,
        disposition=build_resolution_disposition(
            resolution,
            sequence=1,
            verdict="overturned",
            reviewer_actor_context=served._actor("reviewer"),
            recorded_at=served.RECORDED_AT + timedelta(days=2),
        ),
        stream=stream,
    )


def land(instance, *, at: datetime, run: str) -> TriggerEventReferenceV1:  # type: ignore[no-untyped-def]
    """One produced Capture landing whose contract the event selector names."""

    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
    from cruxible_core.exhaust.writer import ProcedureExhaustWriter
    from cruxible_core.service.procedures.procedure_runs import (
        PROCEDURE_RUN_FENCING_TOKEN,
        _activate_writer,
        _journal_for_write,
        _stream,
    )
    from tests.test_procedures.test_procedure_run_surface import _actor, _slotless_procedure

    procedure = _slotless_procedure("capture-anchor").procedure
    journal, _ = _journal_for_write(instance)
    stream = _stream(instance)
    partition = f"run:{run}"
    _activate_writer(journal, stream, partition)
    stored = ProcedureExhaustWriter(
        journal=journal, bodies=instance.body_store(), fencing_token=PROCEDURE_RUN_FENCING_TOKEN
    ).append(
        stream=stream,
        partition_id=partition,
        event_kind="produced_capture",
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        procedure_artifact_digest=procedure_artifact_digest(procedure).tagged,
        definition_digest=procedure.definition_digest,
        actor_context=_actor(instance),
        recorded_at=at,
        run_id=f"RUN-{run}",
        payload={
            "tag": "playbill-procedure-produced-capture-v1",
            "capture_contract_digest": SELECTOR.capture_contract_digest,
            "observed_at": "2000-01-01T00:00:00Z",
        },
    )
    return TriggerEventReferenceV1(
        run_id=f"RUN-{run}",
        partition_id=partition,
        sequence=stored.record.sequence,
        record_digest=stored.record_digest,
    )


def accept_event_contract(instance, owner, capture, *, name: str = "event-test"):  # type: ignore[no-untyped-def]
    """An event-window contract over the served world's hypothesis, accepted at 12:01."""

    hypothesis = served._accept_payload(
        instance,
        owner,
        served._payload(capture, qualifier="prediction", value="ready"),
        "2026-09-02T12:00:45.000000Z",
    )
    contract = ResolutionContractV1(
        identity=ArtifactIdentity(kind="ResolutionContract", name=name),
        hypothesis=hypothesis,
        observation=PredictionObservationSelectorV1(
            subject=subject_address("wi-42"),
            predicate=served.PREDICATE,
            qualifier="prediction-outcome",
        ),
        rule=PredictionEqualityRuleV1(),
        window=CaptureEventWindowV1(event=SELECTOR, duration_seconds=3600),
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[resolution_contract_path(name)] = render_resolution_contract(contract)
    _accept_tree(instance, owner, tree, timestamp="2026-09-02T12:01:00.000000Z", proposal_name=name)
    return contract, ResolutionContractReferenceV1(
        identity=contract.identity,
        artifact_digest=resolution_contract_digest(contract).tagged,
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )


def _windows(instance) -> dict[str, str]:  # type: ignore[no-untyped-def]
    with sqlite3.connect(predictions._root(instance) / "state.sqlite3") as connection:
        return dict(connection.execute("SELECT contract_id,status FROM windows").fetchall())


def test_a_fixed_window_is_owed_once_closed_until_settled_and_again_once_overturned(
    tmp_path: Path,
) -> None:
    instance, owner, capture, contract = fixed_world(tmp_path)
    drain(instance, now=FIXED_CLOSES - timedelta(minutes=30))
    (status,) = _windows(instance).values()
    assert status == "open" and settleable_windows(instance) == ()

    drain(instance, now=FIXED_CLOSES)
    (owed,) = settleable_windows(instance)
    assert owed.contract == contract.model_copy(update={"coordinate": owed.contract.coordinate})
    assert owed.hypothesis.startswith("Claim:")
    assert owed.window.ends_at == FIXED_CLOSES and owed.window.event is None

    observation = observe(instance, owner, capture, at="2026-09-02T12:02:00.000000Z")
    result = settle(instance, contract, observation)
    assert result.activation["contract_id"] == owed.bound_contract_id
    drain(instance, now=FIXED_CLOSES + timedelta(minutes=1))
    assert settleable_windows(instance) == ()
    assert set(_windows(instance).values()) == {"resolved"}

    overturn(instance, result)
    drain(instance, now=FIXED_CLOSES + timedelta(minutes=2))
    (again,) = settleable_windows(instance)
    assert again.bound_contract_id == owed.bound_contract_id


def test_each_matching_landing_binds_its_own_window_and_contract_instance(
    tmp_path: Path,
) -> None:
    instance, owner, capture = served._world(tmp_path)
    contract, reference = accept_event_contract(instance, owner, capture)
    drain(instance, now=served.PREDICTED_AT)  # first start, nothing landed yet
    assert _windows(instance) == {}

    first = land(instance, at=served.PREDICTED_AT + timedelta(minutes=1), run="one")
    second = land(instance, at=served.PREDICTED_AT + timedelta(minutes=30), run="two")
    drain(instance, now=served.PREDICTED_AT + timedelta(minutes=31))
    assert list(_windows(instance).values()) == ["open", "open"]

    drain(instance, now=served.PREDICTED_AT + timedelta(minutes=61, seconds=1))
    (early,) = settleable_windows(instance)
    assert early.window.event == first
    assert early.window.ends_at == served.PREDICTED_AT + timedelta(minutes=61)
    drain(instance, now=served.PREDICTED_AT + timedelta(minutes=91))
    owed = settleable_windows(instance)
    assert [item.window.event for item in owed] == [first, second]
    assert len({item.bound_contract_id for item in owed}) == 2

    observation = observe(instance, owner, capture, at="2026-09-02T12:35:00.000000Z")
    result = settle(instance, reference, observation, event=second)
    assert result.activation["contract_id"] == owed[1].bound_contract_id
    drain(instance, now=served.PREDICTED_AT + timedelta(minutes=92))
    (remaining,) = settleable_windows(instance)
    assert remaining.window.event == first


def test_a_new_worker_catches_up_on_contracts_and_landings_it_never_saw(
    tmp_path: Path,
) -> None:
    instance, owner, capture = served._world(tmp_path)
    accept_event_contract(instance, owner, capture)
    first = land(instance, at=served.PREDICTED_AT + timedelta(minutes=1), run="one")
    second = land(instance, at=served.PREDICTED_AT + timedelta(minutes=2), run="two")

    drain(instance, now=served.PREDICTED_AT + timedelta(hours=2))

    assert [item.window.event for item in settleable_windows(instance)] == [first, second]


def unbindable_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    """An event contract whose one matching landing lost its payload after it was indexed.

    Returns the instance, the anchor event, and a callable that restores the payload.
    """

    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    instance, owner, capture = served._world(tmp_path)
    event = land(instance, at=served.PREDICTED_AT + timedelta(minutes=1), run="one")
    journal, _ = _journal(instance)
    (stored,) = journal.select_records(
        _stream(instance), partition_id=event.partition_id, first_sequence=event.sequence
    )
    journal.index.captures(
        _stream(instance),
        bodies=instance.body_store(),
        contract_digest=SELECTOR.capture_contract_digest,
        since=None,
        until=served.PREDICTED_AT + timedelta(hours=1),
        limit=1,
    )
    payload = instance.body_store()._path(stored.record.payload_digest)
    original = payload.read_bytes()
    payload.unlink()
    accept_event_contract(instance, owner, capture)
    return instance, event, lambda: payload.write_bytes(original)


def test_an_anchor_whose_material_is_gone_is_a_finding_until_restored(tmp_path: Path) -> None:
    instance, event, restore = unbindable_world(tmp_path)
    now = served.PREDICTED_AT + timedelta(minutes=5)
    drain(instance, now=now)
    (anchor,) = unbindable_anchors(instance)
    assert anchor.event == event and anchor.code == "trigger_capture_unavailable"
    assert _windows(instance) == {}

    restore()
    drain(instance, now=now + UNBINDABLE_RETRY)
    assert unbindable_anchors(instance) == ()
    assert len(_windows(instance)) == 1


def retire(instance, owner, contract, *, at: str) -> None:  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.resolution_contracts import parse_resolution_contract

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = resolution_contract_path(contract.identity.name)
    accepted = parse_resolution_contract(tree[path], path=path)
    retired = accepted.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=contract.artifact_digest
            )
        }
    )
    tree[path] = render_resolution_contract(retired)
    _accept_tree(instance, owner, tree, timestamp=at, proposal_name="retire")


def test_retiring_a_contract_withdraws_what_it_owed(tmp_path: Path) -> None:
    instance, owner, _capture, contract = fixed_world(tmp_path)
    drain(instance, now=FIXED_CLOSES)
    assert len(settleable_windows(instance)) == 1
    retire(instance, owner, contract, at="2026-09-02T13:30:00.000000Z")

    drain(instance, now=FIXED_CLOSES + timedelta(hours=1))

    assert settleable_windows(instance) == () and _windows(instance) == {}


def test_a_retirement_landing_while_its_old_version_loads_is_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner, capture = served._world(tmp_path)
    drain(instance, now=served.PREDICTED_AT)  # the worker runs before the contract exists
    contract = served._predict(instance, owner, capture)
    load = WORKER._load
    raced: list[bool] = []

    def load_while_retired(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not raced:
            raced.append(True)
            retire(instance, owner, contract, at="2026-09-02T12:30:00.000000Z")
            WORKER.match(instance, now=served.PREDICTED_AT, daemon_id="daemon")
        return load(*args, **kwargs)

    monkeypatch.setattr(WORKER, "_load", load_while_retired)
    drain(instance, now=FIXED_CLOSES)

    assert raced and settleable_windows(instance) == () and _windows(instance) == {}


def test_the_operator_can_turn_the_worker_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "prediction")
    assert not WORKER.active(SimpleNamespace())
    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "evidence")
    assert WORKER.active(SimpleNamespace())


def test_a_failing_worker_is_stalled_with_a_repair_and_recovers_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.consumers.runner import consumer_health

    instance, _owner, _capture, _contract = fixed_world(tmp_path)
    drain(instance, now=served.PREDICTED_AT)
    original = WORKER._windows

    def broken(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("journal unreadable")

    monkeypatch.setattr(WORKER, "_windows", broken)
    with pytest.raises(OSError):
        drain(instance, now=FIXED_CLOSES)
    (health,) = [
        item for item in consumer_health(instance, now=FIXED_CLOSES) if item.kind == "prediction"
    ]
    assert health.state == "stalled" and "journal unreadable" in health.detail["last_error"]
    assert health.repair is not None and health.repair.operation == "hand_edit"
    assert health.detail["contracts"] == 1 and health.detail["open_windows"] == 1

    monkeypatch.setattr(WORKER, "_windows", original)
    drain(instance, now=FIXED_CLOSES)
    (health,) = WORKER.health(instance, now=FIXED_CLOSES)
    assert health.state == "running" and health.detail["settleable_windows"] == 1


def _windows_table(count: int) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(predictions._SCHEMA)
    connection.executemany(
        "INSERT INTO windows(contract_id,identity,window,ends_at_us,status) "
        "VALUES (?,'ResolutionContract:x','{}',?,'resolved')",
        ((f"RSC-{index:032d}", index) for index in range(count)),
    )
    connection.execute("ANALYZE")
    return connection


def test_a_pass_costs_the_windows_it_touches_not_every_window_ever_bound() -> None:
    now = datetime(2026, 9, 3, tzinfo=UTC)

    def steps(count: int) -> int:
        connection = _windows_table(count)
        counter = [0]

        def tick() -> int:
            counter[0] += 1
            return 0

        connection.set_progress_handler(tick, 1)
        connection.execute(
            "SELECT 1 FROM windows WHERE status='open' AND ends_at_us<=? LIMIT 1",
            (predictions._microseconds(now),),
        ).fetchall()
        connection.execute("SELECT 1 FROM windows WHERE dirty IS NOT NULL LIMIT 1").fetchall()
        connection.execute(
            "SELECT contract_id FROM windows INDEXED BY windows_by_status "
            "WHERE status='settleable' ORDER BY ends_at_us,contract_id"
        ).fetchall()
        connection.execute(
            "SELECT count(*) FROM windows WHERE status=?", ("settleable",)
        ).fetchall()
        return counter[0]

    small, large = steps(1_000), steps(10_000)
    assert large < small * 1.5, (small, large)


def _fake_instance(root: Path):  # type: ignore[no-untyped-def]
    from contextlib import nullcontext

    return SimpleNamespace(
        root=root,
        descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust")),
        accepted_history_reader=lambda: nullcontext(SimpleNamespace(sequence=0)),
    )


def _steps(monkeypatch: pytest.MonkeyPatch, call) -> int:  # type: ignore[no-untyped-def]
    """SQLite VM steps one call takes across every connection it opens."""

    counter = [0]
    connect = sqlite3.connect

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        connection = connect(*args, **kwargs)

        def tick() -> int:
            counter[0] += 1
            return 0

        connection.set_progress_handler(tick, 1)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", counted)
        call()
    return counter[0]


def test_an_idle_readiness_check_costs_the_same_whatever_the_contract_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 3, tzinfo=UTC)

    def steps(count: int) -> int:
        instance = _fake_instance(tmp_path / str(count))
        with predictions._state(instance) as connection:
            assert connection is not None
            connection.execute(
                "INSERT INTO progress(singleton,generation,index_generation,capture_head) "
                "VALUES (1,0,'g',100)"
            )
            connection.executemany(
                "INSERT INTO contracts(identity,artifact_digest,hypothesis,reference,contract,"
                "accepted_at,selector_digest,capture_generation,capture_ordinal) "
                "VALUES (?,'d','h','r','c','a','s','g',100)",
                ((f"ResolutionContract:c{index:06d}",) for index in range(count)),
            )
        return _steps(monkeypatch, lambda: WORKER.due(instance, now=now))

    small, large = steps(1_000), steps(10_000)
    assert large < small * 1.5, (small, large)
