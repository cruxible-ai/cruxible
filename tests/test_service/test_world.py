"""Tests for published world release, fork, and pull flows."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cruxible_core.config.loader import load_config
from cruxible_core.config.schema import WorkflowSchema, WorkflowStepSchema, WorkflowTestSchema
from cruxible_core.errors import OwnershipError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import (
    service_add_entities,
    service_add_relationships,
    service_fork_world,
    service_lock,
    service_publish_world,
    service_pull_world_apply,
    service_pull_world_preview,
    service_reload_config,
    service_test,
    service_world_status,
)
from cruxible_core.snapshot.types import UpstreamMetadata
from cruxible_core.workflow.executor import _apply_entity_set, _apply_relationship_set
from cruxible_core.world_kits import WorldKitEntry
from cruxible_core.world_refs import WorldCatalogEntry

WORLD_MODEL_YAML = """\
version: "1.0"
name: case_reference
kind: world_model

entity_types:
  Case:
    properties:
      case_id:
        type: string
        primary_key: true
      title:
        type: string

relationships:
  - name: cites
    from: Case
    to: Case
"""


@pytest.fixture
def published_release_fixture(tmp_path: Path) -> tuple[CruxibleInstance, Path]:
    root = tmp_path / "root-model"
    root.mkdir()
    (root / "config.yaml").write_text(WORLD_MODEL_YAML)
    instance = CruxibleInstance.init(root, "config.yaml")
    service_add_entities(
        instance,
        [
            _case("CASE-A", "Alpha"),
            _case("CASE-B", "Beta"),
        ],
    )

    release_dir = tmp_path / "releases" / "current"
    service_publish_world(
        instance,
        transport_ref=f"file://{release_dir}",
        world_id="case-law",
        release_id="v1.0.0",
        compatibility="data_only",
    )
    return instance, release_dir


def test_publish_fork_and_pull_apply_preserves_fork_overlay(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    root_instance, release_dir = published_release_fixture
    fork_root = tmp_path / "forked-model"

    fork_result = service_fork_world(
        transport_ref=f"file://{release_dir}",
        root_dir=fork_root,
    )
    fork_instance = fork_result.instance
    _write_overlay_config(fork_root)
    service_reload_config(fork_instance)

    add_result = service_add_relationships(
        fork_instance,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="follow_up",
                to_type="Case",
                to_id="CASE-B",
                properties={"reason": "watch"},
            )
        ],
        source="test",
        source_ref="model-test",
    )
    assert add_result.added == 1

    root_graph = root_instance.load_graph()
    root_graph.add_entity(
        EntityInstance(
            entity_type="Case",
            entity_id="CASE-C",
            properties={"case_id": "CASE-C", "title": "Gamma"},
        )
    )
    root_instance.save_graph(root_graph)

    successor_dir = tmp_path / "releases" / "successor"
    service_publish_world(
        root_instance,
        transport_ref=f"file://{successor_dir}",
        world_id="case-law",
        release_id="v1.1.0",
        compatibility="data_only",
    )
    _replace_release_dir(successor_dir, release_dir)

    preview = service_pull_world_preview(fork_instance)
    assert preview.target_release_id == "v1.1.0"
    assert preview.conflicts == []
    assert preview.upstream_entity_delta == 1

    applied = service_pull_world_apply(
        fork_instance,
        expected_apply_digest=preview.apply_digest,
    )
    assert applied.release_id == "v1.1.0"
    assert applied.pre_pull_snapshot_id.startswith("snap_")

    merged_graph = fork_instance.load_graph()
    assert merged_graph.has_entity("Case", "CASE-C")
    assert merged_graph.has_relationship("Case", "CASE-A", "Case", "CASE-B", "follow_up")
    status = service_world_status(fork_instance)
    assert status.upstream is not None
    assert status.upstream.release_id == "v1.1.0"


def test_fork_world_ref_specific_release_tracks_latest_ref(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_instance, current_dir = published_release_fixture
    releases_dir = current_dir.parent
    version_dir = releases_dir / "v1.0.0"
    shutil.copytree(current_dir, version_dir)
    monkeypatch.setattr(
        "cruxible_core.world_refs.get_world_catalog",
        lambda: {
            "case-law": WorldCatalogEntry(
                alias="case-law",
                base_transport_ref=f"file://{releases_dir}",
                latest_release="current",
            )
        },
    )

    fork_root = tmp_path / "forked-model"
    fork_instance = service_fork_world(
        world_ref="case-law@v1.0.0",
        root_dir=fork_root,
    ).instance

    status = service_world_status(fork_instance)
    assert status.upstream is not None
    assert status.upstream.release_id == "v1.0.0"
    assert status.upstream.requested_source_ref == "case-law@v1.0.0"
    assert status.upstream.requested_transport_ref == f"file://{version_dir}"
    assert status.upstream.transport_ref == f"file://{current_dir}"

    root_graph = root_instance.load_graph()
    root_graph.add_entity(
        EntityInstance(
            entity_type="Case",
            entity_id="CASE-C",
            properties={"case_id": "CASE-C", "title": "Gamma"},
        )
    )
    root_instance.save_graph(root_graph)
    successor_dir = tmp_path / "releases" / "v1.1.0"
    service_publish_world(
        root_instance,
        transport_ref=f"file://{successor_dir}",
        world_id="case-law",
        release_id="v1.1.0",
        compatibility="data_only",
    )
    _replace_release_dir(successor_dir, current_dir)

    preview = service_pull_world_preview(fork_instance)
    assert preview.target_release_id == "v1.1.0"
    assert preview.conflicts == []


def test_fork_with_explicit_kit_materializes_local_overlay(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root_instance, release_dir = published_release_fixture
    kit_dir = tmp_path / "kit"
    kit_dir.mkdir()
    (kit_dir / "config.yaml").write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: case_overlay",
                "kind: world_model",
                "extends: base-kit.yaml",
                "entity_types:",
                "  Note:",
                "    properties:",
                "      note_id:",
                "        type: string",
                "        primary_key: true",
                "relationships: []",
                "artifacts:",
                "  local_seed:",
                "    kind: directory",
                "    uri: ./data/seed",
                "    sha256: sha256:test",
            ]
        )
        + "\n"
    )
    (kit_dir / "providers.py").write_text("KIT = True\n")
    seed_dir = kit_dir / "data" / "seed"
    seed_dir.mkdir(parents=True)
    (seed_dir / "notes.csv").write_text("note_id\nNOTE-1\n")
    monkeypatch.setattr(
        "cruxible_core.world_kits.get_world_kit_catalog",
        lambda: {
            "case-overlay": WorldKitEntry(
                kit="case-overlay",
                source_dir=kit_dir,
                copy_paths=("providers.py", "data/seed"),
                world_id="case-law",
            )
        },
    )

    fork_root = tmp_path / "forked-model"
    fork_result = service_fork_world(
        transport_ref=f"file://{release_dir}",
        kit="case-overlay",
        root_dir=fork_root,
    )

    overlay_text = (fork_root / "config.yaml").read_text()
    assert "extends: .cruxible/upstream/current/config.yaml" in overlay_text
    assert (fork_root / "providers.py").exists()
    assert (fork_root / "data" / "seed" / "notes.csv").exists()
    loaded = fork_result.instance.load_config()
    assert "Case" in loaded.entity_types
    assert "Note" in loaded.entity_types


def test_fork_world_ref_uses_default_kit(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root_instance, current_dir = published_release_fixture
    releases_dir = current_dir.parent
    monkeypatch.setattr(
        "cruxible_core.world_refs.get_world_catalog",
        lambda: {
            "case-law": WorldCatalogEntry(
                alias="case-law",
                base_transport_ref=f"file://{releases_dir}",
                latest_release="current",
                default_kit="case-overlay",
            )
        },
    )
    kit_dir = tmp_path / "default-kit"
    kit_dir.mkdir()
    (kit_dir / "config.yaml").write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: case_overlay",
                "kind: world_model",
                "extends: base-kit.yaml",
                "entity_types:",
                "  Note:",
                "    properties:",
                "      note_id:",
                "        type: string",
                "        primary_key: true",
                "relationships: []",
            ]
        )
        + "\n"
    )
    (kit_dir / "providers.py").write_text("DEFAULT_KIT = True\n")
    monkeypatch.setattr(
        "cruxible_core.world_kits.get_world_kit_catalog",
        lambda: {
            "case-overlay": WorldKitEntry(
                kit="case-overlay",
                source_dir=kit_dir,
                copy_paths=("providers.py",),
                world_id="case-law",
            )
        },
    )

    fork_root = tmp_path / "forked-default-kit"
    fork_result = service_fork_world(world_ref="case-law", root_dir=fork_root)

    assert (fork_root / "providers.py").exists()
    assert "Note" in fork_result.instance.load_config().entity_types


def test_explicit_kit_overrides_world_default_kit(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root_instance, current_dir = published_release_fixture
    releases_dir = current_dir.parent
    monkeypatch.setattr(
        "cruxible_core.world_refs.get_world_catalog",
        lambda: {
            "case-law": WorldCatalogEntry(
                alias="case-law",
                base_transport_ref=f"file://{releases_dir}",
                latest_release="current",
                default_kit="default-kit",
            )
        },
    )
    default_dir = tmp_path / "default-kit"
    default_dir.mkdir()
    (default_dir / "config.yaml").write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: default_overlay",
                "kind: world_model",
                "extends: base-kit.yaml",
                "entity_types:",
                "  DefaultNote:",
                "    properties:",
                "      note_id:",
                "        type: string",
                "        primary_key: true",
                "relationships: []",
            ]
        )
        + "\n"
    )
    override_dir = tmp_path / "override-kit"
    override_dir.mkdir()
    (override_dir / "config.yaml").write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: override_overlay",
                "kind: world_model",
                "extends: base-kit.yaml",
                "entity_types:",
                "  OverrideNote:",
                "    properties:",
                "      note_id:",
                "        type: string",
                "        primary_key: true",
                "relationships: []",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        "cruxible_core.world_kits.get_world_kit_catalog",
        lambda: {
            "default-kit": WorldKitEntry(
                kit="default-kit",
                source_dir=default_dir,
                world_id="case-law",
            ),
            "override-kit": WorldKitEntry(
                kit="override-kit",
                source_dir=override_dir,
                world_id="case-law",
            ),
        },
    )

    fork_root = tmp_path / "forked-override-kit"
    loaded = service_fork_world(
        world_ref="case-law",
        kit="override-kit",
        root_dir=fork_root,
    ).instance.load_config()

    assert "OverrideNote" in loaded.entity_types
    assert "DefaultNote" not in loaded.entity_types


def test_no_kit_skips_world_default_kit(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root_instance, current_dir = published_release_fixture
    releases_dir = current_dir.parent
    monkeypatch.setattr(
        "cruxible_core.world_refs.get_world_catalog",
        lambda: {
            "case-law": WorldCatalogEntry(
                alias="case-law",
                base_transport_ref=f"file://{releases_dir}",
                latest_release="current",
                default_kit="case-overlay",
            )
        },
    )
    kit_dir = tmp_path / "default-kit"
    kit_dir.mkdir()
    (kit_dir / "config.yaml").write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: case_overlay",
                "kind: world_model",
                "extends: base-kit.yaml",
                "entity_types:",
                "  Note:",
                "    properties:",
                "      note_id:",
                "        type: string",
                "        primary_key: true",
                "relationships: []",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        "cruxible_core.world_kits.get_world_kit_catalog",
        lambda: {
            "case-overlay": WorldKitEntry(
                kit="case-overlay",
                source_dir=kit_dir,
                world_id="case-law",
            )
        },
    )

    fork_root = tmp_path / "forked-no-kit"
    fork_result = service_fork_world(world_ref="case-law", no_kit=True, root_dir=fork_root)

    assert not (fork_root / "providers.py").exists()
    loaded = fork_result.instance.load_config()
    assert "Note" not in loaded.entity_types
    assert "Case" in loaded.entity_types


def test_transport_ref_fork_does_not_auto_apply_any_kit(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root_instance, release_dir = published_release_fixture
    kit_dir = tmp_path / "transport-kit"
    kit_dir.mkdir()
    (kit_dir / "config.yaml").write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: transport_overlay",
                "kind: world_model",
                "extends: base-kit.yaml",
                "entity_types:",
                "  TransportNote:",
                "    properties:",
                "      note_id:",
                "        type: string",
                "        primary_key: true",
                "relationships: []",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        "cruxible_core.world_kits.get_world_kit_catalog",
        lambda: {
            "transport-kit": WorldKitEntry(
                kit="transport-kit",
                source_dir=kit_dir,
                world_id="case-law",
            )
        },
    )

    fork_root = tmp_path / "forked-transport-ref"
    loaded = service_fork_world(
        transport_ref=f"file://{release_dir}",
        root_dir=fork_root,
    ).instance.load_config()

    assert "TransportNote" not in loaded.entity_types
    assert "Case" in loaded.entity_types


def test_pull_preview_surfaces_dangling_fork_relationships(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    root_instance, release_dir = published_release_fixture
    fork_root = tmp_path / "forked-model"
    fork_instance = service_fork_world(
        transport_ref=f"file://{release_dir}",
        root_dir=fork_root,
    ).instance
    _write_overlay_config(fork_root)
    service_reload_config(fork_instance)
    service_add_relationships(
        fork_instance,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="follow_up",
                to_type="Case",
                to_id="CASE-B",
                properties={"reason": "watch"},
            )
        ],
        source="test",
        source_ref="model-test",
    )

    root_graph = root_instance.load_graph()
    root_graph.remove_entity("Case", "CASE-B")
    root_instance.save_graph(root_graph)

    successor_dir = tmp_path / "releases" / "successor"
    service_publish_world(
        root_instance,
        transport_ref=f"file://{successor_dir}",
        world_id="case-law",
        release_id="v2.0.0",
        compatibility="breaking",
    )
    _replace_release_dir(successor_dir, release_dir)

    preview = service_pull_world_preview(fork_instance)
    assert preview.target_release_id == "v2.0.0"
    assert any("missing upstream entity Case:CASE-B" in conflict for conflict in preview.conflicts)


def test_fork_runtime_config_excludes_upstream_canonical_workflows(
    canonical_workflow_instance: CruxibleInstance,
    tmp_path: Path,
) -> None:
    config = canonical_workflow_instance.load_config()
    config.workflows["list_vendors_runtime"] = WorkflowSchema(
        contract_in="EmptyInput",
        steps=[
            WorkflowStepSchema(
                id="vendors",
                query="get_vendors",
                params={"vendor_id": "vendor-acme"},
                as_="vendors",
            )
        ],
        returns="vendors",
    )
    config.tests.extend(
        [
            WorkflowTestSchema(
                name="canonical_reference_smoke",
                workflow="build_reference",
            ),
            WorkflowTestSchema(
                name="runtime_vendor_smoke",
                workflow="list_vendors_runtime",
            ),
        ]
    )
    canonical_workflow_instance.save_config(config)
    service_add_entities(
        canonical_workflow_instance,
        [
            EntityInstance(
                entity_type="Vendor",
                entity_id="vendor-acme",
                properties={"vendor_id": "vendor-acme", "name": "Acme"},
            )
        ],
    )

    service_lock(canonical_workflow_instance)
    release_dir = tmp_path / "releases" / "current"
    service_publish_world(
        canonical_workflow_instance,
        transport_ref=f"file://{release_dir}",
        world_id="canonical-reference",
        release_id="v1.0.0",
        compatibility="data_only",
    )

    fork_root = tmp_path / "forked-runtime"
    fork_result = service_fork_world(
        transport_ref=f"file://{release_dir}",
        root_dir=fork_root,
    )

    fork_config = fork_result.instance.load_config()
    assert "build_reference" not in fork_config.workflows
    assert "list_vendors_runtime" in fork_config.workflows
    assert "reference_loader" not in fork_config.providers
    assert [test.name for test in fork_config.tests] == ["runtime_vendor_smoke"]
    assert (fork_result.instance.get_instance_dir() / "cruxible.lock.yaml").exists()
    test_result = service_test(fork_result.instance)
    assert test_result.total == 1
    assert test_result.failed == 0


def test_load_config_with_extends_remains_single_file(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    overlay = tmp_path / "overlay.yaml"
    base.write_text(WORLD_MODEL_YAML)
    overlay.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: case_reference_fork",
                f"extends: {base}",
                "entity_types: {}",
                "relationships: []",
            ]
        )
        + "\n"
    )

    config = load_config(overlay)
    assert config.extends == str(base)
    assert config.entity_types == {}
    assert config.relationships == []


def test_canonical_apply_respects_upstream_ownership(tmp_path: Path) -> None:
    root = tmp_path / "owned-case-model"
    root.mkdir()
    (root / "config.yaml").write_text(
        WORLD_MODEL_YAML
        + """
  - name: follow_up
    from: Case
    to: Case
    properties:
      reason:
        type: string
      note:
        type: string
        optional: true
