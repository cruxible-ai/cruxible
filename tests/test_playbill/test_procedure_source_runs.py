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
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast, get_args

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
from cruxible_client.contracts.captures import (
    CanonicalDurationV1,
    CaptureContractV1,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateV1,
    procedure_mandate_path,
    render_procedure_mandate,
)
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
from cruxible_client.contracts.procedures.line_specs import (
    LineSpecV2,
    ManualTriggerPolicyV1,
    line_identity_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    CaptureEgressNodeV3,
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
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust.records import parse_journal_payload
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.procedures.execution import ProcedureAdmissionBoundPayloadV5
from cruxible_core.playbill.provider_classifiers import (
    install_compiler_owned_provider_classifier,
)
from cruxible_core.playbill.provider_local_runtime import (
    BoundLocalProviderV1,
    ProviderDriverOutcomeV1,
)
from cruxible_core.playbill.provider_runtime_contract import (
    ProviderRuntimeRefusalV1,
    ProviderRuntimeResultEnvelopeV1,
)
from cruxible_core.playbill.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_IMPLEMENTATION_DIGEST,
    WORKSPACE_FILE_INTERFACE_ID,
    WORKSPACE_FILE_PROVIDER_ID,
)
from cruxible_core.playbill.service.provider_seed import service_seed_workspace_file_provider
from cruxible_core.playbill.workspace_file import WorkspaceFileReader, workspace_binding_digest
from cruxible_core.service.playbill_procedure_runs import (
    SERVED_NODE_KINDS,
    LineRunRequestV1,
    ProcedureReadinessRequestV1,
    ProcedureRunRequestV2,
    served_node_kinds,
    service_get_playbill_procedure_run,
    service_playbill_procedure_readiness,
    service_run_playbill_line,
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


def _procedure(  # noqa: PLR0913
    instance: PlaybillInstance,
    *,
    root: Path,
    contract: CaptureContractV1,
    provider_pin: ArtifactPin,
    interface_pin: ArtifactPin,
    relative_path: str = RELATIVE_PATH,
    policy_pin: ArtifactPin | None = None,
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
                (
                    contract_in,
                    contract_out,
                    capture_pin,
                    provider_pin,
                    interface_pin,
                    *(() if policy_pin is None else (policy_pin,)),
                ),
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


def _policy(
    *,
    input_name: str = SOURCE_ALIAS,
    name: str = "advisory-reads",
    requirement: str = "required",
    on_failure: str = "refuse",
) -> SourceAcquisitionPolicyV1:
    return SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name=name),
        inputs=(
            InputAcquisitionRuleV1(
                input_name=input_name,
                requirement=cast(Any, requirement),
                permitted_replayability=("attested_only", "exact"),
                max_age=CanonicalDurationV1(microseconds=3_600_000_000),
                on_unavailable=cast(Any, on_failure),
                on_stale=cast(Any, on_failure),
                on_oversized=cast(Any, on_failure),
                on_conflict="preserve",
            ),
        ),
        coherence=IndependentCoherenceV1(),
    )


