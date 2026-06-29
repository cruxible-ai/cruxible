"""Tests for the KEV release publish orchestration script."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from cruxible_core.graph.entity_graph import EntityGraph


def test_build_publish_refs_for_oci_and_file() -> None:
    module = _load_publish_script()

    oci_refs = module.build_publish_refs(
        transport_ref="oci://ghcr.io/cruxible-ai/models/kev-reference",
        release_id="2026-03-27",
    )
    assert oci_refs.immutable_ref == "oci://ghcr.io/cruxible-ai/models/kev-reference:2026-03-27"
    assert oci_refs.latest_ref == "oci://ghcr.io/cruxible-ai/models/kev-reference:latest"

    file_refs = module.build_publish_refs(
        transport_ref="file:///tmp/kev-release",
        release_id="2026-03-27",
    )
    assert file_refs.immutable_ref == "file:///tmp/kev-release/2026-03-27"
    assert file_refs.latest_ref == "file:///tmp/kev-release/latest"


def test_build_publish_refs_rejects_tagged_oci_ref() -> None:
    module = _load_publish_script()

    with pytest.raises(Exception, match="must not already include a tag or digest"):
        module.build_publish_refs(
            transport_ref="oci://ghcr.io/cruxible-ai/models/kev-reference:latest",
            release_id="2026-03-27",
        )


def test_write_temp_kev_config_updates_artifact_sha(tmp_path: Path) -> None:
    module = _load_publish_script()
    source = Path("kits/kev-reference/config.yaml")
    output = tmp_path / "config.yaml"

    module.write_temp_kev_config(
        source_path=source,
        output_path=output,
        artifact_digest="sha256:test-artifact",
    )

    loaded = module.yaml.safe_load(output.read_text(encoding="utf-8"))
    assert loaded["artifacts"]["public_kev_bundle"]["digest"] == "sha256:test-artifact"
    assert loaded["workflows"]["build_public_kev_reference"]["type"] == "canonical"


def test_publish_script_uses_split_reference_kit() -> None:
    script_text = Path("scripts/publish_kev_release.py").read_text(encoding="utf-8")
    assert '"kev-reference"' in script_text
    assert '"kev-triage" / "kev-reference.yaml"' not in script_text


def test_publish_kev_release_file_transport_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_publish_script()
    releases_dir = tmp_path / "releases"

    def fake_download_text(url: str) -> str:
        if url == module.CISA_KEV_URL:
            return (
                "cveID,vendorProject,product,shortDescription,dueDate,knownRansomwareCampaignUse\n"
                "CVE-2026-0001,Acme,Widget,KEV description,2026-04-01,Known\n"
            )
        if url == module.EPSS_KEV_URL:
            return (
                "CVE,Vendor,Product,Description,CVSS3,EPSS\n"
                "CVE-2026-0001,Acme,Widget,EPSS description,9.8,0.95\n"
            )
        raise AssertionError(f"Unexpected URL: {url}")

    def fake_nvd_fetcher():
        return lambda _api_key: [
            {
                "cve": {
                    "id": "CVE-2026-0001",
                    "lastModified": "2026-03-01T12:00:00.000",
                    "vulnStatus": "Analyzed",
                    "cisaRequiredAction": "Apply updates per vendor instructions.",
                    "cisaVulnerabilityName": "Acme Widget Remote Code Execution Vulnerability",
                    "weaknesses": [
                        {
                            "description": [
                                {"lang": "en", "value": "CWE-94"},
                            ]
                        }
                    ],
                    "metrics": {
                        "cvssMetricV31": [
                            {
                                "cvssData": {"baseScore": 9.8},
                                "baseSeverity": "CRITICAL",
                            }
                        ]
                    },
                    "configurations": [
                        {
                            "nodes": [
                                {
                                    "cpeMatch": [
                                        {
                                            "vulnerable": True,
                                            "criteria": "cpe:2.3:a:acme:widget:*:*:*:*:*:*:*:*",
                                            "matchCriteriaId": "MATCH-1",
                                            "versionEndExcluding": "2.0.0",
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                }
            }
        ]

    monkeypatch.setattr(module, "download_text", fake_download_text)
    monkeypatch.setattr(module, "load_nvd_fetcher", fake_nvd_fetcher)
    monkeypatch.setattr(module, "MIN_KEV_ROWS", 0)
    monkeypatch.setattr(module, "MIN_EPSS_ROWS", 0)
    monkeypatch.setattr(module, "MIN_NVD_ENTRIES", 0)

    result = module.publish_kev_release(
        transport_ref=f"file://{releases_dir}",
        release_id="2026-03-27",
    )

    immutable_dir = releases_dir / "2026-03-27"
    latest_dir = releases_dir / "latest"
    assert immutable_dir.exists()
    assert latest_dir.exists()
    assert result.immutable_ref == f"file://{immutable_dir}"
    assert result.latest_ref == f"file://{latest_dir}"

    immutable_manifest = json.loads((immutable_dir / "manifest.json").read_text(encoding="utf-8"))
    latest_manifest = json.loads((latest_dir / "manifest.json").read_text(encoding="utf-8"))
    assert immutable_manifest == latest_manifest
    assert immutable_manifest["release_id"] == "2026-03-27"

    graph_payload = json.loads((immutable_dir / "graph.json").read_text(encoding="utf-8"))
    graph = EntityGraph.from_dict(graph_payload)
    assert graph.entity_count() == 3
    assert graph.edge_count() == 2
    vulnerability = graph.get_entity("Vulnerability", "CVE-2026-0001")
    assert vulnerability is not None
    assert vulnerability.properties["vulnerability_name"] == (
        "Acme Widget Remote Code Execution Vulnerability"
    )
    assert vulnerability.properties["cvss_severity"] == "CRITICAL"
    assert vulnerability.properties["epss_percentile"] is None
    assert vulnerability.properties["cwes"] == ["CWE-94"]
    edge = graph.list_edges("vulnerability_affects_product")[0]
    assert edge["properties"]["source"] == "nvd"
    assert edge["properties"]["vulnerable"] is True
    assert edge["properties"]["affected_versions"]
    assert edge["properties"]["source_last_modified_at"].startswith("2026-03-01T12:00:00")
    evidence_refs = edge["metadata"]["evidence"]["evidence_refs"]
    assert evidence_refs[0]["metadata"]["match_criteria_id"] == "MATCH-1"


def _load_publish_script() -> ModuleType:
    path = Path("scripts/publish_kev_release.py")
    spec = importlib.util.spec_from_file_location("publish_kev_release", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
