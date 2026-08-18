"""§8.5.3 per-terminal-item dependency closure derived from actual dataflow."""

from __future__ import annotations

import pytest

from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.errors import PlaybillExecutionError
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    ProcedureExhaustWriter,
    parse_journal_payload,
)
from cruxible_core.playbill.procedures.execution import (
    ProcedureExecutor,
    ProcedureRunResultV1,
)
from cruxible_core.playbill.procedures.input_planes import run_input_digest
from cruxible_core.playbill.procedures.models import (
    ExhaustTapNodeV3,
    InboxEgressNodeV3,
    ProcedureNodeV3,
    ProcedurePinSlotRefV1,
    StateTapNodeV3,
    TransformNodeV3,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.procedures.terminal_dependencies import (
    TAINT_ACCEPTED_STATE,
    TAINT_UNPROMOTED_EXHAUST,
    TerminalItemDependencyManifestV1,
    TerminalItemDerivedFactsV1,
)
from cruxible_core.playbill.run_inputs import (
    JournalExhaustTapReader,
    admit_line_procedure_run,
    line_run_slot_pins,
    select_line_run_sources,
)
from tests.test_playbill._line_runtime_support import (
    FILTER_IN,
    FILTER_OUT,
    INSTANCE_ID,
    INTERFACE_DIGESTS,
    NOW,
    QUERY_PIN,
    REDUCER_PIN,
    Authority,
    Contracts,
    CountingReducer,
    FixedClock,
    LineRuntimeFixture,
    StateReader,
    accepted_procedure,
    actor,
    build_fixture,
    cadence_occurrence,
    coordinate,
    default_nodes,
    digest,
    mandate_read,
    source_node,
)

PRIOR_RUNS = JournalStreamIdentityV1(
    instance_id=INSTANCE_ID,
    journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    stream_id="prior-runs",
)
_ACCESS = BodyAccessContext(principal_id="terminal-dependency-test", can_read_body=True)


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


def _admit(fixture: LineRuntimeFixture, *, state_reader=None, run_id: str = "line-run-1"):
    selection = select_line_run_sources(
        fixture.policy,
        (),
        anchor=None,
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
        exhaust_reader=JournalExhaustTapReader(
            journal=fixture.journal,
            bodies=fixture.bodies,
            instance_id=INSTANCE_ID,
            partition_id="line-runs",
            reducers={CountingReducer().reducer_digest: CountingReducer()},
        ),
        acquirer=fixture.acquirer,
    )


def _executor(fixture: LineRuntimeFixture, *, run_index: ProcedureRunIndex | None = None):
    return ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=run_index or fixture.run_index,
        fencing_token=fixture.lease.fencing_token,
        activation_authority=Authority(fixture.accepted_procedure.artifact_digest),
        contract_validator=Contracts(),
        source_acquirer=fixture.acquirer,
        acquisition_policy=fixture.policy,
        slot_pins=line_run_slot_pins(fixture.accepted_line),
        clock=FixedClock(),
    )


def _item_records(fixture: LineRuntimeFixture) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for stored in fixture.journal.all_records(fixture.stream, fixture.run_partition):
        if stored.record.event_kind != "item_dependencies":
            continue
        payload = parse_journal_payload(
            fixture.bodies.read(stored.record.payload_digest, access=_ACCESS)
        )
        assert isinstance(payload, dict)
        payloads.append(payload)
    return payloads


def _manifests(fixture: LineRuntimeFixture) -> list[TerminalItemDependencyManifestV1]:
    return [
        TerminalItemDependencyManifestV1.model_validate(payload["manifest"])
        for payload in _item_records(fixture)
    ]


def _facts(fixture: LineRuntimeFixture) -> list[TerminalItemDerivedFactsV1]:
    return [
        TerminalItemDerivedFactsV1.model_validate(payload["derived"])
        for payload in _item_records(fixture)
    ]


def _terminal_egress(fixture: LineRuntimeFixture) -> dict[str, object]:
    for stored in fixture.journal.all_records(fixture.stream, fixture.run_partition):
        if stored.record.event_kind == "terminal_egress":
            payload = parse_journal_payload(
                fixture.bodies.read(stored.record.payload_digest, access=_ACCESS)
            )
            assert isinstance(payload, dict)
            return payload
    raise AssertionError("run recorded no terminal egress")


