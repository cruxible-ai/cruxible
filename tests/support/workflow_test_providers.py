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


def typed_error_provider(
    _input_payload: dict[str, Any], _context: ProviderContext
) -> dict[str, Any]:
    """Raise a typed Cruxible error to exercise provider error subtype preservation."""
    from cruxible_core.errors import DataValidationError

    raise DataValidationError("provider rejected malformed upstream data")


def reference_bundle_loader(
    _input_payload: dict[str, Any], context: ProviderContext
) -> dict[str, Any]:
    """Load canonical rows from a directory artifact bundle."""
    if context.artifact is None or context.artifact.local_path is None:
        raise ValueError("reference_bundle_loader requires a local artifact bundle")
    bundle_root = Path(context.artifact.local_path)
    rows_path = bundle_root / "rows.json"
    return {"items": json.loads(rows_path.read_text())}


def emit_entity_set(input_payload: dict[str, Any], _context: ProviderContext) -> dict[str, Any]:
    """Emit a workflow ``EntitySet`` directly, without a ``make_entities`` step.

    Mirrors a provider whose ``contract_out`` is an entity-upsert artifact that an
    ``apply_entities`` step then commits. This is the write-policy bypass surface:
    a provider-emitted ``EntitySet`` never passes through the config-time
    ``make_entities`` mint_only validator, so only the runtime chokepoint can
    refuse it. Entity type / id / property payload are taken from the workflow
    input so one provider can drive the mint_only, direct, and proposal_only
    cases through a single canonical workflow. Tests may also pass a raw
    ``entities`` list to exercise provider-emitted duplicate ids.
    """
    entity_type = input_payload["entity_type"]
    entities_payload = input_payload.get("entities")
    if isinstance(entities_payload, list) and entities_payload:
        return {"entity_type": entity_type, "entities": entities_payload}

    entity_id = input_payload["entity_id"]
    properties = dict(input_payload.get("properties") or {})
    if properties:
        properties.setdefault("id", entity_id)
    else:
        properties = {"id": entity_id, "label": input_payload.get("label", "x")}
    return {
        "entity_type": entity_type,
        "entities": [
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "properties": properties,
            }
        ],
    }
