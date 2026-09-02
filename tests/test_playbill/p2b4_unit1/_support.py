"""Exact Capture-v2 fixtures shared by the Unit-1 self-attacks."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import (
    CaptureContractV1,
    ProviderResultToExternalCaptureV1,
    capture_contract_digest,
)
from cruxible_client.contracts.provider_execution import (
    ProviderBudgetTranslationV1,
    ProviderEgressObservationV1,
    ProviderExternalOccurrencePlanV1,
    ProviderInvocationOutcomeV1,
    ProviderInvocationReceiptV1,
    ProviderSecretBindingIdentityV1,
    ProviderSecretReceiptReferenceV1,
    ProviderSecretReferenceV1,
    ProviderSecretResolutionPlanV1,
    VerifiedProviderBindingV1,
    provider_secret_binding_identity_digest,
)
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from tests.test_playbill._pc_c_support import NOW, capture_contract


def digest(domain: str, value: str) -> str:
    return typed_digest(Sha256Value, domain, {"value": value}).tagged


@dataclass(frozen=True)
class ProviderCaptureFixture:
    store: ContentAddressedBodyStore
    contract: CaptureContractV1
    producer: ArtifactIdentity
    occurrence: ProviderExternalOccurrencePlanV1
    receipt: ProviderInvocationReceiptV1
    result: ProviderResultToExternalCaptureV1
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
    secret = ProviderSecretReferenceV1(
        realm="orders",
        name="reader",
        epoch="epoch-7",
        purpose="read",
        resolver_kind="file",
    )
    secret_identity_digest = provider_secret_binding_identity_digest(
        ProviderSecretBindingIdentityV1(realm=secret.realm, name=secret.name)
    )
    secret_plan = ProviderSecretResolutionPlanV1(
        references=(secret,),
        binding_identity_digests=(secret_identity_digest,),
    )
    budget = ProviderBudgetTranslationV1(
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
    local = VerifiedProviderBindingV1(
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
    occurrence = ProviderExternalOccurrencePlanV1(
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
    result = ProviderResultToExternalCaptureV1(
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
    egress = ProviderEgressObservationV1(
        declared_endpoints=local.declared_endpoints,
        observed_endpoints=local.declared_endpoints,
        observer_backend="sandbox",
        observer_grade="conformance",
    )
    receipt = ProviderInvocationReceiptV1(
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
        outcome=ProviderInvocationOutcomeV1(
            status="ok",
            outcome_class="ok",
            attribution="none",
        ),
        output=result.model_dump(mode="json"),
        egress=egress,
        fence_scope="process_group+descendant_sweep",
        secret_references=(
            ProviderSecretReceiptReferenceV1(
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
