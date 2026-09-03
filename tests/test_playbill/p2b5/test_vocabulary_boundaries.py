"""The retained Procedure refusal vocabulary has production public emitters."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.test_playbill._p2b1_support import install_demo_classifier
from tests.test_playbill.test_procedure_execution import (
    NOW,
    _Authority,
    _Contracts,
    _coordinate,
    _digest,
    _fixture,
    _prepare,
    _state_procedure,
    _StateReader,
)
from tests.test_playbill.test_provider_invocation_journal import (
    _accepted_one_provider,
    _Invoker,
    _prepared_v5,
)

import cruxible_core.playbill.procedures.execution as execution_module
from cruxible_client.contracts.errors import PlaybillJournalError
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import parse_journal_payload
from cruxible_core.playbill.procedures.execution import (
    ProcedureBoundaryRefused,
    ProcedureExecutor,
    prepare_direct_procedure_run,
)
from cruxible_core.playbill.provider_classifiers import ProviderBucketClassifierRegistry


class _RefusingStateReader:
    def read_accepted_state(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("query backend unavailable")


def test_state_tap_refusal_is_emitted_by_the_public_preparer(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()

    with pytest.raises(ProcedureBoundaryRefused) as caught:
        prepare_direct_procedure_run(
            accepted,
            instance_id="instance-a",
            run_id="run-state-refusal",
            accepted_coordinate=_coordinate(),
            invocation_input={},
            actor_context=SimpleNamespace(),  # type: ignore[arg-type]
            state_reader=_RefusingStateReader(),  # type: ignore[arg-type]
            bodies=fixture.bodies,
            journal_stream=fixture.stream,
            journal_partition_id="runs",
            admitted_at=NOW,
        )

    assert caught.value.code == "state_tap_refused"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("pin", "pin_binding_mismatch"),
        ("input", "input_material_mismatch"),
    ),
)
def test_admission_binding_refusals_leave_the_public_executor_typed(
    tmp_path,
    mutation: str,
    expected: str,
) -> None:  # type: ignore[no-untyped-def]
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    admission = prepared.admission
    if mutation == "pin":
        admission = admission.model_copy(
            update={"procedure_artifact_digest": _digest("substituted-procedure")}
        )
    else:
        item = admission.accepted_state_inputs[0]
        admission = admission.model_copy(
            update={
                "accepted_state_inputs": (
                    item.model_copy(
                        update={"query_definition_digest": _digest("substituted-query")}
                    ),
                )
            }
        )

    with pytest.raises(ProcedureBoundaryRefused) as caught:
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
        ).execute(prepared.model_copy(update={"admission": admission}), accepted)

    assert caught.value.code == expected


def test_not_current_is_a_public_refusal_instead_of_generic_failure(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())

    with pytest.raises(ProcedureBoundaryRefused) as caught:
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(_digest("retired")),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)

    assert caught.value.code == "not_current"


@pytest.mark.parametrize(
    ("message", "expected"),
    (
        ("append made no progress", "journal_append_failed"),
        ("append expected head is stale or forked", "journal_conflict"),
        ("journal chain is corrupt", "journal_integrity_error"),
    ),
)
def test_public_executor_classifies_journal_boundary_failures(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected: str,
) -> None:  # type: ignore[no-untyped-def]
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())

    def refuse_append(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise PlaybillJournalError(message)

    monkeypatch.setattr(fixture.journal, "append", refuse_append)
    with pytest.raises(ProcedureBoundaryRefused) as caught:
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)

    assert caught.value.code == expected


def test_compiler_invariant_break_projects_from_the_public_executor(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    monkeypatch.setattr(
        execution_module,
        "analyze_procedure_v3",
        lambda _definition: SimpleNamespace(
            edges={accepted.procedure.definition.nodes[0].node_id: {"next": "missing"}}
        ),
    )

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)

    assert result.status == "failed"
    records = fixture.journal.all_records(fixture.stream, "runs")
    final = parse_journal_payload(
        fixture.bodies.read(
            records[-1].record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert isinstance(final, dict)
    assert final["failure_code"] == "compiler_invariant_broken"


def test_provider_replay_requires_every_durable_completion_via_public_executor(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    first = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_Invoker(),
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    assert first.status == "succeeded"

    class _CompletionOmittingJournal:
        def all_records(self, stream, partition_id):  # type: ignore[no-untyped-def]
            return tuple(
                item
                for item in fixture.journal.all_records(stream, partition_id)
                if item.record.event_kind != "provider_invocation_completed"
            )

    class _CompletedIndex:
        def rebuild(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def get(self, _run_id):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                admission_binding_digest=prepared.admission.admission_binding_digest,
                status="succeeded",
            )

    with pytest.raises(ProcedureBoundaryRefused) as caught:
        ProcedureExecutor(
            journal=_CompletionOmittingJournal(),  # type: ignore[arg-type]
            bodies=fixture.bodies,
            run_index=_CompletedIndex(),  # type: ignore[arg-type]
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)

    assert caught.value.code == "provider_replay_receipt_required"
