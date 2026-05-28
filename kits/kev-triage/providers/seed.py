"""Local KEV seed data providers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from cruxible_core.provider.payloads import JsonItems, ParsedTabularBundle
from cruxible_core.provider.types import ProviderContext

_LOCAL_SEED_FILES = {
    "assets": "assets.csv",
    "business_services": "business_services.csv",
    "owners": "owners.csv",
    "compensating_controls": "compensating_controls.csv",
    "vulnerability_classes": "vulnerability_classes.csv",
    "exceptions": "exceptions.csv",
    "patch_windows": "patch_windows.csv",
    "service_depends_on_asset": "service_depends_on_asset.csv",
    "asset_owned_by": "asset_owned_by.csv",
    "asset_has_control": "asset_has_control.csv",
    "asset_has_exception": "asset_has_exception.csv",
    "asset_patch_window": "asset_patch_window.csv",
    "control_mitigates_class": "control_mitigates_class.csv",
}


def load_local_seed_data(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load deterministic local entity and relationship rows from the seed bundle."""
    bundle_root = _require_artifact_root(context, "load_local_seed_data")
    tables = {
        key: _load_csv_rows(bundle_root / filename)
        for key, filename in _LOCAL_SEED_FILES.items()
    }
    return _build_local_seed_data(tables)


def normalize_local_seed_tables(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Normalize parsed local seed tables into deterministic internal graph rows."""
    bundle = ParsedTabularBundle.from_payload(input_payload)
    tables = {
        table_name: _strip_provider_rows(bundle.require_table(table_name))
        for table_name in _LOCAL_SEED_FILES
    }
    return _build_local_seed_data(tables)


def _build_local_seed_data(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    payload = {key: [dict(row) for row in rows] for key, rows in tables.items()}
    for row in payload["assets"]:
        row["internet_exposed"] = _parse_bool(row.get("internet_exposed"))
    for row in payload["patch_windows"]:
        for field in (
            "emergency_patch_allowed",
            "outage_allowed",
            "testing_required",
            "rollback_required",
        ):
            row[field] = _parse_bool(row.get(field))
    for row in payload["control_mitigates_class"]:
        row["evidence_refs"] = _parse_json_list(row.get("evidence_refs"))
    return payload


def load_software_inventory(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load raw software inventory rows from the seed bundle."""
    bundle_root = _require_artifact_root(context, "load_software_inventory")
    return JsonItems(items=_load_csv_rows(bundle_root / "software_inventory.csv")).to_payload()


def _require_artifact_root(context: ProviderContext, provider_name: str) -> Path:
    if context.artifact is None or context.artifact.local_path is None:
        raise ValueError(f"{provider_name} requires a local artifact bundle")
    return Path(context.artifact.local_path)


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _strip_provider_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in rows
    ]


def _parse_bool(value: Any) -> bool | None:
    text = _first_non_empty(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def _parse_json_list(value: Any) -> list[Any]:
    text = _first_non_empty(value)
    if text is None:
        return []
    parsed = json.loads(text)
    if isinstance(parsed, list):
        return parsed
    return []


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
