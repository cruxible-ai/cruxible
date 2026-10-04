"""Exact Capture-v2 fixtures shared by the Unit-1 self-attacks."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import (
    CaptureContract,
    ProviderResultToExternalCapture,
    capture_contract_digest,
)
from cruxible_client.contracts.provider_execution import (
    ProviderBudgetTranslation,
    ProviderEgressObservation,
    ProviderExternalOccurrencePlan,
    ProviderInvocationOutcome,
    ProviderInvocationOutputDigest,
    ProviderInvocationReceipt,
    ProviderSecretBindingIdentity,
    ProviderSecretReceiptReference,
    ProviderSecretReference,
    ProviderSecretResolutionPlan,
    VerifiedProviderBinding,
    provider_invocation_output_digest,
    provider_secret_binding_identity_digest,
)
from cruxible_core.storage.cas import ContentAddressedBodyStore
from tests.core_support._pc_c_support import NOW, capture_contract


def digest(domain: str, value: str) -> str:
    return typed_digest(Sha256Value, domain, {"value": value}).tagged


@dataclass(frozen=True)
class ProviderCaptureFixture:
    store: ContentAddressedBodyStore
    contract: CaptureContract
    producer: ArtifactIdentity
    occurrence: ProviderExternalOccurrencePlan
    receipt: ProviderInvocationReceipt
    result: ProviderResultToExternalCapture
    bound_generation: str


def provider_capture_fixture(root: Path) -> ProviderCaptureFixture:
    cas_root = root / "cas"
    cas_root.mkdir(parents=True)
    store = ContentAddressedBodyStore(cas_root)
    contract = capture_contract()
    provider_digest = digest("provider", "source")
    interface_artifact_digest = digest("interface-artifact", "source")
    interface_digest = digest("interface", "source")
    implementation_digest = digest("implementation", "source")
    deployment_digest = digest("deployment", "source")
    materialization_digest = digest("materialization", "source")
    secret = ProviderSecretReference(
        realm="orders",
        name="reader",
        epoch="epoch-7",
        purpose="read",
        resolver_kind="file",
    )
    secret_identity_digest = provider_secret_binding_identity_digest(
        ProviderSecretBindingIdentity(realm=secret.realm, name=secret.name)
    )
    secret_plan = ProviderSecretResolutionPlan(
        references=(secret,),
        binding_identity_digests=(secret_identity_digest,),
    )
    budget = ProviderBudgetTranslation(
        remaining_wall_clock_microseconds=5_000_000,
        procedure_wall_clock_microseconds=5_000_000,
        hard_cap_wall_clock_microseconds=5_000_000,
        runtime_wall_clock_seconds=5,
        policy_output_bytes_cap=4096,
        runtime_output_bytes_cap=4096,
        max_provider_calls=1,
        max_items=4,
        result_bytes_cap=4096,
    )
    local = VerifiedProviderBinding(
        provider_artifact_digest=provider_digest,
        interface_artifact_digest=interface_artifact_digest,
        interface_id="orders.read",
        interface_digest=interface_digest,
        implementation_digest=implementation_digest,
        deployment_digest=deployment_digest,
        materialization_digest=materialization_digest,
        environment_manifest_digest=digest("environment", "source"),
        entrypoint="orders.source:Provider",
        declared_endpoints=("https://orders.example",),
    )
    occurrence = ProviderExternalOccurrencePlan(
        occurrence_path="source/orders",
        occurrence_kind="source",
        node_id="source-orders",
        input_name="orders",
        provider_artifact_digest=provider_digest,
        interface_artifact_digest=interface_artifact_digest,
        interface_id=local.interface_id,
        interface_digest=interface_digest,
        vocabulary_digest=digest("vocabulary", "source"),
        classifier_digest=digest("classifier", "source"),
        accepted_bucket_selectors=("kind=orders",),
        implementation_digest=implementation_digest,
        effect_class="external_read",
        capture_contract_digest=capture_contract_digest(contract).tagged,
        local_execution=local,
        secret_plan=secret_plan,
        budget_translation=budget,
        source_runtime_plan_digest=digest("source-runtime-plan", "source"),
    )
    body = b'{"order_id":7,"status":"settled"}'
    result = ProviderResultToExternalCapture(
        source_identity="commerce.production.orders",
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "0/16B6C50"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
        replayability="exact",
        content_base64=base64.b64encode(body).decode("ascii"),
        byte_length=len(body),
        bytes_digest="sha256:" + hashlib.sha256(body).hexdigest(),
        observed_at=NOW,
    )
    egress = ProviderEgressObservation(
        declared_endpoints=local.declared_endpoints,
        observed_endpoints=local.declared_endpoints,
        observer_backend="sandbox",
        observer_grade="conformance",
    )
    receipt = ProviderInvocationReceipt(
        invocation_id=digest("invocation", "source"),
        occurrence_path=occurrence.occurrence_path,
        run_id="run-b4-source",
        admission_binding_digest=digest("admission", "source"),
        provider_artifact_digest=provider_digest,
        implementation_digest=implementation_digest,
        materialization_digest=materialization_digest,
        deployment_digest=deployment_digest,
        interface_id=local.interface_id,
        interface_digest=interface_digest,
        protocol_version="1.0",
        input_bucket="kind=orders",
        capture_contract_digest=capture_contract_digest(contract).tagged,
        input_digest=digest("input", "source"),
        outcome=ProviderInvocationOutcome(
            status="ok",
            outcome_class="ok",
            attribution="none",
        ),
        output=ProviderInvocationOutputDigest(
            output_digest=provider_invocation_output_digest(result.model_dump(mode="json"))
        ).model_dump(mode="json"),
        egress=egress,
        fence_scope="process_group+descendant_sweep",
        secret_references=(
            ProviderSecretReceiptReference(
                binding_identity_digest=secret_identity_digest,
                purpose=secret.purpose,
            ),
        ),
        budget_translation=budget,
        duration_microseconds=25_000,
    )
    return ProviderCaptureFixture(
        store=store,
        contract=contract,
        producer=ArtifactIdentity(kind="Provider", name="orders.source"),
        occurrence=occurrence,
        receipt=receipt,
        result=result,
        bound_generation=digest("generation", "source"),
    )
