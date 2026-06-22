"""Tests for published state release, overlay, and pull flows."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import cruxible_core.service.state as state_service
from cruxible_core.config.loader import load_config
from cruxible_core.config.schema import WorkflowSchema, WorkflowStepSchema, WorkflowTestSchema
from cruxible_core.errors import OwnershipError, QueryExecutionError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.kits.state_refs import StateCatalogEntry
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import (
    service_add_entities,
    service_add_relationships,
    service_create_state_overlay,
    service_lock,
    service_publish_state,
    service_pull_state_apply,
    service_pull_state_preview,
    service_reload_config,
    service_state_status,
    service_test,
)
from cruxible_core.snapshot.types import UpstreamMetadata
from cruxible_core.temporal import utc_now
from cruxible_core.workflow.apply import apply_entity_set, apply_relationship_set

STATE_MODEL_YAML = """\
version: "1.0"
name: case_reference

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


GUARDED_STATE_MODEL_YAML = """\
version: "1.0"
name: guarded_case_reference

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

mutation_guards:
  - name: cites_requires_source_evidence
    relationship_type: cites
    condition:
      require_evidence: source_evidence
    message: "Citation assertions require source evidence."
"""


@pytest.fixture
def published_release_fixture(tmp_path: Path) -> tuple[CruxibleInstance, Path]:
    root = tmp_path / "root-model"
    root.mkdir()
    (root / "config.yaml").write_text(STATE_MODEL_YAML)
    instance = CruxibleInstance.init(root, "config.yaml")
    service_add_entities(
        instance,
        [
            _case("CASE-A", "Alpha"),
            _case("CASE-B", "Beta"),
        ],
    )

    release_dir = tmp_path / "releases" / "current"
    service_publish_state(
        instance,
        transport_ref=f"file://{release_dir}",
        state_id="case-law",
        release_id="v1.0.0",
        compatibility="data_only",
    )
    return instance, release_dir


def _write_overlay_kit_manifest(
    kit_dir: Path,
    kit_id: str,
    *,
    target_state: str = "case-law",
) -> None:
    (kit_dir / "cruxible-kit.yaml").write_text(
        "\n".join(
            [
                "schema_version: cruxible.kit.v1",
                f"kit_id: {kit_id}",
                "version: 0.2.0",
                "role: overlay",
                f"target_state: {target_state}",
                "entry_config: config.yaml",
                "provider_paths: []",
                "copy_paths: []",
                "requires_extras: []",
            ]
        )
        + "\n"
    )
    (kit_dir / "cruxible.lock.yaml").write_text(
        "version: '1'\nconfig_digest: test\nartifacts: {}\nproviders: {}\n"
    )


