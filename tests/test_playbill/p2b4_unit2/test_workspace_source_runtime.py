from __future__ import annotations

import base64
from pathlib import Path

import pytest
from tests.test_playbill.p2b4_unit1.test_source_v4_runtime import _source_fixture
from tests.test_playbill.test_procedure_execution import _Authority, _Contracts

from cruxible_client.contracts.canonical import CanonicalValue, canonical_bytes
from cruxible_client.contracts.captures import (
    CaptureFormatError,
    capture_contract_digest,
    verify_capture,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.models import SourceNodeV4
from cruxible_client.contracts.procedures.results import (
    ProcedureAcquisitionPlanV2,
    procedure_acquisition_plan_digest,
)
from cruxible_client.contracts.provider_execution import (
    ProviderEgressObservationV1,
    ProviderExternalOccurrencePlanV1,
    ProviderSecretResolutionPlanV1,
)
from cruxible_client.contracts.workspace_file import (
    SourceReadReceiptV1,
    WorkspaceFileSourceRequestV1,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import parse_journal_payload
from cruxible_core.playbill.procedures.execution import (
    PreparedProcedureRunV5,
    ProcedureExecutor,
    ProcedureRunAdmissionV5,
    procedure_admission_digest,
    procedure_line_run_id,
    procedure_node_pin_sets,
    procedure_pin_set_digest,
    procedure_semantic_replay_key_digest,
)
from cruxible_core.playbill.producer_receipts import journal_producer_receipt_resolver
from cruxible_core.playbill.provider_local_runtime import (
    BoundLocalProviderV1,
    ProviderDriverOutcomeV1,
)
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeResultEnvelopeV1
from cruxible_core.playbill.workspace_file import WorkspaceFileReader, workspace_binding_digest


class _WorkspaceClassifier:
    def classify(self, canonical_input: CanonicalValue) -> str:
        assert isinstance(canonical_input, dict)
        return "content_kind=text;byte_size=tiny"


class _WorkspaceRegistry:
    def require(self, _classifier_digest: str) -> _WorkspaceClassifier:
        return _WorkspaceClassifier()


class _WorkspaceInvoker:
    def __init__(self) -> None:
        self.bind_calls = 0
        self.spawn_calls = 0
        self.input: object | None = None

    def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
        self.bind_calls += 1
        return BoundLocalProviderV1(
            binding=occurrence.local_execution,
            interpreter_path=Path("/portable/workspace-provider"),
        )

    def invoke_provider(  # type: ignore[no-untyped-def]
        self, *, occurrence, context, invocation_id, bound
    ) -> ProviderDriverOutcomeV1:
        self.spawn_calls += 1
        self.input = context.input
        assert isinstance(context.input, dict)
        assert set(context.input) == {
            "logical_source",
            "commitment_digest",
            "content_encoding",
            "bytes",
            "byte_length",
            "bytes_digest",
        }
        assert base64.b64decode(context.input["bytes"], validate=True) == b"hello\n"
        output = {
            "input_bucket": context.input_bucket,
            "source": {
                "logical_source": context.input["logical_source"],
                "commitment_digest": context.input["commitment_digest"],
                "bytes_digest": context.input["bytes_digest"],
                "byte_length": context.input["byte_length"],
            },
            "content": {
                "kind": "text",
                "encoding": "utf-8",
                "text": "hello\n",
                "lines": ["hello"],
            },
        }
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0",
                run_id=context.run_id,
                status="ok",
                output=output,
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservationV1(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=bound.binding,
        )


def _workspace_source_fixture(tmp_path: Path, relative_path: str):  # type: ignore[no-untyped-def]
    accepted, prepared, fixture, policy, contract = _source_fixture(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    request = WorkspaceFileSourceRequestV1(
        logical_source="commerce.production.orders",
        workspace_binding_digest=workspace_binding_digest(
            instance_id=prepared.admission.instance_id,
            canonical_root=root.resolve(),
        ),
        relative_path=relative_path,
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "workspace"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
    )
    old_node = accepted.procedure.definition.nodes[0]
    assert isinstance(old_node, SourceNodeV4)
    source_node = old_node.model_copy(update={"request": request.model_dump(mode="json")})
    definition = accepted.procedure.definition.model_copy(update={"nodes": (source_node,)})
    procedure = accepted.procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
        }
    )
    accepted = AcceptedProcedureV1(
        path=accepted.path,
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )

    old_occurrence = prepared.acquisition_plan.external_occurrences[0]
    local = old_occurrence.local_execution.model_copy(
        update={"interface_id": "workspace.file", "declared_endpoints": ()}
    )
    occurrence = ProviderExternalOccurrencePlanV1.model_validate(
        {
            **old_occurrence.model_dump(mode="python"),
            "interface_id": "workspace.file",
            "accepted_bucket_selectors": ("content_kind=text;byte_size=tiny",),
            "effect_class": "none",
            "local_execution": local,
            "secret_plan": ProviderSecretResolutionPlanV1(
                references=(), binding_identity_digests=()
            ),
        }
    )
    plan = ProcedureAcquisitionPlanV2.model_validate(
        {
            **prepared.acquisition_plan.model_dump(mode="python"),
            "external_occurrences": (occurrence,),
        }
    )
    plan_digest = procedure_acquisition_plan_digest(plan)
    node_pin_sets = procedure_node_pin_sets(accepted)
    provisional = prepared.admission.model_copy(
        update={
            "procedure_artifact_digest": accepted.artifact_digest,
            "definition_digest": accepted.procedure.definition_digest,
            "node_pin_sets": node_pin_sets,
            "pin_set_digest": procedure_pin_set_digest(accepted.procedure.pins, node_pin_sets),
            "acquisition_plan_digest": plan_digest,
            "semantic_replay_key_digest": "sha256:" + "0" * 64,
            "admission_binding_digest": "sha256:" + "0" * 64,
            "run_id": "RUN-" + "0" * 64,
        }
    )
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
    prepared = PreparedProcedureRunV5.model_validate(
        {
            **prepared.model_dump(mode="python"),
            "admission": admission,
            "acquisition_plan": plan,
            "acquisition_plan_digest": plan_digest,
        }
    )
    reader = WorkspaceFileReader(
        instance_id=admission.instance_id,
        operating_profile="local",
        attached_roots=(root,),
        managed_roots=(tmp_path / "managed",),
    )
    return accepted, prepared, fixture, policy, contract, root, reader


def _execute(tmp_path: Path, relative_path: str):  # type: ignore[no-untyped-def]
    accepted, prepared, fixture, policy, contract, root, reader = _workspace_source_fixture(
        tmp_path, relative_path
    )
    if relative_path == "docs/note.txt":
        (root / "docs").mkdir()
        (root / "docs" / "note.txt").write_bytes(b"hello\n")
    invoker = _WorkspaceInvoker()
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        provider_classifier_registry=_WorkspaceRegistry(),  # type: ignore[arg-type]
        capture_contracts={capture_contract_digest(contract).tagged: contract},
        acquisition_policy=policy,
        workspace_file_reader=reader,
    ).execute(prepared, accepted)
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    access = BodyAccessContext(principal_id="unit-test", can_read_body=True)
    payloads = [
        parse_journal_payload(fixture.bodies.read(item.record.payload_digest, access=access))
        for item in records
    ]
    return result, invoker, records, payloads, root, fixture, contract, prepared, accepted


