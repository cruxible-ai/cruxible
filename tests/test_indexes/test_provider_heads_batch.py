"""Multi-build bookkeeping keeps stage 4's set-based SQL and exact delta rows."""

import re
import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest

from cruxible_core.indexes import provider_heads
from cruxible_core.indexes.sql_batch import insert_rows
from cruxible_core.indexes.typed_state import schema_sql
from tests.test_integration.test_graph_v4_provider_closure import contracted_demo as contracted_demo


def _writes(statements):
    return Counter(
        match.group(1)
        for sql in statements
        if (match := re.search(r"\b(?:INSERT INTO|UPDATE|DELETE FROM) (\w+)", sql))
    )


@pytest.mark.parametrize("size", [1, 30])
def test_provider_maintenance_batches_every_changed_table(monkeypatch, size):
    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    providers = [
        (
            f"Provider:p{i}/{build}",
            f"providers/p{i}/{build}.json",
            "fixture",
            "digest",
            None,
            1,
            "live",
            "operator",
            f"p{i}",
            build,
            "1.0",
            0,
        )
        for i in range(size)
        for build in ("a", "b")
    ]
    interfaces = [
        (
            f"ProviderInterface:i{i}/contract",
            f"provider-interfaces/i{i}/contract.json",
            "fixture",
            "digest",
            None,
            1,
            "live",
            f"i{i}",
            "digest",
            "none",
            "contract",
            0,
        )
        for i in range(size)
    ]
    insert_rows(connection, "providers", providers)
    insert_rows(connection, "provider_interfaces", interfaces)
    sources = {row[1]: b"" for row in providers + interfaces}
    members = tuple(
        SimpleNamespace(
            artifact_kind="provider" if path.startswith("providers/") else "provider-interface",
            path=path,
        )
        for path in sources
    )
    record = SimpleNamespace(sequence=10, members=members)
    monkeypatch.setattr(provider_heads, "parse_change_set_record", lambda *a, **kw: record)
    sources["changesets/10.json"] = b"record"
    statements = []
    connection.set_trace_callback(statements.append)
    provider_heads.refresh_provider_heads(connection, sources)
    connection.set_trace_callback(None)
    assert _writes(statements) == {
        "provider_registration_order": 1,
        "providers": 1,
        "provider_interfaces": 1,
        "provider_control_domains": 1,
        "provider_heads": 1,
    }
    assert connection.execute(
        "SELECT provider_name,identity,ambiguous FROM provider_heads ORDER BY provider_name"
    ).fetchall() == sorted((f"p{i}", f"Provider:p{i}/a", 1) for i in range(size))
    assert connection.execute(
        "SELECT DISTINCT registration_sequence FROM provider_interfaces"
    ).fetchall() == [(10,)]

    # Later metadata preserves the earliest registration; retirement falls back
    # to the other build and subtract/add leaves the domain count unchanged.
    retiring = [row[0] for row in providers if row[0].endswith("/a")]
    statements.clear()
    connection.set_trace_callback(statements.append)
    provider_heads.remove_provider_domains(connection, retiring)
    connection.set_trace_callback(None)
    assert _writes(statements) == {"provider_control_domains": 2}
    connection.executemany(
        "UPDATE providers SET lifecycle='retired' WHERE identity=?",
        [(identity,) for identity in retiring],
    )
    changed = {row[1]: b"" for row in providers if row[0] in retiring}
    changed["changesets/11.json"] = b"record"
    record.sequence = 11
    record.members = tuple(member for member in members if member.path in changed)
    provider_heads.refresh_provider_heads(connection, changed)
    assert connection.execute(
        "SELECT provider_name,identity,ambiguous FROM provider_heads ORDER BY provider_name"
    ).fetchall() == sorted((f"p{i}", f"Provider:p{i}/b", 0) for i in range(size))
    assert connection.execute(
        "SELECT DISTINCT build_count FROM provider_control_domains"
    ).fetchall() == [(2,)]
    assert connection.execute(
        "SELECT DISTINCT sequence FROM provider_registration_order"
    ).fetchall() == [(10,)]
    assert connection.execute(
        "SELECT DISTINCT registration_sequence FROM providers"
    ).fetchall() == [(10,)]
    connection.close()


def test_domain_removal_aggregates_across_parameter_chunks():
    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    # One domain spans multiple identity chunks; another disappears completely;
    # a third domain has enough distinct keys to chunk the DELETE and UPDATE.
    rows = [
        (
            f"Provider:shared/{i}",
            f"providers/shared/{i}.json",
            "fixture",
            "digest",
            None,
            1,
            "live",
            "operator",
            "shared",
            str(i),
            "1.0",
            0,
        )
        for i in range(1001)
    ] + [
        (
            f"Provider:p{i}/a",
            f"providers/p{i}/a.json",
            "fixture",
            "digest",
            None,
            1,
            "live",
            "operator",
            f"p{i}",
            "a",
            "1.0",
            0,
        )
        for i in range(1000)
    ]
    insert_rows(connection, "providers", rows)
    insert_rows(
        connection,
        "provider_control_domains",
        [("shared", "operator", 1001)] + [(f"p{i}", "operator", 1) for i in range(1000)],
    )
    removed = [row[0] for row in rows if row[0] != "Provider:shared/1000"]
    provider_heads.remove_provider_domains(connection, removed + removed[:1])
    assert connection.execute("SELECT * FROM provider_control_domains").fetchall() == [
        ("shared", "operator", 1)
    ]
    connection.close()


