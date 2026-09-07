"""Production measurement resolution and exact-grain readings over a real tree.

The world is the served knowledge loop: two accepted work-item Claims, one
accepted QueryDefinition over them, and a query-only Procedure with a guard so
unit, node, and arm grains all really occur (or really do not). Measurements
of all three declared kinds are evaluated from real evidence, resolved under
the frozen law, persisted, credited to the run's exact grain, replayed on
retry, and inspected through the read-only door.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.acquisition_policies import (
    ACQUISITION_POLICY_PIN_ROLE,
    IndependentCoherenceV1,
    InputAcquisitionRuleV1,
    SourceAcquisitionPolicyV1,
    acquisition_policy_digest,
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.claim_verdicts import claim_verdict_v1_compat
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_statement_address,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.errors import PlaybillCasError, PlaybillFormatError
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateV1,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV2,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.graph import (
    compute_procedure_definition_digest_v3,
    compute_procedure_node_digests_v3,
)
from cruxible_client.contracts.procedures.line_specs import (
    LineSpecV1,
    ManualTriggerPolicyV1,
    line_identity_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.measurements import (
    AcceptedQueryProcedureMeasurementV1,
    ClaimAttestationProcedureMeasurementV1,
    ClaimStatementProcedureMeasurementV1,
    ProcedureMeasurementDeclarationV1,
    ProcedureMeasurementExpectationV1,
)
from cruxible_client.contracts.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProjectNodeV3,
    StateTapNodeV3,
    iter_pin_bindings,
)
from cruxible_client.contracts.procedures.readings import (
    PlaybillProcedureMeasureRequestV1,
    PlaybillProcedureReadingsRequestV1,
)
from cruxible_client.contracts.query.definitions import query_definition_digest
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.procedures.readings import procedure_reading_partition_id
from cruxible_core.playbill.procedures.resolution import (
    ProcedureResolutionBook,
    ResolutionContractActivationV1,
    append_resolution_disposition,
    build_resolution_disposition,
    derive_resolution_activations,
    resolution_contract_partition_id,
)
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service import playbill_measurements as measurements
from cruxible_core.service import playbill_procedure_runs as procedure_run_service
from cruxible_core.service.playbill_claim_attestations import service_append_claim_attestation
from cruxible_core.service.playbill_evidence import service_evaluate_playbill_claim_verdict
from cruxible_core.service.playbill_measurements import (
    ProcedureMeasurementRefused,
    load_retained_claim_verdict_observation,
    load_retained_query_receipt,
    measurement_reading_idempotency_key,
    reconstruct_query_evidence,
    service_list_playbill_procedure_readings,
    service_measure_playbill_procedure,
)
from cruxible_core.service.playbill_procedure_runs import (
    LineRunRequestV1,
    ProcedureRunNotFound,
    ProcedureRunRequestV2,
    load_playbill_procedure_run_grain,
    service_get_playbill_procedure_run,
    service_run_playbill_line,
    service_run_playbill_procedure,
)
from tests.test_playbill._candidate_support import (
    submit_member_candidate,
    submit_query_definition_candidate,
)
from tests.test_playbill._knowledge_loop_support import (
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)
from tests.test_playbill.test_claim_attestation_service import _request as _attestation_request
from tests.test_playbill.test_procedure_owned_contracts import _activate_procedure, _contract

ACCEPT_STAMP = "2026-08-24T15:00:00.000000Z"
ACTIVATED_AT = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
RUN_TIME = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)
OBSERVE_AT = datetime(2026, 8, 24, 16, 30, tzinfo=UTC)
RECORD_AT = datetime(2026, 8, 24, 16, 31, tzinfo=UTC)
PROCEDURE_NAME = "measured-work-items"


def _duration(seconds: int) -> CanonicalDurationV1:
    return CanonicalDurationV1(microseconds=seconds * 1_000_000)


def _actor(instance, actor_id: str = "owner") -> GovernedActorContext:  # type: ignore[no-untyped-def]
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id=instance.descriptor.instance_id,
        operation_id="measurement-test",
        timestamp=RECORD_AT,
    )


def _accepted_claim(instance):  # type: ignore[no-untyped-def]
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = sorted(item for item in tree if item.startswith("claims/"))[0]
    claim = parse_claim(tree[path], path=path)
    return path, claim


def _measurements(
    instance,  # type: ignore[no-untyped-def]
    query_pin: ArtifactPin,
    *,
    statement_digest: str | None = None,
    query_options: dict[str, object] | None = None,
) -> tuple[ProcedureMeasurementDeclarationV1, ...]:
    path, claim = _accepted_claim(instance)
    address = claim_statement_address(path)
    digest = statement_digest or claim_statement_digest(claim.statement).tagged
    query = AcceptedQueryProcedureMeasurementV1(
        query=query_pin,
        execution_options=query_options or {},
        expect=ProcedureMeasurementExpectationV1(min_count=1),
    )
    return (
        ProcedureMeasurementDeclarationV1(
            name="rows-present",
            subject_grain="procedure_unit",
            measurement=query,
            check_after=_duration(0),
            expires_after=_duration(86_400),
        ),
        ProcedureMeasurementDeclarationV1(
            name="hot-claim",
            subject_grain="node",
            node_id="hot",
            measurement=ClaimStatementProcedureMeasurementV1(
                claim_statement=address,
                claim_statement_digest=digest,
                acceptable_verdicts=("supported",),
            ),
            check_after=_duration(0),
            expires_after=_duration(86_400),
        ),
        ProcedureMeasurementDeclarationV1(
            name="hot-arm-attested",
            subject_grain="arm",
            node_id="hot",
            from_node_id="gate",
            arm_label="on_true",
            measurement=ClaimAttestationProcedureMeasurementV1(
                claim_statement=address,
                claim_statement_digest=digest,
                stances=("support",),
                expect=ProcedureMeasurementExpectationV1(min_count=1),
            ),
            check_after=_duration(0),
            expires_after=_duration(86_400),
        ),
        ProcedureMeasurementDeclarationV1(
            name="cold-arm-empty",
            subject_grain="arm",
            node_id="cold",
            from_node_id="gate",
            arm_label="on_false",
            measurement=AcceptedQueryProcedureMeasurementV1(
                query=query_pin,
                expect=ProcedureMeasurementExpectationV1(max_count=0),
            ),
            check_after=_duration(0),
            expires_after=_duration(86_400),
        ),
        ProcedureMeasurementDeclarationV1(
            name="late-check",
            subject_grain="procedure_unit",
            measurement=query,
            check_after=_duration(2 * 3600),
            expires_after=_duration(3 * 3600),
        ),
        ProcedureMeasurementDeclarationV1(
            name="expired-early",
            subject_grain="procedure_unit",
            measurement=query,
            check_after=_duration(0),
            expires_after=_duration(1800),
        ),
    )


def _procedure(
    query_digest: str,
    declarations: tuple[ProcedureMeasurementDeclarationV1, ...],
) -> ProcedureArtifactV2:
    input_contract = _contract("empty-input", {})
    output_contract = _contract("query-rows", {"rows": PropertySchema(type="json")})
    contract_in = ArtifactPin(
        role="contract-in",
        target=input_contract.identity,
        artifact_digest=procedure_owned_contract_digest(input_contract).tagged,
    )
    contract_out = ArtifactPin(
        role="contract-out",
        target=output_contract.identity,
        artifact_digest=procedure_owned_contract_digest(output_contract).tagged,
    )
    query = ArtifactPin(
        role="query",
        target=ArtifactIdentity(kind="QueryDefinition", name=QUERY_NAME),
        artifact_digest=query_digest,
    )
    definition = ProcedureDefinitionV3(
        name=PROCEDURE_NAME,
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(node_id="read", query=query, parameters={}, as_="query", next="gate"),
            GuardNodeV3(
                node_id="gate",
                predicate=GuardPredicateV1(
                    left=PredicateOperandV1(kind="count", alias="query"),
                    operator="gt",
                    right=PredicateOperandV1(kind="literal", value=0),
                ),
                on_true="hot",
                on_false="cold",
                refusal_code="empty",
                message="No rows.",
            ),
            ProjectNodeV3(
                node_id="hot",
                fields={"rows": "$steps.query.rows"},
                contract_out=contract_out,
                as_="hot_result",
                next="finish",
            ),
            ProjectNodeV3(
                node_id="cold",
                fields={"rows": "$steps.query.rows"},
                contract_out=contract_out,
                as_="cold_result",
                next="finish",
            ),
            ProjectNodeV3(
                node_id="finish",
                fields={"rows": "$steps.query.rows"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        measurements=tuple(sorted(declarations, key=lambda item: item.name.encode())),
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=4_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
    )
    pins = tuple(
        sorted(
            {
                binding
                for binding in iter_pin_bindings(definition)
                if isinstance(binding, ArtifactPin)
            },
            key=lambda pin: (
                pin.role.encode(),
                pin.target.qualified.encode(),
                pin.artifact_digest.encode(),
            ),
        )
    )
    return ProcedureArtifactV2(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        pins=pins,
        owned_contracts=tuple(
            sorted(
                (input_contract, output_contract),
                key=lambda contract: canonical_bytes(
                    contract.model_dump(mode="json", by_alias=True)
                ),
            )
        ),
        activation_policy="abort",
    )


OTHER_QUERY_NAME = "project.other_items"


def _world(
    tmp_path: Path,
    *,
    statement_digest: str | None = None,
    other_query: str | None = None,
    query_options: dict[str, object] | None = None,
    declarations: tuple[ProcedureMeasurementDeclarationV1, ...] | None = None,
):  # type: ignore[no-untyped-def]
    """``other_query``: declare the measurement over a second QueryDefinition name.

    ``"absent"`` leaves that definition unaccepted; ``"mismatch"`` accepts it
    under a different digest than the declaration pins.
    """

    instance, owner = seed_claims(tmp_path)
    return _world_on(
        instance,
        owner,
        statement_digest=statement_digest,
        other_query=other_query,
        query_options=query_options,
        declarations=declarations,
    )


def _world_on(
    instance,  # type: ignore[no-untyped-def]
    owner,  # type: ignore[no-untyped-def]
    *,
    statement_digest: str | None = None,
    other_query: str | None = None,
    query_options: dict[str, object] | None = None,
    declarations: tuple[ProcedureMeasurementDeclarationV1, ...] | None = None,
):
    """Accept the query and the measured Procedure into an instance that holds the seed Claims."""

    query = work_item_query()
    inspection = submit_query_definition_candidate(
        instance,
        query=query,
        actor_id="owner",
        proposal_name="measured-procedure-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)
    accepted_query_digest = query_definition_digest(query).tagged
    query_pin = ArtifactPin(
        role="query",
        target=ArtifactIdentity(kind="QueryDefinition", name=QUERY_NAME),
        artifact_digest=accepted_query_digest,
    )
    if other_query is not None:
        other = work_item_query(OTHER_QUERY_NAME)
        if other_query == "mismatch":
            inspection = submit_query_definition_candidate(
                instance,
                query=other,
                actor_id="owner",
                proposal_name="measured-procedure-other-query",
                timestamp=TIMESTAMP,
            )
            accept_proposal(instance, owner, inspection)
        query_pin = ArtifactPin(
            role="query",
            target=ArtifactIdentity(kind="QueryDefinition", name=OTHER_QUERY_NAME),
            artifact_digest="sha256:" + "2" * 64,
        )
    if declarations is None:
        declarations = _measurements(
            instance,
            query_pin,
            statement_digest=statement_digest,
            query_options=query_options,
        )
    procedure = _procedure(accepted_query_digest, declarations)
    _activate_procedure(instance, owner, procedure, sequence=4, timestamp=ACCEPT_STAMP)
    return instance, owner, procedure


def _run(instance, procedure, *, at: datetime = RUN_TIME):  # type: ignore[no-untyped-def]
    run = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=ProcedureRunRequestV2(evaluation_time=at, input={}),
        actor_context=_actor(instance),
    )
    assert run.status == "succeeded", run.terminal
    assert run.run_id is not None
    return run


def _attest(  # type: ignore[no-untyped-def]
    instance,
    owner,
    tmp_path: Path,
    *,
    stance: str = "support",
    attested_at: datetime = RUN_TIME - timedelta(minutes=5),
    recorded_at: datetime | None = None,
):
    path, _claim = _accepted_claim(instance)
    claim_id = path.rsplit("/", 1)[-1].removesuffix(".json")
    request = _attestation_request(
        instance,
        owner,
        claim_id,
        tmp_path,
        stance=stance,
        attested_at=attested_at,
    )
    return service_append_claim_attestation(
        instance,
        request=request,
        actor_id="owner",
        recorded_at=attested_at + timedelta(minutes=1) if recorded_at is None else recorded_at,
    )


def _measure(  # type: ignore[no-untyped-def]
    instance,
    procedure,
    *,
    run_id=None,
    names=(),
    at=OBSERVE_AT,
    recorded=RECORD_AT,
    actor: GovernedActorContext | None = None,
):
    return service_measure_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureMeasureRequestV1(
            run_id=run_id,
            measurement_names=tuple(sorted(names)),
            evaluation_time=at,
        ),
        actor_context=_actor(instance) if actor is None else actor,
        recorded_at=recorded,
    )


def _rows(result):  # type: ignore[no-untyped-def]
    return {row.measurement_name: row for row in result.rows}


# ---------------------------------------------------------------------------
# All three kinds, all three grains, real evidence
# ---------------------------------------------------------------------------


def test_all_kinds_and_grains_resolve_from_real_evidence_and_credit_the_run(
    tmp_path: Path,
) -> None:
    instance, owner, procedure = _world(tmp_path)
    _attest(instance, owner, tmp_path)
    run = _run(instance, procedure)

    result = _measure(instance, procedure, run_id=run.run_id)
    rows = _rows(result)

    # Activation is the accepting generation's signed instant, not the run's;
    # the run's admission coordinate is carried separately from both.
    assert result.run_admission_coordinate is not None
    assert result.run_admission_coordinate.git_oid == run.bound_coordinate.git_oid
    assert all(
        row.eligibility.activation_coordinate == result.activation_coordinate for row in result.rows
    )
    assert all(row.eligibility.activated_at == ACTIVATED_AT for row in result.rows)
    assert all(row.eligibility.observation_time == OBSERVE_AT for row in result.rows)

    unit = rows["rows-present"]
    assert unit.status == "resolved" and unit.resolution is not None
    assert unit.resolution.verdict == "satisfied" and unit.resolution.written_now
    assert unit.resolution.value["count"] == 2  # type: ignore[index]
    assert unit.resolution.evidence_refs[0]["kind"] == "query_receipt"
    assert unit.reading_status == "recorded" and unit.reading is not None
    assert unit.reading.run_id == run.run_id
    assert unit.reading.run_receipt_digest == run.receipt_digest
    assert unit.reading.grade == "contract"
    assert unit.reading.observed_at == OBSERVE_AT

    node = rows["hot-claim"]
    assert node.resolution is not None and node.resolution.verdict == "satisfied"
    assert node.resolution.value == "supported"
    assert node.reading is not None and node.reading.node_id == "hot"
    expected = compute_procedure_node_digests_v3(procedure.definition)
    assert node.reading.subject.selector.scheme == "procedure-node-v1"

    arm = rows["hot-arm-attested"]
    assert arm.resolution is not None and arm.resolution.verdict == "satisfied"
    assert arm.resolution.value["count"] == 1  # type: ignore[index]
    assert arm.reading is not None and arm.reading.arm_label == "on_true"
    assert arm.reading.from_node_id == "gate" and arm.reading.node_id == "hot"
    assert len(arm.reading.claim_attestation_digests) == 1
    assert arm.reading.subject.selector.value == "gate:on_true:hot"

    # The untaken arm resolves (contradicted: two rows, max_count 0) but earns
    # no reading, because the run never went that way.
    cold = rows["cold-arm-empty"]
    assert cold.resolution is not None and cold.resolution.verdict == "contradicted"
    assert cold.resolution.note
    assert cold.reading_status == "grain_not_occurred" and cold.reading is None
    assert "on_true" in (cold.detail or "")

    # Window law: before check_at is pending, after expires_at is expired;
    # neither writes, and neither can grade a reading.
    late = rows["late-check"]
    assert late.status == "pending" and late.resolution is None
    assert late.eligibility.window == "before_check"
    assert late.reading_status == "no_resolution"
    early = rows["expired-early"]
    assert early.status == "expired" and early.resolution is None
    assert early.eligibility.window == "closed"
    assert early.reading_status == "no_resolution"

    # Grain digests are the activation's, which are the graph's.
    activation = next(
        item
        for item in derive_resolution_activations(
            AcceptedProcedureV1(
                path=procedure_path(procedure.identity.name),
                procedure=procedure,
                artifact_digest=procedure_artifact_digest(procedure).tagged,
            ),
            accepted_coordinate=result.activation_coordinate,
            activated_at=ACTIVATED_AT,
        )
        if item.measurement_name == "hot-claim"
    )
    assert activation.node_local_digest == expected["hot"].local_digest
    assert node.contract_id == activation.contract_id


def test_execution_outcome_and_measurement_verdict_stay_distinct(tmp_path: Path) -> None:
    """A succeeded run does not satisfy a measurement whose evidence says otherwise."""

    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)

    result = _measure(instance, procedure, run_id=run.run_id, names=("hot-arm-attested",))
    row = _rows(result)["hot-arm-attested"]

    # No attestation exists: the frozen law forbids a proof-less contradiction,
    # so the honest answer is indeterminate, and the arm still gets its reading
    # because it really occurred.
    assert row.resolution is not None and row.resolution.verdict == "indeterminate"
    assert row.resolution.evidence_refs == ()
    assert row.reading is not None and row.reading.verdict == "indeterminate"


# ---------------------------------------------------------------------------
# Standing resolutions, retries, crash resume, overturn
# ---------------------------------------------------------------------------


def test_retry_replays_the_standing_resolution_and_reading(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)

    first = _rows(_measure(instance, procedure, run_id=run.run_id, names=("rows-present",)))
    again = _rows(
        _measure(
            instance,
            procedure,
            run_id=run.run_id,
            names=("rows-present",),
            at=OBSERVE_AT + timedelta(minutes=10),
            recorded=RECORD_AT + timedelta(minutes=10),
        )
    )
    a, b = first["rows-present"], again["rows-present"]
    assert a.resolution is not None and b.resolution is not None
    assert b.resolution.resolution_id == a.resolution.resolution_id
    assert b.resolution.observed_at == OBSERVE_AT and not b.resolution.written_now
    assert b.reading_status == "replayed"
    assert b.reading is not None and a.reading is not None
    assert b.reading.reading_id == a.reading.reading_id
    assert b.reading.journal_record_digest == a.reading.journal_record_digest

    listed = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(measurement_names=("rows-present",)),
        evaluation_time=RECORD_AT,
    )
    assert len(listed.readings) == 1
    assert listed.contracts[0].reading_count == 1


def test_crash_between_resolution_and_reading_resumes_at_the_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)

    def crash(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("daemon died between the two appends")

    monkeypatch.setattr(measurements, "append_procedure_reading", crash)
    with pytest.raises(RuntimeError):
        _measure(instance, procedure, run_id=run.run_id, names=("rows-present",))
    monkeypatch.undo()

    resumed = _rows(
        _measure(
            instance,
            procedure,
            run_id=run.run_id,
            names=("rows-present",),
            at=OBSERVE_AT + timedelta(minutes=1),
            recorded=RECORD_AT + timedelta(minutes=1),
        )
    )["rows-present"]
    assert resumed.resolution is not None and not resumed.resolution.written_now
    assert resumed.resolution.observed_at == OBSERVE_AT
    assert resumed.reading_status == "recorded"
    listed = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(),
        evaluation_time=RECORD_AT,
    )
    assert [row.measurement_name for row in listed.readings] == ["rows-present"]


def test_two_runs_each_earn_one_reading_and_a_repeated_key_never_doubles(
    tmp_path: Path,
) -> None:
    instance, _owner, procedure = _world(tmp_path)
    first = _run(instance, procedure)
    second = _run(instance, procedure, at=RUN_TIME + timedelta(minutes=1))
    assert first.run_id != second.run_id

    for run in (first, second, first):
        _measure(instance, procedure, run_id=run.run_id, names=("rows-present",))

    listed = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(),
        evaluation_time=RECORD_AT,
    )
    assert sorted(row.run_id for row in listed.readings) == sorted(  # type: ignore[type-var]
        [first.run_id, second.run_id]
    )
    standing = next(item for item in listed.contracts if item.measurement_name == "rows-present")
    assert standing.resolution is not None
    assert {row.resolution_id for row in listed.readings} == {standing.resolution.resolution_id}


def test_overturn_reopens_the_contract_and_the_old_key_refuses_a_new_answer(
    tmp_path: Path,
) -> None:
    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)
    first = _rows(_measure(instance, procedure, run_id=run.run_id, names=("rows-present",)))[
        "rows-present"
    ]
    assert first.resolution is not None

    accepted = AcceptedProcedureV1(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    activation = next(
        item
        for item in derive_resolution_activations(
            accepted,
            accepted_coordinate=first.eligibility.activation_coordinate,
            activated_at=ACTIVATED_AT,
        )
        if item.measurement_name == "rows-present"
    )
    journal, stream = measurements._journal(instance)  # noqa: SLF001
    partition = resolution_contract_partition_id(activation)
    book = ProcedureResolutionBook((activation,))
    book.replay(journal.all_records(stream, partition), bodies=instance.body_store())
    standing = book.latest_non_overturned(activation.contract_id)
    assert standing is not None
    fenced = measurements._FencedWriter(instance, journal)  # noqa: SLF001
    fenced.acquire(stream, partition)
    try:
        append_resolution_disposition(
            fenced.writer,
            activation=activation,
            resolution=standing,
            disposition=build_resolution_disposition(
                standing,
                sequence=1,
                verdict="overturned",
                reviewer_actor_context=_actor(instance, "reviewer"),
                recorded_at=RECORD_AT + timedelta(minutes=2),
            ),
            stream=stream,
        )
    finally:
        fenced.release()

    # The old answer is gone; a new evaluation lands as sequence 2, and the
    # run's existing reading (bound to the overturned answer) refuses to be
    # silently rewritten under the same key.
    with pytest.raises(ProcedureMeasurementRefused) as refused:
        _measure(
            instance,
            procedure,
            run_id=run.run_id,
            names=("rows-present",),
            at=OBSERVE_AT + timedelta(minutes=5),
            recorded=RECORD_AT + timedelta(minutes=5),
        )
    assert refused.value.error_code == "measurement_reading_conflict"
    listed = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(measurement_names=("rows-present",)),
        evaluation_time=RECORD_AT + timedelta(minutes=6),
    )
    contract = listed.contracts[0]
    assert contract.resolution is not None and contract.resolution.sequence == 2
    assert contract.resolution.resolution_id != first.resolution.resolution_id
    assert len(listed.readings) == 1

    # A different run is a different key and credits the new answer.
    later = _run(instance, procedure, at=RUN_TIME + timedelta(minutes=3))
    row = _rows(
        _measure(
            instance,
            procedure,
            run_id=later.run_id,
            names=("rows-present",),
            at=OBSERVE_AT + timedelta(minutes=6),
            recorded=RECORD_AT + timedelta(minutes=6),
        )
    )["rows-present"]
    assert (
        row.reading is not None and row.reading.resolution_id == contract.resolution.resolution_id
    )


def test_run_not_final_and_run_mismatch_are_explicit(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)
    grain = load_playbill_procedure_run_grain(instance, run_id=run.run_id)
    assert grain.finalized and grain.invocation_origin == "actor"
    assert grain.node_verdicts == {
        "read": "succeeded",
        "gate": "succeeded",
        "hot": "succeeded",
        "finish": "succeeded",
    }
    assert grain.selected_arms == {"gate": ("on_true",)}

    with pytest.raises(ProcedureRunNotFound):
        service_measure_playbill_procedure(
            instance,
            name=procedure.identity.name,
            request=PlaybillProcedureMeasureRequestV1(run_id="RUN-" + "0" * 64),
            actor_context=_actor(instance),
            recorded_at=RECORD_AT,
        )


def test_idempotency_key_collapses_line_attempts_but_separates_direct_runs(
    tmp_path: Path,
) -> None:
    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)
    grain = load_playbill_procedure_run_grain(instance, run_id=run.run_id)
    accepted = AcceptedProcedureV1(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    activation = derive_resolution_activations(
        accepted,
        accepted_coordinate=measurements.measurement_activation_basis(
            instance, accepted=accepted, observation=instance.accepted_coordinate()
        ).coordinate,
        activated_at=ACTIVATED_AT,
    )[0]
    direct_key = measurement_reading_idempotency_key(activation, grain)
    assert direct_key.endswith(run.run_id)  # type: ignore[arg-type]

    class _LineGrain:
        state = grain.state
        occurrence_id = "OCC-1"

    line_key_a = measurement_reading_idempotency_key(activation, _LineGrain())  # type: ignore[arg-type]
    assert line_key_a.endswith("OCC-1")
    assert line_key_a != direct_key


# ---------------------------------------------------------------------------
# Subject and basis refusals, truncation, evidence closure
# ---------------------------------------------------------------------------


def test_statement_digest_mismatch_refuses_typed(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path, statement_digest="sha256:" + "1" * 64)
    with pytest.raises(ProcedureMeasurementRefused) as refused:
        _measure(instance, procedure, names=("hot-claim",))
    assert refused.value.error_code == "measurement_subject_mismatch"


def test_query_pin_mismatch_and_absence_refuse_typed(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path, other_query="mismatch")
    with pytest.raises(ProcedureMeasurementRefused) as refused:
        _measure(instance, procedure, names=("rows-present",))
    assert refused.value.error_code == "measurement_subject_mismatch"

    (tmp_path / "absent").mkdir()
    instance, _owner, procedure = _world(tmp_path / "absent", other_query="absent")
    with pytest.raises(ProcedureMeasurementRefused) as absent:
        _measure(instance, procedure, names=("rows-present",))
    assert absent.value.error_code == "measurement_subject_absent"


def test_undeclared_measurement_and_unsupported_basis_refuse_typed(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path, query_options={"relationship_state": "x"})
    with pytest.raises(ProcedureMeasurementRefused) as undeclared:
        _measure(instance, procedure, names=("nope",))
    assert undeclared.value.error_code == "measurement_not_declared"
    with pytest.raises(ProcedureMeasurementRefused) as basis:
        _measure(instance, procedure, names=("rows-present",))
    assert basis.value.error_code == "measurement_basis_unsupported"


def test_truncated_query_is_indeterminate_and_retains_a_reconstructable_receipt(
    tmp_path: Path,
) -> None:
    instance, _owner, procedure = _world(
        tmp_path, query_options={"max_results": 1, "max_traversal_depth": 0}
    )
    row = _rows(_measure(instance, procedure, names=("rows-present",)))["rows-present"]
    assert row.resolution is not None and row.resolution.verdict == "indeterminate"
    assert row.resolution.value["status"] == "truncated"  # type: ignore[index]
    assert row.reading_status == "not_requested"

    proof = row.resolution.evidence_refs[0]
    retained = load_retained_query_receipt(instance, record_digest=str(proof["digest"]))
    assert retained.receipt.truncation.candidate_result_count == 2
    assert reconstruct_query_evidence(instance, evidence=retained)
    with pytest.raises(PlaybillFormatError):
        load_retained_query_receipt(instance, record_digest="sha256:" + "f" * 64)


def test_readings_inspection_is_bounded_paginated_and_never_writes(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    runs = [_run(instance, procedure, at=RUN_TIME + timedelta(minutes=i)) for i in range(3)]
    for run in runs:
        _measure(instance, procedure, run_id=run.run_id, names=("rows-present", "hot-claim"))
    journal, stream = measurements._journal(instance)  # noqa: SLF001
    partitions_before = journal.partition_ids(stream)

    first = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(limit=4),
        evaluation_time=RECORD_AT,
    )
    assert len(first.readings) == 4 and first.truncated and first.cursor
    second = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(limit=4, cursor=first.cursor),
        evaluation_time=RECORD_AT,
    )
    assert len(second.readings) == 2 and not second.truncated
    ids = {row.reading_id for row in (*first.readings, *second.readings)}
    assert len(ids) == 6
    only_run = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(run_id=runs[1].run_id),
        evaluation_time=RECORD_AT,
    )
    assert {row.run_id for row in only_run.readings} == {runs[1].run_id}
    assert journal.partition_ids(stream) == partitions_before
    with pytest.raises(PlaybillFormatError):
        service_list_playbill_procedure_readings(
            instance,
            name=procedure.identity.name,
            request=PlaybillProcedureReadingsRequestV1(
                run_id=runs[0].run_id, limit=4, cursor=first.cursor
            ),
            evaluation_time=RECORD_AT,
        )


def test_reading_partition_stays_separate_from_run_authority(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)
    before = service_get_playbill_procedure_run(instance, run_id=run.run_id)
    _measure(instance, procedure, run_id=run.run_id)
    assert service_get_playbill_procedure_run(instance, run_id=run.run_id) == before


def test_no_measurement_fast_path_writes_nothing(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path, declarations=())
    run = _run(instance, procedure)
    journal, stream = measurements._journal(instance)  # noqa: SLF001
    before = journal.partition_ids(stream)
    result = _measure(instance, procedure, run_id=run.run_id)
    assert result.rows == ()
    assert journal.partition_ids(stream) == before


def test_activation_memo_is_keyed_on_revision_and_observation(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    accepted = AcceptedProcedureV1(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    observation = instance.accepted_coordinate()
    basis = measurements.measurement_activation_basis(
        instance, accepted=accepted, observation=observation
    )
    assert basis.activated_at == ACTIVATED_AT
    assert isinstance(basis.activations[0], ResolutionContractActivationV1)
    assert (
        measurements.measurement_activation_basis(
            instance, accepted=accepted, observation=observation
        )
        is basis
    )


# ---------------------------------------------------------------------------
# Review regressions: concurrency, retry identity, warm-body integrity,
# continuation, retained observation basis, temporal attestation selection
# ---------------------------------------------------------------------------


def _reading_records(instance, procedure):  # type: ignore[no-untyped-def]
    journal, stream = measurements._journal(instance)  # noqa: SLF001
    partition = procedure_reading_partition_id(
        AcceptedProcedureV1(
            path=procedure_path(procedure.identity.name),
            procedure=procedure,
            artifact_digest=procedure_artifact_digest(procedure).tagged,
        )
    )
    return [
        stored
        for stored in journal.all_records(stream, partition)
        if stored.record.event_kind == "procedure_reading"
    ]


def test_concurrent_credit_of_one_grain_lands_exactly_one_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests race on the same key; the second append is a replay, not a credit."""

    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)
    original = measurements.append_procedure_reading
    competitor: list[object] = []
    raced = False

    def racing_append(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal raced
        if not raced:
            raced = True
            # The competing request lands the same reading after this request
            # indexed the partition and before it appends.
            competitor.append(
                _rows(
                    _measure(
                        instance,
                        procedure,
                        run_id=run.run_id,
                        names=("rows-present",),
                        actor=_actor(instance).model_copy(update={"operation_id": "competitor"}),
                    )
                )["rows-present"]
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(measurements, "append_procedure_reading", racing_append)
    outer = _rows(_measure(instance, procedure, run_id=run.run_id, names=("rows-present",)))[
        "rows-present"
    ]

    inner = competitor[0]
    assert inner.reading_status == "recorded"  # type: ignore[attr-defined]
    assert outer.reading_status == "replayed"
    assert outer.reading is not None and inner.reading is not None  # type: ignore[attr-defined]
    assert outer.reading.reading_id == inner.reading.reading_id  # type: ignore[attr-defined]
    assert len(_reading_records(instance, procedure)) == 1


def test_a_fresh_authenticated_retry_replays_the_standing_reading(tmp_path: Path) -> None:
    """A retry re-mints request attribution; the reading's meaning is unchanged."""

    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)
    first = _rows(_measure(instance, procedure, run_id=run.run_id, names=("rows-present",)))[
        "rows-present"
    ]
    retried = _rows(
        _measure(
            instance,
            procedure,
            run_id=run.run_id,
            names=("rows-present",),
            recorded=RECORD_AT + timedelta(seconds=7),
            actor=_actor(instance).model_copy(
                update={
                    "operation_id": "op_second_request",
                    "request_id": "req-2",
                    "timestamp": RECORD_AT + timedelta(seconds=7),
                }
            ),
        )
    )["rows-present"]

    assert first.reading_status == "recorded" and retried.reading_status == "replayed"
    assert first.reading is not None and retried.reading is not None
    assert retried.reading.reading_id == first.reading.reading_id
    assert retried.reading.recorded_at == first.reading.recorded_at
    assert len(_reading_records(instance, procedure)) == 1

    # Another PRINCIPAL is another reading key, never a replay of this one.
    other = _rows(
        _measure(
            instance,
            procedure,
            run_id=run.run_id,
            names=("rows-present",),
            actor=_actor(instance, "auditor"),
        )
    )["rows-present"]
    assert other.reading_status == "recorded"
    assert other.reading is not None and other.reading.reading_id != first.reading.reading_id


def test_a_warm_index_never_vouches_for_a_body_cas_cannot_show(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    run = _run(instance, procedure)
    _measure(instance, procedure, run_id=run.run_id, names=("rows-present",))
    warm = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(),
        evaluation_time=RECORD_AT,
    )
    assert len(warm.readings) == 1

    stored = _reading_records(instance, procedure)[0]
    body_path = instance.body_store()._path(stored.record.payload_digest)  # noqa: SLF001
    original = body_path.read_bytes()
    body_path.chmod(0o600)
    body_path.write_bytes(b"corrupted")

    # Warm inspection and warm retry both refuse exactly as a cold process does.
    with pytest.raises(PlaybillCasError):
        service_list_playbill_procedure_readings(
            instance,
            name=procedure.identity.name,
            request=PlaybillProcedureReadingsRequestV1(),
            evaluation_time=RECORD_AT,
        )
    with pytest.raises(PlaybillCasError):
        _measure(instance, procedure, run_id=run.run_id, names=("rows-present",))
    assert len(_reading_records(instance, procedure)) == 1

    body_path.write_bytes(original)
    restored = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(),
        evaluation_time=RECORD_AT,
    )
    assert [row.reading_id for row in restored.readings] == [warm.readings[0].reading_id]


