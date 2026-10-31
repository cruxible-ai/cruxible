"""Source convergence self-attacks on B2's landed execution carrier."""

from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import cruxible_core.service.procedures.procedure_runs as procedure_run_service
from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    CanonicalDuration,
    CaptureContract,
    CaptureEnvelope,
    CaptureFormatError,
    ProviderInvocationCaptureEvidence,
    ProviderProducerReceiptResolution,
    ProviderResultToExternalCapture,
    SourceEffectiveTime,
    capture_contract_digest,
    capture_digest,
    parse_capture_envelope,
    render_capture_envelope,
    verify_capture,
)
from cruxible_client.contracts.errors import (
    CasError,
    ExecutionError,
    JournalError,
)
from cruxible_client.contracts.procedures.acquisition_rules import (
    AcquisitionDefault,
    acquisition_rules_digest,
    resolve_acquisition_rules,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import (
    CallNode,
    CaptureEgressNode,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAcquisitionPlan,
    ProcedureRunReceipt,
    procedure_acquisition_plan_digest,
    procedure_selection_decision_digest,
)
from cruxible_client.contracts.provider_execution import (
    ProcedureDerivedSourceRequest,
    ProviderEgressObservation,
    ProviderExternalOccurrencePlan,
    ProviderInvocationCompleted,
    ProviderInvocationOutputDigest,
    ProviderInvocationStarted,
    ProviderSecretReference,
    provider_invocation_receipt_digest,
)
from cruxible_client.contracts.provider_operations import ProviderOperation
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    ProviderDistributionRef,
    ProviderImplementationManifest,
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    ProviderRuntimeManifest,
    provider_digest,
    provider_path,
)
from cruxible_client.contracts.shapes import Contract, contract_digest
from cruxible_core.exhaust import journal_payload_bytes, parse_journal_payload
from cruxible_core.exhaust.producer_receipts import (
    ProducerReceiptJournalNote,
    journal_producer_receipt_resolver,
)
from cruxible_core.procedures.egress import (
    CaptureTerminalEgressSink,
    TerminalEgressReceiptV2,
    compute_effective_rung,
)
from cruxible_core.procedures.execution import (
    PreparedProcedureRun,
    ProcedureExecutor,
    _source_capture_association_fields,
    admission_fields,
    procedure_node_pin_sets,
    procedure_pin_set_digest,
    seal_procedure_run_admission,
)
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from cruxible_core.providers.provider_local_runtime import (
    BoundLocalProviderV1,
    ProviderDriverOutcomeV1,
)
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeRefusal,
    ProviderRuntimeResultEnvelope,
)
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.material_reservations import (
    ProcedureMaterialReservationStore,
    ReservedCaptureStore,
)
from tests.core_support._pc_c_support import capture_contract, digest
from tests.support.procedures import MAX_WALL_CLOCK, RUN_WALL_CLOCK, named
from tests.test_procedures.test_procedure_execution import _Authority, _Contracts
from tests.test_providers.test_provider_invocation_journal import (
    _accepted_one_provider,
    _prepared_acquisition_run,
)

# F02: an external-read Source's provider output is checked against its own
# operation Contract (execution.py, "every operation output is checked
# against its own pinned Contract, call or Source ... alike"). There is no
# shortcut for a free-form envelope: the Contract below mirrors
# ProviderResultToExternalCapture's own fields exactly, closed, so this
# fixture's provider validates the same way a real external-read provider
# would.
_COORDINATE_CONTRACT = Contract.model_validate(
    {"name": "test.source-coordinate", "version": 1, "fields": {"lsn": {"type": "string"}}}
)
_SELECTOR_CONTRACT = Contract.model_validate(
    {
        "name": "test.source-selector",
        "version": 1,
        "fields": {"id": {"type": "int"}, "relation": {"type": "string"}},
    }
)
_EFFECTIVE_CONTRACT = Contract.model_validate(
    {
        "name": "test.source-effective",
        "version": 1,
        "fields": {
            "tag": {"type": "string"},
            "effective_from": {"type": "datetime"},
            "effective_until": {"type": "datetime"},
        },
    }
)


def _pinned_nested(contract: Contract) -> dict[str, Any]:
    """A nested-Contract field type, pinned by exact digest (required once accepted)."""

    return {
        "kind": "contract",
        "contract": {
            "name": contract.name,
            "version": contract.version,
            "digest": contract_digest(contract).tagged,
        },
    }