def _policy_pin(policy: SourceAcquisitionPolicyV1) -> ArtifactPin:
    """The envelope pin an author's `acquisition_policy` name lowers into."""

    return ArtifactPin(
        role=ACQUISITION_POLICY_PIN_ROLE,
        target=policy.identity,
        artifact_digest=acquisition_policy_digest(policy).tagged,
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
    pin_policy: bool = False,
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
        policy_pin=_policy_pin(accepted_policy) if pin_policy else None,
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


# --- the same governed read, on the Line lane --------------------------------


LINE_NAME = "advisory-hourly"


def _served_line(
    procedure: ProcedureArtifactV2,
    policy: SourceAcquisitionPolicyV1,
) -> LineSpecV2:
    """A Line over the same graph-v4 Source Procedure the direct lane runs.

    Graph-v4 requires the v2 Line wire; this Procedure pins its Provider
    exactly, so it fills no slot and its closure list is empty.
    """

    procedure_pin = ArtifactPin(
        role="procedure",
        target=procedure.identity,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    policy_pin = _policy_pin(policy)
    caps = procedure.definition.hard_caps
    return LineSpecV2(
        identity=ArtifactIdentity(kind="Line", name=LINE_NAME),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={},
        slot_bindings=(),
        trigger_policy=ManualTriggerPolicyV1(),
        acquisition_policy=policy_pin,
        requested_terminal_rung=1,
        budgets={
            "max_capture_bytes": caps.max_capture_bytes,
            "max_items": caps.max_items,
            "max_provider_calls": caps.max_provider_calls,
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
        },
        epsilon={"$decimal": "0.1"},
        pins=tuple(
            sorted(
                (procedure_pin, policy_pin),
                key=lambda pin: (
                    pin.role.encode("utf-8"),
                    pin.target.qualified.encode("utf-8"),
                    pin.artifact_digest.encode("ascii"),
                ),
            )
        ),
        provider_implementation_closures=(),
    )


def _line_mandate(procedure: ProcedureArtifactV2) -> ProcedureMandateV1:
    return ProcedureMandateV1(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="advisory-line-mandate"),
        procedure=ArtifactPin(
            role="procedure",
            target=procedure.identity,
            artifact_digest=procedure_artifact_digest(procedure).tagged,
        ),
        rung=2,
        authority_ceiling=procedure.definition.hard_caps,
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


def _line_world(tmp_path: Path, **kwargs):  # type: ignore[no-untyped-def]
    """The direct lane's world, plus the accepted Line that triggers the same graph."""

    instance, owner, procedure, root, policy_artifact = _world(tmp_path, **kwargs)
    line = _served_line(procedure, policy_artifact)
    mandate = _line_mandate(procedure)
    _accept_more(
        instance,
        owner,
        {
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="advisory-line",
    )
    return instance, root, line


def _run_line(instance, root, line, *, invoker=None):  # type: ignore[no-untyped-def]
    spawned = _WorkspaceInvoker() if invoker is None else invoker
    identity_digest = line_identity_digest(line.identity)
    return (
        service_run_playbill_line(
            instance,
            path_identity_digest=identity_digest,
            request=LineRunRequestV1(
                line_identity_digest=identity_digest,
                occurrence_id=None,
                evaluation_time=None,
            ),
            actor_context=_actor(instance),
            caller_rung=2,
            provider_runtime_operator=_Operator(spawned),  # type: ignore[arg-type]
            workspace_file_reader=_reader(instance, root),
            daemon_clock=_TestClock(NOW),
        ),
        spawned,
    )


@dataclass(frozen=True)
class _TestClock:
    """The daemon clock a Line occurrence is derived from, pinned."""

    evaluation_time: datetime

    def now(self) -> datetime:
        return self.evaluation_time

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def test_a_line_occurrence_reads_the_workspace_file_through_the_same_reader(
    tmp_path: Path,
) -> None:
    """The Line lane now builds the reader too, and reads exactly what the direct lane does."""

    instance, root, line = _line_world(tmp_path)

    state, invoker = _run_line(instance, root, line)

    assert state.status == "succeeded", state.terminal
    assert state.result == {"severity": "high"}
    assert invoker.spawn_calls == 1
    observation = state.source_observations[0]
    assert observation.source_read_receipt is not None
    assert observation.source_read_receipt.relative_path == RELATIVE_PATH
    assert observation.capture_digest is not None


@pytest.mark.parametrize(
    ("relative_path", "write_at", "path_class"),
    [
        ("../outside.json", RELATIVE_PATH, "path_grammar"),
        (".GIT/config", RELATIVE_PATH, "git_metadata"),
        ("Data/.PlayBill/keys.json", RELATIVE_PATH, "playbill_control"),
    ],
)
def test_the_line_lane_refuses_the_same_uncontained_paths(
    tmp_path: Path,
    relative_path: str,
    write_at: str,
    path_class: str,
) -> None:
    """One reader, one root policy, one containment law -- on both lanes."""

    instance, root, line = _line_world(tmp_path, relative_path=relative_path, write_at=write_at)

    state, invoker = _run_line(instance, root, line)

    assert _read_refusal(state).details["path_class"] == path_class
    assert invoker.spawn_calls == 0


def test_the_line_lane_refuses_a_symlinked_leaf_and_a_symlinked_directory(
    tmp_path: Path,
) -> None:
    """The two containment cases the request grammar cannot reach, on the Line lane."""

    leaf_path = tmp_path / "leaf"
    leaf_path.mkdir()
    instance, root, line = _line_world(leaf_path, relative_path="data/link.json")
    secret = leaf_path / "outside-secret.json"
    secret.write_bytes(canonical_bytes({"severity": "leaked"}))
    (root / "data" / "link.json").symlink_to(secret)

    state, invoker = _run_line(instance, root, line)

    assert _read_refusal(state).details["path_class"] == "symlink"
    assert invoker.spawn_calls == 0

    dir_path = tmp_path / "dir"
    dir_path.mkdir()
    instance, root, line = _line_world(dir_path, relative_path="linkdir/osv.json")
    outside = dir_path / "outside-dir"
    outside.mkdir()
    (outside / "osv.json").write_bytes(canonical_bytes({"severity": "leaked"}))
    (root / "linkdir").symlink_to(outside, target_is_directory=True)

    state, invoker = _run_line(instance, root, line)

    assert _read_refusal(state).details["path_class"] == "symlink"
    assert invoker.spawn_calls == 0


def test_the_line_lane_attests_the_real_on_disk_name_for_a_case_flipped_read(
    tmp_path: Path,
) -> None:
    """The APFS law holds on the Line lane too: the kernel names the file, not the request."""

    instance, root, line = _line_world(
        tmp_path, relative_path="DATA/OSV-ADVISORY.JSON", write_at=RELATIVE_PATH
    )

    state, _invoker = _run_line(instance, root, line)

    if state.status == "node_refused":
        assert _read_refusal(state).details["path_class"] == "missing"
        pytest.skip("case-sensitive volume: no folding case to attest")
    assert state.status == "succeeded", state.terminal
    receipt = state.source_observations[0].source_read_receipt
    assert receipt is not None
    assert receipt.relative_path == RELATIVE_PATH
    assert receipt.requested_path == "DATA/OSV-ADVISORY.JSON"


# --- typed refusals ----------------------------------------------------------


def _read_refusal(state) -> ProcedureNodeRefusalV1:  # type: ignore[no-untyped-def]
    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusalV1)
    assert state.terminal.code == "workspace_file_read_refused"
    assert state.terminal.details["repair_commands"]
    # The daemon-authored path class travels in `details`; `detail_code` is the
    # Procedure-authored guard code alone.
    assert state.terminal.detail_code is None
    return state.terminal


def test_a_path_outside_the_authorized_root_refuses_typed(tmp_path: Path) -> None:
    """The `..` case the request GRAMMAR refuses, with a real file to reach."""

    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path,
        relative_path="../outside.json",
        write_at=RELATIVE_PATH,
    )
    (root.parent / "outside.json").write_bytes(canonical_bytes({"severity": "leaked"}))

    state, invoker = _run(instance, root)

    assert _read_refusal(state).details["path_class"] == "path_grammar"
    assert invoker.spawn_calls == 0


def test_a_symlinked_leaf_out_of_the_root_refuses_typed(tmp_path: Path) -> None:
    """Containment is on the real on-disk object, not on the spelling."""

    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path, relative_path="data/link.json"
    )
    secret = tmp_path / "outside-secret.json"
    secret.write_bytes(canonical_bytes({"severity": "leaked"}))
    (root / "data" / "link.json").symlink_to(secret)

    state, invoker = _run(instance, root)

    assert _read_refusal(state).details["path_class"] == "symlink"
    assert invoker.spawn_calls == 0


