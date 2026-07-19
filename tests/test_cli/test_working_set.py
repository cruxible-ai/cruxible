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
    server_instance_key,
    working_set_dir,
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
    monkeypatch.delenv("CRUXIBLE_WORKING_SET_DIR", raising=False)
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


class TestConfigDigest:
    def test_config_reload_marks_records_stale_and_refresh_restamps(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        tmp_project: Path,
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
        records = read_records(_ws_file(populated_instance))
        assert len(records) == 1
        assert isinstance(records[0]["config_digest"], str)

        fresh_verify = _chdir_run(runner, root, ["ws", "verify", "--json"])
        assert fresh_verify.exit_code == 0
        payload = json.loads(fresh_verify.output)
        assert payload["fresh"] == 1
        assert payload["current_config_digest"] == records[0]["config_digest"]

        # Config change WITHOUT any graph mutation: read_revision does not
        # move, so only the config digest can catch the drift.
        config_path = tmp_project / "config.yaml"
        config_path.write_text(
            config_path.read_text().replace(
                "description: Vehicle-to-part fitment",
                "description: Vehicle-to-part fitment (reloaded)",
            )
        )
        stale_verify = _chdir_run(runner, root, ["ws", "verify", "--json"])
        assert stale_verify.exit_code == 1
        payload = json.loads(stale_verify.output)
        assert payload["stale"] == 1
        assert payload["fresh"] == 0
        assert payload["current_config_digest"] != records[0]["config_digest"]

        # Text mode names the config axis, not a phantom revision drift.
        stale_text = _chdir_run(runner, root, ["ws", "verify"])
        assert stale_text.exit_code == 1
        assert "config changed" in stale_text.output

        # Refresh re-fetches and re-stamps with the new digest.
        refresh = _chdir_run(runner, root, ["ws", "refresh"])
        assert refresh.exit_code == 0
        clean_verify = _chdir_run(runner, root, ["ws", "verify", "--json"])
        assert clean_verify.exit_code == 0
        payload = json.loads(clean_verify.output)
        assert payload["fresh"] == 1
        assert payload["stale"] == 0

    def test_record_without_digest_is_unknown_not_fresh(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
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
        path = _ws_file(populated_instance)
        # Simulate a pre-digest record: current revision but no config_digest.
        record = read_records(path)[0]
        legacy = {key: value for key, value in record.items() if key != "config_digest"}
        legacy["entity_id"] = "BP-1002"
        with path.open("a") as handle:
            handle.write(json.dumps(legacy) + "\n")

        verify = _chdir_run(runner, root, ["ws", "verify", "--json"])
        assert verify.exit_code == 0  # unknown alone never fails verification
        payload = json.loads(verify.output)
        assert payload["fresh"] == 1
        assert payload["unknown"] == 1


class TestCredentialScope:
    def test_distinct_credentials_get_distinct_dirs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CRUXIBLE_SERVER_BEARER_TOKEN", "crx_secret_token_aaa")
        key_a = server_instance_key("inst_1")
        key_a_again = server_instance_key("inst_1")
        monkeypatch.setenv("CRUXIBLE_SERVER_BEARER_TOKEN", "crx_secret_token_bbb")
        key_b = server_instance_key("inst_1")

        assert key_a == key_a_again  # stable across invocations (persisted salt)
        assert key_a != key_b  # different credentials never share records
        assert key_a.startswith("inst_1-cred-")
        assert key_b.startswith("inst_1-cred-")
        assert records_path(key_a) != records_path(key_b)
        assert records_path(key_a).parent.parent == records_path(key_b).parent.parent

    def test_scope_never_leaks_token_material(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hashlib

        token = "crx_secret_token_ccc"
        monkeypatch.setenv("CRUXIBLE_SERVER_BEARER_TOKEN", token)
        key = server_instance_key("inst_1")
        assert token not in key
        # Not the raw (unsalted) hash either.
        assert hashlib.sha256(token.encode()).hexdigest()[:12] not in key
        salt_path = working_set_dir() / ".scope-salt"
        assert salt_path.exists()
        assert (salt_path.stat().st_mode & 0o777) == 0o600

    def test_tokenless_server_mode_uses_plain_instance_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
        assert server_instance_key("inst_1") == "inst_1"


class TestRelationshipGetCapture:
    def test_relationship_get_ws_captures_edge_record(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        result = _chdir_run(
            runner,
            root,
            [
                "relationship",
                "get",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1002",
                "--relationship",
                "replaces",
                "--to-type",
                "Part",
                "--to-id",
                "BP-1001",
                "--ws",
                "--json",
            ],
        )
        assert result.exit_code == 0
        records = read_records(_ws_file(populated_instance))
        assert len(records) == 1
        record = records[0]
        assert record["kind"] == "edge"
        assert record["relationship_type"] == "replaces"
        assert record["from_id"] == "BP-1002"
        assert record["to_id"] == "BP-1001"
        assert record["props"]["direction"] == "upgrade"
        assert record["source_cmd"] == "relationship get"
        assert record["read_revision"] == populated_instance.get_read_revision()
        assert isinstance(record["config_digest"], str)

    def test_relationship_get_without_ws_captures_nothing(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        isolated_home: Path,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.get_root_path(),
            [
                "relationship",
                "get",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1002",
                "--relationship",
                "replaces",
                "--to-type",
                "Part",
                "--to-id",
                "BP-1001",
                "--json",
            ],
        )
        assert result.exit_code == 0
        assert not (isolated_home / ".cruxible" / "working-set").exists()


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

    def test_refresh_drops_deleted_edge_despite_depth_truncation(
        self,
        runner: CliRunner,
        initialized_project: CruxibleInstance,
    ) -> None:
        """A deleted cached edge must be dropped even when the owner's scan is
        depth-truncated: the owner keeps another same-type outgoing edge whose
        target has onward same-type edges, so the depth-1 read reports
        ``truncation_reasons == ["depth"]`` — which cannot hide any of the
        owner's own edges and must stay authoritative for edge presence."""
        from cruxible_core.service import service_inspect_entity
        from cruxible_core.service.types import InspectNeighborhoodResult

        instance = initialized_project
        root = instance.get_root_path()

        def _part(part_id: str) -> EntityInstance:
            return EntityInstance(
                entity_type="Part",
                entity_id=part_id,
                properties={"part_number": part_id, "name": part_id, "category": "brakes"},
            )

        def _replaces(from_id: str, to_id: str) -> RelationshipInstance:
            return RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id=from_id,
                to_type="Part",
                to_id=to_id,
                properties={"direction": "upgrade", "confidence": 0.9},
            )

        graph = EntityGraph()
        for part_id in ("P-0", "P-1", "P-2", "P-3"):
            graph.add_entity(_part(part_id))
        graph.add_relationship(_replaces("P-3", "P-1"))  # the edge we cache, then delete
        graph.add_relationship(_replaces("P-3", "P-2"))  # owner keeps this same-type edge
        graph.add_relationship(_replaces("P-2", "P-0"))  # forces depth truncation at P-2
        instance.save_graph(graph)

        capture = _chdir_run(
            runner,
            root,
            [
                "relationship",
                "get",
                "--from-type",
                "Part",
                "--from-id",
                "P-3",
                "--relationship",
                "replaces",
                "--to-type",
                "Part",
                "--to-id",
                "P-1",
                "--ws",
                "--json",
            ],
        )
        assert capture.exit_code == 0
        assert len(read_records(_ws_file(instance))) == 1

        # Delete ONLY the cached edge; everything else survives.
        new_graph = EntityGraph()
        for part_id in ("P-0", "P-1", "P-2", "P-3"):
            new_graph.add_entity(_part(part_id))
        new_graph.add_relationship(_replaces("P-3", "P-2"))
        new_graph.add_relationship(_replaces("P-2", "P-0"))
        instance.save_graph(new_graph)

        # Pin the topology: the owner's scoped depth-1 scan really is
        # depth-truncated (and ONLY depth-truncated).
        scan = service_inspect_entity(
            instance,
            "Part",
            "P-3",
            direction="outgoing",
            depth=1,
            relationship_types=["replaces"],
            max_edges=1000,
        )
        assert isinstance(scan, InspectNeighborhoodResult)
        assert scan.truncated is True
        assert list(scan.truncation_reasons) == ["depth"]

        refresh = _chdir_run(runner, root, ["ws", "refresh"])
        assert refresh.exit_code == 0
        assert "removed" in refresh.output
        assert "edge gone" in refresh.output
        assert "could not confirm" not in refresh.output

        by_identity = _records_by_identity(instance)
        assert not any(identity[0] == "edge" and "P-1" in identity for identity in by_identity)

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

        # Invalid lines make verify loud: exit 1 even with nothing stale.
        verify = _chdir_run(runner, root, ["ws", "verify"])
        assert verify.exit_code == 1

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


class TestHygiene:
    def test_capture_creates_private_dirs_and_files(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        isolated_home: Path,
    ) -> None:
        root = populated_instance.get_root_path()
        capture = _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        assert capture.exit_code == 0
        ws_root = isolated_home / ".cruxible" / "working-set"
        records = _ws_file(populated_instance)
        assert (ws_root.stat().st_mode & 0o777) == 0o700
        assert (records.parent.stat().st_mode & 0o777) == 0o700
        assert (records.stat().st_mode & 0o777) == 0o600

    def test_rewrite_keeps_records_file_private(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        root = populated_instance.get_root_path()
        capture = _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        assert capture.exit_code == 0
        # Refresh rewrites atomically (temp file + rename): mode must survive.
        assert _chdir_run(runner, root, ["ws", "refresh"]).exit_code == 0
        assert (_ws_file(populated_instance).stat().st_mode & 0o777) == 0o600

    def test_preexisting_lax_modes_are_tightened_on_next_capture(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        isolated_home: Path,
    ) -> None:
        """Idempotent hygiene: a cache created with lax modes (e.g. by an older
        build or a manual copy) — including a lax working-set ROOT — is
        tightened the next time a write touches it, not only at creation."""
        import os as _os

        root = populated_instance.get_root_path()
        records = _ws_file(populated_instance)
        ws_root = isolated_home / ".cruxible" / "working-set"
        records.parent.mkdir(parents=True)
        _os.chmod(ws_root, 0o755)
        _os.chmod(records.parent, 0o755)
        records.write_text(HEADER_LINE + "\n")
        _os.chmod(records, 0o644)

        capture = _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        assert capture.exit_code == 0
        assert (ws_root.stat().st_mode & 0o777) == 0o700
        assert (records.parent.stat().st_mode & 0o777) == 0o700
        assert (records.stat().st_mode & 0o777) == 0o600
        # And the capture actually appended through the tightened file.
        assert read_records(records)

    def test_env_root_parents_are_never_chmodded(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tightening stops at the configured root: a user-supplied env root's
        PARENT directories are not ours to manage."""
        import os as _os

        root = populated_instance.get_root_path()
        parent = tmp_path / "user-owned-parent"
        parent.mkdir()
        _os.chmod(parent, 0o755)
        env_root = parent / "ws-root"
        _os.chmod(env_root.parent, 0o755)
        monkeypatch.setenv("CRUXIBLE_WORKING_SET_DIR", str(env_root))

        capture = _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        assert capture.exit_code == 0
        assert (parent.stat().st_mode & 0o777) == 0o755  # untouched
        assert (env_root.stat().st_mode & 0o777) == 0o700  # root itself healed


_SYMLINK_LEVELS = ("root", "instance-dir", "records-file")


class TestSymlinkProtection:
    """Full level x verb refusal matrix.

    Levels: the configured working-set ROOT, the instance DIRECTORY, and the
    records FILE — each replaced by a symlink whose target is pre-populated
    with a plausible cache, so a follow-through would have real data to read,
    rewrite, or delete. Verbs: capture (the read itself succeeds; capture
    refuses with a stderr warning) and ws verify / refresh / clear (usage
    error, exit non-zero). The full matrix is exercised because each verb has
    a DIFFERENT first filesystem touch (append, read, rewrite, unlink) and
    each level is refused at a different chain position — a representative
    pair would leave e.g. the root-level read path unpinned (the exact hole
    this matrix regression-tests).
    """

    def _plant_symlink(
        self, level: str, instance: CruxibleInstance, home: Path
    ) -> tuple[Path, str]:
        """Replace *level* with a symlink; return (target records file, content)."""
        key = local_instance_key(instance.get_root_path())
        ws_root = home / ".cruxible" / "working-set"
        outside = home / "outside"
        content = (
            HEADER_LINE
            + "\n"
            + json.dumps(
                {"kind": "entity", "entity_type": "Part", "entity_id": "BP-X", "props": {}}
            )
            + "\n"
        )
        if level == "root":
            (outside / key).mkdir(parents=True)
            target = outside / key / "records.jsonl"
            target.write_text(content)
            ws_root.parent.mkdir(parents=True, exist_ok=True)
            ws_root.symlink_to(outside, target_is_directory=True)
            return target, content
        if level == "instance-dir":
            outside.mkdir()
            target = outside / "records.jsonl"
            target.write_text(content)
            ws_root.mkdir(parents=True)
            (ws_root / key).symlink_to(outside, target_is_directory=True)
            return target, content
        assert level == "records-file"
        outside.mkdir()
        target = outside / "records.jsonl"
        target.write_text(content)
        (ws_root / key).mkdir(parents=True)
        (ws_root / key / "records.jsonl").symlink_to(target)
        return target, content

    @pytest.mark.parametrize("level", _SYMLINK_LEVELS)
    def test_capture_refuses_symlinked_level(
        self,
        level: str,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        isolated_home: Path,
    ) -> None:
        target, content = self._plant_symlink(level, populated_instance, isolated_home)
        result = _chdir_run(
            runner,
            populated_instance.get_root_path(),
            ["sample", "--type", "Part", "--ws", "--json"],
        )
        assert result.exit_code == 0  # the read itself is never affected
        assert "symlink" in result.output
        assert target.read_text() == content  # nothing written through

    @pytest.mark.parametrize("level", _SYMLINK_LEVELS)
    @pytest.mark.parametrize("verb", ("verify", "refresh", "clear"))
    def test_ws_verbs_refuse_symlinked_level(
        self,
        level: str,
        verb: str,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        isolated_home: Path,
    ) -> None:
        target, content = self._plant_symlink(level, populated_instance, isolated_home)
        result = _chdir_run(runner, populated_instance.get_root_path(), ["ws", verb])
        assert result.exit_code != 0
        assert "symlink" in result.output
        # Validation precedes reading: no classification/refresh/clear output.
        assert "fresh=" not in result.output
        assert "Refreshed" not in result.output
        assert "Cleared" not in result.output
        assert target.read_text() == content  # never read-through-then-write, never unlinked

    def test_symlinked_env_root_is_refused(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        isolated_home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Codex repro: CRUXIBLE_WORKING_SET_DIR pointing at a symlink must
        not be followed by capture or any ws verb."""
        real_root = tmp_path / "real-root"
        real_root.mkdir()
        env_root = tmp_path / "symlinked-root"
        env_root.symlink_to(real_root, target_is_directory=True)
        monkeypatch.setenv("CRUXIBLE_WORKING_SET_DIR", str(env_root))

        root = populated_instance.get_root_path()
        capture = _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        assert capture.exit_code == 0
        assert "symlink" in capture.output
        assert list(real_root.iterdir()) == []  # nothing written through the root

        verify = _chdir_run(runner, root, ["ws", "verify"])
        assert verify.exit_code != 0
        assert "symlink" in verify.output


class TestRecordValidation:
    def test_wrong_shaped_lines_are_skipped_counted_and_never_fresh(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
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
        path = _ws_file(populated_instance)
        current_revision = populated_instance.get_read_revision()
        wrong_shaped = [
            {"kind": "bogus", "entity_type": "Part", "entity_id": "X", "props": {}},
            # Would be "fresh" by revision but has a non-string identity field.
            {
                "kind": "entity",
                "entity_type": "Part",
                "entity_id": 5,
                "props": {},
                "read_revision": current_revision,
            },
            {"kind": "entity", "entity_type": "Part", "entity_id": "P-L", "props": []},
            {
                "kind": "edge",
                "relationship_type": "fits",
                "from_type": "Part",
                "from_id": "A",
                "to_type": "Vehicle",
                "to_id": "B",
                "edge_key": "zero",
                "props": {},
            },
        ]
        with path.open("a") as handle:
            for record in wrong_shaped:
                handle.write(json.dumps(record) + "\n")
            handle.write("{not json\n")

        status = _chdir_run(runner, root, ["ws", "status", "--json"])
        assert status.exit_code == 0
        assert "Warning: skipping" in status.output
        json_text = "\n".join(
            line for line in status.output.splitlines() if not line.startswith("Warning:")
        )
        payload = json.loads(json_text[json_text.index("{") :])
        assert payload["record_count"] == 1
        assert payload["invalid_lines"] == 5

        verify = _chdir_run(runner, root, ["ws", "verify", "--json"])
        # Invalid lines are loud: exit 1 even though nothing is stale.
        assert verify.exit_code == 1
        json_text = "\n".join(
            line for line in verify.output.splitlines() if not line.startswith("Warning:")
        )
        payload = json.loads(json_text[json_text.index("{") :])
        # The invalid lines are counted — and never classified fresh.
        assert payload["invalid"] == 5
        assert payload["fresh"] == 1
        assert payload["total"] == 1
        assert payload["stale"] == 0

    def test_validate_record_reasons(self) -> None:
        from cruxible_core.working_set import validate_record

        assert (
            validate_record({"kind": "entity", "entity_type": "T", "entity_id": "I", "props": {}})
            is None
        )
        assert validate_record("nope") == "not a JSON object"
        assert validate_record({"kind": "widget"}) == "unknown kind 'widget'"
        assert (
            validate_record({"kind": "entity", "entity_type": "T", "entity_id": "", "props": {}})
            == "missing or non-string identity field 'entity_id'"
        )
        assert (
            validate_record(
                {
                    "kind": "entity",
                    "entity_type": "T",
                    "entity_id": "I",
                    "props": {},
                    "read_revision": True,
                }
            )
            == "read_revision must be an integer or null"
        )
        assert (
            validate_record(
                {
                    "kind": "entity",
                    "entity_type": "T",
                    "entity_id": "I",
                    "props": {},
                    "config_digest": 7,
                }
            )
            == "config_digest must be a string or null"
        )


class TestHonestFreshness:
    def test_unresolvable_current_digest_is_unknown_not_fresh(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the CURRENT config digest cannot be resolved, the config axis
        is unverifiable — records must be unknown, never quietly fresh."""
        from cruxible_core.cli.commands import working_set as ws_commands

        root = populated_instance.get_root_path()
        assert (
            _chdir_run(
                runner,
                root,
                ["entity", "get", "--type", "Part", "--id", "BP-1001", "--ws", "--json"],
            ).exit_code
            == 0
        )
        monkeypatch.setattr(ws_commands, "_current_config_digest", lambda context: None)
        verify = _chdir_run(runner, root, ["ws", "verify", "--json"])
        assert verify.exit_code == 0  # unknown alone never fails verification
        payload = json.loads(verify.output)
        assert payload["fresh"] == 0
        assert payload["unknown"] == 1
        assert payload["stale"] == 0


class TestEnvDirResolution:
    def test_env_dir_overrides_default_root_for_capture_and_ws_verbs(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        isolated_home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = populated_instance.get_root_path()
        env_root = tmp_path / "mcp-ws-root"
        monkeypatch.setenv("CRUXIBLE_WORKING_SET_DIR", str(env_root))

        # Capture and every ws verb resolve through the env-configured root.
        capture = _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        assert capture.exit_code == 0
        expected = env_root / local_instance_key(root) / "records.jsonl"
        assert expected.exists()
        assert not (isolated_home / ".cruxible" / "working-set").exists()

        path_result = _chdir_run(runner, root, ["ws", "path"])
        assert path_result.output.strip() == str(expected)
        status = _chdir_run(runner, root, ["ws", "status", "--json"])
        assert status.exit_code == 0
        assert json.loads(status.output)["record_count"] == 2

        # Precedence: explicit env > default home dir.
        monkeypatch.delenv("CRUXIBLE_WORKING_SET_DIR")
        default_path = _chdir_run(runner, root, ["ws", "path"])
        assert default_path.output.strip() == str(
            isolated_home / ".cruxible" / "working-set" / local_instance_key(root) / "records.jsonl"
        )


class _WorkingSetTouched(AssertionError):
    """Marker: a mutation path touched the working-set cache."""


class _StubWriteClient:
    """Server-mode stub covering every client call the write commands make."""

    def get_entity(self, instance_id, entity_type, entity_id, profile=None):
        from cruxible_client import contracts

        # BP-1001 "exists" (entity update passes); BP-9000 does not (add passes).
        return contracts.GetEntityResult(
            found=entity_id == "BP-1001",
            entity_type=entity_type,
            entity_id=entity_id,
            properties={},
            metadata={},
        )

    def get_relationship(self, instance_id, **kwargs):
        from cruxible_client import contracts

        return contracts.GetRelationshipResult(
            found=False,
            from_type=kwargs["from_type"],
            from_id=kwargs["from_id"],
            relationship_type=kwargs["relationship_type"],
            to_type=kwargs["to_type"],
            to_id=kwargs["to_id"],
        )

    def batch_direct_write(self, instance_id, payload, *, dry_run=False):
        from cruxible_client import contracts

        return contracts.BatchDirectWriteResult(
            dry_run=dry_run,
            valid=True,
            entities_added=len(payload.entities),
            relationships_added=len(payload.relationships),
            receipt_id="RCPT-WRITE",
        )

    def workflow_apply(self, instance_id, **kwargs):
        from cruxible_client import contracts

        return contracts.WorkflowApplyResult(
            workflow=kwargs["workflow_name"],
            output={},
            receipt_id="RCPT-APPLY",
        )

    def propose_workflow(self, instance_id, **kwargs):
        from cruxible_client import contracts

        return contracts.WorkflowProposeResult(
            workflow=kwargs["workflow_name"],
            output={},
            receipt_id="RCPT-PROPOSE",
            group_status="no_candidates",
            review_priority="normal",
        )


class TestMutationBoundary:
    def test_write_paths_never_touch_the_working_set(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Structural insurance for the write-only invariant: every reader and
        writer of the cache is replaced with a bomb, then representative write
        commands run to a SUCCESSFUL completion (server mode, stub client) —
        proving the full write code path never imports or reads the cache."""
        import cruxible_core.cli.working_set as cli_ws
        import cruxible_core.working_set as core_ws

        def _bomb(*args: object, **kwargs: object) -> None:
            raise _WorkingSetTouched("write path touched the working-set cache")

        for module in (core_ws, cli_ws):
            for name in (
                "read_records",
                "read_records_detailed",
                "iter_record_lines",
                "append_records",
                "write_records",
                "capture_read_payload",
            ):
                monkeypatch.setattr(module, name, _bomb)
        monkeypatch.setattr(cli_ws, "capture_json_read", _bomb)
        monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
        monkeypatch.setattr(
            "cruxible_core.cli.commands._common._get_client", lambda: _StubWriteClient()
        )

        payload_file = tmp_path / "batch.json"
        payload_file.write_text(
            json.dumps(
                {
                    "entities": [
                        {
                            "entity_type": "Part",
                            "entity_id": "BP-9001",
                            "properties": {"part_number": "BP-9001", "name": "Pad"},
                        }
                    ],
                    "relationships": [],
                }
            )
        )
        server = ["--server-url", "http://server", "--instance-id", "inst_x"]
        write_commands = [
            [
                "entity",
                "add",
                "Part",
                "BP-9000",
                "--set",
                "part_number=BP-9000",
                "--set",
                "name=New Part",
                "--set",
                "category=brakes",
            ],
            ["entity", "update", "Part", "BP-1001", "--set", "name=Renamed"],
            [
                "relationship",
                "add",
                "replaces",
                "Part",
                "BP-1001",
                "Part",
                "BP-1002",
                "--set",
                "direction=downgrade",
            ],
            ["batch-direct-write", "--payload-file", str(payload_file)],
            ["apply", "--workflow", "wf", "--apply-digest", "sha256:abc", "--json"],
            ["propose", "--workflow", "wf"],
        ]
        root = populated_instance.get_root_path()
        for args in write_commands:
            result = _chdir_run(runner, root, [*server, *args])
            assert not isinstance(result.exception, _WorkingSetTouched), args
            assert "_WorkingSetTouched" not in result.output, args
            assert result.exit_code == 0, (args, result.output)


class TestArchiveExclusion:
    def test_instance_backup_never_includes_working_set(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The cache lives outside the instance state dir by construction; pin
        that the backup artifact stays working-set-free even when the cache is
        deliberately rooted INSIDE the instance root via the env override."""
        import zipfile

        from cruxible_core.service.snapshots import service_backup_instance

        root = populated_instance.get_root_path()
        monkeypatch.setenv("CRUXIBLE_WORKING_SET_DIR", str(root / "ws-cache"))
        capture = _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        assert capture.exit_code == 0
        assert (root / "ws-cache").exists()

        artifact = tmp_path / "backup.zip"
        service_backup_instance(
            populated_instance,
            instance_id="inst-backup-test",
            artifact_path=artifact,
        )
        with zipfile.ZipFile(artifact) as archive:
            members = archive.namelist()
        assert members  # a real archive was produced
        for member in members:
            assert "working-set" not in member
            assert "ws-cache" not in member
            assert "records.jsonl" not in member
        # The archive is an explicit allowlist of instance state artifacts.
        # This allowlist is ALSO the chokepoint for instance transfer:
        # service_relocate_instance is implemented strictly as
        # service_backup_instance -> service_restore_instance, so any member
        # this pin excludes can never ride along a transfer either.
        assert set(members) <= {
            "manifest.json",
            "state.db",
            "config.yaml",
            "instance.json",
            "workflow.lock",
        }

    def _plant_cache_in_root(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Path:
        """Root a live working-set cache INSIDE the instance root (worst case)."""
        root = populated_instance.get_root_path()
        monkeypatch.setenv("CRUXIBLE_WORKING_SET_DIR", str(root / "ws-cache"))
        capture = _chdir_run(runner, root, ["sample", "--type", "Part", "--ws", "--json"])
        assert capture.exit_code == 0
        assert (root / "ws-cache").exists()
        return root

    def test_snapshot_artifacts_never_include_working_set(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Snapshot producer pin: _write_snapshot builds an explicit artifact
        dict (graph.json / config.yaml / optional lock / snapshot.json) — never
        a directory sweep of the instance root."""
        from cruxible_core.service.snapshots import service_create_snapshot

        self._plant_cache_in_root(runner, populated_instance, monkeypatch)
        snapshot = service_create_snapshot(populated_instance, label="ws-pin").snapshot
        artifact_names = set(populated_instance._read_snapshot_artifacts(snapshot.snapshot_id))
        assert artifact_names  # a real snapshot was produced
        assert artifact_names <= {
            "snapshot.json",
            "graph.json",
            "config.yaml",
            "cruxible.lock.yaml",
        }

    def test_state_publish_bundle_never_includes_working_set(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """State-publication producer pin: build_release_bundle is the single
        artifact-assembly chokepoint for service_publish_state (the transport
        only ships the bundle directory verbatim), and it copies an explicit
        name list from the snapshot export plus manifest.json."""
        from cruxible_core.service.snapshots import service_create_snapshot
        from cruxible_core.service.state import build_release_bundle

        self._plant_cache_in_root(runner, populated_instance, monkeypatch)
        snapshot = service_create_snapshot(populated_instance, label="release").snapshot
        bundle_dir = build_release_bundle(
            instance=populated_instance,
            snapshot_id=snapshot.snapshot_id,
            state_id="ws-pin-state",
            release_id="r1",
            compatibility="data_only",
            parent_release_id=None,
        )
        members = {member.name for member in bundle_dir.iterdir()}
        assert "manifest.json" in members  # a real bundle was produced
        assert members <= {
            "manifest.json",
            "snapshot.json",
            "graph.json",
            "config.yaml",
            "cruxible.lock.yaml",
        }

    def test_instance_relocate_never_carries_working_set(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Instance-transfer smoke: relocate rides the backup artifact
        (backup -> restore), so the backup allowlist is its structural
        chokepoint — and the relocated tree really is working-set-free even
        with the cache rooted inside the SOURCE instance root."""
        from cruxible_core.service.snapshots import service_relocate_instance

        self._plant_cache_in_root(runner, populated_instance, monkeypatch)
        # A sibling of the instance root (relocate refuses nested targets).
        target = tmp_path_factory.mktemp("relocate-target") / "relocated"
        service_relocate_instance(
            populated_instance,
            instance_id="inst-relocate-test",
            to_dir=target,
            instance_mode="dev",
        )
        transferred = {
            str(item.relative_to(target)) for item in target.rglob("*") if item.is_file()
        }
        assert transferred  # a real instance landed
        for member in transferred:
            assert "working-set" not in member
            assert "ws-cache" not in member
            assert "records.jsonl" not in member


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
