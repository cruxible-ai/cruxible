"""Installing one build never reads unrelated builds (N and ten times N)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.providers import (
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
)
from cruxible_core.derived.derived_state import SnapshotTree
from cruxible_core.providers.package_metadata import read_wheel_registration
from cruxible_core.service.procedures.provider_installation import _definition_changes
from tests.test_providers.test_package_metadata_inert import (
    CASES,
    MODULE,
    NAME,
    VERSION,
    build_inert_wheel,
)


def test_install_work_is_flat_with_unrelated_build_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = read_wheel_registration(
        build_inert_wheel(tmp_path, CASES["a claim naming a fixture the package does not ship"])
    )
    provider = document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=NAME,
            version=VERSION,
            filename=f"{MODULE}-{VERSION}-py3-none-any.whl",
            sha256="sha256:" + "a" * 64,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp313": "sha256:" + "c" * 64},
        ),
        control_domain="operator",
        interfaces=document.interface_registrations(),
    )
    work = []
    for size in (20, 200):
        # Invalid bytes deliberately fail if installation parses another name's build.
        tree = SnapshotTree({f"providers/other/{i:064x}.json": b"unrelated" for i in range(size)})
        instance = SimpleNamespace(immutable_tree_at=lambda _oid: tree)
        count = 0
        original = SnapshotTree.__getitem__

        def counted(self, path):
            nonlocal count
            count += 1
            return original(self, path)

        with monkeypatch.context() as scoped:
            scoped.setattr(SnapshotTree, "__getitem__", counted)
            _candidate, paths, installed = _definition_changes(
                instance, document, provider, "a" * 40
            )
        assert installed.identity == provider.identity
        assert len(paths) == 2
        work.append(count)
    assert work[0] == work[1], work