def test_a_symlinked_directory_inside_the_root_refuses_typed(tmp_path: Path) -> None:
    """Every component is opened without following, not just the leaf."""

    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path, relative_path="linkdir/osv.json"
    )
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "osv.json").write_bytes(canonical_bytes({"severity": "leaked"}))
    (root / "linkdir").symlink_to(outside, target_is_directory=True)

    state, invoker = _run(instance, root)

    assert _read_refusal(state).details["path_class"] == "symlink"
    assert invoker.spawn_calls == 0


def test_a_case_flipped_git_metadata_path_refuses_typed(tmp_path: Path) -> None:
    """The APFS incident's own case: the deny list folds, so `.GIT` is `.git`."""

    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path, relative_path=".GIT/config"
    )

    state, invoker = _run(instance, root)

    assert _read_refusal(state).details["path_class"] == "git_metadata"
    assert invoker.spawn_calls == 0


def test_a_case_flipped_playbill_control_path_refuses_typed(tmp_path: Path) -> None:
    """The same folding, on a denied name that is not the leading component."""

    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path, relative_path="Data/.PlayBill/keys.json"
    )

    state, invoker = _run(instance, root)

    assert _read_refusal(state).details["path_class"] == "playbill_control"
    assert invoker.spawn_calls == 0


def test_a_case_flipped_real_read_attests_the_real_on_disk_name(tmp_path: Path) -> None:
    """On a folding volume the requested spelling never becomes the attested one."""

    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path, relative_path="DATA/OSV-ADVISORY.JSON", write_at=RELATIVE_PATH
    )

    state, _invoker = _run(instance, root)

    if state.status == "node_refused":
        # A case-sensitive volume: the request simply names no file, and there
        # is no folding case to attest.
        assert _read_refusal(state).details["path_class"] == "missing"
        pytest.skip("case-sensitive volume: no folding case to attest")
    assert state.status == "succeeded", state.terminal
    receipt = state.source_observations[0].source_read_receipt
    assert receipt is not None
    assert receipt.relative_path == RELATIVE_PATH
    assert receipt.requested_path == "DATA/OSV-ADVISORY.JSON"


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


