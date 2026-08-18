"""§0.4 Line track records folded from one accepted ExhaustPromotion."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.canonical import canonical_bytes, normalize_canonical
from cruxible_core.playbill.captures import CanonicalDurationV1
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    AcceptedExhaustPromotionV1,
    ExhaustPromotionV1,
    ExhaustReceiptSetManifestV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
    VerifiedExhaustRecordV1,
    evaluate_exhaust_promotion_law,
    exhaust_promotion_digest,
    exhaust_promotion_output_digest,
    exhaust_promotion_path,
    exhaust_receipt_set_manifest_digest,
    parse_journal_payload,
    render_exhaust_promotion,
)
from cruxible_core.playbill.exhaust.line_track_records import (
    LINE_TRACK_RECORD_TAG,
    LineTrackRecordError,
    LineTrackRecordReducer,
    LineTrackRecordV1,
    build_line_track_record,
    line_declared_input_bucket,
    line_declared_inputs,
    line_slot_interface_digest,
    line_track_record_dimension_key,
    line_track_record_dimensions,
    line_track_record_facts,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.lines import (
    LineRunnerIdentityV1,
    line_deployment_digest,
    revise_line_deployment,
    take_over_line_lease,
)
from cruxible_core.playbill.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_core.playbill.procedures.egress import (
    EFFECTIVE_RUNG_TERMS,
    EffectiveRungV1,
    effective_rung_digest,
)
from cruxible_core.playbill.procedures.execution import ProcedureExecutor
from cruxible_core.playbill.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_core.playbill.procedures.line_specs import (
    AcceptedLineSpecV1,
    CadenceTriggerPolicyV1,
    LineSpecV1,
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_core.playbill.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    InboxEgressNodeV3,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedureNodeV3,
    ProcedurePinSlotRefV1,
    StateTapNodeV3,
    TransformNodeV3,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.projection_extensions import playbill_runtime_extension_registry
from cruxible_core.playbill.run_inputs import (
    ProcedureMandateReadV1,
    admit_line_procedure_run,
    build_calibration_read,
    build_deployment_binding_snapshot,
    line_effective_rung,
    line_run_slot_pins,
    provider_binding_snapshot,
    read_mandate_basis,
    select_line_run_sources,
)
from cruxible_core.playbill.serving import bind_current_projection
from cruxible_core.playbill.source_readers import ProducerBindingV1
from cruxible_core.service.playbill_procedures import (
    ExhaustReducerRegistry,
    LocalExhaustPromotionVerifier,
)
from tests.test_playbill._line_runtime_support import (
    CAPTURE_CONTRACT,
    CONTRACT_IN,
    CONTRACT_OUT,
    FILTER_IN,
    FILTER_OUT,
    INTERFACE_DIGESTS,
    LINE_ID,
    NOW,
    PROVIDER_PIN,
    QUERY_PIN,
    SOURCE_IDENTITY,
    SOURCE_PROVIDER,
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
    pin,
    source_node,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_resolution_contracts import _accept_tree, _actor

_ACCESS = BodyAccessContext(principal_id="line-track-record-test", can_read_body=True)

REBOUND_BINDING = ProducerBindingV1(
    provider=SOURCE_PROVIDER.identity,
    logical_source_identity=SOURCE_IDENTITY,
    adapter_digest=digest("adapter-rebound"),
)


def _read_node(*, next_node: str) -> StateTapNodeV3:
    return StateTapNodeV3(
        node_id="read",
        query=ProcedurePinSlotRefV1(slot_name="query"),
        parameters={"status": "open"},
        as_="rows",
        next=next_node,
    )


def _base_nodes() -> tuple[ProcedureNodeV3, ...]:
    """One graph over the accepted-state and landed-Capture planes."""

    return (
        _read_node(next_node="fetch"),
        source_node(next_node="pick"),
        TransformNodeV3(
            node_id="pick",
            transform_kind="filter_items",
            contract_in=FILTER_IN,
            contract_out=FILTER_OUT,
            spec={"items": "$steps.rows.items", "where": {"status": "open"}},
            as_="picked",
            next="gate",
        ),
        GuardNodeV3(
            node_id="gate",
            predicate=GuardPredicateV1(
                left=PredicateOperandV1(kind="count", alias="picked"),
                operator="gt",
                right=PredicateOperandV1(kind="literal", value=0),
            ),
            on_true="emit",
            on_false="$abort",
            refusal_code="no-open-orders",
            message="No open order rows survived the filter.",
        ),
        InboxEgressNodeV3(node_id="emit", input={"items": "$steps.picked.items"}),
    )


def _line_procedure(*, name: str = "orders-triage", terminal_capability=1):
    return accepted_procedure(
        name=name,
        nodes=_base_nodes(),
        returns="picked",
        terminal_capability=terminal_capability,
    )


# ---------------------------------------------------------------------------
# Run, promote, fold
# ---------------------------------------------------------------------------


def _no_mandate() -> ProcedureMandateReadV1:
    return read_mandate_basis(
        (),
        accepted_basis={},
        accepted_coordinate=coordinate(),
        evaluation_time=NOW,
    )


def _rung(fixture: LineRuntimeFixture) -> EffectiveRungV1:
    return line_effective_rung(
        accepted_line=fixture.accepted_line,
        accepted_procedure=fixture.accepted_procedure,
        sensitivity_policy=fixture.sensitivity(),
        mandate_read=_no_mandate(),
        calibration=build_calibration_read(
            accepted_line=fixture.accepted_line,
            occurrence=cadence_occurrence(),
            accepted_coordinate=coordinate(),
        ),
        evaluation_time=NOW,
    )


class _Sink:
    """A sink that accepts exactly the children its terminal handed it."""

    def deliver_terminal_egress(self, *, request):  # type: ignore[no-untyped-def]
        from cruxible_core.playbill.procedures.egress import (
            TerminalEgressChildReceiptV1,
            TerminalEgressReceiptV1,
        )

        return TerminalEgressReceiptV1(
            kind=request.kind,
            run_id=request.run_id,
            node_id=request.node_id,
            disposition="posted",
            children=tuple(
                TerminalEgressChildReceiptV1(
                    child_index=item.child_index,
                    item_key=item.item_key,
                    egress_digest=digest(f"egress-{request.run_id}-{item.child_index}"),
                )
                for item in request.items
            ),
        )


def _run(
    fixture: LineRuntimeFixture,
    *,
    run_id: str,
    tick_index: int = 0,
    deployment=None,
    lease=None,
    binding_snapshot=None,
    rung: EffectiveRungV1 | None = None,
):
    """Admit and execute one Line run against an exact deployment revision."""

    deployment = deployment if deployment is not None else fixture.deployment
    lease = lease if lease is not None else fixture.lease
    snapshot = binding_snapshot if binding_snapshot is not None else fixture.binding_snapshot()
    prepared = admit_line_procedure_run(
        accepted_line=fixture.accepted_line,
        accepted_procedure=fixture.accepted_procedure,
        policy=fixture.policy,
        deployment=deployment,
        lease=lease,
        occurrence=cadence_occurrence(tick_index=tick_index),
        attempt=1,
        run_id=run_id,
        accepted_coordinate=coordinate(),
        invocation_input={"request": "triage"},
        actor_context=actor(),
        state_reader=StateReader(),
        selection=select_line_run_sources(
            fixture.policy,
            (),
            anchor=None,
            evaluation_time=NOW,
            source_input_names=frozenset({"orders"}),
        ),
        binding_snapshot=snapshot,
        mandate_read=_no_mandate(),
        sensitivity_policy=fixture.sensitivity(),
        interface_digests=INTERFACE_DIGESTS,
        admitted_at=NOW,
        acquirer=fixture.acquirer,
    )
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token=lease.fencing_token,
        activation_authority=Authority(fixture.accepted_procedure.artifact_digest),
        contract_validator=Contracts(),
        provider_executor=None,  # type: ignore[arg-type]
        source_acquirer=fixture.acquirer,
        acquisition_policy=fixture.policy,
        slot_pins=line_run_slot_pins(fixture.accepted_line),
        effective_rung=rung if rung is not None else _rung(fixture),
        egress_sink=_Sink(),  # type: ignore[arg-type]
        clock=FixedClock(),
    )
    return executor.execute(prepared, fixture.accepted_procedure)


def _verified(fixture: LineRuntimeFixture) -> tuple[VerifiedExhaustRecordV1, ...]:
    return tuple(
        VerifiedExhaustRecordV1(
            record_digest=stored.record_digest,
            sequence=stored.record.sequence,
            event_kind=stored.record.event_kind,
            generation_digest=stored.record.accepted_coordinate.generation_root,
            payload_digest=stored.record.payload_digest,
            payload=parse_journal_payload(
                fixture.bodies.read(stored.record.payload_digest, access=_ACCESS)
            ),
            procedure_artifact_digest=stored.record.procedure_artifact_digest,
            definition_digest=stored.record.definition_digest,
            run_id=stored.record.run_id,
            occurrence_id=stored.record.occurrence_id,
            attempt=stored.record.attempt,
            line_spec_digest=stored.record.line_spec_digest,
        )
        for stored in fixture.journal.all_records(fixture.stream, fixture.run_partition)
    )


def _promote(
    fixture: LineRuntimeFixture,
    *,
    reducer,
    name: str = "orders-triage-window",
    line_pin: ArtifactPin | None = None,
    include_line_pin: bool = True,
) -> AcceptedExhaustPromotionV1:
    """Verify one exact range through the ordinary promotion law, then accept it."""

    records = fixture.journal.all_records(fixture.stream, fixture.run_partition)
    manifest = ExhaustReceiptSetManifestV1(
        stream_id=fixture.stream.stream_id,
        partition_id=fixture.run_partition,
        first_sequence=1,
        last_sequence=records[-1].record.sequence,
        record_digests=tuple(item.record_digest for item in records),
        payload_digests=tuple(item.record.payload_digest for item in records),
    )
    manifest_digest = exhaust_receipt_set_manifest_digest(manifest)
    assert fixture.bodies.store(canonical_bytes(manifest.model_dump(mode="json"))).digest == (
        manifest_digest
    )
    output = normalize_canonical(reducer.reduce(_verified(fixture)))
    fixture.bodies.store(canonical_bytes(output))
    procedure_pin = pin(
        "procedure",
        "Procedure",
        fixture.accepted_procedure.procedure.identity.name,
        artifact_digest=fixture.accepted_procedure.artifact_digest,
    )
    pins = [
        procedure_pin,
        pin(
            "reducer",
            "ExhaustReducer",
            "line-track-record",
            artifact_digest=reducer.reducer_digest,
        ),
        pin("receipt-set-manifest", "ReceiptSetManifest", name, artifact_digest=manifest_digest),
    ]
    if include_line_pin:
        pins.append(
            line_pin
            if line_pin is not None
            else pin(
                "line",
                "Line",
                fixture.accepted_line.line.identity.name,
                artifact_digest=fixture.accepted_line.artifact_digest,
            )
        )
    promotion = ExhaustPromotionV1(
        identity=ArtifactIdentity(kind="ExhaustPromotion", name=name),
        stream_id=fixture.stream.stream_id,
        partition_id=fixture.run_partition,
        first_sequence=1,
        last_sequence=records[-1].record.sequence,
        chain_head_digest=records[-1].record_digest,
        receipt_set_manifest_digest=manifest_digest,
        reducer_digest=reducer.reducer_digest,
        output_digest=exhaust_promotion_output_digest(output),
        bound_generation_digests=tuple(
            sorted({item.record.accepted_coordinate.generation_root for item in records})
        ),
        authority=ArtifactAuthority(propose_roles=("author",), approve_roles=("reviewer",)),
        pins=tuple(
            sorted(
                pins,
                key=lambda item: (
                    item.role.encode(),
                    item.target.qualified.encode(),
                    item.artifact_digest.encode(),
                ),
            )
        ),
    )
    result = evaluate_exhaust_promotion_law(
        promotion,
        records=records,
        bodies=fixture.bodies,
        reducer=reducer,
    )
    assert result.verdict == "accepted", result.refusal_code
    return AcceptedExhaustPromotionV1(
        path=exhaust_promotion_path(name),
        promotion=promotion,
        artifact_digest=exhaust_promotion_digest(promotion),
        accepted_coordinate=coordinate(),
    )


def _reducer(fixture: LineRuntimeFixture) -> LineTrackRecordReducer:
    return LineTrackRecordReducer(
        accepted_line=fixture.accepted_line,
        accepted_procedure=fixture.accepted_procedure,
    )


def _output(fixture: LineRuntimeFixture, reducer: LineTrackRecordReducer) -> object:
    return normalize_canonical(reducer.reduce(_verified(fixture)))


# ---------------------------------------------------------------------------
# History spans deployment revisions and provider rebinds
# ---------------------------------------------------------------------------


def _rebind_provider(fixture: LineRuntimeFixture):
    """Advance one deployment revision that rebinds the provider adapter."""

    deployment = revise_line_deployment(
        fixture.deployment,
        runner=LineRunnerIdentityV1(runner_id="runner-b"),
        activated_at=NOW,
    )
    lease = take_over_line_lease(
        fixture.journal,
        deployment,
        previous=fixture.lease,
        fencing_token="writer-b",
        acquired_at=NOW,
    )
    snapshot = build_deployment_binding_snapshot(
        deployment,
        provider_bindings=(
            provider_binding_snapshot(
                binding=REBOUND_BINDING,
                provider_artifact_digest=PROVIDER_PIN.artifact_digest,
                contract=CAPTURE_CONTRACT,
            ),
        ),
    )
    return deployment, lease, snapshot


def test_line_history_spans_a_provider_rebind_under_one_dimension_key(tmp_path) -> None:
    """A rebind is a deployment act: same Line, same epoch, same credit bucket."""

    fixture = build_fixture(tmp_path, accepted=_line_procedure())
    assert _run(fixture, run_id="line-run-1", tick_index=0).status == "succeeded"

    deployment, lease, snapshot = _rebind_provider(fixture)
    assert line_deployment_digest(deployment) != line_deployment_digest(fixture.deployment)
    assert (
        _run(
            fixture,
            run_id="line-run-2",
            tick_index=1,
            deployment=deployment,
            lease=lease,
            binding_snapshot=snapshot,
        ).status
        == "succeeded"
    )

    reducer = _reducer(fixture)
    accepted = _promote(fixture, reducer=reducer)
    facts = line_track_record_facts(accepted, output=_output(fixture, reducer))

    assert len(facts) == 1, "one Line, one epoch, one dimension: one fact"
    fact = facts[0]
    assert fact.schema_id == "playbill.line.track_record"
    assert fact.subject_identity == f"Line:{LINE_ID}"
    assert fact.value["line_id"] == LINE_ID
    assert fact.value["occurrence_epoch"] == 1

    record = LineTrackRecordV1.model_validate(fact.value["track_record"])
    assert len(record.deployment_snapshot_digests) == 2, "history spans both deployment revisions"
    assert len(record.occurrence_ids) == 2
    assert [item.run_id for item in record.readings] == ["line-run-1", "line-run-2"]
    assert record.tally.delivered == 2, "a rebind never splits a Line's earned credit"
    assert record.line_spec_digest == fixture.accepted_line.artifact_digest


def test_the_fact_key_carries_the_epoch_and_the_dimension_key(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_line_procedure())
    _run(fixture, run_id="line-run-1")
    reducer = _reducer(fixture)
    accepted = _promote(fixture, reducer=reducer)
    fact = line_track_record_facts(accepted, output=_output(fixture, reducer))[0]

    dimensions = line_track_record_dimensions(
        fixture.accepted_line,
        fixture.accepted_procedure,
    )
    key = line_track_record_dimension_key(dimensions)
    assert fact.fact_key == f"{accepted.promotion.identity.name}.epoch-1.{key}"
    assert fact.value["dimension_key"] == key


# ---------------------------------------------------------------------------
# The three dimensions never merge credit
# ---------------------------------------------------------------------------


def _other_implementation():
    """A second implementation with the same slot interface and input planes."""

    return accepted_procedure(
        name="orders-triage-v2",
        nodes=(
            _read_node(next_node="fetch"),
            source_node(next_node="pick"),
            TransformNodeV3(
                node_id="pick",
                transform_kind="filter_items",
                contract_in=FILTER_IN,
                contract_out=FILTER_OUT,
                spec={"items": "$steps.rows.items", "where": {"status": "open"}},
                as_="picked",
                next="emit",
            ),
            InboxEgressNodeV3(node_id="emit", input={"items": "$steps.picked.items"}),
        ),
        returns="picked",
    )


def _state_only_implementation():
    """A third implementation that declares only the accepted-state plane."""

    return accepted_procedure(
        name="orders-triage-state-only",
        nodes=(
            _read_node(next_node="emit"),
            InboxEgressNodeV3(node_id="emit", input={"items": "$steps.rows.items"}),
        ),
        returns="rows",
    )


def test_implementation_digests_never_merge_credit(tmp_path) -> None:
    """Two implementations of one Line earn separate, never-summed facts."""

    first = build_fixture(tmp_path / "first", accepted=_line_procedure())
    _run(first, run_id="line-run-1")
    first_reducer = _reducer(first)
    first_fact = line_track_record_facts(
        _promote(first, reducer=first_reducer),
        output=_output(first, first_reducer),
    )[0]

    replacement = _other_implementation()
    second = build_fixture(tmp_path / "second", accepted=replacement)
    _run(second, run_id="line-run-1")
    second_reducer = _reducer(second)
    second_fact = line_track_record_facts(
        _promote(second, reducer=second_reducer),
        output=_output(second, second_reducer),
    )[0]

    assert first_fact.subject_identity == second_fact.subject_identity == f"Line:{LINE_ID}"
    assert first_fact.value["implementation_digest"] != second_fact.value["implementation_digest"]
    assert first_fact.value["dimension_key"] != second_fact.value["dimension_key"]
    assert first_fact.fact_key != second_fact.fact_key, "credit lands in separate buckets"


def test_slot_interface_and_declared_input_bucket_are_independent_axes() -> None:
    """The interface axis compares implementations; it never identifies one."""

    policy = acquisition_policy()
    original = _line_procedure()
    replacement = _other_implementation()
    state_only = _state_only_implementation()

    assert original.artifact_digest != replacement.artifact_digest
    assert line_slot_interface_digest(
        accepted_line(original, policy), original
    ) == line_slot_interface_digest(accepted_line(replacement, policy), replacement), (
        "two implementations may share one nominal slot interface"
    )
    assert line_declared_inputs(original) == line_declared_inputs(replacement)

    assert line_declared_inputs(original) != line_declared_inputs(state_only)
    assert line_declared_input_bucket(line_declared_inputs(original)) != (
        line_declared_input_bucket(line_declared_inputs(state_only))
    ), "a different declared input plane is a different bucket"

    dimensions = line_track_record_dimensions(accepted_line(original, policy), original)
    assert dimensions.implementation_digest == original.artifact_digest
    assert dimensions.slot_interface_digest != dimensions.implementation_digest
    assert dimensions.declared_input_bucket != dimensions.slot_interface_digest


def test_a_line_range_that_spans_two_implementations_refuses(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_line_procedure())
    _run(fixture, run_id="line-run-1")
    records = _verified(fixture)
    forged = tuple(
        item.model_copy(update={"procedure_artifact_digest": digest("another-implementation")})
        if item.sequence == 1
        else item
        for item in records
    )
    with pytest.raises(LineTrackRecordError, match="another Procedure implementation"):
        build_line_track_record(
            forged,
            accepted_line=fixture.accepted_line,
            accepted_procedure=fixture.accepted_procedure,
        )

    other_line = tuple(
        item.model_copy(update={"line_spec_digest": digest("another-line")})
        if item.sequence == 1
        else item
        for item in records
    )
    with pytest.raises(LineTrackRecordError, match="another accepted LineSpec"):
        build_line_track_record(
            other_line,
            accepted_line=fixture.accepted_line,
            accepted_procedure=fixture.accepted_procedure,
        )


# ---------------------------------------------------------------------------
# The slice-4 egress seam
# ---------------------------------------------------------------------------


def test_egress_history_reports_delivered_and_capped_by_which_term(tmp_path) -> None:
    """The PC-G status feed: verdict, limiting term, five terms, and closure."""

    fixture = build_fixture(
        tmp_path,
        accepted=_line_procedure(terminal_capability=3),
        requested_terminal_rung=3,
    )
    assert _run(fixture, run_id="line-run-1").status == "succeeded"

    capped = _rung(fixture).model_copy(
        update={
            "terms": tuple(
                item.model_copy(update={"rung": 0})
                if item.term == "propagated_sensitivity"
                else item
                for item in _rung(fixture).terms
            ),
            "effective_rung": 0,
            "limiting_term": "propagated_sensitivity",
        }
    )
    result = _run(fixture, run_id="line-run-2", tick_index=1, rung=capped)
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "terminal_rung_capped_by_propagated_sensitivity"

    reducer = _reducer(fixture)
    accepted = _promote(fixture, reducer=reducer)
    fact = line_track_record_facts(accepted, output=_output(fixture, reducer))[0]
    record = LineTrackRecordV1.model_validate(fact.value["track_record"])

    assert record.tally.delivered == 1
    assert record.tally.capped == 1
    assert [(item.term, item.capped_count) for item in record.tally.capped_by_term] == [
        ("propagated_sensitivity", 1)
    ]

    delivered, refused = record.readings
    assert delivered.verdict == "delivered"
    assert delivered.kind == "post_inbox"
    assert delivered.effective_rung_digest == effective_rung_digest(_rung(fixture))
    assert tuple(item.term for item in delivered.terms) == EFFECTIVE_RUNG_TERMS

    assert refused.verdict == "refused_effective_rung"
    assert refused.limiting_term == "propagated_sensitivity"
    assert refused.effective_rung == 0
    assert refused.effective_rung_digest == effective_rung_digest(capped)
    assert refused.children, "a capped terminal still owes its bound closure"
    assert [item.child_index for item in refused.children] == list(range(len(refused.children)))
    assert all(item.manifest_digest.startswith("sha256:") for item in refused.children)


def test_a_run_that_never_bound_a_rung_reports_pending_without_a_handle(tmp_path) -> None:
    unbound = build_fixture(tmp_path / "unbound", accepted=_line_procedure())
    prepared_status = _run_without_rung(unbound)
    assert prepared_status.status == "refused"
    assert prepared_status.refusal is not None
    assert prepared_status.refusal.code == "terminal_not_available"

    reducer = _reducer(unbound)
    accepted = _promote(unbound, reducer=reducer)
    fact = line_track_record_facts(accepted, output=_output(unbound, reducer))[0]
    record = LineTrackRecordV1.model_validate(fact.value["track_record"])
    assert record.tally.pending == 1 and record.tally.delivered == 0
    reading = record.readings[0]
    assert reading.verdict == "dependencies_bound_egress_pending"
    assert reading.effective_rung_digest is None and reading.limiting_term is None
    assert reading.terms == ()
    assert reading.children, "an unbound terminal still owes its bound closure"


def _run_without_rung(fixture: LineRuntimeFixture):
    """Execute one run whose executor holds no effective rung and no sink."""

    prepared = admit_line_procedure_run(
        accepted_line=fixture.accepted_line,
        accepted_procedure=fixture.accepted_procedure,
        policy=fixture.policy,
        deployment=fixture.deployment,
        lease=fixture.lease,
        occurrence=cadence_occurrence(),
        attempt=1,
        run_id="line-run-1",
        accepted_coordinate=coordinate(),
        invocation_input={"request": "triage"},
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
        mandate_read=_no_mandate(),
        sensitivity_policy=fixture.sensitivity(),
        interface_digests=INTERFACE_DIGESTS,
        admitted_at=NOW,
        acquirer=fixture.acquirer,
    )
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token=fixture.lease.fencing_token,
        activation_authority=Authority(fixture.accepted_procedure.artifact_digest),
        contract_validator=Contracts(),
        provider_executor=None,  # type: ignore[arg-type]
        source_acquirer=fixture.acquirer,
        acquisition_policy=fixture.policy,
        slot_pins=line_run_slot_pins(fixture.accepted_line),
        effective_rung=None,
        egress_sink=None,  # type: ignore[arg-type]
        clock=FixedClock(),
    )
    return executor.execute(prepared, fixture.accepted_procedure)


# ---------------------------------------------------------------------------
# Only accepted promotions, and only their exact pins, contribute
# ---------------------------------------------------------------------------


def test_only_an_exactly_pinned_line_and_implementation_contribute(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_line_procedure())
    _run(fixture, run_id="line-run-1")
    reducer = _reducer(fixture)
    output = _output(fixture, reducer)

    unpinned = _promote(fixture, reducer=reducer, include_line_pin=False)
    with pytest.raises(LineTrackRecordError, match="exactly one accepted Line"):
        line_track_record_facts(unpinned, output=output)

    other_name = _promote(
        fixture,
        reducer=reducer,
        line_pin=pin(
            "line",
            "Line",
            "another-line",
            artifact_digest=fixture.accepted_line.artifact_digest,
        ),
    )
    with pytest.raises(LineTrackRecordError, match="does not pin"):
        line_track_record_facts(other_name, output=output)

    stale_revision = _promote(
        fixture,
        reducer=reducer,
        line_pin=pin("line", "Line", LINE_ID, artifact_digest=digest("stale-line-revision")),
    )
    with pytest.raises(LineTrackRecordError, match="another accepted LineSpec revision"):
        line_track_record_facts(stale_revision, output=output)


def test_a_promotion_that_declares_no_line_track_record_emits_no_line_fact(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_line_procedure())
    _run(fixture, run_id="line-run-1")
    counting = CountingReducer(digest("count-records"))
    accepted = _promote(fixture, reducer=counting)
    output = normalize_canonical(counting.reduce(_verified(fixture)))

    assert output != {}
    assert line_track_record_facts(accepted, output=output) == ()


def test_a_malformed_declared_track_record_refuses(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_line_procedure())
    _run(fixture, run_id="line-run-1")
    reducer = _reducer(fixture)
    accepted = _promote(fixture, reducer=reducer)
    forged = dict(_output(fixture, reducer))  # type: ignore[arg-type]
    forged["occurrence_epoch"] = 0
    with pytest.raises(LineTrackRecordError, match="malformed Line track record"):
        line_track_record_facts(accepted, output=forged)

    assert forged["tag"] == LINE_TRACK_RECORD_TAG


def test_the_runtime_projection_registry_declares_the_line_grain() -> None:
    registry = playbill_runtime_extension_registry()
    assert registry.supports("playbill.line.track_record", 1, classification="semantic")
    assert registry.supports("playbill.procedure.track_record", 1, classification="semantic")


# ---------------------------------------------------------------------------
# The promotion projection
# ---------------------------------------------------------------------------


def _term_readings() -> list[dict[str, object]]:
    rungs = {"line_requested_rung": 1, "mandate_grant": 2}
    return [
        {
            "tag": "playbill-effective-rung-term-v1",
            "term": term,
            "rung": rungs.get(term, 3),
            "reason": f"{term} is open",
            "basis_digest": None,
        }
        for term in EFFECTIVE_RUNG_TERMS
    ]


def _projectable_procedure(name: str = "orders-triage") -> AcceptedProcedureV1:
    """The smallest slot-closed Procedure whose pins all resolve inside one tree."""

    definition = ProcedureDefinitionV3(
        name=name,
        contract_in=CONTRACT_IN,
        contract_out=CONTRACT_OUT,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=QUERY_PIN,
                parameters={"status": "open"},
                as_="rows",
                next="emit",
            ),
            InboxEgressNodeV3(node_id="emit", input={"items": "$steps.rows.items"}),
        ),
        returns="rows",
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=5_000_000),
            max_provider_calls=0,
            max_capture_bytes=65_536,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=10_000_000),
            max_provider_calls=1,
            max_capture_bytes=131_072,
            max_items=200,
            max_repeat_attempts=2,
        ),
        terminal_capability=1,
    )
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        pins=tuple(
            sorted(
                {CONTRACT_IN, CONTRACT_OUT, QUERY_PIN},
                key=lambda item: (item.role, item.target.qualified, item.artifact_digest),
            )
        ),
        activation_policy="drain",
    )
    return AcceptedProcedureV1(
        path=procedure_path(name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _projectable_line(accepted_proc: AcceptedProcedureV1) -> AcceptedLineSpecV1:
    """One accepted LineSpec that closes the Procedure's only declared slot."""

    procedure_pin = pin(
        "procedure",
        "Procedure",
        accepted_proc.procedure.identity.name,
        artifact_digest=accepted_proc.artifact_digest,
    )
    cadence = CadenceTriggerPolicyV1(cadence_policy_digest=digest("hourly"))
    cadence_pin = pin(
        "trigger-cadence-policy",
        "Policy",
        "hourly",
        artifact_digest=cadence.cadence_policy_digest,
    )
    line = LineSpecV1(
        identity=ArtifactIdentity(kind="Line", name=LINE_ID),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={"status": "open"},
        slot_bindings=(),
        trigger_policy=cadence,
        requested_terminal_rung=1,
        budgets={
            "max_capture_bytes": 65_536,
            "max_items": 100,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": 5_000_000,
        },
        epsilon={"$decimal": "0.1"},
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        pins=tuple(
            sorted(
                {procedure_pin, cadence_pin},
                key=lambda item: (item.role, item.target.qualified, item.artifact_digest),
            )
        ),
    )
    return AcceptedLineSpecV1(
        path=line_spec_path(LINE_ID),
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )


