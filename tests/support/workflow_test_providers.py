"""Provider callables used by workflow tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cruxible_core.provider.types import ProviderContext


def lift_predictor(input_payload: dict[str, Any], context: ProviderContext) -> dict[str, Any]:
    """Return a deterministic forecast payload."""
    base = 0.10 if context.deterministic else 0.08
    return {
        "predicted_lift_pct": round(base + 0.01 * len(input_payload.get("sku", "")), 4),
        "confidence_lower": 0.05,
        "confidence_upper": 0.25,
        "model_version": context.provider_version,
    }


def margin_calculator(input_payload: dict[str, Any], context: ProviderContext) -> dict[str, Any]:
    """Convert lift into a simple expected margin result."""
    lift = float(input_payload["predicted_lift_pct"])
    return {
        "expected_margin_pct": round(lift / 2, 4),
        "decision": "approve" if lift >= 0.10 else "review",
        "calculator_version": context.provider_version,
    }


def campaign_recommendations(
    input_payload: dict[str, Any], _context: ProviderContext
) -> dict[str, Any]:
    """Return deterministic raw recommendation rows for declarative proposal assembly."""
    region = input_payload["region"]
    return {
        "items": [
            {
                "product_sku": "SKU-123",
                "verdict": "match",
                "reason": f"{region} bestseller",
            },
            {
                "product_sku": "SKU-456",
                "verdict": "fallback",
                "reason": f"{region} fallback",
            },
        ]
    }


def duplicate_campaign_recommendations(
    input_payload: dict[str, Any], _context: ProviderContext
) -> dict[str, Any]:
    """Return recommendations with a duplicate candidate pair for diagnostics tests."""
    region = input_payload["region"]
    return {
        "items": [
            {
                "product_sku": "SKU-123",
                "verdict": "match",
                "reason": f"{region} bestseller",
            },
            {
                "product_sku": "SKU-123",
                "verdict": "match",
                "reason": f"{region} duplicate rationale",
            },
            {
                "product_sku": "SKU-456",
                "verdict": "fallback",
                "reason": f"{region} fallback",
            },
        ]
    }


def echo_json_payload(input_payload: dict[str, Any], _context: ProviderContext) -> dict[str, Any]:
    """Echo nested JSON test payload items for contract validation tests."""
    payload = input_payload.get("payload", {})
    if not isinstance(payload, dict):
        return {"items": []}
    return {"items": payload.get("items", [])}


def broken_provider(_input_payload: dict[str, Any], _context: ProviderContext) -> dict[str, Any]:
    """Return an invalid output shape for contract failure tests."""
    return {"unexpected": "value"}


def reference_bundle_loader(
    _input_payload: dict[str, Any], context: ProviderContext
) -> dict[str, Any]:
    """Load canonical rows from a directory artifact bundle."""
    if context.artifact is None or context.artifact.local_path is None:
        raise ValueError("reference_bundle_loader requires a local artifact bundle")
    bundle_root = Path(context.artifact.local_path)
    rows_path = bundle_root / "rows.json"
    return {"items": json.loads(rows_path.read_text())}