def test_workspace_source_reads_before_spawn_and_commits_both_receipts(tmp_path: Path) -> None:
    result, invoker, records, payloads, _root, fixture, contract, prepared, accepted = _execute(
        tmp_path, "docs/note.txt"
    )
    if result.status != "succeeded":
        raise AssertionError(result)
    assert invoker.bind_calls == invoker.spawn_calls == 1
    kinds = [item.record.event_kind for item in records]
    assert (
        kinds.index("source_request_derived")
        < kinds.index("source_read")
        < kinds.index("provider_invocation_started")
    )
    source_read = SourceReadReceiptV1.model_validate(
        payloads[kinds.index("source_read")]["receipt"]
    )
    completed = payloads[kinds.index("provider_invocation_completed")]["receipt"]
    produced = payloads[kinds.index("produced_capture")]
    assert source_read.provider_input_digest == completed["input_digest"]
    assert (
        produced["invocation_receipt_digest"]
        == payloads[kinds.index("provider_invocation_completed")]["receipt_digest"]
    )
    source_node = accepted.procedure.definition.nodes[0]
    assert isinstance(source_node, SourceNodeV4)
    verified = verify_capture(
        produced["capture_digest"],
        store=fixture.bodies,
        contract=contract,
        producer_artifact_digests={
            source_node.provider.target.qualified: completed["provider_artifact_digest"]
        },
        producer_receipt_resolver=journal_producer_receipt_resolver(
            journal=fixture.journal,
            instance_id=prepared.admission.instance_id,
            bodies=fixture.bodies,
        ),
    )
    assert (
        verified.production_evidence.source_read_receipt_digest
        == payloads[kinds.index("source_read")]["receipt_digest"]
    )

    source_read_payload_digest = records[kinds.index("source_read")].record.payload_digest

    class _ForgedReadBodies:
        def read(self, digest: str, *, access: BodyAccessContext) -> bytes:
            content = fixture.bodies.read(digest, access=access)
            if digest != source_read_payload_digest:
                return content
            payload = parse_journal_payload(content)
            forged = SourceReadReceiptV1.model_validate(payload["receipt"]).model_copy(
                update={"relative_path": "docs/other.txt"}
            )
            from cruxible_client.contracts.workspace_file import source_read_receipt_digest

            return canonical_bytes(
                {
                    "receipt": forged.model_dump(mode="json"),
                    "receipt_digest": source_read_receipt_digest(forged),
                }
            )

    with pytest.raises(CaptureFormatError, match="journal is invalid"):
        verify_capture(
            produced["capture_digest"],
            store=fixture.bodies,
            contract=contract,
            producer_artifact_digests={
                source_node.provider.target.qualified: completed["provider_artifact_digest"]
            },
            producer_receipt_resolver=journal_producer_receipt_resolver(
                journal=fixture.journal,
                instance_id=prepared.admission.instance_id,
                bodies=_ForgedReadBodies(),  # type: ignore[arg-type]
            ),
        )


def test_forbidden_workspace_path_refuses_before_provider_bind_or_spawn(tmp_path: Path) -> None:
    accepted, prepared, fixture, policy, contract, root, reader = _workspace_source_fixture(
        tmp_path, ".git/config"
    )
    (root / ".git").mkdir()
    (root / ".git" / "config").write_bytes(b"secret")
    invoker = _WorkspaceInvoker()
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        provider_classifier_registry=_WorkspaceRegistry(),  # type: ignore[arg-type]
        capture_contracts={capture_contract_digest(contract).tagged: contract},
        acquisition_policy=policy,
        workspace_file_reader=reader,
    ).execute(prepared, accepted)
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "workspace_file_read_refused"
    assert result.refusal.detail_code == "git_metadata"
    assert (invoker.bind_calls, invoker.spawn_calls) == (0, 0)