def _run(fixture: LineRuntimeFixture, **kwargs) -> ProcedureRunResultV1:
    prepared = _admit(fixture, **kwargs)
    return _executor(fixture).execute(prepared, fixture.accepted_procedure)


def test_item_manifest_excludes_run_inputs_the_item_never_consumed(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)
    result = _executor(fixture).execute(prepared, fixture.accepted_procedure)
    assert result.status == "refused"

    manifests = _manifests(fixture)
    assert len(manifests) == 1
    manifest = manifests[0]

    rows_digest = run_input_digest(prepared.admission.accepted_state_inputs[0])
    receipts_digest = run_input_digest(prepared.admission.exhaust_inputs[0])

    assert manifest.accepted_state_input_digests == (rows_digest,)
    assert manifest.exhaust_input_digests == ()
    assert receipts_digest not in manifest.policy_and_law_digests
    assert manifest.produced_capture_digests == ()
    assert manifest.admitted_capture_digests == ()
    assert QUERY_PIN.artifact_digest in manifest.policy_and_law_digests
    assert FILTER_IN.artifact_digest in manifest.policy_and_law_digests
    assert FILTER_OUT.artifact_digest in manifest.policy_and_law_digests
    assert REDUCER_PIN.artifact_digest not in manifest.policy_and_law_digests

    facts = _facts(fixture)[0]
    assert facts.taint_labels == (TAINT_ACCEPTED_STATE,)
    assert facts.epistemic_grade == "observed"
    assert facts.provenance_grade == "self-asserted"
    assert [item.disposition for item in facts.source_coverage] == ["absent"]


def _exhaust_terminal_nodes() -> tuple[ProcedureNodeV3, ...]:
    return (
        StateTapNodeV3(
            node_id="read",
            query=ProcedurePinSlotRefV1(slot_name="query"),
            parameters={"status": "open"},
            as_="rows",
            next="tap",
        ),
        ExhaustTapNodeV3(
            node_id="tap",
            reducer_or_query=REDUCER_PIN,
            journal_identity="prior-runs",
            as_="receipts",
            next="pick",
        ),
        TransformNodeV3(
            node_id="pick",
            transform_kind="filter_items",
            contract_in=FILTER_IN,
            contract_out=FILTER_OUT,
            spec={"items": "$steps.rows.items", "where": {"status": "open"}},
            as_="picked",
            next="emit",
        ),
        InboxEgressNodeV3(
            node_id="emit",
            input={"context": "$steps.receipts", "items": "$steps.picked.items"},
        ),
    )


def test_item_manifest_reproduces_taint_and_grade_from_its_real_closure(tmp_path) -> None:
    accepted = accepted_procedure(nodes=_exhaust_terminal_nodes())
    fixture = build_fixture(tmp_path, accepted=accepted)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)
    _executor(fixture).execute(prepared, fixture.accepted_procedure)

    manifest = _manifests(fixture)[0]
    facts = _facts(fixture)[0]
    assert manifest.accepted_state_input_digests == (
        run_input_digest(prepared.admission.accepted_state_inputs[0]),
    )
    assert manifest.exhaust_input_digests == (
        run_input_digest(prepared.admission.exhaust_inputs[0]),
    )
    assert facts.taint_labels == (TAINT_ACCEPTED_STATE, TAINT_UNPROMOTED_EXHAUST)
    assert facts.epistemic_grade == "derived"


def _split_plane_nodes() -> tuple[ProcedureNodeV3, ...]:
    return (
        StateTapNodeV3(
            node_id="read",
            query=ProcedurePinSlotRefV1(slot_name="query"),
            parameters={"status": "open"},
            as_="rows",
            next="tap",
        ),
        ExhaustTapNodeV3(
            node_id="tap",
            reducer_or_query=REDUCER_PIN,
            journal_identity="prior-runs",
            as_="receipts",
            next="emit",
        ),
        InboxEgressNodeV3(node_id="emit", input=["$steps.rows", "$steps.receipts"]),
    )


