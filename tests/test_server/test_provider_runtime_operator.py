"""Daemon construction keeps Provider execution inside the operational state root."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import cruxible_core.runtime.playbill_manager as playbill_manager_module
import cruxible_core.service.procedures.procedure_runs as procedure_run_service
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandate,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedure_runtime_policy import PROCEDURE_RUNTIME_POLICY_IDENTITY
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    ProcedureOwnedContract,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpec,
    LineSpec,
    evaluate_line_spec_law,
    line_identity_digest,
    line_slot_signatures,
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProcedureSlot,
)
from cruxible_client.contracts.procedures.results import procedure_acquisition_plan_digest
from cruxible_client.contracts.provider_execution import ProviderSecretResolutionPlan
from cruxible_client.contracts.provider_operations import (
    LineBinding,
    ProviderOperation,
    binding_pin,
    operation_signature_digest,
)
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    NamedProviderBuild,
    ProviderDistributionRef,
    ProviderImplementationManifest,
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    ProviderRuntimeManifest,
    provider_digest,
    provider_expected_implementation_records,
    provider_path,
    render_provider,
)
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
)
from cruxible_core.exhaust.records import parse_journal_payload
from cruxible_core.ledger.bootstrap import seeded_procedure_runtime_policy
from cruxible_core.procedures.execution import (
    PreparedProcedureRunV9,
    ProcedureRunAdmissionV9,
    procedure_admission_digest,
    procedure_line_run_id,
    procedure_semantic_replay_key_digest,
)
from cruxible_core.procedures.reference_binding import (
    authored_procedure,
    bind_procedure_references,
)
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from cruxible_core.providers.provider_local_runtime import (
    LocalProviderDeploymentV1,
    ProviderSpawnDeadline,
)
from cruxible_core.providers.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessRecoveryFailureV1,
    ProviderProcessRecoveryResultV1,
)
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeBudgetsV1,
    ProviderRuntimeRunContextV1,
)
from cruxible_core.runtime.playbill_manager import PlaybillInstanceManager, get_playbill_manager
from cruxible_core.runtime.provider_runtime import (
    PROVIDER_RUNTIME_CONFIG_PATH,
    ProviderRuntimeOperator,
)
from cruxible_core.server.config import get_server_state_root
from cruxible_core.service.procedures.procedure_runs import ProcedureRunRecoveryRequired
from cruxible_core.service.procedures.procedures import service_execute_direct_procedure
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._provider_seal_support import write_test_provider_seal_v2
from tests.support.invocation_ids import unique_invocation_id
from tests.support.procedures import MAX_WALL_CLOCK, RUN_WALL_CLOCK, named
from tests.support.short_temporary_root import short_temporary_directory
from tests.test_procedures.test_procedure_execution import (
    _accepted_line_for_admission,
)
from tests.test_providers.test_provider_invocation_journal import (
    _accepted_one_provider,
    _Authority,
    _Contracts,
    _prepared_acquisition_run,
)
from tests.test_providers.test_provider_local_driver import _fake_interpreter
from tests.test_server.test_playbill_line_run_refusals import (
    _accept_members,
)


def _indexed_instance(tree: dict[str, bytes]) -> SimpleNamespace:
    """Unit double for the pinned-version reader: one row per exact (identity, digest).

    Every path is a provider build now (ProviderInterface removed, C12): there
    is no second artifact kind this tree can hold.
    """
    from cruxible_client.contracts.providers import parse_provider, provider_digest

    sources: dict[str, object] = {}
    rows: dict[tuple[str, str], str] = {}
    for path, content in tree.items():
        provider = parse_provider(content, path=path)
        digest = provider_digest(provider).tagged
        source: object = provider
        sources[provider.identity.qualified] = source
        if provider.lifecycle.state == "live":
            rows[(provider.identity.qualified, digest)] = path

    def execute(_sql: str, parameters: tuple[str, str]) -> SimpleNamespace:
        path = rows.get(parameters)
        return SimpleNamespace(fetchone=lambda: None if path is None else (path,))

    typed = SimpleNamespace(
        connection=SimpleNamespace(execute=execute),
        source=lambda identity: sources.get(identity),
    )
    return SimpleNamespace(
        coordinate_for_oid=lambda oid: oid,
        bind_accepted_projection=lambda coordinate: nullcontext(SimpleNamespace(typed=typed)),
    )


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# This file's fake interpreter (``_fake_interpreter``) always reads its
# document's ``input.value`` and always answers ``{"echo": <that value>}`` --
# a fixed wire shape predating the operations model. F02 now checks every
# operation's output against its own pinned Contract (call or Source alike),
# so the served operation has to declare exactly this in/out shape itself,
# rather than reusing ``operation_world``'s generic ``test.value`` Contract.
_OPERATION_INPUT = Contract.model_validate(
    {"name": "test.served-provider-input", "version": 1, "fields": {"value": {"type": "string"}}}
)
_OPERATION_OUTPUT = Contract.model_validate(
    {"name": "test.served-provider-output", "version": 1, "fields": {"echo": {"type": "string"}}}
)


def _served_operation_digests(ref: object) -> str | None:
    for contract in (_OPERATION_INPUT, _OPERATION_OUTPUT):
        if (ref.name, ref.version) == (contract.name, contract.version):  # type: ignore[attr-defined]
            return contract_digest(contract).tagged
    return None


def _served_provider_build(*, provider_id: str = "served-provider") -> NamedProviderBuild:
    """A fresh build serving ``compute`` at the fake interpreter's exact wire shape."""

    manifest = ProviderRuntimeManifest(
        provider_id=provider_id,
        distribution=ProviderDistributionRef(name=provider_id, version="1.0.0"),
        supported_protocol_majors=(1,),
        implementations=(
            ProviderImplementationManifest(
                interface_id="compute",
                interface_digest="sha256:" + "9" * 64,
                entrypoint="demo.runtime:Provider",
                backends=("local_env",),
                declared_input_buckets=(),
                deterministic=True,
                side_effects=False,
            ),
        ),
    )
    operation = ProviderOperation.model_validate(
        {
            "name": "compute",
            "in": f"{_OPERATION_INPUT.name}@{_OPERATION_INPUT.version}",
            "out": f"{_OPERATION_OUTPUT.name}@{_OPERATION_OUTPUT.version}",
            "effect": "pure",
            "entrypoint": "demo.runtime:Provider",
        }
    )
    document = PackageRegistrationDocumentV1(
        schema_version=2, manifest=manifest, operations=(operation,)
    )
    return document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=provider_id,
            version="1.0.0",
            filename=f"{provider_id}-1.0.0-py3-none-any.whl",
            sha256="sha256:" + "0" * 64,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "0" * 64,
            materialization_digests={"linux-cp311+engine": "sha256:" + "0" * 64},
        ),
        control_domain=provider_id,
        operations=document.pinned_operations(_served_operation_digests),
    )


