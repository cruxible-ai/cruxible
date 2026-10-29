"""Package-owned registrations preserve old authority and describe local readiness."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    Provider,
    ProviderRuntimeArtifactPayload,
    ProviderRuntimeArtifactPayloadV1,
    ProviderRuntimeManifest,
    ProviderV2,
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
from tests.core_support._p2b1_support import provider_v2


def package_provider(*, engine: bool = True) -> Provider:
    old = provider_v2()
    document = old.runtime_artifact.model_dump(mode="json")
    document["schema_version"] = 2
    document["manifest"]["implementations"][0]["backends"] = ["local_env", "container"]
    document["manifest"]["implementations"][0]["declared_endpoints"] = [
        "dynamic:target-from-configuration"
    ]
    # Require the "engine" extra explicitly, so a materialization pinned
    # without it (``engine=False`` below) is genuinely unavailable.
    document["manifest"]["implementations"][0]["requires_extras"] = ["engine"]
    document["manifest_digest"] = provider_manifest_digest(
        ProviderRuntimeManifest.model_validate(document["manifest"])
    )
    document["local_env"]["materialization_digests"] = (
        {"linux-cp311": "sha256:" + "b" * 64}
        if not engine
        else {"linux-cp311+engine": "sha256:" + "c" * 64}
    )
    payload = ProviderRuntimeArtifactPayload.model_validate(document)
    # NamedProviderBuild (what the shared provider_v2() fixture now returns)
    # carries `capture_contracts` (named refs) and keeps `capture_contract_digests`
    # as a ClassVar; the bare Provider format this test probes wants the
    # opposite (a real `capture_contract_digests` field, no `capture_contracts`).
    base = {k: v for k, v in old.model_dump(mode="json").items() if k != "capture_contracts"}
    return Provider.model_validate(
        {
            **base,
            "identity": {"kind": "Provider", "name": "demo-provider"},
            "capture_contract_digests": list(old.capture_contract_digests),
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


def test_advertised_but_unprepared_implementation_refuses_before_reading_environment(
    tmp_path: Path,
) -> None:
    provider = package_provider(engine=False)
    accepted_provider = AcceptedProvider(
        path=provider_path(provider.identity.name),
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
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
            accepted_provider, provider.operations[0], "sha256:" + "b" * 64, deployment
        )
    assert failure.value.code == "no_compatible_artifact"


def test_new_registration_formats_require_the_explicit_compiler_succession() -> None:
    # ProviderInterfaceRegistration (the "interface" package-registration kind
    # this test used to also parametrize over) is removed (B2 C12); only the
    # provider package-registration format remains to probe here.
    value = package_provider()
    path, raw = provider_path(value.identity.name), render_provider(value)
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


def test_prepared_environment_can_supply_a_superset_of_operation_extras() -> None:
    provider = package_provider()
    payload = provider.runtime_artifact.model_copy(
        update={
            "local_env": provider.runtime_artifact.local_env.model_copy(
                update={
                    "materialization_digests": {"linux-cp311+engine+other": "sha256:" + "b" * 64}
                }
            )
        }
    )
    records = provider_expected_implementation_records(payload)
    assert (
        records[0].materialization_references[0].environment_pin_key == "linux-cp311+engine+other"
    )
    # A pin key is a superset match only up to what it actually carries: a
    # manifest requiring an extra ("gpu") the pin key lacks still refuses --
    # for an *advertised* (not-yet-prepared) artifact, where an unmatched
    # local_env backend is a hard error rather than silently contributing no
    # reference (the available-artifact behaviour exercised above).
    old_dict = payload.model_dump(mode="json")
    old_dict["schema_version"] = 1
    old_dict["manifest"]["schema_version"] = 1
    old_dict["manifest"]["implementations"][0]["requires_extras"] = ["gpu"]
    old_dict["manifest"]["implementations"][0]["declared_endpoints"] = []
    old = ProviderRuntimeArtifactPayloadV1.model_validate(old_dict)
    with pytest.raises(ValueError, match="materialization_reference_missing"):
        provider_expected_implementation_records(old)


def test_an_index_default_never_requires_its_path_dependency_wheels() -> None:
    """kit build --default-provider ships none of the default's files (C1)."""

    from cruxible_core.errors import RequestRefusedError
    from cruxible_core.providers.package_inspection import check_path_dependencies

    runtime = {"cruxible-provider-runtime": "0.4.0"}
    # A default installs by name: the runtime its lock names by path is the index's.
    check_path_dependencies("cruxible-provider-web", runtime, {}, delivery="index")
    # A bundled package still carries it.
    with pytest.raises(RequestRefusedError) as refused:
        check_path_dependencies("cruxible-provider-web", runtime, {}, delivery="bundled")
    assert refused.value.error_code == "cruxible.kit.provider_dependency_missing"
    # A wheel that is given must still be exactly the locked one, either way.
    for delivery in ("index", "bundled"):
        with pytest.raises(RequestRefusedError) as mismatched:
            check_path_dependencies(
                "cruxible-provider-web",
                runtime,
                {"cruxible-provider-runtime": "0.3.0"},
                delivery=delivery,  # type: ignore[arg-type]
            )
        assert mismatched.value.error_code == "cruxible.kit.provider_dependency_mismatch"
