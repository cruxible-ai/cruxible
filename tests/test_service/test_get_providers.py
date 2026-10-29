"""Provider inventory references open the exact accepted build through get."""

from pathlib import Path

import pytest
from cruxible_client.contracts.provider_interfaces import (
    provider_interface_path,
    render_provider_interface,
)

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.providers import provider_digest, provider_path, render_provider
from cruxible_core.service.procedures.provider_installation import installed_providers
from tests.core_support._support import initialize_local
from tests.test_procedures.test_procedure_source_runs import _accept_more
from tests.test_scale.test_provider_build_install import package as _package
from tests.test_service.test_get_reads import _get, _refusal


@pytest.fixture
def package(tmp_path: Path):
    return _package.__wrapped__(tmp_path)


def provider_world(tmp_path, package, *, together=False):
    instance, owner = initialize_local(tmp_path)
    document, build = package
    from cruxible_client.contracts.named_references import name_provider

    first, second = name_provider(build(1)), name_provider(build(2))
    definitions = {
        provider_interface_path(item.identity.name): render_provider_interface(item)
        for item in document.interface_registrations()
    }
    definitions[provider_path(first.identity.name)] = render_provider(first)
    if together:
        definitions[provider_path(second.identity.name)] = render_provider(second)
    _accept_more(instance, owner, definitions, name="provider-first")
    return instance, owner, first, second


def test_provider_inventory_names_exact_paths_proof_and_history(tmp_path: Path, package) -> None:
    instance, _owner, first, _second = provider_world(tmp_path, package)
    listed = next(
        row for row in installed_providers(instance) if row.provider == first.provider_name
    )
    refs = (
        f"Provider:{listed.provider}",
        f"Provider:{listed.build}",
        provider_path(listed.build),
    )
    results = [_get(instance, ref) for ref in refs]
    assert all(result.kind == "provider" for result in results)
    assert all(result.ref == first.identity.qualified for result in results)
    assert all(result.card == results[0].card for result in results)
    card = results[0].card
    assert card.provider == listed.provider
    assert (card.build, card.package, card.version, card.interfaces) == (
        listed.build,
        listed.package,
        listed.version,
        listed.interfaces,
    )
    proof = _get(instance, refs[0], detail="proof").proof
    assert proof["envelope"] == first.model_dump(mode="json")
    assert proof["artifact_digest"] == provider_digest(first).tagged
    assert _get(instance, refs[0], detail="history").history.revisions
    builtin = _get(instance, "Provider:cruxible-builtin").card
    assert builtin.provider == builtin.build == "cruxible-builtin"
    builtin_row = next(
        row for row in installed_providers(instance) if row.provider == "cruxible-builtin"
    )
    assert (builtin.package, builtin.version) == (builtin_row.package, builtin_row.version)


def test_provider_name_follows_newest_and_historical_coordinate(tmp_path: Path, package) -> None:
    instance, owner, first, second = provider_world(tmp_path, package)
    before = instance.accepted_coordinate().git_oid
    _accept_more(
        instance,
        owner,
        {provider_path(second.identity.name): render_provider(second)},
        name="provider-second",
    )
    ref = f"Provider:{first.provider_name}"
    assert _get(instance, ref).ref == second.identity.qualified
    assert _get(instance, ref, at=before).ref == first.identity.qualified
    assert _get(instance, first.identity.qualified).ref == first.identity.qualified
    retired = first.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=provider_digest(first).tagged
            )
        }
    )
    _accept_more(
        instance,
        owner,
        {provider_path(first.identity.name): render_provider(retired)},
        name="provider-retired",
    )
    assert _get(instance, first.identity.qualified).card.state == "retired"


def test_ambiguous_provider_name_names_builds_and_inventory_repair(tmp_path: Path, package) -> None:
    instance, _owner, first, second = provider_world(tmp_path, package, together=True)
    refusal = _refusal(instance, f"Provider:{first.provider_name}")
    assert refusal.error_code == "cruxible.get.ref_ambiguous"
    assert set(refusal.candidates) == {first.identity.qualified, second.identity.qualified}
    assert refusal.repair.operation == "cruxible.provider.list"
    assert _get(instance, first.identity.qualified).ref == first.identity.qualified


def test_missing_provider_lists_inventory_as_repair(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    refusal = _refusal(instance, "Provider:missing")
    assert refusal.error_code == "cruxible.get.ref_not_found"
    assert refusal.repair.operation == "cruxible.provider.list"


@pytest.mark.parametrize("size", [20, 200])
def test_provider_name_resolution_uses_maintained_head_not_sibling_enumeration(
    tmp_path: Path, package, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    from cruxible_core.providers import builds
    from cruxible_core.service.discovery.get import _resolve_typed
    from tests.test_scale.test_provider_build_install import _population

    instance, providers = _population(tmp_path, package, size)

    def forbidden(*args, **kwargs):
        raise AssertionError("a successful get must seek the maintained name head")

    monkeypatch.setattr(builds, "indexed_provider_builds", forbidden)
    # Resolution alone needs no artifact materialization; the full cards are
    # exercised above against the public service and a real accepted ledger.
    from types import SimpleNamespace

    projection = SimpleNamespace(typed=SimpleNamespace(connection=instance.connection))
    result = _resolve_typed(
        projection,
        "provider",
        providers[0].provider_name,
        ref=providers[0].identity.qualified,
        surface="sdk",
    )
    assert result.identity == providers[-1].identity.qualified