class _DecliningInvoker(_WorkspaceInvoker):
    """The governed read happens; the Provider then declines the material."""

    def invoke_provider(self, *, occurrence, context, invocation_id, bound):  # type: ignore[no-untyped-def]
        self.spawn_calls += 1
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0",
                run_id=context.run_id,
                status="refused",
                refusal=ProviderRuntimeRefusalV1(
                    code="provider_declined",
                    message="the adapter declined this material",
                ),
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservationV1(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=bound.binding,
        )


def test_a_planned_occurrence_that_fails_acquisition_applies_on_unavailable(
    tmp_path: Path,
) -> None:
    """The declared failure behaviour fires from a real read, at execution time.

    The planner never scores an absent occurrence, so this is where
    `on_unavailable` belongs: the occurrence WAS planned, the read WAS attempted,
    and the Provider declined it. An optional input whose rule says
    `omit_optional` is then omitted rather than refused -- the same rule, applied
    to a fact rather than to a plan.
    """

    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path,
        policy=_policy(requirement="optional", on_failure="omit_optional"),
    )

    state, invoker = _run(instance, root, invoker=_DecliningInvoker())

    assert invoker.spawn_calls == 1
    assert _acquisition_decisions(instance) == [(SOURCE_ALIAS, "omitted")]
    # The Source node did not refuse: the omission carried past it, and the
    # graph then failed where an omitted input actually shows -- at the
    # projection that names it.
    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusalV1)
    assert (state.terminal.node_id, state.terminal.code) == (
        "shape",
        "runtime_reference_unresolved",
    )
    # The read still happened and is still retained; only the material was
    # declined, so the occurrence carries a receipt and no Capture.
    observation = state.source_observations[0]
    assert observation.source_read_receipt is not None
    assert observation.capture_digest is None


