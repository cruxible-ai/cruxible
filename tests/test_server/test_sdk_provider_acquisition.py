"""An agent authors and runs a Source through the SDK, then reads its Capture.

Only the local adapter process is substituted. Provider/interface deployment is
governed setup; authoring, admission, output checks, capture construction and
public readback are the actual services.
"""

import base64
import hashlib
from datetime import UTC, datetime

import pytest

from cruxible_client import Playbill
from cruxible_client.contracts.acquisition_policies import (
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    ProviderResultToExternalCaptureV1,
    capture_component_pin,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import ProcedureDefinitionV5
from cruxible_client.contracts.provider_interfaces import (
    provider_interface_digest,
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    ProviderV2,
    provider_digest,
    provider_expected_implementation_records,
    provider_manifest_digest,
    provider_path,
    render_provider,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.providers.web_fetch import WEB_FETCH_SELECTORS, web_fetch_interface_registration
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._p2b1_support import provider_v2
from tests.core_support._pc_c_support import capture_contract
from tests.test_procedures import test_procedure_source_runs as source
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

URL = "https://fixture.invalid/api/v1/measurements.json"


def web_provider(registration, selectors=WEB_FETCH_SELECTORS):
    template = provider_v2()
    implementation = template.runtime_artifact.manifest.implementations[0].model_copy(
        update={
            "interface_id": registration.interface_id,
            "interface_digest": registration.interface_digest,
            "entrypoint": "cruxible_provider_web.fetch:WebFetch",
            "declared_input_buckets": tuple(sorted(selectors.values())),
            "bucket_conformance": {selector: name for name, selector in selectors.items()},
            "declared_endpoints": ("https://fixture.invalid",),
            "requires_extras": (),
            "deterministic": False,
            "side_effects": False,
        }
    )
    manifest = template.runtime_artifact.manifest.model_copy(
        update={"implementations": (implementation,)}
    )
    runtime = template.runtime_artifact.model_copy(
        update={
            "manifest": manifest,
            "manifest_digest": provider_manifest_digest(manifest),
        }
    )
    provider = template.model_copy(
        update={
            "runtime_artifact": runtime,
            "implementations": provider_expected_implementation_records(runtime),
            "pins": (
                ArtifactPin(
                    role="provider-interface",
                    target=registration.identity,
                    artifact_digest=provider_interface_digest(registration).tagged,
                ),
            ),
        }
    )
    return ProviderV2.model_validate(provider.model_dump(mode="json"))


def acquisition_result():
    content = canonical_bytes({"derived": {"text": "critical"}, "retrieved": {"url": URL}})
    return ProviderResultToExternalCaptureV1(
        source_identity="web.response",
        coordinate_type="http-response-v1",
        coordinate={"status": 200},
        selector_type="whole-response-v1",
        selector={},
        replayability="attested_only",
        content_base64=base64.b64encode(content).decode(),
        byte_length=len(content),
        bytes_digest="sha256:" + hashlib.sha256(content).hexdigest(),
        observed_at=datetime.now(UTC),
    ).model_dump(mode="json")


class AcquisitionInvoker(source._WorkspaceInvoker):
    def __init__(self, output):
        super().__init__()
        self.output = output

    def invoke_provider(self, *, occurrence, context, invocation_id, bound):
        from cruxible_client.contracts.provider_execution import ProviderEgressObservationV1
        from cruxible_core.providers.provider_local_runtime import ProviderDriverOutcomeV1
        from cruxible_core.providers.provider_runtime_contract import (
            ProviderRuntimeResultEnvelopeV1,
        )

        self.spawn_calls += 1
        if occurrence.occurrence_kind == "source":
            assert context.input["url"] == URL
            assert (
                occurrence.operation_contract.output
                == "playbill-provider-result-to-external-capture-v1"
            )
        else:
            assert occurrence.occurrence_kind == "call"
            assert context.input == {"size": 1}
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0", run_id=context.run_id, status="ok", output=self.output
            ),
            stderr="",
            duration_seconds=0.001,
            verified_binding=bound.binding,
            egress=ProviderEgressObservationV1(
                observer_backend="test-attribution", observer_grade="attribution"
            ),
        )