def test_a_cursor_continues_the_first_pages_selection_under_a_moving_clock(
    tmp_path: Path,
) -> None:
    instance, _owner, procedure = _world(tmp_path)
    runs = [_run(instance, procedure, at=RUN_TIME + timedelta(minutes=i)) for i in range(3)]
    for run in runs:
        _measure(instance, procedure, run_id=run.run_id, names=("rows-present", "hot-claim"))

    first = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(limit=4),
        evaluation_time=RECORD_AT,
    )
    assert first.truncated and first.cursor
    # The SDK stamps a fresh instant on every call: the continuation keeps the
    # first page's observation, the request's later instant does not re-select.
    second = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(limit=4, cursor=first.cursor),
        evaluation_time=RECORD_AT + timedelta(hours=3),
    )
    assert second.observation_time == first.observation_time == RECORD_AT
    assert second.observation_coordinate == first.observation_coordinate
    assert not second.truncated
    assert len({row.reading_id for row in (*first.readings, *second.readings)}) == 6
    with pytest.raises(PlaybillFormatError):
        service_list_playbill_procedure_readings(
            instance,
            name=procedure.identity.name,
            request=PlaybillProcedureReadingsRequestV1(
                run_id=runs[0].run_id, limit=4, cursor=first.cursor
            ),
            evaluation_time=RECORD_AT,
        )


