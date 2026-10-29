"""Served Source acquisition honors accepted eligibility and coherence rules."""

from datetime import timedelta
from pathlib import Path

import pytest
from cruxible_client.contracts.acquisition_policies import (
    AcquisitionRule,
    IndependentCoherence,
    InputAcquisitionRule,
    SourceAcquisitionPolicyV1,
    acquisition_policy_path,
    acquisition_rule,
    parse_acquisition_policy,
    render_acquisition_policy,
)

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_core.evidence.source_readers import (
    ExternalSourceReadRequestV1,
    FakeVersionedExternalSourceReader,
    ProducerBindingV1,
)
from cruxible_core.procedures.acquisition import (
    ProcedureSourceAcquisitionResultV1,
    apply_acquisition_result,
)
from tests.core_support._pc_c_support import (
    NOW,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)
from tests.test_procedures import test_procedure_source_runs as source


@pytest.fixture
def acquired(tmp_path: Path) -> ProcedureSourceAcquisitionResultV1:
    contract = capture_contract()
    adapter = provider(contract)
    binding = ProducerBindingV1(
        provider=adapter.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("adapter", "source-policy"),
    )
    reader = FakeVersionedExternalSourceReader()
    reader.seed(
        source_identity=binding.logical_source_identity,
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "1"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
        value={"order_id": 7},
    )
    result = reader.acquire(
        ExternalSourceReadRequestV1(
            contract=contract,
            provider=adapter,
            binding=binding,
            coordinate_type="postgres-lsn-v1",
            coordinate={"lsn": "1"},
            selector_type="relation-primary-key-v1",
            selector={"id": 7, "relation": "orders"},
            materialization="cas",
            run_coordinate=provider_run(adapter),
            observed_at=NOW,
            resource_budget=contract.selection_budget,
        ),
        store=body_store(tmp_path),
    )
    return ProcedureSourceAcquisitionResultV1(
        node_id="source", input_name="advisory", outcome="acquired", acquisition=result
    )


@pytest.mark.parametrize("reason", ["replayability", "age"])
@pytest.mark.parametrize(
    ("requirement", "behavior", "authorized", "expected"),
    [
        ("required", "refuse", False, "refused"),
        ("optional", "omit_optional", False, "omitted"),
        ("conservative_default", "declared_conservative_default", False, "refused"),
        ("conservative_default", "declared_conservative_default", True, "defaulted"),
    ],
)
def test_acquired_eligibility_applies_declared_failure_and_default_authority(
    acquired: ProcedureSourceAcquisitionResultV1,
    reason: str,
    requirement: str,
    behavior: str,
    authorized: bool,
    expected: str,
) -> None:
    rule = InputAcquisitionRule.model_validate(
        {
            "input_name": "advisory",
            "requirement": requirement,
            "permitted_replayability": ("attested_only",)
            if reason == "replayability"
            else ("exact",),
            "max_age": CanonicalDuration(microseconds=1_000_000),
            "on_unavailable": behavior,
            "on_stale": behavior,
            "on_oversized": behavior,
            "on_conflict": "preserve",
            "conservative_default": {"severity": "unknown"}
            if requirement == "conservative_default"
            else None,
        }
    )
    from cruxible_client.contracts.artifacts import ArtifactIdentity

    policy = SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="legacy"),
        inputs=(rule,),
        coherence=IndependentCoherence(),
    )
    parsed = parse_acquisition_policy(
        render_acquisition_policy(policy), path=acquisition_policy_path("legacy")
    )
    assert isinstance(parsed, SourceAcquisitionPolicyV1)
    assert parsed.inputs[0] == rule
    decision = apply_acquisition_result(
        rule,
        acquired,
        default_authorized=authorized,
        evaluation_time=NOW + timedelta(seconds=2),
    )
    assert decision.disposition == expected
    assert decision.selected_capture_digests == ()
    assert acquired.acquisition is not None
    assert decision.considered_capture_digests == (acquired.acquisition.capture_digest,)
    assert decision.reason_codes == (
        "cruxible.acquisition.unavailable"
        if reason == "replayability"
        else "cruxible.acquisition.stale",
    )


def test_capture_at_maximum_age_remains_eligible(
    acquired: ProcedureSourceAcquisitionResultV1,
) -> None:
    rule = AcquisitionRule(input_name="advisory", max_age=CanonicalDuration(microseconds=1_000_000))
    decision = apply_acquisition_result(
        rule, acquired, default_authorized=False, evaluation_time=NOW + timedelta(seconds=1)
    )
    assert decision.disposition == "selected"
    assert acquired.acquisition is not None
    assert decision.selected_capture_digests == (acquired.acquisition.capture_digest,)


