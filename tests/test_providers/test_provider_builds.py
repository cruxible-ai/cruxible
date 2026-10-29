"""Build identities, immutable contracts, and accepted-order resolution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    ProviderBuild,
    evaluate_provider_law,
    parse_provider,
    provider_build_key,
    provider_digest,
    provider_path,
    render_provider,
)
from cruxible_client.contracts.shapes import contract_digest
from cruxible_core.errors import RequestRefusedError
from cruxible_core.providers.builds import BuildRow, resolve_provider_build
from tests.core_support._p2b1_support import provider_v2
from tests.support.operation_world import REFUSAL, VALUE
from tests.support.provider_builds import as_build

_OPERATION_CONTRACTS = {
    (contract.name, contract.version): contract_digest(contract).tagged
    for contract in (VALUE, REFUSAL)
}


def build() -> ProviderBuild:
    return as_build(provider_v2())


def test_build_key_excludes_host_materialization_and_normalizes_distribution() -> None:
    provider = build()
    runtime = provider.runtime_artifact
    other = runtime.model_copy(
        update={
            "local_env": runtime.local_env.model_copy(
                update={"materialization_digests": {"linux-cp311": "sha256:" + "f" * 64}}
            )
        }
    )
    assert provider_build_key(runtime) == provider_build_key(other)
    assert (
        parse_provider(render_provider(provider), path=provider_path(provider.identity.name))
        == provider
    )
    wrong = provider.model_dump(mode="json")
    wrong["identity"]["name"] = "demo-provider/" + "0" * 64
    with pytest.raises(ValidationError, match="build_key_mismatch"):
        ProviderBuild.model_validate(wrong)


def test_build_law_accepts_add_and_retire_but_refuses_mutation_and_domain_takeover() -> None:
    provider = build()
    kw = {
        "path": provider_path(provider.identity.name),
        "contracts": _OPERATION_CONTRACTS,
    }
    assert evaluate_provider_law(provider, predecessor=None, **kw).verdict == "accepted"
    previous = AcceptedProvider(
        provider=provider, artifact_digest=provider_digest(provider).tagged, path=kw["path"]
    )
    retired = provider.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=previous.artifact_digest
            )
        }
    )
    assert evaluate_provider_law(retired, predecessor=previous, **kw).verdict == "accepted"
    # A changed runtime declaration is forbidden even under a lifecycle successor.
    changed = retired.model_copy(update={"implementations": ()})
    assert (
        evaluate_provider_law(changed, predecessor=previous, **kw).diagnostics[0].code
        == "cruxible.provider.build_immutable"
    )
    assert (
        evaluate_provider_law(
            provider, predecessor=None, sibling_control_domains=frozenset({"other"}), **kw
        )
        .diagnostics[0]
        .code
        == "cruxible.provider.control_domain_differs"
    )


def test_newest_means_last_registered_even_for_a_lower_version_and_excludes_retired() -> None:
    coordinate = AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root="sha256:" + "b" * 64,
        generation_root="sha256:" + "c" * 64,
        compiler_digest="sha256:" + "d" * 64,
    )
    rows = (
        BuildRow("Provider:demo/" + "a" * 64, "one", "sha256:" + "1" * 64, True, "2.0.0"),
        BuildRow("Provider:demo/" + "b" * 64, "two", "sha256:" + "2" * 64, True, "1.0.0"),
        BuildRow("Provider:demo/" + "c" * 64, "three", "sha256:" + "3" * 64, False, "3.0.0"),
    )
    sequences = {row.identity: i for i, row in enumerate(rows)}
    resolved = resolve_provider_build("demo", rows=rows, sequences=sequences, coordinate=coordinate)
    assert resolved.rule == "most_recently_registered" and resolved.version == "1.0.0"
    assert resolved.resolved == rows[1].identity.partition(":")[2]
    assert (
        resolve_provider_build(
            rows[0].identity, rows=rows, sequences=sequences, coordinate=coordinate
        ).rule
        == "exact"
    )
    with pytest.raises(RequestRefusedError, match="retired"):
        resolve_provider_build(
            rows[2].identity, rows=rows, sequences=sequences, coordinate=coordinate
        )
