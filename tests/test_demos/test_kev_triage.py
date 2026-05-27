"""Integration tests for the KEV kit providers and workflows."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.composer import compose_config_files
from cruxible_core.config.loader import save_config
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.types import RelationshipInstance, RelationshipMetadata
from cruxible_core.kits import load_kit_provider_module, write_materialized_kit_metadata
from cruxible_core.provider.types import ProviderContext, ResolvedArtifact
from cruxible_core.providers.common.tabular import load_tabular_artifact_bundle
from cruxible_core.service import (
    service_apply_workflow,
    service_create_world_overlay,
    service_init,
    service_lock,
    service_propose_workflow,
    service_publish_world,
    service_query,
    service_resolve_group,
    service_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KEV_KIT_DIR = REPO_ROOT / "kits" / "kev-triage"
KEV_REFERENCE_KIT_DIR = REPO_ROOT / "kits" / "kev-reference"

_seed_module = load_kit_provider_module(KEV_KIT_DIR / "providers" / "seed.py", KEV_KIT_DIR)
_reference_module = load_kit_provider_module(
    KEV_REFERENCE_KIT_DIR / "providers" / "reference.py",
    KEV_REFERENCE_KIT_DIR,
)
_matching_module = load_kit_provider_module(
    KEV_KIT_DIR / "providers" / "matching.py",
    KEV_KIT_DIR,
)


def _query_entity_ids(rows: list[object]) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        values = getattr(row, "values", None)
        if isinstance(values, dict):
            for key in ("entity_id", "cve_id", "asset_id", "product_id"):
                if key in values:
                    ids.add(values[key])
                    break
            continue
        entity = getattr(row, "result", row)
        entity_id = getattr(entity, "entity_id")
        ids.add(entity_id)
    return ids


def _row_path_segment(row: object, alias: str) -> object:
    for segment in getattr(row, "path", ()):
        if getattr(segment, "alias", None) == alias:
            return segment
    raise AssertionError(f"missing path segment {alias}")


def _include_result(row: object, alias: str) -> object:
    includes = getattr(row, "includes", {})
    include = includes.get(alias)
    assert include is not None, f"missing include {alias}"
    return include


def _include_items(row: object, alias: str) -> list[object]:
    return list(getattr(_include_result(row, alias), "items", []))
_assessment_module = load_kit_provider_module(
    KEV_KIT_DIR / "providers" / "assessment.py",
    KEV_KIT_DIR,
)

load_local_seed_data = _seed_module.load_local_seed_data
normalize_local_seed_tables = _seed_module.normalize_local_seed_tables
normalize_public_kev_reference = _reference_module.normalize_public_kev_reference
match_software_to_products = _matching_module.match_software_to_products
assess_asset_affected = _assessment_module.assess_asset_affected
assess_asset_exposure = _assessment_module.assess_asset_exposure
assess_exposure_reconciliation = _assessment_module.assess_exposure_reconciliation


def _provider_context(artifact_path: Path | None) -> ProviderContext:
    artifact = None
    if artifact_path is not None:
        artifact = ResolvedArtifact(
            name="bundle",
            kind="directory",
            uri=str(artifact_path),
            local_path=str(artifact_path),
            sha256="sha256:test",
        )
    return ProviderContext(
        workflow_name="test",
        step_id="provider",
        provider_name="provider",
        provider_version="1.0.0",
        artifact=artifact,
    )


def _csv_row_count(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _composed_kev_config_path(tmp_path: Path) -> Path:
    composed = compose_config_files(
        base_path=KEV_REFERENCE_KIT_DIR / "config.yaml",
        overlay_path=KEV_KIT_DIR / "config.yaml",
    )
    config_path = tmp_path / "config.yaml"
    save_config(composed, config_path)
    shutil.copy2(KEV_KIT_DIR / "cruxible-kit.yaml", tmp_path / "cruxible-kit.yaml")
    shutil.copytree(KEV_KIT_DIR / "providers", tmp_path / "providers", dirs_exist_ok=True)
    shutil.copytree(KEV_KIT_DIR / "data", tmp_path / "data", dirs_exist_ok=True)
    write_materialized_kit_metadata(tmp_path)
    return config_path


def test_kev_config_omits_incident_and_finding_ontology() -> None:
    config = compose_config_files(
        base_path=KEV_REFERENCE_KIT_DIR / "config.yaml",
        overlay_path=KEV_KIT_DIR / "config.yaml",
    )

    assert "Incident" not in config.entity_types
    assert "Finding" not in config.entity_types
    relationship_names = {relationship.name for relationship in config.relationships}
    assert "incident_owned_by" not in relationship_names
    assert "incident_involved_asset" not in relationship_names
    assert "incident_exploited_vulnerability" not in relationship_names
    assert "finding_from_incident" not in relationship_names
    assert "incident_history_for_product" not in config.named_queries
    assert "open_findings_for_asset" not in config.named_queries
    assert "prior_exploitation_context" not in config.named_queries


def _apply_canonical_workflow(instance: CruxibleInstance, workflow_name: str) -> None:
    preview = service_run(instance, workflow_name, {})
    assert preview.mode == "preview"
    assert preview.apply_digest is not None

    applied = service_apply_workflow(
        instance,
        workflow_name,
        {},
        expected_apply_digest=preview.apply_digest or "",
        expected_head_snapshot_id=preview.head_snapshot_id,
    )
    assert applied.committed_snapshot_id is not None


def _approve_workflow_group(instance: CruxibleInstance, workflow_name: str) -> None:
    _approve_workflow_group_with_input(instance, workflow_name, {})


def _approve_workflow_group_with_input(
    instance: CruxibleInstance,
    workflow_name: str,
    input_payload: dict,
) -> None:
    proposed = service_propose_workflow(instance, workflow_name, input_payload)
    assert proposed.group_id is not None
    resolved = service_resolve_group(
        instance,
        proposed.group_id,
        "approve",
        expected_pending_version=1,
    )
    assert resolved.edges_created > 0


def test_load_local_seed_data_reads_expected_rows() -> None:
    payload = load_local_seed_data({}, _provider_context(KEV_KIT_DIR / "data" / "seed"))
    assert set(payload) == {
        "assets",
        "business_services",
        "owners",
        "compensating_controls",
        "vulnerability_classes",
        "exceptions",
        "patch_windows",
        "service_depends_on_asset",
        "asset_owned_by",
        "asset_has_control",
        "asset_has_exception",
        "asset_patch_window",
    }
    assert payload["assets"][0]["internet_exposed"] is True


def test_normalize_local_seed_tables_accepts_common_tabular_output() -> None:
    parsed = load_tabular_artifact_bundle(
        {"expected_tables": ["assets", "asset_owned_by"]},
        _provider_context(KEV_KIT_DIR / "data" / "seed"),
    )

    payload = normalize_local_seed_tables(parsed, _provider_context(None))

    assert payload["assets"][0]["internet_exposed"] is True
    assert payload["asset_owned_by"][0]["asset_id"]


def test_normalize_public_kev_reference_accepts_common_tabular_output() -> None:
    parsed = load_tabular_artifact_bundle(
        {
            "expected_tables": [
                "known_exploited_vulnerabilities",
                "epss_kev_nvd",
                "nvd_kev_cves",
            ]
        },
        _provider_context(KEV_REFERENCE_KIT_DIR / "data"),
    )

    payload = normalize_public_kev_reference(parsed, _provider_context(None))

    assert payload["items"]
    row = payload["items"][0]
    assert row["cve_id"].startswith("CVE-")
    assert row["product_id"]
    assert row["vulnerability_name"]
    assert row["date_added_to_kev"]
    assert row["required_action"]
    assert row["epss_percentile"] is not None
    assert isinstance(row["cwes"], list)
    assert row["source"]
    assert row["vulnerable"] is True
    assert row["evidence_refs"]


def test_match_software_to_products_deduplicates_asset_product_pairs() -> None:
    payload = match_software_to_products(
        {
            "inventory_items": [
                {
                    "asset_id": "ASSET-1",
                    "software_name": "Apache HTTP Server",
                    "vendor": "Apache",
                    "version": "2.4.49",
                    "evidence_source": "scanner-a",
                    "last_seen": "2026-03-20",
                },
                {
                    "asset_id": "ASSET-1",
                    "software_name": "Apache HTTP Server",
                    "vendor": "Apache",
                    "version": "2.4.49",
                    "evidence_source": "scanner-b",
                    "last_seen": "2026-03-21",
                },
            ],
            "reference_products": [
                {
                    "product_id": "apache__http_server",
                    "product_name": "Http Server",
                    "vendor_id": "apache",
                    "vendor_name": "Apache",
                    "cpe_vendor": "apache",
                    "cpe_product": "http_server",
                    "cpe_part": "a",
                },
                {
                    "product_id": "nginx__nginx",
                    "product_name": "Nginx",
                    "vendor_id": "nginx",
                    "vendor_name": "Nginx",
                    "cpe_vendor": "nginx",
                    "cpe_product": "nginx",
                    "cpe_part": "a",
                },
            ],
        },
        _provider_context(None),
    )

    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["asset_id"] == "ASSET-1"
    assert item["product_id"] == "apache__http_server"
    assert item["observed_software_name"] == "Apache HTTP Server"
    assert item["observed_vendor"] == "Apache"
    assert item["installed_version"] == "2.4.49"
    assert item["inventory_source"] == "scanner-b"
    assert item["last_seen_at"] == "2026-03-21"
    assert item["evidence_source"] == "scanner-b"
    assert item["match_score"] == payload["items"][0]["match_score"]
    assert item["match_basis"]
    assert item["evidence_refs"][0]["source"] == "scanner-b"
    assert item["rationale"] == item["match_basis"]
    assert item["verdict"] == "support"


def test_match_software_to_products_accepts_entity_shaped_reference_products() -> None:
    payload = match_software_to_products(
        {
            "inventory_items": [
                {
                    "asset_id": "ASSET-1",
                    "software_name": "Apache HTTP Server",
                    "vendor": "Apache",
                    "version": "2.4.49",
                    "evidence_source": "scanner-a",
                    "last_seen": "2026-03-20",
                }
            ],
            "reference_products": [
                {
                    "entity_type": "Product",
                    "entity_id": "apache__http_server",
                    "properties": {
                        "product_id": "apache__http_server",
                        "vendor_id": "apache",
                        "product_name": "Http Server",
                        "vendor_name": "Apache",
                        "cpe_vendor": "apache",
                        "cpe_product": "http_server",
                        "cpe_part": "a",
                    },
                }
            ],
        },
        _provider_context(None),
    )

    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["asset_id"] == "ASSET-1"
    assert item["product_id"] == "apache__http_server"
    assert item["observed_software_name"] == "Apache HTTP Server"
    assert item["observed_vendor"] == "Apache"
    assert item["installed_version"] == "2.4.49"
    assert item["evidence_source"] == "scanner-a"
    assert item["match_score"] == payload["items"][0]["match_score"]
    assert item["evidence_refs"][0]["source"] == "scanner-a"
    assert item["verdict"] == "support"


def test_assess_asset_affected_uses_version_ranges() -> None:
    payload = assess_asset_affected(
        {
            "asset_product_edges": [
                {
                    "from_id": "ASSET-1",
                    "to_id": "apache__http_server",
                    "properties": {
                        "installed_version": "2.4.49",
                        "evidence_source": "qualys",
                    },
                },
                {
                    "from_id": "ASSET-2",
                    "to_id": "apache__http_server",
                    "properties": {
                        "installed_version": "2.4.58",
                        "evidence_source": "qualys",
                    },
                },
            ],
            "vulnerability_product_edges": [
                {
                    "from_id": "CVE-2021-41773",
                    "to_id": "apache__http_server",
                    "properties": {
                        "affected_versions": [
                            {"version_start_including": "2.4.0", "version_end_excluding": "2.4.50"}
                        ],
                        "fixed_version": "2.4.50",
                    },
                }
            ],
        },
        _provider_context(None),
    )

    assert payload["items"] == [
        {
            "asset_id": "ASSET-1",
            "cve_id": "CVE-2021-41773",
            "product_id": "apache__http_server",
            "installed_version": "2.4.49",
            "source": "qualys",
            "rationale": payload["items"][0]["rationale"],
            "verdict": "support",
            "evidence_refs": [],
        }
    ]


def test_assess_asset_exposure_derives_posture_and_control_signals() -> None:
    payload = assess_asset_exposure(
        {
            "affected_edges": [
                {"from_id": "ASSET-1", "to_id": "CVE-2021-0001", "properties": {}},
                {"from_id": "ASSET-2", "to_id": "CVE-2021-0001", "properties": {}},
            ],
            "assets": [
                {
                    "entity_id": "ASSET-1",
                    "properties": {
                        "hostname": "prod-web-01",
                        "criticality": "critical",
                        "environment": "production",
                        "internet_exposed": True,
                    },
                },
                {
                    "entity_id": "ASSET-2",
                    "properties": {
                        "hostname": "dev-app-01",
                        "criticality": "low",
                        "environment": "development",
                        "internet_exposed": False,
                    },
                },
            ],
            "asset_control_edges": [{"from_id": "ASSET-1", "to_id": "CTRL-1", "properties": {}}],
            "controls": [
                {
                    "entity_id": "CTRL-1",
                    "properties": {"name": "WAF", "status": "active"},
                }
            ],
        },
        _provider_context(None),
    )

    assert payload["items"] == [
        {
            "asset_id": "ASSET-1",
            "cve_id": "CVE-2021-0001",
            "status": "exposed",
            "priority": "high",
            "rationale": payload["items"][0]["rationale"],
            "product_id": "",
            "installed_version": "",
            "affected_basis": "",
            "affected_rationale": "",
            "exposure_basis": payload["items"][0]["exposure_basis"],
            "control_basis": payload["items"][0]["control_basis"],
            "evidence_source": "",
            "evidence_refs": [],
            "affected_verdict": "support",
            "exploitability_verdict": "support",
            "control_verdict": "unsure",
            "control_exposure_verdict": "unsure",
            "control_effect": "",
        }
    ]


def test_assess_asset_exposure_critical_when_no_active_controls() -> None:
    payload = assess_asset_exposure(
        {
            "affected_edges": [
                {"from_id": "ASSET-1", "to_id": "CVE-2021-0001", "properties": {}},
            ],
            "assets": [
                {
                    "entity_id": "ASSET-1",
                    "properties": {
                        "hostname": "prod-web-01",
                        "criticality": "critical",
                        "environment": "production",
                        "internet_exposed": True,
                    },
                },
            ],
            "asset_control_edges": [],
            "controls": [],
        },
        _provider_context(None),
    )

    assert payload["items"] == [
        {
            "asset_id": "ASSET-1",
            "cve_id": "CVE-2021-0001",
            "status": "exposed",
            "priority": "critical",
            "rationale": payload["items"][0]["rationale"],
            "product_id": "",
            "installed_version": "",
            "affected_basis": "",
            "affected_rationale": "",
            "exposure_basis": payload["items"][0]["exposure_basis"],
            "control_basis": payload["items"][0]["control_basis"],
            "evidence_source": "",
            "evidence_refs": [],
            "affected_verdict": "support",
            "exploitability_verdict": "support",
            "control_verdict": "support",
            "control_exposure_verdict": "support",
            "control_effect": "",
        }
    ]
    assert "due_by" not in payload["items"][0]


def test_assess_asset_exposure_uses_class_aware_control_mitigation() -> None:
    def run_with(effect: str, *, class_match: bool = True) -> dict[str, object]:
        return assess_asset_exposure(
            {
                "affected_edges": [
                    {"from_id": "ASSET-1", "to_id": "CVE-2021-0001", "properties": {}},
                ],
                "assets": [
                    {
                        "entity_id": "ASSET-1",
                        "properties": {
                            "hostname": "prod-web-01",
                            "criticality": "critical",
                            "environment": "production",
                            "internet_exposed": True,
                        },
                    },
                ],
                "asset_control_edges": [
                    {"from_id": "ASSET-1", "to_id": "CTRL-1", "properties": {}}
                ],
                "controls": [
                    {
                        "entity_id": "CTRL-1",
                        "properties": {"name": "WAF", "status": "active"},
                    }
                ],
                "vulnerability_classification_edges": [
                    {
                        "from_id": "CVE-2021-0001",
                        "to_id": "path_traversal",
                        "properties": {},
                    }
                ],
                "control_mitigation_edges": [
                    {
                        "from_id": "CTRL-1",
                        "to_id": "path_traversal" if class_match else "deserialization",
                        "properties": {
                            "effect": effect,
                            "validation_basis": f"{effect} regression",
                        },
                    }
                ],
            },
            _provider_context(None),
        )["items"][0]

    for effect in ("blocks", "compensates"):
        item = run_with(effect)
        assert item["status"] == "mitigated"
        assert item["priority"] == "medium"
        assert item["control_verdict"] == "support"
        assert item["control_exposure_verdict"] == "contradict"
        assert item["control_effect"] == effect
        assert effect in str(item["control_basis"])

    reduced = run_with("reduces")
    assert reduced["status"] == "exposed"
    assert reduced["priority"] == "high"
    assert reduced["control_verdict"] == "support"
    assert reduced["control_exposure_verdict"] == "support"
    assert reduced["control_effect"] == "reduces"
    assert "reduces" in str(reduced["control_basis"])

    detected = run_with("detects")
    assert detected["status"] == "exposed"
    assert detected["priority"] == "critical"
    assert detected["control_verdict"] == "support"
    assert detected["control_exposure_verdict"] == "support"
    assert detected["control_effect"] == "detects"
    assert "detects" in str(detected["control_basis"])

    unmatched = run_with("blocks", class_match=False)
    assert unmatched["status"] == "exposed"
    assert unmatched["priority"] == "high"
    assert unmatched["control_verdict"] == "unsure"
    assert unmatched["control_exposure_verdict"] == "unsure"
    assert unmatched["control_effect"] == ""
    assert "require review" in str(unmatched["control_basis"])


def test_propose_asset_exposure_mitigated_control_signal_supports_candidate(
    tmp_path: Path,
) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    service_lock(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    _approve_workflow_group(instance, "propose_asset_products")

    graph = instance.load_graph()
    active_controls = {
        control.entity_id
        for control in graph.list_entities("CompensatingControl")
        if control.properties.get("status") == "active"
    }
    affected = assess_asset_affected(
        {
            "asset_product_edges": graph.list_edges("asset_runs_product"),
            "vulnerability_product_edges": graph.list_edges("vulnerability_affects_product"),
        },
        _provider_context(None),
    )["items"]
    affected_cves_by_asset: dict[str, list[str]] = {}
    for item in affected:
        affected_cves_by_asset.setdefault(item["asset_id"], []).append(item["cve_id"])

    selected: tuple[str, str, str] | None = None
    for edge in sorted(
        graph.list_edges("asset_has_control"),
        key=lambda item: (item["from_id"], item["to_id"]),
    ):
        if edge["to_id"] not in active_controls:
            continue
        cve_ids = sorted(affected_cves_by_asset.get(edge["from_id"], []))
        if cve_ids:
            selected = (edge["from_id"], edge["to_id"], cve_ids[0])
            break

    assert selected is not None
    asset_id, control_id, cve_id = selected
    class_id = "path_traversal"

    _approve_workflow_group_with_input(
        instance,
        "propose_vulnerability_classification",
        {
            "items": [
                {
                    "cve_id": cve_id,
                    "class_id": class_id,
                    "basis": "Regression classification for mitigated posture signal.",
                    "source": "test",
                    "verdict": "support",
                }
            ]
        },
    )
    _approve_workflow_group_with_input(
        instance,
        "propose_control_mitigates_class",
        {
            "items": [
                {
                    "control_id": control_id,
                    "class_id": class_id,
                    "effect": "blocks",
                    "validation_basis": "Regression control coverage.",
                    "verified_at": "2026-04-01",
                    "expires_at": "2026-10-01",
                    "evidence_refs": [],
                    "verdict": "support",
                }
            ]
        },
    )

    proposed = service_propose_workflow(instance, "propose_asset_exposure", {})
    assert proposed.group_id is not None
    group_store = instance.get_group_store()
    try:
        group = group_store.get_group(proposed.group_id)
        members = group_store.get_members(proposed.group_id)
    finally:
        group_store.close()

    assert group is not None
    assert group.review_priority == "review"
    member = next(
        item for item in members if item.from_id == asset_id and item.to_id == cve_id
    )
    assert member.properties["status"] == "mitigated"
    assert member.properties["priority"] == "medium"
    signals = {signal.signal_source: signal.signal for signal in member.signals}
    assert signals["control_effectiveness"] == "support"


def test_assess_exposure_reconciliation_closes_stale_reference_pairs() -> None:
    payload = assess_exposure_reconciliation(
        {
            "accepted_exposure_edges": [
                {
                    "from_id": "ASSET-1",
                    "to_id": "CVE-2021-0001",
                    "properties": {"product_id": "apache__http_server"},
                }
            ],
            "affected_items": [],
            "asset_product_edges": [
                {"from_id": "ASSET-1", "to_id": "apache__http_server", "properties": {}}
            ],
            "vulnerability_product_edges": [],
            "remediated_edges": [],
        },
        _provider_context(None),
    )

    assert payload["items"] == [
        {
            "asset_id": "ASSET-1",
            "cve_id": "CVE-2021-0001",
            "remediation_type": "reference_changed",
            "evidence_source": "kev_reference_reconciliation",
            "evidence_refs": [
                {
                    "source": "kev_reference_reconciliation",
                    "source_record_id": "ASSET-1:CVE-2021-0001",
                }
            ],
            "rationale": payload["items"][0]["rationale"],
            "verdict": "support",
        }
    ]


def test_kev_demo_workflows_run_end_to_end_from_composed_config(tmp_path: Path) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path, str(config_path))

    lock_result = service_lock(instance)
    assert lock_result.providers_locked >= 7

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")

    _approve_workflow_group(instance, "propose_asset_products")
    pre_exposure_graph = instance.load_graph()
    pre_exposure_product_id = pre_exposure_graph.list_edges("asset_runs_product")[0]["to_id"]
    pre_exposure_context = service_query(
        instance,
        "product_asset_context",
        {"product_id": pre_exposure_product_id},
    )
    assert pre_exposure_context.total_results > 0
    assert any(
        getattr(_include_result(row, "affected_vulnerabilities"), "count") > 0
        for row in pre_exposure_context.results
    )

    _approve_workflow_group(instance, "propose_asset_exposure")

    graph = instance.load_graph()
    assert graph.entity_count("Asset") == _csv_row_count(
        KEV_KIT_DIR / "data" / "seed" / "assets.csv"
    )
    assert graph.entity_count("VulnerabilityClass") == _csv_row_count(
        KEV_KIT_DIR / "data" / "seed" / "vulnerability_classes.csv"
    )
    assert graph.edge_count("asset_owned_by") == _csv_row_count(
        KEV_KIT_DIR / "data" / "seed" / "asset_owned_by.csv"
    )
    relationship_names = {
        relationship.name for relationship in instance.load_config().relationships
    }
    assert "asset_vulnerability_posture" in relationship_names
    assert "asset_exposed_to_vulnerability" not in relationship_names
    assert graph.edge_count("asset_runs_product") > 0
    assert graph.edge_count("asset_vulnerability_posture") > 0
    assert graph.edge_count("asset_remediated_vulnerability") == 0

    exposure_edge = graph.list_edges("asset_vulnerability_posture")[0]

    affected_asset_id = exposure_edge["from_id"]
    affected_cve_id = exposure_edge["to_id"]
    assert exposure_edge["properties"]["product_id"]
    assert exposure_edge["properties"]["installed_version"]
    assert exposure_edge["properties"]["status"] == "exposed"
    assert exposure_edge["properties"]["affected_basis"]
    assert exposure_edge["properties"]["exposure_basis"]
    assert exposure_edge["properties"]["control_basis"]
    assert exposure_edge["properties"]["evidence_refs"]
    vulnerability = graph.get_entity("Vulnerability", affected_cve_id)
    assert vulnerability is not None
    assert vulnerability.properties["vulnerability_name"]
    assert vulnerability.properties["date_added_to_kev"]
    assert vulnerability.properties["required_action"]
    assert isinstance(vulnerability.properties["cwes"], list)
    reference_edge = next(
        edge
        for edge in graph.list_edges("vulnerability_affects_product")
        if edge["from_id"] == affected_cve_id
        and edge["to_id"] == exposure_edge["properties"]["product_id"]
    )
    assert reference_edge["properties"]["source"]
    assert reference_edge["properties"]["vulnerable"] is True
    assert reference_edge["properties"]["evidence_refs"]
    owner_edge = next(
        edge
        for edge in graph.list_edges("asset_owned_by")
        if edge["from_id"] == exposure_edge["from_id"]
    )
    owner_id = owner_edge["to_id"]
    product_edge = next(
        edge
        for edge in graph.list_edges("asset_runs_product")
        if edge["from_id"] == affected_asset_id
    )
    product_id = product_edge["to_id"]
    assert product_edge["properties"]["observed_software_name"]
    assert product_edge["properties"]["installed_version"]
    assert product_edge["properties"]["match_basis"]
    assert product_edge["properties"]["evidence_refs"]

    vulnerability_asset_context = service_query(
        instance,
        "vulnerability_asset_context",
        {"cve_id": affected_cve_id},
    )
    owner_patch_queue = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    product_asset_context = service_query(
        instance,
        "product_asset_context",
        {"product_id": product_id},
    )

    assert vulnerability_asset_context.total_results > 0
    assert owner_patch_queue.total_results > 0
    assert product_asset_context.total_results > 0

    _approve_workflow_group_with_input(
        instance,
        "propose_vulnerability_classification",
        {
            "items": [
                {
                    "cve_id": affected_cve_id,
                    "class_id": "path_traversal",
                    "basis": "Test classification for control coverage traversal",
                    "source": "test",
                    "verdict": "support",
                }
            ]
        },
    )
    _approve_workflow_group_with_input(
        instance,
        "propose_control_mitigates_class",
        {
            "items": [
                {
                    "control_id": "CTRL-1",
                    "class_id": "path_traversal",
                    "effect": "blocks",
                    "validation_basis": "Test control coverage traversal",
                    "verified_at": "2026-04-01",
                    "expires_at": "2026-10-01",
                    "evidence_refs": [
                        {
                            "source": "control_review",
                            "source_record_id": "CTRL-1:path_traversal",
                        }
                    ],
                    "verdict": "support",
                }
            ]
        },
    )
    vulnerability_class_context = service_query(
        instance,
        "vulnerability_class_context",
        {"class_id": "path_traversal"},
    )
    control_coverage_gap = service_query(
        instance,
        "control_coverage_gap",
        {"control_id": "CTRL-1"},
    )

    assert vulnerability_class_context.total_results > 0
    assert control_coverage_gap.total_results > 0
    assert any(
        getattr(_row_path_segment(row, "mitigated_class"), "properties", {}).get("effect")
        == "blocks"
        for row in control_coverage_gap.results
    )
    graph_after_control_review = instance.load_graph()
    control_mitigation_edge = next(
        edge
        for edge in graph_after_control_review.list_edges("control_mitigates_class")
        if edge["from_id"] == "CTRL-1" and edge["to_id"] == "path_traversal"
    )
    assert control_mitigation_edge["properties"]["effect"] == "blocks"
    assert control_mitigation_edge["properties"]["verified_at"] == "2026-04-01"
    assert control_mitigation_edge["properties"]["expires_at"] == "2026-10-01"
    assert control_mitigation_edge["properties"]["evidence_refs"] == [
        {
            "source": "control_review",
            "source_record_id": "CTRL-1:path_traversal",
        }
    ]


def test_owner_patch_queue_excludes_remediated_pairs(tmp_path: Path) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    service_lock(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    _approve_workflow_group(instance, "propose_asset_products")
    _approve_workflow_group(instance, "propose_asset_exposure")

    graph = instance.load_graph()
    asset_to_owner = {
        edge["from_id"]: edge["to_id"] for edge in graph.list_edges("asset_owned_by")
    }
    assets_with_services = {
        edge["to_id"] for edge in graph.list_edges("service_depends_on_asset")
    }
    product_to_vendor = {
        edge["from_id"]: edge["to_id"] for edge in graph.list_edges("product_from_vendor")
    }
    remediated_pairs = {
        (edge["from_id"], edge["to_id"])
        for edge in graph.list_edges("asset_remediated_vulnerability")
    }
    owner_vuln_counts: dict[tuple[str, str], int] = {}
    unique_pair: tuple[str, str, str, str] | None = None
    for edge in graph.list_edges("asset_vulnerability_posture"):
        if (edge["from_id"], edge["to_id"]) in remediated_pairs:
            continue
        owner_id = asset_to_owner.get(edge["from_id"])
        if owner_id is None:
            continue
        key = (owner_id, edge["to_id"])
        owner_vuln_counts[key] = owner_vuln_counts.get(key, 0) + 1
    for edge in graph.list_edges("asset_vulnerability_posture"):
        if (edge["from_id"], edge["to_id"]) in remediated_pairs:
            continue
        owner_id = asset_to_owner.get(edge["from_id"])
        if owner_id is None:
            continue
        product_id = edge["properties"].get("product_id")
        vendor_id = product_to_vendor.get(product_id)
        if edge["from_id"] not in assets_with_services or vendor_id is None:
            continue
        key = (owner_id, edge["to_id"])
        if owner_vuln_counts.get(key) == 1:
            unique_pair = (edge["from_id"], edge["to_id"], owner_id, vendor_id)
            break

    assert unique_pair is not None
    asset_id, cve_id, owner_id, vendor_id = unique_pair

    before = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    before_ids = _query_entity_ids(before.results)
    assert cve_id in before_ids

    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_remediated_vulnerability",
            from_type="Asset",
            from_id=asset_id,
            to_type="Vulnerability",
            to_id=cve_id,
            properties={
                "remediation_type": "patched",
                "verified_at": "2026-05-04",
                "evidence_source": "test",
                "evidence_refs": [],
                "rationale": "Regression test closure context.",
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                )
            ),
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_patch_exception_for",
            from_type="Asset",
            from_id=asset_id,
            to_type="Vulnerability",
            to_id=cve_id,
            properties={
                "exception_id": "EXC-2026-REMEDIATED-PAIR",
                "review_due_at": "2026-05-07",
                "scope_basis": "Regression test scoped exception context.",
                "rationale": "Regression test scoped exception context.",
                "evidence_source": "test",
                "evidence_refs": [],
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                )
            ),
        )
    )
    instance.save_graph(graph)

    after = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    after_ids = _query_entity_ids(after.results)

    assert cve_id not in after_ids
    assert after.total_results == before.total_results - 1

    vendor_context = service_query(instance, "vendor_service_impact", {"vendor_id": vendor_id})
    context_row = next(
        row
        for row in vendor_context.results
        if getattr(_row_path_segment(row, "exposure"), "from_id") == asset_id
        and getattr(_row_path_segment(row, "exposure"), "to_id") == cve_id
    )
    assert any(item.edge.to_id == cve_id for item in _include_items(context_row, "remediations"))
    assert any(
        item.edge.to_id == cve_id for item in _include_items(context_row, "scoped_exceptions")
    )


def test_owner_patch_queue_excludes_non_exposed_posture_rows(tmp_path: Path) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    service_lock(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    _approve_workflow_group(instance, "propose_asset_products")
    _approve_workflow_group(instance, "propose_asset_exposure")

    graph = instance.load_graph()
    asset_to_owner = {
        edge["from_id"]: edge["to_id"] for edge in graph.list_edges("asset_owned_by")
    }
    assert asset_to_owner
    asset_id, owner_id = sorted(asset_to_owner.items())[0]

    before = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    before_ids = _query_entity_ids(before.results)
    remediated_pairs = {
        (edge["from_id"], edge["to_id"])
        for edge in graph.list_edges("asset_remediated_vulnerability")
    }
    candidate_cve = next(
        (
            vulnerability.entity_id
            for vulnerability in sorted(
                graph.list_entities("Vulnerability"),
                key=lambda entity: entity.entity_id,
            )
            if vulnerability.entity_id not in before_ids
            and (asset_id, vulnerability.entity_id) not in remediated_pairs
        ),
        None,
    )
    assert candidate_cve is not None

    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_vulnerability_posture",
            from_type="Asset",
            from_id=asset_id,
            to_type="Vulnerability",
            to_id=candidate_cve,
            properties={
                "status": "not_affected",
                "priority": "low",
                "product_id": "test-product",
                "installed_version": "",
                "affected_basis": "Regression test non-actionable posture.",
                "exposure_basis": "Regression test non-actionable posture.",
                "control_basis": "Regression test non-actionable posture.",
                "evidence_source": "test",
                "evidence_refs": [],
                "rationale": "Approved posture rows that are not exposed stay out of queues.",
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                )
            ),
        )
    )
    instance.save_graph(graph)

    after = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})

    assert candidate_cve not in _query_entity_ids(after.results)
    assert after.total_results == before.total_results


def test_owner_patch_queue_excludes_scoped_exception_pairs(tmp_path: Path) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    service_lock(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    _approve_workflow_group(instance, "propose_asset_products")
    _approve_workflow_group(instance, "propose_asset_exposure")

    graph = instance.load_graph()
    asset_to_owner = {
        edge["from_id"]: edge["to_id"] for edge in graph.list_edges("asset_owned_by")
    }
    owner_vuln_counts: dict[tuple[str, str], int] = {}
    unique_pair: tuple[str, str, str] | None = None
    for edge in graph.list_edges("asset_vulnerability_posture"):
        owner_id = asset_to_owner.get(edge["from_id"])
        if owner_id is None:
            continue
        key = (owner_id, edge["to_id"])
        owner_vuln_counts[key] = owner_vuln_counts.get(key, 0) + 1
    for edge in graph.list_edges("asset_vulnerability_posture"):
        owner_id = asset_to_owner.get(edge["from_id"])
        if owner_id is None:
            continue
        key = (owner_id, edge["to_id"])
        if owner_vuln_counts.get(key) == 1:
            unique_pair = (edge["from_id"], edge["to_id"], owner_id)
            break

    assert unique_pair is not None
    asset_id, cve_id, owner_id = unique_pair

    before = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    before_ids = _query_entity_ids(before.results)
    assert cve_id in before_ids

    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_patch_exception_for",
            from_type="Asset",
            from_id=asset_id,
            to_type="Vulnerability",
            to_id=cve_id,
            properties={
                "exception_id": "EXC-2026-TEST",
                "review_due_at": "2026-05-03",
                "scope_basis": "Regression test scoped exception.",
                "rationale": "Approved scoped exception suppresses patch queue action.",
                "evidence_source": "test",
                "evidence_refs": [
                    {
                        "source": "test",
                        "source_record_id": f"{asset_id}:{cve_id}:exception",
                    }
                ],
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                )
            ),
        )
    )
    instance.save_graph(graph)

    after = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    after_ids = _query_entity_ids(after.results)

    assert cve_id not in after_ids
    assert after.total_results == before.total_results - 1


def test_exposure_reconciliation_no_candidates_completes_without_group(
    tmp_path: Path,
) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    service_lock(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    _approve_workflow_group(instance, "propose_asset_products")
    _approve_workflow_group(instance, "propose_asset_exposure")

    proposed = service_propose_workflow(instance, "propose_exposure_reconciliation", {})

    assert proposed.group_id is None
    assert proposed.group_status == "no_candidates"
    assert proposed.output["status"] == "no_candidates"
    assert proposed.output["candidate_count"] == 0
    assert proposed.output["group_created"] is False
    assert proposed.receipt is not None
    assert proposed.receipt.committed is False
    group_store = instance.get_group_store()
    try:
        groups = group_store.list_groups(
            relationship_type="asset_remediated_vulnerability"
        )
    finally:
        group_store.close()
    assert groups == []


def test_release_backed_kev_overlay_can_propose_asset_products(tmp_path: Path) -> None:
    reference_root = tmp_path / "reference"
    reference = service_init(reference_root, kit="kev-reference").instance
    service_lock(reference)
    _apply_canonical_workflow(reference, "build_public_kev_reference")
    product = reference.load_graph().list_entities("Product")[0]
    assert product.properties.get("vendor_id")

    release_dir = tmp_path / "releases" / "current"
    service_publish_world(
        reference,
        transport_ref=f"file://{release_dir}",
        world_id="kev-reference",
        release_id="2026-03-31",
        compatibility="data_only",
    )

    overlay_root = tmp_path / "overlay"
    overlay = service_create_world_overlay(
        transport_ref=f"file://{release_dir}",
        kit="kev-triage",
        root_dir=overlay_root,
    ).instance

    proposed = service_propose_workflow(overlay, "propose_asset_products", {})
    assert proposed.group_id is not None