def _procedure_bound_to_provider(accepted_provider: AcceptedProvider):  # type: ignore[no-untyped-def]
    """``_accepted_one_provider``'s stock Procedure, with its one Call re-input.

    The executor reads ``accepted.procedure.slot_bindings`` straight off a
    ``BoundAcceptedProcedure`` (``ProcedureExecutor.execute``'s
    ``self.slot_bindings``), so the literal input change has to happen on the
    unbound ``ProcedureArtifact`` first and the slot rebound afterward --
    patching the already-bound execution plan in place would leave its
    ``definition_digest``/``slot_bindings`` stale.
    """

    from tests.core_support._p2b1_support import digest as _p2b1_digest

    accepted = _accepted_one_provider(provider=accepted_provider)
    authored = authored_procedure(accepted)
    node = authored.definition.nodes[0]
    assert isinstance(node, CallNode)
    node = node.model_copy(update={"input": {"value": "line-served"}})
    definition = authored.definition.model_copy(update={"nodes": (node,)})
    procedure = authored.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    unbound = AcceptedProcedure(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )

    operation = accepted_provider.provider.operations[0]
    build_pin = binding_pin(accepted_provider.provider.identity, accepted_provider.artifact_digest)
    slot = next(iter(definition.slots))

    def lookup(identity):  # type: ignore[no-untyped-def]
        if identity.kind == "Provider" and identity == accepted_provider.provider.identity:
            return accepted_provider.provider, accepted_provider.artifact_digest
        if identity.kind == "Contract":
            return object(), _p2b1_digest(identity.name)
        return None

    return bind_procedure_references(
        unbound,
        lookup=lookup,
        bindings={slot: LineBinding(build=build_pin, operation=operation.name)},
    )