def test_fanout_children_carry_only_their_own_plane(tmp_path) -> None:
    accepted = accepted_procedure(nodes=_split_plane_nodes(), returns="receipts")
    fixture = build_fixture(tmp_path, accepted=accepted)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)
    _executor(fixture).execute(prepared, fixture.accepted_procedure)

    manifests = _manifests(fixture)
    facts = _facts(fixture)
    assert len(manifests) == 2

    rows_digest = run_input_digest(prepared.admission.accepted_state_inputs[0])
    receipts_digest = run_input_digest(prepared.admission.exhaust_inputs[0])

    assert manifests[0].accepted_state_input_digests == (rows_digest,)
    assert manifests[0].exhaust_input_digests == ()
    assert manifests[1].accepted_state_input_digests == ()
    assert manifests[1].exhaust_input_digests == (receipts_digest,)

    assert facts[0].taint_labels == (TAINT_ACCEPTED_STATE,)
    assert facts[1].taint_labels == (TAINT_UNPROMOTED_EXHAUST,)
    assert facts[0].epistemic_grade == "observed"
    assert facts[1].epistemic_grade == "derived"
    assert [item.child_index for item in facts] == [0, 1]


def _source_terminal_nodes() -> tuple[ProcedureNodeV3, ...]:
    """A live source feeds the terminal; the accepted-state read is never consumed."""

    return (
        StateTapNodeV3(
            node_id="read",
            query=ProcedurePinSlotRefV1(slot_name="query"),
            parameters={"status": "open"},
            as_="rows",
            next="fetch",
        ),
        source_node(next_node="emit"),
        InboxEgressNodeV3(node_id="emit", input={"items": "$steps.orders"}),
    )


def test_produced_capture_coverage_and_provenance_come_from_the_item_closure(tmp_path) -> None:
    accepted = accepted_procedure(nodes=_source_terminal_nodes(), returns="orders")
    fixture = build_fixture(tmp_path, accepted=accepted)
    prepared = _admit(fixture)
    result = _executor(fixture).execute(prepared, fixture.accepted_procedure)
    assert result.status == "refused"

    manifests = _manifests(fixture)
    facts = _facts(fixture)
    assert len(manifests) == 2
    for manifest, fact in zip(manifests, facts, strict=True):
        assert len(manifest.produced_capture_digests) == 1
        assert manifest.accepted_state_input_digests == ()
        assert len(manifest.receipt_digests) == 1
        assert fact.provenance_grade == "daemon-fetched"
        assert fact.epistemic_grade == "observed"
        coverage = {item.input_name: item for item in fact.source_coverage}
        assert coverage["orders"].disposition == "consumed"
        assert coverage["orders"].capture_digests == manifest.produced_capture_digests


def test_caller_supplied_evidence_never_enters_a_manifest(tmp_path) -> None:
    forged = digest("forged-evidence")
    nodes = (
        StateTapNodeV3(
            node_id="read",
            query=ProcedurePinSlotRefV1(slot_name="query"),
            parameters={"status": "open"},
            as_="rows",
            next="emit",
        ),
        InboxEgressNodeV3(
            node_id="emit",
            input={
                "evidence": [forged],
                "items": "$steps.rows.items",
                "receipt_digests": [forged],
            },
        ),
    )
    accepted = accepted_procedure(nodes=nodes, returns="rows")
    fixture = build_fixture(tmp_path, accepted=accepted)
    prepared = _admit(fixture)
    _executor(fixture).execute(prepared, fixture.accepted_procedure)

    for manifest in _manifests(fixture):
        every = (
            manifest.accepted_state_input_digests
            + manifest.admitted_capture_digests
            + manifest.produced_capture_digests
            + manifest.exhaust_input_digests
            + manifest.receipt_digests
            + manifest.policy_and_law_digests
        )
        assert forged not in every


def test_fanout_children_are_recorded_in_declared_index_order(tmp_path) -> None:
    accepted = accepted_procedure(nodes=_source_terminal_nodes(), returns="orders")
    fixture = build_fixture(tmp_path, accepted=accepted)
    _run(fixture)

    egress = _terminal_egress(fixture)
    children = egress["children"]
    assert isinstance(children, list)
    assert [child["child_index"] for child in children] == [0, 1]
    sequences = [child["sequence"] for child in children]
    assert sequences == sorted(sequences)
    keys = [child["item_key"] for child in children]
    assert keys[0].startswith("00000000.")
    assert keys[1].startswith("00000001.")
    assert len(set(keys)) == 2


