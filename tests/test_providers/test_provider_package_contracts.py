"""Package-owned registrations preserve old authority and describe local readiness."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    ProviderClassifierCodeV1,
    ProviderInterfaceRegistrationV2,
    evaluate_provider_interface_law,
    provider_interface_digest,
    provider_interface_path,
    provider_package_classifier_digest,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderRuntimeArtifactPayloadV2,
    ProviderRuntimeManifestV2,
    ProviderV2,
    ProviderV3,
    provider_digest,
    provider_expected_implementation_records,
    provider_manifest_digest,
    provider_path,
    render_provider,
)
from cruxible_core.compiler.compiler import (
    PROVIDER_CONTRACT_COMPILER,
    PROVIDER_PACKAGE_COMPILER,
    artifact_kinds_for_compiler,
    projection_registry_for_compiler,
)
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from cruxible_core.compiler.upgrades import upgrade_law
from cruxible_core.providers.provider_local_runtime import (
    LocalProviderDeploymentV1,
    LocalProviderExecutionDriver,
    ProviderLocalRuntimeRefused,
)
from tests.core_support._p2b1_support import (
    interface_fixture,
    interface_registration,
    provider_v2,
)


def package_interface() -> ProviderInterfaceRegistrationV2:
    old = interface_registration()
    code = ProviderClassifierCodeV1(
        entrypoint="demo.classifier:classify", source_digest="sha256:" + "a" * 64
    )
    return ProviderInterfaceRegistrationV2.model_validate(
        {
            **old.model_dump(mode="json"),
            "artifact_format": "playbill-provider-interface-v2",
            "classifier_code": code,
            "conformance_fixtures": (interface_fixture(),),
            "classifier_digest": provider_package_classifier_digest(
                classifier_identity=old.classifier_identity,
                classifier_version=old.classifier_version,
                conformance_fixture_set_digest=old.conformance_fixture_set_digest,
                code=code,
            ),
        }
    )


def package_provider(*, engine: bool = True) -> ProviderV3:
    old = provider_v2()
    document = old.runtime_artifact.model_dump(mode="json")
    document["schema_version"] = 2
    document["manifest"]["implementations"][0]["backends"] = ["local_env", "container"]
    document["manifest"]["implementations"][0]["declared_endpoints"] = [
        "dynamic:target-from-configuration"
    ]
    document["manifest_digest"] = provider_manifest_digest(
        ProviderRuntimeManifestV2.model_validate(document["manifest"])
    )
    if not engine:
        document["local_env"]["materialization_digests"] = {"linux-cp311": "sha256:" + "b" * 64}
    payload = ProviderRuntimeArtifactPayloadV2.model_validate(document)
    return ProviderV3.model_validate(
        {
            **old.model_dump(mode="json"),
            "artifact_format": "playbill-provider-v3",
            "runtime_artifact": payload,
            "implementations": provider_expected_implementation_records(payload),
        }
    )


def test_local_registration_keeps_the_full_manifest_without_inventing_container_pins() -> None:
    provider = package_provider()
    assert provider.runtime_artifact.manifest.implementations[0].backends == (
        "local_env",
        "container",
    )
    assert provider.implementations[0].backend_kinds == ("local_env",)
    assert provider.runtime_artifact.container is None
    unavailable = package_provider(engine=False)
    assert unavailable.implementations == ()
    assert len(unavailable.runtime_artifact.manifest.implementations) == 1
    with pytest.raises(ValidationError):
        ProviderV2.model_validate(provider.model_dump(mode="json"))


def test_package_classifier_authority_retains_fixture_bytes_and_executable_identity() -> None:
    registration = package_interface()
    assert (
        evaluate_provider_interface_law(
            registration,
            path=provider_interface_path(registration.interface_id),
            predecessor=None,
            conformance_fixtures={},
        ).verdict
        == "accepted"
    )
    accepted = AcceptedProviderInterfaceRegistrationV1(
        path=provider_interface_path(registration.interface_id),
        registration=registration,
        artifact_digest=provider_interface_digest(registration).tagged,
    )
    assert accepted.model_dump(mode="json")["registration"]["classifier_code"]
    data = registration.model_dump(mode="json")
    data["conformance_fixtures"][0]["canonical_input"]["size"] = 4
    with pytest.raises(ValidationError, match="fixture bytes"):
        ProviderInterfaceRegistrationV2.model_validate(data)
    data = registration.model_dump(mode="json")
    data["classifier_code"]["source_digest"] = "sha256:" + "b" * 64
    with pytest.raises(ValidationError, match="classifier digest"):
        ProviderInterfaceRegistrationV2.model_validate(data)


def test_advertised_but_unprepared_implementation_refuses_before_reading_environment(
    tmp_path: Path,
) -> None:
    provider = package_provider(engine=False)
    registration = package_interface()
    accepted_provider = AcceptedProviderV1(
        path=provider_path(provider.identity.name),
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
    accepted_interface = AcceptedProviderInterfaceRegistrationV1(
        path=provider_interface_path(registration.interface_id),
        registration=registration,
        artifact_digest=provider_interface_digest(registration).tagged,
    )
    missing = tmp_path / "not-installed"
    deployment = LocalProviderDeploymentV1(
        deployment_digest="sha256:" + "a" * 64,
        distribution_path=missing,
        lock_path=missing,
        environment_path=missing,
        environment_manifest_path=missing,
        environment_pin_key="linux-cp311",
        interpreter_path=missing,
        provider_runtime_version="0.2.0",
    )
    with pytest.raises(ProviderLocalRuntimeRefused) as failure:
        LocalProviderExecutionDriver().bind(
            accepted_provider, accepted_interface, "sha256:" + "b" * 64, deployment
        )
    assert failure.value.code == "no_compatible_artifact"


@pytest.mark.parametrize("kind", ["provider", "interface"])
def test_new_registration_formats_require_the_explicit_compiler_succession(kind: str) -> None:
    if kind == "provider":
        value = package_provider()
        path, raw = provider_path(value.identity.name), render_provider(value)
    else:
        registration = package_interface()
        path, raw = (
            provider_interface_path(registration.interface_id),
            render_provider_interface(registration),
        )
    for compiler in (PROVIDER_CONTRACT_COMPILER, PROVIDER_PACKAGE_COMPILER):

        def project():
            return parse_projection_tree(
                {path: raw},
                registry=projection_registry_for_compiler(compiler),
                artifact_kinds=artifact_kinds_for_compiler(compiler),
            )

        if compiler == PROVIDER_CONTRACT_COMPILER:
            with pytest.raises(ProjectionFormatError, match="provider-package compiler"):
                project()
        else:
            assert project().envelopes
    assert (
        upgrade_law(PROVIDER_CONTRACT_COMPILER, PROVIDER_PACKAGE_COMPILER).coordinate.identifier
        == "playbill.compiler-upgrade.v1"
    )
    with pytest.raises(ValueError, match="unsupported compiler transition"):
        upgrade_law(PROVIDER_PACKAGE_COMPILER, PROVIDER_CONTRACT_COMPILER)
