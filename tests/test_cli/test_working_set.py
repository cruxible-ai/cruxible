"""Tests for the opt-in agent-local working set (capture + ws command group)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import cli
from cruxible_core.cli.working_set import (
    HEADER_LINE,
    local_instance_key,
    read_records,
    records_path,
    working_set_enabled,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate HOME (working-set dir + CLI context) and the opt-in env var."""
    home = tmp_path / "ws-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CRUXIBLE_WORKING_SET", raising=False)
    return home


def _chdir_run(runner: CliRunner, directory: Path, args: list[str]):
    original = os.getcwd()
    try:
        os.chdir(directory)
        return runner.invoke(cli, args)
    finally:
        os.chdir(original)


def _ws_file(instance: CruxibleInstance) -> Path:
    return records_path(local_instance_key(instance.get_root_path()))


def _records_by_identity(instance: CruxibleInstance) -> dict[tuple, dict]:
    from cruxible_core.cli.working_set import record_identity

    return {record_identity(record): record for record in read_records(_ws_file(instance))}


def _mutate(instance: CruxibleInstance, graph: EntityGraph) -> None:
    """Advance the instance read revision with a state-mutating commit."""
    instance.save_graph(graph)


def _find_edge(by_identity: dict[tuple, dict], relationship: str, from_id: str, to_id: str) -> dict:
    """Look up one captured edge record, ignoring the graph-assigned edge_key."""
    matches = [
        record
        for identity, record in by_identity.items()
        if identity[0] == "edge"
        and record["relationship_type"] == relationship
        and record["from_id"] == from_id
        and record["to_id"] == to_id
    ]
    assert len(matches) == 1, f"expected one {relationship} {from_id}->{to_id}, got {matches}"
    return matches[0]


