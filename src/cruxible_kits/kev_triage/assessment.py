"""Governed KEV impact assessment providers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from cruxible_core.provider.types import ProviderContext

from .common import (
    _edge_from_id,
    _edge_properties,
    _edge_to_id,
    _entity_id,
    _entity_properties,
    _first_non_empty,
    _require_items,
    _verdict_rank,
)
from .versioning import _assess_version_membership


def assess_asset_affected(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Join approved asset-product edges to vulnerability-product edges."""
    asset_product_edges = _require_items(input_payload, "asset_product_edges")
    vulnerability_product_edges = _require_items(input_payload, "vulnerability_product_edges")

    vulnerability_edges_by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in vulnerability_product_edges:
        product_id = _edge_to_id(edge)
        if product_id:
            vulnerability_edges_by_product[product_id].append(edge)

    rows_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in asset_product_edges:
        asset_id = _edge_from_id(edge)
        product_id = _edge_to_id(edge)
        properties = _edge_properties(edge)
        if not asset_id or not product_id:
            continue

        installed_version = _first_non_empty(properties.get("installed_version")) or ""
        source = _first_non_empty(properties.get("evidence_source")) or "asset_runs_product"
        for vulnerability_edge in vulnerability_edges_by_product.get(product_id, []):
            cve_id = _edge_from_id(vulnerability_edge)
            if not cve_id:
                continue
            vulnerability_properties = _edge_properties(vulnerability_edge)
            verdict, rationale = _assess_version_membership(
                installed_version,
                vulnerability_properties.get("affected_versions"),
                vulnerability_properties.get("fixed_version"),
            )
            if verdict == "contradict":
                continue

            row = {
                "asset_id": asset_id,
                "cve_id": cve_id,
                "product_id": product_id,
                "installed_version": installed_version,
                "source": source,
                "rationale": rationale,
                "verdict": verdict,
            }
            key = (asset_id, cve_id)
            current = rows_by_pair.get(key)
            if current is None or _verdict_rank(verdict) > _verdict_rank(current["verdict"]):
                rows_by_pair[key] = row

    return {"items": [rows_by_pair[key] for key in sorted(rows_by_pair)]}


def assess_asset_exposure(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Assess which affected assets are materially exposed."""
    affected_items = _affected_items(input_payload)
    assets = _require_items(input_payload, "assets")
    asset_control_edges = _require_items(input_payload, "asset_control_edges")
    controls = _require_items(input_payload, "controls")

    assets_by_id = {_entity_id(entity): _entity_properties(entity) for entity in assets}
    controls_by_id = {_entity_id(entity): _entity_properties(entity) for entity in controls}
    active_controls_by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in asset_control_edges:
        asset_id = _edge_from_id(edge)
        control_id = _edge_to_id(edge)
        if not asset_id or not control_id:
            continue
        control = controls_by_id.get(control_id)
        if control is None or _first_non_empty(control.get("status")) != "active":
            continue
        active_controls_by_asset[asset_id].append(control)

    rows_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for item in affected_items:
        asset_id = _first_non_empty(item.get("asset_id")) or _edge_from_id(item)
        cve_id = _first_non_empty(item.get("cve_id")) or _edge_to_id(item)
        if not asset_id or not cve_id:
            continue

        properties = _edge_properties(item)
        asset = assets_by_id.get(asset_id, {})
        active_controls = active_controls_by_asset.get(asset_id, [])
        exploitability_verdict = _derive_exploitability_verdict(asset)
        if exploitability_verdict == "contradict":
            continue

        control_verdict = "support" if not active_controls else "unsure"
        priority = _derive_exposure_priority(asset, exploitability_verdict, control_verdict)
        due_by = _priority_due_by(priority)
        rationale = _build_exposure_rationale(asset, active_controls, exploitability_verdict)
        rows_by_pair[(asset_id, cve_id)] = {
            "asset_id": asset_id,
            "cve_id": cve_id,
            "priority": priority,
            "due_by": due_by,
            "rationale": rationale,
            "product_id": _first_non_empty(item.get("product_id"), properties.get("product_id"))
            or "",
            "installed_version": _first_non_empty(
                item.get("installed_version"),
                properties.get("installed_version"),
            )
            or "",
            "affected_rationale": _first_non_empty(
                item.get("rationale"),
                properties.get("rationale"),
            )
            or "",
            "evidence_source": _first_non_empty(item.get("source"), properties.get("source"))
            or "",
            "affected_verdict": _first_non_empty(item.get("verdict"), properties.get("verdict"))
            or "support",
            "exploitability_verdict": exploitability_verdict,
            "control_verdict": control_verdict,
        }

    return {"items": [rows_by_pair[key] for key in sorted(rows_by_pair)]}


def _affected_items(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = input_payload.get("affected_items")
    if raw_items is None:
        return _require_items(input_payload, "affected_edges")
    if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
        raise ValueError("Expected 'affected_items' to be a list of objects")
    return raw_items


def _derive_exploitability_verdict(asset: dict[str, Any]) -> str:
    internet_exposed = asset.get("internet_exposed")
    environment = _first_non_empty(asset.get("environment")) or ""
    criticality = _first_non_empty(asset.get("criticality")) or ""
    if internet_exposed is True:
        return "support"
    if environment == "production" and criticality in {"critical", "high"}:
        return "unsure"
    if environment == "production":
        return "unsure"
    return "contradict"


def _derive_exposure_priority(
    asset: dict[str, Any],
    exploitability_verdict: str,
    control_verdict: str,
) -> str:
    criticality = _first_non_empty(asset.get("criticality")) or ""
    if exploitability_verdict == "support" and control_verdict == "support":
        return "urgent" if criticality == "critical" else "high"
    if criticality in {"critical", "high"}:
        return "high"
    return "medium"


def _priority_due_by(priority: str) -> str:
    return {
        "urgent": "24h",
        "high": "72h",
        "medium": "7d",
    }.get(priority, "14d")


def _build_exposure_rationale(
    asset: dict[str, Any],
    active_controls: list[dict[str, Any]],
    exploitability_verdict: str,
) -> str:
    hostname = _first_non_empty(asset.get("hostname")) or "asset"
    environment = _first_non_empty(asset.get("environment")) or "unknown"
    exposure_clause = (
        "internet-facing"
        if asset.get("internet_exposed") is True
        else f"{environment} asset with {exploitability_verdict} exploitability"
    )
    if not active_controls:
        return f"{hostname} is {exposure_clause} and has no active compensating controls"
    control_names = ", ".join(
        sorted(
            _first_non_empty(control.get("name")) or "unknown control"
            for control in active_controls
        )
    )
    return f"{hostname} is {exposure_clause}; active controls require review: {control_names}"
