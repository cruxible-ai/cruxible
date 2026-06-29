"""Integration tests for the KEV kit providers and workflows."""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.composer import compose_config_files
from cruxible_core.config.loader import save_config
from cruxible_core.errors import QueryExecutionError
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.evidence import RelationshipEvidence
from cruxible_core.graph.types import RelationshipInstance, RelationshipMetadata
from cruxible_core.kits import load_kit_provider_module, write_materialized_kit_metadata
from cruxible_core.provider.types import ProviderContext, ResolvedArtifact
from cruxible_core.providers.common.tabular import load_tabular_artifact_bundle
from cruxible_core.service import (
    service_apply_workflow,
    service_create_state_overlay,
    service_init,
    service_lock,
    service_propose_workflow,
    service_publish_state,
    service_query,
    service_resolve_group,
    service_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KEV_KIT_DIR = REPO_ROOT / "kits" / "kev-triage"
KEV_REFERENCE_KIT_DIR = REPO_ROOT / "kits" / "kev-reference"

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


def _path_segment_review_status(row: object, alias: str) -> str:
    segment = _row_path_segment(row, alias)
    return segment.metadata.assertion.review.status


def _has_exposure_path(rows: list[object], asset_id: str, cve_id: str) -> bool:
    return any(
        getattr(_row_path_segment(row, "exposure"), "from_id") == asset_id
        and getattr(_row_path_segment(row, "exposure"), "to_id") == cve_id
        for row in rows
    )


def _assess_joined_asset_affected(
    asset_product_edges: list[dict],
    vulnerability_product_edges: list[dict],
) -> list[dict]:
    joined_product_edges = [
        {
            "product_id": asset_edge["to_id"],
            "asset_product_edge": asset_edge,
            "vulnerability_product_edge": vulnerability_edge,
        }
        for asset_edge in asset_product_edges
        for vulnerability_edge in vulnerability_product_edges
        if asset_edge["to_id"] == vulnerability_edge["to_id"]
    ]
    return assess_asset_affected(
        {"joined_product_edges": joined_product_edges},
        _provider_context(None),
    )["items"]


_assessment_module = load_kit_provider_module(
    KEV_KIT_DIR / "providers" / "assessment.py",
    KEV_KIT_DIR,
)

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
            digest="sha256:test",
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
    assert "propose_control_mitigates_class" not in config.workflows
    control_mitigation = next(
        relationship
        for relationship in config.relationships
        if relationship.name == "control_mitigates_class"
    )
    assert control_mitigation.proposal_policy is None


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


def _lock_mutable_kev_fixture(instance: CruxibleInstance):
    return service_lock(instance, force=True)


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


def test_build_local_state_shapes_seed_tables_in_config(tmp_path: Path) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    _lock_mutable_kev_fixture(instance)

    _apply_canonical_workflow(instance, "build_local_state")

    graph = instance.load_graph()
    asset = graph.get_entity("Asset", "ASSET-1")
    assert asset is not None
    assert asset.properties["internet_exposed"] is True
    patch_window = graph.get_entity("PatchWindow", "PW-1")
    assert patch_window is not None
    assert patch_window.properties["emergency_patch_allowed"] is True
    assert patch_window.properties["testing_required"] is True
    mitigation = next(
        edge
        for edge in graph.list_edges("control_mitigates_class")
        if edge["from_id"] == "CTRL-1" and edge["to_id"] == "path_traversal"
    )
    assert mitigation["properties"]["effect"] == "blocks"
    assert mitigation["metadata"]["evidence"]["evidence_refs"] == [
        {
            "source": "control_review",
            "source_record_id": "CTRL-1:path_traversal",
            "metadata": {"observed_at": "2026-04-01"},
        }
    ]


def test_build_local_state_requires_control_mitigation_effect(tmp_path: Path) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    mitigation_seed = tmp_path / "data" / "seed" / "control_mitigates_class.csv"
    with mitigation_seed.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        assert fieldnames is not None
        rows = list(reader)
    assert rows
    rows[0]["effect"] = ""
    with mitigation_seed.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    config = compose_config_files(
        base_path=KEV_REFERENCE_KIT_DIR / "config.yaml",
        overlay_path=KEV_KIT_DIR / "config.yaml",
    )
    config.artifacts["local_seed_bundle"] = config.artifacts["local_seed_bundle"].model_copy(
        update={"uri": str(tmp_path / "data" / "seed")}
    )
    save_config(config, config_path)
    write_materialized_kit_metadata(tmp_path)

    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    _lock_mutable_kev_fixture(instance)

    with pytest.raises(QueryExecutionError) as exc_info:
        _apply_canonical_workflow(instance, "build_local_state")

    message = str(exc_info.value)
    assert "control_mitigates_class" in message
    assert "effect" in message


def test_kev_domain_providers_do_not_own_seed_table_inventory() -> None:
    config = compose_config_files(
        base_path=KEV_REFERENCE_KIT_DIR / "config.yaml",
        overlay_path=KEV_KIT_DIR / "config.yaml",
    )

    assert "normalize_local_seed_tables" not in config.providers
    assert not (KEV_KIT_DIR / "providers" / "seed.py").exists()
    assert config.providers["normalize_public_kev_reference"].artifact is None

    artifact_providers = {
        name for name, provider in config.providers.items() if provider.artifact is not None
    }
    assert artifact_providers == {"parse_public_kev_bundle", "parse_local_seed_bundle"}


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

    payload = normalize_public_kev_reference(
        {
            "kev_rows": parsed["tables"]["known_exploited_vulnerabilities"]["rows"],
            "epss_rows": parsed["tables"]["epss_kev_nvd"]["rows"],
            "nvd_cpe_rows": parsed["tables"]["nvd_kev_cves"]["rows"],
        },
        _provider_context(None),
    )

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
    assert "match score" not in item["match_basis"].lower()
    assert "confidence" not in item["match_basis"].lower()
    assert re.search(r"\b\d+\.\d+\b", item["match_basis"]) is None
    assert item["evidence_refs"][0]["source"] == "scanner-b"
    assert item["rationale"] == item["match_basis"]
    assert "match score" not in item["rationale"].lower()
    assert re.search(r"\b\d+\.\d+\b", item["rationale"]) is None
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
            "joined_product_edges": [
                {
                    "product_id": "apache__http_server",
                    "asset_product_edge": {
                        "from_id": "ASSET-1",
                        "to_id": "apache__http_server",
                        "properties": {
                            "installed_version": "2.4.49",
                            "evidence_source": "qualys",
                        },
                    },
                    "vulnerability_product_edge": {
                        "from_id": "CVE-2021-41773",
                        "to_id": "apache__http_server",
                        "properties": {
                            "affected_versions": [
                                {
                                    "version_start_including": "2.4.0",
                                    "version_end_excluding": "2.4.50",
                                }
                            ],
                            "fixed_version": "2.4.50",
                        },
                    },
                },
                {
                    "product_id": "apache__http_server",
                    "asset_product_edge": {
                        "from_id": "ASSET-2",
                        "to_id": "apache__http_server",
                        "properties": {
                            "installed_version": "2.4.58",
                            "evidence_source": "qualys",
                        },
                    },
                    "vulnerability_product_edge": {
                        "from_id": "CVE-2021-41773",
                        "to_id": "apache__http_server",
                        "properties": {
                            "affected_versions": [
                                {
                                    "version_start_including": "2.4.0",
                                    "version_end_excluding": "2.4.50",
                                }
                            ],
                            "fixed_version": "2.4.50",
                        },
                    },
                },
                {
                    "product_id": "apache__http_server",
                    "asset_product_edge": {
                        "from_id": "ASSET-3",
                        "to_id": "apache__http_server",
                        "properties": {
                            "installed_version": "2.4.49",
                            "evidence_source": "qualys",
                        },
                    },
                    "vulnerability_product_edge": {
                        "from_id": "CVE-2021-0002",
                        "to_id": "apache__http_server",
                        "properties": {},
                    },
                },
                {
                    "product_id": "apache__http_server_alt",
                    "asset_product_edge": {
                        "from_id": "ASSET-3",
                        "to_id": "apache__http_server_alt",
                        "properties": {
                            "installed_version": "2.4.49",
                            "evidence_source": "qualys",
                        },
                    },
                    "vulnerability_product_edge": {
                        "from_id": "CVE-2021-0002",
                        "to_id": "apache__http_server_alt",
                        "properties": {
                            "affected_versions": [
                                {
                                    "version_start_including": "2.4.0",
                                    "version_end_excluding": "2.4.50",
                                }
                            ],
                            "fixed_version": "2.4.50",
                        },
                    },
                },
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
            "verdict_rank": 2,
        },
        {
            "asset_id": "ASSET-3",
            "cve_id": "CVE-2021-0002",
            "product_id": "apache__http_server_alt",
            "installed_version": "2.4.49",
            "source": "qualys",
            "rationale": payload["items"][1]["rationale"],
            "verdict": "support",
            "evidence_refs": [],
            "verdict_rank": 2,
        },
        {
            "asset_id": "ASSET-3",
            "cve_id": "CVE-2021-0002",
            "product_id": "apache__http_server",
            "installed_version": "2.4.49",
            "source": "qualys",
            "rationale": payload["items"][2]["rationale"],
            "verdict": "unsure",
            "evidence_refs": [],
            "verdict_rank": 1,
        },
    ]


