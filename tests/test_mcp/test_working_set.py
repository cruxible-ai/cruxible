"""MCP working-set capture tests (opt-in via CRUXIBLE_WORKING_SET_DIR).

Pins the wi-working-set-hardening MCP bridge: entity/edge-shaped read results
flowing through the MCP handlers are ALSO captured as working-set records
rooted at the env-configured directory; when the variable is unset the hook
is a hard no-op (isolation test); server mode reuses the credential-scoped
instance key derivation; capture failures never break a read.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client import contracts
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.mcp import handlers
from cruxible_core.mcp import working_set as mcp_working_set
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.working_set import (
    local_instance_key,
    read_records,
    records_path,
)

ENV_DIR = "CRUXIBLE_WORKING_SET_DIR"


@pytest.fixture(autouse=True)
def isolated_capture_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate HOME and clear the capture env vars for every test."""
    home = tmp_path / "mcp-ws-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(ENV_DIR, raising=False)
    monkeypatch.delenv("CRUXIBLE_WORKING_SET", raising=False)
    return home


@pytest.fixture
def env_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Opt in to MCP capture rooted at a test directory."""
    root = tmp_path / "mcp-working-set"
    monkeypatch.setenv(ENV_DIR, str(root))
    return root


@pytest.fixture
def local_instance(tmp_project: Path) -> CruxibleInstance:
    """Dev-mode local instance with one entity pair and one edge."""
    instance = CruxibleInstance.init(tmp_project, "config.yaml")
    graph = instance.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1",
            properties={"part_number": "BP-1", "name": "Brake Pads", "category": "brakes"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1", "year": 2024, "make": "Honda", "model": "Civic"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": True, "source": "catalog"},
        )
    )
    instance.save_graph(graph)
    return instance


class TestHardNoOpWhenUnset:
    def test_gate_short_circuits_before_any_capture_work(
        self,
        local_instance: CruxibleInstance,
        isolated_capture_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the env unset, the hook must return BEFORE any capture work.

        ``_capture`` (everything past the gate) is replaced with a bomb: if
        the gate ever lets a call through, the read blows up. It doesn't —
        and no working-set directory appears anywhere under HOME either.
        """

        def _bomb(*args: object, **kwargs: object) -> None:
            raise AssertionError("capture ran despite CRUXIBLE_WORKING_SET_DIR being unset")

        monkeypatch.setattr(mcp_working_set, "_capture", _bomb)
        result = handlers.handle_get_entity(str(local_instance.get_root_path()), "Part", "BP-1")
        assert result.found is True
        assert not (isolated_capture_env / ".cruxible" / "working-set").exists()

    def test_result_is_identical_with_and_without_capture(
        self,
        local_instance: CruxibleInstance,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance_id = str(local_instance.get_root_path())
        without = handlers.handle_get_entity(instance_id, "Part", "BP-1")
        monkeypatch.setenv(ENV_DIR, str(tmp_path / "mcp-working-set"))
        with_capture = handlers.handle_get_entity(instance_id, "Part", "BP-1")
        assert with_capture == without


class TestLocalModeCapture:
    def test_read_tools_capture_records_rooted_at_env_dir(
        self,
        local_instance: CruxibleInstance,
        env_dir: Path,
    ) -> None:
        instance_id = str(local_instance.get_root_path())
        handlers.handle_get_entity(instance_id, "Part", "BP-1")
        handlers.handle_list(instance_id, "entities", entity_type="Vehicle")
        handlers.handle_get_relationship(instance_id, "Part", "BP-1", "fits", "Vehicle", "V-1")

        key = local_instance_key(local_instance.get_root_path())
        path = env_dir / key / "records.jsonl"
        assert path == records_path(key)
        records = read_records(path)
        by_identity = {
            (record["kind"], record.get("entity_id") or record.get("to_id")): record
            for record in records
        }
        revision = local_instance.get_read_revision()

        part = by_identity[("entity", "BP-1")]
        assert part["source_cmd"] == "cruxible_get_entity"
        assert part["read_revision"] == revision
        assert isinstance(part["config_digest"], str)

        vehicle = by_identity[("entity", "V-1")]
        assert vehicle["source_cmd"] == "cruxible_list"
        assert vehicle["read_revision"] == revision

        edge = by_identity[("edge", "V-1")]
        assert edge["relationship_type"] == "fits"
        assert edge["source_cmd"] == "cruxible_get_relationship"
        # GetRelationshipResult carries no envelope revision; the local
        # instance's current revision is the fallback.
        assert edge["read_revision"] == revision

    def test_inspect_and_sample_capture(
        self,
        local_instance: CruxibleInstance,
        env_dir: Path,
    ) -> None:
        instance_id = str(local_instance.get_root_path())
        handlers.handle_inspect_entity(instance_id, "Part", "BP-1", depth=1)
        handlers.handle_sample(instance_id, "Vehicle", 5)
        records = read_records(
            env_dir / local_instance_key(local_instance.get_root_path()) / "records.jsonl"
        )
        kinds = {record["kind"] for record in records}
        assert kinds == {"entity", "edge"}
        sources = {record["source_cmd"] for record in records}
        assert "cruxible_inspect_entity" in sources
        assert "cruxible_sample" in sources

    def test_nothing_written_under_home(
        self,
        local_instance: CruxibleInstance,
        env_dir: Path,
        isolated_capture_env: Path,
    ) -> None:
        handlers.handle_get_entity(str(local_instance.get_root_path()), "Part", "BP-1")
        assert env_dir.exists()
        assert not (isolated_capture_env / ".cruxible").exists()


class TestServerModeCapture:
    def test_capture_uses_credential_scoped_instance_key(
        self,
        env_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = "crx_secret_mcp_token"
        monkeypatch.setenv("CRUXIBLE_SERVER_URL", "http://server")
        monkeypatch.setenv("CRUXIBLE_SERVER_BEARER_TOKEN", token)

        class StubClient:
            def __init__(self, *, base_url=None, socket_path=None, token=None):
                pass

            def close(self):
                pass

            def get_entity(self, instance_id, entity_type, entity_id, profile=None):
                return contracts.GetEntityResult(
                    found=True,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    properties={"name": "Brake Pads"},
                    metadata={},
                    read_revision=7,
                )

            def config_status(self, instance_id):
                return SimpleNamespace(
                    provenance=SimpleNamespace(active_config_digest="digest-live")
                )

        monkeypatch.setattr(handlers, "CruxibleClient", StubClient)
        handlers.reset_client_cache()
        try:
            result = handlers.handle_get_entity("inst_9", "Part", "BP-1")
        finally:
            handlers.reset_client_cache()
        assert result.found is True

        instance_dirs = [p.name for p in env_dir.iterdir() if p.is_dir()]
        assert len(instance_dirs) == 1
        key = instance_dirs[0]
        # Credential-scoped, and no token material leaks into the key.
        assert key.startswith("inst_9-cred-")
        assert token not in key

        records = read_records(env_dir / key / "records.jsonl")
        assert len(records) == 1
        record = records[0]
        # Revision from the response payload; digest from the daemon status.
        assert record["read_revision"] == 7
        assert record["config_digest"] == "digest-live"
        assert record["source_cmd"] == "cruxible_get_entity"


class TestCaptureNeverBreaksReads:
    def test_capture_failure_downgrades_to_stderr_warning(
        self,
        local_instance: CruxibleInstance,
        env_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("cache exploded")

        monkeypatch.setattr(mcp_working_set, "capture_read_payload", _boom)
        result = handlers.handle_get_entity(str(local_instance.get_root_path()), "Part", "BP-1")
        assert result.found is True
        assert "working-set capture failed" in capsys.readouterr().err

    def test_symlinked_records_file_is_refused_with_warning(
        self,
        local_instance: CruxibleInstance,
        env_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        instance_id = str(local_instance.get_root_path())
        key = local_instance_key(local_instance.get_root_path())
        target = tmp_path / "outside.jsonl"
        target.write_text("untouched\n")
        (env_dir / key).mkdir(parents=True)
        (env_dir / key / "records.jsonl").symlink_to(target)

        result = handlers.handle_get_entity(instance_id, "Part", "BP-1")
        assert result.found is True
        assert "symlink" in capsys.readouterr().err
        assert target.read_text() == "untouched\n"