"""
    )
    instance = CruxibleInstance.init(root, "config.yaml")
    instance.set_upstream_metadata(
        UpstreamMetadata(
            transport_ref="file:///tmp/release",
            world_id="case-law",
            release_id="v1.0.0",
            snapshot_id="snap_1",
            compatibility="data_only",
            owned_entity_types=["Case"],
            owned_relationship_types=["cites"],
        )
    )

    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Case",
            entity_id="CASE-A",
            properties={"case_id": "CASE-A", "title": "Alpha"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Case",
            entity_id="CASE-B",
            properties={"case_id": "CASE-B", "title": "Beta"},
        )
    )
    receipt_builder = ReceiptBuilder(query_name="wf", parameters={}, operation_type="workflow")

    with pytest.raises(OwnershipError, match="upstream-owned entity types"):
        _apply_entity_set(
            instance,
            graph,
            "step_entities",
            {
                "entity_type": "Case",
                "entities": [
                    {
                        "entity_type": "Case",
                        "entity_id": "CASE-C",
                        "properties": {"case_id": "CASE-C"},
                    }
                ],
            },
            receipt_builder,
            persist_writes=False,
            parent_id=None,
        )

    preview = _apply_relationship_set(
        instance,
        graph,
        "wf",
        "step_edges",
        {
            "relationship_type": "follow_up",
            "relationships": [
                {
                    "relationship_type": "follow_up",
                    "from_type": "Case",
                    "from_id": "CASE-A",
                    "to_type": "Case",
                    "to_id": "CASE-B",
                    "properties": {"reason": "watch"},
                }
            ],
        },
        receipt_builder,
        persist_writes=False,
        parent_id=None,
    )
    assert preview.create_count == 1
    assert graph.has_relationship("Case", "CASE-A", "Case", "CASE-B", "follow_up")

    update_preview = _apply_relationship_set(
        instance,
        graph,
        "wf",
        "step_edges_patch",
        {
            "relationship_type": "follow_up",
            "relationships": [
                {
                    "relationship_type": "follow_up",
                    "from_type": "Case",
                    "from_id": "CASE-A",
                    "to_type": "Case",
                    "to_id": "CASE-B",
                    "properties": {"note": "still watching"},
                }
            ],
        },
        receipt_builder,
        persist_writes=False,
        parent_id=None,
    )
    assert update_preview.update_count == 1
    rel = graph.get_relationship("Case", "CASE-A", "Case", "CASE-B", "follow_up")
    assert rel is not None
    assert rel.properties["reason"] == "watch"
    assert rel.properties["note"] == "still watching"


def _case(case_id: str, title: str) -> EntityInstance:
    return EntityInstance(
        entity_type="Case",
        entity_id=case_id,
        properties={"case_id": case_id, "title": title},
    )


def _write_overlay_config(root: Path) -> None:
    (root / "config.yaml").write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: case-law-fork",
                "extends: .cruxible/upstream/current/config.yaml",
                "entity_types: {}",
                "relationships:",
                "  - name: follow_up",
                "    from: Case",
                "    to: Case",
                "    properties:",
                "      reason:",
                "        type: string",
                "        optional: true",
            ]
        )
        + "\n"
    )


def _replace_release_dir(source: Path, target: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
