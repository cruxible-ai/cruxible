"""Tests for kit manifests and kit-local provider loading."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.loader import load_config
from cruxible_core.config.schema import ProviderSchema
from cruxible_core.config.validator import validate_config
from cruxible_core.errors import ConfigError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.kits import (
    KitManifest,
    compute_kit_provider_sha256,
    compute_kit_runtime_digest,
    config_yaml_has_kit_provider_refs,
    get_kit_catalog,
    load_kit_provider_module,
    materialize_kit,
    resolve_kit_provider_ref,
    resolve_kit_ref,
    write_materialized_kit_metadata,
)
from cruxible_core.provider.registry import resolve_provider
from cruxible_core.query.types import QueryPathRow
from cruxible_core.service import (
    EntityWriteInput,
    service_add_entity_inputs,
    service_inspect_entity,
    service_query,
)

PROJECT_STATE_CONFIG = (
    Path(__file__).resolve().parents[2] / "kits" / "project-state" / "config.yaml"
)


def test_kit_manifest_validates_roles() -> None:
    standalone = KitManifest(
        kit_id="demo",
        version="0.2.0",
        role="standalone",
        entry_config="config.yaml",
    )
    assert standalone.target_state is None

    overlay = KitManifest(
        kit_id="demo-overlay",
        version="0.2.0",
        role="overlay",
        target_state="demo",
        entry_config="config.yaml",
    )
    assert overlay.target_state == "demo"

    with pytest.raises(ValidationError, match="requires target_state"):
        KitManifest(
            kit_id="bad-overlay",
            version="0.2.0",
            role="overlay",
            entry_config="config.yaml",
        )


def test_kit_provider_ref_loads_relative_imports(tmp_path: Path) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "common.py").write_text("VALUE = 42\n")
    (providers / "main.py").write_text(
        "from .common import VALUE\n\ndef run(_input, _context):\n    return {'value': VALUE}\n"
    )
    write_materialized_kit_metadata(tmp_path)

    path, attr, kit_root = resolve_kit_provider_ref(
        "kit://providers/main.py::run",
        tmp_path,
    )
    module = load_kit_provider_module(path, kit_root)

    assert attr == "run"
    assert module.run({}, None) == {"value": 42}


def test_materialize_rejects_overlay_kit_for_standalone_init(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="overlay", target_state="demo")

    with pytest.raises(ConfigError, match="Use `cruxible state create-overlay --kit`"):
        materialize_kit(
            kit=f"file://{source}",
            root=tmp_path / "target",
            expected_role="standalone",
        )


def test_shipped_catalog_is_overridden_by_local_kits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cruxible_core.kits._discover_local_kit_catalog", lambda: {})
    shipped = get_kit_catalog()
    assert shipped["kev-reference"] == "oci://ghcr.io/cruxible-ai/kits/kev-reference:0.2.0"

    monkeypatch.setattr(
        "cruxible_core.kits._discover_local_kit_catalog",
        lambda: {"kev-reference": "file:///tmp/local-kev-reference"},
    )
    assert get_kit_catalog()["kev-reference"] == "file:///tmp/local-kev-reference"


def test_alias_oci_resolution_uses_shipped_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="standalone")
    pulled: list[str] = []
    monkeypatch.setattr("cruxible_core.kits._discover_local_kit_catalog", lambda: {})
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "cache"))

    def fake_pull(ref: str) -> Path:
        pulled.append(ref)
        return source

    monkeypatch.setattr("cruxible_core.kits._pull_oci_kit", fake_pull)

    bundle = resolve_kit_ref("kev-reference")

    assert pulled == ["ghcr.io/cruxible-ai/kits/kev-reference:0.2.0"]
    assert bundle.manifest.kit_id == "demo"


def test_runtime_digest_ignores_unrelated_files_and_tracks_kit_files(tmp_path: Path) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    provider = providers / "main.py"
    provider.write_text("def run(_input, _context):\n    return {}\n")

    baseline = compute_kit_runtime_digest(tmp_path)
    (tmp_path / "notes.txt").write_text("not kit owned\n")
    assert compute_kit_runtime_digest(tmp_path) == baseline

    provider.write_text("def run(_input, _context):\n    return {'changed': True}\n")
    assert compute_kit_runtime_digest(tmp_path) != baseline


def test_dev_tree_resolution_requires_explicit_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "main.py").write_text("def run(_input, _context):\n    return {}\n")

    with pytest.raises(ConfigError, match="dev-tree kit root"):
        resolve_kit_provider_ref("kit://providers/main.py::run", tmp_path)

    monkeypatch.setenv("CRUXIBLE_KIT_DEV_RESOLVE", "1")
    path, _attr, _root = resolve_kit_provider_ref("kit://providers/main.py::run", tmp_path)
    assert path.name == "main.py"


def test_materialized_metadata_ignores_unrelated_files_but_detects_provider_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    _write_minimal_kit(source, role="standalone")
    providers = source / "providers"
    providers.mkdir()
    (providers / "main.py").write_text("def run(_input, _context):\n    return {}\n")

    materialize_kit(kit=f"file://{source}", root=target, expected_role="standalone")
    (target / "unrelated.txt").write_text("outside the kit runtime\n")
    resolve_kit_provider_ref("kit://providers/main.py::run", target)

    (target / "providers" / "main.py").write_text(
        "def run(_input, _context):\n    return {'changed': True}\n"
    )
    with pytest.raises(ConfigError, match="Materialized kit contents changed"):
        resolve_kit_provider_ref("kit://providers/main.py::run", target)


def test_provider_resolution_rejects_traversal_symlink_and_missing_callable(
    tmp_path: Path,
) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    target = providers / "target.py"
    target.write_text("VALUE = 1\n")
    write_materialized_kit_metadata(tmp_path)
    symlink = providers / "link.py"
    symlink.symlink_to(target)

    with pytest.raises(ConfigError, match="without '..'"):
        resolve_kit_provider_ref("kit://../target.py::run", tmp_path)
    with pytest.raises(ConfigError, match="symlinks"):
        resolve_kit_provider_ref("kit://providers/link.py::run", tmp_path)
    symlink.unlink()
    write_materialized_kit_metadata(tmp_path)

    provider = ProviderSchema(
        kind="function",
        contract_in="EmptyInput",
        contract_out="EmptyOutput",
        ref="kit://providers/target.py::missing",
        version="1.0.0",
    )
    with pytest.raises(ConfigError, match="does not resolve to an attribute"):
        resolve_provider("missing_callable", provider, config_base_path=tmp_path)


def test_provider_hash_changes_when_provider_tree_changes(tmp_path: Path) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    provider = providers / "main.py"
    provider.write_text("def run(_input, _context):\n    return {}\n")
    write_materialized_kit_metadata(tmp_path)

    before = compute_kit_provider_sha256("kit://providers/main.py::run", tmp_path)
    provider.write_text("def run(_input, _context):\n    return {'changed': True}\n")
    write_materialized_kit_metadata(tmp_path)

    assert compute_kit_provider_sha256("kit://providers/main.py::run", tmp_path) != before


def test_config_yaml_kit_ref_detection_is_provider_ref_only() -> None:
    assert config_yaml_has_kit_provider_refs(
        "version: '1.0'\nproviders:\n  p:\n    ref: kit://providers/main.py::run\n"
    )
    assert not config_yaml_has_kit_provider_refs(
        "version: '1.0'\ndescription: 'example kit:// text only'\nproviders: {}\n"
    )


def test_project_state_kit_config_is_dev_project_scoped() -> None:
    config = load_config(PROJECT_STATE_CONFIG)

    validate_config(config)

    expected_entity_types = {
        "ProductArea",
        "Capability",
        "RoadmapItem",
        "ReleaseLine",
        "Milestone",
        "WorkItem",
        "DesignDecision",
        "Risk",
        "OpenQuestion",
        "ReviewRequest",
    }
    removed_entity_types = {
        "Assumption",
        "CustomerAccount",
        "Persona",
        "UseCase",
        "PainPoint",
        "FeatureRequest",
        "UsageSignal",
        "SupportSignal",
        "Experiment",
        "Outcome",
    }
    assert set(config.entity_types) == expected_entity_types
    review_request_properties = config.entity_types["ReviewRequest"].properties
    for property_name in {
        "review_notes",
        "change_repo",
        "change_base",
        "change_head",
    }:
        prop = review_request_properties[property_name]
        assert prop.type == "string"
        assert prop.optional is True

    relationships = {relationship.name: relationship for relationship in config.relationships}
    assert not any(
        relationship.from_entity in removed_entity_types
        or relationship.to_entity in removed_entity_types
        for relationship in relationships.values()
    )

    required_relationships = {
        "roadmap_item_depends_on_roadmap_item",
        "work_item_depends_on_work_item",
        "work_item_mitigates_risk",
        "work_item_answers_open_question",
        "decision_answers_open_question",
        "decision_affects_roadmap_item",
        "decision_constrains_work_item",
    }
    assert required_relationships <= set(relationships)
    for name in required_relationships:
        policy = relationships[name].proposal_policy
        assert policy is not None
        assert policy.signals["source_evidence"].role == "required"
        assert policy.signals["maintainer_judgment"].role == "advisory"

    assert relationships["work_item_depends_on_work_item"].from_entity == "WorkItem"
    assert relationships["work_item_depends_on_work_item"].to_entity == "WorkItem"
    assert relationships["work_item_mitigates_risk"].to_entity == "Risk"
    assert relationships["work_item_answers_open_question"].to_entity == "OpenQuestion"
    assert relationships["decision_answers_open_question"].to_entity == "OpenQuestion"
    assert (
        relationships["decision_affects_roadmap_item"].properties["impact_type"].enum_ref
        == "decision_impact_type"
    )
    assert (
        relationships["decision_constrains_work_item"].properties["impact_type"].enum_ref
        == "decision_impact_type"
    )

    required_queries = {
        "roadmap_item_context",
        "work_item_change_context",
        "work_items_for_area",
        "area_change_context",
        "release_readiness_context",
        "deferred_release_gating_work_items",
        "decision_impact_context",
        "open_question_context",
        "blocked_work_items",
        "active_risks",
        "open_questions_needing_review",
        "superseded_decisions",
        "approved_reviews_for_work_item",
        "review_queue",
        "changes_requested_reviews",
    }
    assert set(config.named_queries) == required_queries
    for name in {
        "roadmap_item_context",
        "work_item_change_context",
        "area_change_context",
        "release_readiness_context",
        "decision_impact_context",
        "open_question_context",
    }:
        query = config.named_queries[name]
        assert query.relationship_state == "reviewable"
        assert query.allow_relationship_state_override is True

    quality_checks = {check.name for check in config.quality_checks}
    assert {
        "roadmap_items_target_area",
        "work_items_target_area",
        "roadmap_dependencies_have_basis",
        "work_dependencies_have_basis",
        "decision_roadmap_impacts_have_type",
        "decision_work_constraints_have_type",
        "deferred_release_work_not_gating_0_2",
    } <= quality_checks
    assert config.workflows == {}
    assert config.artifacts == {}


def test_project_state_context_queries_return_agent_read_models(tmp_path: Path) -> None:
    shutil.copy(PROJECT_STATE_CONFIG, tmp_path / "config.yaml")
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="ProductArea",
            entity_id="area-core",
            properties={"area_id": "area-core", "name": "Core"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="WorkItem",
            entity_id="wi-context",
            properties={
                "work_item_id": "wi-context",
                "title": "Context read models",
                "type": "feature",
                "status": "active",
                "priority": "high",
            },
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="DesignDecision",
            entity_id="dec-context",
            properties={
                "decision_id": "dec-context",
                "title": "Use product-area anchored context",
                "status": "accepted",
            },
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="work_item_targets_area",
            from_type="WorkItem",
            from_id="wi-context",
            to_type="ProductArea",
            to_id="area-core",
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="decision_constrains_work_item",
            from_type="DesignDecision",
            from_id="dec-context",
            to_type="WorkItem",
            to_id="wi-context",
            properties={"impact_type": "constrains"},
        )
    )
    instance.save_graph(graph)

    area_result = service_query(instance, "work_items_for_area", {"area_id": "area-core"})

    assert area_result.total == 1
    [work_item] = area_result.items
    assert isinstance(work_item, EntityInstance)
    assert work_item.entity_type == "WorkItem"
    assert work_item.entity_id == "wi-context"
    assert {
        "work_item_id": "wi-context",
        "title": "Context read models",
        "status": "active",
        "priority": "high",
    }.items() <= work_item.properties.items()

    context_result = service_query(
        instance,
        "work_item_change_context",
        {"work_item_id": "wi-context"},
    )

    assert context_result.total == 2
    assert {
        (row.result.entity_type, row.result.entity_id, row.path[0].relationship_type)
        for row in context_result.items
        if isinstance(row, QueryPathRow) and row.path
    } == {
        ("ProductArea", "area-core", "work_item_targets_area"),
        ("DesignDecision", "dec-context", "decision_constrains_work_item"),
    }
    for row in context_result.items:
        assert isinstance(row, QueryPathRow)
        assert row.includes["constraining_decisions"].count == 1
        assert row.includes["constraining_decisions"].items[0].source.entity_id == "dec-context"

    inspected = service_inspect_entity(instance, "WorkItem", "wi-context")

    assert inspected.total_neighbors == 2
    assert {
        (neighbor.direction, neighbor.relationship_type, neighbor.entity.entity_id)
        for neighbor in inspected.neighbors
        if neighbor.entity is not None
    } == {
        ("outgoing", "work_item_targets_area", "area-core"),
        ("incoming", "decision_constrains_work_item", "dec-context"),
    }


def test_project_state_review_queue_surfaces_change_refs_and_allows_legacy_requests(
    tmp_path: Path,
) -> None:
    shutil.copy(PROJECT_STATE_CONFIG, tmp_path / "config.yaml")
    instance = CruxibleInstance.init(tmp_path, "config.yaml")

    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="ReviewRequest",
                entity_id="rr-with-change-ref",
                properties={
                    "review_request_id": "rr-with-change-ref",
                    "title": "Review exact diff",
                    "status": "requested",
                    "change_repo": "cruxible-ai/cruxible-core",
                    "change_base": "0d57f6f",
                    "change_head": "b4458b5",
                },
            ),
            EntityWriteInput(
                entity_type="ReviewRequest",
                entity_id="rr-legacy",
                properties={
                    "review_request_id": "rr-legacy",
                    "title": "Historical review",
                    "status": "requested",
                },
            ),
            EntityWriteInput(
                entity_type="ReviewRequest",
                entity_id="rr-changes-requested",
                properties={
                    "review_request_id": "rr-changes-requested",
                    "title": "Needs changes",
                    "status": "changes_requested",
                    "change_head": "aaaaaaa",
                },
            ),
        ],
    )

    result = service_query(instance, "review_queue", {})

    assert result.total == 2
    rows_by_id = {}
    for row in result.items:
        assert isinstance(row, EntityInstance)
        rows_by_id[row.entity_id] = row
    assert set(rows_by_id) == {"rr-with-change-ref", "rr-legacy"}
    assert rows_by_id["rr-with-change-ref"].properties["change_repo"] == (
        "cruxible-ai/cruxible-core"
    )
    assert rows_by_id["rr-with-change-ref"].properties["change_base"] == "0d57f6f"
    assert rows_by_id["rr-with-change-ref"].properties["change_head"] == "b4458b5"
    assert "change_repo" not in rows_by_id["rr-legacy"].properties
    assert "change_base" not in rows_by_id["rr-legacy"].properties
    assert "change_head" not in rows_by_id["rr-legacy"].properties


def test_materialized_metadata_records_bundle_and_runtime_digest(tmp_path: Path) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    write_materialized_kit_metadata(tmp_path, bundle_digest="sha256:bundle")

    payload = json.loads((tmp_path / ".cruxible" / "kit.json").read_text())
    assert payload["bundle_digest"] == "sha256:bundle"
    assert payload["runtime_digest"].startswith("sha256:")


def _write_minimal_kit(
    root: Path,
    *,
    role: str,
    target_state: str | None = None,
) -> None:
    target_line = f"target_state: {target_state}\n" if target_state else ""
    root.joinpath("cruxible-kit.yaml").write_text(
        "schema_version: cruxible.kit.v1\n"
        "kit_id: demo\n"
        "version: 0.2.0\n"
        f"role: {role}\n"
        f"{target_line}"
        "entry_config: config.yaml\n"
        "provider_paths:\n"
        "  - providers\n"
        "copy_paths: []\n"
        "requires_extras: []\n"
    )
    root.joinpath("config.yaml").write_text(
        "version: '1.0'\nname: demo\nentity_types: {}\nrelationships: []\n"
    )
    root.joinpath("cruxible.lock.yaml").write_text(
        "version: '1'\nconfig_digest: test\nartifacts: {}\nproviders: {}\n"
    )
