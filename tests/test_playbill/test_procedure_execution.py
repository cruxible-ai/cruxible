"""Authenticated graph-v3 execution and log-sufficiency laws."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    GenerationRoot,
    SemanticRoot,
    typed_digest,
)
from cruxible_core.playbill.captures import CanonicalDurationV1
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.errors import PlaybillExecutionError
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
)
from cruxible_core.playbill.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_core.playbill.procedures.execution import (
    ProcedureExecutor,
    ProviderInvocationResultV1,
    prepare_direct_procedure_run,
)
from cruxible_core.playbill.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_core.playbill.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProjectNodeV3,
    ProviderNodeV3,
    SourceNodeV3,
    StateTapNodeV3,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.projection import AcceptedCoordinate

NOW = datetime(2026, 8, 17, 13, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-execution-test-v1",
        {"label": label},
    ).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root=typed_digest(
            SemanticRoot,
            "playbill-procedure-execution-semantic-v1",
            {"value": "accepted"},
        ).tagged,
        generation_root=typed_digest(
            GenerationRoot,
            "playbill-procedure-execution-generation-v1",
            {"value": "accepted"},
        ).tagged,
        compiler_digest=_digest("compiler"),
    )


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="operator",
        org_id="instance-a",
        operation_id="operation-a",
        timestamp=NOW,
    )


def _budget(*, providers: int = 0, items: int = 100) -> ProcedureBudgetV3:
    return ProcedureBudgetV3(
        wall_clock=CanonicalDurationV1(microseconds=1_000_000),
        max_provider_calls=providers,
        max_capture_bytes=0,
        max_items=items,
    )


def _hard_caps(*, providers: int = 0, items: int = 100) -> ProcedureHardCapsV3:
    return ProcedureHardCapsV3(
        max_wall_clock=CanonicalDurationV1(microseconds=2_000_000),
        max_provider_calls=providers,
        max_capture_bytes=0,
        max_items=items,
        max_repeat_attempts=3,
    )


def _accepted(
    definition: ProcedureDefinitionV3,
    *,
    pins: tuple[ArtifactPin, ...],
    activation_policy: str = "abort",
) -> AcceptedProcedureV1:
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        authority=ArtifactAuthority(
            propose_roles=("author",),
            approve_roles=("reviewer",),
        ),
        pins=tuple(
            sorted(
                pins,
                key=lambda pin: (
                    pin.role.encode(),
                    pin.target.qualified.encode(),
                    pin.artifact_digest.encode(),
                ),
            )
        ),
        activation_policy=activation_policy,  # type: ignore[arg-type]
    )
    return AcceptedProcedureV1(
        path=procedure_path(definition.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _state_procedure(*, false_branch: bool = False, max_items: int = 100) -> AcceptedProcedureV1:
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    query = _pin("query", "QueryDefinition", "accepted-items")
    predicate = GuardPredicateV1(
        left=PredicateOperandV1(kind="count", alias="rows"),
        operator="gt",
        right=PredicateOperandV1(kind="literal", value=100 if false_branch else 0),
    )
    definition = ProcedureDefinitionV3(
        name="state-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=query,
                parameters={"status": "accepted"},
                as_="rows",
                next="gate",
            ),
            GuardNodeV3(
                node_id="gate",
                predicate=predicate,
                on_true="project",
                on_false="$abort",
                refusal_code="no-items",
                message="No accepted items satisfy the query.",
            ),
            ProjectNodeV3(
                node_id="project",
                fields={"items": "$steps.rows.items", "status": "ok"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(items=max_items),
        hard_caps=_hard_caps(items=max_items),
        terminal_capability=1,
    )
    return _accepted(definition, pins=(contract_in, contract_out, query))


def _provider_procedure(*, effectful: bool, activation_policy: str = "epoch-check"):
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    provider = _pin("provider", "Provider", "calculator")
    environment = _pin("environment", "EnvironmentManifest", "python")
    effect_policy = _pin("effect-policy", "EffectPolicy", "network") if effectful else None
    definition = ProcedureDefinitionV3(
        name="provider-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            ProviderNodeV3(
                node_id="invoke",
                provider=provider,
                contract_in=contract_in,
                contract_out=contract_out,
                environment=environment,
                effect_policy=effect_policy,
                input="$input",
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(providers=1),
        hard_caps=_hard_caps(providers=1),
        terminal_capability=1,
    )
    return _accepted(
        definition,
        pins=tuple(
            item
            for item in (contract_in, contract_out, provider, environment, effect_policy)
            if item is not None
        ),
        activation_policy=activation_policy,
    )


class _StateReader:
    def __init__(self, value=None) -> None:
        self.value = value or {"items": [{"id": "one"}]}
        self.calls = []

    def read_accepted_state(self, *, query, parameters, coordinate):
        self.calls.append((query, parameters, coordinate))
        return self.value


class _Contracts:
    def validate_contract(self, *, contract, payload, direction):
        assert contract.target.kind == "Contract"
        assert direction in {"input", "output"}
        return payload


class _Authority:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = 0

    def current_procedure_digest(self, identity, *, coordinate):
        self.calls += 1
        return self.digest


class _ChangingAuthority(_Authority):
    def current_procedure_digest(self, identity, *, coordinate):
        self.calls += 1
        return self.digest if self.calls == 1 else _digest("superseded")


class _Provider:
    def __init__(self, journal: LocalJournalBackend, stream: JournalStreamIdentityV1) -> None:
        self.journal = journal
        self.stream = stream
        self.calls = 0

    def execute_provider(self, **kwargs):
        self.calls += 1
        records = self.journal.all_records(self.stream, "runs")
        assert records[-1].record.event_kind == "effect_intent"
        return ProviderInvocationResultV1(
            output={"answer": kwargs["payload"]},
            trace={"provider_call": self.calls},
        )


@dataclass
class _Fixture:
    journal: LocalJournalBackend
    bodies: ContentAddressedBodyStore
    stream: JournalStreamIdentityV1
    run_index: ProcedureRunIndex


def _fixture(tmp_path) -> _Fixture:
    journal_root = tmp_path / "journal"
    journal_root.mkdir(mode=0o700)
    cas_root = tmp_path / "cas"
    cas_root.mkdir(mode=0o700)
    journal = LocalJournalBackend(journal_root)
    stream = JournalStreamIdentityV1(
        instance_id="instance-a",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )
    head = journal.read_head(stream, "runs")
    journal.activate_writer(stream, "runs", fencing_token="writer", expected_head=head)
    return _Fixture(
        journal=journal,
        bodies=ContentAddressedBodyStore(cas_root),
        stream=stream,
        run_index=ProcedureRunIndex(tmp_path / "run-index.sqlite"),
    )


def _prepare(accepted: AcceptedProcedureV1, fixture: _Fixture, reader: _StateReader, **kwargs):
    return prepare_direct_procedure_run(
        accepted,
        instance_id="instance-a",
        run_id=kwargs.get("run_id", "run-a"),
        accepted_coordinate=_coordinate(),
        invocation_input=kwargs.get("invocation_input", {"value": 7}),
        actor_context=_actor(),
        state_reader=reader,
        journal_stream=fixture.stream,
        journal_partition_id="runs",
        admitted_at=NOW,
    )


def test_successful_state_run_binds_inputs_and_logs_every_path(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    reader = _StateReader()
    prepared = _prepare(accepted, fixture, reader)
    assert reader.calls[0][2] == _coordinate()
    assert prepared.admission.accepted_state_inputs[0].input_name == "rows"

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)
    assert result.status == "succeeded"
    assert result.output == {"items": [{"id": "one"}], "status": "ok"}

    records = fixture.journal.all_records(fixture.stream, "runs")
    kinds = [item.record.event_kind for item in records]
    assert kinds == [
        "attempt_started",
        "admission_bound",
        "node_fired",
        "branch_evaluated",
        "node_fired",
        "node_fired",
        "attempt_finalized",
    ]
    assert all(fixture.bodies.verify(item.record.payload_digest) for item in records)
    assert result.receipt.record_digests == tuple(item.record_digest for item in records)


def test_false_guard_is_a_typed_refusal_with_complete_finalize(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure(false_branch=True)
    prepared = _prepare(accepted, fixture, _StateReader())
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)
    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == "no-items"
    assert fixture.journal.all_records(fixture.stream, "runs")[-1].record.event_kind == (
        "attempt_finalized"
    )


def test_source_node_refuses_line_binding_until_pc_e2(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    capture = _pin("capture-contract", "CaptureContract", "external")
    provider = _pin("provider", "Provider", "source")
    definition = ProcedureDefinitionV3(
        name="source-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            SourceNodeV3(
                node_id="source",
                capture_contract=capture,
                provider=provider,
                request={"resource": "orders"},
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    accepted = _accepted(definition, pins=(contract_in, contract_out, capture, provider))
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)
    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == "line_binding_required"


def test_effect_intent_is_durable_before_dispatch_and_result_follows(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _provider_procedure(effectful=True)
    provider = _Provider(fixture.journal, fixture.stream)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_executor=provider,
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)
    assert result.status == "succeeded"
    assert provider.calls == 1
    kinds = [item.record.event_kind for item in fixture.journal.all_records(fixture.stream, "runs")]
    assert kinds.index("effect_intent") < kinds.index("effect_result")


def test_epoch_check_refuses_superseded_effect_before_intent(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _provider_procedure(effectful=True, activation_policy="epoch-check")
    provider = _Provider(fixture.journal, fixture.stream)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_ChangingAuthority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_executor=provider,
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)
    assert result.status == "failed"
    assert provider.calls == 0
    assert "effect_intent" not in {
        item.record.event_kind for item in fixture.journal.all_records(fixture.stream, "runs")
    }


def test_item_budget_exhaustion_is_finalized_without_output(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure(max_items=1)
    prepared = _prepare(
        accepted,
        fixture,
        _StateReader({"items": [{"id": "one"}, {"id": "two"}]}),
    )
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)
    assert result.status == "budget_exhausted"
    assert result.output is None


def test_noncurrent_procedure_refuses_before_any_journal_record(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    with pytest.raises(PlaybillExecutionError, match="not current"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(_digest("superseded")),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)
    assert fixture.journal.all_records(fixture.stream, "runs") == ()


def test_completed_retry_replays_result_without_redispatch_or_append(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _provider_procedure(effectful=True)
    provider = _Provider(fixture.journal, fixture.stream)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_executor=provider,
    )
    prepared = _prepare(accepted, fixture, _StateReader())
    first = executor.execute(prepared, accepted)
    record_count = len(fixture.journal.all_records(fixture.stream, "runs"))

    retried = executor.execute(prepared, accepted)

    assert retried == first
    assert provider.calls == 1
    assert len(fixture.journal.all_records(fixture.stream, "runs")) == record_count


def test_deleted_run_index_rebuilds_without_changing_retry_answer(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    )
    first = executor.execute(prepared, accepted)

    index_path = fixture.run_index.path
    fixture.run_index.close()
    index_path.unlink()
    rebuilt = ProcedureRunIndex(index_path)
    replay = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=rebuilt,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)

    assert replay == first
    assert rebuilt.get(prepared.admission.run_id) is not None


class _CrashingClock:
    def __init__(self) -> None:
        self.now_calls = 0

    def now(self):
        self.now_calls += 1
        if self.now_calls == 2:
            raise KeyboardInterrupt("simulated process crash")
        return NOW

    def monotonic_ns(self):
        return 0


def test_incomplete_attempt_is_recovered_as_typed_no_redispatch_refusal(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    crashing = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        clock=_CrashingClock(),
    )
    with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
        crashing.execute(prepared, accepted)
    assert len(fixture.journal.all_records(fixture.stream, "runs")) == 1

    with pytest.raises(PlaybillExecutionError, match="run_recovery_required"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)


class _CrashingProvider(_Provider):
    def execute_provider(self, **kwargs):
        super().execute_provider(**kwargs)
        raise KeyboardInterrupt("simulated effect dispatch crash")


def test_unmatched_effect_intent_never_redispatches_on_retry(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _provider_procedure(effectful=True)
    provider = _CrashingProvider(fixture.journal, fixture.stream)
    prepared = _prepare(accepted, fixture, _StateReader())
    with pytest.raises(KeyboardInterrupt, match="effect dispatch crash"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
            provider_executor=provider,
        ).execute(prepared, accepted)
    assert provider.calls == 1

    with pytest.raises(PlaybillExecutionError, match="unresolved durable effect intent"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
            provider_executor=provider,
        ).execute(prepared, accepted)
    assert provider.calls == 1
