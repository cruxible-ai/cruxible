"""§8.5.2 Line run admission: three planes, one frozen tuple, no relabelling."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cruxible_client.contracts.captures import parse_capture_envelope
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    ProcedureExhaustWriter,
)
from cruxible_core.playbill.lines import LineRuntimeRefusal
from cruxible_core.playbill.procedures.execution import (
    ProcedureExecutor,
    procedure_admission_digest,
)
from cruxible_core.playbill.procedures.input_planes import LandedCaptureRunInputV1
from cruxible_core.playbill.procedures.resolution import AcceptedAuthorityBasisV1
from cruxible_core.playbill.run_inputs import (
    JournalExhaustTapReader,
    admit_line_procedure_run,
    assert_nonsecret_binding,
    build_deployment_binding_snapshot,
    deployment_binding_snapshot_digest,
    epsilon_membership,
    line_run_slot_pins,
    read_mandate_basis,
    select_line_run_sources,
)
from tests.test_playbill._line_runtime_support import (
    CAPTURE_PIN,
    INSTANCE_ID,
    INTERFACE_DIGESTS,
    NOW,
    PROVIDER_PIN,
    QUERY_PIN,
    Authority,
    Contracts,
    CountingReducer,
    FixedClock,
    LineRuntimeFixture,
    StateReader,
    accepted_line,
    accepted_procedure,
    acquisition_policy,
    actor,
    build_fixture,
    cadence_occurrence,
    coordinate,
    digest,
    landed_candidate,
    mandate_read,
)

PRIOR_RUNS = JournalStreamIdentityV1(
    instance_id=INSTANCE_ID,
    journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    stream_id="prior-runs",
)


def _seed_prior_runs(fixture: LineRuntimeFixture, *, records: int = 2) -> None:
    head = fixture.journal.read_head(PRIOR_RUNS, "line-runs")
    fixture.journal.activate_writer(
        PRIOR_RUNS,
        "line-runs",
        fencing_token="seed-writer",
        expected_head=head,
    )
    writer = ProcedureExhaustWriter(
        journal=fixture.journal,
        bodies=fixture.bodies,
        fencing_token="seed-writer",
    )
    for index in range(records):
        writer.append(
            stream=PRIOR_RUNS,
            partition_id="line-runs",
            event_kind="attempt_finalized",
            accepted_coordinate=coordinate(),
            procedure_artifact_digest=fixture.accepted_procedure.artifact_digest,
            definition_digest=fixture.accepted_procedure.procedure.definition_digest,
            actor_context=actor(),
            recorded_at=NOW,
            payload={"prior_run_index": index},
        )


def _exhaust_reader(fixture: LineRuntimeFixture) -> JournalExhaustTapReader:
    return JournalExhaustTapReader(
        journal=fixture.journal,
        bodies=fixture.bodies,
        instance_id=INSTANCE_ID,
        partition_id="line-runs",
        reducers={CountingReducer().reducer_digest: CountingReducer()},
    )


def _acquire_landed_capture(fixture: LineRuntimeFixture) -> tuple[str, object]:
    """Land one Capture the way a prior watcher run would have landed it."""

    result = fixture.acquirer.acquire(
        node_id="fetch",
        input_name="orders",
        capture_contract=CAPTURE_PIN,
        provider=PROVIDER_PIN,
        request={
            "coordinate": {"lsn": "0/16B6C50"},
            "coordinate_type": "postgres-lsn-v1",
            "materialization": "cas",
            "selector": {"table": "orders"},
            "selector_type": "relation-primary-key-v1",
        },
        run_id="prior-watcher-run",
        bound_generation=coordinate().generation_root,
        observed_at=NOW,
    )
    assert result.outcome == "acquired"
    assert result.acquisition is not None
    return result.acquisition.capture_digest, result.acquisition.envelope


def _admit(
    fixture: LineRuntimeFixture,
    *,
    candidates: tuple[object, ...] = (),
    state_reader: StateReader | None = None,
    run_id: str = "line-run-1",
):
    selection = select_line_run_sources(
        fixture.policy,
        candidates,  # type: ignore[arg-type]
        anchor=None if not candidates else candidates[0].landing_event,  # type: ignore[attr-defined]
        evaluation_time=NOW,
        source_input_names=frozenset({"orders"}),
    )
    return admit_line_procedure_run(
        accepted_line=fixture.accepted_line,
        accepted_procedure=fixture.accepted_procedure,
        policy=fixture.policy,
        deployment=fixture.deployment,
        lease=fixture.lease,
        occurrence=cadence_occurrence(),
        attempt=1,
        run_id=run_id,
        accepted_coordinate=coordinate(),
        invocation_input={"request": "triage"},
        actor_context=actor(),
        state_reader=state_reader or StateReader(),
        selection=selection,
        binding_snapshot=fixture.binding_snapshot(),
        mandate_read=mandate_read(),
        sensitivity_policy=fixture.sensitivity(),
        interface_digests=INTERFACE_DIGESTS,
        admitted_at=NOW,
        exhaust_reader=_exhaust_reader(fixture),
        acquirer=fixture.acquirer,
    )


def _executor(fixture: LineRuntimeFixture) -> ProcedureExecutor:
    return ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token=fixture.lease.fencing_token,
        activation_authority=Authority(fixture.accepted_procedure.artifact_digest),
        contract_validator=Contracts(),
        source_acquirer=fixture.acquirer,
        acquisition_policy=fixture.policy,
        slot_pins=line_run_slot_pins(fixture.accepted_line),
        clock=FixedClock(),
    )


def test_all_three_input_planes_bind_exact_coordinates_cursors_and_results(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    capture_digest_value, envelope = _acquire_landed_capture(fixture)
    candidate = landed_candidate(
        envelope=parse_capture_envelope(
            fixture.captures.read(
                capture_digest_value,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        ),
        capture_digest_value=capture_digest_value,
    )
    assert candidate.envelope == envelope

    prepared = _admit(fixture, candidates=(candidate,))
    admission = prepared.admission

    assert admission.invocation_origin == "line"
    assert [item.input_name for item in admission.run_inputs] == ["orders", "receipts", "rows"]

    state_input = admission.accepted_state_inputs[0]
    assert state_input.read_coordinate == coordinate()
    assert state_input.query_definition_digest == QUERY_PIN.artifact_digest

    landed = admission.landed_capture_inputs[0]
    assert landed.capture_digest == capture_digest_value
    assert landed.landing_cursor == candidate.landing_event.cursor

    exhaust = admission.exhaust_inputs[0]
    assert exhaust.journal_identity == "prior-runs"
    assert exhaust.first_cursor.endswith("00000000000000000001")
    assert exhaust.last_cursor.endswith("00000000000000000002")
    assert prepared.exhaust_materials[0].value == {
        "kinds": ["attempt_finalized"],
        "record_count": 2,
    }

    assert admission.line_spec_digest == fixture.accepted_line.artifact_digest
    assert admission.acquisition_policy_digest is not None
    assert admission.selection_receipt_digest is not None
    assert admission.mandate_coordinate_digest is not None
    assert admission.calibration_coordinate_digest is not None
    assert admission.sensitivity_policy_digest is not None
    assert admission.deployment_snapshot_digest == deployment_binding_snapshot_digest(
        fixture.binding_snapshot()
    )
    assert admission.admission_binding_digest == procedure_admission_digest(admission)


def test_cross_plane_relabelling_refuses_before_any_node_fires(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)
    relabelled = prepared.admission.model_copy(
        update={
            "landed_capture_inputs": (
                LandedCaptureRunInputV1(
                    input_name="receipts",
                    capture_digest=digest("smuggled-capture"),
                    capture_contract_digest=digest("smuggled-contract"),
                    landing_cursor="partition:0001",
                ),
            ),
            "exhaust_inputs": (),
        }
    )
    attacked = prepared.model_copy(update={"admission": relabelled, "exhaust_materials": ()})

    with pytest.raises(PlaybillExecutionError, match="input_plane_relabelled"):
        _executor(fixture).execute(attacked, fixture.accepted_procedure)
    assert fixture.journal.all_records(fixture.stream, fixture.run_partition) == ()


def test_admitted_input_naming_no_input_node_refuses(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)
    stray = prepared.admission.model_copy(
        update={
            "landed_capture_inputs": (
                LandedCaptureRunInputV1(
                    input_name="unknown",
                    capture_digest=digest("stray-capture"),
                    capture_contract_digest=digest("stray-contract"),
                    landing_cursor="partition:0002",
                ),
            )
        }
    )
    with pytest.raises(PlaybillExecutionError, match="names no v3 input node"):
        _executor(fixture).execute(
            prepared.model_copy(update={"admission": stray}),
            fixture.accepted_procedure,
        )


def test_binding_snapshot_refuses_credential_shaped_names_and_values() -> None:
    with pytest.raises(LineRuntimeRefusal, match="credential_shaped"):
        assert_nonsecret_binding({"api_token": "abc"}, label="test")
    with pytest.raises(LineRuntimeRefusal, match="credential_shaped"):
        assert_nonsecret_binding(
            {"endpoint": "postgres://user:hunter2@db.internal/orders"},
            label="test",
        )
    with pytest.raises(LineRuntimeRefusal, match="credential_shaped"):
        assert_nonsecret_binding({"header": "Bearer abcdefabcdef"}, label="test")
    assert_nonsecret_binding({"backend_id": "local-a", "revision": 3}, label="test")


def test_deployment_binding_snapshot_is_nonsecret_and_reproduces(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    snapshot = build_deployment_binding_snapshot(fixture.deployment)
    assert snapshot.runner_id == "runner-a"
    assert snapshot.backend_id == "local-a"
    assert deployment_binding_snapshot_digest(snapshot) == deployment_binding_snapshot_digest(
        build_deployment_binding_snapshot(fixture.deployment)
    )
    assert "url" not in snapshot.model_dump(mode="json")


def test_epsilon_membership_is_fixed_by_occurrence_and_never_by_attempt() -> None:
    first = epsilon_membership(
        line_id="orders-triage",
        occurrence_epoch=1,
        occurrence_digest=digest("occurrence-a"),
        epsilon={"$decimal": "0.5"},
    )
    again = epsilon_membership(
        line_id="orders-triage",
        occurrence_epoch=1,
        occurrence_digest=digest("occurrence-a"),
        epsilon={"$decimal": "0.5"},
    )
    assert first is again
    assert (
        epsilon_membership(
            line_id="orders-triage",
            occurrence_epoch=1,
            occurrence_digest=digest("occurrence-a"),
            epsilon={"$decimal": "0"},
        )
        is False
    )
    assert (
        epsilon_membership(
            line_id="orders-triage",
            occurrence_epoch=1,
            occurrence_digest=digest("occurrence-a"),
            epsilon={"$decimal": "1"},
        )
        is True
    )


def test_mandate_read_binds_only_current_effective_authority() -> None:
    current = AcceptedAuthorityBasisV1(
        kind="standing_mandate",
        basis_digest=digest("mandate-live"),
        accepted_coordinate=coordinate(),
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        current_artifact_digest=digest("mandate-artifact"),
        artifact_digest=digest("mandate-artifact"),
    )
    expired = AcceptedAuthorityBasisV1(
        kind="standing_mandate",
        basis_digest=digest("mandate-expired"),
        accepted_coordinate=coordinate(),
        valid_from=NOW - timedelta(days=10),
        valid_until=NOW - timedelta(days=5),
        current_artifact_digest=digest("mandate-artifact"),
        artifact_digest=digest("mandate-artifact"),
    )
    read = read_mandate_basis(
        tuple(sorted({current.basis_digest, expired.basis_digest})),
        accepted_basis={
            current.basis_digest: current,
            expired.basis_digest: expired,
        },
        accepted_coordinate=coordinate(),
        evaluation_time=NOW,
    )
    assert read.resolved_basis_digests == (current.basis_digest,)
    assert len(read.requested_basis_digests) == 2


def test_source_acquisition_produces_a_post_admission_capture_without_mutating_admission(
    tmp_path,
) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)
    admission = prepared.admission
    assert admission.landed_capture_inputs == ()
    frozen_digest = admission.admission_binding_digest

    result = _executor(fixture).execute(prepared, fixture.accepted_procedure)
    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == "terminal_not_available"

    kinds = [
        item.record.event_kind
        for item in fixture.journal.all_records(fixture.stream, fixture.run_partition)
    ]
    assert "source_acquisition" in kinds
    assert "produced_capture" in kinds

    assert prepared.admission.admission_binding_digest == frozen_digest
    assert prepared.admission.landed_capture_inputs == ()
    assert procedure_admission_digest(prepared.admission) == frozen_digest


def test_landed_capture_admission_consumes_the_planner_and_never_reacquires(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    capture_digest_value, _envelope = _acquire_landed_capture(fixture)
    candidate = landed_candidate(
        envelope=parse_capture_envelope(
            fixture.captures.read(
                capture_digest_value,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        ),
        capture_digest_value=capture_digest_value,
    )
    prepared = _admit(fixture, candidates=(candidate,))
    assert prepared.landed_capture_materials[0].material.value == [
        {"order_id": "o-1"},
        {"order_id": "o-2"},
    ]

    result = _executor(fixture).execute(prepared, fixture.accepted_procedure)
    assert result.status == "refused"
    kinds = [
        item.record.event_kind
        for item in fixture.journal.all_records(fixture.stream, fixture.run_partition)
    ]
    assert "source_acquisition" not in kinds
    assert "produced_capture" not in kinds


def test_landed_only_input_without_an_anchor_refuses_admission(tmp_path) -> None:
    policy = acquisition_policy()
    accepted = accepted_procedure()
    fixture = build_fixture(tmp_path, accepted=accepted, policy=policy)
    with pytest.raises(LineRuntimeRefusal, match="selection_anchor_absent"):
        select_line_run_sources(
            fixture.policy,
            (),
            anchor=None,
            evaluation_time=NOW,
            source_input_names=frozenset(),
        )


def test_direct_admission_still_refuses_line_and_deployment_state(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)
    with pytest.raises(ValueError, match="direct admission binds only"):
        prepared.admission.model_copy().model_validate(
            {
                **prepared.admission.model_dump(mode="json"),
                "invocation_origin": "actor",
                "line_spec_digest": None,
                "occurrence_id": None,
                "deployment_snapshot_digest": None,
                "acquisition_policy_digest": None,
                "mandate_coordinate_digest": None,
                "calibration_coordinate_digest": None,
                "epsilon_member": False,
            }
        )


def test_line_admission_requires_every_policy_coordinate_together(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)
    payload = prepared.admission.model_dump(mode="json")
    payload["mandate_coordinate_digest"] = None
    with pytest.raises(ValueError, match="must bind LineSpec, occurrence"):
        type(prepared.admission).model_validate(payload)


def test_line_spec_and_deployment_must_name_the_same_accepted_artifacts(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    other = accepted_line(fixture.accepted_procedure, acquisition_policy(), epsilon="0.2")
    with pytest.raises(LineRuntimeRefusal, match="deployment_spec_mismatch"):
        admit_line_procedure_run(
            accepted_line=other,
            accepted_procedure=fixture.accepted_procedure,
            policy=fixture.policy,
            deployment=fixture.deployment,
            lease=fixture.lease,
            occurrence=cadence_occurrence(),
            attempt=1,
            run_id="line-run-x",
            accepted_coordinate=coordinate(),
            invocation_input={},
            actor_context=actor(),
            state_reader=StateReader(),
            selection=select_line_run_sources(
                fixture.policy,
                (),
                anchor=None,
                evaluation_time=NOW,
                source_input_names=frozenset({"orders"}),
            ),
            binding_snapshot=fixture.binding_snapshot(),
            mandate_read=mandate_read(),
            sensitivity_policy=fixture.sensitivity(),
            interface_digests=INTERFACE_DIGESTS,
            admitted_at=NOW,
            exhaust_reader=_exhaust_reader(fixture),
            acquirer=fixture.acquirer,
        )