_ENVELOPE_CONTRACT = Contract.model_validate(
    {
        "name": "test.source-envelope",
        "version": 1,
        "fields": {
            "tag": {"type": "string"},
            "source_identity": {"type": "string"},
            "coordinate_type": {"type": "string"},
            "coordinate": {"type": _pinned_nested(_COORDINATE_CONTRACT)},
            "selector_type": {"type": "string"},
            "selector": {"type": _pinned_nested(_SELECTOR_CONTRACT)},
            "replayability": {"type": "string"},
            "content_encoding": {"type": "string"},
            "content_base64": {"type": "string"},
            "byte_length": {"type": "int"},
            "bytes_digest": {"type": "string"},
            "observed_at": {"type": "datetime"},
            "source_effective_time": {"type": _pinned_nested(_EFFECTIVE_CONTRACT)},
        },
    }
)
_VALUE_CONTRACT = Contract.model_validate(
    {"name": "test.source-value", "version": 1, "fields": {"value": {"type": "int"}}}
)
_SOURCE_CONTRACTS = (
    _VALUE_CONTRACT,
    _COORDINATE_CONTRACT,
    _SELECTOR_CONTRACT,
    _EFFECTIVE_CONTRACT,
    _ENVELOPE_CONTRACT,
)


def _source_contract_digest(ref: Any) -> str | None:
    for item in _SOURCE_CONTRACTS:
        if (ref.name, ref.version) == (item.name, item.version):
            return contract_digest(item).tagged
    return None


def source_contract_files() -> dict[str, bytes]:
    """Every Contract :func:`_source_provider`'s operation pins, for an accepted tree."""

    from cruxible_client.contracts.shapes import contract_path, render_contract

    return {
        contract_path(item.name, item.version): render_contract(item) for item in _SOURCE_CONTRACTS
    }


def _source_provider() -> AcceptedProvider:
    """A ``read`` operation: ``test.source-value@1 -> test.source-envelope@1`` (external_read)."""

    operation = ProviderOperation.model_validate(
        {
            "name": "read",
            "in": f"{_VALUE_CONTRACT.name}@{_VALUE_CONTRACT.version}",
            "out": f"{_ENVELOPE_CONTRACT.name}@{_ENVELOPE_CONTRACT.version}",
            "effect": "external_read",
            "entrypoint": "demo.runtime:Provider",
        }
    )
    manifest = ProviderRuntimeManifest(
        provider_id="source-demo-provider",
        distribution=ProviderDistributionRef(name="source-demo-provider", version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=(
            ProviderImplementationManifest(
                entrypoint="demo.runtime:Provider",
                backends=("local_env",),
                deterministic=True,
            ),
        ),
    )
    document = PackageRegistrationDocumentV1(
        schema_version=2, manifest=manifest, operations=(operation,)
    )
    build = document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name="source-demo-provider",
            version="1.0.0",
            filename="source_demo_provider-1.0.0-py3-none-any.whl",
            sha256="sha256:" + ("a" * 64),
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain="source-demo-provider",
        operations=document.pinned_operations(_source_contract_digest),
    )
    return AcceptedProvider(
        path=provider_path(build.identity.name),
        provider=build,
        artifact_digest=provider_digest(build).tagged,
    )


class _SourceInvoker:
    def __init__(
        self, *, observed_at, result_size: int = 3, coordinate_type="postgres-lsn-v1"
    ) -> None:  # type: ignore[no-untyped-def]
        self.observed_at = observed_at
        self.coordinate_type = coordinate_type
        self.result_size = result_size
        self.bind_calls = 0
        self.spawn_calls = 0
        self.coordinates = None

    def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
        self.bind_calls += 1
        return BoundLocalProviderV1(
            binding=occurrence.local_execution,
            interpreter_path=Path("/portable/test-provider"),
        )

    def invoke_provider(  # type: ignore[no-untyped-def]
        self, *, occurrence, context, invocation_id, bound, deadline
    ):
        self.spawn_calls += 1
        self.coordinates = context.coordinates
        assert context.input == {"value": 3}
        body = canonical_bytes({"size": self.result_size})
        output = ProviderResultToExternalCapture(
            source_identity="commerce.production.orders",
            coordinate_type=self.coordinate_type,
            coordinate={"lsn": "0/16B6C50"},
            selector_type="relation-primary-key-v1",
            selector={"id": self.result_size, "relation": "orders"},
            replayability="exact",
            content_base64=base64.b64encode(body).decode("ascii"),
            byte_length=len(body),
            bytes_digest="sha256:" + hashlib.sha256(body).hexdigest(),
            observed_at=self.observed_at,
            source_effective_time=SourceEffectiveTime(
                effective_from=self.observed_at,
                effective_until=self.observed_at + timedelta(microseconds=1),
            ),
        )
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelope(
                protocol_version="2.0",
                run_id=context.run_id,
                status="ok",
                output=output.model_dump(mode="json"),
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=bound.binding,
        )


def _acquisition_rules(policy: AcquisitionDefault, *, alias: str = "source_result.capture"):
    from cruxible_core.procedures.ports import ResolvedAcquisitionRules

    return ResolvedAcquisitionRules(
        rules=resolve_acquisition_rules((alias,), defaults={alias: policy}, overrides={})
    )


