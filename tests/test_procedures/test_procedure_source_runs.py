"""Served Source runs: a Procedure reads a workspace file, typed.

These are lane tests, not kernel tests: everything from the accepted tree to the
run receipt is the real served path -- accepted Provider seed, accepted
CaptureContract and the Procedure's inline acquisition defaults, the shared
occurrence planner, the V5 admission the direct lane now binds, the governed
`WorkspaceFileReader`, and
the Capture the read becomes. The one stubbed seam is the Provider SUBPROCESS
itself, which `tests/test_exhaust/p2b4_unit2/test_workspace_source_runtime.py`
already stubs at the same boundary; materializing the adapter environment is a
deployment concern, not a run-lane one.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast, get_args

import pytest

from cruxible_client.authoring.inputs import CarriedContractInput, ProcedureInput
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    CanonicalDuration,
    CaptureContract,
    capture_contract_digest,
    capture_contract_path,
    parse_capture_envelope,
    render_capture_contract,
)
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandate,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.acquisition_rules import AcquisitionDefault
from cruxible_client.contracts.procedures.artifacts import (
    DIRECT_NODE_KINDS,
    LINE_NODE_KINDS,
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
    LineSpec,
    line_identity_digest,
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
)
from cruxible_client.contracts.procedures.results import (
    FAILURE_CAUSE_MESSAGE_LIMIT,
    ProcedureAdmissionRefusal,
    ProcedureNodeRefusal,
    ProcedureOperationalFailure,
    strip_url_userinfo,
)
from cruxible_client.contracts.provider_execution import (
    ProviderEgressObservation,
    VerifiedProviderBinding,
)
from cruxible_client.contracts.providers import (
    parse_provider,
    provider_digest,
    provider_path,
)
from cruxible_client.contracts.repairs import HandEditRepair
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILE_OPERATION,
    WorkspaceFileSourceRequestV2,
)
from cruxible_core.documents.workspace_file import WorkspaceFileReader, workspace_binding_digest
from cruxible_core.exhaust.records import parse_journal_payload
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.governance.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_BUILTIN_PROVIDER_ID,
)
from cruxible_core.procedures.execution import ProcedureAdmissionBoundPayload
from cruxible_core.providers.provider_local_runtime import (
    BoundLocalProviderV1,
    ProviderDriverOutcomeV1,
)
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeProviderErrorPayload,
    ProviderRuntimeRefusal,
    ProviderRuntimeResultEnvelope,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    ProcedureReadinessRequestV1,
    ProcedureRunRequest,
    service_get_playbill_procedure_run,
    service_playbill_procedure_readiness,
    service_run_playbill_line,
    service_run_playbill_procedure,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._candidate_support import submit_member_candidate
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.core_support._light_capture_support import record_contract as capture_contract
from tests.core_support._support import initialize_local
from tests.support.procedures import MAX_WALL_CLOCK, RUN_WALL_CLOCK, named

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SEED_STAMP = "2026-09-12T11:00:00.000000Z"
ACCEPT_STAMP = "2026-09-12T11:30:00.000000Z"
PROCEDURE_NAME = "osv-advisory"
SOURCE_ALIAS = "advisory"
RELATIVE_PATH = "data/osv-advisory.json"
ADVISORY = b"high"


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
        self, *, occurrence, context, invocation_id, bound, deadline
    ) -> ProviderDriverOutcomeV1:
        self.spawn_calls += 1
        assert isinstance(context.input, dict)
        raw = base64.b64decode(context.input["bytes"], validate=True)
        assert context.input["bytes_digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        self.observed_bytes_digest = str(context.input["bytes_digest"])
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelope(
                protocol_version="2.0",
                run_id=context.run_id,
                status="ok",
                output={
                    "input_bucket": "content_kind=text;byte_size=tiny",
                    "source": {
                        "logical_source": context.input["logical_source"],
                        "commitment_digest": context.input["commitment_digest"],
                        "bytes_digest": context.input["bytes_digest"],
                        "byte_length": context.input["byte_length"],
                    },
                    "content": {
                        "kind": "text",
                        "encoding": "utf-8",
                        "bom": False,
                        "newline": "none",
                        "trailing_newline": False,
                        "line_count": 1,
                        "character_count": len(raw.decode("utf-8")),
                        "text": raw.decode("utf-8"),
                        "lines": [raw.decode("utf-8")],
                    },
                },
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=bound.binding,
        )


class _Operator:
    """A ProviderRuntimeOperator whose deployment is already materialized."""

    def __init__(self, invoker: _WorkspaceInvoker) -> None:
        self.invoker = invoker

    def invoker_for(self, instance, *, accepted_oid, pins=()):  # type: ignore[no-untyped-def]
        return self.invoker

    def admit_line_provider(  # type: ignore[no-untyped-def]
        self,
        accepted_provider,
        bound,
        implementation_digest,
        *,
        eligible_environment_pin_keys,
    ) -> VerifiedProviderBinding:
        from cruxible_client.contracts.provider_operations import operation_signature_digest

        implementation = accepted_provider.provider.implementations[0]
        return VerifiedProviderBinding(
            provider_artifact_digest=accepted_provider.artifact_digest,
            operation=bound.name,
            operation_signature_digest=operation_signature_digest(bound.signature()),
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


def _contracts() -> tuple[ProcedureOwnedContract, ProcedureOwnedContract]:
    return (
        ProcedureOwnedContract(
            identity=ArtifactIdentity(kind="Contract", name=f"{PROCEDURE_NAME}-input"),
            schema=ContractSchema(fields={}),
        ),
        ProcedureOwnedContract(
            identity=ArtifactIdentity(kind="Contract", name=f"{PROCEDURE_NAME}-output"),
            schema=ContractSchema(fields={"severity": PropertySchema(type="string")}),
        ),
    )


def _source_request(instance: PlaybillInstance, root: Path) -> WorkspaceFileSourceRequestV2:
    # Authored as intent only: the daemon resolves the attached workspace.
    del instance, root
    return WorkspaceFileSourceRequestV2(
        logical_source="commerce.production.orders",
        relative_path=RELATIVE_PATH,
        coordinate_type="database-snapshot-v1",
        coordinate={"lsn": "workspace"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
    )


def _procedure(  # noqa: PLR0913
    instance: PlaybillInstance,
    *,
    root: Path,
    contract: CaptureContract,
    relative_path: str = RELATIVE_PATH,
    acquisition_defaults: dict[str, AcquisitionDefault] | None = None,
) -> ProcedureArtifact:
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
    from cruxible_client.contracts.captures import capture_components

    components = capture_components(contract)
    request = request.model_copy(
        update={
            "coordinate_type": components.coordinate_schema_pins[0].target.name,
            "selector_type": next(
                (
                    pin.target.name
                    for pin in components.selector_schema_pins
                    if pin.target.name == "relation-primary-key-v1"
                ),
                components.selector_schema_pins[0].target.name,
            ),
        }
    )
    from cruxible_client.contracts.shapes import contract_digest
    from cruxible_client.contracts.workspace_file import workspace_file_contracts

    _wf_source, _wf_content, wf_request, wf_result = workspace_file_contracts()
    definition = ProcedureDefinition(
        name=PROCEDURE_NAME,
        description="Read one accepted advisory document and shape its severity.",
        contract_in=named(contract_in),
        contract_out=named(contract_out),
        slots={
            "read": {
                "in": {
                    "name": "workspace.file.request",
                    "version": 1,
                    "digest": contract_digest(wf_request).tagged,
                },
                "out": {
                    "name": "workspace.file.result",
                    "version": 1,
                    "digest": contract_digest(wf_result).tagged,
                },
                "effect": "pure",
            }
        },
        nodes=(
            CallNode(
                node_id="read",
                captures={"capture": named(capture_pin)},
                slot="read",
                input=request.model_dump(mode="json"),
                as_=SOURCE_ALIAS,
                next="shape",
            ),
            ProjectNode(
                node_id="shape",
                fields={"severity": f"$steps.{SOURCE_ALIAS}.content.text"},
                contract_out=named(contract_out),
                as_="result",
            ),
        ),
        returns="result",
        budget=ProcedureBudget(
            wall_clock=RUN_WALL_CLOCK,
            max_provider_calls=2,
            max_capture_bytes=65_536,
            # No max_items: the authoring law ties a declared item budget to a
            # pinned Contract with a list field, and this graph shapes one row.
            max_items=None,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=MAX_WALL_CLOCK,
            max_provider_calls=4,
            max_capture_bytes=131_072,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=2,
    )
    procedure = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=PROCEDURE_NAME),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        owned_contracts=tuple(
            sorted(
                (input_contract, output_contract),
                key=lambda item: canonical_bytes(item.model_dump(mode="json", by_alias=True)),
            )
        ),
        activation_policy="drain",
        acquisition_defaults=acquisition_defaults or {},
    )

    from tests.support.procedures import revalidated

    return revalidated(procedure)


#: The extract that reads proposal items out of the workspace read's exact bytes.
PROPOSALS_ALIAS = "proposals"
#: A terminal fan-out over the items that extract read, one row per item.
FAN_OUT = {"items": f"$steps.{PROPOSALS_ALIAS}.rows"}
#: The Contracts a fan-out item row is typed by. Each nested record is read
#: whole: its Contract declares no fields, so the item's own model decides it.
_FAN_OUT_NESTED = ("test.fan-out-record",)
_FAN_OUT_ROW: dict[str, object] = {
    "name": "test.fan-out-item",
    "version": 1,
    "description": "One proposal item a feed states, read whole for a terminal fan-out.",
    "fields": {
        "tag": "string",
        "statement": "test.fan-out-record@1?",
        "rationale": "string?",
        "revises": "string?",
        "source": "test.fan-out-record@1?",
        "citation_role": "string?",
        "derivation": "test.fan-out-record@1?",
        "existing_claim_dispositions": "list<test.fan-out-record@1>{max_items: 64}?",
        "subject_kind": "string?",
        "key": "test.fan-out-record@1?",
        "create_new": "bool?",
        "retires": "string?",
        "reason": "string?",
        "effective_until": "string?",
    },
}


_DOCUMENT_ROW: dict[str, object] = {
    "name": "test.advisory-document",
    "version": 1,
    "description": "One advisory document: its severity, and whatever else it states.",
    "fields": {"severity": "string", "advisory_id": "string?"},
}
#: The extract reading the whole advisory as one record.
DOCUMENT_ALIAS = "document"


def fan_out_contracts() -> tuple[Any, ...]:
    """The fan-out row Contract and the open record it nests, digests pinned."""

    from cruxible_client.contracts.shapes import (
        contract_digest,
        pin_contract_refs,
        validate_contract,
    )

    record = validate_contract(
        {"name": "test.fan-out-record", "version": 1, "fields": {}, "description": "Any record."}
    )
    row = pin_contract_refs(
        validate_contract(_FAN_OUT_ROW),
        lambda ref: ref.model_copy(update={"digest": contract_digest(record).tagged}),
    )
    return record, row


def document_contract() -> Any:
    from cruxible_client.contracts.shapes import validate_contract

    return validate_contract(_DOCUMENT_ROW)


def fan_out_members() -> dict[str, bytes]:
    from cruxible_client.contracts.shapes import contract_path, render_contract

    return {
        contract_path(contract.name, contract.version): render_contract(contract)
        for contract in (*fan_out_contracts(), document_contract())
    }


def proposals_extract(*, next_node: str | None = None) -> Any:
    """Read ``/proposals/*`` of the workspace read's bytes, one row per proposal item."""

    from cruxible_client.contracts.procedures.extract import ExtractNode
    from cruxible_client.contracts.shapes import contract_digest

    _record, row = fan_out_contracts()
    return ExtractNode.model_validate(
        {
            "kind": "extract",
            "node_id": "items",
            "capture": f"$steps.{SOURCE_ALIAS}.capture",
            "records": "/proposals/*",
            "row": {
                "name": row.name,
                "version": row.version,
                "digest": contract_digest(row).tagged,
            },
            "fields": {
                name: {"at": name}
                for name in _FAN_OUT_ROW["fields"]  # type: ignore[union-attr]
            },
            "as": PROPOSALS_ALIAS,
            **({} if next_node is None else {"next": next_node}),
        }
    )


def with_fan_out(nodes: tuple[Any, ...], templates: object) -> tuple[Any, ...]:
    """Insert the proposals extract after the workspace read when a terminal fans out over it."""

    if f"$steps.{PROPOSALS_ALIAS}." not in json_text(templates):
        return nodes
    source, *rest = nodes
    return (
        source.model_copy(update={"next": "items"}),
        proposals_extract(next_node=source.next),
        *rest,
    )


def severity_from_document(nodes: tuple[Any, ...]) -> tuple[Any, ...]:
    """Read the shaped severity out of the JSON document, through a whole-document extract.

    The document may then change anywhere else while its severity stays.
    """

    from cruxible_client.contracts.procedures.extract import ExtractNode
    from cruxible_client.contracts.shapes import contract_digest

    row = document_contract()
    source, shape, *rest = nodes
    extract = ExtractNode.model_validate(
        {
            "kind": "extract",
            "node_id": "document_read",
            "capture": f"$steps.{SOURCE_ALIAS}.capture",
            "records": "/",
            "row": {
                "name": row.name,
                "version": row.version,
                "digest": contract_digest(row).tagged,
            },
            "fields": {"severity": {"at": "severity"}, "advisory_id": {"at": "advisory_id"}},
            "as": DOCUMENT_ALIAS,
            "next": shape.node_id,
        }
    )
    return (
        source.model_copy(update={"next": extract.node_id}),
        extract,
        shape.model_copy(
            update={"fields": {"severity": f"$steps.{DOCUMENT_ALIAS}.rows.0.severity"}}
        ),
        *rest,
    )


def json_text(value: object) -> str:
    import json

    return json.dumps(value, default=lambda item: item.model_dump(mode="json"))


def _policy(
    *, name: str = "default", overrides: dict[str, CanonicalDuration | None] | None = None
) -> AcquisitionDefault:
    del name  # Acquisition defaults are inline data now, not a named artifact.
    return AcquisitionDefault(max_age=CanonicalDuration(microseconds=3_600_000_000))


def _world(  # noqa: PLR0913
    tmp_path: Path,
    *,
    contents: bytes | None = None,
    contract: CaptureContract | None = None,
    policy: AcquisitionDefault | None = None,
    accept_procedure: bool = True,
    relative_path: str = RELATIVE_PATH,
    write_at: str = RELATIVE_PATH,
    document: bool = False,
):  # type: ignore[no-untyped-def]
    """The Source world; ``document`` reads the severity out of a JSON document."""

    if document and contents is None:
        contents = canonical_bytes({"severity": ADVISORY.decode()})
    instance, owner = initialize_local(tmp_path)
    # `initialize_local` attaches exactly this root to the instance.
    workspace_root = tmp_path / "workspace"
    target = workspace_root / write_at
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ADVISORY if contents is None else contents)

    accepted_contract = capture_contract() if contract is None else contract
    accepted_policy = _policy() if policy is None else policy
    procedure = _procedure(
        instance,
        root=workspace_root,
        contract=accepted_contract,
        relative_path=relative_path,
        acquisition_defaults={SOURCE_ALIAS: accepted_policy},
    )
    if document:
        definition = procedure.definition.model_copy(
            update={"nodes": severity_from_document(procedure.definition.nodes)}
        )
        from tests.support.procedures import revalidated

        procedure = revalidated(
            procedure.model_copy(
                update={
                    "definition": definition,
                    "definition_digest": compute_procedure_definition_digest(definition).tagged,
                }
            )
        )
    members = {
        capture_contract_path(accepted_contract.identity.name): render_capture_contract(
            accepted_contract
        ),
        **fan_out_members(),
    }
    if accept_procedure:
        members[procedure_path(PROCEDURE_NAME)] = render_procedure(procedure)
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
            request=ProcedureRunRequest(input={}, evaluation_time=evaluation_time),
            actor_context=_actor(instance).model_copy(update={"timestamp": evaluation_time}),
            provider_runtime_operator=_Operator(spawned),  # type: ignore[arg-type]
            workspace_file_reader=_reader(instance, root) if reader is None else reader,
        ),
        spawned,
    )


# --- law ---------------------------------------------------------------------


def test_source_is_served_on_the_direct_lane_and_terminals_only_on_a_line() -> None:
    """A Source reads; it runs on its own. Every terminal waits for a Line."""

    assert "call" in DIRECT_NODE_KINDS
    # Terminals and post_inbox stay dark on the direct lane.
    assert DIRECT_NODE_KINDS.isdisjoint({"emit_capture", "post_inbox", "propose_change_set"})
    assert {"emit_capture", "propose_change_set"} <= LINE_NODE_KINDS


def test_the_sdk_authors_a_source_node(tmp_path: Path) -> None:
    """The SDK authors the same Source graph the run lane executes."""

    from cruxible_client.authoring.sdk import Cruxible, ProcedureDraft

    _instance, _owner, procedure, _root, _policy_artifact = _world(tmp_path)
    draft = Cruxible.procedure(
        object(),
        definition=_sdk_procedure_input(procedure),
    )
    assert isinstance(draft, ProcedureDraft)


# --- the served direct lane --------------------------------------------------


def test_readiness_reports_a_source_procedure_directly_runnable(tmp_path: Path) -> None:
    instance, _owner, _procedure, _root, _policy_artifact = _world(tmp_path)

    readiness = service_playbill_procedure_readiness(
        instance,
        name=PROCEDURE_NAME,
        request=ProcedureReadinessRequestV1(),
    )

    assert readiness.runnable == "direct"
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
        "sha256:" + hashlib.sha256(ADVISORY).hexdigest()
    )
    # The Source was authored without a binding digest: the one recorded is the
    # daemon's binding of the attached root to this instance.
    assert observation.source_read_receipt.workspace_binding_digest == workspace_binding_digest(
        instance_id=instance.descriptor.instance_id, canonical_root=root.resolve()
    )
    assert observation.capture_digest is not None
    from cruxible_client.contracts.procedures.windows import CaptureEventSelector
    from cruxible_core.service.procedures.resolution_contracts import capture_event_time

    event = next(
        item.capture_event for item in state.outcomes if item.event_kind == "produced_capture"
    )
    assert event is not None and event.run_id == state.run_id
    capture = capture_contract()
    assert (
        capture_event_time(
            instance,
            CaptureEventSelector(
                capture_contract_identity=capture.identity,
                capture_contract_digest=capture_contract_digest(capture).tagged,
            ),
            event,
            now=NOW,
        )
        == NOW
    )
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
    assert policy_artifact.max_age is not None


def test_a_changed_file_yields_a_different_receipt_and_a_different_value(
    tmp_path: Path,
) -> None:
    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)

    first, _invoker = _run(instance, root)
    assert first.status == "succeeded"

    changed = b"critical"
    (root / RELATIVE_PATH).write_bytes(changed)

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
    instance: PlaybillInstance,
    procedure: ProcedureArtifact,
    policy: AcquisitionDefault,
) -> LineSpec:
    """A Line over the same Source Procedure the direct lane runs."""

    from cruxible_client.contracts.provider_operations import LineBinding, binding_pin

    procedure_pin = ArtifactPin(
        role="procedure",
        target=procedure.identity,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    provider = parse_provider(
        tree[provider_path(WORKSPACE_FILE_BUILTIN_PROVIDER_ID)],
        path=provider_path(WORKSPACE_FILE_BUILTIN_PROVIDER_ID),
    )
    caps = procedure.definition.hard_caps
    line = LineSpec(
        identity=ArtifactIdentity(kind="Line", name=LINE_NAME),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={},
        bindings={
            "read": LineBinding(
                build=binding_pin(provider.identity, provider_digest(provider).tagged),
                operation=WORKSPACE_FILE_OPERATION,
            )
        },
        acquisition={SOURCE_ALIAS: policy},
        max_authority="observe",
        budgets={
            "max_capture_bytes": caps.max_capture_bytes,
            "max_items": caps.max_items,
            "max_provider_calls": caps.max_provider_calls,
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
        },
        epsilon={"$decimal": "0.1"},
    )
    return line


def _line_mandate(line: LineSpec, procedure: ProcedureArtifact) -> ProcedureMandate:
    return ProcedureMandate(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="advisory-line-mandate"),
        line=ArtifactPin(
            role="line",
            target=line.identity,
            artifact_digest=line_spec_digest(line).tagged,
        ),
        grants="propose",
        resource_ceiling=procedure.definition.hard_caps,
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


def _line_world(tmp_path: Path, **kwargs):  # type: ignore[no-untyped-def]
    """The direct lane's world, plus the accepted Line that triggers the same graph."""

    instance, owner, procedure, root, policy_artifact = _world(tmp_path, **kwargs)
    line = _served_line(instance, procedure, policy_artifact)
    mandate = _line_mandate(line, procedure)
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
            request=LineRunRequest(
                line=identity_digest,
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
        ("Data/.CruxIble/keys.json", RELATIVE_PATH, "playbill_control"),
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


def _read_refusal(state) -> ProcedureNodeRefusal:  # type: ignore[no-untyped-def]
    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusal)
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
        tmp_path, relative_path="Data/.CruxIble/keys.json"
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
    instance, _owner, _procedure, root, _policy_artifact = _world(
        tmp_path, contract=small, contents=b"well over sixteen bytes of content"
    )

    state, invoker = _run(instance, root)

    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusal)
    assert state.terminal.code == "workspace_file_read_refused"
    assert state.terminal.details["path_class"] == "size_budget"
    assert invoker.spawn_calls == 0


def test_a_run_without_a_daemon_reader_refuses_typed(tmp_path: Path) -> None:
    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)
    invoker = _WorkspaceInvoker()

    state = service_run_playbill_procedure(
        instance,
        name=PROCEDURE_NAME,
        request=ProcedureRunRequest(input={}, evaluation_time=NOW),
        actor_context=_actor(instance),
        provider_runtime_operator=_Operator(invoker),  # type: ignore[arg-type]
        workspace_file_reader=None,
    )

    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusal)
    assert state.terminal.code == "workspace_file_read_refused"
    assert state.terminal.details["path_class"] == "binding"
    assert invoker.spawn_calls == 0


def test_two_authorized_workspaces_and_no_name_refuse_listing_them(tmp_path: Path) -> None:
    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)
    other = tmp_path / "other-workspace"
    other.mkdir()
    reader = WorkspaceFileReader(
        instance_id=instance.descriptor.instance_id,
        operating_profile="local",
        attached_roots=(),
        operational_allowed_roots=(root, other),
        managed_roots=(instance.root,),
    )

    state, invoker = _run(instance, root, reader=reader)

    refusal = _read_refusal(state)
    assert refusal.details["path_class"] == "workspace_unnamed"
    assert refusal.details["workspace_names"] == sorted([root.name, other.name])
    assert invoker.spawn_calls == 0


class _DecliningInvoker(_WorkspaceInvoker):
    """The governed read happens; the Provider then declines the material."""

    def invoke_provider(self, *, occurrence, context, invocation_id, bound, deadline):  # type: ignore[no-untyped-def]
        self.spawn_calls += 1
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelope(
                protocol_version="2.0",
                run_id=context.run_id,
                status="refused",
                refusal=ProviderRuntimeRefusal(
                    code="provider_declined",
                    message="the adapter declined this material",
                ),
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=bound.binding,
        )


def test_a_required_rule_refuses_the_same_declined_read_at_the_source_node(
    tmp_path: Path,
) -> None:
    """The other direction of the same execution-time application.

    An operation's declared ``refusals`` are its complete domain vocabulary
    (F10): any other code is ``undeclared_refusal``, never a pass-through. The
    genesis-seeded ``workspace.file`` operation
    (`governance/seed_artifacts/workspace_file.py:workspace_file_operation`)
    declares the built-in's (`providers/builtin_workspace_file.py`) own typed
    refusals -- ``provider_declined``/``invalid_parameter``/
    ``mismatched_lengths`` (its oversized-file and length/digest-mismatch
    guards) -- so they surface as ``operation_refused`` node refusals naming
    the operation's code.
    """

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)

    state, invoker = _run(instance, root, invoker=_DecliningInvoker())

    assert invoker.spawn_calls == 1
    assert state.status == "node_refused", state.terminal
    assert (state.terminal.code, state.terminal.detail_code) == (
        "operation_refused",
        "provider_declined",
    )


class _UnreachableInvoker(_WorkspaceInvoker):
    """The governed read happens; the Provider then cannot reach its target."""

    def invoke_provider(self, *, occurrence, context, invocation_id, bound, deadline):  # type: ignore[no-untyped-def]
        self.spawn_calls += 1
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelope(
                protocol_version="2.0",
                run_id=context.run_id,
                status="error",
                error=ProviderRuntimeProviderErrorPayload(
                    kind="ConnectError",
                    message=(
                        "[Errno 8] nodename nor servname provided: "
                        "https://reader:s3cret@advisories.invalid/osv.json" + " (retrying)" * 200
                    ),
                ),
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=bound.binding,
        )


def test_a_provider_error_reaches_the_run_state_with_its_cause(tmp_path: Path) -> None:
    """A failed run names the node, the Provider's kind and its own message.

    The message keeps what tells a DNS failure from an HTTP status, loses any
    URL credential, and is capped; the declared repair says what to do with it.
    """

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)

    state, invoker = _run(instance, root, invoker=_UnreachableInvoker())

    assert invoker.spawn_calls == 1
    assert state.status == "operational_failed", state.terminal
    assert isinstance(state.terminal, ProcedureOperationalFailure)
    assert state.terminal.code == "provider_execution_error"
    cause = state.terminal.cause
    assert cause is not None
    assert (cause.node_id, cause.provider_code, cause.kind) == (
        "read",
        "provider_execution_error",
        "ConnectError",
    )
    assert state.terminal.last_node_id == "read"
    assert cause.message.startswith(
        "[Errno 8] nodename nor servname provided: https://advisories.invalid/osv.json"
    )
    assert "s3cret" not in cause.message and "reader" not in cause.message
    assert len(cause.message) == FAILURE_CAUSE_MESSAGE_LIMIT
    assert isinstance(state.terminal.repair, HandEditRepair)
    assert "transport error" in state.terminal.repair.hand_edit.required_change


def test_a_failure_cause_strips_userinfo_from_every_url() -> None:
    assert strip_url_userinfo(
        "GET https://u:p@a.example/x then git+ssh://git@b.example/r and mailto:x@y"
    ) == ("GET https://a.example/x then git+ssh://b.example/r and mailto:x@y")


def _acquisition_decisions(instance: PlaybillInstance) -> list[tuple[str, str]]:
    """Every declared failure behaviour the RUN applied, straight off the journal."""

    import cruxible_core.service.procedures.procedure_runs as service

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


def test_a_direct_run_reaches_a_terminal_under_an_ephemeral_propose_capped_line(
    tmp_path: Path,
) -> None:
    """C4, as the F01 fix-round changed it: a direct run is no longer blocked
    at admission just for having a terminal (B2 rev 4.1 restated C4 as capped
    at propose under an ephemeral Line, not refused outright -- see the F01
    fix-round commit `7f0bf1cc3`, "direct runs reach their terminal under an
    ephemeral Line, capped at propose"). Readiness still reports the Procedure
    as Line-runnable (the terminal is still an unsupported *readiness* node for
    the direct lane), but admission itself now proceeds: the Source still runs
    and the terminal is reached, refusing or proposing on its own terms rather
    than being pre-empted.
    """

    from cruxible_client.contracts.procedures.models import CaptureEgressNode

    instance, owner, procedure, root, _policy_artifact = _world(tmp_path, accept_procedure=False)
    source = procedure.definition.nodes[0]
    assert isinstance(source, CallNode)
    definition = procedure.definition.model_copy(
        update={
            "nodes": (
                source.model_copy(update={"next": "emit"}),
                CaptureEgressNode(
                    node_id="emit",
                    capture_contract=source.captures["capture"],
                    input=f"$steps.{SOURCE_ALIAS}",
                    result=f"$steps.{SOURCE_ALIAS}",
                ),
            ),
            "returns": SOURCE_ALIAS,
        }
    )
    with_terminal = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
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
        request=ProcedureReadinessRequestV1(),
    )
    assert [row.kind for row in readiness.unsupported_nodes] == ["emit_capture"]
    assert readiness.runnable == "line"

    state, invoker = _run(instance, root)

    # Admission is no longer the lane that stops this run: the Source runs
    # (the Provider is spawned) and the terminal itself is reached. This
    # fixture's CaptureContract does not permit CAS storage, so the terminal
    # node refuses on that unrelated ground -- not because the direct lane
    # pre-empted it.
    assert state.status != "admission_refused", state.terminal
    assert state.run_id is not None
    assert invoker.spawn_calls == 1
    assert journal_root.exists()
    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusal)
    assert state.terminal.node_id == "emit"
    assert state.terminal.code == "terminal_egress_unverified"


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


def _admission(instance: PlaybillInstance, state):  # type: ignore[no-untyped-def]
    """The V9 admission this run bound, read back off its own journal."""

    import cruxible_core.service.procedures.procedure_runs as service

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
            return ProcedureAdmissionBoundPayload.model_validate(payload).admission
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

    import cruxible_core.service.procedures.procedure_runs as service

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)
    real_execute = service.service_execute_direct_procedure

    def without_contracts(*args, **kwargs):  # type: ignore[no-untyped-def]
        return real_execute(*args, **{**kwargs, "capture_contracts": {}})

    monkeypatch.setattr(service, "service_execute_direct_procedure", without_contracts)
    invoker = _WorkspaceInvoker()
    with pytest.raises(ExecutionError) as excinfo:
        _run(instance, root, invoker=invoker)

    assert "source_acquisition_plan_mismatch" in str(excinfo.value)
    assert invoker.spawn_calls == 0


def test_an_external_mutation_occurrence_refuses_before_the_first_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Source occurrence is a READ; its slot's operation declares ``pure``.

    ``effect_class`` comes from the planned occurrence, so the only way to
    reach the guard is to plan the same occurrence with a mutating class. The
    mismatch is caught by ``ProcedureAcquisitionPlan``'s own shape invariant
    (``src/cruxible_core/.../results.py``'s "external occurrence effect
    differs from its operation"), a bare pydantic ``ValidationError`` at the
    ``ProcedureAcquisitionPlan(...)`` construction in
    `_plan_direct_external_run`
    (`src/cruxible_core/service/procedures/procedure_runs.py`), which sits
    after the function's only ``except ExecutionError`` (that earlier guard
    covers only the `_plan_external_occurrences` call). That construction is
    now wrapped in its own ``except ValidationError``, surfacing a typed
    ``artifact_binding_mismatch`` admission refusal instead of letting the
    bare ``ValidationError`` escape.
    """

    import cruxible_core.service.procedures.procedure_runs as service

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)
    real_plan = service._plan_external_occurrences  # noqa: SLF001

    def mutating(*args, **kwargs):  # type: ignore[no-untyped-def]
        return tuple(
            item.model_copy(update={"effect_class": "external_mutation"})
            for item in real_plan(*args, **kwargs)
        )

    monkeypatch.setattr(service, "_plan_external_occurrences", mutating)
    invoker = _WorkspaceInvoker()

    result, _spawned = _run(instance, root, invoker=invoker)

    assert result.status == "admission_refused"
    assert isinstance(result.terminal, ProcedureAdmissionRefusal)
    assert result.terminal.code == "artifact_binding_mismatch"
    assert "external occurrence effect differs from its operation" in str(
        result.terminal.details["reason"]  # type: ignore[index]
    )
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

    from cruxible_core.service.procedures.procedure_runs import ProcedureRunRecoveryRequired

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)

    class _Exploding(_WorkspaceInvoker):
        def invoke_provider(self, **kwargs):  # type: ignore[no-untyped-def]
            self.spawn_calls += 1
            raise RuntimeError("simulated daemon crash after the governed read")

    with pytest.raises(ExecutionError) as crash:
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
        "sha256:" + hashlib.sha256(ADVISORY).hexdigest()
    )
    assert observation.capture_digest is None


def _admitted_run_ids(instance: PlaybillInstance) -> list[str]:
    """Every run id this instance's procedure journal bound."""

    import cruxible_core.service.procedures.procedure_runs as service

    journal, _root = service._journal(instance)  # noqa: SLF001
    stream = service._stream(instance)  # noqa: SLF001
    return [
        stored.record.run_id
        for partition_id in journal.partition_ids(stream)
        for stored in journal.all_records(stream, partition_id)
        if stored.record.event_kind == "admission_bound" and stored.record.run_id is not None
    ]


def _admitted_run_id(instance: PlaybillInstance) -> str:
    """The one run id this instance's procedure journal bound."""

    run_ids = _admitted_run_ids(instance)
    assert len(run_ids) == 1, run_ids
    return run_ids[0]


def test_the_admitted_plan_mismatch_code_stays_served(tmp_path: Path) -> None:
    """The node-level mismatch code an admitted-then-drifted plan raises is served."""

    from cruxible_client.contracts.procedures.results import ProcedureNodeRefusalCode

    assert "provider_acquisition_plan_mismatch" in get_args(ProcedureNodeRefusalCode)
    assert "workspace_file_read_refused" in get_args(ProcedureNodeRefusalCode)


def test_the_authoring_path_produces_the_exact_artifact_the_run_lane_executes(
    tmp_path: Path,
) -> None:
    """One lowering, one artifact: what authoring lowers is what the lane runs.

    The served surfaces -- SDK, HTTP, MCP and CLI -- all submit the same
    authoring intent into the one coordinator, so proving the coordinator lowers
    this Source Procedure into the exact accepted artifact the run lane
    executed above is what "the same definition gives the same digest" means.
    """

    from cruxible_client.authoring.sdk import Cruxible
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.preflight import compute_preflight
    from cruxible_core.proposals.proposals import AuthenticatedActor

    instance, _owner, procedure, _root, _policy_artifact = _world(tmp_path, accept_procedure=False)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")

    # Authoring never supplies an exact digest: it names the accepted artifact
    # and the owned Contract, and lowering resolves both. That is what makes
    # the digest below an identity claim rather than an echo.
    compiled = coordinator.compile(
        actor=actor,
        payload=Cruxible.procedure(object(), definition=_sdk_procedure_input(procedure)).payload,
        canonical_timestamp=ACCEPT_STAMP,
    )

    assert compiled.verdict == "passed", compiled.frontier.diagnostics
    intent = coordinator.list_pending(actor=actor).intents[0]
    lowered = compute_preflight(instance, intent=intent, actor=actor).lowered
    assert lowered is not None
    assert lowered.resolved_authoring["artifact_digest"] == (
        procedure_artifact_digest(procedure).tagged
    )
    assert lowered.resolved_authoring["definition"]["graph_format"] == 7
    assert lowered.proposed_tree[procedure_path(PROCEDURE_NAME)] == render_procedure(procedure)


def _authored_definition(procedure: ProcedureArtifact) -> dict[str, object]:
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


def _sdk_procedure_input(procedure: ProcedureArtifact) -> ProcedureInput:
    return ProcedureInput(
        kind="procedure",
        definition=_authored_definition(procedure),
        activation_policy=procedure.activation_policy,
        contracts=tuple(
            CarriedContractInput(
                name=contract.identity.name,
                description=contract.contract_schema.description,
                fields=contract.contract_schema.fields,
                allow_extra=contract.contract_schema.allow_extra,
            )
            for contract in procedure.owned_contracts
        ),
        acquisition_defaults=dict(procedure.acquisition_defaults),
    )


def test_a_line_over_a_source_procedure_authors_without_an_acquisition_override(
    tmp_path: Path,
) -> None:
    """A Line authored with no `acquisition` override still lowers and binds its slot.

    Acquisition rules resolve per run from the Procedure's `acquisition_defaults`
    and the Line's `acquisition` override (C2/F05); a Line need not declare an
    override to run -- there is no separate pinned policy artifact to require.
    """

    from cruxible_client.contracts.authoring.models import LineAuthoringPayload
    from cruxible_core.authoring import lowering
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor

    instance, _owner, _procedure, _root, _policy_artifact = _world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    intent = coordinator.create(
        actor=actor,
        payload=LineAuthoringPayload(
            name="source-line",
            procedure_name=PROCEDURE_NAME,
        ),
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    ).intent
    from cruxible_client.contracts.procedures.line_specs import parse_line_spec

    lowered = lowering.lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    line = parse_line_spec(
        lowered.proposed_tree[line_spec_path("source-line")], path=line_spec_path("source-line")
    )
    assert line.acquisition == {}
    assert "read" in line.bindings


def test_a_direct_run_without_an_instant_evaluates_when_it_is_admitted(tmp_path: Path) -> None:
    """The operator's invocation is the trigger: its Capture is observed at the read.

    It used to evaluate at the accepted head's time, so the Capture predated the
    read and no capture-landing Trigger accepted after that head ever fired.
    """

    instance, _owner, _procedure, root, _policy_artifact = _world(tmp_path)
    admitted = NOW + timedelta(hours=3)
    assert instance.accepted_evaluation_time(instance.accepted_coordinate().git_oid) < admitted

    state = service_run_playbill_procedure(
        instance,
        name=PROCEDURE_NAME,
        request=ProcedureRunRequest(input={}),
        actor_context=_actor(instance),
        provider_runtime_operator=_Operator(_WorkspaceInvoker()),  # type: ignore[arg-type]
        workspace_file_reader=_reader(instance, root),
        daemon_clock=_TestClock(admitted),
    )

    assert state.status == "succeeded", state.terminal
    assert state.evaluation_time == admitted
    (observation,) = state.source_observations
    assert observation.capture_digest is not None
    envelope = parse_capture_envelope(
        instance.body_store().read(
            observation.capture_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert envelope.observed_at == admitted