def test_a_required_rule_refuses_the_same_declined_read_at_the_source_node(
    tmp_path: Path,
) -> None:
    """The other direction of the same execution-time application."""

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)

    state, invoker = _run(instance, root, invoker=_DecliningInvoker())

    assert invoker.spawn_calls == 1
    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusalV1)
    assert state.terminal.node_id == "read"
    assert state.terminal.code == "provider_declined"


def _acquisition_decisions(instance: PlaybillInstance) -> list[tuple[str, str]]:
    """Every declared failure behaviour the RUN applied, straight off the journal."""

    import cruxible_core.service.playbill_procedure_runs as service

    journal, _root = service._journal(instance)  # noqa: SLF001
    stream = service._stream(instance)  # noqa: SLF001
    bodies = instance.body_store()
    access = BodyAccessContext(principal_id="test", can_read_body=True)
    applied: list[tuple[str, str]] = []
    for partition_id in journal.partition_ids(stream):
        for stored in journal.all_records(stream, partition_id):
            if stored.record.event_kind != "source_acquisition":
                continue
            payload = parse_journal_payload(
                bodies.read(stored.record.payload_digest, access=access)
            )
            assert isinstance(payload, dict)
            decision = cast(dict[str, Any], payload["decision"])
            applied.append((str(payload["input_name"]), str(decision["disposition"])))
    return applied


# --- C2: a direct run causes no effect ---------------------------------------


def test_a_v4_terminal_cannot_fire_on_the_direct_lane(tmp_path: Path) -> None:
    """The restated C2 law, driven rather than read.

    Graph-v4 serves `source`; it does not serve a terminal, and a direct run
    binds no effective rung, so `_verify_effective_rung` would refuse one even
    if a caller found a way to hand it over. What a caller can actually reach is
    this: a v4 Procedure whose Source flows into an `emit_capture` terminal.
    Readiness names the terminal unsupported, the run refuses at ADMISSION --
    before any journal directory exists -- no Provider is spawned, and no egress
    is observed.
    """

    instance, owner, procedure, root, _policy_artifact = _world(tmp_path, accept_procedure=False)
    source = procedure.definition.nodes[0]
    assert isinstance(source, SourceNodeV4)
    definition = procedure.definition.model_copy(
        update={
            "nodes": (
                source.model_copy(update={"next": "emit"}),
                CaptureEgressNodeV3(
                    node_id="emit",
                    capture_contract=source.capture_contract,
                    input=f"$steps.{SOURCE_ALIAS}",
                ),
            ),
            "returns": SOURCE_ALIAS,
        }
    )
    with_terminal = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
        }
    )
    _accept_more(
        instance,
        owner,
        {procedure_path(PROCEDURE_NAME): render_procedure(with_terminal)},
        name="terminal-procedure",
    )
    journal_root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    assert not journal_root.exists()

    readiness = service_playbill_procedure_readiness(
        instance,
        name=PROCEDURE_NAME,
        request=ProcedureReadinessRequestV1(evaluation_time=NOW),
    )
    assert [row.kind for row in readiness.unsupported_nodes] == ["emit_capture"]
    assert readiness.state == "unsupported"

    state, invoker = _run(instance, root)

    assert state.status == "admission_refused", state.terminal
    assert isinstance(state.terminal, ProcedureAdmissionRefusalV1)
    assert state.terminal.code == "unsupported_node"
    assert state.run_id is None
    assert invoker.spawn_calls == 0
    # No journal at all, so no effect record and nothing to observe egress from.
    assert not journal_root.exists()


# --- the durable policy binding: the Procedure's own envelope pin -------------


SECOND_STAMP = "2026-09-12T11:45:00.000000Z"


def _accept_more(instance, owner, members, *, name: str) -> None:  # type: ignore[no-untyped-def]
    inspection = submit_member_candidate(
        instance,
        members=members,
        actor_id="owner",
        proposal_name=name,
        proposal_family="procedure",
        timestamp=SECOND_STAMP,
    )
    accept_proposal(instance, owner, inspection)


def _twin_policy() -> SourceAcquisitionPolicyV1:
    """Another team's policy that happens to declare the same alias set."""

    return _policy(name="advisory-twin")


