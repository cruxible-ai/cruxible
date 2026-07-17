"""Tests for bundled common tabular providers."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.provider.types import ProviderContext, ResolvedArtifact
from cruxible_core.providers.common.tabular import load_tabular_artifact_bundle, source_diff


def _provider_context(path: Path) -> ProviderContext:
    return ProviderContext(
        workflow_name="wf",
        step_id="step",
        provider_name="provider",
        provider_version="1.0.0",
        artifact=ResolvedArtifact(
            name="bundle",
            kind="directory",
            uri=str(path),
            local_path=str(path),
            digest="sha256:test",
        ),
    )


def test_load_tabular_artifact_bundle_reads_csv_and_jsonl_with_provenance(
    tmp_path: Path,
) -> None:
    (tmp_path / "assets.csv").write_text("Asset ID,Host Name\nasset-1,api-01\nasset-2,api-02\n")
    (tmp_path / "software_inventory.jsonl").write_text(
        '{"Asset ID":"asset-1","Software Name":"Apache HTTP Server"}\n'
        '{"Asset ID":"asset-2","Software Name":"Google Chrome"}\n'
    )

    payload = load_tabular_artifact_bundle(
        {"expected_tables": ["assets", "software_inventory", "owners"]},
        _provider_context(tmp_path),
    )

    assert sorted(payload["tables"]) == ["assets", "software_inventory"]
    assert payload["tables"]["assets"]["columns"] == ["asset_id", "host_name"]
    first_asset = payload["tables"]["assets"]["rows"][0]
    assert first_asset["asset_id"] == "asset-1"
    assert first_asset["_source_file"] == "assets.csv"
    assert first_asset["_source_row"] == 2
    assert first_asset["_row_hash"].startswith("sha256:")
    assert payload["tables"]["software_inventory"]["rows"][0]["software_name"] == (
        "Apache HTTP Server"
    )
    assert payload["diagnostics"] == [
        {
            "level": "warning",
            "code": "missing_expected_table",
            "table": "owners",
            "message": "Expected table 'owners' was not found",
        }
    ]


def test_load_tabular_artifact_bundle_supports_table_name_overrides(tmp_path: Path) -> None:
    (tmp_path / "CMDB Assets.csv").write_text("Asset ID\nasset-1\n")

    payload = load_tabular_artifact_bundle(
        {"table_names": {"CMDB Assets.csv": "assets"}},
        _provider_context(tmp_path),
    )

    assert list(payload["tables"]) == ["assets"]


def test_source_diff_reports_added_changed_removed_rows() -> None:
    previous = {
        "tables": {
            "assets": {
                "rows": [
                    {"asset_id": "a-1", "hostname": "api-01", "owner": "sec"},
                    {"asset_id": "a-2", "hostname": "api-02", "owner": "app"},
                ]
            }
        }
    }
    current = {
        "tables": {
            "assets": {
                "rows": [
                    {"asset_id": "a-1", "hostname": "api-01", "owner": "platform"},
                    {"asset_id": "a-3", "hostname": "api-03", "owner": "sec"},
                ]
            }
        }
    }

    payload = source_diff(
        {
            "previous": previous,
            "current": current,
            "key_fields": {"assets": ["asset_id"]},
        },
        ProviderContext(
            workflow_name="wf",
            step_id="step",
            provider_name="provider",
            provider_version="1.0.0",
        ),
    )

    assets = payload["tables"]["assets"]
    assert assets["counts"] == {"added": 1, "changed": 1, "removed": 1, "unchanged": 0}
    assert assets["added"][0]["asset_id"] == "a-3"
    assert assets["removed"][0]["asset_id"] == "a-2"
    assert assets["changed"][0]["key"] == {"asset_id": "a-1"}
    assert assets["changed"][0]["changed_fields"] == ["owner"]
    assert payload["summary"] == {
        "tables": 1,
        "added": 1,
        "changed": 1,
        "removed": 1,
        "unchanged": 0,
    }


def test_load_tabular_artifact_bundle_infers_schema_past_leading_nulls(tmp_path: Path) -> None:
    """Optional columns null beyond polars' default 100-row window still load."""
    import json

    rows = [{"id": f"row-{i:04d}", "extra": None} for i in range(150)]
    rows.append({"id": "row-0150", "extra": '{"nested": 1}'})
    (tmp_path / "sparse.json").write_text(json.dumps(rows))

    payload = load_tabular_artifact_bundle(
        {"expected_tables": ["sparse"]},
        _provider_context(tmp_path),
    )

    table = payload["tables"]["sparse"]
    assert len(table["rows"]) == 151
    assert table["rows"][-1]["extra"] == '{"nested": 1}'
    assert table["rows"][0]["extra"] is None
