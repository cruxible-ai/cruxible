"""Local KEV seed data providers."""

from __future__ import annotations

from typing import Any

from cruxible_core.provider.types import ProviderContext

from .common import (
    _load_csv_rows,
    _parse_bool,
    _parsed_table_rows,
    _require_artifact_root,
)

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
    tables = {
        table_name: _parsed_table_rows(input_payload, table_name)
        for table_name in _LOCAL_SEED_FILES
    }
    return _build_local_seed_data(tables)


def _build_local_seed_data(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    payload = {key: [dict(row) for row in rows] for key, rows in tables.items()}
    for row in payload["assets"]:
        row["internet_exposed"] = _parse_bool(row.get("internet_exposed"))
    return payload


def load_software_inventory(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load raw software inventory rows from the seed bundle."""
    bundle_root = _require_artifact_root(context, "load_software_inventory")
    return {"items": _load_csv_rows(bundle_root / "software_inventory.csv")}