def _source_fixture(
    tmp_path: Path,
    *,
    include_terminal: bool = False,
    capture_budget: int | None = None,
    contract: CaptureContract | None = None,
    terminal_contract: CaptureContract | None = None,
) -> tuple[
    AcceptedProcedure,
    PreparedProcedureRun,
    object,
    AcquisitionDefault,
    CaptureContract,
]:
    # Runs on the real clock: a budget a loaded CI runner cannot run out.
    from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
    from cruxible_core.procedures.reference_binding import (
        authored_procedure,
        bind_procedure_references,
    )
    from tests.test_providers.test_provider_invocation_journal import SLOT as PROVIDER_SLOT

    historical_provider = _source_provider()
    provider_accepted = _accepted_one_provider(
        wall_clock=RUN_WALL_CLOCK,
        max_wall_clock=MAX_WALL_CLOCK,
        provider=historical_provider,
    )
    provider_prepared, fixture = _prepared_acquisition_run(
        provider_accepted, tmp_path, provider=historical_provider, effect_class="external_read"
    )
    contract = capture_contract() if contract is None else contract
    contract_pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    terminal_contract = contract if terminal_contract is None else terminal_contract
    terminal_pin = ArtifactPin(
        role="capture-contract",
        target=terminal_contract.identity,
        artifact_digest=capture_contract_digest(terminal_contract).tagged,
    )
    source_node = CallNode(
        node_id="direct",
        captures={"capture": named(contract_pin)},
        slot=PROVIDER_SLOT,
        input={"value": "$input.value"},
        as_="source_result",
        next="capture-output" if include_terminal else None,
    )
    nodes = (
        (
            source_node,
            CaptureEgressNode(
                node_id="capture-output",
                capture_contract=named(terminal_pin),
                input="$steps.source_result",
                result="$steps.source_result",
            ),
        )
        if include_terminal
        else (source_node,)
    )
    authored = authored_procedure(provider_accepted)
    definition = authored.definition.model_copy(update={"nodes": nodes, "returns": source_node.as_})
    pins = tuple(
        sorted(
            (
                *authored.pins,
                contract_pin,
                *((terminal_pin,) if terminal_pin != contract_pin else ()),
            ),
            key=lambda pin: (
                pin.role.encode("utf-8"),
                pin.target.qualified.encode("utf-8"),
                pin.artifact_digest.encode("ascii"),
            ),
        )
    )
    policy = AcquisitionDefault(max_age=CanonicalDuration(microseconds=60_000_000))
    (source_field,) = source_node.captures
    source_output = f"{source_node.as_}.{source_field}"
    procedure = authored.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "pins": pins,
            "acquisition_defaults": {source_output: policy},
        }
    )
    # New Procedure artifacts are accepted only in the named (v3) format at the
    # current compiler; name it here so execution and any later real
    # acceptance of the same bytes (test_live_capture_consumers.py) agree on
    # one exact digest throughout.
    from tests.support.procedures import revalidated

    procedure = revalidated(procedure)
    plain = AcceptedProcedure(
        path=provider_accepted.path,
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )

    def lookup(identity):  # type: ignore[no-untyped-def]
        if identity.kind == "Provider" and identity == historical_provider.provider.identity:
            return historical_provider.provider, historical_provider.artifact_digest
        if identity.kind == "CaptureContract":
            for candidate in (contract, terminal_contract):
                if candidate.identity == identity:
                    return candidate, capture_contract_digest(candidate).tagged
        return None

    operation = historical_provider.provider.operations[0]
    accepted = bind_procedure_references(
        plain,
        lookup=lookup,
        bindings={
            PROVIDER_SLOT: LineBinding(
                build=binding_pin(
                    historical_provider.provider.identity, historical_provider.artifact_digest
                ),
                operation=operation.name,
            )
        },
    )
    policy_digest = acquisition_rules_digest(
        resolve_acquisition_rules((source_output,), defaults={source_output: policy}, overrides={})
    )
    old_plan = provider_prepared.acquisition_plan
    old_occurrence = old_plan.external_occurrences[0]
    occurrence = ProviderExternalOccurrencePlan.model_validate(
        {
            **old_occurrence.model_dump(mode="python"),
            "occurrence_path": "source/direct",
            "occurrence_kind": "source",
            "input_name": source_node.as_,
            "capture_contracts": {source_field: contract_pin.artifact_digest},
            "contracts": (
                _VALUE_CONTRACT,
                _ENVELOPE_CONTRACT,
                _COORDINATE_CONTRACT,
                _SELECTOR_CONTRACT,
                _EFFECTIVE_CONTRACT,
            ),
            "source_runtime_plan_digest": digest("source-runtime-plan", "unit1"),
        }
    )
    decision = old_plan.selection_decision.model_copy(update={"policy_digest": policy_digest})
    plan = ProcedureAcquisitionPlan(
        **{
            **old_plan.model_dump(mode="python", exclude={"tag"}),
            "acquisition_policy_digest": policy_digest,
            "selection_decision": decision,
            "selection_decision_digest": procedure_selection_decision_digest(decision),
            "external_occurrences": (occurrence,),
        }
    )
    node_pin_sets = procedure_node_pin_sets(accepted)
    plan_digest = procedure_acquisition_plan_digest(plan)
    old_admission = provider_prepared.admission
    fields = admission_fields(old_admission)
    fields.update(
        {
            "procedure_artifact_digest": accepted.artifact_digest,
            "definition_digest": accepted.procedure.definition_digest,
            "invocation_input": {"value": 3},
            "full_pins": accepted.procedure.pins,
            "node_pin_sets": node_pin_sets,
            "pin_set_digest": procedure_pin_set_digest(accepted.procedure.pins, node_pin_sets),
            "acquisition_policy_digest": policy_digest,
            "selection_decision": decision,
            "selection_decision_digest": plan.selection_decision_digest,
            "acquisition_plan_digest": plan_digest,
        }
    )
    if capture_budget is not None:
        fields["budget"] = old_admission.budget.model_copy(
            update={"max_capture_bytes": capture_budget}
        )
    admission = seal_procedure_run_admission(fields)
    prepared = PreparedProcedureRun.model_validate(
        {
            **provider_prepared.model_dump(mode="python"),
            "admission": admission,
            "acquisition_plan": plan,
            "acquisition_plan_digest": plan_digest,
        }
    )
    return accepted, prepared, fixture, policy, contract