def test_a_claim_statement_resolution_retains_the_observation_that_produced_it(
    tmp_path: Path,
) -> None:
    instance, owner, procedure = _world(tmp_path)
    activation_coordinate = instance.accepted_coordinate()
    # An unrelated generation moves the head: the observation is later than
    # the activation, and the retained account must say which one it read.
    inspection = submit_query_definition_candidate(
        instance,
        query=work_item_query(OTHER_QUERY_NAME),
        actor_id="owner",
        proposal_name="unrelated-generation",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)
    observation_coordinate = instance.accepted_coordinate()
    assert observation_coordinate.git_oid != activation_coordinate.git_oid

    row = _rows(_measure(instance, procedure, names=("hot-claim",)))["hot-claim"]
    assert row.resolution is not None and row.resolution.value == "supported"
    assert row.eligibility.activation_coordinate.git_oid == activation_coordinate.git_oid
    proofs = {proof["kind"]: proof["digest"] for proof in row.resolution.evidence_refs}
    assert set(proofs) == {"claim_statement", "journal_record"}

    retained = load_retained_claim_verdict_observation(
        instance, record_digest=proofs["journal_record"]
    )
    account = retained.observation
    assert account.observation_coordinate.git_oid == observation_coordinate.git_oid
    assert account.observation_time == OBSERVE_AT
    path, claim = _accepted_claim(instance)
    assert account.claim_artifact_path == path
    assert account.claim_artifact_digest == claim_artifact_digest(claim).tagged
    assert account.claim_statement_digest == claim_statement_digest(claim.statement).tagged
    assert account.verdict_result.verdict == "supported"
    assert account.verdict_result.evaluation_time == OBSERVE_AT
    assert retained.stored.record.accepted_coordinate.git_oid == observation_coordinate.git_oid

    # The account reproduces from retained material alone: the same Claim at
    # the same coordinate and instant yields the same verdict inputs.
    reproduced = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=account.claim_identity,
        evaluation_time=account.observation_time,
        at=PlaybillAcceptedCoordinate.model_validate(
            account.observation_coordinate.model_dump(mode="json")
        ),
    )
    assert claim_verdict_v1_compat(reproduced.verdict) == account.verdict_result