@pytest.mark.parametrize("adapter", [False, True], ids=["protocol", "web-adapter"])
def test_sdk_source_retains_and_reads_acquisition(playbill_http, tmp_path, monkeypatch, adapter):
    output = acquisition_result()
    if adapter:
        web = pytest.importorskip(
            "cruxible_provider_web.fetch", reason="cross-repo web provider integration"
        )
        from cruxible_provider_runtime.egress import EgressRecorder
        from cruxible_provider_runtime.protocol import Budgets
        from cruxible_provider_runtime.provider_api import ProviderRunContext
        from cruxible_provider_web.interfaces import FETCH_PREIMAGE

        from cruxible_core.providers.web_fetch import WEB_FETCH_INTERFACE_PREIMAGE

        assert FETCH_PREIMAGE == WEB_FETCH_INTERFACE_PREIMAGE
        result = web.WebFetch()(
            ProviderRunContext(
                run_id="sdk-acquisition",
                input={"url": URL},
                interface_id="web.fetch",
                interface_digest=web_fetch_interface_registration().interface_digest,
                implementation_digest="sha256:" + "a" * 64,
                declared_endpoints=("https://fixture.invalid",),
                input_bucket="source_kind=api_json;access=public;page_weight=light",
                budgets=Budgets(wall_clock_seconds=30, output_bytes=1_000_000),
                coordinates={},
                capture_contract=None,
                secrets={},
                egress=EgressRecorder(),
            )
        )
        assert result.status == "ok", result
        output = result.output

    http, instance_id, reviewer = playbill_http
    manager = get_playbill_manager()
    instance = manager.get(instance_id)
    registration = web_fetch_interface_registration()
    provider = web_provider(registration)
    contract = capture_contract().model_copy(
        update={
            "logical_source_identities": ("web.response",),
            "coordinate_schema_pins": (
                capture_component_pin("coordinate-schema", "http-response-v1"),
            ),
            "selector_schema_pins": (
                capture_component_pin("selector-schema", "whole-response-v1"),
            ),
            "selection_budget": capture_contract().selection_budget.model_copy(
                update={"max_bytes": 1_000_000}
            ),
        }
    )
    policy = source._policy()
    proposal = source.submit_member_candidate(
        instance,
        members={
            provider_interface_path(registration.interface_id): render_provider_interface(
                registration
            ),
            provider_path(provider.identity.name): render_provider(provider),
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
        },
        actor_id="operator",
        proposal_name="acquisition-deployment",
        proposal_family="procedure",
        timestamp=source.ACCEPT_STAMP,
    ).proposal
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    _approve_and_activate(http, instance_id, reviewer, proposal.admission.proposal_id)

    procedure = source._procedure(
        instance,
        root=tmp_path,
        contract=contract,
        provider_pin=ArtifactPin(
            role="provider",
            target=provider.identity,
            artifact_digest=provider_digest(provider).tagged,
        ),
        interface_pin=provider.pins[0],
        policy_pin=source._policy_pin(policy),
    )
    raw = procedure.definition.model_dump(mode="json", by_alias=True)
    raw["graph_format"] = 5
    raw["nodes"][0].update(
        {
            "interface_digest": registration.interface_digest,
            "implementation_digest": provider.implementations[0].implementation_digest,
            "request": {"url": URL},
        }
    )
    raw["nodes"][1]["fields"] = {"severity": f"$steps.{source.SOURCE_ALIAS}.derived.text"}
    definition = ProcedureDefinitionV5.model_validate(raw)
    procedure = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    invoker = AcquisitionInvoker(output)
    operator = source._Operator(invoker)
    real = manager.provider_runtime_operator()

    class Lane:
        def __getattr__(self, name):
            return getattr(operator if hasattr(operator, name) else real, name)

    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: Lane())
    from cruxible_client.contracts.provider_interfaces import (
        AcceptedProviderInterfaceRegistrationV1,
    )
    from cruxible_core.providers.provider_classifiers import (
        install_compiler_owned_provider_classifier,
    )

    install_compiler_owned_provider_classifier(
        AcceptedProviderInterfaceRegistrationV1(
            registration=registration,
            path=provider_interface_path(registration.interface_id),
            artifact_digest=provider_interface_digest(registration).tagged,
        )
    )

    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=tmp_path)
    intent = pb.procedure(definition=source._sdk_procedure_input(procedure)).prepare()
    assert not intent.refused, intent.diagnostics
    intent.submit()
    _approve_and_activate(http, instance_id, reviewer, intent.proposal.proposal_id)
    run = pb.accepted_procedure(procedure.identity.name).run()
    assert run.status == "succeeded", transport.get_playbill_procedure_run(instance_id, run.run_id)
    assert run.result["severity"]
    assert invoker.spawn_calls == 1
    state = transport.get_playbill_procedure_run(instance_id, run.run_id)
    assert state.receipt_digest is not None
    assert state.source_observations[0].capture_digest
    # The source result, its evidence identity and receipt survive public readback.
    assert state.result == run.result
    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_core.storage.cas import BodyAccessContext

    access = BodyAccessContext(principal_id="test", can_read_body=True)
    envelope = parse_capture_envelope(
        instance.body_store().read(state.source_observations[0].capture_digest, access=access)
    )
    material = instance.body_store().read(envelope.commitment.digest, access=access)
    assert material == base64.b64decode(output["content_base64"])


