"""Daemon construction keeps Provider execution inside the operational state root."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import cruxible_core.playbill.procedures.execution as execution_module
import cruxible_core.runtime.playbill_manager as playbill_manager_module
import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_PATH,
    render_procedure_runtime_policy,
)
from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpecV1,
    LineSpecV2,
    evaluate_line_spec_law,
    line_identity_digest,
    line_spec_digest,
    line_spec_path,
)
from cruxible_client.contracts.procedures.models import ProviderNodeV4
from cruxible_client.contracts.procedures.results import procedure_acquisition_plan_digest
from cruxible_client.contracts.provider_execution import ProviderSecretResolutionPlanV1
from cruxible_client.contracts.provider_interfaces import (
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderV2,
    provider_digest,
    provider_expected_implementation_records,
    render_provider,
)
from cruxible_core.playbill.bootstrap import seeded_procedure_runtime_policy
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust.records import parse_journal_payload
from cruxible_core.playbill.procedures.execution import (
    PreparedProcedureRunV5,
    ProcedureRunAdmissionV5,
    procedure_admission_digest,
    procedure_line_run_id,
    procedure_semantic_replay_key_digest,
)
from cruxible_core.playbill.provider_classifiers import ProviderBucketClassifierRegistry
from cruxible_core.playbill.provider_local_runtime import LocalProviderDeploymentV1
from cruxible_core.playbill.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessRecoveryFailureV1,
    ProviderProcessRecoveryResultV1,
)
from cruxible_core.playbill.provider_runtime_contract import (
    ProviderRuntimeBudgetsV1,
    ProviderRuntimeRunContextV1,
)
from cruxible_core.playbill.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_INTERFACE_ID,
    workspace_file_interface_registration,
)
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.playbill_manager import PlaybillInstanceManager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
from cruxible_core.service.playbill_procedure_runs import ProcedureRunRecoveryRequired
from cruxible_core.service.playbill_procedures import service_execute_direct_procedure
from tests.test_playbill._p2b1_support import (
    accepted_interface,
    install_demo_classifier,
    provider_v2,
)
from tests.test_playbill._provider_seal_support import write_test_provider_seal_v2
from tests.test_playbill.test_procedure_execution import (
    _accepted_line_for_admission,
)
from tests.test_playbill.test_provider_invocation_journal import (
    _accepted_one_provider,
    _Authority,
    _Contracts,
    _prepared_v5,
)
from tests.test_playbill.test_provider_local_driver import _fake_interpreter


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _procedure_bound_to_provider(accepted_provider: AcceptedProviderV1):  # type: ignore[no-untyped-def]
    accepted = _accepted_one_provider()
    definition = accepted.procedure.definition
    node = definition.nodes[0]
    assert isinstance(node, ProviderNodeV4)
    provider_pin = node.provider
    assert not isinstance(provider_pin, str)
    provider_pin = provider_pin.model_copy(
        update={"artifact_digest": accepted_provider.artifact_digest}
    )
    implementation_digest = accepted_provider.provider.implementations[0].implementation_digest
    node = node.model_copy(
        update={
            "provider": provider_pin,
            "implementation_digest": implementation_digest,
            "input": {"size": 3, "value": "line-served"},
        }
    )
    definition = definition.model_copy(update={"nodes": (node,)})
    pins = tuple(
        sorted(
            (
                provider_pin if item.role == "provider" and item.target.kind == "Provider" else item
                for item in accepted.procedure.pins
            ),
            key=lambda item: (
                item.role.encode(),
                item.target.qualified.encode(),
                item.artifact_digest.encode(),
            ),
        )
    )
    procedure = accepted.procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
            "pins": pins,
        }
    )
    return accepted.model_copy(
        update={
            "procedure": procedure,
            "artifact_digest": procedure_artifact_digest(procedure).tagged,
        }
    )


def _complete_line_for_admission(
    admission: ProcedureRunAdmissionV5,
    accepted_procedure,  # type: ignore[no-untyped-def]
    *,
    accepted_provider: AcceptedProviderV1,
    interface,  # type: ignore[no-untyped-def]
) -> AcceptedLineSpecV1:
    historical = _accepted_line_for_admission(admission, accepted_procedure).line
    definition = accepted_procedure.procedure.definition
    line = LineSpecV2.model_validate(
        {
            **historical.model_dump(mode="json"),
            "artifact_format": "playbill-line-v2",
            "budgets": {
                "max_capture_bytes": definition.budget.max_capture_bytes,
                "max_items": definition.budget.max_items,
                "max_provider_calls": definition.budget.max_provider_calls,
                "max_wall_clock_microseconds": definition.budget.wall_clock.microseconds,
            },
            "provider_implementation_closures": [],
        }
    )
    path = line_spec_path(line.identity.name)
    law = evaluate_line_spec_law(
        line,
        path=path,
        procedure=accepted_procedure,
        interface_digests={
            accepted_provider.artifact_digest: interface.registration.interface_digest
        },
        predecessor=None,
        providers={accepted_provider.artifact_digest: accepted_provider},
        provider_interfaces={interface.artifact_digest: interface},
    )
    assert law.verdict == "accepted", law.diagnostics
    return AcceptedLineSpecV1(
        path=path,
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )


def _rebind_prepared_line(
    prepared: PreparedProcedureRunV5,
    accepted_line: AcceptedLineSpecV1,
) -> PreparedProcedureRunV5:
    plan = prepared.acquisition_plan.model_copy(
        update={"line_spec_digest": accepted_line.artifact_digest}
    )
    plan_digest = procedure_acquisition_plan_digest(plan)
    fields = {
        name: getattr(prepared.admission, name)
        for name in ProcedureRunAdmissionV5.model_fields
        if name != "tag"
    }
    fields.update(
        {
            "line_spec_digest": accepted_line.artifact_digest,
            "acquisition_plan_digest": plan_digest,
            "semantic_replay_key_digest": _digest("line-replay-placeholder"),
            "admission_binding_digest": _digest("line-admission-placeholder"),
            "run_id": "RUN-" + "0" * 64,
        }
    )
    provisional = ProcedureRunAdmissionV5.model_construct(**fields)
    provisional = provisional.model_copy(
        update={"semantic_replay_key_digest": procedure_semantic_replay_key_digest(provisional)}
    )
    admission_digest = procedure_admission_digest(provisional)
    admission = ProcedureRunAdmissionV5.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "admission_binding_digest": admission_digest,
            "run_id": procedure_line_run_id(
                occurrence_id=provisional.occurrence_id or "",
                attempt=provisional.attempt,
                admission_binding_digest=admission_digest,
                occurrence_evaluation_time=provisional.occurrence_evaluation_time,
            ),
        }
    )
    return PreparedProcedureRunV5.model_validate(
        {
            **prepared.model_dump(mode="python"),
            "admission": admission,
            "acquisition_plan": plan,
            "acquisition_plan_digest": plan_digest,
        }
    )


def test_daemon_operator_rebinds_and_runs_a_real_local_subprocess(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    playbill_http,
) -> None:
    state_root = Path(tempfile.mkdtemp(prefix=".provider-state-", dir=Path.cwd()))
    request.addfinalizer(lambda: shutil.rmtree(state_root, ignore_errors=True))
    materialization = state_root / "materializations" / "demo"
    materialization.mkdir(parents=True)
    distribution = materialization / "provider.whl"
    distribution.write_bytes(b"provider-wheel")
    lock = materialization / "uv.lock"
    lock.write_bytes(b"exact-lock")
    interpreter = _fake_interpreter(materialization / "python")
    materialization_digest = _digest("operator-materialization")
    seal = materialization / "environment.json"
    write_test_provider_seal_v2(
        environment_root=materialization,
        seal_path=seal,
        interpreter_path=interpreter,
        lock_digest=_sha256_file(lock),
        materialization_digest=materialization_digest,
    )
    base = provider_v2()
    assert base.runtime_artifact.local_env is not None
    payload = base.runtime_artifact.model_copy(
        update={
            "distribution": base.runtime_artifact.distribution.model_copy(
                update={"sha256": _sha256_file(distribution)}
            ),
            "local_env": base.runtime_artifact.local_env.model_copy(
                update={
                    "lock_sha256": _sha256_file(lock),
                    "materialization_digests": {"linux-cp311+engine": materialization_digest},
                }
            ),
        }
    )
    provider = ProviderV2.model_validate(
        base.model_copy(
            update={
                "runtime_artifact": payload,
                "implementations": provider_expected_implementation_records(payload),
            }
        ).model_dump(mode="python")
    )
    accepted_provider = AcceptedProviderV1(
        path="providers/demo-provider.json",
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
    interface = accepted_interface()
    deployment_digest = _digest("operator-deployment")
    config_path = state_root / "daemon" / "provider-runtime.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(
        canonical_bytes(
            {
                "tag": "cruxible-provider-runtime-operational-config-v1",
                "lease_acquisition_timeout_seconds": 5,
                "lease_recovery_timeout_seconds": 5,
                "deployments": [
                    {
                        "tag": "cruxible-provider-deployment-config-v1",
                        "deployment_digest": deployment_digest,
                        "distribution_path": str(distribution.relative_to(state_root)),
                        "lock_path": str(lock.relative_to(state_root)),
                        "environment_path": str(materialization.relative_to(state_root)),
                        "environment_manifest_path": str(seal.relative_to(state_root)),
                        "environment_pin_key": "linux-cp311+engine",
                        "interpreter_path": str(interpreter.relative_to(state_root)),
                        "provider_runtime_version": "1.0.0",
                    }
                ],
            }
        )
    )

    class _Instance:
        def tree_at(self, oid: str) -> dict[str, bytes]:
            assert oid == "a" * 40
            return {
                accepted_provider.path: render_provider(accepted_provider.provider),
                interface.path: render_provider_interface(interface.registration),
            }

    implementation = provider.implementations[0]
    deployment = LocalProviderDeploymentV1(
        deployment_digest=deployment_digest,
        distribution_path=distribution,
        lock_path=lock,
        environment_path=materialization,
        environment_manifest_path=seal,
        environment_pin_key="linux-cp311+engine",
        interpreter_path=interpreter,
        provider_runtime_version="1.0.0",
    )
    operator = ProviderRuntimeOperator(state_root)
    assert operator.deployments == {deployment_digest: deployment}
    assert operator.process_leases is not None
    assert operator.process_leases.control_root == state_root / "c"
    recovery = operator.recover_all()
    assert recovery.recovered == ()
    assert recovery.removed == ()
    assert recovery.could_not_clean == ()
    invoker = operator.invoker_for(_Instance(), accepted_oid="a" * 40)  # type: ignore[arg-type]
    admitted_binding = operator.driver.bind(
        accepted_provider,
        interface,
        implementation.implementation_digest,
        deployment,
    ).binding
    accepted_procedure = _procedure_bound_to_provider(accepted_provider)
    (state_root / "line-service").mkdir()
    prepared, line_fixture = _prepared_v5(
        accepted_procedure,
        state_root / "line-service",
        provider=accepted_provider,
        interface=interface,
        local_binding=admitted_binding,
    )
    accepted_line = _complete_line_for_admission(
        prepared.admission,
        accepted_procedure,
        accepted_provider=accepted_provider,
        interface=interface,
    )
    prepared = _rebind_prepared_line(prepared, accepted_line)
    policy_tree = {
        PROCEDURE_RUNTIME_POLICY_PATH: render_procedure_runtime_policy(
            seeded_procedure_runtime_policy()
        )
    }
    bound = procedure_run_service.service_prepare_playbill_line_admission(
        SimpleNamespace(tree_at=lambda _oid: policy_tree),  # type: ignore[arg-type]
        admission=prepared.admission,
        accepted_line=accepted_line,
    )
    assert isinstance(bound, ProcedureRunAdmissionV5)
    prepared = PreparedProcedureRunV5.model_validate(
        {**prepared.model_dump(mode="python"), "admission": bound}
    )
    classifier_registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(classifier_registry)
    monkeypatch.setattr(
        execution_module,
        "PROVIDER_BUCKET_CLASSIFIER_REGISTRY",
        classifier_registry,
    )

    def execute_through_line_route(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        result = service_execute_direct_procedure(
            prepared,
            accepted_procedure,
            journal=line_fixture.journal,
            bodies=line_fixture.bodies,
            run_index_path=state_root / "line-run-index.sqlite",
            fencing_token="writer",
            activation_authority=_Authority(accepted_procedure.artifact_digest),
            contract_validator=_Contracts(),
            provider_runtime_invoker=invoker,
        )
        monkeypatch.setattr(
            procedure_run_service,
            "_records_for_run",
            lambda _instance, _run_id: line_fixture.journal.all_records(
                prepared.admission.journal_stream,
                prepared.admission.journal_partition_id,
            ),
        )
        return procedure_run_service._state_from_records(  # noqa: SLF001
            SimpleNamespace(body_store=lambda: line_fixture.bodies),  # type: ignore[arg-type]
            run_id=prepared.admission.run_id,
            receipt=result.receipt,
        )

    monkeypatch.setattr(playbill_api, "service_run_playbill_line", execute_through_line_route)
    client, instance_id, _reviewer_key = playbill_http
    identity_digest = line_identity_digest(accepted_line.line.identity)
    response = client.post(
        f"/api/v1/{instance_id}/playbill/lines/{identity_digest}/runs",
        json={
            "tag": "playbill-line-run-request-v1",
            "line_identity_digest": identity_digest,
            "occurrence_id": None,
            "evaluation_time": "2026-08-21T12:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    records = line_fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    payloads = [
        parse_journal_payload(
            line_fixture.bodies.read(
                item.record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
        for item in records
    ]
    assert result["status"] == "succeeded", json.dumps(payloads[-1], indent=2)
    assert result["receipt"] is not None
    assert result["result"] == {"echo": "line-served"}
    assert [item.record.event_kind for item in records].count("provider_invocation_completed") == 1
    assert tuple(operator.process_leases.root.glob("*.json")) == ()


def test_malformed_runtime_config_degrades_only_the_provider_lane(tmp_path: Path) -> None:
    state_root = tmp_path / "malformed-runtime-state"
    config_path = state_root / "daemon" / "provider-runtime.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("not-json", encoding="utf-8")

    operator = ProviderRuntimeOperator(state_root)

    assert operator.unavailable_reason is not None
    assert "provider_process_lease_invalid" in operator.unavailable_reason
    invoker = operator.invoker_for(SimpleNamespace(), accepted_oid="a" * 40)  # type: ignore[arg-type]
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        invoker.bind_provider(occurrence=object())  # type: ignore[arg-type]
    assert caught.value.code == "provider_unavailable"
    assert caught.value.details == {
        "reason": {
            "code": operator.unavailable_code,
            "detail": operator.unavailable_reason,
        }
    }


def test_overlong_state_root_degrades_provider_at_operator_construction(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retracted P2-B2 oracle: a verified fallback keeps the Provider lane live."""

    runtime_root = Path(tempfile.mkdtemp(prefix=".u8-operator-", dir=Path.cwd()))
    request.addfinalizer(lambda: shutil.rmtree(runtime_root, ignore_errors=True))
    monkeypatch.setenv("TMPDIR", str(runtime_root))
    state_root = tmp_path / ("overlong-" + "x" * 110)
    real_fsencode = os.fsencode
    monkeypatch.setattr(
        os,
        "fsencode",
        lambda value: (
            b"f" * 80 if str(value).startswith(str(runtime_root)) else real_fsencode(value)
        ),
    )
    operator = ProviderRuntimeOperator(state_root)
    assert operator.process_leases is not None
    assert operator.process_leases.control_root.is_relative_to(runtime_root)
    assert operator.unavailable_reason is None