def _complete_line_for_admission(
    admission: ProcedureRunAdmissionV9,
    accepted_procedure,  # type: ignore[no-untyped-def]
    *,
    accepted_provider: AcceptedProvider,
) -> AcceptedLineSpec:
    historical = _accepted_line_for_admission(admission, accepted_procedure).line
    definition = accepted_procedure.procedure.definition
    operation = accepted_provider.provider.operations[0]
    line = LineSpec.model_validate(
        {
            **historical.model_dump(mode="json"),
            "bindings": {
                slot: LineBinding(
                    build=binding_pin(
                        accepted_provider.provider.identity, accepted_provider.artifact_digest
                    ),
                    operation=operation.name,
                ).model_dump(mode="json")
                for slot in definition.slots
            },
            "budgets": {
                "max_capture_bytes": definition.budget.max_capture_bytes,
                "max_items": definition.budget.max_items,
                "max_provider_calls": definition.budget.max_provider_calls,
                "max_wall_clock_microseconds": definition.budget.wall_clock.microseconds,
            },
        }
    )
    path = line_spec_path(line.identity.name)
    law = evaluate_line_spec_law(
        line,
        path=path,
        procedure=accepted_procedure,
        predecessor=None,
        slots=line_slot_signatures(definition),
        providers={accepted_provider.provider.identity.qualified: accepted_provider},
    )
    assert law.verdict == "accepted", law.diagnostics
    return AcceptedLineSpec(
        path=path,
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )


