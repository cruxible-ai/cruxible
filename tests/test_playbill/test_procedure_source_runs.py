"""Served graph-v4 Source runs: a Procedure reads a workspace file, typed.

These are lane tests, not kernel tests: everything from the accepted tree to the
run receipt is the real served path -- accepted Provider seed, accepted
CaptureContract and SourceAcquisitionPolicy, the shared occurrence planner, the
V5 admission the direct lane now binds, the governed `WorkspaceFileReader`, and
the Capture the read becomes. The one stubbed seam is the Provider SUBPROCESS
itself, which `tests/test_playbill/p2b4_unit2/test_workspace_source_runtime.py`
already stubs at the same boundary; materializing the adapter environment is a
deployment concern, not a run-lane one.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, get_args

import pytest

from cruxible_client.contracts.acquisition_policies import (
    IndependentCoherenceV1,
    InputAcquisitionRuleV1,
    SourceAcquisitionPolicyV1,
    acquisition_policy_digest,
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    CanonicalDurationV1,
    CaptureContractV1,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactV2,
    ProcedureOwnedContractV1,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.models import (
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureDefinitionV4,
    ProcedureHardCapsV3,
    ProjectNodeV3,
    SourceNodeV3,
    SourceNodeV4,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalV1,
    ProcedureNodeRefusalV1,
)
from cruxible_client.contracts.provider_execution import (
    ProviderEgressObservationV1,
    VerifiedProviderBindingV1,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    parse_provider_interface,
    provider_interface_digest,
    provider_interface_path,
)
from cruxible_client.contracts.providers import (
    parse_provider,
    provider_digest,
    provider_path,
)
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILE_INTERFACE_DIGEST,
    WorkspaceFileSourceRequestV1,
)
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.provider_classifiers import (
    install_compiler_owned_provider_classifier,
)
from cruxible_core.playbill.provider_local_runtime import (
    BoundLocalProviderV1,
    ProviderDriverOutcomeV1,
)
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeResultEnvelopeV1
from cruxible_core.playbill.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_IMPLEMENTATION_DIGEST,
    WORKSPACE_FILE_INTERFACE_ID,
    WORKSPACE_FILE_PROVIDER_ID,
)
from cruxible_core.playbill.service.provider_seed import service_seed_workspace_file_provider
from cruxible_core.playbill.workspace_file import WorkspaceFileReader, workspace_binding_digest
from cruxible_core.service.playbill_procedure_runs import (
    SERVED_NODE_KINDS,
    ProcedureReadinessRequestV1,
    ProcedureRunRequestV2,
    served_node_kinds,
    service_get_playbill_procedure_run,
    service_playbill_procedure_readiness,
    service_run_playbill_procedure,
)
from tests.support.provider_seed import workspace_seed_materialization
from tests.test_playbill._candidate_support import submit_member_candidate
from tests.test_playbill._knowledge_loop_support import accept_proposal
from tests.test_playbill._pc_c_support import capture_contract
from tests.test_playbill._support import initialize_local

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SEED_STAMP = "2026-09-12T11:00:00.000000Z"
ACCEPT_STAMP = "2026-09-12T11:30:00.000000Z"
PROCEDURE_NAME = "osv-advisory"
SOURCE_ALIAS = "advisory"
RELATIVE_PATH = "data/osv-advisory.json"
ADVISORY = {"advisory_id": "OSV-2026-0001", "severity": "high"}


class _WorkspaceInvoker:
    """Stand in for the workspace.file adapter subprocess, byte-faithfully.

    It asserts the daemon handed it exactly the governed read -- the bytes the
    reader confirmed and the commitment digest the run derived -- and returns
    the parsed document, which is what the real adapter returns.
    """

    def __init__(self) -> None:
        self.spawn_calls = 0
        self.observed_bytes_digest: str | None = None

    def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
        return BoundLocalProviderV1(
            binding=occurrence.local_execution,
            interpreter_path=Path("/portable/workspace-provider"),
        )

    def invoke_provider(  # type: ignore[no-untyped-def]
        self, *, occurrence, context, invocation_id, bound
    ) -> ProviderDriverOutcomeV1:
        self.spawn_calls += 1
        assert isinstance(context.input, dict)
        raw = base64.b64decode(context.input["bytes"], validate=True)
        assert context.input["bytes_digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        self.observed_bytes_digest = str(context.input["bytes_digest"])
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0",
                run_id=context.run_id,
                status="ok",
                output={
                    "input_bucket": context.input_bucket,
                    "source": {
                        "logical_source": context.input["logical_source"],
                        "commitment_digest": context.input["commitment_digest"],
                        "bytes_digest": context.input["bytes_digest"],
                        "byte_length": context.input["byte_length"],
                    },
                    "content": {
                        "kind": "json",
                        "encoding": "utf-8",
                        "json": json.loads(raw),
                    },
                },
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservationV1(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=bound.binding,
        )


class _Operator:
    """A ProviderRuntimeOperator whose deployment is already materialized."""

    def __init__(self, invoker: _WorkspaceInvoker) -> None:
        self.invoker = invoker

    def invoker_for(self, instance, *, accepted_oid):  # type: ignore[no-untyped-def]
        return self.invoker

    def admit_line_provider(  # type: ignore[no-untyped-def]
        self,
        accepted_provider,
        accepted_interface,
        implementation_digest,
        *,
        eligible_environment_pin_keys,
    ) -> VerifiedProviderBindingV1:
        implementation = accepted_provider.provider.implementations[0]
        return VerifiedProviderBindingV1(
            provider_artifact_digest=accepted_provider.artifact_digest,
            interface_artifact_digest=accepted_interface.artifact_digest,
            interface_id=accepted_interface.registration.interface_id,
            interface_digest=accepted_interface.registration.interface_digest,
            implementation_digest=implementation_digest,
            deployment_digest="sha256:" + "a" * 64,
            materialization_digest=(
                implementation.materialization_references[0].materialization_digest
            ),
            environment_manifest_digest="sha256:" + "b" * 64,
            entrypoint=(
                accepted_provider.provider.runtime_artifact.manifest.implementations[0].entrypoint
            ),
        )


def _contracts() -> tuple[ProcedureOwnedContractV1, ProcedureOwnedContractV1]:
    return (
        ProcedureOwnedContractV1(
            identity=ArtifactIdentity(kind="Contract", name=f"{PROCEDURE_NAME}-input"),
            schema=ContractSchema(fields={}),
        ),
        ProcedureOwnedContractV1(
            identity=ArtifactIdentity(kind="Contract", name=f"{PROCEDURE_NAME}-output"),
            schema=ContractSchema(fields={"severity": PropertySchema(type="string")}),
        ),
    )


def _source_request(instance: PlaybillInstance, root: Path) -> WorkspaceFileSourceRequestV1:
    return WorkspaceFileSourceRequestV1(
        logical_source="commerce.production.orders",
        workspace_binding_digest=workspace_binding_digest(
            instance_id=instance.descriptor.instance_id,
            canonical_root=root.resolve(),
        ),
        relative_path=RELATIVE_PATH,
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "workspace"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
    )


def _procedure(
    instance: PlaybillInstance,
    *,
    root: Path,
    contract: CaptureContractV1,
    provider_pin: ArtifactPin,
    interface_pin: ArtifactPin,
    relative_path: str = RELATIVE_PATH,
) -> ProcedureArtifactV2:
    input_contract, output_contract = _contracts()
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
    capture_pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    request = _source_request(instance, root).model_copy(update={"relative_path": relative_path})
    definition = ProcedureDefinitionV4(
        name=PROCEDURE_NAME,
        description="Read one accepted advisory document and shape its severity.",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            SourceNodeV4(
                node_id="read",
                capture_contract=capture_pin,
                provider=provider_pin,
                interface=interface_pin,
                interface_digest=WORKSPACE_FILE_INTERFACE_DIGEST,
                implementation_digest=WORKSPACE_FILE_IMPLEMENTATION_DIGEST,
                request=request.model_dump(mode="json"),
                as_=SOURCE_ALIAS,
                next="shape",
            ),
            ProjectNodeV3(
                node_id="shape",
                fields={"severity": f"$steps.{SOURCE_ALIAS}.content.json.severity"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=5_000_000),
            max_provider_calls=2,
            max_capture_bytes=65_536,
            # No max_items: the authoring law ties a declared item budget to a
            # pinned Contract with a list field, and this graph shapes one row.
            max_items=None,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=10_000_000),
            max_provider_calls=4,
            max_capture_bytes=131_072,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=2,
    )
    return ProcedureArtifactV2(
        identity=ArtifactIdentity(kind="Procedure", name=PROCEDURE_NAME),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v4(definition).tagged,
        pins=tuple(
            sorted(
                (contract_in, contract_out, capture_pin, provider_pin, interface_pin),
                key=lambda pin: (
                    pin.role.encode("utf-8"),
                    pin.target.qualified.encode("utf-8"),
                    pin.artifact_digest.encode("ascii"),
                ),
            )
        ),
        owned_contracts=tuple(
            sorted(
                (input_contract, output_contract),
                key=lambda item: canonical_bytes(item.model_dump(mode="json", by_alias=True)),
            )
        ),
        activation_policy="drain",
    )


def _policy(*, input_name: str = SOURCE_ALIAS) -> SourceAcquisitionPolicyV1:
    return SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="advisory-reads"),
        inputs=(
            InputAcquisitionRuleV1(
                input_name=input_name,
                requirement="required",
                permitted_replayability=("attested_only", "exact"),
                max_age=CanonicalDurationV1(microseconds=3_600_000_000),
                on_unavailable="refuse",
                on_stale="refuse",
                on_oversized="refuse",
                on_conflict="preserve",
            ),
        ),
        coherence=IndependentCoherenceV1(),
    )


def _world(  # noqa: PLR0913
    tmp_path: Path,
    *,
    contents: bytes | None = None,
    contract: CaptureContractV1 | None = None,
    policy: SourceAcquisitionPolicyV1 | None = None,
    accept_policy: bool = True,
    accept_procedure: bool = True,
    relative_path: str = RELATIVE_PATH,
    write_at: str = RELATIVE_PATH,
):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(tmp_path)
    service_seed_workspace_file_provider(
        instance,
        actor_id="owner",
        timestamp=SEED_STAMP,
        configured_materialization=workspace_seed_materialization(),
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    registration = parse_provider_interface(
        tree[provider_interface_path(WORKSPACE_FILE_INTERFACE_ID)],
        path=provider_interface_path(WORKSPACE_FILE_INTERFACE_ID),
    )
    provider = parse_provider(
        tree[provider_path(WORKSPACE_FILE_PROVIDER_ID)],
        path=provider_path(WORKSPACE_FILE_PROVIDER_ID),
    )
    provider_pin = ArtifactPin(
        role="provider",
        target=provider.identity,
        artifact_digest=provider_digest(provider).tagged,
    )
    interface_pin = ArtifactPin(
        role="provider-interface",
        target=registration.identity,
        artifact_digest=provider_interface_digest(registration).tagged,
    )
    # The daemon operator installs the compiler-owned bucket classifier when it
    # binds an invoker; the stub operator does the same thing here so the run
    # classifies its input exactly as the served lane would.
    install_compiler_owned_provider_classifier(
        AcceptedProviderInterfaceRegistrationV1(
            path=provider_interface_path(WORKSPACE_FILE_INTERFACE_ID),
            registration=registration,
            artifact_digest=interface_pin.artifact_digest,
        )
    )
    # `initialize_local` attaches exactly this root to the instance.
    workspace_root = tmp_path / "workspace"
    target = workspace_root / write_at
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_bytes(ADVISORY) if contents is None else contents)

    accepted_contract = capture_contract() if contract is None else contract
    accepted_policy = _policy() if policy is None else policy
    procedure = _procedure(
        instance,
        root=workspace_root,
        contract=accepted_contract,
        provider_pin=provider_pin,
        interface_pin=interface_pin,
        relative_path=relative_path,
    )
    members = {
        capture_contract_path(accepted_contract.identity.name): render_capture_contract(
            accepted_contract
        )
    }
    if accept_procedure:
        members[procedure_path(PROCEDURE_NAME)] = render_procedure(procedure)
    if accept_policy:
        members[acquisition_policy_path(accepted_policy.identity.name)] = render_acquisition_policy(
            accepted_policy
        )
    inspection = submit_member_candidate(
        instance,
        members=members,
        actor_id="owner",
        proposal_name="source-run-world",
        proposal_family="procedure",
        timestamp=ACCEPT_STAMP,
    )
    accept_proposal(instance, owner, inspection)
    return instance, owner, procedure, workspace_root, accepted_policy


def _reader(instance: PlaybillInstance, root: Path) -> WorkspaceFileReader:
    return WorkspaceFileReader(
        instance_id=instance.descriptor.instance_id,
        operating_profile="local",
        attached_roots=(root,),
        managed_roots=(instance.root,),
    )


def _actor(instance: PlaybillInstance) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="owner",
        org_id=instance.descriptor.instance_id,
        operation_id="served-source-run-test",
        timestamp=NOW,
    )


def _run(  # type: ignore[no-untyped-def]
    instance,
    root,
    *,
    invoker: _WorkspaceInvoker | None = None,
    reader: WorkspaceFileReader | None = None,
    evaluation_time: datetime = NOW,
):
    spawned = _WorkspaceInvoker() if invoker is None else invoker
    return (
        service_run_playbill_procedure(
            instance,
            name=PROCEDURE_NAME,
            request=ProcedureRunRequestV2(input={}, evaluation_time=evaluation_time),
            actor_context=_actor(instance).model_copy(update={"timestamp": evaluation_time}),
            provider_runtime_operator=_Operator(spawned),  # type: ignore[arg-type]
            workspace_file_reader=_reader(instance, root) if reader is None else reader,
        ),
        spawned,
    )


# --- law ---------------------------------------------------------------------


def test_source_is_served_on_both_run_lanes_only_in_graph_v4() -> None:
    """The law moves by one token, and only for the generation that can plan it."""

    assert "source" in SERVED_NODE_KINDS
    assert "source" in served_node_kinds(4)
    assert "source" not in served_node_kinds(3)
    # Terminals, post_inbox, and mandate_settlement stay dark on both lanes.
    assert SERVED_NODE_KINDS.isdisjoint(
        {"emit_capture", "post_inbox", "propose_change_set", "mandate_settlement"}
    )


def test_the_sdk_authors_a_source_node_on_v4_and_refuses_it_on_v3(tmp_path: Path) -> None:
    """The SDK gate and the run lane move together: one token, one generation."""

    from cruxible_client.authoring.sdk import CapabilityNotServed, Playbill, ProcedureDraft

    instance, _owner, procedure, _root, _policy_artifact = _world(tmp_path)
    draft = Playbill.procedure(
        object(),
        definition=procedure.definition,
        activation_policy="drain",
        retire=False,
    )
    assert isinstance(draft, ProcedureDraft)

    v3_source = _graph_v3_source_definition(procedure)
    with pytest.raises(CapabilityNotServed) as excinfo:
        Playbill.procedure(
            object(),
            definition=v3_source,
            activation_policy="drain",
            retire=False,
        )
    assert excinfo.value.code == "playbill.sdk.procedure_capability_not_served"
    assert served_node_kinds(4) - served_node_kinds(3) == {"source"}


def _graph_v3_source_definition(procedure: ProcedureArtifactV2) -> ProcedureDefinitionV3:
    """The same shape one generation back, where nothing can plan the read."""

    source = procedure.definition.nodes[0]
    assert isinstance(source, SourceNodeV4)
    return ProcedureDefinitionV3(
        name=procedure.definition.name,
        contract_in=procedure.definition.contract_in,
        contract_out=procedure.definition.contract_out,
        nodes=(
            SourceNodeV3(
                node_id=source.node_id,
                capture_contract=source.capture_contract,
                provider=source.provider,
                request=source.request,
                as_=source.as_,
                next="shape",
            ),
            procedure.definition.nodes[1],
        ),
        returns=procedure.definition.returns,
        budget=procedure.definition.budget,
        hard_caps=procedure.definition.hard_caps,
        terminal_capability=procedure.definition.terminal_capability,
    )


# --- the served direct lane --------------------------------------------------


def test_readiness_reports_a_v4_source_procedure_ready(tmp_path: Path) -> None:
    instance, _owner, _procedure, _root, _policy_artifact = _world(tmp_path)

    readiness = service_playbill_procedure_readiness(
        instance,
        name=PROCEDURE_NAME,
        request=ProcedureReadinessRequestV1(evaluation_time=NOW),
    )

    assert readiness.state == "ready"
    assert readiness.unsupported_nodes == ()
    assert readiness.next_operation.kind == "run"


def test_a_direct_run_reads_the_workspace_file_and_retains_its_receipt(
    tmp_path: Path,
) -> None:
    instance, _owner, _procedure, root, policy_artifact = _world(tmp_path)

    state, invoker = _run(instance, root)

    assert state.status == "succeeded", state.terminal
    assert state.result == {"severity": "high"}
    assert invoker.spawn_calls == 1

    observation = state.source_observations[0]
    assert observation.input_name == SOURCE_ALIAS
    assert observation.occurrence_path == "source/read"
    assert observation.source_read_receipt is not None
    assert observation.source_read_receipt.relative_path == RELATIVE_PATH
    assert observation.source_read_receipt.bytes_digest == (
        "sha256:" + hashlib.sha256(canonical_bytes(ADVISORY)).hexdigest()
    )
    assert observation.capture_digest is not None
    assert invoker.observed_bytes_digest == observation.source_read_receipt.bytes_digest

    # The admission really planned this occurrence under the accepted policy,
    # with per-input decisions rather than the old empty placeholder.
    assert state.receipt is not None
    status = service_get_playbill_procedure_run(instance, run_id=state.run_id or "")
    assert status.source_observations == state.source_observations
    assert (
        status.source_observations[0].source_read_receipt_digest
        == observation.source_read_receipt_digest
    )
    assert acquisition_policy_digest(policy_artifact).tagged is not None


def test_a_changed_file_yields_a_different_receipt_and_a_different_value(
    tmp_path: Path,
) -> None:
    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)

    first, _invoker = _run(instance, root)
    assert first.status == "succeeded"

    changed = {"advisory_id": "OSV-2026-0001", "severity": "critical"}
    (root / RELATIVE_PATH).write_bytes(canonical_bytes(changed))

    # Re-running AT THE SAME EVALUATION INSTANT is the same occurrence, so it
    # replays the retained observation rather than silently minting a second
    # reading of a moved world.
    replayed, replay_invoker = _run(instance, root)
    assert replayed.run_id == first.run_id
    assert replayed.result == {"severity": "high"}
    assert replay_invoker.spawn_calls == 0

    second, _second_invoker = _run(instance, root, evaluation_time=NOW + timedelta(minutes=5))

    assert second.status == "succeeded", second.terminal
    assert second.result == {"severity": "critical"}
    assert (
        second.source_observations[0].source_read_receipt_digest
        != first.source_observations[0].source_read_receipt_digest
    )
    assert second.source_observations[0].capture_digest != (
        first.source_observations[0].capture_digest
    )
    # A changed observation is a different run, not a replay of the first.
    assert second.run_id != first.run_id


# --- typed refusals ----------------------------------------------------------


def test_a_path_outside_the_authorized_root_refuses_typed(tmp_path: Path) -> None:
    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path,
        relative_path="../outside.json",
        write_at=RELATIVE_PATH,
    )

    state, invoker = _run(instance, root)

    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusalV1)
    assert state.terminal.code == "workspace_file_read_refused"
    assert state.terminal.details["repair_commands"]
    assert invoker.spawn_calls == 0


def test_a_file_over_the_contract_selection_budget_refuses_typed(tmp_path: Path) -> None:
    contract = capture_contract()
    small = contract.model_copy(
        update={
            "selection_budget": contract.selection_budget.model_copy(update={"max_bytes": 16}),
        }
    )
    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path, contract=small)

    state, invoker = _run(instance, root)

    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusalV1)
    assert state.terminal.code == "workspace_file_read_refused"
    assert state.terminal.details["path_class"] == "size_budget"
    assert invoker.spawn_calls == 0


def test_a_run_without_a_daemon_reader_refuses_typed(tmp_path: Path) -> None:
    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)
    invoker = _WorkspaceInvoker()

    state = service_run_playbill_procedure(
        instance,
        name=PROCEDURE_NAME,
        request=ProcedureRunRequestV2(input={}, evaluation_time=NOW),
        actor_context=_actor(instance),
        provider_runtime_operator=_Operator(invoker),  # type: ignore[arg-type]
        workspace_file_reader=None,
    )

    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusalV1)
    assert state.terminal.code == "workspace_file_read_refused"
    assert state.terminal.details["path_class"] == "binding"
    assert invoker.spawn_calls == 0


def test_a_missing_accepted_policy_refuses_before_any_journal(tmp_path: Path) -> None:
    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path, accept_policy=False)
    journal_root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    assert not journal_root.exists()

    state, invoker = _run(instance, root)

    assert state.status == "admission_refused"
    assert isinstance(state.terminal, ProcedureAdmissionRefusalV1)
    assert state.terminal.code == "source_acquisition_policy_required"
    assert state.terminal.details["required_input_names"] == [SOURCE_ALIAS]
    assert state.terminal.repair is not None
    assert invoker.spawn_calls == 0
    assert not journal_root.exists()


def test_a_policy_that_declares_another_input_refuses_before_any_journal(
    tmp_path: Path,
) -> None:
    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path, policy=_policy(input_name="other-input")
    )

    state, invoker = _run(instance, root)

    assert state.status == "admission_refused"
    assert isinstance(state.terminal, ProcedureAdmissionRefusalV1)
    assert state.terminal.code == "source_acquisition_policy_required"
    assert state.terminal.details["matching_policy_digests"] == []
    assert invoker.spawn_calls == 0


def test_a_source_closure_that_stops_reproducing_refuses_before_the_first_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan whose CaptureContract no longer resolves refuses before execution.

    The accepted change-set law already refuses a Procedure whose Source pins a
    CaptureContract the tree does not hold, so the reachable stale-closure case
    is the contract going away between admission and execution. The run refuses
    at the Source preflight rather than reaching a Provider spawn.
    """

    import cruxible_core.service.playbill_procedure_runs as service

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)
    real_execute = service.service_execute_direct_procedure

    def without_contracts(*args, **kwargs):  # type: ignore[no-untyped-def]
        return real_execute(*args, **{**kwargs, "capture_contracts": {}})

    monkeypatch.setattr(service, "service_execute_direct_procedure", without_contracts)
    invoker = _WorkspaceInvoker()
    with pytest.raises(PlaybillExecutionError) as excinfo:
        _run(instance, root, invoker=invoker)

    assert "source_acquisition_plan_mismatch" in str(excinfo.value)
    assert invoker.spawn_calls == 0


