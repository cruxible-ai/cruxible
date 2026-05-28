"""Decide which asset/vulnerability situations deserve review.

This file contains the demo kit's security judgment. It answers questions like:

- Does this installed product version appear affected by this CVE?
- Is the affected asset exposed enough to propose an action item?
- Do active controls actually mitigate this kind of vulnerability?
- Has an already accepted exposure become stale after the reference data changed?

The surrounding configuration supplies explicit inputs: assets, products,
vulnerabilities, control mappings, and existing accepted facts. The functions in
this file decide what those facts mean for triage. They return plain rationale
and evidence references so a user or their agent can inspect the decision and
customize the policy for their own environment.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from cruxible_core.provider.payloads import JsonItems, evidence_ref, merge_evidence_refs
from cruxible_core.provider.types import ProviderContext

from .versioning import _assess_version_membership


def assess_asset_affected(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Decide whether an asset's installed product version is affected.

    The input rows already connect an asset's installed product with a
    vulnerability's affected product claim. This function only decides whether
    the installed version falls inside the affected version range.

    A version inside the affected range becomes review evidence. A version that
    is known fixed, or outside the affected range, is dropped so it does not
    create a noisy asset/vulnerability item.

    If several installed products point to the same asset and CVE, the output
    keeps one row for that asset/CVE pair. That matches the demo kit's model:
    the thing to review is the asset's current posture for that vulnerability,
    not every individual product path. If your environment needs product-by-
    product posture, this is one of the places to customize.
    """
    joined_product_edges = JsonItems.from_payload(
        input_payload, key="joined_product_edges"
    ).items
    rows_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for joined_row in joined_product_edges:
        edge = _nested_mapping(joined_row, "asset_product_edge")
        vulnerability_edge = _nested_mapping(joined_row, "vulnerability_product_edge")
        asset_id = _edge_from_id(edge)
        product_id = _edge_to_id(edge) or _first_non_empty(joined_row.get("product_id")) or ""
        cve_id = _edge_from_id(vulnerability_edge)
        properties = _edge_properties(edge)
        vulnerability_properties = _edge_properties(vulnerability_edge)
        if not asset_id or not product_id:
            continue
        if not cve_id:
            continue

        installed_version = _first_non_empty(properties.get("installed_version")) or ""
        source = _first_non_empty(properties.get("evidence_source")) or "asset_runs_product"
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
            "evidence_refs": merge_evidence_refs(
                _evidence_refs(properties),
                _evidence_refs(vulnerability_properties),
            ),
        }
        key = (asset_id, cve_id)
        current = rows_by_pair.get(key)
        if current is None or _verdict_rank(verdict) > _verdict_rank(current["verdict"]):
            rows_by_pair[key] = row

    return JsonItems(items=[rows_by_pair[key] for key in sorted(rows_by_pair)]).to_payload()


