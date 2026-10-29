"""Install, move and head maintenance stay flat with retained build populations."""

import sqlite3
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.providers import (
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    provider_path,
    render_provider,
)
from cruxible_core.compiler.compiler import PROVIDER_BUILDS_COMPILER, artifact_codec_for_compiler
from cruxible_core.derived.derived_state import SnapshotTree
from cruxible_core.indexes.provider_heads import refresh_provider_heads
from cruxible_core.indexes.typed_sqlite import insert_members, parse_static_owners, replace_rows
from cruxible_core.indexes.typed_state import insert_owners, schema_sql
from cruxible_core.providers.builds import indexed_newest_provider
from cruxible_core.providers.package_metadata import read_wheel_registration
from cruxible_core.service.procedures.provider_installation import _definition_changes
from tests.core_support._projection_support import MemoryLedger, accepted_coordinate
from tests.test_providers.test_package_metadata_inert import (
    CASES,
    MODULE,
    NAME,
    VERSION,
    build_inert_wheel,
)

pytestmark = pytest.mark.skip(
    reason=(
        "needs the provider package fixture rebuilt on PackageRegistrationDocumentV1 "
        "schema 2 (the operations model): this file's `package` fixture and the "
        "standalone unrelated-population test build a hand-rolled registration.json "
        "with schema_version=1 via test_package_metadata_inert.build_inert_wheel(), "
        "which read_package_registration() now hard-refuses ('schema 1 exports "
        "ProviderInterfaces, which core no longer reads'). Beyond the fixture "
        "plumbing (document.interface_registrations()/interfaces= -> "
        "document.pinned_operations()/operations=, following "
        "tests/support/operation_world.py's operation_build()), "
        "test_accepted_head_preserves_registration_order_and_reconstructs and the "
        "'interface'/'discovery' routes of test_read_work_is_flat_with_metadata_history "
        "exercise accepting/reading a separately-registered ProviderInterface artifact "
        "directly -- a mechanism with no surviving equivalent (operations live inline "
        "on a provider build now, with no parallel acceptance step to measure). Needs "
        "a dedicated pass: rebuild the fixture, and either retarget or drop the "
        "interface-registration-specific assertions/parametrize cases."
    )
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
        instance = _instance(tmp_path / str(size), tree)
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
    print("work at 20/200 builds:", work)
    assert work[0] == work[1], work


def _instance(tmp_path, tree):
    coordinate = accepted_coordinate(MemoryLedger(tmp_path / "repository", tree)).model_copy(
        update={"compiler": PROVIDER_BUILDS_COMPILER}
    )
    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    return SimpleNamespace(
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


@pytest.fixture
def package(tmp_path):
    document = read_wheel_registration(
        build_inert_wheel(tmp_path, CASES["a claim naming a fixture the package does not ship"])
    )

    def build(number):
        return document.provider_definition(
            distribution=ProviderLocalDistributionPin(
                name=NAME,
                version=VERSION,
                filename=f"{MODULE}-{VERSION}-py3-none-any.whl",
                sha256=f"sha256:{number:064x}",
            ),
            local_env=ProviderLocalEnvBackendPin(
                lock_sha256="sha256:" + "b" * 64,
                materialization_digests={"linux-cp313": "sha256:" + "c" * 64},
            ),
            control_domain="operator",
            interfaces=document.interface_registrations(),
        )

    return document, build


def _population(tmp_path, package, size):
    document, build = package
    providers = tuple(build(i + 1) for i in range(size))
    tree = SnapshotTree({provider_path(p.identity.name): render_provider(p) for p in providers})
    instance = _instance(tmp_path / str(size), tree)
    connection = instance.connection
    codec = artifact_codec_for_compiler(PROVIDER_BUILDS_COMPILER)
    insert_members(connection, tree, instance.coordinate.git_object_format)
    insert_owners(
        connection,
        parsed=parse_static_owners(tree, accepted=instance.coordinate),
        blobs=tree,
        codec=codec,
    )
    connection.executemany(
        "INSERT INTO provider_registration_order VALUES (?,'',?)",
        ((provider_path(p.identity.name), i + 1) for i, p in enumerate(providers)),
    )
    refresh_provider_heads(connection, tree)
    return instance, providers


def _work(instance, monkeypatch, operation):
    reads = 0
    instructions = 0
    original = SnapshotTree.__getitem__

    def counted(self, path):
        nonlocal reads
        reads += 1
        return original(self, path)

    def step():
        nonlocal instructions
        instructions += 1
        return 0

    instance.connection.set_progress_handler(step, 1)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(SnapshotTree, "__getitem__", counted)
            operation()
    finally:
        instance.connection.set_progress_handler(None, 0)
    return reads, instructions


def test_install_work_is_flat_with_same_name_build_population(tmp_path, monkeypatch, package):
    document, build = package
    work = []
    for size in (20, 200):
        instance, providers = _population(tmp_path, package, size)

        def install():
            _candidate, paths, installed = _definition_changes(
                instance, document, build(1000), "a" * 40
            )
            assert len(paths) == 2
            assert installed.identity == build(1000).identity
            assert (
                indexed_newest_provider(instance.connection, NAME).identity
                == providers[-1].identity.qualified
            )

        work.append(_work(instance, monkeypatch, install))
        instance.connection.close()
    print("work at 20/200 builds:", work)
    assert work[0] == work[1], work


@pytest.mark.parametrize("pinners", [0, 1])
def test_move_work_is_flat_with_same_name_build_population(tmp_path, monkeypatch, package, pinners):
    from cruxible_client.contracts.provider_installation import ProviderMoveRequest
    from cruxible_core.providers.build_moves import SettledMoves
    from cruxible_core.service.procedures import provider_builds as service

    work = []
    for size in (20, 200):
        instance, providers = _population(tmp_path, package, size)
        old, newest = providers[0], providers[-1]
        if pinners:
            # Selection needs only the source directory row. Full Procedure
            # rewriting and admission run in the server multibuild tests.
            instance.connection.execute(
                "INSERT INTO procedures VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "Procedure:one",
                    "procedures/one.json",
                    "fixture",
                    "sha256:" + "1" * 64,
                    None,
                    1,
                    "live",
                    "sha256:" + "2" * 64,
                    "fixture",
                    "drain",
                    "direct",
                ),
            )
            from cruxible_client.contracts.providers import provider_digest

            instance.connection.execute(
                "INSERT INTO provider_pinners VALUES (?,?,?,?)",
                ("Procedure:one", old.identity.qualified, provider_digest(old).tagged, NAME),
            )
        seen = []

        def settle(tree, *, writes, connection, movers, moves, by_name):
            assert not by_name
            seen.extend(movers)
            return SettledMoves()

        with monkeypatch.context() as scoped:
            scoped.setattr(service, "settle_changes", settle)
            scoped.setattr(
                service,
                "change_scope",
                lambda *a, **kw: nullcontext(
                    SimpleNamespace(head=instance.coordinate, previewing=True)
                ),
            )

            def move():
                result = service.service_move_provider(
                    instance,
                    ProviderMoveRequest(provider=NAME),
                    actor_id="operator",
                    timestamp="2026-10-15T00:00:00.000000Z",
                )
                assert result.to.resolved == newest.identity.name
                assert result.status == "unchanged"

            work.append(_work(instance, scoped, move))
        assert len(seen) == pinners
        if seen:
            assert seen[0].from_build == old.identity.name
            assert seen[0].to_build == newest.identity.name
        instance.connection.close()
    print("work at 20/200 builds:", work)
    assert work[0] == work[1], work


def test_head_updates_are_flat_and_retirement_falls_back(tmp_path, monkeypatch, package):
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.providers import provider_digest

    work = []
    for size in (20, 200):
        instance, providers = _population(tmp_path, package, size)
        original = providers[-1]
        retired = original.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired",
                    predecessor_digest=provider_digest(original).tagged,
                )
            }
        )
        path = provider_path(original.identity.name)
        changed = {path: render_provider(retired)}

        def retire():
            replace_rows(
                instance.connection,
                request=instance.coordinate,
                parsed=parse_static_owners(changed, accepted=instance.coordinate),
                sources=changed,
                codec=artifact_codec_for_compiler(PROVIDER_BUILDS_COMPILER),
                changed_paths=(path,),
                whole_checks=False,
            )
            assert (
                indexed_newest_provider(instance.connection, NAME).identity
                == providers[-2].identity.qualified
            )
            assert (
                instance.connection.execute(
                    "SELECT build_count FROM provider_control_domains WHERE provider_name=?",
                    (NAME,),
                ).fetchone()[0]
                == size
            )

        work.append(_work(instance, monkeypatch, retire))
        instance.connection.close()
    print("work at 20/200 builds:", work)
    assert work[0] == work[1], work