def test_retry_replays_identical_manifests_without_appending(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)
    executor = _executor(fixture)
    first = executor.execute(prepared, fixture.accepted_procedure)
    first_manifests = _manifests(fixture)
    record_count = len(fixture.journal.all_records(fixture.stream, fixture.run_partition))

    retried = executor.execute(prepared, fixture.accepted_procedure)

    assert retried == first
    assert _manifests(fixture) == first_manifests
    assert len(fixture.journal.all_records(fixture.stream, fixture.run_partition)) == record_count


def test_recovery_reproduces_occurrence_input_vector_and_exhaust_chain(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    first = _admit(fixture)
    result = _executor(fixture).execute(first, fixture.accepted_procedure)
    manifests = _manifests(fixture)
    chain = result.receipt.record_digests

    # A crashed runner rebuilds its disposable index and re-admits from the same
    # journal state: occurrence, input vector, terminal dependencies, and the
    # exhaust chain all reproduce exactly.
    index_path = fixture.run_index.path
    fixture.run_index.close()
    index_path.unlink()
    rebuilt = ProcedureRunIndex(index_path)

    second = _admit(fixture)
    assert second.admission == first.admission
    assert second.admission.occurrence_id == first.admission.occurrence_id
    assert second.admission.run_inputs == first.admission.run_inputs

    replay = _executor(fixture, run_index=rebuilt).execute(second, fixture.accepted_procedure)
    assert replay == result
    assert replay.receipt.record_digests == chain
    assert _manifests(fixture) == manifests


def test_incomplete_attempt_refuses_redispatch_before_any_new_manifest(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    prepared = _admit(fixture)

    class _CrashingClock(FixedClock):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def now(self):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt("simulated crash")
            return NOW

    crashing = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token=fixture.lease.fencing_token,
        activation_authority=Authority(fixture.accepted_procedure.artifact_digest),
        contract_validator=Contracts(),
        source_acquirer=fixture.acquirer,
        acquisition_policy=fixture.policy,
        slot_pins=line_run_slot_pins(fixture.accepted_line),
        clock=_CrashingClock(),
    )
    with pytest.raises(KeyboardInterrupt, match="simulated crash"):
        crashing.execute(prepared, fixture.accepted_procedure)

    assert _manifests(fixture) == []
    with pytest.raises(PlaybillExecutionError, match="run_recovery_required"):
        _executor(fixture).execute(prepared, fixture.accepted_procedure)


def test_guard_branch_dependencies_reach_every_downstream_item(tmp_path) -> None:
    nodes = default_nodes()
    accepted = accepted_procedure(nodes=nodes)
    fixture = build_fixture(tmp_path, accepted=accepted)
    _seed_prior_runs(fixture)
    reader = StateReader({"items": [{"id": "a", "status": "open"}]})
    prepared = _admit(fixture, state_reader=reader)
    _executor(fixture).execute(prepared, fixture.accepted_procedure)

    manifest = _manifests(fixture)[0]
    assert manifest.accepted_state_input_digests == (
        run_input_digest(prepared.admission.accepted_state_inputs[0]),
    )


def test_no_open_rows_refuses_at_the_guard_without_a_terminal_manifest(tmp_path) -> None:
    fixture = build_fixture(tmp_path)
    _seed_prior_runs(fixture)
    reader = StateReader({"items": [{"id": "b", "status": "closed"}]})
    prepared = _admit(fixture, state_reader=reader)
    result = _executor(fixture).execute(prepared, fixture.accepted_procedure)

    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == "no-open-orders"
    assert _manifests(fixture) == []


def test_unavailable_required_source_refuses_only_after_a_typed_result(tmp_path) -> None:
    accepted = accepted_procedure(nodes=_source_terminal_nodes(), returns="orders")
    fixture = build_fixture(tmp_path, accepted=accepted, seed_source=False)
    prepared = _admit(fixture)
    result = _executor(fixture).execute(prepared, fixture.accepted_procedure)

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "playbill.acquisition.unavailable"
    kinds = [
        item.record.event_kind
        for item in fixture.journal.all_records(fixture.stream, fixture.run_partition)
    ]
    assert "source_acquisition" in kinds
    assert "produced_capture" not in kinds
    assert _manifests(fixture) == []