def test_classifier_installation_failure_degrades_only_provider_lane(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    state_root = Path(tempfile.mkdtemp(dir=repository_root, prefix=".b2-classifier-"))
    request.addfinalizer(lambda: shutil.rmtree(state_root, ignore_errors=True))
    operator = ProviderRuntimeOperator(state_root)
    registration = workspace_file_interface_registration()
    tree = {
        provider_interface_path(WORKSPACE_FILE_INTERFACE_ID): render_provider_interface(
            registration
        )
    }
    monkeypatch.setattr(
        "cruxible_core.runtime.provider_runtime.install_compiler_owned_provider_classifier",
        lambda _accepted: (_ for _ in ()).throw(RuntimeError("broken classifier")),
    )

    invoker = operator.invoker_for(
        SimpleNamespace(tree_at=lambda _oid: tree),  # type: ignore[arg-type]
        accepted_oid="a" * 40,
    )

    state, code, detail = operator.lane_status()
    assert (state, code) == ("unavailable", "provider_runtime_recovery_failed")
    assert detail is not None and "broken classifier" in detail
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        invoker.bind_provider(occurrence=object())
    assert caught.value.details == {"reason": {"code": code, "detail": detail}}


def test_unmatched_recovered_start_degrades_provider_and_continues_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_id = _digest("orphan-without-admitted-occurrence")
    result = ProviderProcessRecoveryResultV1(
        recovered=(invocation_id,),
        removed=(),
        could_not_clean=(),
    )
    unavailable: list[tuple[str, str]] = []
    operator = SimpleNamespace(
        recover_all=lambda: result,
        mark_unavailable=lambda code, message, **_kwargs: unavailable.append((code, message)),
    )
    records = (
        SimpleNamespace(instance_id="inst_one", backend="governed_daemon"),
        SimpleNamespace(instance_id="inst_two", backend="governed_daemon"),
    )
    manager = PlaybillInstanceManager()
    operator.recover_all_with_bound_fold = lambda: (  # type: ignore[attr-defined]
        manager._fold_provider_recovery(operator, result),
        result,
    )[1]
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: operator)
    monkeypatch.setattr(manager, "get", lambda instance_id: instance_id)
    monkeypatch.setattr(
        playbill_manager_module,
        "get_registry",
        lambda: SimpleNamespace(list_instances=lambda: records),
    )
    visited: list[str] = []

    def recover(instance: str, **_kwargs: object) -> tuple[str, ...]:
        visited.append(instance)
        if instance == "inst_one":
            raise ProcedureRunRecoveryRequired(
                "procedure_run_recovery_required: no exact admitted occurrence"
            )
        return ()

    monkeypatch.setattr(procedure_run_service, "service_recover_provider_invocations", recover)

    assert manager.recover_provider_runtime() == result
    assert visited == ["inst_one", "inst_two"]
    assert unavailable == [
        (
            "provider_runtime_recovery_failed",
            "procedure_run_recovery_required: no exact admitted occurrence",
        )
    ]