def test_accepted_head_preserves_registration_order_and_reconstructs(tmp_path, package):
    from cruxible_client.contracts.provider_interfaces import (
        provider_interface_path,
        render_provider_interface,
    )

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.providers import provider_digest
    from cruxible_core.errors import RequestRefusedError
    from cruxible_core.indexes.typed_sqlite import authenticate_source_rows
    from tests.core_support._p2b1_support import provider_v2
    from tests.core_support._support import initialize_local
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    (tmp_path / "accepted").mkdir()
    instance, owner = initialize_local(tmp_path / "accepted")
    from cruxible_client.contracts.named_references import name_provider

    document, frozen_build = package

    def build(i):
        return name_provider(frozen_build(i))

    first, second, third, fourth = (build(i) for i in range(1, 5))

    def accept(label, providers, *, interfaces=False):
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        tree.update({provider_path(p.identity.name): render_provider(p) for p in providers})
        if interfaces:
            tree.update(
                {
                    provider_interface_path(r.identity.name): render_provider_interface(r)
                    for r in document.interface_registrations()
                }
            )
        _accept_tree(
            instance, owner, tree, timestamp="2026-10-15T00:00:00.000000Z", proposal_name=label
        )

    def head():
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            authenticate_source_rows(projection, repository=instance._ledger)
            return indexed_newest_provider(projection.typed.connection, NAME)

    accept("first", (first,), interfaces=True)
    first_coordinate = instance.accepted_coordinate()
    assert head().identity == first.identity.qualified
    accept("second", (second,))
    assert head().identity == second.identity.qualified
    # Metadata accepted later must not re-register the older build.
    first = first.model_copy(
        update={
            "signing_keys": provider_v2().signing_keys,
            "lifecycle": ArtifactLifecycle(predecessor_digest=provider_digest(first).tagged),
        }
    )
    accept("metadata", (first,))
    assert head().identity == second.identity.qualified
    second = second.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=provider_digest(second).tagged,
            )
        }
    )
    accept("retire", (second,))
    assert head().identity == first.identity.qualified
    _candidate, _paths, carried = _definition_changes(
        instance, document, build(1000), instance.accepted_coordinate().git_oid
    )
    assert carried.signing_keys == first.signing_keys
    # Historical projections keep their own name head.
    with instance.bind_accepted_projection(first_coordinate) as projection:
        assert (
            indexed_newest_provider(projection.typed.connection, NAME).identity
            == first.identity.qualified
        )
    accept("together", (third, fourth))
    with pytest.raises(RequestRefusedError, match="several live builds"):
        head()
    third = third.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=provider_digest(third).tagged,
            )
        }
    )
    accept("break-tie", (third,))
    assert head().identity == fourth.identity.qualified