class TestOptIn:
    def test_off_by_default_no_file_and_identical_stdout(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        isolated_home: Path,
    ) -> None:
        root = populated_instance.get_root_path()
        args = ["sample", "--type", "Part", "--json"]
        off = _chdir_run(runner, root, args)
        assert off.exit_code == 0
        assert not (isolated_home / ".cruxible" / "working-set").exists()

        os.environ["CRUXIBLE_WORKING_SET"] = "1"
        try:
            on = _chdir_run(runner, root, args)
        finally:
            del os.environ["CRUXIBLE_WORKING_SET"]
        assert on.exit_code == 0
        # Capture is a pure side effect: stdout is byte-identical either way.
        assert on.output == off.output
        assert _ws_file(populated_instance).exists()

    def test_ws_flag_enables_capture_without_env(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        result = _chdir_run(
            runner, root, ["entity", "get", "--type", "Part", "--id", "BP-1001", "--ws", "--json"]
        )
        assert result.exit_code == 0
        records = read_records(_ws_file(populated_instance))
        assert [(r["kind"], r["entity_type"], r["entity_id"]) for r in records] == [
            ("entity", "Part", "BP-1001")
        ]

    def test_working_set_enabled_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CRUXIBLE_WORKING_SET", raising=False)
        assert working_set_enabled() is False
        assert working_set_enabled(ws_flag=True) is True
        monkeypatch.setenv("CRUXIBLE_WORKING_SET", "1")
        assert working_set_enabled() is True
        monkeypatch.setenv("CRUXIBLE_WORKING_SET", "0")
        assert working_set_enabled() is False

    def test_stdout_unchanged_by_ws_flag(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        base = ["list", "edges", "--json"]
        without = _chdir_run(runner, root, base)
        with_ws = _chdir_run(runner, root, [*base[:2], "--ws", *base[2:]])
        assert without.exit_code == 0
        assert with_ws.exit_code == 0
        assert with_ws.output == without.output


class TestCaptureShapes:
    def test_query_path_rows_capture_entities_and_edges(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        result = _chdir_run(
            runner,
            root,
            [
                "query",
                "run",
                "vehicles_for_part",
                "--param",
                "part_number=BP-1001",
                "--ws",
                "--json",
            ],
        )
        assert result.exit_code == 0
        revision = populated_instance.get_read_revision()
        by_identity = _records_by_identity(populated_instance)
        assert ("entity", "Part", "BP-1001") in by_identity
        assert ("entity", "Vehicle", "V-2024-CIVIC-EX") in by_identity
        edge = by_identity[("edge", "fits", "Part", "BP-1001", "Vehicle", "V-2024-CIVIC-EX", 0)]
        assert edge["read_revision"] == revision
        assert edge["review"] is not None
        assert edge["props"]["verified"] is True
        entity = by_identity[("entity", "Part", "BP-1001")]
        assert entity["read_revision"] == revision
        assert entity["source_cmd"] == "query run"
        # The query receipt is threaded into receipt_refs.
        assert entity["receipt_refs"]
        # Compact profile: display key only, not the full property bag.
        assert entity["props"] == {"name": "Ceramic Brake Pads"}

    def test_inspect_neighborhood_captures_nodes_and_edges(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        result = _chdir_run(
            runner,
            root,
            [
                "entity",
                "inspect",
                "--type",
                "Part",
                "--id",
                "BP-1001",
                "--depth",
                "1",
                "--ws",
                "--json",
            ],
        )
        assert result.exit_code == 0
        revision = populated_instance.get_read_revision()
        by_identity = _records_by_identity(populated_instance)
        assert ("entity", "Part", "BP-1001") in by_identity  # root
        assert ("entity", "Vehicle", "V-2024-CIVIC-EX") in by_identity  # node
        edge = by_identity[("edge", "fits", "Part", "BP-1001", "Vehicle", "V-2024-CIVIC-EX", 0)]
        assert edge["read_revision"] == revision
        assert edge["source_cmd"] == "entity inspect"

    def test_single_hop_inspect_synthesizes_edge_endpoints(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        result = _chdir_run(
            runner,
            root,
            ["entity", "inspect", "--type", "Part", "--id", "BP-1001", "--ws", "--json"],
        )
        assert result.exit_code == 0
        by_identity = _records_by_identity(populated_instance)
        # Outgoing fits edge: root -> neighbor.
        _find_edge(by_identity, "fits", "BP-1001", "V-2024-CIVIC-EX")
        # Incoming replaces edge: neighbor -> root.
        _find_edge(by_identity, "replaces", "BP-1002", "BP-1001")

    def test_list_edges_and_entities_capture(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        assert _chdir_run(runner, root, ["list", "edges", "--ws", "--json"]).exit_code == 0
        assert (
            _chdir_run(
                runner, root, ["list", "entities", "--type", "Vehicle", "--ws", "--json"]
            ).exit_code
            == 0
        )
        revision = populated_instance.get_read_revision()
        by_identity = _records_by_identity(populated_instance)
        replaces = _find_edge(by_identity, "replaces", "BP-1002", "BP-1001")
        assert replaces["read_revision"] == revision
        assert replaces["source_cmd"] == "list edges"
        vehicle = by_identity[("entity", "Vehicle", "V-2024-ACCORD-SPORT")]
        assert vehicle["read_revision"] == revision
        assert vehicle["source_cmd"] == "list entities"

    def test_header_line_present_and_tolerated(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        path = _ws_file(populated_instance)
        lines = path.read_text().splitlines()
        assert lines[0] == HEADER_LINE
        # Reader skips the header without warnings or crashes.
        records = read_records(path)
        assert records and all(record["kind"] == "entity" for record in records)


class TestDedupe:
    def test_reread_after_mutation_keeps_only_newer_revision_line(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        populated_graph: EntityGraph,
    ) -> None:
        root = populated_instance.get_root_path()
        args = ["entity", "get", "--type", "Part", "--id", "BP-1001", "--ws", "--json"]
        assert _chdir_run(runner, root, args).exit_code == 0
        first_revision = populated_instance.get_read_revision()

        _mutate(populated_instance, populated_graph)
        second_revision = populated_instance.get_read_revision()
        assert second_revision > first_revision

        assert _chdir_run(runner, root, args).exit_code == 0
        path = _ws_file(populated_instance)
        matching = [
            record
            for record in read_records(path)
            if record["kind"] == "entity" and record["entity_id"] == "BP-1001"
        ]
        assert len(matching) == 1
        assert matching[0]["read_revision"] == second_revision
        # Physically one line too, not just logically deduped.
        raw_lines = [line for line in path.read_text().splitlines() if "BP-1001" in line]
        assert len(raw_lines) == 1


class TestVerify:
    def test_verify_classifies_fresh_stale_unknown_with_exit_codes(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        populated_graph: EntityGraph,
    ) -> None:
        root = populated_instance.get_root_path()
        assert (
            _chdir_run(
                runner,
                root,
                ["entity", "get", "--type", "Part", "--id", "BP-1001", "--ws", "--json"],
            ).exit_code
            == 0
        )

        fresh_verify = _chdir_run(runner, root, ["ws", "verify", "--json"])
        assert fresh_verify.exit_code == 0
        payload = json.loads(fresh_verify.output)
        assert payload["fresh"] == 1
        assert payload["stale"] == 0
        assert payload["unknown"] == 0

        # Inject an unknown-revision record (revision None => unverifiable).
        path = _ws_file(populated_instance)
        with path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": "entity",
                        "entity_type": "Part",
                        "entity_id": "BP-9999",
                        "props": {},
                        "lifecycle": None,
                        "review": None,
                        "read_revision": None,
                        "as_of": "2026-01-01T00:00:00+00:00",
                        "receipt_refs": [],
                        "source_cmd": "test",
                    }
                )
                + "\n"
            )
        unknown_verify = _chdir_run(runner, root, ["ws", "verify", "--json"])
        # Unknown records alone do not fail verification.
        assert unknown_verify.exit_code == 0
        payload = json.loads(unknown_verify.output)
        assert payload["unknown"] == 1

        _mutate(populated_instance, populated_graph)
        stale_verify = _chdir_run(runner, root, ["ws", "verify", "--json"])
        assert stale_verify.exit_code == 1
        payload = json.loads(stale_verify.output)
        assert payload["fresh"] == 0
        assert payload["stale"] == 1
        assert payload["unknown"] == 1
        assert payload["current_read_revision"] == populated_instance.get_read_revision()


class TestRefresh:
    def test_refresh_updates_stale_drops_deleted_keeps_fresh(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        # Capture: two entities and two edges at revision R1.
        for args in (
            ["entity", "get", "--type", "Part", "--id", "BP-1001", "--ws", "--json"],
            ["entity", "get", "--type", "Part", "--id", "BP-1002", "--ws", "--json"],
            ["list", "edges", "--ws", "--json"],
        ):
            assert _chdir_run(runner, root, args).exit_code == 0

        # Mutation: rename BP-1001, delete BP-1002 (and every edge touching it).
        new_graph = EntityGraph()
        new_graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-2024-CIVIC-EX",
                properties={
                    "vehicle_id": "V-2024-CIVIC-EX",
                    "year": 2024,
                    "make": "Honda",
                    "model": "Civic",
                },
            )
        )
        new_graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1001",
                properties={
                    "part_number": "BP-1001",
                    "name": "Renamed Brake Pads",
                    "category": "brakes",
                    "price": 59.99,
                },
            )
        )
        new_graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1001",
                to_type="Vehicle",
                to_id="V-2024-CIVIC-EX",
                properties={"verified": True, "source": "catalog"},
            )
        )
        _mutate(populated_instance, new_graph)
        current_revision = populated_instance.get_read_revision()

        # A fresh record captured after the mutation must stay untouched.
        assert (
            _chdir_run(
                runner,
                root,
                ["entity", "get", "--type", "Vehicle", "--id", "V-2024-CIVIC-EX", "--ws", "--json"],
            ).exit_code
            == 0
        )

        refresh = _chdir_run(runner, root, ["ws", "refresh"])
        assert refresh.exit_code == 0
        assert "removed" in refresh.output

        by_identity = _records_by_identity(populated_instance)
        # Stale entity refreshed in place with the new revision and new props.
        refreshed = by_identity[("entity", "Part", "BP-1001")]
        assert refreshed["read_revision"] == current_revision
        assert refreshed["props"] == {"name": "Renamed Brake Pads"}
        assert refreshed["source_cmd"] == "ws refresh"
        # Deleted entity dropped, along with edges owned by it.
        assert ("entity", "Part", "BP-1002") not in by_identity
        assert not any(identity[0] == "edge" and "BP-1002" in identity for identity in by_identity)
        # Surviving stale edge refreshed to the current revision.
        edge = by_identity[("edge", "fits", "Part", "BP-1001", "Vehicle", "V-2024-CIVIC-EX", 0)]
        assert edge["read_revision"] == current_revision
        # Fresh record untouched (not rewritten by refresh).
        fresh = by_identity[("entity", "Vehicle", "V-2024-CIVIC-EX")]
        assert fresh["source_cmd"] == "entity get"

        # After refresh everything verifies clean.
        verify = _chdir_run(runner, root, ["ws", "verify"])
        assert verify.exit_code == 0


