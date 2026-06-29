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
from copy import deepcopy
from typing import Any

from cruxible_core.provider.payloads import JsonItems, evidence_ref, merge_evidence_refs
from cruxible_core.provider.types import ProviderContext

from .versioning import _assess_version_membership

DEFAULT_ASSESSMENT_POLICY: dict[str, Any] = {
    "exploitability": {
        "direct_exposure_verdict": "support",
        "reviewable_environments": ["production"],
        "reviewable_environment_verdict": "unsure",
        "default_verdict": "contradict",
    },
    "priority": {
        "elevated_exploitability_verdict": "support",
        "elevated_control_verdict": "support",
        "elevated_priority_by_criticality": {
            "critical": "critical",
            "default": "high",
        },
        "high_priority_criticalities": ["critical", "high"],
        "high_priority": "high",
        "default_priority": "medium",
        "mitigated_priority_by_current_priority": {
            "critical": "medium",
            "high": "medium",
            "medium": "low",
            "low": "low",
        },
        "reducing_control_effects": ["reduces"],
        "priority_order": ["low", "medium", "high", "critical"],
    },
    "control_effects": {
        "rank": ["blocks", "compensates", "reduces", "detects"],
        "mitigating": ["blocks", "compensates"],
    },
}


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

    The provider intentionally returns one row for each supported joined product
    path. The workflow owns pair-level deduplication with ``dedupe_items`` so
    this function stays focused on version membership instead of collection
    shaping.
    """
    joined_product_edges = JsonItems.from_payload(input_payload, key="joined_product_edges").items
    rows: list[dict[str, Any]] = []
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
        source = (
            _first_non_empty(
                properties.get("inventory_source"),
                properties.get("evidence_source"),
                properties.get("source"),
            )
            or "asset_runs_product"
        )
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
                _evidence_refs(edge),
                _evidence_refs(vulnerability_edge),
            ),
            "verdict_rank": _verdict_rank(verdict),
        }
        rows.append(row)

    return JsonItems(
        items=sorted(
            rows,
            key=lambda item: (
                str(item["asset_id"]),
                str(item["cve_id"]),
                -int(item["verdict_rank"]),
                str(item["product_id"]),
            ),
        )
    ).to_payload()


def assess_asset_exposure(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Decide whether an affected asset should be treated as exposed.

    This is the main triage decision in the demo kit. The workflow supplies
    affected asset rows joined to their asset record plus active control
    bindings. This function applies the kit's domain policy:

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

    These are not universal rules. The workflow passes ``assessment_policy`` so
    kit authors can change exposure, priority, and control-effect knobs in
    config. ``DEFAULT_ASSESSMENT_POLICY`` exists for direct provider tests and
    as a documented fallback when older callers omit the policy payload.
    """
    policy = _assessment_policy(input_payload)
    affected_asset_context = _affected_asset_context(input_payload)
    active_control_bindings = _active_control_bindings(input_payload)
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

    classes_by_vulnerability = _classes_by_vulnerability(classification_edges)
    mitigations_by_control_class = _mitigations_by_control_class(control_mitigation_edges)
    active_controls_by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for binding in active_control_bindings:
        asset_id = _first_non_empty(binding.get("asset_id")) or ""
        control_id = _first_non_empty(binding.get("control_id")) or ""
        if not asset_id or not control_id:
            continue
        control = _entity_properties(_nested_mapping(binding, "control_entity"))
        active_controls_by_asset[asset_id].append(
            {
                "control_id": control_id,
                **control,
                "evidence_refs": merge_evidence_refs(
                    _evidence_refs(_nested_mapping(binding, "asset_control_edge")),
                    _evidence_refs(_nested_mapping(binding, "control_entity")),
                    _evidence_refs(control),
                ),
            }
        )

    rows_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for context in affected_asset_context:
        item = _nested_mapping(context, "affected_item")
        asset = _entity_properties(_nested_mapping(context, "asset_entity"))
        asset_id = _first_non_empty(item.get("asset_id")) or _edge_from_id(item)
        cve_id = _first_non_empty(item.get("cve_id")) or _edge_to_id(item)
        if not asset_id or not cve_id:
            continue

        properties = _edge_properties(item)
        active_controls = active_controls_by_asset.get(asset_id, [])
        exploitability_verdict = _derive_exploitability_verdict(asset, policy)
        if exploitability_verdict == "contradict":
            continue

        control_assessment = _assess_class_aware_controls(
            active_controls,
            classes_by_vulnerability.get(cve_id, []),
            mitigations_by_control_class,
            policy,
        )
        control_verdict = str(control_assessment["verdict"])
        status = str(control_assessment["status"])
        priority = _derive_exposure_priority(
            asset,
            exploitability_verdict,
            control_verdict,
            policy,
        )
        priority = _adjust_priority_for_control_effect(priority, control_assessment, policy)
        exposure_basis = _build_exposure_rationale(
            asset,
            active_controls,
            exploitability_verdict,
            control_assessment,
        )
        affected_basis = (
            _first_non_empty(
                item.get("rationale"),
                properties.get("rationale"),
                properties.get("affected_basis"),
            )
            or ""
        )
        rows_by_pair[(asset_id, cve_id)] = {
            "asset_id": asset_id,
            "cve_id": cve_id,
            "status": status,
            "priority": priority,
            "product_id": _first_non_empty(item.get("product_id"), properties.get("product_id"))
            or "",
            "installed_version": _first_non_empty(
                item.get("installed_version"),
                properties.get("installed_version"),
            )
            or "",
            "basis": {
                "affected": affected_basis,
                "exposure": exposure_basis,
                "control": str(control_assessment["basis"]),
            },
            "evidence_refs": merge_evidence_refs(
                _evidence_refs(item),
                _evidence_refs(properties),
                control_assessment.get("evidence_refs", []),
            ),
            "verdicts": {
                "affected": _first_non_empty(item.get("verdict"), properties.get("verdict"))
                or "support",
                "exploitability": exploitability_verdict,
                "control": control_verdict,
            },
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
                    _evidence_refs(edge),
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


def _affected_asset_context(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = input_payload.get("affected_asset_context")
    if raw_items is not None:
        if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
            raise ValueError("Expected 'affected_asset_context' to be a list of objects")
        return raw_items

    assets = JsonItems.from_payload(input_payload, key="assets").items
    assets_by_id = {_entity_id(entity): entity for entity in assets}
    context: list[dict[str, Any]] = []
    for item in _affected_items(input_payload):
        asset_id = _first_non_empty(item.get("asset_id")) or _edge_from_id(item)
        if not asset_id or asset_id not in assets_by_id:
            continue
        context.append({"affected_item": item, "asset_entity": assets_by_id[asset_id]})
    return context


def _active_control_bindings(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = input_payload.get("active_control_bindings")
    if raw_items is not None:
        if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
            raise ValueError("Expected 'active_control_bindings' to be a list of objects")
        return raw_items

    asset_control_edges = JsonItems.from_payload(input_payload, key="asset_control_edges").items
    controls = JsonItems.from_payload(input_payload, key="controls").items
    controls_by_id = {_entity_id(entity): entity for entity in controls}
    bindings: list[dict[str, Any]] = []
    for edge in asset_control_edges:
        control_id = _edge_to_id(edge)
        control_entity = controls_by_id.get(control_id)
        if control_entity is None:
            continue
        if _first_non_empty(_entity_properties(control_entity).get("status")) != "active":
            continue
        bindings.append(
            {
                "asset_id": _edge_from_id(edge),
                "control_id": control_id,
                "asset_control_edge": edge,
                "control_entity": control_entity,
            }
        )
    return bindings


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
                "evidence_refs": _evidence_refs(edge),
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
                "evidence_refs": _evidence_refs(edge),
            }
        )
    return {
        key: sorted(items, key=lambda item: (str(item["effect"]), str(item["class_id"])))
        for key, items in mitigations_by_control_class.items()
    }


def _assessment_policy(input_payload: dict[str, Any]) -> dict[str, Any]:
    raw_policy = input_payload.get("assessment_policy")
    policy = deepcopy(DEFAULT_ASSESSMENT_POLICY)
    if raw_policy is None:
        return policy
    if not isinstance(raw_policy, dict):
        raise ValueError("Expected 'assessment_policy' to be an object")
    return _deep_merge(policy, raw_policy)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _policy_section(policy: dict[str, Any], name: str) -> dict[str, Any]:
    value = policy.get(name)
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _string_mapping(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    return {}


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


def _derive_exploitability_verdict(asset: dict[str, Any], policy: dict[str, Any]) -> str:
    """Decide whether the asset looks reachable enough to review.

    Internet exposure is treated as strong evidence. Production systems remain
    worth review even without explicit internet exposure because internal
    reachability and business impact may still matter. Lower-risk assets are
    filtered out so the review queue does not fill with weak cases.
    """
    internet_exposed = asset.get("internet_exposed")
    environment = _first_non_empty(asset.get("environment")) or ""
    exploitability_policy = _policy_section(policy, "exploitability")
    if internet_exposed is True:
        return str(exploitability_policy.get("direct_exposure_verdict", "support"))
    reviewable_environments = set(
        _string_list(exploitability_policy.get("reviewable_environments"))
    )
    if environment in reviewable_environments:
        return str(exploitability_policy.get("reviewable_environment_verdict", "unsure"))
    return str(exploitability_policy.get("default_verdict", "contradict"))


def _derive_exposure_priority(
    asset: dict[str, Any],
    exploitability_verdict: str,
    control_verdict: str,
    policy: dict[str, Any],
) -> str:
    """Choose the first review priority before control effects are applied.

    This priority is about operational urgency for the asset, not just CVSS or
    EPSS severity. A directly exposed critical asset starts high. Important
    production assets also stay high enough for review even when some evidence is
    uncertain. Control coverage may lower the priority later.
    """
    priority_policy = _policy_section(policy, "priority")
    criticality = _first_non_empty(asset.get("criticality")) or ""
    if exploitability_verdict == str(
        priority_policy.get("elevated_exploitability_verdict", "support")
    ) and control_verdict == str(priority_policy.get("elevated_control_verdict", "support")):
        elevated_priorities = _string_mapping(
            priority_policy.get("elevated_priority_by_criticality")
        )
        return elevated_priorities.get(
            criticality,
            elevated_priorities.get("default", "high"),
        )
    if criticality in set(_string_list(priority_policy.get("high_priority_criticalities"))):
        return str(priority_policy.get("high_priority", "high"))
    return str(priority_policy.get("default_priority", "medium"))


def _assess_class_aware_controls(
    active_controls: list[dict[str, Any]],
    vulnerability_classes: list[dict[str, Any]],
    mitigations_by_control_class: dict[tuple[str, str], list[dict[str, Any]]],
    policy: dict[str, Any],
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
            _control_effect_rank(str(item.get("effect", "")), policy),
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
                f"Active controls require review before claiming mitigation; {class_clause}."
            ),
            "matches": [],
            "evidence_refs": [],
        }

    strongest = matches[0]
    effect = str(strongest.get("effect", ""))
    basis = _class_aware_control_basis(matches)
    evidence_refs = merge_evidence_refs(*(match.get("evidence_refs", []) for match in matches))
    control_policy = _policy_section(policy, "control_effects")
    if effect in set(_string_list(control_policy.get("mitigating"))):
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


def _control_effect_rank(effect: str, policy: dict[str, Any]) -> int:
    """Order control effects from strongest to weakest mitigation."""
    ranked_effects = _string_list(_policy_section(policy, "control_effects").get("rank"))
    try:
        return ranked_effects.index(effect)
    except ValueError:
        return len(ranked_effects)


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
    policy: dict[str, Any],
) -> str:
    """Lower priority when controls reduce the need for urgent action.

    A mitigated issue should still be visible, but it should be less urgent than
    an unmitigated exposure. ``reduces`` lowers priority once while keeping the
    item exposed. ``blocks`` and ``compensates`` mark the item mitigated and lower
    urgency more strongly.
    """
    effect = str(control_assessment.get("effect", ""))
    status = str(control_assessment.get("status", ""))
    priority_policy = _policy_section(policy, "priority")
    if status == "mitigated":
        mitigated_priorities = _string_mapping(
            priority_policy.get("mitigated_priority_by_current_priority")
        )
        return mitigated_priorities.get(priority, priority)
    if effect in set(_string_list(priority_policy.get("reducing_control_effects"))):
        return _lower_priority(priority, policy)
    return priority


def _lower_priority(priority: str, policy: dict[str, Any]) -> str:
    """Move one step down the demo kit's urgency scale."""
    order = _string_list(_policy_section(policy, "priority").get("priority_order"))
    if priority not in order:
        return priority
    index = order.index(priority)
    return order[max(index - 1, 0)]


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
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        evidence = metadata.get("evidence")
        if isinstance(evidence, dict) and isinstance(evidence.get("evidence_refs"), list):
            return [dict(ref) for ref in evidence["evidence_refs"] if isinstance(ref, dict)]
    refs = payload.get("evidence_refs")
    if isinstance(refs, list):
        return [dict(ref) for ref in refs if isinstance(ref, dict)]
    return []