def test_attestation_evidence_is_selected_at_the_observed_instant(tmp_path: Path) -> None:
    """A principal's later word cannot erase the word that stood when observed."""

    instance, owner, procedure = _world(tmp_path)
    _attest(instance, owner, tmp_path, stance="support", attested_at=RUN_TIME)
    _attest(
        instance,
        owner,
        tmp_path,
        stance="contradict",
        attested_at=OBSERVE_AT + timedelta(minutes=10),
    )
    store = instance.claim_attestation_evidence_store()
    assert len(store.fold_events(at_head=store.head())) == 1, "the fold keeps the latest only"

    row = _rows(_measure(instance, procedure, names=("hot-arm-attested",)))["hot-arm-attested"]
    assert row.resolution is not None and row.resolution.verdict == "satisfied"
    value = row.resolution.value
    assert isinstance(value, dict) and value["count"] == 1
    items = value["items"]
    assert isinstance(items, list) and items[0]["stance"] == "support"  # type: ignore[index]
    assert datetime.fromisoformat(str(value["observation_time"])) == OBSERVE_AT
    assert isinstance(value["observation_coordinate"], dict)


def test_a_contradiction_that_stood_at_the_observation_is_the_principals_word(
    tmp_path: Path,
) -> None:
    instance, owner, procedure = _world(tmp_path)
    _attest(instance, owner, tmp_path, stance="support", attested_at=RUN_TIME)
    _attest(
        instance,
        owner,
        tmp_path,
        stance="contradict",
        attested_at=OBSERVE_AT - timedelta(minutes=10),
    )
    row = _rows(_measure(instance, procedure, names=("hot-arm-attested",)))["hot-arm-attested"]
    assert row.resolution is not None and row.resolution.verdict == "indeterminate"
    assert row.resolution.evidence_refs == ()


