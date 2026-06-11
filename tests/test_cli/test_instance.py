"""Tests for CruxibleInstance (.cruxible/ management)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, InstanceNotFoundError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.store import GroupStore
from cruxible_core.snapshot.types import UpstreamMetadata
from cruxible_core.storage.sqlite import SQLiteGraphRepository


class TestInit:
    def test_creates_instance_dir(self, tmp_project: Path) -> None:
        CruxibleInstance.init(tmp_project, "config.yaml")
        assert (tmp_project / ".cruxible").is_dir()
        assert (tmp_project / ".cruxible" / "instance.json").exists()
        assert (tmp_project / ".cruxible" / "state.db").exists()
        assert not (tmp_project / ".cruxible" / "graph.json").exists()

    def test_instance_json_metadata(self, tmp_project: Path) -> None:
        CruxibleInstance.init(tmp_project, "config.yaml", data_dir="data")
        meta = json.loads((tmp_project / ".cruxible" / "instance.json").read_text())
        assert meta["config_path"] == "config.yaml"
        assert meta["data_dir"] == "data"
        assert meta["instance_mode"] == CruxibleInstance.DEV_MODE
        assert "created_at" in meta
        assert "version" in meta

    def test_rejects_invalid_config(self, tmp_path: Path) -> None:
        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text("not_valid: true\n")
        with pytest.raises(ConfigError):
            CruxibleInstance.init(tmp_path, "bad.yaml")

    def test_rejects_missing_config(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            CruxibleInstance.init(tmp_path, "nonexistent.yaml")


class TestLoad:
    def test_loads_from_root(self, initialized_project: CruxibleInstance) -> None:
        loaded = CruxibleInstance.load(initialized_project.root)
        assert loaded.root == initialized_project.root
        assert loaded.metadata.config_path == "config.yaml"

    def test_walks_up_to_find_instance(self, initialized_project: CruxibleInstance) -> None:
        subdir = initialized_project.root / "subdir" / "nested"
        subdir.mkdir(parents=True)
        loaded = CruxibleInstance.load(subdir)
        assert loaded.root == initialized_project.root

    def test_raises_when_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(InstanceNotFoundError):
            CruxibleInstance.load(tmp_path)


class TestMetadata:
    def test_missing_instance_mode_defaults_to_dev(
        self, initialized_project: CruxibleInstance
    ) -> None:
        metadata_path = initialized_project.instance_dir / "instance.json"
        meta = json.loads(metadata_path.read_text())
        del meta["instance_mode"]
        metadata_path.write_text(json.dumps(meta))

        loaded = CruxibleInstance.load(initialized_project.root)

        assert loaded.get_instance_mode() == CruxibleInstance.DEV_MODE
        assert loaded.is_dev_mode()

    def test_invalid_instance_mode_raises(self, initialized_project: CruxibleInstance) -> None:
        metadata_path = initialized_project.instance_dir / "instance.json"
        meta = json.loads(metadata_path.read_text())
        meta["instance_mode"] = "other"
        metadata_path.write_text(json.dumps(meta))

        with pytest.raises(ConfigError, match="Invalid instance metadata"):
            CruxibleInstance.load(initialized_project.root)

    def test_set_config_path_persists_through_metadata_model(
        self, initialized_project: CruxibleInstance
    ) -> None:
        alt_config = initialized_project.root / "alt.yaml"
        alt_config.write_text((initialized_project.root / "config.yaml").read_text())

        initialized_project.set_config_path("alt.yaml")

        meta = json.loads((initialized_project.instance_dir / "instance.json").read_text())
        assert meta["config_path"] == "alt.yaml"
        assert CruxibleInstance.load(initialized_project.root).metadata.config_path == "alt.yaml"

    def test_unknown_metadata_fields_survive_rewrite(
        self, initialized_project: CruxibleInstance
    ) -> None:
        metadata_path = initialized_project.instance_dir / "instance.json"
        meta = json.loads(metadata_path.read_text())
        meta["future_field"] = {"kept": True}
        metadata_path.write_text(json.dumps(meta))

        loaded = CruxibleInstance.load(initialized_project.root)
        loaded.set_config_path("config.yaml")

        rewritten = json.loads(metadata_path.read_text())
        assert rewritten["future_field"] == {"kept": True}

    def test_upstream_metadata_round_trips(self, initialized_project: CruxibleInstance) -> None:
        upstream = UpstreamMetadata(
            state_id="state",
            release_id="v1",
            snapshot_id="snap_1",
            compatibility="data_only",
            transport_ref="file:///tmp/world",
            owned_entity_types=["Vehicle"],
            owned_relationship_types=["fits"],
        )

        initialized_project.set_upstream_metadata(upstream)
        loaded = CruxibleInstance.load(initialized_project.root)

        assert loaded.get_upstream_metadata() == upstream
        raw = json.loads((initialized_project.instance_dir / "instance.json").read_text())
        assert raw["upstream"]["state_id"] == "state"
        assert raw["upstream"]["transport_ref"] == "file:///tmp/world"

    def test_snapshot_metadata_round_trips(
        self, initialized_project: CruxibleInstance, tmp_path: Path
    ) -> None:
        snapshot = initialized_project.create_snapshot(label="baseline")
        assert initialized_project.get_head_snapshot_id() == snapshot.snapshot_id

        clone, _ = CruxibleInstance.clone_from_snapshot(
            initialized_project,
            snapshot.snapshot_id,
            tmp_path / "clone",
        )

        reloaded = CruxibleInstance.load(clone.root)
        assert reloaded.metadata.head_snapshot_id == snapshot.snapshot_id
        assert reloaded.metadata.origin_snapshot_id == snapshot.snapshot_id


class TestGraphPersistence:
    def test_save_and_load_empty_graph(self, initialized_project: CruxibleInstance) -> None:
        graph = initialized_project.load_graph()
        assert graph.entity_count() == 0
        assert graph.edge_count() == 0

    def test_roundtrip_entities(self, initialized_project: CruxibleInstance) -> None:
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-1",
                properties={"make": "Honda", "year": 2024},
            )
        )
        initialized_project.save_graph(graph)

        loaded = initialized_project.load_graph()
        assert loaded.entity_count() == 1
        entity = loaded.get_entity("Vehicle", "V-1")
        assert entity is not None
        assert entity.properties["make"] == "Honda"

    def test_roundtrip_relationships(self, initialized_project: CruxibleInstance) -> None:
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": True},
            )
        )
        initialized_project.save_graph(graph)

        loaded = initialized_project.load_graph()
        assert loaded.edge_count() == 1
        assert loaded.has_relationship("Part", "P-1", "Vehicle", "V-1", "fits")

    def test_entities_by_type_rebuilt(self, initialized_project: CruxibleInstance) -> None:
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={}))
        initialized_project.save_graph(graph)

        loaded = initialized_project.load_graph()
        assert len(loaded.list_entities("Part")) == 2
        assert len(loaded.list_entities("Vehicle")) == 1

    def test_edge_counter_rebuilt(self, initialized_project: CruxibleInstance) -> None:
        """After load, new edges should not collide with existing keys."""
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={},
            )
        )
        initialized_project.save_graph(graph)

        loaded = initialized_project.load_graph()
        # Adding another relationship should work without key collision
        loaded.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-2",
                to_type="Vehicle",
                to_id="V-1",
                properties={},
            )
        )
        assert loaded.edge_count() == 2


class TestGraphRoundTrip:
    """Integration test: save_graph/load_graph preserves full graph state."""

    def test_entities_and_relationships_preserved(
        self, initialized_project: CruxibleInstance
    ) -> None:
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P-1", properties={"name": "Widget"})
        )
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P-2", properties={"name": "Gizmo"})
        )
        graph.add_entity(
            EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={"make": "Honda"})
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"confidence": 0.95},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P-2",
                to_type="Vehicle",
                to_id="V-1",
                properties={"confidence": 0.8},
            )
        )
        initialized_project.save_graph(graph)

        loaded = initialized_project.load_graph()
        assert loaded.entity_count() == 3
        assert loaded.edge_count() == 2
        rel = loaded.get_relationship("Part", "P-1", "Vehicle", "V-1", "fits")
        assert rel is not None
        assert rel.properties["confidence"] == 0.95


class TestGraphCache:
    def test_load_graph_caches(self, initialized_project: CruxibleInstance) -> None:
        """Second load_graph call returns same object (identity check)."""
        g1 = initialized_project.load_graph()
        g2 = initialized_project.load_graph()
        assert g1 is g2

    def test_save_graph_updates_cache(self, initialized_project: CruxibleInstance) -> None:
        """save_graph sets cache so load_graph returns saved graph without re-read."""
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P-1", properties={"name": "Pad"})
        )
        initialized_project.save_graph(graph)
        loaded = initialized_project.load_graph()
        assert loaded is graph

    def test_cache_survives_across_calls(self, tmp_project: Path) -> None:
        """init → ingest-style save → load_graph twice → same object."""
        instance = CruxibleInstance.init(tmp_project, "config.yaml")
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={"make": "Honda"})
        )
        instance.save_graph(graph)
        g1 = instance.load_graph()
        g2 = instance.load_graph()
        assert g1 is g2
        assert g1 is graph

    def test_invalidate_graph_cache(self, initialized_project: CruxibleInstance) -> None:
        """invalidate_graph_cache forces re-read from disk."""
        g1 = initialized_project.load_graph()
        initialized_project.invalidate_graph_cache()
        g2 = initialized_project.load_graph()
        assert g1 is not g2


class TestStores:
    def test_receipt_store(self, initialized_project: CruxibleInstance) -> None:
        store = initialized_project.get_receipt_store()
        assert store is not None
        store.close()
        assert (initialized_project.instance_dir / "state.db").exists()

    def test_feedback_store(self, initialized_project: CruxibleInstance) -> None:
        store = initialized_project.get_feedback_store()
        assert store is not None
        store.close()
        assert (initialized_project.instance_dir / "state.db").exists()

    def test_group_store(self, initialized_project: CruxibleInstance) -> None:
        store = initialized_project.get_group_store()
        assert isinstance(store, GroupStore)
        store.close()
        assert (initialized_project.instance_dir / "state.db").exists()


class TestAtomicSaveGraph:
    def test_sql_graph_intact_after_simulated_full_save_failure(
        self, initialized_project: CruxibleInstance
    ) -> None:
        """Original SQL graph rows are preserved when save_graph fails mid-write."""
        # Save initial graph
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P-1", properties={"name": "original"})
        )
        initialized_project.save_graph(graph)

        # Mutate in-memory graph
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P-2", properties={"name": "new"})
        )

        def fail_save_graph(_self: SQLiteGraphRepository, _graph: EntityGraph) -> None:
            raise OSError("disk full")

        # Simulate failure during SQL replacement
        with patch.object(SQLiteGraphRepository, "save_graph", fail_save_graph):
            with pytest.raises(OSError, match="disk full"):
                initialized_project.save_graph(graph)

        reloaded = initialized_project.load_graph()
        assert reloaded.get_entity("Part", "P-1") is not None
        assert reloaded.get_entity("Part", "P-2") is None
        assert not (initialized_project.instance_dir / "graph.json").exists()

    def test_cache_invalidated_on_exception(self, initialized_project: CruxibleInstance) -> None:
        """After failed save_graph, load_graph re-reads from disk (no phantom edges)."""
        # Save initial graph
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        initialized_project.save_graph(graph)

        # Mutate in-memory graph
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-2", properties={}))

        # Fail the save
        def fail_save_graph(_self: SQLiteGraphRepository, _graph: EntityGraph) -> None:
            raise OSError("disk full")

        with patch.object(SQLiteGraphRepository, "save_graph", fail_save_graph):
            with pytest.raises(OSError):
                initialized_project.save_graph(graph)

        # Cache was invalidated — next load_graph reads from disk
        reloaded = initialized_project.load_graph()
        assert reloaded.entity_count() == 1  # Only P-1, not P-2

    def test_invalidate_graph_cache_forces_reread(
        self, initialized_project: CruxibleInstance
    ) -> None:
        """Mutate in-memory graph, invalidate cache, reload → mutations gone."""
        graph = initialized_project.load_graph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P-1", properties={}))
        # Don't save — just invalidate
        initialized_project.invalidate_graph_cache()
        reloaded = initialized_project.load_graph()
        assert reloaded.entity_count() == 0  # Back to empty


class TestTransportRequirement:
    """Server transport policy does not change direct instance filesystem behavior."""

    def test_require_server_does_not_block_direct_instance_load(
        self, monkeypatch, tmp_project: Path
    ) -> None:
        monkeypatch.setenv("CRUXIBLE_REQUIRE_SERVER", "true")

        instance = CruxibleInstance.init(tmp_project, "config.yaml")
        assert (tmp_project / ".cruxible").is_dir()
        loaded = CruxibleInstance.load(instance.root)
        assert loaded.root == tmp_project
