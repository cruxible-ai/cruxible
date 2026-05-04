"""Shared helpers for KEV triage kit providers."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from cruxible_core.provider.types import ProviderContext


def _require_artifact_root(context: ProviderContext, provider_name: str) -> Path:
    if context.artifact is None or context.artifact.local_path is None:
        raise ValueError(f"{provider_name} requires a local artifact bundle")
    return Path(context.artifact.local_path)


def _require_items(input_payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = input_payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Expected '{key}' to be a list of objects")
    return value


def _parsed_table_rows(input_payload: dict[str, Any], table_name: str) -> list[dict[str, Any]]:
    tables = input_payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("Expected input.tables to be a mapping")
    table = tables.get(table_name)
    if not isinstance(table, dict):
        raise ValueError(f"Expected parsed table '{table_name}'")
    rows = table.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"Expected parsed table '{table_name}' to contain rows")
    return [_strip_provider_metadata(row) for row in rows if isinstance(row, dict)]


def _strip_provider_metadata(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


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


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _parse_float(value: Any) -> float | None:
    text = _first_non_empty(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _slugify(value: str) -> str:
    normalized = value.lower().replace("+", "plus")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug or "unknown"


def _humanize(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def _edge_from_id(edge: dict[str, Any]) -> str:
    return _first_non_empty(edge.get("from_id")) or ""


def _edge_to_id(edge: dict[str, Any]) -> str:
    return _first_non_empty(edge.get("to_id")) or ""


def _edge_properties(edge: dict[str, Any]) -> dict[str, Any]:
    properties = edge.get("properties")
    return properties if isinstance(properties, dict) else {}


def _entity_id(entity: dict[str, Any]) -> str:
    return _first_non_empty(entity.get("entity_id")) or ""


def _entity_properties(entity: dict[str, Any]) -> dict[str, Any]:
    properties = entity.get("properties")
    return properties if isinstance(properties, dict) else {}


def _verdict_rank(verdict: str) -> int:
    return {"support": 2, "unsure": 1, "contradict": 0}.get(verdict, -1)