@pytest.mark.usefixtures("contracted_demo")
def test_pinner_replacement_is_batched_and_matches_cold_rows(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedures.artifacts import (
        procedure_artifact_digest,
        render_procedure,
    )
    from cruxible_client.contracts.procedures.blueprints import (
        blueprint_digest,
        instantiate_blueprint,
    )
    from cruxible_core.compiler.compiler import (
        PROVIDER_BUILDS_COMPILER,
        artifact_codec_for_compiler,
    )
    from cruxible_core.derived.derived_state import SnapshotTree
    from cruxible_core.indexes.typed_sqlite import parse_static_owners, replace_rows
    from tests.test_providers.test_legacy_blueprint_build_move import legacy_blueprint_move
    from tests.test_scale.test_provider_build_install import _instance

    old, _new, blueprint, _procedure, path, tree, _move = legacy_blueprint_move()
    del tree[path]
    procedures = [
        instantiate_blueprint(
            blueprint,
            blueprint_artifact_digest=blueprint_digest(blueprint).tagged,
            name=f"batch.p{i}",
            providers={"lookup": old},
        )
        for i in range(20)
    ]
    sources = tree | {f"procedures/{p.identity.name}.json": render_procedure(p) for p in procedures}
    codec = artifact_codec_for_compiler(PROVIDER_BUILDS_COMPILER)
    instance = _instance(tmp_path / "warm", SnapshotTree({}))
    instance.connection.execute("PRAGMA foreign_keys=ON")

    def replace(target, changed):
        replace_rows(
            target.connection,
            request=target.coordinate,
            parsed=parse_static_owners(changed, accepted=target.coordinate),
            sources=changed,
            codec=codec,
            changed_paths=tuple(changed),
            whole_checks=False,
        )

    statements = []
    instance.connection.set_trace_callback(statements.append)
    replace(instance, sources)
    instance.connection.set_trace_callback(None)
    assert _writes(statements)["provider_pinners"] == 1
    expected = sorted(
        (
            p.identity.qualified,
            pin.target.qualified,
            pin.artifact_digest,
            pin.target.name.partition("/")[0],
        )
        for p in procedures
        for pin in p.pins
        if pin.target.kind == "Provider"
    )
    assert expected
    assert (
        instance.connection.execute(
            "SELECT * FROM provider_pinners ORDER BY source_identity,target_identity,target_digest"
        ).fetchall()
        == expected
    )

    changes = {
        f"procedures/{p.identity.name}.json": render_procedure(
            p.model_copy(
                update={
                    "lifecycle": ArtifactLifecycle(
                        state="retired", predecessor_digest=procedure_artifact_digest(p).tagged
                    ),
                }
            )
        )
        for p in procedures[:10]
    }
    gone = [f"procedures/{p.identity.name}.json" for p in procedures[10:19]]
    statements.clear()
    instance.connection.set_trace_callback(statements.append)
    replace_rows(
        instance.connection,
        request=instance.coordinate,
        parsed=parse_static_owners(changes, accepted=instance.coordinate),
        sources=changes,
        codec=codec,
        changed_paths=tuple(changes) + tuple(gone),
        whole_checks=False,
    )
    instance.connection.set_trace_callback(None)
    assert _writes(statements)["provider_pinners"] == 1  # one DELETE, no retired INSERTs
    assert next(
        i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM provider_pinners")
    ) < next(i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM procedures"))
    cold_sources = sources | changes
    for path in gone:
        del cold_sources[path]
    cold = _instance(tmp_path / "cold", SnapshotTree({}))
    replace(cold, cold_sources)
    for table in (
        "members",
        "providers",
        "provider_interfaces",
        "procedures",
        "pins",
        "provider_pinners",
        "provider_control_domains",
        "provider_registration_order",
        "provider_heads",
    ):
        assert sorted(instance.connection.execute(f"SELECT * FROM {table}").fetchall()) == sorted(
            cold.connection.execute(f"SELECT * FROM {table}").fetchall()
        ), table
    assert instance.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    instance.connection.close()
    cold.connection.close()


def test_cold_provider_source_uses_supplied_memory_repository(tmp_path, monkeypatch):
    from tests.core_support._projection_support import MemoryLedger

    old = MemoryLedger(tmp_path / "old", {"providers/demo.json": b"old"}, oid_seed="old")
    sources = {"providers/demo.json": b"new", "changesets/1.json": b"record"}
    head = MemoryLedger(tmp_path / "head", sources)
    monkeypatch.setattr(
        provider_heads, "parse_change_set_record", lambda *a, **kw: SimpleNamespace(sequence=1)
    )
    monkeypatch.setattr(head, "parent_of", lambda oid: old.oid if oid == head.oid else None)
    monkeypatch.setattr(head, "list_tree", old.list_tree)
    monkeypatch.setattr(head, "read_blob", old.read_blob)
    read = provider_heads.cold_provider_source(head, head.oid, sources)

    assert read("providers/demo.json", 1) == b"new"
    assert old.list_calls == 0
    assert read("providers/demo.json", 0) == b"old"
    assert read("providers/absent.json", 0) is None
    assert old.list_calls == 1


def test_cold_provider_source_has_no_ancestor_in_a_memory_snapshot(tmp_path, monkeypatch):
    from tests.core_support._projection_support import MemoryLedger

    sources = {"providers/demo.json": b"new", "changesets/1.json": b"record"}
    repository = MemoryLedger(tmp_path / "snapshot", sources)
    monkeypatch.setattr(
        provider_heads, "parse_change_set_record", lambda *a, **kw: SimpleNamespace(sequence=1)
    )
    read = provider_heads.cold_provider_source(repository, repository.oid, sources)

    assert read("providers/demo.json", 0) is None
    assert read("providers/demo.json", 1) == b"new"
    assert repository.list_calls == repository.read_calls == 0
