"""Provider-v2 mirror, implementation identity, and succession laws."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderImplementationManifestV1,
    ProviderV1,
    ProviderV2,
    evaluate_provider_law,
    parse_provider,
    provider_digest,
    provider_implementation_digest,
    provider_path,
    render_provider,
)
from tests.test_playbill._p2b1_support import (
    accepted_interface,
    provider_v2,
)


def test_provider_v2_round_trip_external_digests_and_filtered_materializations() -> None:
    provider = provider_v2()

    assert provider.runtime_artifact.manifest_digest == (
        "sha256:0229d1a9dd68fdc7f89bcb0424304bcc33ecbfa9ba3693bf21a8a8576fceaa1f"
    )
    implementation = provider.implementations[0]
    assert implementation.implementation_digest == (
        "sha256:dec6763f6709a713f1399871ec416d19a5d76481b13b8ac1f8701fb7d28b4658"
    )
    assert tuple(
        reference.environment_pin_key
        for reference in implementation.materialization_references
        if reference.kind == "local_env"
    ) == ("linux-cp311+engine",)

    content = render_provider(provider)
    assert parse_provider(content, path=provider_path("demo-provider")) == provider
    assert provider_digest(provider).tagged.startswith("sha256:")


def test_implementation_digest_is_backend_invariant_and_four_field_exact() -> None:
    provider = provider_v2()
    row = provider.implementations[0]
    manifest = provider.runtime_artifact.manifest.implementations[0]

    assert row.implementation_digest == provider_implementation_digest(
        interface_id=manifest.interface_id,
        interface_digest=manifest.interface_digest,
        entrypoint=manifest.entrypoint,
        distribution_sha256=provider.runtime_artifact.distribution.sha256,
    )
    assert row.implementation_digest != provider_implementation_digest(
        interface_id=manifest.interface_id,
        interface_digest=manifest.interface_digest,
        entrypoint="demo.runtime:OtherProvider",
        distribution_sha256=provider.runtime_artifact.distribution.sha256,
    )


def test_provider_v2_refuses_manifest_and_normalized_table_divergence() -> None:
    provider = provider_v2()
    payload = provider.model_dump(mode="json")
    payload["runtime_artifact"]["manifest_digest"] = f"sha256:{'0' * 64}"
    with pytest.raises(ValidationError, match="manifest_divergence"):
        ProviderV2.model_validate(payload)

    payload = provider.model_dump(mode="json")
    payload["runtime_artifact"]["local_env"] = None
    with pytest.raises(ValidationError, match="backend_pin_missing: local_env"):
        ProviderV2.model_validate(payload)

    payload = provider.model_dump(mode="json")
    payload["implementations"][0]["implementation_digest"] = f"sha256:{'1' * 64}"
    with pytest.raises(ValidationError, match="implementation_digest_mismatch"):
        ProviderV2.model_validate(payload)


def test_provider_runtime_mirror_rejects_unknown_endpoint_forms() -> None:
    payload = provider_v2().runtime_artifact.manifest.implementations[0].model_dump(mode="json")
    payload["declared_endpoints"] = ["dynamic:not-a-runtime-form"]

    with pytest.raises(ValidationError, match="unknown dynamic endpoint form"):
        ProviderImplementationManifestV1.model_validate(payload)


def test_provider_v1_bytes_remain_version_discriminated() -> None:
    provider = provider_v2()
    payload = provider.model_dump(mode="json")
    payload.pop("runtime_artifact")
    payload.pop("implementations")
    payload["artifact_format"] = "playbill-provider-v1"
    historical = ProviderV1.model_validate(payload)
    content = render_provider(historical)

    assert json.loads(content)["artifact_format"] == "playbill-provider-v1"
    assert parse_provider(content, path=provider_path("demo-provider")) == historical
    assert provider_digest(historical) != provider_digest(provider)


def test_provider_v1_to_v2_succession_and_effect_parity() -> None:
    provider = provider_v2()
    historical_payload = provider.model_dump(mode="json")
    historical_payload.pop("runtime_artifact")
    historical_payload.pop("implementations")
    historical_payload["artifact_format"] = "playbill-provider-v1"
    historical = ProviderV1.model_validate(historical_payload)
    predecessor = AcceptedProviderV1(
        path=provider_path("demo-provider"),
        provider=historical,
        artifact_digest=provider_digest(historical).tagged,
    )
    successor = provider.model_copy(
        update={"lifecycle": ArtifactLifecycle(predecessor_digest=predecessor.artifact_digest)}
    )
    registration = accepted_interface()

    assert (
        evaluate_provider_law(
            successor,
            path=predecessor.path,
            predecessor=predecessor,
            interface_registrations={
                registration.registration.interface_id: registration,
            },
        ).verdict
        == "accepted"
    )

    mutation_registration = registration.model_copy(
        update={
            "registration": registration.registration.model_copy(
                update={"effect_class": "external_mutation"}
            )
        }
    )
    refused = evaluate_provider_law(
        successor,
        path=predecessor.path,
        predecessor=predecessor,
        interface_registrations={
            mutation_registration.registration.interface_id: mutation_registration,
        },
    )
    assert refused.diagnostics[0].code == "playbill.provider.effect_declaration_mismatch"

    unpinned = successor.model_copy(update={"pins": ()})
    refused = evaluate_provider_law(
        unpinned,
        path=predecessor.path,
        predecessor=predecessor,
        interface_registrations={registration.registration.identity.qualified: registration},
    )
    assert refused.diagnostics[0].code == "playbill.provider.interface_pin_missing"