def test_could_not_clean_is_forwarded_as_recovery_required_without_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_id = _digest("could-not-clean")
    result = ProviderProcessRecoveryResultV1(
        recovered=(),
        removed=(),
        could_not_clean=(
            ProviderProcessRecoveryFailureV1(
                record_name="fence.json",
                invocation_id=invocation_id,
                code="provider_process_group_survived_recovery",
                message="group remains live",
            ),
        ),
    )
    operator = SimpleNamespace(
        recover_all=lambda: result,
        mark_unavailable=lambda *_args, **_kwargs: None,
    )
    manager = PlaybillInstanceManager()
    operator.recover_all_with_bound_fold = lambda: (  # type: ignore[attr-defined]
        manager._fold_provider_recovery(operator, result),
        result,
    )[1]
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: operator)
    monkeypatch.setattr(manager, "get", lambda instance_id: instance_id)
    monkeypatch.setattr(
        playbill_manager_module,
        "get_registry",
        lambda: SimpleNamespace(
            list_instances=lambda: (
                SimpleNamespace(instance_id="inst_one", backend="governed_daemon"),
            )
        ),
    )
    observed: list[dict[str, object]] = []

    def recover(_instance: str, **kwargs: object) -> tuple[str, ...]:
        observed.append(kwargs)
        return ()

    monkeypatch.setattr(procedure_run_service, "service_recover_provider_invocations", recover)

    assert manager.recover_provider_runtime() == result
    assert observed[0]["invocation_ids"] == ()
    assert observed[0]["recovery_failure_codes"] == {
        invocation_id: "provider_process_group_survived_recovery"
    }