def test_assess_asset_exposure_derives_posture_and_control_signals() -> None:
    payload = assess_asset_exposure(
        {
            "affected_asset_context": [
                {
                    "affected_item": {
                        "asset_id": "ASSET-1",
                        "cve_id": "CVE-2021-0001",
                    },
                    "asset_entity": {
                        "entity_id": "ASSET-1",
                        "properties": {
                            "hostname": "prod-web-01",
                            "criticality": "critical",
                            "environment": "production",
                            "internet_exposed": True,
                        },
                    },
                },
                {
                    "affected_item": {
                        "asset_id": "ASSET-2",
                        "cve_id": "CVE-2021-0001",
                    },
                    "asset_entity": {
                        "entity_id": "ASSET-2",
                        "properties": {
                            "hostname": "dev-app-01",
                            "criticality": "low",
                            "environment": "development",
                            "internet_exposed": False,
                        },
                    },
                },
            ],
            "active_control_bindings": [
                {
                    "asset_id": "ASSET-1",
                    "control_id": "CTRL-1",
                    "asset_control_edge": {
                        "from_id": "ASSET-1",
                        "to_id": "CTRL-1",
                        "properties": {},
                    },
                    "control_entity": {
                        "entity_id": "CTRL-1",
                        "properties": {"name": "WAF", "status": "active"},
                    },
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
            "product_id": "",
            "installed_version": "",
            "basis": {
                "affected": "",
                "exposure": payload["items"][0]["basis"]["exposure"],
                "control": payload["items"][0]["basis"]["control"],
            },
            "evidence_refs": [],
            "verdicts": {
                "affected": "support",
                "exploitability": "support",
                "control": "unsure",
            },
            "control_effect": "",
        }
    ]


def test_assess_asset_exposure_critical_when_no_active_controls() -> None:
    payload = assess_asset_exposure(
        {
            "affected_asset_context": [
                {
                    "affected_item": {
                        "asset_id": "ASSET-1",
                        "cve_id": "CVE-2021-0001",
                    },
                    "asset_entity": {
                        "entity_id": "ASSET-1",
                        "properties": {
                            "hostname": "prod-web-01",
                            "criticality": "critical",
                            "environment": "production",
                            "internet_exposed": True,
                        },
                    },
                },
            ],
            "active_control_bindings": [],
        },
        _provider_context(None),
    )

    assert payload["items"] == [
        {
            "asset_id": "ASSET-1",
            "cve_id": "CVE-2021-0001",
            "status": "exposed",
            "priority": "critical",
            "product_id": "",
            "installed_version": "",
            "basis": {
                "affected": "",
                "exposure": payload["items"][0]["basis"]["exposure"],
                "control": payload["items"][0]["basis"]["control"],
            },
            "evidence_refs": [],
            "verdicts": {
                "affected": "support",
                "exploitability": "support",
                "control": "support",
            },
            "control_effect": "",
        }
    ]
    assert "due_by" not in payload["items"][0]


def test_assess_asset_exposure_uses_class_aware_control_mitigation() -> None:
    def run_with(effect: str, *, class_match: bool = True) -> dict[str, object]:
        return assess_asset_exposure(
            {
                "affected_asset_context": [
                    {
                        "affected_item": {
                            "asset_id": "ASSET-1",
                            "cve_id": "CVE-2021-0001",
                        },
                        "asset_entity": {
                            "entity_id": "ASSET-1",
                            "properties": {
                                "hostname": "prod-web-01",
                                "criticality": "critical",
                                "environment": "production",
                                "internet_exposed": True,
                            },
                        },
                    },
                ],
                "active_control_bindings": [
                    {
                        "asset_id": "ASSET-1",
                        "control_id": "CTRL-1",
                        "asset_control_edge": {
                            "from_id": "ASSET-1",
                            "to_id": "CTRL-1",
                            "properties": {},
                            "metadata": {
                                "evidence": {
                                    "evidence_refs": [
                                        {
                                            "source": "control_inventory",
                                            "source_record_id": "asset-control-1",
                                        }
                                    ]
                                }
                            },
                        },
                        "control_entity": {
                            "entity_id": "CTRL-1",
                            "properties": {
                                "name": "WAF",
                                "status": "active",
                                "evidence_refs": [
                                    {
                                        "source": "control_catalog",
                                        "source_record_id": "CTRL-1",
                                    }
                                ],
                            },
                        },
                    },
                ],
                "vulnerability_classification_edges": [
                    {
                        "from_id": "CVE-2021-0001",
                        "to_id": "path_traversal",
                        "properties": {},
                        "metadata": {
                            "evidence": {
                                "evidence_refs": [
                                    {
                                        "source": "classification_review",
                                        "source_record_id": "CVE-2021-0001:path_traversal",
                                    }
                                ]
                            }
                        },
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
                        "metadata": {
                            "evidence": {
                                "evidence_refs": [
                                    {
                                        "source": "control_mapping",
                                        "source_record_id": f"CTRL-1:{effect}",
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
            _provider_context(None),
        )["items"][0]

    mitigated = run_with("blocks")
    assert mitigated["status"] == "mitigated"
    assert mitigated["priority"] == "medium"
    assert mitigated["verdicts"]["control"] == "support"
    assert mitigated["control_effect"] == "blocks"
    assert "WAF blocks path_traversal" in str(mitigated["basis"]["control"])
    evidence_sources = {ref["source"] for ref in mitigated["evidence_refs"]}
    assert {
        "control_inventory",
        "control_catalog",
        "classification_review",
        "control_mapping",
    } <= evidence_sources

    compensated = run_with("compensates")
    assert compensated["status"] == "mitigated"
    assert compensated["priority"] == "medium"
    assert compensated["verdicts"]["control"] == "support"
    assert compensated["control_effect"] == "compensates"

    reduced = run_with("reduces")
    assert reduced["status"] == "exposed"
    assert reduced["priority"] == "high"
    assert reduced["verdicts"]["control"] == "support"
    assert reduced["control_effect"] == "reduces"

    detected = run_with("detects")
    assert detected["status"] == "exposed"
    assert detected["priority"] == "critical"
    assert detected["verdicts"]["control"] == "support"
    assert detected["control_effect"] == "detects"

    no_match = run_with("blocks", class_match=False)
    assert no_match["status"] == "exposed"
    assert no_match["priority"] == "high"
    assert no_match["verdicts"]["control"] == "unsure"
    assert no_match["control_effect"] == ""


def test_propose_asset_exposure_mitigated_control_signal_supports_candidate(
    tmp_path: Path,
) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    _lock_mutable_kev_fixture(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    _approve_workflow_group(instance, "propose_asset_products")

    graph = instance.load_graph()
    active_controls = {
        control.entity_id
        for control in graph.list_entities("CompensatingControl")
        if control.properties.get("status") == "active"
    }
    path_traversal_mitigating_controls = {
        edge["from_id"]
        for edge in graph.list_edges("control_mitigates_class")
        if edge["to_id"] == "path_traversal"
        and edge["properties"].get("effect") in {"blocks", "compensates"}
    }
    affected = _assess_joined_asset_affected(
        graph.list_edges("asset_runs_product"),
        graph.list_edges("vulnerability_affects_product"),
    )
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
        if edge["to_id"] not in path_traversal_mitigating_controls:
            continue
        cve_ids = sorted(affected_cves_by_asset.get(edge["from_id"], []))
        if cve_ids:
            selected = (edge["from_id"], edge["to_id"], cve_ids[0])
            break

    assert selected is not None
    asset_id, _control_id, cve_id = selected
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

    proposed = service_propose_workflow(instance, "propose_asset_exposure", {})
    assert proposed.receipt is not None
    assert any(
        node.detail.get("step_id") == "affected_product_join"
        and node.detail.get("kind") == "join_items"
        and node.detail.get("output_count", 0) > 0
        for node in proposed.receipt.nodes
    )
    assert proposed.group_id is not None
    group_store = instance.get_group_store()
    try:
        group = group_store.get_group(proposed.group_id)
        members = group_store.get_members(proposed.group_id)
    finally:
        group_store.close()

    assert group is not None
    assert group.review_priority == "review"
    member = next(item for item in members if item.from_id == asset_id and item.to_id == cve_id)
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
                    "metadata": {
                        "evidence": {
                            "evidence_refs": [
                                {
                                    "source": "accepted_posture",
                                    "source_record_id": ("ASSET-1:CVE-2021-0001:accepted"),
                                }
                            ],
                            "rationale": "Previously accepted exposure evidence.",
                        }
                    },
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
                    "source": "accepted_posture",
                    "source_record_id": "ASSET-1:CVE-2021-0001:accepted",
                },
                {
                    "source": "kev_reference_reconciliation",
                    "source_record_id": "ASSET-1:CVE-2021-0001",
                },
            ],
            "rationale": payload["items"][0]["rationale"],
            "verdict": "support",
        }
    ]


def test_kev_demo_workflows_run_end_to_end_from_composed_config(tmp_path: Path) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path, str(config_path))

    lock_result = _lock_mutable_kev_fixture(instance)
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
    assert pre_exposure_context.total > 0
    assert any(
        getattr(_include_result(row, "affected_vulnerabilities"), "count") > 0
        for row in pre_exposure_context.items
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
    assert graph.edge_count("control_mitigates_class") > 0
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
    assert any(
        edge["metadata"]["evidence"]["evidence_refs"]
        for edge in graph.list_edges("asset_vulnerability_posture")
    )
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
    assert reference_edge["metadata"]["evidence"]["evidence_refs"]
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
    assert product_edge["metadata"]["evidence"]["evidence_refs"]
    assert "match_score" not in product_edge["properties"]
    assert "match score" not in product_edge["properties"]["match_basis"].lower()
    assert "confidence" not in product_edge["properties"]["match_basis"].lower()
    assert re.search(r"\b\d+\.\d+\b", product_edge["properties"]["match_basis"]) is None
    assert product_edge["metadata"]["evidence"]["rationale"]
    assert "match score" not in product_edge["metadata"]["evidence"]["rationale"].lower()

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

    assert vulnerability_asset_context.total > 0
    assert owner_patch_queue.total > 0
    assert product_asset_context.total > 0

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

    assert vulnerability_class_context.total > 0
    assert control_coverage_gap.total > 0
    assert any(
        getattr(_row_path_segment(row, "mitigated_class"), "properties", {}).get("effect")
        == "blocks"
        for row in control_coverage_gap.items
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
    assert control_mitigation_edge["metadata"]["evidence"]["evidence_refs"] == [
        {
            "source": "control_review",
            "source_record_id": "CTRL-1:path_traversal",
            "metadata": {"observed_at": "2026-04-01"},
        }
    ]


def test_broad_context_queries_include_reviewable_provenance(
    tmp_path: Path,
) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    _lock_mutable_kev_fixture(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")

    graph = instance.load_graph()
    product_to_vendor = {
        edge["from_id"]: edge["to_id"] for edge in graph.list_edges("product_from_vendor")
    }
    vulnerability_product = next(
        edge
        for edge in graph.list_edges("vulnerability_affects_product")
        if edge["to_id"] in product_to_vendor
    )
    cve_id = vulnerability_product["from_id"]
    product_id = vulnerability_product["to_id"]
    vendor_id = product_to_vendor[product_id]

    service_asset = graph.list_edges("service_depends_on_asset")[0]
    asset_id = service_asset["to_id"]
    owner_id = next(
        edge["to_id"] for edge in graph.list_edges("asset_owned_by") if edge["from_id"] == asset_id
    )

    base_posture_properties = {
        "status": "exposed",
        "priority": "high",
        "product_id": product_id,
        "installed_version": "test-version",
        "affected_basis": "Regression test affected basis.",
        "exposure_basis": "Regression test exposure basis.",
        "control_basis": "Regression test control basis.",
    }
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_vulnerability_posture",
            from_type="Asset",
            from_id=asset_id,
            to_type="Vulnerability",
            to_id=cve_id,
            properties={**base_posture_properties, "priority": "medium"},
            metadata=RelationshipMetadata(
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        {
                            "source": "test",
                            "source_record_id": f"{asset_id}:{cve_id}:reviewable",
                        }
                    ],
                    rationale="Regression test reviewable posture.",
                )
            ),
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_vulnerability_posture",
            from_type="Asset",
            from_id=asset_id,
            to_type="Vulnerability",
            to_id=cve_id,
            properties={**base_posture_properties, "priority": "critical"},
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="pending", source="agent")
                ),
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        {
                            "source": "test",
                            "source_record_id": f"{asset_id}:{cve_id}:pending",
                        }
                    ],
                    rationale="Regression test pending posture.",
                ),
            ),
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="vulnerability_classified_as",
            from_type="Vulnerability",
            from_id=cve_id,
            to_type="VulnerabilityClass",
            to_id="path_traversal",
            properties={
                "basis": "Regression pending classification.",
                "source": "test",
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="pending", source="agent")
                )
            ),
        )
    )
    instance.save_graph(graph)

    vendor_context = service_query(
        instance,
        "vendor_service_impact",
        {"vendor_id": vendor_id},
    )
    assert vendor_context.relationship_state == "reviewable"
    vendor_rows = [
        row
        for row in vendor_context.items
        if getattr(_row_path_segment(row, "exposure"), "from_id") == asset_id
        and getattr(_row_path_segment(row, "exposure"), "to_id") == cve_id
    ]
    assert {_path_segment_review_status(row, "exposure") for row in vendor_rows} >= {
        "pending",
        "unreviewed",
    }

    owner_queue = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    assert owner_queue.relationship_state == "live"
    assert not _has_exposure_path(owner_queue.items, asset_id, cve_id)

    vulnerability_class_context = service_query(
        instance,
        "vulnerability_class_context",
        {"class_id": "path_traversal"},
    )
    assert vulnerability_class_context.relationship_state == "reviewable"
    assert any(
        getattr(_row_path_segment(row, "classification"), "from_id") == cve_id
        and _path_segment_review_status(row, "classification") == "pending"
        for row in vulnerability_class_context.items
    )

    control_coverage_gap = service_query(
        instance,
        "control_coverage_gap",
        {"control_id": "CTRL-1"},
    )
    assert control_coverage_gap.relationship_state == "reviewable"
    assert any(
        getattr(_row_path_segment(row, "classified_vulnerability"), "from_id") == cve_id
        and _path_segment_review_status(row, "classified_vulnerability") == "pending"
        and getattr(_row_path_segment(row, "exposure"), "from_id") == asset_id
        and _path_segment_review_status(row, "exposure") == "pending"
        for row in control_coverage_gap.items
    )


