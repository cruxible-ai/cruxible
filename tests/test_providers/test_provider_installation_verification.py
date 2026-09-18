"""New installations verify once; historical V2 binding remains independent."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderRuntimeArtifactPayloadV2,
    ProviderV3,
    provider_digest,
    provider_expected_implementation_records,
    provider_path,
)
from cruxible_core.providers.provider_local_runtime import (
    LocalProviderDeploymentV1,
    LocalProviderExecutionDriver,
    ProviderInstallationVerificationV1,
    ProviderInstallationVerificationV2,
    ProviderLocalRuntimeRefused,
    _deployment_identity,
    verification_format_upgrade,
    verify_provider_installation,
)
from tests.core_support._p2b1_support import accepted_interface
from tests.test_providers.test_provider_package_contracts import package_provider


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@pytest.fixture
def installation(tmp_path: Path):
    root = tmp_path / "environment"
    root.mkdir()
    wheel = root / "provider.whl"
    wheel.write_bytes(b"retained wheel")
    lock = root / "uv.lock"
    lock.write_bytes(b"retained lock")
    interpreter = root / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"interpreter")
    interpreter.with_name("python3").symlink_to("python")
    provider = package_provider()
    document = provider.runtime_artifact.model_dump(mode="json")
    document["distribution"]["sha256"] = digest(wheel.read_bytes())
    document["local_env"]["lock_sha256"] = digest(lock.read_bytes())
    payload = ProviderRuntimeArtifactPayloadV2.model_validate(document)
    provider = ProviderV3.model_validate(
        {
            **provider.model_dump(mode="json"),
            "runtime_artifact": payload,
            "implementations": provider_expected_implementation_records(payload),
        }
    )
    for module in ("demo/runtime.py", "cruxible_provider_runtime/child.py"):
        path = root / ".venv/lib/site-packages" / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retained module")
    seal_path = root / "execution-seal.json"
    seal_path.write_bytes(
        canonical_bytes(
            {
                "tag": "cruxible.provider.seal.v3",
                "materialization_digest": payload.local_env.materialization_digests[
                    "linux-cp311+engine"
                ],
                "lock_sha256": payload.local_env.lock_sha256,
                "installed_distributions": {
                    payload.distribution.name: payload.distribution.version,
                    "cruxible-provider-runtime": "0.2.0",
                },
                "files": [
                    {"path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes())}
                    for path in sorted(root.rglob("*"))
                    if path.is_file() and not path.is_symlink()
                ],
                "links": [{"path": ".venv/bin/python3", "target": "python"}],
            }
        )
    )
    deployment = LocalProviderDeploymentV1(
        deployment_digest=digest(b"deployment"),
        distribution_path=wheel,
        lock_path=lock,
        environment_path=root,
        environment_manifest_path=seal_path,
        environment_pin_key="linux-cp311+engine",
        interpreter_path=interpreter,
        provider_runtime_version="0.2.0",
    )
    return provider, deployment


def bind(provider: ProviderV3, deployment: LocalProviderDeploymentV1):
    return LocalProviderExecutionDriver().bind(
        AcceptedProviderV1(
            path=provider_path(provider.identity.name),
            provider=provider,
            artifact_digest=provider_digest(provider).tagged,
        ),
        accepted_interface(),
        provider.implementations[0].implementation_digest,
        deployment,
    )


def test_repeated_binding_and_restart_read_no_installed_bytes(installation, monkeypatch):
    provider, deployment = installation
    verified = verify_provider_installation(provider, deployment)
    # A restart reloads retained operational evidence, rather than caching just
    # a Python object or recomputing the inventory at the first subsequent run.
    reloaded = ProviderInstallationVerificationV2.model_validate_json(verified.model_dump_json())
    deployment = replace(deployment, installation_verification=reloaded)

    def no_read(path):
        pytest.fail(f"run binding read installation bytes: {path}")

    monkeypatch.setattr(Path, "read_bytes", no_read)
    first = bind(provider, deployment)
    assert bind(provider, deployment) == first
    assert first.binding.environment_manifest_digest == verified.environment_manifest_digest


def test_manual_changes_require_explicit_reverification(installation):
    provider, deployment = installation
    verified = verify_provider_installation(provider, deployment)
    prepared = replace(deployment, installation_verification=verified)
    original = bind(provider, prepared)
    module = deployment.environment_path / ".venv/lib/site-packages/demo/runtime.py"
    module.write_bytes(b"operator modified this file")
    assert bind(provider, prepared) == original
    with pytest.raises(ProviderLocalRuntimeRefused, match="digest does not reproduce"):
        verify_provider_installation(provider, deployment)


@pytest.mark.parametrize("change", ["extra", "link", "wheel", "missing_record", "location"])
def test_verification_refuses_incomplete_or_mismatched_installation(installation, change):
    provider, deployment = installation
    verified = verify_provider_installation(provider, deployment)
    if change == "extra":
        (deployment.environment_path / "undeclared.py").write_bytes(b"extra")
    elif change == "link":
        link = deployment.interpreter_path.with_name("python3")
        link.unlink()
        link.symlink_to(deployment.distribution_path)
    elif change == "wheel":
        deployment.distribution_path.write_bytes(b"changed wheel")
    elif change == "missing_record":
        with pytest.raises(ProviderLocalRuntimeRefused, match="not been verified"):
            bind(provider, deployment)
        return
    else:
        moved = replace(
            deployment,
            installation_verification=verified,
            deployment_digest=digest(b"different deployment"),
        )
        with pytest.raises(ProviderLocalRuntimeRefused, match="does not match"):
            bind(provider, moved)
        return
    with pytest.raises(ProviderLocalRuntimeRefused):
        verify_provider_installation(provider, deployment)


def test_relocated_environment_preserves_verification_and_binding(installation, tmp_path):
    import shutil

    provider, deployment = installation
    verified = verify_provider_installation(provider, deployment)
    original = bind(provider, replace(deployment, installation_verification=verified))
    destination = tmp_path / "moved-state" / "environment"
    shutil.copytree(deployment.environment_path, destination, symlinks=True)
    moved = replace(
        deployment,
        environment_path=destination,
        installation_verification=verified,
        **{
            name: destination / getattr(deployment, name).relative_to(deployment.environment_path)
            for name in (
                "distribution_path",
                "lock_path",
                "environment_manifest_path",
                "interpreter_path",
            )
        },
    )
    assert bind(provider, moved).binding == original.binding
    assert verify_provider_installation(provider, moved) == verified


def test_old_verification_can_only_be_succeeded_with_identical_content(installation):
    provider, deployment = installation
    new = verify_provider_installation(provider, deployment)
    old = ProviderInstallationVerificationV1.model_validate(
        {
            **new.model_dump(exclude={"tag", "deployment_identity_digest"}),
            "deployment_identity_digest": _deployment_identity(deployment),
        }
    )
    assert bind(provider, replace(deployment, installation_verification=old))
    assert verification_format_upgrade(old, new)
    for field in (
        "distribution_digest",
        "lock_digest",
        "materialization_digest",
        "environment_manifest_digest",
    ):
        assert not verification_format_upgrade(
            old, new.model_copy(update={field: digest(b"different")})
        )
    assert not verification_format_upgrade(new, old)


def test_portable_identity_refuses_paths_outside_installation(installation, tmp_path):
    provider, deployment = installation
    verified = verify_provider_installation(provider, deployment)
    outside = tmp_path / "outside.whl"
    outside.write_bytes(deployment.distribution_path.read_bytes())
    with pytest.raises(ProviderLocalRuntimeRefused, match="escape"):
        bind(
            provider,
            replace(deployment, installation_verification=verified, distribution_path=outside),
        )