def test_publish_overlay_and_pull_apply_preserves_overlay_overlay(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_instance, release_dir = published_release_fixture
    overlay_root = tmp_path / "cloned-model"

    overlay_result = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=overlay_root,
    )
    overlay_instance = overlay_result.instance
    _write_overlay_config(overlay_root)
    service_reload_config(overlay_instance)

    add_result = service_add_relationships(
        overlay_instance,
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
    service_publish_state(
        root_instance,
        transport_ref=f"file://{successor_dir}",
        state_id="case-law",
        release_id="v1.1.0",
        compatibility="data_only",
    )
    _replace_release_dir(successor_dir, release_dir)

    preview = service_pull_state_preview(overlay_instance)
    assert preview.target_release_id == "v1.1.0"
    assert preview.conflicts == []
    assert preview.upstream_entity_delta == 1

    pull_count = 0
    real_pull_bundle = state_service._pull_bundle

    def counted_pull_bundle(transport_ref: str):
        nonlocal pull_count
        pull_count += 1
        return real_pull_bundle(transport_ref)

    monkeypatch.setattr(state_service, "_pull_bundle", counted_pull_bundle)
    applied = service_pull_state_apply(
        overlay_instance,
        expected_apply_digest=preview.apply_digest,
    )
    assert pull_count == 1
    assert applied.release_id == "v1.1.0"
    assert applied.pre_pull_snapshot_id.startswith("snap_")

    merged_graph = overlay_instance.load_graph()
    assert merged_graph.has_entity("Case", "CASE-C")
    assert merged_graph.has_relationship("Case", "CASE-A", "Case", "CASE-B", "follow_up")
    status = service_state_status(overlay_instance)
    assert status.upstream is not None
    assert status.upstream.release_id == "v1.1.0"


def test_pull_apply_clears_dangling_upstream_receipt_and_stamps_clone_origin(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    root_instance, release_dir = published_release_fixture
    overlay_root = tmp_path / "cloned-model"
    overlay_instance = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=overlay_root,
    ).instance
    _write_overlay_config(overlay_root)
    service_reload_config(overlay_instance)

    # Author an UPSTREAM edge whose receipt resolves only in the root instance.
    upstream_add = service_add_relationships(
        root_instance,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="cites",
                to_type="Case",
                to_id="CASE-B",
            )
        ],
        source="test",
        source_ref="upstream-author",
    )
    assert upstream_add.added == 1
    upstream_edge = root_instance.load_graph().get_relationship(
        "Case", "CASE-A", "Case", "CASE-B", "cites"
    )
    assert upstream_edge is not None
    assert upstream_edge.metadata.provenance is not None
    upstream_receipt_id = upstream_edge.metadata.provenance.receipt_id
    assert upstream_receipt_id is not None

    successor_dir = tmp_path / "releases" / "successor"
    service_publish_state(
        root_instance,
        transport_ref=f"file://{successor_dir}",
        state_id="case-law",
        release_id="v1.1.0",
        compatibility="data_only",
    )
    _replace_release_dir(successor_dir, release_dir)

    preview = service_pull_state_preview(overlay_instance)
    applied = service_pull_state_apply(
        overlay_instance,
        expected_apply_digest=preview.apply_digest,
    )
    assert applied.release_id == "v1.1.0"

    merged_graph = overlay_instance.load_graph()
    pulled_edge = merged_graph.get_relationship("Case", "CASE-A", "Case", "CASE-B", "cites")
    assert pulled_edge is not None
    provenance = pulled_edge.metadata.provenance
    assert provenance is not None
    # The upstream receipt lives only in the root instance -- not shipped in the
    # bundle -- so the pulled edge's pointer is cleared and clone origin stamped.
    assert provenance.receipt_id is None
    assert provenance.clone_origin == "upstream-snapshot"
    assert getattr(provenance, "cloned_receipt_id", None) == upstream_receipt_id

    # Invariant: no edge in the overlay references a receipt that is not present.
    overlay_store = overlay_instance.get_receipt_store()
    try:
        assert overlay_store.get_receipt(upstream_receipt_id) is None
        for rel in merged_graph.iter_relationships():
            rel_provenance = rel.metadata.provenance
            if rel_provenance is None or rel_provenance.receipt_id is None:
                continue
            assert overlay_store.get_receipt(rel_provenance.receipt_id) is not None, (
                f"dangling receipt_id {rel_provenance.receipt_id} on {rel.relationship_label()}"
            )
    finally:
        overlay_store.close()