class _RefusingSourceInvoker(_SourceInvoker):
    def invoke_provider(  # type: ignore[no-untyped-def]
        self, *, occurrence, context, invocation_id, bound, deadline
    ):
        self.spawn_calls += 1
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelope(
                protocol_version="2.0",
                run_id=context.run_id,
                status="refused",
                refusal=ProviderRuntimeRefusal(
                    code="provider_declined",
                    message="the requested row is temporarily absent",
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


def _payloads(prepared, fixture):  # type: ignore[no-untyped-def]
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    access = BodyAccessContext(principal_id="unit-test", can_read_body=True)
    return records, [
        parse_journal_payload(fixture.bodies.read(item.record.payload_digest, access=access))
        for item in records
    ]


def _next_attempt(prepared: PreparedProcedureRun) -> PreparedProcedureRun:
    admission = seal_procedure_run_admission(
        {**admission_fields(prepared.admission), "attempt": prepared.admission.attempt + 1}
    )
    return PreparedProcedureRun.model_validate(
        {**prepared.model_dump(mode="python"), "admission": admission}
    )


def test_produced_capture_association_needs_both_path_and_receipt() -> None:
    assert _source_capture_association_fields(None, None) == {}
    assert _source_capture_association_fields("source/direct", None) == {}
    assert _source_capture_association_fields(None, digest("receipt", "unit1")) == {}
    assert _source_capture_association_fields("source/direct", digest("receipt", "unit1")) == {
        "occurrence_path": "source/direct",
        "invocation_receipt_digest": digest("receipt", "unit1"),
    }


def test_dynamic_source_request_is_a_pre_spawn_result_and_capture_is_post_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted, prepared, fixture, policy, contract = _source_fixture(tmp_path)
    invoker = _SourceInvoker(observed_at=prepared.admission.occurrence_evaluation_time)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        capture_contracts={capture_contract_digest(contract).tagged: contract},
        acquisition_rules=_acquisition_rules(policy),
    )
    original_append = executor._append_event  # noqa: SLF001
    reservation_observations: list[tuple[str, ...]] = []

    def append_with_reservation_probe(admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        if event_kind == "produced_capture":
            active = executor.material_reservations.active()
            reservation_observations.append(tuple(item.body_digest for item in active))
            assert len(active) == 2
            assert all(fixture.bodies.verify(item.body_digest) for item in active)
        return original_append(admission, records, event_kind, payload)

    monkeypatch.setattr(executor, "_append_event", append_with_reservation_probe)

    result = executor.execute(prepared, accepted)

    assert result.status == "succeeded"
    # The acquiring call's value names the Capture its whole-wire output became.
    assert set(result.output) == {"capture"}  # type: ignore[arg-type]
    assert result.output["capture"].startswith("sha256:")  # type: ignore[index]
    assert (invoker.bind_calls, invoker.spawn_calls) == (1, 1)
    assert invoker.coordinates == {
        "accepted_coordinate": prepared.admission.accepted_coordinate.model_dump(mode="json"),
        "occurrence_path": "source/direct",
        "invocation_id": invoker.coordinates["invocation_id"],
    }
    records, payloads = _payloads(prepared, fixture)
    kinds = [item.record.event_kind for item in records]
    assert kinds.index("attempt_started") < kinds.index("admission_bound")
    assert (
        kinds.index("admission_bound")
        < kinds.index("source_request_derived")
        < kinds.index("provider_invocation_started")
        < kinds.index("provider_invocation_completed")
        < kinds.index("source_acquisition")
        < kinds.index("produced_capture")
        < kinds.index("node_fired")
        < kinds.index("attempt_finalized")
    )
    derived = ProcedureDerivedSourceRequest.model_validate(
        payloads[kinds.index("source_request_derived")]
    )
    assert derived.admission_binding_digest == prepared.admission.admission_binding_digest
    assert derived.request == {"value": 3}
    admission_wire = canonical_bytes(prepared.admission.model_dump(mode="json")).decode("utf-8")
    assert "source_result" not in admission_wire
    started = ProviderInvocationStarted.model_validate(
        payloads[kinds.index("provider_invocation_started")]
    )
    completed = ProviderInvocationCompleted.model_validate(
        payloads[kinds.index("provider_invocation_completed")]
    )
    assert isinstance(
        ProviderInvocationOutputDigest.model_validate(completed.receipt.output),
        ProviderInvocationOutputDigest,
    )
    # Only the journal records from provider_invocation_completed onward could
    # carry a leaked raw output value; admission_bound legitimately carries the
    # operation's own out Contract schema, which (matching the real envelope,
    # F02) declares a field literally named "content_base64".
    assert b"content_base64" not in canonical_bytes(
        payloads[kinds.index("provider_invocation_completed") :]
    )
    assert started.invocation_id == completed.invocation_id
    produced = payloads[kinds.index("produced_capture")]
    assert isinstance(produced, dict)
    assert produced["invocation_receipt_digest"] == completed.receipt_digest
    capture = parse_capture_envelope(
        fixture.bodies.read(
            str(produced["capture_digest"]),
            access=BodyAccessContext(principal_id="unit-test", can_read_body=True),
        )
    )
    assert isinstance(capture, CaptureEnvelope)
    assert capture.producer_receipt_digest == completed.receipt_digest
    assert fixture.bodies.verify(capture.commitment.digest)
    assert reservation_observations
    assert not executor.material_reservations.active()

    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_args: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(  # noqa: SLF001
        _Instance(), run_id=prepared.admission.run_id, receipt=result.receipt
    )
    assert isinstance(state.receipt, ProcedureRunReceipt)
    assert state.receipt.invocation_receipt_digests == (completed.receipt_digest,)
    assert state.receipt.source_capture_associations[0].capture_digest == produced["capture_digest"]


def test_exact_run_id_replay_does_not_construct_an_invoker_or_spawn(
    tmp_path: Path,
) -> None:
    accepted, prepared, fixture, policy, contract = _source_fixture(tmp_path)
    first_invoker = _SourceInvoker(observed_at=prepared.admission.occurrence_evaluation_time)
    first = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=first_invoker,
        capture_contracts={capture_contract_digest(contract).tagged: contract},
        acquisition_rules=_acquisition_rules(policy),
    ).execute(prepared, accepted)
    accepted_path = tmp_path / "accepted.json"
    prepared_path = tmp_path / "prepared.json"
    marker_path = tmp_path / "invoker-constructed"
    from cruxible_core.procedures.reference_binding import authored_procedure

    authored = AcceptedProcedure(
        path=accepted.path,
        procedure=authored_procedure(accepted),
        artifact_digest=accepted.artifact_digest,
    )
    accepted_path.write_text(authored.model_dump_json(), encoding="utf-8")
    (tmp_path / "binding-provider.json").write_text(_source_provider().provider.model_dump_json())
    (tmp_path / "binding-contract.json").write_text(contract.model_dump_json())
    prepared_path.write_text(prepared.model_dump_json(), encoding="utf-8")
    fixture.run_index.close()
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
        from cruxible_core.storage.cas import ContentAddressedBodyStore
        from cruxible_core.exhaust import LocalJournalBackend
        from cruxible_core.procedures.execution import (
            PreparedProcedureRun,
            ProcedureExecutor,
        )
        from cruxible_core.procedures.run_index import ProcedureRunIndex
        from tests.test_procedures.test_procedure_execution import _Authority, _Contracts

        root, accepted_path, prepared_path, marker_path = map(Path, sys.argv[1:])
        accepted = AcceptedProcedure.model_validate_json(accepted_path.read_text())
        prepared = PreparedProcedureRun.model_validate_json(prepared_path.read_text())
        from cruxible_client.contracts.providers import NamedProviderBuild, provider_digest
        from cruxible_client.contracts.captures import CaptureContractV1, capture_contract_digest
        from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
        from cruxible_core.procedures.reference_binding import replay_procedure_references

        material = {}
        provider_artifact = None
        for name, model, digest in (
            ("provider", NamedProviderBuild, provider_digest),
            ("contract", CaptureContractV1, capture_contract_digest),
        ):
            artifact = model.model_validate_json((root / ("binding-" + name + ".json")).read_text())
            material[(artifact.identity, digest(artifact).tagged)] = artifact
            if name == "provider":
                provider_artifact = artifact
        operation = provider_artifact.operations[0]
        bindings = {
            "compute": LineBinding(
                build=binding_pin(
                    provider_artifact.identity, provider_digest(provider_artifact).tagged
                ),
                operation=operation.name,
            )
        }
        accepted = replay_procedure_references(
            accepted, pins=prepared.admission.full_pins,
            lookup_exact=lambda identity, digest: (
                (material[(identity, digest)], digest) if (identity, digest) in material else None
            ),
            bindings=bindings,
        )

        def construct_invoker():
            marker_path.write_text("constructed")
            raise AssertionError("exact replay constructed a Provider invoker")

        index = ProcedureRunIndex(root / "run-index.sqlite")
        try:
            replay = ProcedureExecutor(
                journal=LocalJournalBackend(root / "journal"),
                bodies=ContentAddressedBodyStore(root / "cas"),
                run_index=index,
                fencing_token="writer",
                activation_authority=_Authority(accepted.artifact_digest),
                contract_validator=_Contracts(),
                provider_runtime_invoker_factory=construct_invoker,
            ).execute(prepared, accepted)
            print(replay.model_dump_json())
        finally:
            index.close()
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            str(accepted_path),
            str(prepared_path),
            str(marker_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert not marker_path.exists()
    assert f'"run_id":"{first.run_id}"' in completed.stdout
    assert '"status":"succeeded"' in completed.stdout


def test_capture_reservation_precedes_the_first_body_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accepted, prepared, fixture, _policy, _contract = _source_fixture(tmp_path)
    reserved_store = ReservedCaptureStore(
        bodies=fixture.bodies,
        reservations=ProcedureMaterialReservationStore(fixture.bodies.reservation_root),
        admission=prepared.admission,
    )

    def fail_store(_content: bytes, *, best_effort: bool = False):  # type: ignore[no-untyped-def]
        raise CasError("simulated body-store loss")

    monkeypatch.setattr(fixture.bodies, "store", fail_store)
    with pytest.raises(CasError, match="simulated"):
        reserved_store.store(b'{"size":3}')

    active = ProcedureMaterialReservationStore(fixture.bodies.reservation_root).active()
    assert len(active) == 1
    assert active[0].body_digest == fixture.bodies.digest_bytes(b'{"size":3}').tagged


def test_crash_after_capture_cas_before_produced_event_retains_both_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted, prepared, fixture, policy, contract = _source_fixture(tmp_path)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_SourceInvoker(
            observed_at=prepared.admission.occurrence_evaluation_time
        ),
        capture_contracts={capture_contract_digest(contract).tagged: contract},
        acquisition_rules=_acquisition_rules(policy),
    )
    original_append = executor._append_event  # noqa: SLF001

    def crash_before_produced_event(admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        if event_kind == "produced_capture":
            raise JournalError("simulated journal loss")
        return original_append(admission, records, event_kind, payload)

    monkeypatch.setattr(executor, "_append_event", crash_before_produced_event)

    result = executor.execute(prepared, accepted)

    assert result.status == "failed"
    active = executor.material_reservations.active()
    assert len(active) == 2
    assert all(fixture.bodies.verify(item.body_digest) for item in active)


# NOTE: the former `test_only_input_attributed_provider_refusal_uses_the_declared_source_default`
# exercised a provider refusal defaulting through `requirement=conservative_default`
# / `default_authorized`. That eligibility model has no replacement in the new
# `AcquisitionRule` (`apply_acquisition_result` never returns "defaulted"; B2
# carried forward only max_age/on_unavailable/on_stale/on_oversized/
# permitted_replayability, per F05) -- there is no "succeeded with a defaulted
# output" outcome to reproduce.


def test_incomplete_source_closure_refuses_before_attempt_or_invoker_construction(
    tmp_path: Path,
) -> None:
    accepted, prepared, fixture, policy, _contract = _source_fixture(tmp_path)
    invoker = _SourceInvoker(observed_at=prepared.admission.occurrence_evaluation_time)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        acquisition_rules=_acquisition_rules(policy),
        capture_contracts={},
    )

    with pytest.raises(ExecutionError, match="Source closure does not reproduce"):
        executor.execute(prepared, accepted)

    assert (invoker.bind_calls, invoker.spawn_calls) == (0, 0)
    assert not fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )


