"""Raw lock custody partitions physical environments, never frozen code identity."""

import hashlib
import json
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes, canonical_digest
from cruxible_client.contracts.procedures.line_specs import parse_line_spec
from cruxible_client.contracts.providers import parse_provider, provider_digest, provider_path
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.provider_wheels import install_provider_wheel
from cruxible_core.providers.package_materialization import resolve_provider_package, toolchain
from cruxible_core.providers.provider_local_runtime import (
    ProviderLocalRuntimeRefused,
    verify_provider_installation,
)
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.support.provider_installation import build_local_call
from tests.test_server.test_provider_installation import (
    _run_call,
    _run_line,
    installer_http,  # noqa: F401 - ordinary HTTP initialization and accepted operation Contract
)


def _inventory(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).digest()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def test_raw_locks_have_distinct_sealed_deployments_and_survive_restart(
    installer_http,  # noqa: F811 - imported shared fixture
    tmp_path,
    provider_checkout,
):
    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    wheel, first_lock = build_local_call(tmp_path / "package", provider_checkout.repository)
    second_lock = tmp_path / "comment.lock"
    first_bytes = first_lock.read_bytes()
    second_lock.write_bytes(first_bytes + b"\n# another valid raw lock\n")
    runtime = next(provider_checkout.wheels.glob("cruxible_provider_runtime-*.whl"))
    arguments = {"wheel": wheel, "dependency_wheels": (runtime,)}
    resolved = [
        resolve_provider_package(wheel=wheel, lock_path=p, dependency_wheels=(runtime,))
        for p in (first_lock, second_lock)
    ]
    assert resolved[0].resolved == resolved[1].resolved
    materialization = resolved[0].materialization
    assert resolved[1].materialization == materialization
    # Independently spell the frozen v1 payload, including the root distribution.
    expected_materialization = toolchain("canonical").domain_digest(
        "cruxible.provider.materialization.v1",
        {
            "root": {
                "name": resolved[0].resolved.root_name,
                "distribution_sha256": resolved[0].pin.artifact_id,
            },
            "marker_environment": resolved[0].resolved.marker_environment.digest_payload(),
            "resolved": resolved[0].resolved.triples(),
        },
    )
    assert materialization == expected_materialization
    manager = get_playbill_manager()
    first = install_provider_wheel(client, instance_id, lock=first_lock, **arguments)
    assert first.status == "ready" and first.registered and first.build is not None
    operator = manager.provider_runtime_operator()
    (old_config,) = operator.config.deployments
    old_deployment = operator.deployments[old_config.deployment_digest]
    original_inventory = _inventory(old_deployment.environment_path)
    assert _run_call(
        http, instance_id, reviewer, build=first.build, value={"n": 2}, name="lock-a"
    ) == {"n": 3}
    second = install_provider_wheel(client, instance_id, lock=second_lock, **arguments)
    assert second.status == "ready" and second.registered and second.build is not None
    assert first.build != second.build
    assert first.installation_id != second.installation_id
    assert len(operator.config.deployments) == 2
    assert old_config in operator.config.deployments
    assert _inventory(old_deployment.environment_path) == original_inventory
    expected_ids = []
    providers = []
    paths = []
    for installed, package, raw_lock in zip(
        (first, second), resolved, (first_bytes, second_lock.read_bytes()), strict=True
    ):
        tree = manager.get(instance_id).tree_at(
            manager.get(instance_id).accepted_coordinate().git_oid
        )
        path = provider_path(installed.build)
        provider = parse_provider(tree[path], path=path)
        providers.append(provider)
        expected_id = "sha256:" + canonical_digest(
            "playbill-provider-package-deployment-v2",
            {
                "distribution": package.pin.artifact_id,
                "lock_sha256": package.lock.lock_sha256,
                "materialization": materialization,
                "environment_pin_key": package.pin_key,
            },
        )
        old_v1 = "sha256:" + canonical_digest(
            "playbill-provider-package-deployment-v1",
            {
                "distribution": package.pin.artifact_id,
                "materialization": materialization,
                "environment_pin_key": package.pin_key,
            },
        )
        assert expected_id != old_v1
        expected_ids.append(expected_id)
        deployment = operator.deployments[expected_id]
        paths.append(deployment.environment_path)
        assert deployment.lock_path == deployment.environment_path / "uv.lock"
        assert deployment.lock_path.read_bytes() == raw_lock
        seal = json.loads(deployment.environment_manifest_path.read_bytes())
        assert seal["lock_sha256"] == package.lock.lock_sha256
        assert seal["materialization_digest"] == materialization
        assert (
            verify_provider_installation(provider, deployment)
            == deployment.installation_verification
        )
    assert len(set(expected_ids)) == len(set(paths)) == 2
    assert {p.parent.name for p in paths} == {
        r.lock.lock_sha256.removeprefix("sha256:") for r in resolved
    }
    assert _run_call(
        http, instance_id, reviewer, build=second.build, value={"n": 5}, name="lock-b"
    ) == {"n": 6}
    # Existing accepted Lines keep their exact build after the second install.
    for label, provider in zip(("a", "b"), providers, strict=True):
        instance = manager.get(instance_id)
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        path = f"lines/test.lock-{label}-line.json"
        line = parse_line_spec(tree[path], path=path)
        assert line.bindings["increment"].build.artifact_digest == provider_digest(provider).tagged
    before = tuple(operator.config.deployments)
    for installed, lock in zip((first, second), (first_lock, second_lock), strict=True):
        again = install_provider_wheel(client, instance_id, lock=lock, **arguments)
        assert again.installation_id == installed.installation_id and again.build == installed.build
    assert tuple(operator.config.deployments) == before
    manager.clear()
    restarted = manager.provider_runtime_operator()
    assert tuple(restarted.config.deployments) == before
    for label, expected in (("a", {"n": 3}), ("b", {"n": 6})):
        assert _run_line(instance_id, f"test.lock-{label}-line") == expected
    for installed, lock in zip((first, second), (first_lock, second_lock), strict=True):
        again = install_provider_wheel(client, instance_id, lock=lock, reverify=True, **arguments)
        assert again.installation_id == installed.installation_id and again.build == installed.build
    assert tuple(restarted.config.deployments) == before
    assert _inventory(old_deployment.environment_path) == original_inventory
    # Simulated damage remains refused, including corruption of the execution seal.
    for provider, deployment_id in zip(providers, expected_ids, strict=True):
        deployment = restarted.deployments[deployment_id]
        for field in ("lock_path", "environment_manifest_path"):
            path = getattr(deployment, field)
            retained = path.read_bytes()
            try:
                if field == "lock_path":
                    path.write_bytes(retained + b"\n# damage\n")
                else:
                    seal = json.loads(retained)
                    seal["lock_sha256"] = "sha256:" + "f" * 64
                    path.write_bytes(canonical_bytes(seal))
                with pytest.raises(ProviderLocalRuntimeRefused):
                    verify_provider_installation(provider, deployment)
            finally:
                path.write_bytes(retained)