def test_overlay_state_ref_specific_release_tracks_latest_ref(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_instance, current_dir = published_release_fixture
    releases_dir = current_dir.parent
    version_dir = releases_dir / "v1.0.0"
    shutil.copytree(current_dir, version_dir)
    monkeypatch.setattr(
        "cruxible_core.kits.state_refs.get_state_catalog",
        lambda: {
            "case-law": StateCatalogEntry(
                alias="case-law",
                base_transport_ref=f"file://{releases_dir}",
                latest_release="current",
            )
        },
    )

    overlay_root = tmp_path / "cloned-model"
    overlay_instance = service_create_state_overlay(
        state_ref="case-law@v1.0.0",
        root_dir=overlay_root,
    ).instance

    status = service_state_status(overlay_instance)
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
    service_publish_state(
        root_instance,
        transport_ref=f"file://{successor_dir}",
        state_id="case-law",
        release_id="v1.1.0",
        compatibility="data_only",
    )
    _replace_release_dir(successor_dir, current_dir)

    preview = service_pull_state_preview(overlay_instance)
    assert preview.target_release_id == "v1.1.0"
    assert preview.conflicts == []


def test_overlay_with_explicit_kit_materializes_local_overlay(
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
                "    digest: sha256:test",
            ]
        )
        + "\n"
    )
    (kit_dir / "providers.py").write_text("KIT = True\n")
    seed_dir = kit_dir / "data" / "seed"
    seed_dir.mkdir(parents=True)
    (seed_dir / "notes.csv").write_text("note_id\nNOTE-1\n")
    _write_overlay_kit_manifest(kit_dir, "case-overlay")
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"case-overlay": f"file://{kit_dir}"},
    )

    overlay_root = tmp_path / "cloned-model"
    overlay_result = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        kit="case-overlay",
        root_dir=overlay_root,
    )

    overlay_text = (overlay_root / "config.yaml").read_text()
    assert "extends: .cruxible/upstream/current/config.yaml" in overlay_text
    assert (overlay_root / "providers.py").exists()
    assert (overlay_root / "data" / "seed" / "notes.csv").exists()
    loaded = overlay_result.instance.load_config()
    assert "Case" in loaded.entity_types
    assert "Note" in loaded.entity_types