def test_zero_attestations_never_satisfy_a_max_count_of_zero_without_proof(
    tmp_path: Path,
) -> None:
    instance, owner = seed_claims(tmp_path)
    path, claim = _accepted_claim(instance)
    declaration = ProcedureMeasurementDeclarationV1(
        name="nobody-objects",
        subject_grain="procedure_unit",
        measurement=ClaimAttestationProcedureMeasurementV1(
            claim_statement=claim_statement_address(path),
            claim_statement_digest=claim_statement_digest(claim.statement).tagged,
            stances=("contradict",),
            expect=ProcedureMeasurementExpectationV1(max_count=0),
        ),
        check_after=_duration(0),
        expires_after=_duration(86_400),
    )
    instance, _owner, procedure = _world_on(instance, owner, declarations=(declaration,))

    row = _rows(_measure(instance, procedure, names=("nobody-objects",)))["nobody-objects"]
    assert row.status == "resolved" and row.resolution is not None
    assert row.resolution.verdict == "indeterminate"
    assert row.resolution.evidence_refs == ()
    assert row.resolution.note and "no verified attestation" in row.resolution.note


# ---------------------------------------------------------------------------
# The Line lane, end to end
# ---------------------------------------------------------------------------


def _line_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    """The measured Procedure behind an accepted manual Line, mandate, and policy."""

    instance, owner, procedure = _world(tmp_path)
    procedure_pin = ArtifactPin(
        role="procedure",
        target=procedure.identity,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    policy = SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="measured-reads"),
        inputs=(
            InputAcquisitionRuleV1(
                input_name="query",
                requirement="required",
                permitted_replayability=("attested_only", "exact"),
                max_age=_duration(3600),
                on_unavailable="refuse",
                on_stale="refuse",
                on_oversized="refuse",
                on_conflict="preserve",
            ),
        ),
        coherence=IndependentCoherenceV1(),
    )
    policy_pin = ArtifactPin(
        role=ACQUISITION_POLICY_PIN_ROLE,
        target=policy.identity,
        artifact_digest=acquisition_policy_digest(policy).tagged,
    )
    caps = procedure.definition.hard_caps
    line = LineSpecV1(
        identity=ArtifactIdentity(kind="Line", name="measured-line"),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={},
        slot_bindings=(),
        trigger_policy=ManualTriggerPolicyV1(),
        acquisition_policy=policy_pin,
        requested_terminal_rung=1,
        budgets={
            "max_capture_bytes": 0,
            "max_items": caps.max_items,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
        },
        epsilon={"$decimal": "0.1"},
        pins=tuple(
            sorted((procedure_pin, policy_pin), key=lambda pin: (pin.role, pin.target.qualified))
        ),
    )
    mandate = ProcedureMandateV1(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="measured-line-mandate"),
        procedure=procedure_pin,
        rung=2,
        authority_ceiling=caps,
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    inspection = submit_member_candidate(
        instance,
        members={
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        actor_id="owner",
        proposal_name="measured-line",
        proposal_family="line",
        timestamp="2026-08-24T15:30:00.000000Z",
    )
    accept_proposal(instance, owner, inspection)
    return instance, procedure, line


def _run_line(instance, line, *, at: datetime):  # type: ignore[no-untyped-def]
    digest = line_identity_digest(line.identity)
    return service_run_playbill_line(
        instance,
        path_identity_digest=digest,
        request=LineRunRequestV1(line_identity_digest=digest, evaluation_time=None),
        actor_context=_actor(instance),
        caller_rung=2,
        daemon_clock=procedure_run_service._DeterministicClock(at),  # noqa: SLF001
    )


def test_a_real_line_occurrence_is_credited_once_per_occurrence(tmp_path: Path) -> None:
    instance, procedure, line = _line_world(tmp_path)

    first = _run_line(instance, line, at=RUN_TIME)
    assert first.status == "succeeded", first.terminal
    assert first.run_id is not None
    grain = load_playbill_procedure_run_grain(instance, run_id=first.run_id)
    assert grain.invocation_origin == "line" and grain.occurrence_id is not None

    credited = _rows(_measure(instance, procedure, run_id=first.run_id, names=("rows-present",)))[
        "rows-present"
    ]
    assert credited.reading_status == "recorded" and credited.reading is not None
    assert credited.reading.episode_ref == grain.occurrence_id
    assert credited.reading.run_id == first.run_id
    assert credited.reading.idempotency_key is not None
    assert credited.reading.idempotency_key.endswith(grain.occurrence_id)
    assert first.run_id not in credited.reading.idempotency_key

    # A fresh authenticated retry replays; the same occurrence cannot run twice.
    retried = _rows(
        _measure(
            instance,
            procedure,
            run_id=first.run_id,
            names=("rows-present",),
            recorded=RECORD_AT + timedelta(minutes=1),
            actor=_actor(instance).model_copy(update={"operation_id": "op_retry"}),
        )
    )["rows-present"]
    assert retried.reading_status == "replayed"
    again = _run_line(instance, line, at=RUN_TIME)
    assert again.status == "admission_refused"
    assert again.terminal is not None and again.terminal.code == "occurrence_already_admitted"  # type: ignore[union-attr]

    # The next occurrence is its own grain and earns its own credit.
    second = _run_line(instance, line, at=RUN_TIME + timedelta(minutes=30))
    assert second.status == "succeeded" and second.run_id is not None
    later = _rows(
        _measure(
            instance,
            procedure,
            run_id=second.run_id,
            names=("rows-present",),
            at=OBSERVE_AT + timedelta(minutes=30),
            recorded=RECORD_AT + timedelta(minutes=30),
        )
    )["rows-present"]
    assert later.reading_status == "recorded" and later.reading is not None
    assert later.reading.episode_ref != grain.occurrence_id
    listed = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureReadingsRequestV1(run_id=second.run_id),
        evaluation_time=RECORD_AT + timedelta(minutes=31),
    )
    assert [row.run_id for row in listed.readings] == [second.run_id]
    assert len(_reading_records(instance, procedure)) == 2