def test_a_pinned_procedure_reads_its_policy_off_its_own_envelope(tmp_path: Path) -> None:
    """The pin is the binding: it names the exact policy, and nothing else does."""

    instance, _owner, procedure, root, policy_artifact = _world(tmp_path, pin_policy=True)
    pin = _policy_pin(policy_artifact)
    assert pin in procedure.pins

    state, invoker = _run(instance, root)

    assert state.status == "succeeded", state.terminal
    assert invoker.spawn_calls == 1
    assert _admission(instance, state).acquisition_policy_digest == pin.artifact_digest


def test_accepting_an_unrelated_policy_cannot_change_a_pinned_procedures_run(
    tmp_path: Path,
) -> None:
    """The defect the pin closes: another team's acceptance is not this run's input.

    Resolve-from-accepted-state keys on the Procedure's alias SET, and aliases
    are Procedure-local names, so a second live policy declaring the same set
    made an already accepted Procedure unrunnable. A pinned Procedure never
    consults the rest of the tree, so the same acceptance is a no-op for it.
    """

    instance, owner, _procedure, root, policy_artifact = _world(tmp_path, pin_policy=True)
    twin = _twin_policy()
    assert [rule.input_name for rule in twin.inputs] == [SOURCE_ALIAS]
    _accept_more(
        instance,
        owner,
        {acquisition_policy_path(twin.identity.name): render_acquisition_policy(twin)},
        name="twin-policy",
    )

    state, invoker = _run(instance, root, evaluation_time=NOW + timedelta(minutes=5))

    assert state.status == "succeeded", state.terminal
    assert invoker.spawn_calls == 1
    # It resolved the PINNED policy, not one of the two the alias set matches.
    assert _admission(instance, state).acquisition_policy_digest == (
        acquisition_policy_digest(policy_artifact).tagged
    )


def test_the_same_second_acceptance_refuses_an_unpinned_procedure(tmp_path: Path) -> None:
    """The fallback's own behaviour, unchanged and still the reason to pin."""

    instance, owner, _procedure, root, _policy_artifact = _world(tmp_path)
    twin = _twin_policy()
    _accept_more(
        instance,
        owner,
        {acquisition_policy_path(twin.identity.name): render_acquisition_policy(twin)},
        name="twin-policy",
    )

    state, invoker = _run(instance, root, evaluation_time=NOW + timedelta(minutes=5))

    assert state.status == "admission_refused", state.terminal
    assert isinstance(state.terminal, ProcedureAdmissionRefusalV1)
    assert state.terminal.code == "source_acquisition_policy_required"
    assert len(state.terminal.details["matching_policy_digests"]) == 2
    assert invoker.spawn_calls == 0


def test_a_pinned_policy_declaring_other_inputs_refuses_at_admission(tmp_path: Path) -> None:
    """A pin binds an exact artifact; it does not excuse it from governing these inputs."""

    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path,
        policy=_policy(input_name="other-input"),
        pin_policy=True,
    )
    journal_root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"

    state, invoker = _run(instance, root)

    assert state.status == "admission_refused", state.terminal
    assert isinstance(state.terminal, ProcedureAdmissionRefusalV1)
    assert state.terminal.code == "source_acquisition_policy_required"
    assert state.terminal.details["required_input_names"] == [SOURCE_ALIAS]
    assert state.terminal.details["declared_input_names"] == ["other-input"]
    assert state.terminal.repair is not None
    assert invoker.spawn_calls == 0
    assert not journal_root.exists()