def test_owner_patch_queue_excludes_remediated_pairs(tmp_path: Path) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    _lock_mutable_kev_fixture(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    _approve_workflow_group(instance, "propose_asset_products")
    _approve_workflow_group(instance, "propose_asset_exposure")

    graph = instance.load_graph()
    asset_to_owner = {edge["from_id"]: edge["to_id"] for edge in graph.list_edges("asset_owned_by")}
    assets_with_services = {edge["to_id"] for edge in graph.list_edges("service_depends_on_asset")}
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
    before_ids = _query_entity_ids(before.items)
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
                "verification_basis": "Regression test closure context.",
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                ),
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        {
                            "source": "test",
                            "source_record_id": f"{asset_id}:{cve_id}:remediated",
                        }
                    ],
                    rationale="Regression test closure context.",
                ),
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
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                ),
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        {
                            "source": "test",
                            "source_record_id": f"{asset_id}:{cve_id}:exception",
                        }
                    ],
                    rationale="Regression test scoped exception context.",
                ),
            ),
        )
    )
    instance.save_graph(graph)

    after = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    after_ids = _query_entity_ids(after.items)

    assert cve_id not in after_ids
    assert after.total == before.total - 1

    vendor_context = service_query(instance, "vendor_service_impact", {"vendor_id": vendor_id})
    context_row = next(
        row
        for row in vendor_context.items
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
    _lock_mutable_kev_fixture(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    _approve_workflow_group(instance, "propose_asset_products")
    _approve_workflow_group(instance, "propose_asset_exposure")

    graph = instance.load_graph()
    asset_to_owner = {edge["from_id"]: edge["to_id"] for edge in graph.list_edges("asset_owned_by")}
    assert asset_to_owner
    asset_id, owner_id = sorted(asset_to_owner.items())[0]

    before = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    before_ids = _query_entity_ids(before.items)
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
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                ),
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        {
                            "source": "test",
                            "source_record_id": f"{asset_id}:{candidate_cve}:posture",
                        }
                    ],
                    rationale="Approved posture rows that are not exposed stay out of queues.",
                ),
            ),
        )
    )
    instance.save_graph(graph)

    after = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})

    assert candidate_cve not in _query_entity_ids(after.items)
    assert after.total == before.total


