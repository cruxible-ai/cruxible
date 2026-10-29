"""Provider inventory references open the exact accepted build through get."""

from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.providers import provider_digest, provider_path, render_provider
from cruxible_core.service.procedures.provider_installation import installed_providers
from tests.core_support._support import initialize_local
from tests.support.operation_world import contract_files, operation_build
from tests.test_procedures.test_procedure_source_runs import _accept_more
from tests.test_service.test_get_reads import _get, _refusal

PROVIDER_ID = "get-providers-demo"


@pytest.fixture
def package(tmp_path: Path):
    def build(number):
        # A 64-hex-char build string passes through operation_build's
        # `(build * 64)[:64]` unchanged, so each number gets its own exact,
        # non-colliding build digest (a short repeating digit string would
        # collide: "1" and "11" both expand to the same 64-char run).
        return operation_build(provider_id=PROVIDER_ID, build=f"{number:064x}")

    return None, build


def provider_world(tmp_path, package, *, together=False):
    instance, owner = initialize_local(tmp_path)
    _document, build = package
    from cruxible_client.contracts.named_references import name_provider

    first, second = name_provider(build(1)), name_provider(build(2))
    # A Provider build's operations are its own data now (B2 noun removal);
    # there is no separate ProviderInterface artifact to accept beside it,
    # but its operation Contract pins (test.value@1) must themselves be accepted.
    definitions = {
        **contract_files(),
        provider_path(first.identity.name): render_provider(first),
    }
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
    assert (card.build, card.package, card.version) == (
        listed.build,
        listed.package,
        listed.version,
    )
    # The card's operations are full signatures ("name: in@N -> out@N (effect)");
    # the inventory row's are bare names.
    assert tuple(item.partition(":")[0] for item in card.operations) == listed.operations
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


def _population(tmp_path: Path, package, size: int):
    """``size`` builds of the same package, accepted and head-indexed, no interfaces."""

    import sqlite3
    from contextlib import nullcontext
    from types import SimpleNamespace

    from cruxible_core.compiler.compiler import (
        PROVIDER_BUILDS_COMPILER,
        artifact_codec_for_compiler,
    )
    from cruxible_core.derived.derived_state import SnapshotTree
    from cruxible_core.indexes.provider_heads import refresh_provider_heads
    from cruxible_core.indexes.typed_sqlite import insert_members, parse_static_owners
    from cruxible_core.indexes.typed_state import insert_owners, schema_sql
    from tests.core_support._projection_support import MemoryLedger, accepted_coordinate

    _document, build = package
    providers = tuple(build(i + 1) for i in range(size))
    tree = SnapshotTree({provider_path(p.identity.name): render_provider(p) for p in providers})
    coordinate = accepted_coordinate(MemoryLedger(tmp_path / "repository", tree)).model_copy(
        update={"compiler": PROVIDER_BUILDS_COMPILER}
    )
    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    instance = SimpleNamespace(
        immutable_tree_at=lambda _oid: tree,
        coordinate_for_oid=lambda _oid: coordinate,
        accepted_coordinate=lambda: coordinate,
        bind_accepted_projection=lambda _coordinate: nullcontext(
            SimpleNamespace(typed=SimpleNamespace(connection=connection))
        ),
        connection=connection,
        coordinate=coordinate,
        tree=tree,
    )
    codec = artifact_codec_for_compiler(PROVIDER_BUILDS_COMPILER)
    insert_members(connection, tree, coordinate.git_object_format)
    insert_owners(
        connection,
        parsed=parse_static_owners(tree, accepted=coordinate),
        blobs=tree,
        codec=codec,
    )
    connection.executemany(
        "INSERT INTO provider_registration_order VALUES (?,'',?)",
        ((provider_path(p.identity.name), i + 1) for i, p in enumerate(providers)),
    )
    refresh_provider_heads(connection, tree)
    return instance, providers


@pytest.mark.parametrize("size", [20, 200])
def test_provider_name_resolution_uses_maintained_head_not_sibling_enumeration(
    tmp_path: Path, package, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    from cruxible_core.providers import builds
    from cruxible_core.service.discovery.get import _resolve_typed

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