def test_overlay_state_ref_uses_default_kit(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root_instance, current_dir = published_release_fixture
    releases_dir = current_dir.parent
    monkeypatch.setattr(
        "cruxible_core.kits.state_refs.get_state_catalog",
        lambda: {
            "case-law": StateCatalogEntry(
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
    _write_overlay_kit_manifest(kit_dir, "case-overlay")
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"case-overlay": f"file://{kit_dir}"},
    )

    overlay_root = tmp_path / "cloned-default-kit"
    overlay_result = service_create_state_overlay(state_ref="case-law", root_dir=overlay_root)

    assert (overlay_root / "providers.py").exists()
    assert "Note" in overlay_result.instance.load_config().entity_types


def test_explicit_kit_overrides_state_default_kit(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root_instance, current_dir = published_release_fixture
    releases_dir = current_dir.parent
    monkeypatch.setattr(
        "cruxible_core.kits.state_refs.get_state_catalog",
        lambda: {
            "case-law": StateCatalogEntry(
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
    _write_overlay_kit_manifest(default_dir, "default-kit")
    _write_overlay_kit_manifest(override_dir, "override-kit")
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {
            "default-kit": f"file://{default_dir}",
            "override-kit": f"file://{override_dir}",
        },
    )

    overlay_root = tmp_path / "cloned-override-kit"
    loaded = service_create_state_overlay(
        state_ref="case-law",
        kit="override-kit",
        root_dir=overlay_root,
    ).instance.load_config()

    assert "OverrideNote" in loaded.entity_types
    assert "DefaultNote" not in loaded.entity_types


def test_no_kit_skips_state_default_kit(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root_instance, current_dir = published_release_fixture
    releases_dir = current_dir.parent
    monkeypatch.setattr(
        "cruxible_core.kits.state_refs.get_state_catalog",
        lambda: {
            "case-law": StateCatalogEntry(
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
    _write_overlay_kit_manifest(kit_dir, "case-overlay")
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"case-overlay": f"file://{kit_dir}"},
    )

    overlay_root = tmp_path / "cloned-no-kit"
    overlay_result = service_create_state_overlay(
        state_ref="case-law",
        no_kit=True,
        root_dir=overlay_root,
    )

    assert not (overlay_root / "providers.py").exists()
    loaded = overlay_result.instance.load_config()
    assert "Note" not in loaded.entity_types
    assert "Case" in loaded.entity_types


def test_transport_ref_overlay_does_not_auto_apply_any_kit(
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
    _write_overlay_kit_manifest(kit_dir, "transport-kit")
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"transport-kit": f"file://{kit_dir}"},
    )

    overlay_root = tmp_path / "cloned-transport-ref"
    loaded = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=overlay_root,
    ).instance.load_config()

    assert "TransportNote" not in loaded.entity_types
    assert "Case" in loaded.entity_types


def test_pull_preview_surfaces_dangling_overlay_relationships(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    root_instance, release_dir = published_release_fixture
    overlay_root = tmp_path / "cloned-model"
    overlay_instance = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=overlay_root,
    ).instance
    _write_overlay_config(overlay_root)
    service_reload_config(overlay_instance)
    service_add_relationships(
        overlay_instance,
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
    service_publish_state(
        root_instance,
        transport_ref=f"file://{successor_dir}",
        state_id="case-law",
        release_id="v2.0.0",
        compatibility="breaking",
    )
    _replace_release_dir(successor_dir, release_dir)

    preview = service_pull_state_preview(overlay_instance)
    assert preview.target_release_id == "v2.0.0"
    assert any("missing upstream entity Case:CASE-B" in conflict for conflict in preview.conflicts)


def test_overlay_runtime_config_excludes_upstream_canonical_workflows(
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
    service_publish_state(
        canonical_workflow_instance,
        transport_ref=f"file://{release_dir}",
        state_id="canonical-reference",
        release_id="v1.0.0",
        compatibility="data_only",
    )

    overlay_root = tmp_path / "cloned-runtime"
    overlay_result = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=overlay_root,
    )

    overlay_config = overlay_result.instance.load_config()
    assert "build_reference" not in overlay_config.workflows
    assert "list_vendors_runtime" in overlay_config.workflows
    assert "reference_loader" not in overlay_config.providers
    assert [test.name for test in overlay_config.tests] == ["runtime_vendor_smoke"]
    assert (overlay_result.instance.get_instance_dir() / "cruxible.lock.yaml").exists()
    test_result = service_test(overlay_result.instance)
    assert test_result.total == 1
    assert test_result.failed == 0


def test_load_config_with_extends_remains_single_file(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    overlay = tmp_path / "overlay.yaml"
    base.write_text(STATE_MODEL_YAML)
    overlay.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: case_reference_overlay",
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
        STATE_MODEL_YAML
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
            state_id="case-law",
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
        apply_entity_set(
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

    preview = apply_relationship_set(
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

    update_preview = apply_relationship_set(
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


def test_workflow_apply_relationships_enforces_evidence_mutation_guard(
    tmp_path: Path,
) -> None:
    root = tmp_path / "guarded-model"
    root.mkdir()
    (root / "config.yaml").write_text(GUARDED_STATE_MODEL_YAML)
    instance = CruxibleInstance.init(root, "config.yaml")
    service_add_entities(
        instance,
        [
            _case("CASE-A", "Alpha"),
            _case("CASE-B", "Beta"),
        ],
    )
    graph = instance.load_graph()
    receipt_builder = ReceiptBuilder(query_name="wf", parameters={}, operation_type="workflow")

    with pytest.raises(QueryExecutionError, match="cites_requires_source_evidence"):
        apply_relationship_set(
            instance,
            graph,
            "wf",
            "step_edges",
            {
                "relationship_type": "cites",
                "relationships": [
                    {
                        "relationship_type": "cites",
                        "from_type": "Case",
                        "from_id": "CASE-A",
                        "to_type": "Case",
                        "to_id": "CASE-B",
                        "properties": {},
                    }
                ],
            },
            receipt_builder,
            persist_writes=False,
            parent_id=None,
        )

    assert not graph.has_relationship("Case", "CASE-A", "Case", "CASE-B", "cites")


def test_pull_apply_merge_is_guard_exempt_for_local_overlay_state(
    published_release_fixture: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    """Pin the audit-F4 exemption: the pull-apply merge does NOT re-run entity
    guards over the re-materialized local overlay state.

    The overlay declares a local entity type ``Note`` with an actor-identity
    guard: writing ``Note.status=published`` requires the ``editor`` actor. The
    note is authored with that actor (the guard passes at write time). A later
    upstream pull-apply re-materializes the local note onto a fresh upstream
    baseline WITHOUT any write actor. If ``save_graph(merged)`` naively re-ran
    entity guards over the merged delta, the actor-identity guard would treat
    the re-materialized note as a fresh ``status=published`` transition with no
    actor and reject the apply. This test asserts the apply succeeds and the
    note survives, proving the merge is guard-exempt. It is load-bearing: adding
    guard evaluation at the merge site turns the apply red.
    """
    root_instance, release_dir = published_release_fixture
    overlay_root = tmp_path / "cloned-model"

    overlay_instance = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=overlay_root,
    ).instance
    # Overlay owns a local `Note` type guarded so status=published requires the
    # `editor` actor. `Case` stays upstream-owned and is untouched by the guard.
    (overlay_root / "config.yaml").write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: case-law-overlay",
                "extends: .cruxible/upstream/current/config.yaml",
                "entity_types:",
                "  Note:",
                "    properties:",
                "      note_id:",
                "        type: string",
                "        primary_key: true",
                "      status:",
                "        type: string",
                "relationships: []",
                "mutation_guards:",
                "  - name: note_publish_requires_editor",
                "    entity_type: Note",
                "    property: status",
                "    new_value: published",
                "    condition:",
                "      allowed_actor_ids: [editor]",
                '    message: "Notes can only be published by the editor."',
            ]
        )
        + "\n"
    )
    service_reload_config(overlay_instance)

    editor = GovernedActorContext(
        actor_type="human_user",
        actor_id="editor",
        org_id="org_1",
        operation_id="op_publish_note",
        timestamp=utc_now(),
    )
    # Authoring the published note succeeds because the editor actor satisfies
    # the guard. A non-editor actor would be rejected here (write-path guard).
    add_result = service_add_entities(
        overlay_instance,
        [
            EntityInstance(
                entity_type="Note",
                entity_id="NOTE-1",
                properties={"note_id": "NOTE-1", "status": "published"},
            )
        ],
        actor_context=editor,
    )
    assert add_result.added == 1

    # Sanity: the same write WITHOUT the editor actor is genuinely rejected by
    # the guard, confirming the guard is live and load-bearing.
    with pytest.raises(Exception, match="note_publish_requires_editor"):
        service_add_entities(
            overlay_instance,
            [
                EntityInstance(
                    entity_type="Note",
                    entity_id="NOTE-2",
                    properties={"note_id": "NOTE-2", "status": "published"},
                )
            ],
            actor_context=None,
        )

    # Publish a new upstream release so there is something to pull-apply.
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
    service_publish_state(
        root_instance,
        transport_ref=f"file://{successor_dir}",
        state_id="case-law",
        release_id="v1.1.0",
        compatibility="data_only",
    )
    _replace_release_dir(successor_dir, release_dir)

    preview = service_pull_state_preview(overlay_instance)
    assert preview.target_release_id == "v1.1.0"
    assert preview.conflicts == []

    # The merge re-materializes the published NOTE-1 with NO write actor. This
    # apply MUST succeed; it would fail if the merge re-ran the actor-identity
    # guard over the re-materialized local note.
    applied = service_pull_state_apply(
        overlay_instance,
        expected_apply_digest=preview.apply_digest,
        actor_context=None,
    )
    assert applied.release_id == "v1.1.0"

    merged_graph = overlay_instance.load_graph()
    assert merged_graph.has_entity("Case", "CASE-C")
    note = merged_graph.get_entity("Note", "NOTE-1")
    assert note is not None
    assert note.properties["status"] == "published"


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
                "name: case-law-overlay",
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