def test_lazy_rearm_is_serialized_and_never_runs_during_an_invocation(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    state_root = Path(tempfile.mkdtemp(dir=repository_root, prefix=".b2-server-"))
    request.addfinalizer(lambda: shutil.rmtree(state_root, ignore_errors=True))
    operator = ProviderRuntimeOperator(state_root)
    assert operator.process_leases is not None
    calls: list[str] = []
    clean = ProviderProcessRecoveryResultV1(recovered=(), removed=(), could_not_clean=())
    monkeypatch.setattr(
        operator.process_leases,
        "recover_all",
        lambda **_kwargs: calls.append("recover") or clean,
    )
    operator.mark_unavailable(
        "provider_process_group_survived_recovery",
        "repairable survivor",
        retryable=True,
    )
    operator._in_flight = 1  # noqa: SLF001 - directly pins the K-9 exclusion
    unavailable = operator.invoker_for(
        SimpleNamespace(tree_at=lambda _oid: {}),  # type: ignore[arg-type]
        accepted_oid="a" * 40,
    )
    assert calls == []
    with pytest.raises(ProviderLocalRuntimeRefused):
        unavailable.bind_provider(occurrence=object())

    operator._in_flight = 0  # noqa: SLF001
    operator.invoker_for(
        SimpleNamespace(tree_at=lambda _oid: {}),  # type: ignore[arg-type]
        accepted_oid="a" * 40,
    )
    assert calls == ["recover"]
    assert operator.lane_status() == ("available", None, None)