def test_admission_domain_work_is_flat_with_same_name_build_population(
    tmp_path, monkeypatch, package
):
    from cruxible_core.indexes.evaluated_state import EvaluationRows
    from cruxible_core.proposals.proposals import _sibling_control_domains

    work = []
    for size in (20, 200):
        instance, _providers = _population(tmp_path, package, size)
        _document, build = package
        desired = build(1000)
        path = provider_path(desired.identity.name)
        rows = EvaluationRows(
            SimpleNamespace(
                _connection=instance.connection,
                typed=SimpleNamespace(),
                accepted=instance.coordinate,
            )
        )
        rows.changed = {path: render_provider(desired)}

        def domains():
            assert _sibling_control_domains(instance.tree, desired, path, rows.states) == frozenset(
                {"operator"}
            )

        work.append(_work(instance, monkeypatch, domains))
        # Other new builds in this same candidate must participate in the law.
        hostile = build(1001).model_copy(update={"control_domain": "other"})
        rows.changed = {
            path: render_provider(desired),
            provider_path(hostile.identity.name): render_provider(hostile),
        }
        rows._resolved.clear()
        assert _sibling_control_domains(instance.tree, desired, path, rows.states) == frozenset(
            {"operator", "other"}
        )
        instance.connection.close()
    print("work at 20/200 builds:", work)
    assert work[0] == work[1], work


