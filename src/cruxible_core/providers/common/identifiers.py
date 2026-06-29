"""Common providers for mechanical identifier normalization."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from cruxible_core.provider.types import ProviderContext


def normalize_identifiers(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Normalize configured identifier fields without domain interpretation."""
    items = _object_list(input_payload.get("items"), "items")
    field_types = _field_type_map(input_payload.get("fields"))
    output_suffix = str(input_payload.get("output_suffix", "_normalized"))

    normalized_items: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        normalized = dict(item)
        for field, kind in field_types.items():
            raw_value = item.get(field)
            value, diagnostic = _normalize_value(raw_value, kind)
            normalized[f"{field}{output_suffix}"] = value
            if diagnostic is not None:
                diagnostics.append({"row_index": index, "field": field, **diagnostic})
        normalized_items.append(normalized)

    return {
        "items": normalized_items,
        "diagnostics": diagnostics,
        "summary": {"items": len(items), "fields": len(field_types)},
    }


def _normalize_value(value: Any, kind: str) -> tuple[str | None, dict[str, Any] | None]:
    if value is None:
        return None, {"level": "warning", "code": "missing_value"}
    text = str(value).strip()
    if not text:
        return None, {"level": "warning", "code": "blank_value"}

    if kind == "cve":
        match = re.search(r"cve[-_ ]?(\d{4})[-_ ]?(\d{4,})", text, re.IGNORECASE)
        if match:
            return f"CVE-{match.group(1)}-{match.group(2)}", None
        return None, {"level": "warning", "code": "invalid_cve", "value": text}

    if kind in {"gtin", "upc", "ean"}:
        digits = re.sub(r"\D+", "", text)
        if digits:
            return digits, None
        return None, {"level": "warning", "code": "invalid_numeric_identifier", "value": text}

    if kind == "sku":
        return re.sub(r"\s+", "-", text.upper()), None

    if kind == "slug":
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug or None, None if slug else {"level": "warning", "code": "invalid_slug"}

    if kind == "date":
        normalized = _normalize_date(text)
        if normalized is not None:
            return normalized, None
        return None, {"level": "warning", "code": "invalid_date", "value": text}

    if kind == "cpe":
        return text.lower(), None

    if kind == "upper":
        return text.upper(), None

    if kind == "lower":
        return text.lower(), None

    raise ValueError(f"Unsupported identifier normalization kind: {kind}")


def _normalize_date(text: str) -> str | None:
    candidates = [
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y/%m/%d",
        "%B %d, %Y",
        "%b %d, %Y",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


def _object_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field_name} must be a list of objects")
    return value


def _field_type_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("fields must be a string map of field name to normalization kind")
    return value