def test_sdk_call_uses_universal_protocol_without_producing_a_capture(
    playbill_http, tmp_path, monkeypatch
):
    from cruxible_client.authoring.examples import procedure_example
    from cruxible_client.authoring.inputs import CarriedContractInput
    from cruxible_client.contracts.procedures.contract_schema import PropertySchema
    from cruxible_client.contracts.provider_interfaces import (
        AcceptedProviderInterfaceRegistrationV1,
        provider_interface_definition_digest,
    )
    from cruxible_core.providers.provider_classifiers import PROVIDER_BUCKET_CLASSIFIER_REGISTRY
    from tests.core_support._p2b1_support import DemoSizeClassifier, interface_registration

    http, instance_id, reviewer = playbill_http
    manager = get_playbill_manager()
    instance = manager.get(instance_id)
    schema = {"fields": {"size": {"type": "int"}}, "allow_extra": False}
    preimage = canonical_bytes(
        {"effect_class": "external_read", "contracts": {"input": schema, "output": schema}}
    ).hex()
    registration = interface_registration().model_copy(
        update={
            "interface_bytes_hex": preimage,
            "interface_digest": provider_interface_definition_digest(preimage),
        }
    )
    provider = web_provider(registration, {"demo.small": "size=*"})
    proposal = source.submit_member_candidate(
        instance,
        members={
            provider_interface_path(registration.interface_id): render_provider_interface(
                registration
            ),
            provider_path(provider.identity.name): render_provider(provider),
        },
        actor_id="operator",
        proposal_name="call-deployment",
        proposal_family="procedure",
        timestamp=source.ACCEPT_STAMP,
    ).proposal
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    _approve_and_activate(http, instance_id, reviewer, proposal.admission.proposal_id)
    PROVIDER_BUCKET_CLASSIFIER_REGISTRY.install(
        AcceptedProviderInterfaceRegistrationV1(
            registration=registration,
            path=provider_interface_path(registration.interface_id),
            artifact_digest=provider_interface_digest(registration).tagged,
        ),
        DemoSizeClassifier(),
    )
    invoker = AcquisitionInvoker({"size": 1})
    operator = source._Operator(invoker)
    real = manager.provider_runtime_operator()

    class Lane:
        def __getattr__(self, name):
            return getattr(operator if hasattr(operator, name) else real, name)

    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: Lane())
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=tmp_path)
    example = procedure_example()

    def carried(role):
        return {"kind": "carried_contract", "name": "size", "role": role}

    raw = {
        **example.definition,
        "graph_format": 5,
        "returns": "result",
        "contract_out": carried("contract-out"),
        "nodes": [
            {
                "kind": "call",
                "node_id": "invoke",
                "as": "result",
                "input": {"size": 1},
                "provider": {
                    "kind": "accepted",
                    "role": "provider",
                    "target": provider.identity.qualified,
                },
                "interface": {
                    "kind": "accepted",
                    "role": "provider-interface",
                    "target": registration.identity.qualified,
                },
                "interface_digest": registration.interface_digest,
                "implementation_digest": provider.implementations[0].implementation_digest,
                "contract_in": carried("contract-in"),
                "contract_out": carried("contract-out"),
            }
        ],
    }
    raw["budget"] = {
        **raw["budget"],
        "max_items": None,
        "max_provider_calls": 1,
        "max_capture_bytes": 65536,
    }
    raw["hard_caps"] = {**raw["hard_caps"], "max_provider_calls": 2, "max_capture_bytes": 131072}
    authored = example.model_copy(
        update={
            "definition": raw,
            "contracts": (
                next(item for item in example.contracts if item.name == "empty-input"),
                CarriedContractInput(name="size", fields={"size": PropertySchema(type="int")}),
            ),
        }
    )
    intent = pb.procedure(definition=authored).prepare()
    assert not intent.refused, intent.diagnostics
    intent.submit()
    _approve_and_activate(http, instance_id, reviewer, intent.proposal.proposal_id)
    run = pb.accepted_procedure("replace-me").run()
    state = transport.get_playbill_procedure_run(instance_id, run.run_id)
    assert run.status == "succeeded", str(state.terminal)
    assert run.result == {"size": 1}
    assert state.source_observations == []
    assert invoker.spawn_calls == 1