def test_the_admitted_plan_mismatch_code_stays_served(tmp_path: Path) -> None:
    """The node-level mismatch code an admitted-then-drifted plan raises is served."""

    from cruxible_client.contracts.procedures.results import ProcedureNodeRefusalCodeV1

    assert "provider_acquisition_plan_mismatch" in get_args(ProcedureNodeRefusalCodeV1)
    assert "workspace_file_read_refused" in get_args(ProcedureNodeRefusalCodeV1)


def test_the_authoring_path_produces_the_exact_artifact_the_run_lane_executes(
    tmp_path: Path,
) -> None:
    """One lowering, one artifact: what authoring lowers is what the lane runs.

    The served surfaces -- SDK, HTTP, MCP and CLI -- all submit the same
    authoring intent into the one coordinator, so proving the coordinator lowers
    this graph-v4 Source Procedure into the exact accepted artifact the run lane
    executed above is what "the same definition gives the same digest" means.
    """

    from cruxible_client.contracts.authoring.models import ProcedureAuthoringPayloadV2
    from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.playbill.authoring.preflight import compute_preflight
    from cruxible_core.playbill.proposals import AuthenticatedActor

    instance, _owner, procedure, _root, _policy_artifact = _world(tmp_path, accept_procedure=False)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")

    # Authoring never supplies an exact digest: it names the accepted artifact
    # and the owned Contract, and lowering resolves both. That is what makes
    # the digest below an identity claim rather than an echo.
    compiled = coordinator.compile(
        actor=actor,
        payload=ProcedureAuthoringPayloadV2(
            definition=_authored_definition(procedure),
            activation_policy=procedure.activation_policy,
            owned_contracts=procedure.owned_contracts,
            retire=False,
        ),
        canonical_timestamp=ACCEPT_STAMP,
    )

    assert compiled.verdict == "passed", compiled.frontier.diagnostics
    intent = coordinator.list_pending(actor=actor).intents[0]
    lowered = compute_preflight(instance, intent=intent, actor=actor).lowered
    assert lowered is not None
    assert lowered.resolved_authoring["artifact_digest"] == (
        procedure_artifact_digest(procedure).tagged
    )
    assert lowered.resolved_authoring["definition"]["graph_format"] == 4
    assert lowered.proposed_tree[procedure_path(PROCEDURE_NAME)] == render_procedure(procedure)


def _authored_definition(procedure: ProcedureArtifactV2) -> dict[str, object]:
    """Render the same graph the way an author names it, not the way it resolved."""

    def rewrite(value: object) -> object:
        if isinstance(value, dict):
            if set(value) == {"role", "target", "artifact_digest"}:
                target = cast(dict[str, str], value["target"])
                if target["kind"] == "Contract":
                    return {
                        "kind": "carried_contract",
                        "name": target["name"],
                        "role": value["role"],
                    }
                return {
                    "tag": "playbill-authoring-artifact-reference-v1",
                    "role": value["role"],
                    "target": target,
                    "resolution": "accepted_at_intent_base",
                }
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    rendered = procedure.definition.model_dump(mode="json", by_alias=True)
    return cast(dict[str, object], rewrite(rendered))
