"""Daemon construction keeps Provider execution inside the operational state root."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import cruxible_core.playbill.procedures.execution as execution_module
import cruxible_core.runtime.playbill_manager as playbill_manager_module
import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.acquisition_policies import (
    acquisition_policy_digest,
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateV1,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_PATH,
    render_procedure_runtime_policy,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV2,
    ProcedureOwnedContractV1,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpecV1,
    LineSpecV2,
    ManualTriggerPolicyV1,
    evaluate_line_spec_law,
    line_identity_digest,
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureBudgetV3,
    ProcedureDefinitionV4,
    ProcedureHardCapsV3,
    ProviderNodeV4,
)
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
    provider_path,
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
from cruxible_core.runtime.playbill_manager import PlaybillInstanceManager, get_playbill_manager
from cruxible_core.runtime.provider_runtime import (
    PROVIDER_RUNTIME_CONFIG_PATH,
    ProviderRuntimeOperator,
)
from cruxible_core.server.config import get_server_state_root
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
from tests.test_server.test_playbill_line_run_refusals import (
    _accept_members,
    _acquisition_policy,
)


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
    # The operator's control namespace is asserted in place, so the sample socket
    # path has to stay inside the 103-byte AF_UNIX budget or the store takes the
    # ruled per-user fallback. The repo's gitignored scratch prefix is the shortest
    # root available on any checkout; the budget itself is unchanged.
    state_root = Path(tempfile.mkdtemp(prefix=".b2-", dir=Path(__file__).resolve().parents[2]))
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
    # Retained from before the Line route existed: the operator's own invoke
    # path still has to reach the real subprocess, echo its output, verify the
    # binding it spawned against, and leave no lease behind.
    occurrence = SimpleNamespace(
        local_execution=admitted_binding,
        provider_artifact_digest=accepted_provider.artifact_digest,
        interface_artifact_digest=interface.artifact_digest,
        implementation_digest=implementation.implementation_digest,
        secret_plan=ProviderSecretResolutionPlanV1(),
    )
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id="RUN-daemon-operator",
        interface_id=interface.registration.interface_id,
        interface_digest=interface.registration.interface_digest,
        implementation_digest=implementation.implementation_digest,
        entrypoint=admitted_binding.entrypoint,
        input={"value": "served"},
        input_bucket="size=small",
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=2, output_bytes=65_536),
    )

    outcome = invoker.invoke_provider(
        occurrence=occurrence,  # type: ignore[arg-type]
        context=context,
        invocation_id=_digest("daemon-invocation"),
        bound=invoker.bind_provider(occurrence=occurrence),  # type: ignore[arg-type]
    )
    assert outcome.envelope.output == {"echo": "served"}
    assert outcome.verified_binding == admitted_binding
    assert tuple(operator.process_leases.root.glob("*.json")) == ()

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
    # The served route's own end of this milestone is proved without any double
    # in tests/test_server/test_playbill_line_run_refusals.py, which drives the
    # live route into the real Line service and a real admission. The two ends
    # meet in test_the_live_line_route_runs_a_real_daemon_owned_provider_subprocess
    # below, which drives that route into this operator's own child process.
    assert result.receipt is not None, json.dumps(payloads[-1], indent=2)
    assert result.output == {"echo": "line-served"}
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


def _provider_line_procedure(
    accepted_provider: AcceptedProviderV1,
    interface,  # type: ignore[no-untyped-def]
) -> AcceptedProcedureV1:
    """One accepted Procedure whose single node invokes the demo Provider."""

    run_input = ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name="served-provider-run-input"),
        schema=ContractSchema(fields={"status": PropertySchema(type="string")}),
    )
    provider_input = ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name="served-provider-input"),
        schema=ContractSchema(
            fields={
                "size": PropertySchema(type="int"),
                "value": PropertySchema(type="string"),
            }
        ),
    )
    provider_output = ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name="served-provider-output"),
        schema=ContractSchema(fields={"echo": PropertySchema(type="string")}),
    )
    run_input_pin = ArtifactPin(
        role="contract-in",
        target=run_input.identity,
        artifact_digest=procedure_owned_contract_digest(run_input).tagged,
    )
    provider_input_pin = ArtifactPin(
        role="contract-in",
        target=provider_input.identity,
        artifact_digest=procedure_owned_contract_digest(provider_input).tagged,
    )
    provider_output_pin = ArtifactPin(
        role="contract-out",
        target=provider_output.identity,
        artifact_digest=procedure_owned_contract_digest(provider_output).tagged,
    )
    provider_pin = ArtifactPin(
        role="provider",
        target=accepted_provider.provider.identity,
        artifact_digest=accepted_provider.artifact_digest,
    )
    interface_pin = ArtifactPin(
        role="provider-interface",
        target=interface.registration.identity,
        artifact_digest=interface.artifact_digest,
    )
    implementation = accepted_provider.provider.implementations[0]
    definition = ProcedureDefinitionV4(
        name="served-provider-triage",
        contract_in=run_input_pin,
        contract_out=provider_output_pin,
        nodes=(
            ProviderNodeV4(
                node_id="ask",
                provider=provider_pin,
                interface=interface_pin,
                interface_digest=interface.registration.interface_digest,
                implementation_digest=implementation.implementation_digest,
                contract_in=provider_input_pin,
                contract_out=provider_output_pin,
                input={"size": 3, "value": "line-served"},
                as_="result",
            ),
        ),
        returns="result",
        pin_slots=(),
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=4_000_000),
            max_provider_calls=2,
            max_capture_bytes=1024,
            max_items=10,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=8_000_000),
            max_provider_calls=4,
            max_capture_bytes=2048,
            max_items=20,
            max_repeat_attempts=1,
        ),
        terminal_capability=2,
    )
    procedure = ProcedureArtifactV2(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v4(definition).tagged,
        pins=tuple(
            sorted(
                (
                    run_input_pin,
                    provider_input_pin,
                    provider_output_pin,
                    provider_pin,
                    interface_pin,
                ),
                key=lambda item: (item.role, item.target.qualified, item.artifact_digest),
            )
        ),
        owned_contracts=tuple(
            sorted(
                (run_input, provider_input, provider_output),
                key=lambda contract: canonical_bytes(
                    contract.model_dump(mode="json", by_alias=True)
                ),
            )
        ),
        activation_policy="drain",
    )
    return AcceptedProcedureV1(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def test_the_live_line_route_runs_a_real_daemon_owned_provider_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """U1's exit condition end to end, with no double anywhere on the path.

    One HTTP request reaches the real Line service, which resolves the accepted
    Line, evaluates its law and mandate, derives the occurrence, binds the
    Provider through the DAEMON's own runtime operator -- constructed from the
    operator-written `daemon/provider-runtime.json` in the server state root --
    and spawns the sealed interpreter as a real child process. Only the bucket
    classifier registry is installed (the RAT-9 allowance); nothing on the route,
    the service, the admission, or the invoker is patched.
    """

    client, instance_id, reviewer_key_path = playbill_http
    state_root = get_server_state_root()

    materialization = state_root / "materializations" / "served-line"
    materialization.mkdir(parents=True)
    distribution = materialization / "provider.whl"
    distribution.write_bytes(b"provider-wheel")
    lock = materialization / "uv.lock"
    lock.write_bytes(b"exact-lock")
    interpreter = _fake_interpreter(materialization / "python")
    materialization_digest = _digest("served-line-materialization")
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
        path=provider_path(provider.identity.name),
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
    interface = accepted_interface()
    deployment_digest = _digest("served-line-deployment")
    config_path = state_root / PROVIDER_RUNTIME_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
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
    # The daemon read its operational config at startup, before this deployment
    # existed; dropping the cache is that daemon restarting, not a double.
    get_playbill_manager().clear()

    accepted = _provider_line_procedure(accepted_provider, interface)
    policy = _acquisition_policy("served-provider-inputs")
    procedure_pin = ArtifactPin(
        role="procedure",
        target=accepted.procedure.identity,
        artifact_digest=accepted.artifact_digest,
    )
    policy_pin = ArtifactPin(
        role="acquisition-policy",
        target=policy.identity,
        artifact_digest=acquisition_policy_digest(policy).tagged,
    )
    definition = accepted.procedure.definition
    line = LineSpecV2(
        identity=ArtifactIdentity(kind="Line", name="served-provider-hourly"),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={"status": "open"},
        slot_bindings=(),
        trigger_policy=ManualTriggerPolicyV1(),
        acquisition_policy=policy_pin,
        requested_terminal_rung=1,
        budgets={
            "max_capture_bytes": definition.budget.max_capture_bytes,
            "max_items": definition.budget.max_items,
            "max_provider_calls": definition.budget.max_provider_calls,
            "max_wall_clock_microseconds": definition.budget.wall_clock.microseconds,
        },
        epsilon={"$decimal": "0"},
        provider_implementation_closures=(),
        pins=tuple(
            sorted(
                (procedure_pin, policy_pin),
                key=lambda item: (item.role, item.target.qualified, item.artifact_digest),
            )
        ),
    )
    mandate = ProcedureMandateV1(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="served-provider-mandate"),
        procedure=procedure_pin,
        rung=2,
        authority_ceiling=definition.hard_caps,
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    instance = get_playbill_manager().get(instance_id)
    _accept_members(
        instance,
        reviewer_key_path,
        {
            accepted.path: render_procedure(accepted.procedure),
            accepted_provider.path: render_provider(accepted_provider.provider),
            interface.path: render_provider_interface(interface.registration),
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        timestamp="2026-09-03T09:00:00.000000Z",
    )

    classifier_registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(classifier_registry)
    monkeypatch.setattr(
        execution_module,
        "PROVIDER_BUCKET_CLASSIFIER_REGISTRY",
        classifier_registry,
    )

    identity_digest = line_identity_digest(line.identity)
    response = client.post(
        f"/api/v1/{instance_id}/playbill/lines/{identity_digest}/runs",
        json={
            "tag": "playbill-line-run-request-v1",
            "line_identity_digest": identity_digest,
            "occurrence_id": None,
            "evaluation_time": None,
        },
    )

    assert response.status_code == 200, response.text
    state = response.json()
    assert state["status"] == "succeeded", state["terminal"]
    # Only the sealed child process can produce this value: the fake interpreter
    # echoes the node input it was handed on stdin.
    assert state["result"] == {"echo": "line-served"}
    receipt = state["receipt"]
    assert len(receipt["invocation_receipt_digests"]) == 1
    binding = receipt["resolved_provider_bindings"][0]
    assert binding["node_id"] == "ask"
    assert binding["provider_artifact_digest"] == accepted_provider.artifact_digest
    implementation = accepted_provider.provider.implementations[0]
    assert binding["implementation_digest"] == implementation.implementation_digest

    # The operator that bound and spawned it is the daemon's own, built from the
    # operator-written config, and it left no lease behind.
    operator = get_playbill_manager().cached_provider_runtime_operator()
    assert set(operator.deployments) == {deployment_digest}
    assert operator.process_leases is not None
    assert tuple(operator.process_leases.root.glob("*.json")) == ()
