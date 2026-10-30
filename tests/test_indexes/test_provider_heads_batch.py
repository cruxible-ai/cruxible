"""Multi-build bookkeeping keeps stage 4's set-based SQL and exact delta rows."""

import re
import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest

from cruxible_core.indexes import provider_heads
from cruxible_core.indexes.sql_batch import insert_rows
from cruxible_core.indexes.typed_state import schema_sql


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
    operations = [(row[0], "compute", "in-digest", "out-digest", "pure") for row in providers]
    insert_rows(connection, "providers", providers)
    insert_rows(connection, "provider_operations", operations)
    sources = {row[1]: b"" for row in providers}
    members = tuple(SimpleNamespace(artifact_kind="provider", path=path) for path in sources)
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
        "provider_control_domains": 1,
        "provider_heads": 1,
    }
    assert connection.execute(
        "SELECT provider_name,identity,ambiguous FROM provider_heads ORDER BY provider_name"
    ).fetchall() == sorted((f"p{i}", f"Provider:p{i}/a", 1) for i in range(size))
    assert connection.execute(
        "SELECT DISTINCT registration_sequence FROM providers"
    ).fetchall() == [(10,)]
    assert sorted(connection.execute("SELECT * FROM provider_operations").fetchall()) == sorted(
        operations
    )

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


def test_pinner_replacement_is_batched_and_matches_cold_rows(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        line_spec_path,
        parse_line_spec,
        render_line_spec,
    )
    from cruxible_core.derived.derived_state import SnapshotTree
    from cruxible_core.indexes.typed_sqlite import parse_static_owners, replace_rows
    from tests.support.operation_world import (
        LINE,
        author_line,
        author_procedure,
        operation_build,
        slotted_spec,
        world,
    )
    from tests.test_scale.test_provider_build_install import _instance

    (tmp_path / "accepted").mkdir()
    accepted = world(tmp_path / "accepted", operation_build())
    author_procedure(accepted, slotted_spec())
    author_line(accepted)
    tree = dict(accepted.tree_at(accepted.accepted_coordinate().git_oid))
    path = line_spec_path(LINE)
    prototype = parse_line_spec(tree.pop(path), path=path)
    lines = [
        prototype.model_copy(update={"identity": ArtifactIdentity(kind="Line", name=f"batch.p{i}")})
        for i in range(20)
    ]
    sources = tree | {line_spec_path(line.identity.name): render_line_spec(line) for line in lines}
    instance = _instance(tmp_path / "warm", SnapshotTree({}))
    instance.connection.execute("PRAGMA foreign_keys=ON")

    def replace(target, changed):
        replace_rows(
            target.connection,
            request=target.coordinate,
            parsed=parse_static_owners(changed, accepted=target.coordinate),
            sources=changed,
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
            line.identity.qualified,
            binding.build.target.qualified,
            binding.build.artifact_digest,
            binding.build.target.name.partition("/")[0],
        )
        for line in lines
        for binding in line.bindings.values()
    )
    assert expected
    assert (
        instance.connection.execute(
            "SELECT * FROM provider_pinners ORDER BY source_identity,target_identity,target_digest"
        ).fetchall()
        == expected
    )

    changes = {
        line_spec_path(line.identity.name): render_line_spec(
            line.model_copy(
                update={
                    "lifecycle": ArtifactLifecycle(
                        state="retired", predecessor_digest=line_spec_digest(line).tagged
                    ),
                }
            )
        )
        for line in lines[:10]
    }
    gone = [line_spec_path(line.identity.name) for line in lines[10:19]]
    statements.clear()
    instance.connection.set_trace_callback(statements.append)
    replace_rows(
        instance.connection,
        request=instance.coordinate,
        parsed=parse_static_owners(changes, accepted=instance.coordinate),
        sources=changes,
        changed_paths=tuple(changes) + tuple(gone),
        whole_checks=False,
    )
    instance.connection.set_trace_callback(None)
    assert _writes(statements)["provider_pinners"] == 1  # one DELETE, no retired INSERTs
    assert next(
        i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM provider_pinners")
    ) < next(i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM lines"))
    cold_sources = sources | changes
    for path in gone:
        del cold_sources[path]
    cold = _instance(tmp_path / "cold", SnapshotTree({}))
    replace(cold, cold_sources)
    for table in (
        "members",
        "providers",
        "provider_operations",
        "contracts",
        "lines",
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


@pytest.mark.parametrize(
    "mode,kind", [("120000", "blob"), ("100755", "blob"), ("160000", "commit")]
)
def test_cold_provider_source_refuses_non_plain_historical_members(
    tmp_path, monkeypatch, mode, kind
):
    from cruxible_client.contracts.errors import GitError
    from tests.core_support._projection_support import MemoryLedger

    path = "providers/demo.json"
    old = MemoryLedger(tmp_path / "old", {path: b"old"}, modes={path: (mode, kind)})
    sources = {"changesets/1.json": b"record"}
    monkeypatch.setattr(
        provider_heads, "parse_change_set_record", lambda *a, **kw: SimpleNamespace(sequence=1)
    )
    monkeypatch.setattr(old, "parent_of", lambda oid: old.oid)
    read = provider_heads.cold_provider_source(old, "head", sources)

    with pytest.raises(GitError, match="unsupported"):
        read(path, 0)
    assert old.read_calls == 0