def test_a_procedure_pinning_an_absent_policy_is_refused_at_acceptance(tmp_path: Path) -> None:
    """The pin closes at acceptance, like every other non-deferred pin kind."""

    instance, _owner, procedure, _root, _policy_artifact = _world(tmp_path, accept_procedure=False)
    absent = _policy(name="never-accepted")
    dangling = procedure.model_copy(
        update={
            "pins": tuple(
                sorted(
                    (*procedure.pins, _policy_pin(absent)),
                    key=lambda pin: (
                        pin.role.encode("utf-8"),
                        pin.target.qualified.encode("utf-8"),
                        pin.artifact_digest.encode("ascii"),
                    ),
                )
            )
        }
    )

    inspection = submit_member_candidate(
        instance,
        members={procedure_path(PROCEDURE_NAME): render_procedure(dangling)},
        actor_id="owner",
        proposal_name="dangling-policy-pin",
        proposal_family="procedure",
        timestamp=SECOND_STAMP,
    )

    evaluation = inspection.proposal.evaluation
    assert evaluation.verdict == "refused"
    unresolved = [
        item for item in evaluation.diagnostics if item.code == "playbill.change_set.unresolved_pin"
    ]
    assert len(unresolved) == 1
    detail = json.loads(unresolved[0].message)["pins"]
    assert [item["pin_role"] for item in detail] == [ACQUISITION_POLICY_PIN_ROLE]
    assert detail[0]["target_identity"]["name"] == absent.identity.name
    assert detail[0]["reason"] == "missing_or_digest_mismatch"


def test_the_policy_pin_leaves_the_definition_digest_byte_identical(tmp_path: Path) -> None:
    """The pin rides on the envelope, so no accepted definition byte moves."""

    _instance, _owner, unpinned, _root, policy_artifact = _world(tmp_path)
    pinned = unpinned.model_copy(
        update={
            "pins": tuple(
                sorted(
                    (*unpinned.pins, _policy_pin(policy_artifact)),
                    key=lambda pin: (
                        pin.role.encode("utf-8"),
                        pin.target.qualified.encode("utf-8"),
                        pin.artifact_digest.encode("ascii"),
                    ),
                )
            )
        }
    )

    assert pinned.definition_digest == unpinned.definition_digest
    assert compute_procedure_definition_digest_v4(pinned.definition).tagged == (
        compute_procedure_definition_digest_v4(unpinned.definition).tagged
    )
    # The ARTIFACT digest moves, because adopting the pin authors a new
    # Procedure; nothing already accepted is rewritten.
    assert procedure_artifact_digest(pinned).tagged != procedure_artifact_digest(unpinned).tagged


