"""An agent authors and runs a Source through the SDK, then reads its Capture.

Only the local adapter process is substituted. Provider/interface deployment is
governed setup; authoring, admission, output checks, capture construction and
public readback are the actual services.
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from cruxible_client import Cruxible
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    CaptureContract,
    ProviderResultToExternalCapture,
    SourceEffectiveTime,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.named_references import name_provider
from cruxible_client.contracts.procedures.acquisition_rules import AcquisitionDefault
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    ProcedureOwnedContract,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import (
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
    SourceNode,
)
from cruxible_client.contracts.provider_operations import ProviderOperation
from cruxible_client.contracts.providers import (
    ProviderDistributionRef,
    ProviderImplementationManifest,
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    ProviderRuntimeManifest,
    provider_path,
    render_provider,
)
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._light_capture_support import http_contract
from tests.support.provider_builds import as_build
from tests.test_procedures import test_procedure_source_runs as source
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

URL = "https://fixture.invalid/api/v1/measurements.json"


def web_provider(
    operation: ProviderOperation,
    contracts: tuple[Contract, ...],
    *,
    selectors: dict[str, str] | None = None,
):
    """A fresh build serving ``operation``, declaring exactly ``contracts``.

    ``cruxible_core.providers.web_fetch`` (the core-side web.fetch interface
    registration) is gone (B2, C12 -- ProviderInterface removed); a build now
    declares its operation and Contracts itself, through the ordinary
    operations/package-registration path, rather than pinning a shared
    interface artifact.
    """

    if selectors is None:
        selectors = {}

    def digests(ref: object) -> str | None:
        for item in contracts:
            if (ref.name, ref.version) == (item.name, item.version):  # type: ignore[attr-defined]
                return contract_digest(item).tagged
        return None

    manifest = ProviderRuntimeManifest(
        provider_id="web-fetch-demo",
        distribution=ProviderDistributionRef(name="web-fetch-demo", version="1.0.0"),
        supported_protocol_majors=(1,),
        implementations=(
            ProviderImplementationManifest(
                interface_id=operation.name,
                interface_digest="sha256:" + "9" * 64,
                entrypoint="cruxible_provider_web.fetch:WebFetch",
                backends=("local_env",),
                declared_input_buckets=tuple(sorted(selectors.values())),
                bucket_conformance={selector: name for name, selector in selectors.items()},
                declared_endpoints=("https://fixture.invalid",),
                deterministic=False,
                side_effects=False,
            ),
        ),
    )
    document = PackageRegistrationDocumentV1(
        schema_version=2, manifest=manifest, operations=(operation,)
    )
    build = document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name="web-fetch-demo",
            version="1.0.0",
            filename="web_fetch_demo-1.0.0-py3-none-any.whl",
            sha256="sha256:" + "0" * 64,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "0" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "0" * 64},
        ),
        control_domain="web-fetch-demo",
        operations=document.pinned_operations(digests),
    )
    return name_provider(as_build(build))


# F02: every operation output is checked against its own pinned Contract, so
# this file's fetch operation declares a Contract mirroring
# ProviderResultToExternalCapture's own fields exactly (closed), the same way
# a real external-read provider's operation would.
_FETCH_COORDINATE = Contract.model_validate(
    {"name": "test.sdk-fetch-coordinate", "version": 1, "fields": {"status": {"type": "int"}}}
)
_FETCH_SELECTOR = Contract.model_validate(
    {
        "name": "test.sdk-fetch-selector",
        "version": 1,
        "fields": {"whole": {"type": "bool", "optional": True}},
    }
)
_FETCH_EFFECTIVE = Contract.model_validate(
    {
        "name": "test.sdk-fetch-effective",
        "version": 1,
        "fields": {
            "tag": {"type": "string"},
            "effective_from": {"type": "datetime"},
            "effective_until": {"type": "datetime"},
        },
    }
)


def _pinned_nested(contract: Contract) -> dict[str, object]:
    return {
        "kind": "contract",
        "contract": {
            "name": contract.name,
            "version": contract.version,
            "digest": contract_digest(contract).tagged,
        },
    }


_FETCH_INPUT = Contract.model_validate(
    {"name": "test.sdk-fetch-input", "version": 1, "fields": {"url": {"type": "string"}}}
)
_FETCH_OUTPUT = Contract.model_validate(
    {
        "name": "test.sdk-fetch-output",
        "version": 1,
        "fields": {
            "tag": {"type": "string"},
            "source_identity": {"type": "string"},
            "coordinate_type": {"type": "string"},
            "coordinate": {"type": _pinned_nested(_FETCH_COORDINATE)},
            "selector_type": {"type": "string"},
            "selector": {"type": _pinned_nested(_FETCH_SELECTOR)},
            "replayability": {"type": "string"},
            "content_encoding": {"type": "string"},
            "content_base64": {"type": "string"},
            "byte_length": {"type": "int"},
            "bytes_digest": {"type": "string"},
            "observed_at": {"type": "datetime"},
            "source_effective_time": {"type": _pinned_nested(_FETCH_EFFECTIVE)},
        },
    }
)
_FETCH_CONTRACTS = (
    _FETCH_INPUT,
    _FETCH_COORDINATE,
    _FETCH_SELECTOR,
    _FETCH_EFFECTIVE,
    _FETCH_OUTPUT,
)
_FETCH_OPERATION = ProviderOperation.model_validate(
    {
        "name": "fetch",
        "in": f"{_FETCH_INPUT.name}@{_FETCH_INPUT.version}",
        "out": f"{_FETCH_OUTPUT.name}@{_FETCH_OUTPUT.version}",
        "effect": "external_read",
        "entrypoint": "cruxible_provider_web.fetch:WebFetch",
    }
)
_ALIAS = "fetched"


def _fetch_contract_files() -> dict[str, bytes]:
    return {
        contract_path(item.name, item.version): render_contract(item) for item in _FETCH_CONTRACTS
    }


def acquisition_result():
    content = canonical_bytes({"derived": {"text": "critical"}, "retrieved": {"url": URL}})
    now = datetime.now(UTC)
    return ProviderResultToExternalCapture(
        source_identity="web.response",
        coordinate_type="http-response-v1",
        coordinate={"status": 200},
        selector_type="whole-response-v1",
        selector={},
        replayability="attested_only",
        content_base64=base64.b64encode(content).decode(),
        byte_length=len(content),
        bytes_digest="sha256:" + hashlib.sha256(content).hexdigest(),
        observed_at=now,
        source_effective_time=SourceEffectiveTime(
            effective_from=now, effective_until=now + timedelta(microseconds=1)
        ),
    ).model_dump(mode="json")


def _fetch_procedure(
    contract: CaptureContract, policy: AcquisitionDefault, *, request: dict[str, object]
) -> ProcedureArtifact:
    """One accepted Procedure whose Source slot fetches ``request`` and shapes severity."""

    run_input = ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name="sdk-fetch-run-input"),
        schema=ContractSchema(fields={}, allow_extra=True),
    )
    run_output = ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name="sdk-fetch-run-output"),
        schema=ContractSchema(fields={"severity": PropertySchema(type="string")}),
    )
    run_input_pin = ArtifactPin(
        role="contract-in",
        target=run_input.identity,
        artifact_digest=procedure_owned_contract_digest(run_input).tagged,
    )
    run_output_pin = ArtifactPin(
        role="contract-out",
        target=run_output.identity,
        artifact_digest=procedure_owned_contract_digest(run_output).tagged,
    )
    capture_pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    definition = ProcedureDefinition(
        name="sdk-fetch-triage",
        description="Fetch one external record and shape its severity.",
        contract_in=run_input_pin,
        contract_out=run_output_pin,
        slots={
            "fetch": {
                "in": f"{_FETCH_INPUT.name}@{_FETCH_INPUT.version}",
                "out": f"{_FETCH_OUTPUT.name}@{_FETCH_OUTPUT.version}",
                "effect": "external_read",
            }
        },
        nodes=(
            SourceNode(
                node_id="fetch",
                capture_contract=capture_pin,
                slot="fetch",
                request=request,
                as_=_ALIAS,
                next="shape",
            ),
            ProjectNode(
                node_id="shape",
                fields={"severity": f"$steps.{_ALIAS}.derived.text"},
                contract_out=run_output_pin,
                as_="result",
            ),
        ),
        returns="result",
        budget=ProcedureBudget(
            wall_clock=source.RUN_WALL_CLOCK,
            max_provider_calls=2,
            max_capture_bytes=1_048_576,
            max_items=None,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=source.MAX_WALL_CLOCK,
            max_provider_calls=4,
            max_capture_bytes=2_097_152,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=2,
    )
    procedure = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=tuple(
            sorted(
                (run_input_pin, run_output_pin, capture_pin),
                key=lambda pin: (
                    pin.role.encode("utf-8"),
                    pin.target.qualified.encode("utf-8"),
                    pin.artifact_digest.encode("ascii"),
                ),
            )
        ),
        owned_contracts=tuple(
            sorted(
                (run_input, run_output),
                key=lambda item: canonical_bytes(item.model_dump(mode="json", by_alias=True)),
            )
        ),
        activation_policy="drain",
        acquisition_defaults={_ALIAS: policy},
    )
    from cruxible_client.contracts.procedures.names import name_procedure

    return name_procedure(procedure)


class AcquisitionInvoker(source._WorkspaceInvoker):
    def __init__(self, output, expected_input=None):
        super().__init__()
        self.output = output
        self.expected_input = expected_input
        self.inputs = []

    def invoke_provider(self, *, occurrence, context, invocation_id, bound, deadline):
        from cruxible_client.contracts.provider_execution import ProviderEgressObservation
        from cruxible_core.providers.provider_local_runtime import ProviderDriverOutcomeV1
        from cruxible_core.providers.provider_runtime_contract import (
            ProviderRuntimeResultEnvelope,
        )

        self.spawn_calls += 1
        self.inputs.append(context.input)
        if occurrence.occurrence_kind == "source":
            assert context.input["url"] == URL
            assert occurrence.operation.out.name == _FETCH_OUTPUT.name
            assert occurrence.operation.out.version == _FETCH_OUTPUT.version
        else:
            assert occurrence.occurrence_kind == "call"
            assert context.input == (self.expected_input or {"size": 1})
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelope(
                protocol_version="2.0", run_id=context.run_id, status="ok", output=self.output
            ),
            stderr="",
            duration_seconds=0.001,
            verified_binding=bound.binding,
            egress=ProviderEgressObservation(
                observer_backend="test-attribution", observer_grade="attribution"
            ),
        )


def _lane(monkeypatch, manager, invoker):
    operator = source._Operator(invoker)
    real = manager.provider_runtime_operator()

    class Lane:
        def __getattr__(self, name):
            return getattr(operator if hasattr(operator, name) else real, name)

    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: Lane())


@pytest.mark.parametrize(
    "over_limit",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skip(
                reason=(
                    "exercises an interface declaring an unchecked json-typed field "
                    "alongside a list field (provider-runtime 0.2.x json_schema "
                    "escape); B1 operation Contracts (shapes.py FieldType) have no "
                    "'json'/unchecked field kind, only the closed set in "
                    "shapes.FieldType, so this scenario has no equivalent to migrate to"
                )
            ),
        ),
    ],
    ids=["ordinary", "source-over-max-items"],
)
@pytest.mark.parametrize("adapter", [False, True], ids=["protocol", "web-adapter"])
def test_sdk_source_retains_and_reads_acquisition(
    playbill_http, tmp_path, monkeypatch, adapter, over_limit
):
    output = acquisition_result()
    if adapter:
        web = pytest.importorskip(
            "cruxible_provider_web.fetch", reason="cross-repo web provider integration"
        )
        from cruxible_provider_runtime.egress import EgressRecorder
        from cruxible_provider_runtime.protocol import Budgets
        from cruxible_provider_runtime.provider_api import ProviderRunContext
        from cruxible_provider_web.interfaces import FETCH_PREIMAGE

        del FETCH_PREIMAGE
        result = web.WebFetch()(
            ProviderRunContext(
                run_id="sdk-acquisition",
                input={"url": URL},
                interface_id="web.fetch",
                interface_digest="sha256:" + "9" * 64,
                implementation_digest="sha256:" + "a" * 64,
                declared_endpoints=("https://fixture.invalid",),
                input_bucket="legacy=none",
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
    request = {"url": URL}
    provider = web_provider(_FETCH_OPERATION, _FETCH_CONTRACTS)
    contract = http_contract(max_bytes=1_000_000)
    policy = source._policy()
    procedure = _fetch_procedure(contract, policy, request=request)

    proposal = source.submit_member_candidate(
        instance,
        members={
            provider_path(provider.identity.name): render_provider(provider),
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
            **_fetch_contract_files(),
        },
        actor_id="operator",
        proposal_name="acquisition-deployment",
        proposal_family="procedure",
        timestamp=source.ACCEPT_STAMP,
    ).proposal
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    _approve_and_activate(http, instance_id, reviewer, proposal.admission.proposal_id)

    invoker = AcquisitionInvoker(output)
    _lane(monkeypatch, manager, invoker)

    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=tmp_path)
    intent = pb.procedure(definition=source._sdk_procedure_input(procedure)).prepare()
    assert not intent.refused, intent.diagnostics
    intent.submit(accept="never")
    _approve_and_activate(http, instance_id, reviewer, intent.proposal.proposal_id)
    run = pb.accepted_procedure(procedure.identity.name).run()
    assert run.status == "succeeded", transport.get_procedure_run(instance_id, run.run_id)
    assert run.result["severity"]
    assert invoker.spawn_calls == 1
    assert invoker.inputs == [request]
    state = transport.get_procedure_run(instance_id, run.run_id)
    assert state.receipt_digest is not None
    assert state.source_observations[0].capture_digest
    # The source result, its evidence identity and receipt survive public readback.
    assert state.result == run.result.model_dump()
    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_core.storage.cas import BodyAccessContext

    access = BodyAccessContext(principal_id="test", can_read_body=True)
    envelope = parse_capture_envelope(
        instance.body_store().read(state.source_observations[0].capture_digest, access=access)
    )
    material = instance.body_store().read(envelope.commitment.digest, access=access)
    assert material == base64.b64decode(output["content_base64"])


@pytest.mark.parametrize(
    "defaulted",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skip(
                reason=(
                    "exercises an operation Contract field with a declared default "
                    "value (provider-runtime 0.2.x ContractSchema/PropertySchema "
                    "'default'); B1 operation Contracts (shapes.ContractField) only "
                    "carry type/optional/description, no default value, so the SDK "
                    "fill-in-the-default behavior this test checks has no equivalent"
                )
            ),
        ),
    ],
    ids=["ordinary", "interface-defaults"],
)
def test_sdk_call_uses_universal_protocol_without_producing_a_capture(
    playbill_http, tmp_path, monkeypatch, defaulted
):
    from cruxible_client.authoring.examples import procedure_example
    from cruxible_client.authoring.inputs import CarriedContractInput
    from cruxible_client.contracts.procedures.contract_schema import PropertySchema

    http, instance_id, reviewer = playbill_http
    manager = get_playbill_manager()
    instance = manager.get(instance_id)
    call_input = Contract.model_validate(
        {"name": "test.sdk-call-input", "version": 1, "fields": {"size": {"type": "int"}}}
    )
    call_output = Contract.model_validate(
        {"name": "test.sdk-call-output", "version": 1, "fields": {"size": {"type": "int"}}}
    )
    operation = ProviderOperation.model_validate(
        {
            "name": "invoke",
            "in": f"{call_input.name}@{call_input.version}",
            "out": f"{call_output.name}@{call_output.version}",
            "effect": "external_read",
            "entrypoint": "cruxible_provider_web.fetch:WebFetch",
        }
    )
    provider = web_provider(
        operation, (call_input, call_output), selectors={"demo.small": "size=*"}
    )
    proposal = source.submit_member_candidate(
        instance,
        members={
            provider_path(provider.identity.name): render_provider(provider),
            contract_path(call_input.name, call_input.version): render_contract(call_input),
            contract_path(call_output.name, call_output.version): render_contract(call_output),
        },
        actor_id="operator",
        proposal_name="call-deployment",
        proposal_family="procedure",
        timestamp=source.ACCEPT_STAMP,
    ).proposal
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    _approve_and_activate(http, instance_id, reviewer, proposal.admission.proposal_id)
    expected_input = {"size": 1}
    invoker = AcquisitionInvoker({"size": 1}, expected_input=expected_input)
    _lane(monkeypatch, manager, invoker)
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=tmp_path)
    example = procedure_example()

    def carried(role):
        return {"kind": "carried_contract", "name": "size", "role": role}

    raw = {
        **example.definition,
        "graph_format": 6,
        "returns": "result",
        "contract_out": carried("contract-out"),
        "slots": {
            "invoke": {
                "in": f"{call_input.name}@{call_input.version}",
                "out": f"{call_output.name}@{call_output.version}",
                "effect": "external_read",
            }
        },
        "nodes": [
            {
                "kind": "call",
                "node_id": "invoke",
                "as": "result",
                "input": {"size": 1},
                "slot": "invoke",
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
                CarriedContractInput(
                    name="size",
                    fields={"size": PropertySchema(type="int")},
                ),
            ),
        }
    )
    intent = pb.procedure(definition=authored).prepare()
    assert not intent.refused, intent.diagnostics
    intent.submit(accept="never")
    _approve_and_activate(http, instance_id, reviewer, intent.proposal.proposal_id)
    run = pb.accepted_procedure("replace-me").run()
    state = transport.get_procedure_run(instance_id, run.run_id)
    assert run.status == "succeeded", str(state.terminal)
    assert run.result.size == 1
    assert state.source_observations == []
    assert invoker.spawn_calls == 1
