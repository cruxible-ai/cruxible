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
        evidence_refs = _evidence_refs(properties)
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
                "evidence_refs": _merge_evidence_refs(
                    evidence_refs,
                    _evidence_refs(vulnerability_properties),
                ),
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
    classification_edges = _optional_items(input_payload, "vulnerability_classification_edges")
    control_mitigation_edges = _optional_items(input_payload, "control_mitigation_edges")

    assets_by_id = {_entity_id(entity): _entity_properties(entity) for entity in assets}
    controls_by_id = {_entity_id(entity): _entity_properties(entity) for entity in controls}
    classes_by_vulnerability = _classes_by_vulnerability(classification_edges)
    mitigations_by_control_class = _mitigations_by_control_class(control_mitigation_edges)
    active_controls_by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in asset_control_edges:
        asset_id = _edge_from_id(edge)
        control_id = _edge_to_id(edge)
        if not asset_id or not control_id:
            continue
        control = controls_by_id.get(control_id)
        if control is None or _first_non_empty(control.get("status")) != "active":
            continue
        active_controls_by_asset[asset_id].append({"control_id": control_id, **control})

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

        control_assessment = _assess_class_aware_controls(
            active_controls,
            classes_by_vulnerability.get(cve_id, []),
            mitigations_by_control_class,
        )
        control_verdict = str(control_assessment["verdict"])
        status = str(control_assessment["status"])
        priority = _derive_exposure_priority(asset, exploitability_verdict, control_verdict)
        priority = _adjust_priority_for_control_effect(priority, control_assessment)
        rationale = _build_exposure_rationale(
            asset,
            active_controls,
            exploitability_verdict,
            control_assessment,
        )
        affected_basis = _first_non_empty(
            item.get("rationale"),
            properties.get("rationale"),
            properties.get("affected_basis"),
        ) or ""
        exposure_basis = rationale
        rows_by_pair[(asset_id, cve_id)] = {
            "asset_id": asset_id,
            "cve_id": cve_id,
            "status": status,
            "priority": priority,
            "rationale": rationale,
            "product_id": _first_non_empty(item.get("product_id"), properties.get("product_id"))
            or "",
            "installed_version": _first_non_empty(
                item.get("installed_version"),
                properties.get("installed_version"),
            )
            or "",
            "affected_basis": affected_basis,
            "affected_rationale": affected_basis,
            "exposure_basis": exposure_basis,
            "control_basis": str(control_assessment["basis"]),
            "evidence_source": _first_non_empty(item.get("source"), properties.get("source"))
            or "",
            "evidence_refs": _merge_evidence_refs(
                _evidence_refs(item),
                _evidence_refs(properties),
            ),
            "affected_verdict": _first_non_empty(item.get("verdict"), properties.get("verdict"))
            or "support",
            "exploitability_verdict": exploitability_verdict,
            "control_verdict": control_verdict,
            "control_exposure_verdict": str(control_assessment["exposure_verdict"]),
            "control_effect": str(control_assessment["effect"]),
        }

    return {"items": [rows_by_pair[key] for key in sorted(rows_by_pair)]}