def test_currency_refusal_precedes_source_closure_preflight(tmp_path: Path) -> None:
    accepted, prepared, fixture, policy, _contract = _source_fixture(tmp_path)
    authority = _Authority(digest("procedure", "superseded"))

    with pytest.raises(ExecutionError, match="current"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=authority,
            contract_validator=_Contracts(),
            acquisition_rules=_acquisition_rules(policy),
            capture_contracts={},
        ).execute(prepared, accepted)

    assert authority.calls == 1
    assert not fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )


def test_two_run_ids_under_one_semantic_key_keep_independent_source_results(
    tmp_path: Path,
) -> None:
    accepted, first_prepared, fixture, policy, contract = _source_fixture(tmp_path)
    second_prepared = _next_attempt(first_prepared)
    assert first_prepared.admission.run_id != second_prepared.admission.run_id
    assert (
        first_prepared.admission.semantic_replay_key_digest
        == second_prepared.admission.semantic_replay_key_digest
    )

    def execute(prepared: PreparedProcedureRun, result_size: int):  # type: ignore[no-untyped-def]
        return ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
            provider_runtime_invoker=_SourceInvoker(
                observed_at=prepared.admission.occurrence_evaluation_time,
                result_size=result_size,
            ),
            capture_contracts={capture_contract_digest(contract).tagged: contract},
            acquisition_rules=_acquisition_rules(policy),
        ).execute(prepared, accepted)

    first = execute(first_prepared, 3)
    second = execute(second_prepared, 4)

    # Each run's value names its own Capture: the two materials differ.
    assert first.output != second.output
    assert set(first.output) == set(second.output) == {"capture"}  # type: ignore[arg-type]
    first_replay = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(first_prepared, accepted)
    second_replay = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(second_prepared, accepted)
    assert first_replay.output == first.output
    assert second_replay.output == second.output