def test_an_accepted_promotion_projects_its_line_track_record(tmp_path) -> None:
    """The ExhaustPromotion projection serves the Line grain at its coordinate."""

    instance, _owner = initialize_local(tmp_path)
    accepted_proc = _projectable_procedure()
    base = accepted_proc.procedure
    line = _projectable_line(accepted_proc)
    _accept_tree(
        instance,
        _owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            accepted_proc.path: render_procedure(base),
            line.path: render_line_spec(line.line),
        },
        timestamp="2026-08-18T15:00:00.000000Z",
        proposal_name="line-track-record-procedure",
    )

    journal_root = tmp_path / "track-record-journal"
    journal_root.mkdir()
    journal = LocalJournalBackend(journal_root)
    bodies = instance.body_store()
    stream = JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="lines",
    )
    journal.activate_writer(
        stream,
        "line-runs",
        fencing_token="writer",
        expected_head=journal.read_head(stream, "line-runs"),
    )
    writer = ProcedureExhaustWriter(journal=journal, bodies=bodies, fencing_token="writer")
    accepted_coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    common = {
        "stream": stream,
        "partition_id": "line-runs",
        "accepted_coordinate": accepted_coordinate,
        "procedure_artifact_digest": accepted_proc.artifact_digest,
        "definition_digest": base.definition_digest,
        "actor_context": _actor(),
        "recorded_at": NOW,
        "run_id": "line-run-1",
        "line_spec_digest": line.artifact_digest,
        "occurrence_id": digest("occurrence-one"),
        "attempt": 1,
        "admission_binding_digest": digest("admission"),
    }
    writer.append(
        event_kind="admission_bound",
        payload={"deployment_snapshot_digest": digest("deployment-one")},
        **common,  # type: ignore[arg-type]
    )
    writer.append(
        event_kind="terminal_egress",
        payload={
            "node_id": "emit",
            "kind": "post_inbox",
            "required_rung": 1,
            "children": [
                {
                    "child_index": 0,
                    "item_key": "00000000.order",
                    "manifest_digest": digest("child-manifest"),
                }
            ],
            "verdict": "delivered",
            "effective_rung": 1,
            "effective_rung_digest": digest("effective-rung"),
            "limiting_term": "line_requested_rung",
            "terms": _term_readings(),
        },
        **common,  # type: ignore[arg-type]
    )

    records = journal.all_records(stream, "line-runs")
    manifest = ExhaustReceiptSetManifestV1(
        stream_id=stream.stream_id,
        partition_id="line-runs",
        first_sequence=1,
        last_sequence=2,
        record_digests=tuple(item.record_digest for item in records),
        payload_digests=tuple(item.record.payload_digest for item in records),
    )
    manifest_digest = exhaust_receipt_set_manifest_digest(manifest)
    assert bodies.store(canonical_bytes(manifest.model_dump(mode="json"))).digest == manifest_digest
    reducer = LineTrackRecordReducer(accepted_line=line, accepted_procedure=accepted_proc)
    output = normalize_canonical(
        reducer.reduce(
            tuple(
                VerifiedExhaustRecordV1(
                    record_digest=stored.record_digest,
                    sequence=stored.record.sequence,
                    event_kind=stored.record.event_kind,
                    generation_digest=stored.record.accepted_coordinate.generation_root,
                    payload_digest=stored.record.payload_digest,
                    payload=parse_journal_payload(
                        bodies.read(stored.record.payload_digest, access=_ACCESS)
                    ),
                    procedure_artifact_digest=stored.record.procedure_artifact_digest,
                    definition_digest=stored.record.definition_digest,
                    run_id=stored.record.run_id,
                    occurrence_id=stored.record.occurrence_id,
                    attempt=stored.record.attempt,
                    line_spec_digest=stored.record.line_spec_digest,
                )
                for stored in records
            )
        )
    )
    output_digest = bodies.store(canonical_bytes(output)).digest
    promotion = ExhaustPromotionV1(
        identity=ArtifactIdentity(kind="ExhaustPromotion", name="orders-triage-window"),
        stream_id=stream.stream_id,
        partition_id="line-runs",
        first_sequence=1,
        last_sequence=2,
        chain_head_digest=records[-1].record_digest,
        receipt_set_manifest_digest=manifest_digest,
        reducer_digest=reducer.reducer_digest,
        output_digest=output_digest,
        bound_generation_digests=(accepted_coordinate.generation_root,),
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        pins=tuple(
            sorted(
                (
                    pin(
                        "procedure",
                        "Procedure",
                        base.identity.name,
                        artifact_digest=accepted_proc.artifact_digest,
                    ),
                    pin(
                        "line",
                        "Line",
                        line.line.identity.name,
                        artifact_digest=line.artifact_digest,
                    ),
                    pin(
                        "reducer",
                        "ExhaustReducer",
                        "line-track-record",
                        artifact_digest=reducer.reducer_digest,
                    ),
                    pin(
                        "receipt-set-manifest",
                        "ReceiptSetManifest",
                        "orders-triage-window",
                        artifact_digest=manifest_digest,
                    ),
                ),
                key=lambda item: (
                    item.role.encode(),
                    item.target.qualified.encode(),
                    item.artifact_digest.encode(),
                ),
            )
        ),
    )
    verifier = LocalExhaustPromotionVerifier(
        instance_id=instance.descriptor.instance_id,
        journal=journal,
        bodies=bodies,
        reducers=ExhaustReducerRegistry({reducer.reducer_digest: reducer}),
    )
    instance = PlaybillInstance.open(
        instance.root,
        trust_root=instance.trust_root,
        promotion_verifier=verifier,
    )
    _accept_tree(
        instance,
        _owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            exhaust_promotion_path(promotion.identity.name): render_exhaust_promotion(promotion),
        },
        timestamp="2026-08-18T16:00:00.000000Z",
        proposal_name="line-track-record-promotion",
    )

    publication = Path(instance.inspect().storage_directories["projections"])
    with bind_current_projection(publication, expected=instance.accepted_coordinate()) as handle:
        connection = sqlite3.connect(handle.index_path)
        try:
            row = connection.execute(
                "SELECT fact_key, value_json FROM semantic_facts "
                "WHERE schema_id = 'playbill.line.track_record' AND subject_identity = ?",
                (line.line.identity.qualified,),
            ).fetchone()
        finally:
            connection.close()
    assert row is not None, "an accepted Line promotion projects its track record"
    assert row[0].startswith("orders-triage-window.epoch-1.")
    projected = json.loads(row[1])
    assert projected["line_id"] == LINE_ID
    record = LineTrackRecordV1.model_validate(projected["track_record"])
    assert record.tally.delivered == 1
    assert record.readings[0].limiting_term == "line_requested_rung"
    assert record.deployment_snapshot_digests == (digest("deployment-one"),)