def assess_exposure_reconciliation(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Find accepted exposure edges no longer supported by product-derived evidence."""
    accepted_exposure_edges = _require_items(input_payload, "accepted_exposure_edges")
    affected_items = _require_items(input_payload, "affected_items")
    asset_product_edges = _require_items(input_payload, "asset_product_edges")
    vulnerability_product_edges = _require_items(input_payload, "vulnerability_product_edges")
    remediated_edges = _require_items(input_payload, "remediated_edges")

    current_affected_pairs = {
        (asset_id, cve_id)
        for item in affected_items
        if (asset_id := _first_non_empty(item.get("asset_id")))
        and (cve_id := _first_non_empty(item.get("cve_id")))
    }
    asset_product_pairs = {
        (_edge_from_id(edge), _edge_to_id(edge))
        for edge in asset_product_edges
        if _edge_from_id(edge) and _edge_to_id(edge)
    }
    vulnerability_product_pairs = {
        (_edge_from_id(edge), _edge_to_id(edge))
        for edge in vulnerability_product_edges
        if _edge_from_id(edge) and _edge_to_id(edge)
    }
    remediated_pairs = {
        (_edge_from_id(edge), _edge_to_id(edge))
        for edge in remediated_edges
        if _edge_from_id(edge) and _edge_to_id(edge)
    }

    items: list[dict[str, Any]] = []
    for edge in accepted_exposure_edges:
        asset_id = _edge_from_id(edge)
        cve_id = _edge_to_id(edge)
        if not asset_id or not cve_id:
            continue
        pair = (asset_id, cve_id)
        if pair in current_affected_pairs or pair in remediated_pairs:
            continue

        properties = _edge_properties(edge)
        if _first_non_empty(properties.get("status")) not in {None, "", "exposed"}:
            continue
        product_id = _first_non_empty(properties.get("product_id")) or ""
        remediation_type = _stale_exposure_remediation_type(
            asset_id,
            cve_id,
            product_id,
            asset_product_pairs,
            vulnerability_product_pairs,
        )
        items.append(
            {
                "asset_id": asset_id,
                "cve_id": cve_id,
                "remediation_type": remediation_type,
                "evidence_source": "kev_reference_reconciliation",
                "evidence_refs": _merge_evidence_refs(
                    _evidence_refs(properties),
                    [
                        {
                            "source": "kev_reference_reconciliation",
                            "source_record_id": f"{asset_id}:{cve_id}",
                        }
                    ],
                ),
                "rationale": _stale_exposure_rationale(
                    asset_id,
                    cve_id,
                    product_id,
                    remediation_type,
                ),
                "verdict": "support",
            }
        )

    return {"items": sorted(items, key=lambda item: (item["asset_id"], item["cve_id"]))}


def _affected_items(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = input_payload.get("affected_items")
    if raw_items is None:
        return _require_items(input_payload, "affected_edges")
    if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
        raise ValueError("Expected 'affected_items' to be a list of objects")
    return raw_items


def _optional_items(input_payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw_items = input_payload.get(key, [])
    if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
        raise ValueError(f"Expected '{key}' to be a list of objects")
    return raw_items


def _classes_by_vulnerability(
    classification_edges: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    classes_by_vulnerability: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in classification_edges:
        cve_id = _edge_from_id(edge)
        class_id = _edge_to_id(edge)
        if not cve_id or not class_id:
            continue
        classes_by_vulnerability[cve_id].append(
            {
                "class_id": class_id,
                "properties": _edge_properties(edge),
            }
        )
    return {
        cve_id: sorted(classes, key=lambda item: str(item["class_id"]))
        for cve_id, classes in classes_by_vulnerability.items()
    }


def _mitigations_by_control_class(
    control_mitigation_edges: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    mitigations_by_control_class: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in control_mitigation_edges:
        control_id = _edge_from_id(edge)
        class_id = _edge_to_id(edge)
        if not control_id or not class_id:
            continue
        properties = _edge_properties(edge)
        effect = _first_non_empty(properties.get("effect")) or ""
        mitigations_by_control_class[(control_id, class_id)].append(
            {
                "control_id": control_id,
                "class_id": class_id,
                "effect": effect,
                "validation_basis": _first_non_empty(properties.get("validation_basis")) or "",
            }
        )
    return {
        key: sorted(items, key=lambda item: (str(item["effect"]), str(item["class_id"])))
        for key, items in mitigations_by_control_class.items()
    }


def _stale_exposure_remediation_type(
    asset_id: str,
    cve_id: str,
    product_id: str,
    asset_product_pairs: set[tuple[str, str]],
    vulnerability_product_pairs: set[tuple[str, str]],
) -> str:
    if product_id and (asset_id, product_id) not in asset_product_pairs:
        return "product_mapping_changed"
    if product_id and (cve_id, product_id) not in vulnerability_product_pairs:
        return "reference_changed"
    return "not_affected"


def _stale_exposure_rationale(
    asset_id: str,
    cve_id: str,
    product_id: str,
    remediation_type: str,
) -> str:
    product_clause = f" via product {product_id}" if product_id else ""
    if remediation_type == "product_mapping_changed":
        return (
            f"{asset_id}->{cve_id}{product_clause} is no longer supported by an "
            "accepted asset_runs_product edge"
        )
    if remediation_type == "reference_changed":
        return (
            f"{asset_id}->{cve_id}{product_clause} is no longer supported by the "
            "reference vulnerability_affects_product edge"
        )
    return (
        f"{asset_id}->{cve_id}{product_clause} is no longer in the current "
        "product-version affected candidate set"
    )


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
        return "critical" if criticality == "critical" else "high"
    if criticality in {"critical", "high"}:
        return "high"
    return "medium"


def _assess_class_aware_controls(
    active_controls: list[dict[str, Any]],
    vulnerability_classes: list[dict[str, Any]],
    mitigations_by_control_class: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    if not active_controls:
        return {
            "status": "exposed",
            "verdict": "support",
            "exposure_verdict": "support",
            "effect": "",
            "basis": "No active compensating controls were attached to this asset.",
            "matches": [],
        }

    matches: list[dict[str, Any]] = []
    for control in active_controls:
        control_id = str(control.get("control_id", ""))
        if not control_id:
            continue
        control_name = _first_non_empty(control.get("name")) or control_id
        for vulnerability_class in vulnerability_classes:
            class_id = str(vulnerability_class["class_id"])
            for mitigation in mitigations_by_control_class.get((control_id, class_id), []):
                matches.append(
                    {
                        **mitigation,
                        "control_name": control_name,
                    }
                )

    matches = sorted(
        matches,
        key=lambda item: (
            _control_effect_rank(str(item.get("effect", ""))),
            str(item.get("control_name", "")),
            str(item.get("class_id", "")),
        ),
    )
    if not matches:
        class_clause = (
            "no approved vulnerability classes were available"
            if not vulnerability_classes
            else "no active control was approved for the vulnerability classes "
            + ", ".join(str(item["class_id"]) for item in vulnerability_classes)
        )
        return {
            "status": "exposed",
            "verdict": "unsure",
            "exposure_verdict": "unsure",
            "effect": "",
            "basis": (
                "Active controls require review before claiming mitigation; "
                f"{class_clause}."
            ),
            "matches": [],
        }

    strongest = matches[0]
    effect = str(strongest.get("effect", ""))
    basis = _class_aware_control_basis(matches)
    if effect in {"blocks", "compensates"}:
        return {
            "status": "mitigated",
            "verdict": "support",
            "exposure_verdict": "contradict",
            "effect": effect,
            "basis": basis,
            "matches": matches,
        }
    return {
        "status": "exposed",
        "verdict": "support",
        "exposure_verdict": "support",
        "effect": effect,
        "basis": basis,
        "matches": matches,
    }


def _control_effect_rank(effect: str) -> int:
    return {
        "blocks": 0,
        "compensates": 1,
        "reduces": 2,
        "detects": 3,
    }.get(effect, 4)


def _class_aware_control_basis(matches: list[dict[str, Any]]) -> str:
    phrases: list[str] = []
    for match in matches[:3]:
        phrase = (
            f"{match.get('control_name', 'control')} {match.get('effect', 'covers')} "
            f"{match.get('class_id', 'class')}"
        )
        validation_basis = _first_non_empty(match.get("validation_basis"))
        if validation_basis:
            phrase = f"{phrase} ({validation_basis})"
        phrases.append(phrase)
    suffix = "" if len(matches) <= 3 else f"; {len(matches) - 3} additional matches"
    return "Approved class-aware control coverage: " + "; ".join(phrases) + suffix + "."


def _adjust_priority_for_control_effect(
    priority: str,
    control_assessment: dict[str, Any],
) -> str:
    effect = str(control_assessment.get("effect", ""))
    status = str(control_assessment.get("status", ""))
    if status == "mitigated":
        return "medium" if priority in {"critical", "high"} else "low"
    if effect == "reduces":
        return _lower_priority(priority)
    return priority


def _lower_priority(priority: str) -> str:
    return {
        "critical": "high",
        "high": "medium",
        "medium": "low",
        "low": "low",
    }.get(priority, priority)


def _build_exposure_rationale(
    asset: dict[str, Any],
    active_controls: list[dict[str, Any]],
    exploitability_verdict: str,
    control_assessment: dict[str, Any] | None = None,
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
    if control_assessment is not None and control_assessment.get("matches"):
        return f"{hostname} is {exposure_clause}. {control_assessment['basis']}"
    control_names = ", ".join(
        sorted(
            _first_non_empty(control.get("name")) or "unknown control"
            for control in active_controls
        )
    )
    return f"{hostname} is {exposure_clause}; active controls require review: {control_names}"


def _evidence_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs = payload.get("evidence_refs")
    if isinstance(refs, list):
        return [dict(ref) for ref in refs if isinstance(ref, dict)]
    return []


def _merge_evidence_refs(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for group in groups:
        for ref in group:
            key = (
                str(ref.get("source", "")),
                str(ref.get("source_record_id", "")),
                str(ref.get("criteria", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(ref)
    return merged