def _rebind_prepared_line(
    prepared: PreparedProcedureRunV9,
    accepted_line: AcceptedLineSpec,
) -> PreparedProcedureRunV9:
    plan = prepared.acquisition_plan.model_copy(
        update={"line_spec_digest": accepted_line.artifact_digest}
    )
    plan_digest = procedure_acquisition_plan_digest(plan)
    fields = {
        name: getattr(prepared.admission, name)
        for name in ProcedureRunAdmissionV9.model_fields
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
    provisional = ProcedureRunAdmissionV9.model_construct(**fields)
    provisional = provisional.model_copy(
        update={"semantic_replay_key_digest": procedure_semantic_replay_key_digest(provisional)}
    )
    admission_digest = procedure_admission_digest(provisional)
    admission = ProcedureRunAdmissionV9.model_validate(
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
    return PreparedProcedureRunV9.model_validate(
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
    # ruled per-user fallback. Use a short system temporary root; the budget
    # itself is unchanged.
    state_root = short_temporary_directory(".b2-").resolve()
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
    base = _served_provider_build()
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
    from tests.support.provider_builds import accepted_build

    provider = base.model_copy(
        update={
            "runtime_artifact": payload,
            "implementations": provider_expected_implementation_records(payload),
        }
    )
    accepted_provider = accepted_build(provider)
    provider = accepted_provider.provider
    deployment_digest = _digest("operator-deployment")
    implementation = provider.implementations[0]
    # A build now walks the same Provider/ProviderBuild chain as a package
    # install, so the driver's bind() requires a retained installation
    # verification (the `isinstance(provider, Provider)` branch) same as the
    # live-route test below.
    from cruxible_core.providers.provider_local_runtime import (
        ProviderInstallationVerificationV1,
        _deployment_identity,
    )

    bare_deployment = LocalProviderDeploymentV1(
        deployment_digest=deployment_digest,
        distribution_path=distribution,
        lock_path=lock,
        environment_path=materialization,
        environment_manifest_path=seal,
        environment_pin_key="linux-cp311+engine",
        interpreter_path=interpreter,
        provider_runtime_version="1.0.0",
    )
    verification = ProviderInstallationVerificationV1(
        deployment_identity_digest=_deployment_identity(bare_deployment),
        distribution_digest=_sha256_file(distribution),
        lock_digest=_sha256_file(lock),
        materialization_digest=materialization_digest,
        environment_manifest_digest=_sha256_file(seal),
        provider_runtime_version="1.0.0",
        entrypoints=tuple(item.entrypoint for item in provider.implementations),
    )
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
                        "installation_verification": verification.model_dump(mode="json"),
                    }
                ],
            }
        )
    )

    instance = _indexed_instance(
        {accepted_provider.path: render_provider(accepted_provider.provider)}
    )

    deployment = replace(bare_deployment, installation_verification=verification)
    operator = ProviderRuntimeOperator(state_root)
    assert operator.deployments == {deployment_digest: deployment}
    assert operator.process_leases is not None
    assert operator.process_leases.control_root == state_root / "c"
    recovery = operator.recover_all()
    assert recovery.recovered == ()
    assert recovery.removed == ()
    assert recovery.could_not_clean == ()
    invoker = operator.invoker_for(  # type: ignore[arg-type]
        instance,
        accepted_oid="a" * 40,
        pins=(
            ArtifactPin(
                role="provider",
                target=accepted_provider.provider.identity,
                artifact_digest=accepted_provider.artifact_digest,
            ),
        ),
    )
    operation = provider.operations[0]
    admitted_binding = operator.driver.bind(
        accepted_provider,
        operation,
        implementation.implementation_digest,
        deployment,
    ).binding
    # Retained from before the Line route existed: the operator's own invoke
    # path still has to reach the real subprocess, echo its output, verify the
    # binding it spawned against, and leave no lease behind.
    occurrence = SimpleNamespace(
        local_execution=admitted_binding,
        provider_artifact_digest=accepted_provider.artifact_digest,
        operation=operation,
        operation_signature_digest=operation_signature_digest(operation.signature()),
        implementation_digest=implementation.implementation_digest,
        secret_plan=ProviderSecretResolutionPlan(),
    )
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id="RUN-daemon-operator",
        interface_id=operation.name,
        interface_digest=operation_signature_digest(operation.signature()),
        implementation_digest=implementation.implementation_digest,
        entrypoint=admitted_binding.entrypoint,
        input={"value": "served"},
        input_bucket="legacy=none",
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=2, output_bytes=65_536),
    )

    outcome = invoker.invoke_provider(
        occurrence=occurrence,  # type: ignore[arg-type]
        context=context,
        invocation_id=unique_invocation_id(),
        bound=invoker.bind_provider(occurrence=occurrence),  # type: ignore[arg-type]
        deadline=ProviderSpawnDeadline(
            deadline_ns=time.monotonic_ns() + 60_000_000_000, monotonic_ns=time.monotonic_ns
        ),
    )
    assert outcome.envelope.output == {"echo": "served"}
    assert outcome.verified_binding == admitted_binding
    assert tuple(operator.process_leases.root.glob("*.json")) == ()

    accepted_procedure = _procedure_bound_to_provider(accepted_provider)
    (state_root / "line-service").mkdir()
    prepared, line_fixture = _prepared_acquisition_run(
        accepted_procedure,
        state_root / "line-service",
        provider=accepted_provider,
        local_binding=admitted_binding,
    )
    # ``_prepared_acquisition_run`` hardcodes operation_world's own ``VALUE``
    # Contract onto the occurrence it plans; this file's served operation
    # pins its own (F02 checks the occurrence's output against exactly this
    # closure), so the plan is corrected here rather than in that shared helper.
    occurrence = prepared.acquisition_plan.external_occurrences[0].model_copy(
        update={"contracts": (_OPERATION_INPUT, _OPERATION_OUTPUT)}
    )
    plan = prepared.acquisition_plan.model_copy(update={"external_occurrences": (occurrence,)})
    plan_digest = procedure_acquisition_plan_digest(plan)
    admission_fields = {
        name: getattr(prepared.admission, name)
        for name in ProcedureRunAdmissionV9.model_fields
        if name != "tag"
    }
    admission_fields["acquisition_plan_digest"] = plan_digest
    provisional_admission = ProcedureRunAdmissionV9.model_construct(**admission_fields)
    provisional_admission = provisional_admission.model_copy(
        update={
            "semantic_replay_key_digest": procedure_semantic_replay_key_digest(
                provisional_admission
            )
        }
    )
    admission_digest = procedure_admission_digest(provisional_admission)
    admission = ProcedureRunAdmissionV9.model_validate(
        {
            **provisional_admission.model_dump(mode="python"),
            "admission_binding_digest": admission_digest,
            "run_id": procedure_line_run_id(
                occurrence_id=provisional_admission.occurrence_id or "",
                attempt=provisional_admission.attempt,
                admission_binding_digest=admission_digest,
                occurrence_evaluation_time=provisional_admission.occurrence_evaluation_time,
            ),
        }
    )
    prepared = PreparedProcedureRunV9.model_validate(
        {
            **prepared.model_dump(mode="python"),
            "admission": admission,
            "acquisition_plan": plan,
            "acquisition_plan_digest": plan_digest,
        }
    )
    accepted_line = _complete_line_for_admission(
        prepared.admission,
        accepted_procedure,
        accepted_provider=accepted_provider,
    )
    prepared = _rebind_prepared_line(prepared, accepted_line)
    authored_for_lookup = authored_procedure(accepted_procedure)

    def _source(identity):  # type: ignore[no-untyped-def]
        # The fixture admission fixes its own provider output cap; the policy
        # in force must be the one it was planned under. ``_accepted_procedure``
        # resolves the plain, unbound artifact here -- it rebinds separately.
        if identity == PROCEDURE_RUNTIME_POLICY_IDENTITY:
            return seeded_procedure_runtime_policy().model_copy(
                update={"provider_output_bytes_cap": prepared.admission.provider_output_bytes_cap}
            )
        if identity == authored_for_lookup.identity:
            return authored_for_lookup
        return None

    def _envelope(key):  # type: ignore[no-untyped-def]
        if key == f"Procedure:{authored_for_lookup.identity.name}":
            return SimpleNamespace(
                identity=authored_for_lookup.identity,
                artifact_digest=accepted_procedure.artifact_digest,
            )
        return None

    bound = procedure_run_service.service_prepare_playbill_line_admission(
        SimpleNamespace(  # type: ignore[arg-type]
            resolve_accepted_coordinate=lambda **_kwargs: prepared.admission.bound_coordinate,
            bind_accepted_projection=lambda _coordinate: nullcontext(
                SimpleNamespace(typed=SimpleNamespace(source=_source, envelope=_envelope))
            ),
        ),
        admission=prepared.admission,
        accepted_line=accepted_line,
    )
    assert isinstance(bound, ProcedureRunAdmissionV9)
    prepared = PreparedProcedureRunV9.model_validate(
        {**prepared.model_dump(mode="python"), "admission": bound}
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

    runtime_root = Path(tempfile.mkdtemp(prefix=".u8-operator-"))
    request.addfinalizer(lambda: shutil.rmtree(runtime_root, ignore_errors=True))
    # The fallback reads the host's per-user runtime directory: XDG_RUNTIME_DIR on
    # Linux, TMPDIR on macOS. Point both here, never at the host's real one.
    monkeypatch.setenv("TMPDIR", str(runtime_root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
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


def test_a_deployment_config_written_with_classifier_installations_still_reads() -> None:
    """Deployments once recorded the bucket classifiers they hosted; core runs none
    now, so the legacy field is dropped on read and never written again."""

    from cruxible_core.runtime.provider_runtime import ProviderDeploymentConfigV1

    current = {
        "deployment_digest": _digest("legacy-deployment"),
        "distribution_path": "materializations/demo/provider.whl",
        "lock_path": "materializations/demo/uv.lock",
        "environment_path": "materializations/demo",
        "environment_manifest_path": "materializations/demo/environment.json",
        "environment_pin_key": "linux-cp311",
        "interpreter_path": "materializations/demo/python",
        "provider_runtime_version": "0.2.0",
    }
    legacy = {
        **current,
        "classifier_installations": [
            {
                "tag": "playbill-provider-bucket-classifier-installation-v1",
                "classifier_identity": "demo.size",
                "classifier_version": 1,
                "classifier_digest": _digest("classifier"),
                "conformance_fixture_set_digest": _digest("fixtures"),
                "results": [],
            }
        ],
    }
    read = ProviderDeploymentConfigV1.model_validate(legacy)
    assert read == ProviderDeploymentConfigV1.model_validate(current)
    assert "classifier_installations" not in read.model_dump(mode="json")


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
        _indexed_instance({}),  # type: ignore[arg-type]
        accepted_oid="a" * 40,
    )
    assert calls == []
    with pytest.raises(ProviderLocalRuntimeRefused):
        unavailable.bind_provider(occurrence=object())

    operator._in_flight = 0  # noqa: SLF001
    operator.invoker_for(
        _indexed_instance({}),  # type: ignore[arg-type]
        accepted_oid="a" * 40,
    )
    assert calls == ["recover"]
    assert operator.lane_status() == ("available", None, None)


_SERVED_RUN_INPUT = ProcedureOwnedContract(
    identity=ArtifactIdentity(kind="Contract", name="served-provider-run-input"),
    schema=ContractSchema(fields={"status": PropertySchema(type="string")}),
)
_SERVED_PROVIDER_OUTPUT = ProcedureOwnedContract(
    identity=ArtifactIdentity(kind="Contract", name="served-provider-output"),
    schema=ContractSchema(fields={"echo": PropertySchema(type="string")}),
)


def _provider_line_procedure(accepted_provider: AcceptedProvider) -> AcceptedProcedure:
    """One accepted Procedure whose single Call slot invokes the demo Provider.

    A Call is checked against its bound operation's own Contracts now (F02),
    resolved through the Line's binding at run time -- there is no longer an
    interface for the node to pin directly.
    """

    run_input = _SERVED_RUN_INPUT
    provider_output = _SERVED_PROVIDER_OUTPUT
    run_input_pin = ArtifactPin(
        role="contract-in",
        target=run_input.identity,
        artifact_digest=procedure_owned_contract_digest(run_input).tagged,
    )
    provider_output_pin = ArtifactPin(
        role="contract-out",
        target=provider_output.identity,
        artifact_digest=procedure_owned_contract_digest(provider_output).tagged,
    )
    operation = accepted_provider.provider.operations[0]
    definition = ProcedureDefinition(
        name="served-provider-triage",
        contract_in=named(run_input_pin),
        contract_out=named(provider_output_pin),
        nodes=(
            CallNode(
                node_id="ask",
                slot="ask",
                input={"value": "line-served"},
                as_="result",
            ),
        ),
        returns="result",
        slots={
            "ask": ProcedureSlot.model_validate(operation.signature().model_dump(by_alias=True))
        },
        budget=ProcedureBudget(
            wall_clock=RUN_WALL_CLOCK,
            max_provider_calls=2,
            max_capture_bytes=1024,
            max_items=10,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=MAX_WALL_CLOCK,
            max_provider_calls=4,
            max_capture_bytes=2048,
            max_items=20,
            max_repeat_attempts=1,
        ),
        terminal_capability=2,
    )
    procedure = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        owned_contracts=tuple(
            sorted(
                (run_input, provider_output),
                key=lambda contract: canonical_bytes(
                    contract.model_dump(mode="json", by_alias=True)
                ),
            )
        ),
        activation_policy="drain",
    )
    return AcceptedProcedure(
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
    and spawns the sealed interpreter as a real child process. Nothing on the
    route, the service, the admission, or the invoker is patched.
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
    # A Call is checked against its bound operation's own declared Contracts
    # (F02), so the demo Provider's build declares exactly the fake
    # interpreter's echo wire shape.
    base = _served_provider_build(provider_id="served-line-provider")
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
    provider = base.model_copy(
        update={
            "runtime_artifact": payload,
            "implementations": provider_expected_implementation_records(payload),
        }
    )
    from tests.support.provider_builds import as_build

    provider = as_build(provider)
    accepted_provider = AcceptedProvider(
        path=provider_path(provider.identity.name),
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
    deployment_digest = _digest("served-line-deployment")
    # Package builds require a retained installation verification. This
    # operator-owned fixture uses the v1 record matching its historical v2 seal.
    from cruxible_core.providers.provider_local_runtime import (
        ProviderInstallationVerificationV1,
        _deployment_identity,
    )

    local_deployment = LocalProviderDeploymentV1(
        deployment_digest=deployment_digest,
        distribution_path=distribution,
        lock_path=lock,
        environment_path=materialization,
        environment_manifest_path=seal,
        environment_pin_key="linux-cp311+engine",
        interpreter_path=interpreter,
        provider_runtime_version="1.0.0",
    )
    verification = ProviderInstallationVerificationV1(
        deployment_identity_digest=_deployment_identity(local_deployment),
        distribution_digest=_sha256_file(distribution),
        lock_digest=_sha256_file(lock),
        materialization_digest=materialization_digest,
        environment_manifest_digest=_sha256_file(seal),
        provider_runtime_version="1.0.0",
        entrypoints=tuple(item.entrypoint for item in provider.implementations),
    )
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
                        "installation_verification": verification.model_dump(mode="json"),
                    }
                ],
            }
        )
    )
    # The daemon read its operational config at startup, before this deployment
    # existed; dropping the cache is that daemon restarting, not a double.
    get_playbill_manager().clear()

    from cruxible_client.contracts.named_references import name_provider
    from tests.support.procedures import revalidated

    provider = name_provider(accepted_provider.provider)
    accepted_provider = accepted_provider.model_copy(
        update={"provider": provider, "artifact_digest": provider_digest(provider).tagged}
    )
    accepted = _provider_line_procedure(accepted_provider)
    procedure = revalidated(accepted.procedure)
    accepted = accepted.model_copy(
        update={
            "procedure": procedure,
            "artifact_digest": procedure_artifact_digest(procedure).tagged,
        }
    )
    procedure_pin = ArtifactPin(
        role="procedure",
        target=accepted.procedure.identity,
        artifact_digest=accepted.artifact_digest,
    )
    definition = accepted.procedure.definition
    operation = accepted_provider.provider.operations[0]
    line = LineSpec(
        identity=ArtifactIdentity(kind="Line", name="served-provider-hourly"),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={"status": "open"},
        bindings={
            "ask": LineBinding(
                build=binding_pin(
                    accepted_provider.provider.identity, accepted_provider.artifact_digest
                ),
                operation=operation.name,
            )
        },
        max_authority="propose",
        budgets={
            "max_capture_bytes": definition.budget.max_capture_bytes,
            "max_items": definition.budget.max_items,
            "max_provider_calls": definition.budget.max_provider_calls,
            "max_wall_clock_microseconds": definition.budget.wall_clock.microseconds,
        },
        epsilon={"$decimal": "0"},
    )
    mandate = ProcedureMandate(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="served-provider-mandate"),
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        grants="propose",
        resource_ceiling=definition.hard_caps,
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    instance = get_playbill_manager().get(instance_id)
    _accept_members(
        instance,
        reviewer_key_path,
        {
            accepted.path: render_procedure(authored_procedure(accepted)),
            accepted_provider.path: render_provider(accepted_provider.provider),
            contract_path(_OPERATION_INPUT.name, _OPERATION_INPUT.version): render_contract(
                _OPERATION_INPUT
            ),
            contract_path(_OPERATION_OUTPUT.name, _OPERATION_OUTPUT.version): render_contract(
                _OPERATION_OUTPUT
            ),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        timestamp="2026-09-03T09:00:00.000000Z",
    )

    def unexpected_tree(_oid: str) -> None:
        pytest.fail("Line runtime setup must read only selected accepted artifacts")

    monkeypatch.setattr(instance, "tree_at", unexpected_tree)

    identity_digest = line_identity_digest(line.identity)
    response = client.post(
        f"/api/v1/{instance_id}/lines/{identity_digest}/runs",
        json={
            "tag": "playbill-line-run-request-v1",
            "line": identity_digest,
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