@pytest.mark.parametrize("outcome", ["unavailable", "stale", "oversized", "refused"])
def test_current_policy_refuses_every_failed_fetch(outcome: str) -> None:
    rule = acquisition_rule(source._policy(), "advisory")
    assert rule is not None
    result = ProcedureSourceAcquisitionResultV1.model_validate(
        {
            "node_id": "source",
            "input_name": "advisory",
            "outcome": outcome,
            "reason_code": "cruxible.acquisition." + outcome,
        }
    )
    assert apply_acquisition_result(rule, result, evaluation_time=NOW).disposition == "refused"


def test_selection_does_not_restate_contract_replayability(
    acquired: ProcedureSourceAcquisitionResultV1,
) -> None:
    decision = apply_acquisition_result(
        AcquisitionRule(input_name="advisory"), acquired, evaluation_time=NOW
    )
    assert decision.disposition == "selected"


def test_stale_input_refusal_names_alias_age_and_repair(tmp_path: Path) -> None:
    import base64

    from cruxible_client.contracts.provider_interfaces import (
        provider_interface_digest,
        provider_interface_path,
        render_provider_interface,
    )

    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
    from cruxible_client.contracts.capture_reads import CaptureReadRequest
    from cruxible_client.contracts.captures import (
        ProviderResultToExternalCapture,
        capture_contract_digest,
    )
    from cruxible_client.contracts.procedures.artifacts import (
        procedure_artifact_digest,
        procedure_path,
        render_procedure,
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.providers import (
        ProviderBuild,
        provider_build_key,
        provider_digest,
        provider_expected_implementation_records,
        provider_manifest_digest,
        provider_path,
        render_provider,
    )
    from cruxible_client.contracts.source_references import ExternalSourceReference
    from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
    from tests.support.provider_builds import capture_provider_build
    from tests.test_authoring.p2b4_unit1.test_live_capture_consumers import (
        _acquisition_registration,
    )

    instance, owner, procedure, root, _ = source._world(tmp_path, policy=source._policy())
    acquired, _ = source._run(instance, root)
    assert acquired.status == "succeeded", acquired.terminal
    retained_digest = acquired.source_observations[0].capture_digest
    assert retained_digest is not None
    retained = service_read_playbill_capture(
        instance,
        request=CaptureReadRequest(capture_digest=retained_digest),
        access=source.BodyAccessContext(principal_id="test", can_read_body=True),
    )
    assert retained.status == "verified"
    envelope = retained.envelope
    assert envelope is not None
    assert envelope.observed_at == source.NOW
    assert (
        envelope.capture_contract_digest
        == capture_contract_digest(source.capture_contract()).tagged
    )
    assert isinstance(envelope.source, ExternalSourceReference)

    # A source adapter reads the observation an earlier acquire actually landed.
    # Its recorded observation time stays intact when the adapter serves it later.
    from cruxible_client.contracts.provider_interfaces import interface_definition_digest

    from cruxible_client.contracts.artifacts import ArtifactNameRef
    from cruxible_client.contracts.named_references import name_provider

    registration = _acquisition_registration()
    adapter = capture_provider_build(source.capture_contract(), named=False)
    manifest = adapter.runtime_artifact.manifest.model_copy(
        update={
            "implementations": tuple(
                item.model_copy(
                    update={"interface_digest": interface_definition_digest(registration)}
                )
                for item in adapter.runtime_artifact.manifest.implementations
            ),
        }
    )
    runtime = adapter.runtime_artifact.model_copy(
        update={
            "manifest": manifest,
            "manifest_digest": provider_manifest_digest(manifest),
        }
    )
    interface_pin = ArtifactPin(
        role="provider-interface",
        target=registration.identity,
        artifact_digest=provider_interface_digest(registration).tagged,
    )
    adapter = ProviderBuild.model_validate(
        {
            **adapter.model_dump(mode="python"),
            "identity": ArtifactIdentity(
                kind="Provider",
                name=adapter.provider_name + "/" + provider_build_key(runtime).value,
            ),
            "runtime_artifact": runtime,
            "implementations": provider_expected_implementation_records(runtime),
            "pins": tuple(
                sorted(
                    (
                        interface_pin,
                        *(pin for pin in adapter.pins if pin.role != "provider-interface"),
                    ),
                    key=lambda pin: (pin.role, pin.target.qualified),
                )
            ),
        }
    )
    adapter = name_provider(adapter)
    provider_pin = ArtifactPin(
        role="provider", target=adapter.identity, artifact_digest=provider_digest(adapter).tagged
    )
    read, shape = procedure.definition.nodes
    read = read.model_copy(
        update={
            "provider": ArtifactNameRef(target=adapter.identity),
            "interface": ArtifactNameRef(target=registration.identity),
            "request": {"capture_digest": retained_digest},
        }
    )
    definition = procedure.definition.model_copy(update={"nodes": (read, shape)})
    successor = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "pins": tuple(
                sorted(
                    (
                        provider_pin,
                        interface_pin,
                        *(
                            pin
                            for pin in procedure.pins
                            if pin.target.kind not in {"Provider", "ProviderInterface"}
                        ),
                    ),
                    key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest),
                )
            ),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_artifact_digest(procedure).tagged
            ),
        }
    )
    source._accept_more(
        instance,
        owner,
        {
            provider_interface_path(registration.identity.name): render_provider_interface(
                registration
            ),
            provider_path(adapter.identity.name): render_provider(adapter),
            procedure_path(successor.identity.name): render_procedure(successor),
        },
        name="retained-observation-source",
    )

    class RetainedObservationInvoker(source._WorkspaceInvoker):
        def invoke_provider(self, *, occurrence, context, invocation_id, bound, deadline):  # type: ignore[no-untyped-def]
            self.spawn_calls += 1
            assert context.input == {"capture_digest": retained_digest}
            body = instance.body_store().read(
                envelope.commitment.digest,
                access=source.BodyAccessContext(principal_id="test", can_read_body=True),
            )
            native = envelope.source
            assert isinstance(native, ExternalSourceReference)
            result = ProviderResultToExternalCapture(
                source_identity=native.source_identity,
                coordinate_type=native.coordinate_type,
                coordinate=native.coordinate,
                selector_type=native.selector_type,
                selector=native.selector,
                replayability=native.replayability,
                content_base64=base64.b64encode(body).decode("ascii"),
                byte_length=len(body),
                bytes_digest=envelope.commitment.digest,
                observed_at=envelope.observed_at,
                source_effective_time=envelope.source_effective_time,
            )
            return source.ProviderDriverOutcomeV1(
                envelope=source.ProviderRuntimeResultEnvelopeV1(
                    protocol_version="1.0",
                    run_id=context.run_id,
                    status="ok",
                    output=result.model_dump(mode="json"),
                ),
                stderr="",
                duration_seconds=0.001,
                egress=source.ProviderEgressObservation(
                    observer_backend="test-attribution", observer_grade="attribution"
                ),
                verified_binding=bound.binding,
            )

    state, invoker = source._run(
        instance,
        root,
        invoker=RetainedObservationInvoker(),
        evaluation_time=source.NOW + timedelta(days=1),
    )
    assert invoker.spawn_calls == 1
    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "cruxible.acquisition.stale"
    assert "advisory" in state.terminal.message
    assert "age" in state.terminal.message
    assert "Repair:" in state.terminal.message
    from cruxible_client.contracts.repairs import HandEditRepair

    assert isinstance(state.terminal.repair, HandEditRepair)
    assert state.terminal.repair.hand_edit.target == "Source:advisory"
    assert "age was" in state.terminal.repair.hand_edit.required_change
    assert "max_age" in state.terminal.repair.hand_edit.required_change
    assert state.result is None