def assess_asset_exposure(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Decide whether an affected asset should be treated as exposed.

    This is the main triage decision in the demo kit. It starts with "the asset
    appears affected by the CVE" and adds business and control context:

    - internet-facing assets are treated as directly exposed;
    - production assets still matter even when they are not internet-facing;
    - low-risk non-production assets are filtered out to avoid noisy review
      queues;
    - an active control only mitigates a vulnerability when it is explicitly
      mapped to that vulnerability's class;
    - controls that ``block`` or ``compensate`` can mark the asset mitigated;
    - controls that ``reduce`` lower priority but leave the asset exposed;
    - controls that only ``detect`` preserve useful context but do not claim
      mitigation.

    These are not universal rules. They are the demo kit's starting policy. A
    user or agent should change this function when their exposure model, control
    taxonomy, or priority policy differs.
    """
    affected_items = _affected_items(input_payload)
    assets = JsonItems.from_payload(input_payload, key="assets").items
    asset_control_edges = JsonItems.from_payload(input_payload, key="asset_control_edges").items
    controls = JsonItems.from_payload(input_payload, key="controls").items
    classification_edges = (
        JsonItems.from_payload(input_payload, key="vulnerability_classification_edges").items
        if "vulnerability_classification_edges" in input_payload
        else []
    )
    control_mitigation_edges = (
        JsonItems.from_payload(input_payload, key="control_mitigation_edges").items
        if "control_mitigation_edges" in input_payload
        else []
    )

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
        active_controls_by_asset[asset_id].append(
            {
                "control_id": control_id,
                **control,
                "evidence_refs": merge_evidence_refs(
                    _evidence_refs(_edge_properties(edge)),
                    _evidence_refs(control),
                ),
            }
        )

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
            "evidence_refs": merge_evidence_refs(
                _evidence_refs(item),
                _evidence_refs(properties),
                control_assessment.get("evidence_refs", []),
            ),
            "affected_verdict": _first_non_empty(item.get("verdict"), properties.get("verdict"))
            or "support",
            "exploitability_verdict": exploitability_verdict,
            "control_verdict": control_verdict,
            "control_exposure_verdict": str(control_assessment["exposure_verdict"]),
            "control_effect": str(control_assessment["effect"]),
        }

    return JsonItems(items=[rows_by_pair[key] for key in sorted(rows_by_pair)]).to_payload()


def assess_exposure_reconciliation(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Find accepted exposure facts that no longer look true.

    Accepted exposure facts should continue to be supported by current product
    mappings and current vulnerability reference data. If an asset/CVE exposure
    was previously accepted but the current evidence no longer supports it, this
    function creates a review item to close or update that exposure.

    The reason is kept explicit. A stale exposure can mean the asset/product
    mapping changed, the vulnerability/product reference changed, or the version
    is no longer considered affected. Those reasons help a user or agent decide
    whether to accept the closure or inspect upstream data first.
    """
    accepted_exposure_edges = JsonItems.from_payload(
        input_payload, key="accepted_exposure_edges"
    ).items
    affected_items = JsonItems.from_payload(input_payload, key="affected_items").items
    asset_product_edges = JsonItems.from_payload(input_payload, key="asset_product_edges").items
    vulnerability_product_edges = JsonItems.from_payload(
        input_payload, key="vulnerability_product_edges"
    ).items
    remediated_edges = JsonItems.from_payload(input_payload, key="remediated_edges").items

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
                "evidence_refs": merge_evidence_refs(
                    _evidence_refs(properties),
                    [
                        evidence_ref(
                            "kev_reference_reconciliation",
                            f"{asset_id}:{cve_id}",
                        )
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

    return JsonItems(
        items=sorted(items, key=lambda item: (item["asset_id"], item["cve_id"]))
    ).to_payload()


def _affected_items(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = input_payload.get("affected_items")
    if raw_items is None:
        return JsonItems.from_payload(input_payload, key="affected_edges").items
    if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
        raise ValueError("Expected 'affected_items' to be a list of objects")
    return raw_items


def _edge_from_id(edge: dict[str, Any]) -> str:
    return _first_non_empty(edge.get("from_id")) or ""


def _edge_to_id(edge: dict[str, Any]) -> str:
    return _first_non_empty(edge.get("to_id")) or ""


def _edge_properties(edge: dict[str, Any]) -> dict[str, Any]:
    properties = edge.get("properties")
    return properties if isinstance(properties, dict) else {}


def _nested_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _entity_id(entity: dict[str, Any]) -> str:
    return _first_non_empty(entity.get("entity_id")) or ""


def _entity_properties(entity: dict[str, Any]) -> dict[str, Any]:
    properties = entity.get("properties")
    return properties if isinstance(properties, dict) else {}


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _verdict_rank(verdict: str) -> int:
    """Prefer stronger evidence when several product paths point to one CVE.

    ``support`` beats ``unsure`` because the review item should keep the best
    available reason for why the asset may be affected. ``contradict`` is lowest
    and is normally filtered out earlier.
    """
    return {"support": 2, "unsure": 1, "contradict": 0}.get(verdict, -1)


def _classes_by_vulnerability(
    classification_edges: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group vulnerability classes by CVE for control review.

    A control should not be considered relevant just because it is attached to
    the asset. The CVE also needs a class, such as "path traversal", so the code
    can ask whether the active control is known to help with that class.
    """
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
                "evidence_refs": _evidence_refs(_edge_properties(edge)),
            }
        )
    return {
        cve_id: sorted(classes, key=lambda item: str(item["class_id"]))
        for cve_id, classes in classes_by_vulnerability.items()
    }


def _mitigations_by_control_class(
    control_mitigation_edges: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group control coverage rules by control and vulnerability class.

    These rows say what a control is expected to do for a class of vulnerability:
    block it, reduce the risk, detect it, or compensate for it in some other
    way. They are the source of the control decision later in this file.
    """
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
                "evidence_refs": _evidence_refs(properties),
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
    """Decide whether the asset looks reachable enough to review.

    Internet exposure is treated as strong evidence. Production systems remain
    worth review even without explicit internet exposure because internal
    reachability and business impact may still matter. Lower-risk assets are
    filtered out so the review queue does not fill with weak cases.
    """
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
    """Choose the first review priority before control effects are applied.

    This priority is about operational urgency for the asset, not just CVSS or
    EPSS severity. A directly exposed critical asset starts high. Important
    production assets also stay high enough for review even when some evidence is
    uncertain. Control coverage may lower the priority later.
    """
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
    """Decide whether the asset's active controls help with this CVE.

    The asset must have an active control, and the CVE must have a class that the
    control is mapped to. This prevents broad statements like "the asset has EDR,
    so every vulnerability is mitigated."

    Control effects are interpreted this way:
    - ``blocks`` and ``compensates`` are strong enough to call the exposure
      mitigated;
    - ``reduces`` is useful risk reduction but still leaves the asset exposed;
    - ``detects`` is useful monitoring context but does not reduce exposure;
    - active controls with no matching class produce ``unsure`` so a user can
      decide whether the control mapping needs improvement.
    """
    if not active_controls:
        return {
            "status": "exposed",
            "verdict": "support",
            "exposure_verdict": "support",
            "effect": "",
            "basis": "No active compensating controls were attached to this asset.",
            "matches": [],
            "evidence_refs": [],
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
                        "evidence_refs": merge_evidence_refs(
                            _evidence_refs(control),
                            vulnerability_class.get("evidence_refs", []),
                            mitigation.get("evidence_refs", []),
                        ),
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
            "evidence_refs": [],
        }

    strongest = matches[0]
    effect = str(strongest.get("effect", ""))
    basis = _class_aware_control_basis(matches)
    evidence_refs = merge_evidence_refs(
        *(match.get("evidence_refs", []) for match in matches)
    )
    if effect in {"blocks", "compensates"}:
        return {
            "status": "mitigated",
            "verdict": "support",
            "exposure_verdict": "contradict",
            "effect": effect,
            "basis": basis,
            "matches": matches,
            "evidence_refs": evidence_refs,
        }
    return {
        "status": "exposed",
        "verdict": "support",
        "exposure_verdict": "support",
        "effect": effect,
        "basis": basis,
        "matches": matches,
        "evidence_refs": evidence_refs,
    }


def _control_effect_rank(effect: str) -> int:
    """Order control effects from strongest to weakest mitigation."""
    return {
        "blocks": 0,
        "compensates": 1,
        "reduces": 2,
        "detects": 3,
    }.get(effect, 4)


def _class_aware_control_basis(matches: list[dict[str, Any]]) -> str:
    """Explain which control/class matches affected the decision."""
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
    """Lower priority when controls reduce the need for urgent action.

    A mitigated issue should still be visible, but it should be less urgent than
    an unmitigated exposure. ``reduces`` lowers priority once while keeping the
    item exposed. ``blocks`` and ``compensates`` mark the item mitigated and lower
    urgency more strongly.
    """
    effect = str(control_assessment.get("effect", ""))
    status = str(control_assessment.get("status", ""))
    if status == "mitigated":
        return "medium" if priority in {"critical", "high"} else "low"
    if effect == "reduces":
        return _lower_priority(priority)
    return priority


def _lower_priority(priority: str) -> str:
    """Move one step down the demo kit's urgency scale."""
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
    """Write the short explanation shown to a reviewer.

    The text should explain why the asset is worth review and whether controls
    mitigate, reduce, or simply add context. It avoids internal implementation
    detail so users and agents can reason from the explanation directly.
    """
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