def test_owner_patch_queue_excludes_scoped_exception_pairs(tmp_path: Path) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    _lock_mutable_kev_fixture(instance)

    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    _approve_workflow_group(instance, "propose_asset_products")
    _approve_workflow_group(instance, "propose_asset_exposure")

    graph = instance.load_graph()
    asset_to_owner = {edge["from_id"]: edge["to_id"] for edge in graph.list_edges("asset_owned_by")}
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
    before_ids = _query_entity_ids(before.items)
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
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                ),
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        {
                            "source": "test",
                            "source_record_id": f"{asset_id}:{cve_id}:exception",
                        }
                    ],
                    rationale="Approved scoped exception suppresses patch queue action.",
                ),
            ),
        )
    )
    instance.save_graph(graph)

    after = service_query(instance, "owner_patch_queue", {"owner_id": owner_id})
    after_ids = _query_entity_ids(after.items)

    assert cve_id not in after_ids
    assert after.total == before.total - 1


def test_exposure_reconciliation_no_candidates_completes_without_group(
    tmp_path: Path,
) -> None:
    config_path = _composed_kev_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path / "instance", str(config_path))
    _lock_mutable_kev_fixture(instance)

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
        groups = group_store.list_groups(relationship_type="asset_remediated_vulnerability")
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
    service_publish_state(
        reference,
        transport_ref=f"file://{release_dir}",
        state_id="kev-reference",
        release_id="2026-03-31",
        compatibility="data_only",
    )

    overlay_root = tmp_path / "overlay"
    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        kit="kev-triage",
        root_dir=overlay_root,
    ).instance

    proposed = service_propose_workflow(overlay, "propose_asset_products", {})
    assert proposed.group_id is not None
    assert proposed.receipt is not None
    assert any(
        node.detail.get("step_id") == "inventory_tables"
        and node.detail.get("provider_name") == "parse_local_seed_bundle"
        for node in proposed.receipt.nodes
    )
    assert any(
        node.detail.get("step_id") == "inventory"
        and node.detail.get("kind") == "shape_items"
        and node.detail.get("output_count", 0) > 0
        for node in proposed.receipt.nodes
    )