@pytest.mark.parametrize("budget_refusal", [False, True])
def test_live_v5_capture_terminal_uses_the_v2_topological_receipt_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_refusal: bool,
) -> None:
    accepted, prepared, fixture, policy, contract = _source_fixture(
        tmp_path,
        include_terminal=True,
        capture_budget=10 if budget_refusal else None,
    )
    contract_digest = capture_contract_digest(contract).tagged
    rung = compute_effective_rung(
        procedure_terminal_capability=accepted.procedure.definition.terminal_capability,
        requested_terminal_rung=0,
        selector_privacies={},
        taint_labels=(),
        procedure_definition_digest=prepared.admission.definition_digest,
        line_spec_digest=prepared.admission.line_spec_digest or "",
        sensitivity_policy_digest=prepared.admission.sensitivity_policy_digest or "",
        mandate_coordinate_digest=prepared.admission.mandate_coordinate_digest or "",
        calibration_coordinate_digest=prepared.admission.calibration_coordinate_digest or "",
    )
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_SourceInvoker(
            observed_at=prepared.admission.occurrence_evaluation_time
        ),
        capture_contracts={contract_digest: contract},
        acquisition_rules=_acquisition_rules(policy),
        effective_rung=rung,
        egress_sink=CaptureTerminalEgressSink(
            store=fixture.bodies,
            contracts={contract_digest: contract},
            producer=accepted.procedure.identity,
            producer_binding_digest=accepted.artifact_digest,
        ),
    ).execute(prepared, accepted)

    records, payloads = _payloads(prepared, fixture)
    kinds = [item.record.event_kind for item in records]
    if budget_refusal:
        assert result.status != "succeeded"
        assert "terminal_egress" not in kinds
        assert kinds.count("produced_capture") == 1  # Source succeeded; emission did not.
        assert "max_capture_bytes" in str(payloads[-1])
        return
    assert result.status == "succeeded"
    assert kinds.count("produced_capture") == 2
    terminal = payloads[kinds.index("terminal_egress")]
    assert isinstance(terminal, dict)
    receipt = TerminalEgressReceiptV2.model_validate(terminal["receipt"])
    terminal_capture = parse_capture_envelope(
        fixture.bodies.read(
            receipt.children[0].egress_digest,
            access=BodyAccessContext(principal_id="unit-test", can_read_body=True),
        )
    )
    assert isinstance(terminal_capture, CaptureEnvelope)
    assert terminal_capture.producer_receipt_digest == receipt.producer_receipt_digest
    scan_calls = {"partitions": 0, "records": 0}
    original_partition_ids = fixture.journal.partition_ids
    original_all_records = fixture.journal.all_records

    def counted_partition_ids(stream):  # type: ignore[no-untyped-def]
        scan_calls["partitions"] += 1
        return original_partition_ids(stream)

    def counted_all_records(stream, partition_id):  # type: ignore[no-untyped-def]
        scan_calls["records"] += 1
        return original_all_records(stream, partition_id)

    monkeypatch.setattr(fixture.journal, "partition_ids", counted_partition_ids)
    monkeypatch.setattr(fixture.journal, "all_records", counted_all_records)
    resolver = journal_producer_receipt_resolver(
        journal=fixture.journal,
        instance_id=prepared.admission.instance_id,
        bodies=fixture.bodies,
    )
    produced = payloads[kinds.index("produced_capture")]
    assert isinstance(produced, dict)
    provider_capture_digest = str(produced["capture_digest"])
    provider_capture = parse_capture_envelope(
        fixture.bodies.read(
            provider_capture_digest,
            access=BodyAccessContext(principal_id="unit-test", can_read_body=True),
        )
    )
    assert isinstance(provider_capture, CaptureEnvelope)
    provider_evidence = provider_capture.production_evidence
    assert isinstance(provider_evidence, ProviderInvocationCaptureEvidence)
    provider_artifacts = {
        provider_capture.producer.qualified: prepared.acquisition_plan.external_occurrences[
            0
        ].provider_artifact_digest,
    }
    assert verify_capture(
        provider_capture_digest,
        store=fixture.bodies,
        contract=contract,
        producer_artifact_digests=provider_artifacts,
        producer_receipt_resolver=resolver,
    ).producer_receipt_digest == str(produced["invocation_receipt_digest"])
    assert (
        verify_capture(
            receipt.children[0].egress_digest,
            store=fixture.bodies,
            contract=contract,
            producer_artifact_digests={
                accepted.procedure.identity.qualified: accepted.artifact_digest,
            },
            producer_receipt_resolver=resolver,
        )
        == terminal_capture
    )
    forged_occurrence_digest = digest("forged-occurrence", "unit1-fix2")
    forged_secret = ProviderSecretReference(
        realm="orders",
        name="reader",
        epoch="forged-epoch",
        purpose="read",
        resolver_kind="environment",
    )
    forged_provider_captures = {
        "provider_artifact_digest": provider_capture.model_copy(
            update={
                "production_evidence": provider_evidence.model_copy(
                    update={
                        "provider_artifact_digest": digest("forged-provider-artifact", "unit1-fix2")
                    }
                )
            }
        ),
        "external_occurrence_plan_digest": provider_capture.model_copy(
            update={
                "source": provider_capture.source.model_copy(
                    update={"producer_binding_digest": forged_occurrence_digest}
                ),
                "producer_binding_digest": forged_occurrence_digest,
                "production_evidence": provider_evidence.model_copy(
                    update={"external_occurrence_plan_digest": forged_occurrence_digest}
                ),
            }
        ),
        "secret_references": provider_capture.model_copy(
            update={
                "production_evidence": provider_evidence.model_copy(
                    update={"secret_references": (forged_secret,)}
                )
            }
        ),
    }
    for field, forged in forged_provider_captures.items():
        forged_digest = capture_digest(forged).tagged
        fixture.bodies.store(render_capture_envelope(forged))
        with pytest.raises(CaptureFormatError, match=field):
            verify_capture(
                forged_digest,
                store=fixture.bodies,
                contract=contract,
                producer_artifact_digests=provider_artifacts,
                producer_receipt_resolver=resolver,
                # No run produced the forgery; this pins each receipt edge.
                require_production_proof=False,
            )
    # Every lookup read only the cited run's records, once: no journal scan.
    assert scan_calls == {"partitions": 0, "records": 0}
    assert resolver.lookups == 1

    unrelated = next(
        stored
        for stored in records
        if stored.record.event_kind
        not in {
            "admission_bound",
            "source_request_derived",
            "source_read",
            "provider_invocation_completed",
            "terminal_egress",
            "produced_capture",
        }
    )
    original_body_read = fixture.bodies.read
    unrelated_reads = 0

    def read_with_unrelated_damage(body_digest, *, access):  # type: ignore[no-untyped-def]
        nonlocal unrelated_reads
        if body_digest == unrelated.record.payload_digest:
            unrelated_reads += 1
            return b"not a journal payload"
        return original_body_read(body_digest, access=access)

    notes: list[ProducerReceiptJournalNote] = []
    with monkeypatch.context() as patch:
        patch.setattr(fixture.bodies, "read", read_with_unrelated_damage)
        damaged_resolver = journal_producer_receipt_resolver(
            journal=fixture.journal,
            instance_id=prepared.admission.instance_id,
            bodies=fixture.bodies,
            note_sink=notes.append,
            full_journal=True,
        )
        assert (
            verify_capture(
                provider_capture_digest,
                store=fixture.bodies,
                contract=contract,
                producer_artifact_digests=provider_artifacts,
                producer_receipt_resolver=damaged_resolver,
            )
            == provider_capture
        )
    # A record no receipt or production proof is built from is never read.
    assert unrelated_reads == 0
    assert notes == []

    completed_record = records[kinds.index("provider_invocation_completed")]
    completed_digest = str(produced["invocation_receipt_digest"])

    transient_reads = 0

    def fail_first_partition_read(stream, partition_id):  # type: ignore[no-untyped-def]
        nonlocal transient_reads
        transient_reads += 1
        if transient_reads == 1:
            raise OSError("transient journal read")
        return original_all_records(stream, partition_id)

    with monkeypatch.context() as patch:
        patch.setattr(fixture.journal, "all_records", fail_first_partition_read)
        transient_resolver = journal_producer_receipt_resolver(
            journal=fixture.journal,
            instance_id=prepared.admission.instance_id,
            bodies=fixture.bodies,
            full_journal=True,
        )
        run_id = prepared.admission.run_id
        with pytest.raises(
            CaptureFormatError,
            match="producer receipt journal partition is unavailable",
        ):
            transient_resolver.producer_receipt(completed_digest, run_id=run_id)
        recovered = transient_resolver.producer_receipt(completed_digest, run_id=run_id)
    assert isinstance(recovered, ProviderProducerReceiptResolution)
    assert provider_invocation_receipt_digest(recovered.receipt) == completed_digest
    assert transient_reads > 1

    def read_with_target_damage(body_digest, *, access):  # type: ignore[no-untyped-def]
        if body_digest == completed_record.record.payload_digest:
            return journal_payload_bytes({"receipt_digest": completed_digest})
        return original_body_read(body_digest, access=access)

    with monkeypatch.context() as patch:
        patch.setattr(fixture.bodies, "read", read_with_target_damage)
        damaged_resolver = journal_producer_receipt_resolver(
            journal=fixture.journal,
            instance_id=prepared.admission.instance_id,
            bodies=fixture.bodies,
        )
        with pytest.raises(CaptureFormatError, match=completed_digest):
            damaged_resolver.producer_receipt(completed_digest, run_id=run_id)
    assert result.receipt.chain_head_digest == records[-1].record_digest