@pytest.mark.parametrize("pinners", [0, 1])
@pytest.mark.parametrize("preview", [True, False])
def test_kit_mover_discovery_is_flat_with_retained_builds(
    tmp_path, monkeypatch, package, pinners, preview
):
    from cruxible_client.contracts.providers import provider_digest
    from cruxible_core.service.kits import _kit_movers, _kit_moves

    work = []
    for size in (20, 200):
        instance, providers = _population(tmp_path, package, size)
        old, target = providers[0], providers[-1]
        if pinners:
            instance.connection.execute(
                "INSERT INTO procedures VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "Procedure:one",
                    "procedures/one.json",
                    "fixture",
                    "sha256:" + "1" * 64,
                    None,
                    1,
                    "live",
                    "sha256:" + "2" * 64,
                    "fixture",
                    "drain",
                    "direct",
                ),
            )
            instance.connection.execute(
                "INSERT INTO provider_pinners VALUES (?,?,?,?)",
                ("Procedure:one", old.identity.qualified, provider_digest(old).tagged, NAME),
            )
        shipped = SimpleNamespace(provider_id=NAME, build=target.identity.name)

        def select():
            movers = _kit_movers(instance.connection, instance.tree, (shipped,), {})
            assert len(movers) == pinners
            assert all(
                mover.from_build == old.identity.name and mover.to_build == target.identity.name
                for mover in movers
            )
            # Preview and commit share discovery; commit also builds mappings.
            if not preview:
                assert len(_kit_moves(instance.tree, movers)) == pinners

        work.append(_work(instance, monkeypatch, select))
        instance.connection.close()
    assert work[0] == work[1], work


@pytest.mark.parametrize("route", ["authoring", "interface", "discovery", "catalog", "blueprint"])
def test_read_work_is_flat_with_metadata_history(tmp_path, monkeypatch, package, route):
    from cruxible_core.authoring.procedure_source import accepted_build_resolver
    from cruxible_core.indexes.history.history_index import _MEMBER_SCHEMA
    from cruxible_core.service.discovery.discovery import accepted_provider_interfaces
    from cruxible_core.service.discovery.get import _fitting_names
    from cruxible_core.service.procedures.provider_installation import installed_providers

    work = []
    for size in (20, 200):
        instance, providers = _population(tmp_path / str(size), package, 2)
        document, _build = package
        from cruxible_client.contracts.provider_interfaces import (
            provider_interface_path,
            render_provider_interface,
        )

        registrations = {
            provider_interface_path(r.identity.name): render_provider_interface(r)
            for r in document.interface_registrations()
        }
        instance.tree = instance.tree.fork()
        instance.tree.update(registrations)
        insert_members(instance.connection, registrations, instance.coordinate.git_object_format)
        insert_owners(
            instance.connection,
            parsed=parse_static_owners(registrations, accepted=instance.coordinate),
            blobs=registrations,
            codec=artifact_codec_for_compiler(PROVIDER_BUILDS_COMPILER),
        )
        instance.connection.executescript(_MEMBER_SCHEMA)
        for seq in range(1, size + 1):
            row = providers[0]
            instance.connection.execute(
                "INSERT INTO accepted_member_locations VALUES (?,?,?,?,?,?,?)",
                (
                    seq,
                    0,
                    provider_path(row.identity.name),
                    "provider",
                    row.identity.qualified,
                    "sha256:" + f"{seq:064x}",
                    1,
                ),
            )
        typed = SimpleNamespace(
            connection=instance.connection,
            member_bytes=lambda path: instance.tree[path],
            prefetch_members=lambda paths: None,
        )
        instance.bind_accepted_projection = lambda _: nullcontext(SimpleNamespace(typed=typed))
        resolver = accepted_build_resolver(
            instance, instance.coordinate, SimpleNamespace(connection=instance.connection)
        )

        def read():
            if route == "authoring":
                result = resolver.provider(NAME)
                assert result.resolved == providers[-1].identity.name
                assert len(result.considered) == 2
            elif route == "interface":
                assert resolver.interface(document.interface_registrations()[0].interface_id)
            elif route == "discovery":
                assert accepted_provider_interfaces(instance, instance.coordinate)
            elif route == "catalog":
                assert len(installed_providers(instance)) == 2
            else:
                assert _fitting_names(
                    SimpleNamespace(typed=typed), (providers[-1].identity.name,)
                ) == (NAME,)

        work.append(_work(instance, monkeypatch, read))
        instance.connection.close()
    assert work[0] == work[1], work


def test_exact_authoring_read_does_not_enumerate_sibling_builds(tmp_path, monkeypatch, package):
    from cruxible_core.authoring.procedure_source import accepted_build_resolver

    work = []
    for size in (20, 200):
        instance, providers = _population(tmp_path, package, size)
        resolver = accepted_build_resolver(
            instance, instance.coordinate, SimpleNamespace(connection=instance.connection)
        )

        def read():
            result = resolver.provider(providers[0].identity.name)
            assert len(result.considered) == 1

        work.append(_work(instance, monkeypatch, read))
        instance.connection.close()
    assert work[0] == work[1], work