def test_line_without_named_policy_runs_with_the_instance_default(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_path,
        render_line_spec,
    )

    instance, owner, procedure, root, policy = source._world(tmp_path)
    line = source._served_line(procedure, policy)
    line = line.model_copy(
        update={
            "acquisition_policy": None,
            "pins": tuple(pin for pin in line.pins if pin.target.kind != "SourceAcquisitionPolicy"),
        }
    )
    source._accept_more(
        instance,
        owner,
        {line_spec_path(line.identity.name): render_line_spec(line)},
        name="inherited-line",
    )
    state, invoker = source._run_line(instance, root, line)
    assert state.status == "succeeded", state.terminal
    assert invoker.spawn_calls == 1
    assert (
        source._admission(instance, state).acquisition_policy_digest
        == source.acquisition_policy_digest(policy).tagged
    )


def test_line_default_is_independent_of_a_procedures_direct_run_override(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec

    instance, owner, procedure, root, default = source._world(tmp_path, accept_procedure=False)
    custom = source._policy(name="direct-override")
    procedure = procedure.model_copy(update={"acquisition_policy": custom.identity.name})
    source._accept_more(
        instance,
        owner,
        {
            source.acquisition_policy_path(custom.identity.name): source.render_acquisition_policy(
                custom
            ),
            procedure_path(procedure.identity.name): render_procedure(procedure),
        },
        name="direct-override",
    )
    line = source._served_line(procedure, default)
    line = line.model_copy(
        update={
            "acquisition_policy": None,
            "pins": tuple(pin for pin in line.pins if pin.target.kind != "SourceAcquisitionPolicy"),
        }
    )
    source._accept_more(
        instance,
        owner,
        {line_spec_path(line.identity.name): render_line_spec(line)},
        name="default-line",
    )
    direct, _ = source._run(instance, root)
    inherited, _ = source._run_line(instance, root, line)
    assert direct.status == inherited.status == "succeeded"
    assert (
        source._admission(instance, direct).acquisition_policy_digest
        == source.acquisition_policy_digest(custom).tagged
    )
    assert (
        source._admission(instance, inherited).acquisition_policy_digest
        == source.acquisition_policy_digest(default).tagged
    )