class TestClear:
    def test_clear_deletes_only_inside_working_set_dir(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = populated_instance.get_root_path()
        _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        path = _ws_file(populated_instance)
        assert path.exists()

        cleared = _chdir_run(runner, root, ["ws", "clear"])
        assert cleared.exit_code == 0
        assert not path.exists()

        again = _chdir_run(runner, root, ["ws", "clear"])
        assert again.exit_code == 0
        assert "No working-set records file" in again.output

        # Traversal attempt: a hostile instance key is refused outright.
        from cruxible_core.cli.commands import working_set as ws_commands

        outside = isolated_home / "victim.txt"
        outside.write_text("do not delete")
        monkeypatch.setattr(
            ws_commands,
            "_ws_context",
            lambda: ws_commands._WsContext(instance_key="../../victim.txt"),
        )
        refused = _chdir_run(runner, root, ["ws", "clear"])
        assert refused.exit_code != 0
        assert "invalid working-set instance key" in refused.output
        assert outside.exists()

    def test_records_path_rejects_hostile_keys(self) -> None:
        for hostile in ("../x", "a/b", "..", ".hidden", "", "/abs"):
            with pytest.raises(ValueError):
                records_path(hostile)


class TestCorruption:
    def test_corrupt_lines_are_skipped_with_warning_never_crash(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        path = _ws_file(populated_instance)
        with path.open("a") as handle:
            handle.write("{not valid json\n")
            handle.write('"a bare string"\n')

        good_count = 2  # BP-1001 and BP-1002 sampled
        status = _chdir_run(runner, root, ["ws", "status", "--json"])
        assert status.exit_code == 0
        assert "Warning: skipping" in status.output
        json_text = "\n".join(
            line for line in status.output.splitlines() if not line.startswith("Warning:")
        )
        payload = json.loads(json_text[json_text.index("{") :])
        assert payload["record_count"] == good_count

        verify = _chdir_run(runner, root, ["ws", "verify"])
        assert verify.exit_code == 0

        # A further capture still works and keeps the valid records.
        assert (
            _chdir_run(
                runner,
                root,
                ["entity", "get", "--type", "Vehicle", "--id", "V-2024-CIVIC-EX", "--ws", "--json"],
            ).exit_code
            == 0
        )
        records = read_records(path)
        assert len(records) == good_count + 1


class TestStatusAndPath:
    def test_ws_path_prints_records_file(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        result = _chdir_run(runner, root, ["ws", "path"])
        assert result.exit_code == 0
        assert result.output.strip() == str(_ws_file(populated_instance))

    def test_ws_status_reports_counts_and_revisions(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        _chdir_run(runner, root, ["list", "edges", "--ws", "--json"])
        _chdir_run(runner, root, ["sample", "--type", "Vehicle", "--ws", "--json"])
        status = _chdir_run(runner, root, ["ws", "status", "--json"])
        assert status.exit_code == 0
        payload = json.loads(status.output)
        revision = populated_instance.get_read_revision()
        assert payload["kind_counts"] == {"edge": 4, "entity": 2}
        assert payload["type_counts"]["fits"] == 3
        assert payload["type_counts"]["Vehicle"] == 2
        assert payload["current_read_revision"] == revision
        assert payload["newest_cached_revision"] == revision
        assert payload["oldest_cached_revision"] == revision