def test_the_authoring_path_lowers_the_named_policy_into_the_envelope_pin(
    tmp_path: Path,
) -> None:
    """How an author names it: the policy's semantic name, and lowering owns the digest."""

    from cruxible_client.contracts.authoring.models import ProcedureAuthoringPayloadV2
    from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.playbill.authoring.preflight import compute_preflight
    from cruxible_core.playbill.proposals import AuthenticatedActor

    instance, _owner, procedure, _root, policy_artifact = _world(
        tmp_path, accept_procedure=False, pin_policy=True
    )
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")

    compiled = coordinator.compile(
        actor=actor,
        payload=ProcedureAuthoringPayloadV2(
            definition=_authored_definition(procedure),
            activation_policy=procedure.activation_policy,
            owned_contracts=procedure.owned_contracts,
            acquisition_policy=policy_artifact.identity.name,
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
    assert lowered.proposed_tree[procedure_path(PROCEDURE_NAME)] == render_procedure(procedure)
    assert (
        _policy_pin(policy_artifact).model_dump(mode="json") in (lowered.resolved_authoring["pins"])
    )


def test_the_sdk_carries_the_named_policy_into_the_authoring_payload(tmp_path: Path) -> None:
    """The SDK verb an author reaches for, and the payload it emits."""

    from cruxible_client.authoring.sdk import Playbill, ProcedureDraft

    _instance, _owner, procedure, _root, policy_artifact = _world(tmp_path)
    draft = Playbill.procedure(
        object(),
        definition=procedure.definition,
        activation_policy="drain",
        retire=False,
        acquisition_policy=policy_artifact.identity.name,
    )

    assert isinstance(draft, ProcedureDraft)
    assert draft.payload.acquisition_policy == policy_artifact.identity.name


def _admission(instance: PlaybillInstance, state):  # type: ignore[no-untyped-def]
    """The V5 admission this run bound, read back off its own journal."""

    import cruxible_core.service.playbill_procedure_runs as service

    journal, _root = service._journal(instance)  # noqa: SLF001
    stream = service._stream(instance)  # noqa: SLF001
    bodies = instance.body_store()
    access = BodyAccessContext(principal_id="test", can_read_body=True)
    for partition_id in journal.partition_ids(stream):
        for stored in journal.all_records(stream, partition_id):
            if stored.record.event_kind != "admission_bound":
                continue
            if stored.record.run_id != state.run_id:
                continue
            payload = parse_journal_payload(
                bodies.read(stored.record.payload_digest, access=access)
            )
            assert isinstance(payload, dict)
            return ProcedureAdmissionBoundPayloadV5.model_validate(payload).admission
    raise AssertionError("no admission_bound record for this run")


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


def test_an_external_mutation_occurrence_refuses_before_the_first_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Source occurrence is a READ; an interface declaring mutation is not one.

    `effect_class` comes from the accepted interface registration, so the only
    way to reach the guard is to plan the same occurrence with a mutating class.
    The Source preflight refuses the whole closure before the first attempt
    record, so the Provider is never spawned.
    """

    import cruxible_core.service.playbill_procedure_runs as service

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)
    real_plan = service._plan_external_occurrences  # noqa: SLF001

    def mutating(*args, **kwargs):  # type: ignore[no-untyped-def]
        return tuple(
            item.model_copy(update={"effect_class": "external_mutation"})
            for item in real_plan(*args, **kwargs)
        )

    monkeypatch.setattr(service, "_plan_external_occurrences", mutating)
    invoker = _WorkspaceInvoker()

    with pytest.raises(PlaybillExecutionError) as excinfo:
        _run(instance, root, invoker=invoker)

    assert "source_acquisition_plan_mismatch" in str(excinfo.value)
    assert invoker.spawn_calls == 0


def test_a_crash_between_the_read_receipt_and_the_capture_leaves_no_half_capture(
    tmp_path: Path,
) -> None:
    """The retained receipt is what makes an interrupted read legible and safe.

    The daemon reads the file, journals the receipt, and then dies before the
    Provider result is durable. Re-running the same occurrence must not read
    again: it refuses typed, and the half-run's own state shows exactly what
    happened -- the receipt for the bytes that were read, and no Capture.
    """

    from cruxible_core.service.playbill_procedure_runs import ProcedureRunRecoveryRequired

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)

    class _Exploding(_WorkspaceInvoker):
        def invoke_provider(self, **kwargs):  # type: ignore[no-untyped-def]
            self.spawn_calls += 1
            raise RuntimeError("simulated daemon crash after the governed read")

    with pytest.raises(PlaybillExecutionError) as crash:
        _run(instance, root, invoker=_Exploding())
    assert "provider_completion_not_durable" in str(crash.value)

    with pytest.raises(ProcedureRunRecoveryRequired) as rerun:
        _run(instance, root)
    assert "recovery_required" in str(rerun.value)

    status = service_get_playbill_procedure_run(instance, run_id=_admitted_run_id(instance))
    assert status.status == "running"
    observation = status.source_observations[0]
    assert observation.source_read_receipt is not None
    assert observation.source_read_receipt.bytes_digest == (
        "sha256:" + hashlib.sha256(canonical_bytes(ADVISORY)).hexdigest()
    )
    assert observation.capture_digest is None


def _admitted_run_id(instance: PlaybillInstance) -> str:
    """The one run id this instance's procedure journal bound."""

    import cruxible_core.service.playbill_procedure_runs as service

    journal, _root = service._journal(instance)  # noqa: SLF001
    stream = service._stream(instance)  # noqa: SLF001
    run_ids = [
        stored.record.run_id
        for partition_id in journal.partition_ids(stream)
        for stored in journal.all_records(stream, partition_id)
        if stored.record.event_kind == "admission_bound" and stored.record.run_id is not None
    ]
    assert len(run_ids) == 1, run_ids
    return run_ids[0]


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
