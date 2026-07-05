"""Deterministic data providers for the supply-chain blast-radius kit."""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from cruxible_core.provider.payloads import load_artifact_json
from cruxible_core.provider.types import ProviderContext

_SEED_DIR = Path(__file__).resolve().parents[1] / "data" / "seed"

_SUPPLIER_KEYS = ("supplier_id", "name", "primary_geography", "tier_hint")
_COMPONENT_KEYS = (
    "component_id",
    "name",
    "component_kind",
    "manufacturer",
    "mpn",
    "revision",
    "lifecycle_status",
    "criticality",
    "category",
)
_ASSEMBLY_KEYS = (
    "assembly_id",
    "name",
    "revision",
    "lifecycle_status",
    "criticality",
    "category",
)
_PRODUCT_KEYS = ("product_id", "sku", "name", "lifecycle_status")
_SHIPMENT_KEYS = ("shipment_id", "customer_id", "status", "ship_date", "eta")
_SUPPLY_EDGE_KEYS = (
    "lead_time_days",
    "qualification_status",
    "sourcing_role",
    "priority_rank",
    "allocation_pct",
    "activation_state",
    "capacity_units_per_week",
    "effective_from",
    "effective_to",
    "last_verified_at",
)
_BOM_EDGE_KEYS = ("quantity", "bom_variant_id", "plant_id", "effective_from", "effective_to")
_LOCATION_KEYS = ("location_id", "name", "location_type", "geography")
_INVENTORY_POSITION_KEYS = (
    "inventory_position_id",
    "item_type",
    "item_id",
    "location_id",
    "quantity_on_hand",
    "quantity_allocated",
    "net_available",
    "unit_of_measure",
    "as_of",
)
_INCIDENT_KEYS = (
    "incident_id",
    "title",
    "severity",
    "scope_type",
    "scope_id",
    "status",
    "reported_at",
    "closed_at",
    "summary",
)
_WORK_ITEM_KEYS = (
    "work_item_id",
    "title",
    "summary",
    "description",
    "rationale",
    "type",
    "status",
    "priority",
    "target_date",
)
_RISK_KEYS = ("risk_id", "title", "summary", "status", "priority")

_BUFFER_STATE_RANK = {
    "sufficient_buffer": 0,
    "unknown": 1,
    "partial_buffer": 2,
    "no_buffer": 3,
}
_DEPTH_RANK = {"direct": 0, "tier_2": 1, "tier_3_plus": 2}


def load_seed_data(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load and expand the pinned open-hardware BOM seed bundle."""
    bundle = _load_seed_json("bundle.json", context)
    components, component_bom_edges, component_meta = _expand_components(bundle)

    suppliers = _sorted_rows(
        [_project(row, _SUPPLIER_KEYS, defaults={"tier_hint": None}) for row in bundle["suppliers"]],
        "supplier_id",
    )
    assemblies = _sorted_rows(
        [
            _project(
                row,
                _ASSEMBLY_KEYS,
                defaults={"revision": None, "lifecycle_status": "active"},
            )
            for row in bundle["assemblies"]
        ],
        "assembly_id",
    )
    products = _sorted_rows(
        [_project(row, _PRODUCT_KEYS, defaults={"lifecycle_status": "active"}) for row in bundle["products"]],
        "product_id",
    )
    shipments = _sorted_rows([_project(row, _SHIPMENT_KEYS) for row in bundle["shipments"]], "shipment_id")

    return {
        "suppliers": suppliers,
        "components": _sorted_rows(
            [
                _project(
                    row,
                    _COMPONENT_KEYS,
                    defaults={"manufacturer": None, "mpn": None, "revision": None},
                )
                for row in components
            ],
            "component_id",
        ),
        "assemblies": assemblies,
        "products": products,
        "shipments": shipments,
        "supplier_supplies_component": _sorted_rows(
            _expand_component_supply_edges(bundle, components, component_meta),
            "supplier_id",
            "component_id",
            "priority_rank",
        ),
        "supplier_supplies_assembly": _sorted_rows(
            [
                _supply_edge(
                    {
                        **row,
                        "assembly_id": row["assembly_id"],
                    }
                )
                for row in bundle.get("supplier_supplies_assembly", [])
            ],
            "supplier_id",
            "assembly_id",
            "priority_rank",
        ),
        "component_part_of_assembly": _sorted_rows(component_bom_edges, "component_id", "assembly_id"),
        "assembly_part_of_assembly": _sorted_rows(
            [
                _project(
                    row,
                    ("child_assembly_id", "parent_assembly_id", *_BOM_EDGE_KEYS),
                    defaults={"quantity": 1, "effective_to": None},
                )
                for row in bundle["assembly_part_of_assembly"]
            ],
            "child_assembly_id",
            "parent_assembly_id",
        ),
        "assembly_part_of_product": _sorted_rows(
            [
                _project(
                    row,
                    ("assembly_id", "product_id", *_BOM_EDGE_KEYS),
                    defaults={"quantity": 1, "effective_to": None},
                )
                for row in bundle["assembly_part_of_product"]
            ],
            "assembly_id",
            "product_id",
        ),
        "product_in_shipment": _sorted_rows(
            [
                _project(row, ("product_id", "shipment_id", "qty"), defaults={"qty": None})
                for row in bundle["product_in_shipment"]
            ],
            "product_id",
            "shipment_id",
        ),
    }


def load_incident_feed(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load the pinned incident feed."""
    payload = _load_seed_json("incidents.json", context)
    return {
        "incidents": _sorted_rows(
            [
                _project(
                    row,
                    _INCIDENT_KEYS,
                    defaults={"scope_id": None, "closed_at": None, "summary": None},
                )
                for row in payload["incidents"]
            ],
            "incident_id",
        )
    }


def analyze_operations_routing(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load deterministic operations work/risk routing rows from the seed bundle."""
    payload = _load_seed_json("operations_routing.json", context)
    work_items = []
    work_item_edges = []
    for row in payload.get("work_items", []):
        work_item = _project(
            row,
            _WORK_ITEM_KEYS,
            defaults={"summary": None, "description": None, "rationale": None, "target_date": None},
        )
        work_items.append(work_item)
        if row.get("incident_id"):
            work_item_edges.append(
                {
                    "work_item_id": row["work_item_id"],
                    "incident_id": row["incident_id"],
                }
            )
    risks = [
        _project(row, _RISK_KEYS, defaults={"summary": None})
        for row in payload.get("risks", [])
    ]
    return {
        "work_items": _sorted_rows(work_items, "work_item_id"),
        "risks": _sorted_rows(risks, "risk_id"),
        "work_item_addresses_incident": _sorted_rows(
            work_item_edges,
            "work_item_id",
            "incident_id",
        ),
    }


def assess_incident_supplier_scope(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Map open incident scopes to suppliers by direct id or geography."""
    incidents = [_coerce_row(row) for row in _list(input_payload, "incidents")]
    suppliers = [_coerce_row(row) for row in _list(input_payload, "suppliers")]

    items: list[dict[str, Any]] = []
    for incident in sorted(incidents, key=lambda row: _entity_id(row, "Incident")):
        if _value(incident, "status") != "open":
            continue
        incident_id = _entity_id(incident, "Incident")
        scope_type = _value(incident, "scope_type")
        scope_id = _value(incident, "scope_id")
        for supplier in sorted(suppliers, key=lambda row: _entity_id(row, "Supplier")):
            supplier_id = _entity_id(supplier, "Supplier")
            supplier_geo = str(_value(supplier, "primary_geography") or "")
            match_basis: str | None = None
            if scope_type == "supplier" and scope_id == supplier_id:
                match_basis = "direct"
            elif scope_type == "geography" and _geography_matches(str(scope_id or ""), supplier_geo):
                match_basis = "geography"
            if match_basis is None:
                continue
            verdict = "support" if match_basis == "direct" else "unsure"
            items.append(
                {
                    "incident_id": incident_id,
                    "supplier_id": supplier_id,
                    "match_basis": match_basis,
                    "rationale": (
                        f"Incident {incident_id} {match_basis}-matches supplier {supplier_id} "
                        f"({supplier_geo})."
                    ),
                    "verdict": verdict,
                }
            )
    return {"items": _sorted_rows(items, "incident_id", "supplier_id")}


def assess_supplier_risk_attachments(
    input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Propose supplier risk attachments from routed risks and cascade evidence."""
    routing = _load_seed_json("operations_routing.json", context)
    routed_suppliers = {
        str(row["risk_id"]): str(row["supplier_id"])
        for row in routing.get("risks", [])
        if row.get("risk_id") and row.get("supplier_id")
    }
    risks = {_entity_id(row, "Risk"): _coerce_row(row) for row in _list(input_payload, "risks")}
    suppliers = {_entity_id(row, "Supplier"): _coerce_row(row) for row in _list(input_payload, "suppliers")}
    incidents = {_entity_id(row, "Incident"): _coerce_row(row) for row in _list(input_payload, "incidents")}

    incidents_by_supplier: dict[str, set[str]] = defaultdict(set)
    for edge in _list(input_payload, "impacted_supplier_edges"):
        row = _coerce_row(edge)
        incident_id = _edge_incident_id(row)
        if incidents and _value(incidents.get(incident_id, {}), "status") != "open":
            continue
        incidents_by_supplier[_edge_supplier_id(row)].add(incident_id)

    component_counts: dict[str, int] = defaultdict(int)
    for edge in _list(input_payload, "impacted_component_edges"):
        component_counts[_edge_incident_id(_coerce_row(edge))] += 1

    assembly_counts: dict[str, int] = defaultdict(int)
    for edge in _list(input_payload, "impacted_assembly_edges"):
        assembly_counts[_edge_incident_id(_coerce_row(edge))] += 1

    items: list[dict[str, Any]] = []
    for risk_id, supplier_id in sorted(routed_suppliers.items()):
        if risk_id not in risks or supplier_id not in suppliers:
            continue
        incident_ids = sorted(incidents_by_supplier.get(supplier_id, set()))
        if not incident_ids:
            continue
        cascade_count = sum(
            component_counts[incident_id] + assembly_counts[incident_id]
            for incident_id in incident_ids
        )
        supplier_name = _value(suppliers[supplier_id], "name") or supplier_id
        risk_title = _value(risks[risk_id], "title") or risk_id
        incident_text = ", ".join(incident_ids)
        impact_basis = (
            f"Risk {risk_id} attaches to supplier {supplier_id} because open incident(s) "
            f"{incident_text} already impact the supplier; cascade evidence rows={cascade_count}."
        )
        maintainer_verdict = "support" if cascade_count else "unsure"
        items.append(
            {
                "risk_id": risk_id,
                "supplier_id": supplier_id,
                "impact_basis": impact_basis,
                "source_evidence_verdict": "support",
                "source_evidence": (
                    f"{supplier_name} is linked to open incident(s) {incident_text} in the cascade graph."
                ),
                "maintainer_judgment_verdict": maintainer_verdict,
                "maintainer_judgment": (
                    f"{risk_title}; cascade evidence row count for the routed supplier is {cascade_count}."
                ),
                "verdict": "support" if maintainer_verdict == "support" else "unsure",
            }
        )
    return {"items": _sorted_rows(items, "risk_id", "supplier_id")}


def load_inventory_positions(
    input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load the pinned inventory fixture, applying the requested filters.

    Acquisition is deliberately not a provider concern: pull live positions
    with ``scripts/fetch_inventory.py`` (which owns API auth) and apply the
    reviewed rows through the sync workflow — same contract as this output.
    """
    payload = _load_seed_json("inventory_positions.json", context)
    return _filter_inventory_payload(payload, input_payload)


def assess_buffer_coverage(
    input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Compute item/product days-of-cover from inventory, BOM, and demand."""
    components = {_entity_id(row, "Component"): _coerce_row(row) for row in _list(input_payload, "components")}
    assemblies = {_entity_id(row, "Assembly"): _coerce_row(row) for row in _list(input_payload, "assemblies")}
    products = {_entity_id(row, "Product"): _coerce_row(row) for row in _list(input_payload, "products")}
    positions = {
        _entity_id(row, "InventoryPosition"): _coerce_row(row)
        for row in _list(input_payload, "inventory_positions")
    }
    demand = _demand_context(input_payload.get("demand_context"), context)

    component_inventory = defaultdict(list)
    for edge in _list(input_payload, "component_inventory_position_edges"):
        row = _coerce_row(edge)
        component_inventory[_edge_component_id(row)].append(_edge_inventory_position_id(row))

    assembly_inventory = defaultdict(list)
    for edge in _list(input_payload, "assembly_inventory_position_edges"):
        row = _coerce_row(edge)
        assembly_inventory[_edge_assembly_id(row)].append(_edge_inventory_position_id(row))

    component_requirements, assembly_requirements = _product_requirements(
        component_edges=[_coerce_row(row) for row in _list(input_payload, "component_part_of_assembly_edges")],
        assembly_edges=[_coerce_row(row) for row in _list(input_payload, "assembly_part_of_assembly_edges")],
        product_edges=[_coerce_row(row) for row in _list(input_payload, "assembly_part_of_product_edges")],
        known_components=set(components),
        known_assemblies=set(assemblies),
    )

    assessments: list[dict[str, Any]] = []
    component_edges_out: list[dict[str, Any]] = []
    assembly_edges_out: list[dict[str, Any]] = []
    product_edges_out: list[dict[str, Any]] = []
    inventory_edges_out: list[dict[str, Any]] = []

    for product_id in sorted(products):
        open_demand_units = _product_demand_units(demand, product_id)
        for component_id, requirement in sorted(component_requirements.get(product_id, {}).items()):
            row, linked_positions = _buffer_row(
                item_type="component",
                item_id=component_id,
                product_id=product_id,
                requirement=requirement,
                open_demand_units=open_demand_units,
                position_ids=component_inventory.get(component_id, []),
                positions=positions,
                demand=demand,
            )
            assessments.append(row)
            component_edges_out.append(
                {"component_id": component_id, "buffer_assessment_id": row["buffer_assessment_id"]}
            )
            product_edges_out.append({"product_id": product_id, "buffer_assessment_id": row["buffer_assessment_id"]})
            for position_id in linked_positions:
                inventory_edges_out.append(
                    {"buffer_assessment_id": row["buffer_assessment_id"], "inventory_position_id": position_id}
                )

        for assembly_id, requirement in sorted(assembly_requirements.get(product_id, {}).items()):
            row, linked_positions = _buffer_row(
                item_type="assembly",
                item_id=assembly_id,
                product_id=product_id,
                requirement=requirement,
                open_demand_units=open_demand_units,
                position_ids=assembly_inventory.get(assembly_id, []),
                positions=positions,
                demand=demand,
            )
            assessments.append(row)
            assembly_edges_out.append({"assembly_id": assembly_id, "buffer_assessment_id": row["buffer_assessment_id"]})
            product_edges_out.append({"product_id": product_id, "buffer_assessment_id": row["buffer_assessment_id"]})
            for position_id in linked_positions:
                inventory_edges_out.append(
                    {"buffer_assessment_id": row["buffer_assessment_id"], "inventory_position_id": position_id}
                )

    return {
        "buffer_assessments": _sorted_rows(assessments, "buffer_assessment_id"),
        "component_buffer_assessments": _sorted_rows(
            component_edges_out,
            "component_id",
            "buffer_assessment_id",
        ),
        "assembly_buffer_assessments": _sorted_rows(
            assembly_edges_out,
            "assembly_id",
            "buffer_assessment_id",
        ),
        "product_buffer_assessments": _sorted_rows(
            product_edges_out,
            "product_id",
            "buffer_assessment_id",
        ),
        "buffer_assessment_inventory": _sorted_rows(
            inventory_edges_out,
            "buffer_assessment_id",
            "inventory_position_id",
        ),
    }


def assess_incident_component_cascade(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Cascade impacted suppliers to supplied components."""
    impacted = _impacted_suppliers_by_incident(input_payload.get("impacted_supplier_edges", []))
    supplies_by_component = defaultdict(list)
    for edge in _list(input_payload, "supplier_supplies_component_edges"):
        row = _coerce_row(edge)
        supplies_by_component[_edge_component_id(row)].append(row)
    components = {_entity_id(row, "Component"): _coerce_row(row) for row in _list(input_payload, "components")}

    items: list[dict[str, Any]] = []
    for incident_id, affected_suppliers in sorted(impacted.items()):
        for component_id, supply_edges in sorted(supplies_by_component.items()):
            impacted_edges = [
                edge for edge in supply_edges if _edge_supplier_id(edge) in affected_suppliers
            ]
            if not impacted_edges:
                continue
            state, verdict = _alternate_state_and_verdict(supply_edges, affected_suppliers)
            component = components.get(component_id, {})
            if _value(component, "lifecycle_status") not in (None, "active"):
                verdict = "contradict"
                rationale = f"Component {component_id} is not active in the catalog."
            else:
                impacted_ids = ", ".join(sorted({_edge_supplier_id(edge) for edge in impacted_edges}))
                rationale = (
                    f"Incident {incident_id} affects supplier(s) {impacted_ids} for component "
                    f"{component_id}; alternate_state={state}."
                )
            items.append(
                {
                    "incident_id": incident_id,
                    "component_id": component_id,
                    "alternate_state": state,
                    "rationale": rationale,
                    "verdict": verdict,
                }
            )
    return {"items": _sorted_rows(items, "incident_id", "component_id")}


def assess_incident_assembly_cascade(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Cascade impacted suppliers to directly supplied assemblies."""
    impacted = _impacted_suppliers_by_incident(input_payload.get("impacted_supplier_edges", []))
    supplies_by_assembly = defaultdict(list)
    for edge in _list(input_payload, "supplier_supplies_assembly_edges"):
        row = _coerce_row(edge)
        supplies_by_assembly[_edge_assembly_id(row)].append(row)
    assemblies = {_entity_id(row, "Assembly"): _coerce_row(row) for row in _list(input_payload, "assemblies")}

    items: list[dict[str, Any]] = []
    for incident_id, affected_suppliers in sorted(impacted.items()):
        for assembly_id, supply_edges in sorted(supplies_by_assembly.items()):
            impacted_edges = [
                edge for edge in supply_edges if _edge_supplier_id(edge) in affected_suppliers
            ]
            if not impacted_edges:
                continue
            state, verdict = _alternate_state_and_verdict(supply_edges, affected_suppliers)
            impacted_supplier_id = sorted({_edge_supplier_id(edge) for edge in impacted_edges})[0]
            assembly = assemblies.get(assembly_id, {})
            if _value(assembly, "lifecycle_status") not in (None, "active"):
                verdict = "contradict"
                rationale = f"Assembly {assembly_id} is not active in the catalog."
            else:
                rationale = (
                    f"Incident {incident_id} affects supplier {impacted_supplier_id} for directly "
                    f"supplied assembly {assembly_id}; alternate_state={state}."
                )
            items.append(
                {
                    "incident_id": incident_id,
                    "assembly_id": assembly_id,
                    "impacted_supplier_id": impacted_supplier_id,
                    "alternate_state": state,
                    "rationale": rationale,
                    "verdict": verdict,
                }
            )
    return {"items": _sorted_rows(items, "incident_id", "assembly_id")}


def assess_incident_product_exposure(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Roll accepted component/direct-assembly impacts up to product exposure."""
    component_edges = [_coerce_row(row) for row in _list(input_payload, "component_part_of_assembly_edges")]
    assembly_edges = [_coerce_row(row) for row in _list(input_payload, "assembly_part_of_assembly_edges")]
    product_edges = [_coerce_row(row) for row in _list(input_payload, "assembly_part_of_product_edges")]
    products = {_entity_id(row, "Product"): _coerce_row(row) for row in _list(input_payload, "products")}

    component_to_assemblies = defaultdict(list)
    for edge in component_edges:
        component_to_assemblies[_edge_component_id(edge)].append(edge)

    assembly_to_parents = defaultdict(list)
    for edge in assembly_edges:
        assembly_to_parents[_edge_child_assembly_id(edge)].append(edge)

    assembly_to_products = defaultdict(list)
    for edge in product_edges:
        assembly_to_products[_edge_assembly_id(edge)].append(edge)

    buffer_lookup = _buffer_lookup(input_payload)
    product_paths: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for impact in _list(input_payload, "impacted_component_edges"):
        row = _coerce_row(impact)
        incident_id = _edge_incident_id(row)
        component_id = _edge_component_id(row)
        for component_edge in component_to_assemblies.get(component_id, []):
            start_assembly = _edge_assembly_id(component_edge)
            component_variant = _bom_variant(component_edge)
            for product_id, assembly_path, depth in _assembly_product_paths(
                start_assembly,
                assembly_to_parents,
                assembly_to_products,
                component_variant,
            ):
                if product_id not in products:
                    continue
                product_paths[(incident_id, product_id)].append(
                    {
                        "basis": "component_bom_path",
                        "depth_bucket": _depth_bucket(depth),
                        "item_type": "component",
                        "item_id": component_id,
                        "buffer_state": buffer_lookup.get(("component", component_id, product_id), "unknown"),
                        "summary": " -> ".join([component_id, *assembly_path, product_id]),
                    }
                )

    for impact in _list(input_payload, "impacted_assembly_edges"):
        row = _coerce_row(impact)
        incident_id = _edge_incident_id(row)
        assembly_id = _edge_assembly_id(row)
        for product_id, assembly_path, depth in _assembly_product_paths(
            assembly_id,
            assembly_to_parents,
            assembly_to_products,
            "all",
        ):
            if product_id not in products:
                continue
            product_paths[(incident_id, product_id)].append(
                {
                    "basis": "direct_assembly_bom_path",
                    "depth_bucket": _depth_bucket(depth),
                    "item_type": "assembly",
                    "item_id": assembly_id,
                    "buffer_state": buffer_lookup.get(("assembly", assembly_id, product_id), "unknown"),
                    "summary": " -> ".join([*assembly_path, product_id]),
                }
            )

    items: list[dict[str, Any]] = []
    for (incident_id, product_id), paths in sorted(product_paths.items()):
        basis_values = {path["basis"] for path in paths}
        basis = next(iter(basis_values)) if len(basis_values) == 1 else "mixed_paths"
        depth_bucket = max(
            (path["depth_bucket"] for path in paths),
            key=lambda value: _DEPTH_RANK[value],
        )
        buffer_state = max(
            (path["buffer_state"] for path in paths),
            key=lambda value: _BUFFER_STATE_RANK.get(value, _BUFFER_STATE_RANK["unknown"]),
        )
        verdict = (
            "support"
            if buffer_state in {"no_buffer", "partial_buffer"}
            else "unsure"
            if buffer_state == "unknown"
            else "contradict"
        )
        summaries = sorted({path["summary"] for path in paths})
        items.append(
            {
                "incident_id": incident_id,
                "product_id": product_id,
                "bom_depth_bucket": depth_bucket,
                "buffer_state": buffer_state,
                "exposure_basis": basis,
                "exposure_path_summary": "; ".join(summaries[:5]),
                "contributing_path_count": len(paths),
                "rationale": (
                    f"{len(paths)} accepted upstream impact path(s) reach product {product_id}; "
                    f"worst buffer_state={buffer_state}."
                ),
                "verdict": verdict,
            }
        )
    return {"items": _sorted_rows(items, "incident_id", "product_id")}


def _load_seed_json(filename: str, context: ProviderContext | None) -> dict[str, Any]:
    """SDK artifact loading; the dev-tree fallback serves direct calls in tests."""
    return load_artifact_json(context, filename, fallback_dir=_SEED_DIR)


def _list(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"'{key}' must be a list")
    return value


def _project(
    row: dict[str, Any],
    keys: tuple[str, ...],
    *,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = defaults or {}
    out: dict[str, Any] = {}
    for key in keys:
        if key in row:
            out[key] = row[key]
        elif key in defaults:
            out[key] = defaults[key]
        else:
            raise ValueError(f"Seed row missing required key '{key}': {row!r}")
    return out


def _sorted_rows(rows: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: tuple(str(row.get(key, "")) for key in keys))


def _expand_components(
    bundle: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    components: list[dict[str, Any]] = []
    bom_edges: list[dict[str, Any]] = []
    meta: dict[str, dict[str, Any]] = {}

    for row in bundle.get("components", []):
        component = _component_from_seed(row)
        component_id = component["component_id"]
        components.append(component)
        meta[component_id] = {"group_id": None, **row}
        bom_edges.extend(_component_bom_edges(component_id, row))

    for group in bundle.get("component_groups", []):
        count = int(group.get("count", 0))
        explicit_items = group.get("items")
        if explicit_items is not None and not isinstance(explicit_items, list):
            raise ValueError("component_groups[].items must be a list")
        group_items = explicit_items or [{"n": index} for index in range(1, count + 1)]
        for index, item in enumerate(group_items, start=1):
            if not isinstance(item, dict):
                raise ValueError("component group items must be objects")
            tokens = {**group, **item, "n": item.get("n", index), "nn": f"{int(item.get('n', index)):02d}"}
            component_id = item.get("component_id") or _format_template(
                str(group["component_id_template"]),
                tokens,
            )
            component = _component_from_seed(
                {
                    "component_id": component_id,
                    "name": item.get("name") or _format_template(str(group["name_template"]), tokens),
                    "component_kind": item.get("component_kind", group.get("component_kind", "part")),
                    "manufacturer": item.get("manufacturer", group.get("manufacturer")),
                    "mpn": item.get("mpn")
                    if "mpn" in item
                    else _format_template(str(group["mpn_template"]), tokens)
                    if group.get("mpn_template")
                    else group.get("mpn"),
                    "revision": item.get("revision", group.get("revision")),
                    "lifecycle_status": item.get("lifecycle_status", group.get("lifecycle_status", "active")),
                    "criticality": item.get("criticality", group.get("criticality", "standard")),
                    "category": item.get("category", group["category"]),
                }
            )
            components.append(component)
            meta[component_id] = {"group_id": group["group_id"], **group, **item}
            bom_edges.extend(_component_bom_edges(component_id, {**group, **item}))

    return components, _sorted_rows(bom_edges, "component_id", "assembly_id"), meta


def _component_from_seed(row: dict[str, Any]) -> dict[str, Any]:
    return _project(
        row,
        _COMPONENT_KEYS,
        defaults={
            "manufacturer": None,
            "mpn": None,
            "revision": None,
            "lifecycle_status": "active",
            "criticality": "standard",
        },
    )


def _component_bom_edges(component_id: str, row: dict[str, Any]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    edge_specs = row.get("bom_edges")
    if edge_specs is None:
        assembly_id = row.get("assembly_id")
        if not assembly_id:
            return edges
        edge_specs = [{"assembly_id": assembly_id, "quantity": row.get("quantity", 1)}]
    for edge_spec in edge_specs:
        edges.append(
            _project(
                {
                    **edge_spec,
                    "component_id": component_id,
                },
                ("component_id", "assembly_id", *_BOM_EDGE_KEYS),
                defaults={
                    "quantity": 1,
                    "bom_variant_id": row.get("bom_variant_id", "all"),
                    "plant_id": row.get("plant_id", "SEA-FINAL-01"),
                    "effective_from": row.get("effective_from", "2026-01-01"),
                    "effective_to": None,
                },
            )
        )
    return edges


def _expand_component_supply_edges(
    bundle: dict[str, Any],
    components: list[dict[str, Any]],
    component_meta: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rules = bundle.get("component_supply_rules", [])
    default_supplies = bundle.get("default_component_supply", [])
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for component in components:
        matched = False
        for rule in rules:
            if not _component_matches_rule(component, component_meta[component["component_id"]], rule.get("match", {})):
                continue
            matched = True
            for supplier in rule.get("suppliers", []):
                edge = _supply_edge({**supplier, "component_id": component["component_id"]})
                key = (edge["supplier_id"], edge["component_id"])
                if key not in seen:
                    edges.append(edge)
                    seen.add(key)
            if rule.get("stop"):
                break
        if matched:
            continue
        for supplier in default_supplies:
            edge = _supply_edge({**supplier, "component_id": component["component_id"]})
            key = (edge["supplier_id"], edge["component_id"])
            if key not in seen:
                edges.append(edge)
                seen.add(key)
    return edges


def _component_matches_rule(
    component: dict[str, Any],
    meta: dict[str, Any],
    match: dict[str, Any],
) -> bool:
    merged = {**meta, **component}
    for key, expected in match.items():
        actual = merged.get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _supply_edge(row: dict[str, Any]) -> dict[str, Any]:
    endpoint_keys = ["supplier_id"]
    if "component_id" in row:
        endpoint_keys.append("component_id")
    if "assembly_id" in row:
        endpoint_keys.append("assembly_id")
    return _project(
        row,
        tuple(endpoint_keys + list(_SUPPLY_EDGE_KEYS)),
        defaults={
            "lead_time_days": None,
            "qualification_status": "qualified",
            "sourcing_role": "primary",
            "priority_rank": 1,
            "allocation_pct": None,
            "activation_state": "active",
            "capacity_units_per_week": None,
            "effective_from": "2026-01-01",
            "effective_to": None,
            "last_verified_at": "2026-06-20",
        },
    )


def _format_template(template: str, tokens: dict[str, Any]) -> str:
    return template.format(**tokens)


def _filter_inventory_payload(payload: dict[str, Any], filters: dict[str, Any]) -> dict[str, Any]:
    item_ids = {str(value) for value in filters.get("item_ids") or []}
    item_types = {str(value) for value in filters.get("item_types") or []}
    location_ids = {str(value) for value in filters.get("location_ids") or []}
    as_of = str(filters.get("as_of") or "")

    positions = []
    for row in payload.get("inventory_positions", []):
        position = _project(
            row,
            _INVENTORY_POSITION_KEYS,
            defaults={
                "quantity_on_hand": None,
                "quantity_allocated": None,
                "net_available": None,
            },
        )
        if item_ids and position["item_id"] not in item_ids:
            continue
        if item_types and position["item_type"] not in item_types:
            continue
        if location_ids and position["location_id"] not in location_ids:
            continue
        if as_of and position["as_of"] != as_of:
            continue
        if position["net_available"] is None:
            on_hand = _float_or_none(position["quantity_on_hand"]) or 0.0
            allocated = _float_or_none(position["quantity_allocated"]) or 0.0
            position["net_available"] = on_hand - allocated
        positions.append(position)

    kept_position_ids = {row["inventory_position_id"] for row in positions}
    kept_location_ids = {row["location_id"] for row in positions}
    locations = [
        _project(row, _LOCATION_KEYS)
        for row in payload.get("locations", [])
        if row.get("location_id") in kept_location_ids
    ]

    def edge_kept(row: dict[str, Any]) -> bool:
        return row.get("inventory_position_id") in kept_position_ids

    return {
        "locations": _sorted_rows(locations, "location_id"),
        "inventory_positions": _sorted_rows(positions, "inventory_position_id"),
        "component_inventory_positions": _sorted_rows(
            [
                _project(row, ("component_id", "inventory_position_id"))
                for row in payload.get("component_inventory_positions", [])
                if edge_kept(row)
            ],
            "component_id",
            "inventory_position_id",
        ),
        "assembly_inventory_positions": _sorted_rows(
            [
                _project(row, ("assembly_id", "inventory_position_id"))
                for row in payload.get("assembly_inventory_positions", [])
                if edge_kept(row)
            ],
            "assembly_id",
            "inventory_position_id",
        ),
        "product_inventory_positions": _sorted_rows(
            [
                _project(row, ("product_id", "inventory_position_id"))
                for row in payload.get("product_inventory_positions", [])
                if edge_kept(row)
            ],
            "product_id",
            "inventory_position_id",
        ),
        "inventory_position_locations": _sorted_rows(
            [
                _project(row, ("inventory_position_id", "location_id"))
                for row in payload.get("inventory_position_locations", [])
                if edge_kept(row)
            ],
            "inventory_position_id",
            "location_id",
        ),
    }


def _demand_context(value: Any, context: ProviderContext) -> dict[str, Any]:
    if isinstance(value, dict) and value:
        return value
    return _load_seed_json("demand_context.json", context)


def _product_requirements(
    *,
    component_edges: list[dict[str, Any]],
    assembly_edges: list[dict[str, Any]],
    product_edges: list[dict[str, Any]],
    known_components: set[str],
    known_assemblies: set[str],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    component_by_assembly = defaultdict(list)
    for edge in component_edges:
        component_id = _edge_component_id(edge)
        if component_id in known_components:
            component_by_assembly[_edge_assembly_id(edge)].append((component_id, _quantity(edge), _bom_variant(edge)))

    child_assembly_by_parent = defaultdict(list)
    for edge in assembly_edges:
        child_id = _edge_child_assembly_id(edge)
        if child_id in known_assemblies:
            child_assembly_by_parent[_edge_parent_assembly_id(edge)].append(
                (child_id, _quantity(edge), _bom_variant(edge))
            )

    component_requirements: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    assembly_requirements: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for edge in product_edges:
        product_id = _edge_product_id(edge)
        root_assembly = _edge_assembly_id(edge)
        root_quantity = _quantity(edge)
        product_variant = _bom_variant(edge)
        queue: deque[tuple[str, float]] = deque([(root_assembly, root_quantity)])
        while queue:
            assembly_id, multiplier = queue.popleft()
            assembly_requirements[product_id][assembly_id] += multiplier
            for component_id, component_quantity, component_variant in component_by_assembly.get(assembly_id, []):
                if not _variant_matches(component_variant, product_variant):
                    continue
                component_requirements[product_id][component_id] += multiplier * component_quantity
            for child_id, child_quantity, child_variant in child_assembly_by_parent.get(assembly_id, []):
                if not _variant_matches(child_variant, product_variant):
                    continue
                queue.append((child_id, multiplier * child_quantity))

    return component_requirements, assembly_requirements


def _buffer_row(
    *,
    item_type: str,
    item_id: str,
    product_id: str,
    requirement: float,
    open_demand_units: float,
    position_ids: list[str],
    positions: dict[str, dict[str, Any]],
    demand: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    linked_positions = sorted(position_id for position_id in position_ids if position_id in positions)
    net_available = (
        sum(_net_available(positions[position_id]) for position_id in linked_positions)
        if linked_positions
        else None
    )
    unit = _first_present(
        *[positions[position_id].get("unit_of_measure") for position_id in linked_positions],
        "ea",
    )
    horizon_duration = float(demand.get("horizon_duration", 14))
    horizon_unit = str(demand.get("horizon_unit", "days"))
    as_of = str(demand.get("as_of", "2026-07-01"))
    rate_period = "day"
    estimated_rate = _item_rate(demand, item_type, item_id)
    if estimated_rate is None:
        estimated_rate = (open_demand_units * requirement / horizon_duration) if horizon_duration else 0.0
    coverage_duration = (
        (net_available / estimated_rate)
        if net_available is not None and estimated_rate and estimated_rate > 0
        else None
    )
    if not linked_positions:
        state = "unknown"
    elif net_available is not None and net_available <= 0:
        state = "no_buffer"
    elif estimated_rate is None or estimated_rate <= 0:
        state = "sufficient_buffer" if open_demand_units <= 0 or requirement <= 0 else "unknown"
    elif coverage_duration is not None and coverage_duration >= horizon_duration:
        state = "sufficient_buffer"
    else:
        state = "partial_buffer"
    buffer_assessment_id = _stable_assessment_id(item_type, item_id, product_id, as_of)
    row = {
        "buffer_assessment_id": buffer_assessment_id,
        "item_type": item_type,
        "item_id": item_id,
        "product_id": product_id,
        "bom_variant_id": _product_bom_variant_id(demand, product_id),
        "net_available": round(net_available, 3) if net_available is not None else None,
        "required_per_unit": round(requirement, 3),
        "open_demand_units": round(open_demand_units, 3),
        "estimated_consumption_rate": round(estimated_rate, 3) if estimated_rate is not None else None,
        "rate_period": rate_period,
        "coverage_duration": round(coverage_duration, 3) if coverage_duration is not None else None,
        "coverage_unit": "days",
        "horizon_duration": horizon_duration,
        "horizon_unit": horizon_unit,
        "buffer_state": state,
        "unit_of_measure": str(unit),
        "as_of": as_of,
        "rationale": (
            f"{item_type} {item_id} has "
            f"{net_available:g} {unit} net available"
            if net_available is not None
            else f"{item_type} {item_id} has no linked inventory positions"
        )
        + (
            f" for {open_demand_units:g} open {product_id} unit(s)."
            if net_available is not None
            else f" for {product_id}."
        ),
    }
    return row, linked_positions


def _stable_assessment_id(item_type: str, item_id: str, product_id: str, as_of: str) -> str:
    return f"BUF-{item_type.upper()}-{item_id}-{product_id}-{as_of}".replace("_", "-")


def _product_demand_units(demand: dict[str, Any], product_id: str) -> float:
    by_product = demand.get("product_open_demand_units", {})
    if isinstance(by_product, dict):
        return _float_or_none(by_product.get(product_id)) or 0.0
    return 0.0


def _product_bom_variant_id(demand: dict[str, Any], product_id: str) -> str:
    by_product = demand.get("product_bom_variant_ids", {})
    if isinstance(by_product, dict) and by_product.get(product_id):
        return str(by_product[product_id])
    return str(demand.get("bom_variant_id", "voron-2.4-r2-350"))


def _item_rate(demand: dict[str, Any], item_type: str, item_id: str) -> float | None:
    rates = demand.get("item_consumption_rates", {})
    if not isinstance(rates, dict):
        return None
    value = rates.get(f"{item_type}:{item_id}") or rates.get(item_id)
    if isinstance(value, dict):
        return _float_or_none(value.get("rate"))
    return _float_or_none(value)


def _net_available(position: dict[str, Any]) -> float:
    net = _float_or_none(_value(position, "net_available"))
    if net is not None:
        return net
    on_hand = _float_or_none(_value(position, "quantity_on_hand")) or 0.0
    allocated = _float_or_none(_value(position, "quantity_allocated")) or 0.0
    return on_hand - allocated


def _quantity(row: dict[str, Any]) -> float:
    return _float_or_none(_value(row, "quantity")) or 1.0


def _bom_variant(row: dict[str, Any]) -> str:
    return str(_value(row, "bom_variant_id") or "all")


def _variant_matches(edge_variant: str, product_variant: str) -> bool:
    if edge_variant in {"all", "common", "*"}:
        return True
    return edge_variant == product_variant or product_variant.startswith(f"{edge_variant}-")


def _impacted_suppliers_by_incident(rows: Any) -> dict[str, set[str]]:
    impacted: dict[str, set[str]] = defaultdict(set)
    if not isinstance(rows, list):
        return impacted
    for raw in rows:
        row = _coerce_row(raw)
        impacted[_edge_incident_id(row)].add(_edge_supplier_id(row))
    return impacted


def _alternate_state_and_verdict(
    supply_edges: list[dict[str, Any]],
    affected_suppliers: set[str],
) -> tuple[str, str]:
    viable_edges = [edge for edge in supply_edges if _is_viable_supply(edge)]
    outside = [edge for edge in viable_edges if _edge_supplier_id(edge) not in affected_suppliers]
    if outside:
        return "alternate_viable", "unsure"
    in_scope = [edge for edge in viable_edges if _edge_supplier_id(edge) in affected_suppliers]
    if len(in_scope) > 1:
        return "alternate_in_scope", "support"
    return "no_alternate", "support"


def _is_viable_supply(edge: dict[str, Any]) -> bool:
    return (
        _value(edge, "qualification_status") in {"qualified", "conditional"}
        and _value(edge, "activation_state") in {"active", "standby", "constrained"}
        and _value(edge, "sourcing_role") != "inactive"
    )


def _assembly_product_paths(
    start_assembly: str,
    assembly_to_parents: dict[str, list[dict[str, Any]]],
    assembly_to_products: dict[str, list[dict[str, Any]]],
    path_variant: str,
) -> list[tuple[str, list[str], int]]:
    paths: list[tuple[str, list[str], int]] = []
    queue: deque[tuple[str, list[str], int]] = deque([(start_assembly, [start_assembly], 0)])
    while queue:
        assembly_id, path, depth = queue.popleft()
        for product_edge in assembly_to_products.get(assembly_id, []):
            product_variant = _bom_variant(product_edge)
            if not _variant_matches(path_variant, product_variant):
                continue
            paths.append((_edge_product_id(product_edge), list(path), depth))
        for parent_edge in sorted(assembly_to_parents.get(assembly_id, []), key=_edge_parent_assembly_id):
            if path_variant not in {"all", "common", "*"} and not _variant_matches(
                _bom_variant(parent_edge),
                path_variant,
            ):
                continue
            parent_id = _edge_parent_assembly_id(parent_edge)
            if parent_id in path:
                continue
            queue.append((parent_id, [*path, parent_id], depth + 1))
    return paths


def _depth_bucket(depth: int) -> str:
    if depth <= 0:
        return "direct"
    if depth == 1:
        return "tier_2"
    return "tier_3_plus"


def _buffer_lookup(input_payload: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    assessments = {
        _entity_id(row, "BufferAssessment"): _coerce_row(row)
        for row in _list(input_payload, "buffer_assessments")
    }
    product_by_assessment: dict[str, str] = {}
    for edge in _list(input_payload, "product_buffer_assessment_edges"):
        row = _coerce_row(edge)
        product_by_assessment[_edge_buffer_assessment_id(row)] = _edge_product_id(row)

    lookup: dict[tuple[str, str, str], str] = {}
    for item_type, edge_key in (
        ("component", "component_buffer_assessment_edges"),
        ("assembly", "assembly_buffer_assessment_edges"),
    ):
        for edge in _list(input_payload, edge_key):
            row = _coerce_row(edge)
            assessment_id = _edge_buffer_assessment_id(row)
            product_id = product_by_assessment.get(assessment_id)
            assessment = assessments.get(assessment_id)
            if product_id is None or assessment is None:
                continue
            item_id = _edge_component_id(row) if item_type == "component" else _edge_assembly_id(row)
            lookup[(item_type, item_id, product_id)] = str(
                _value(assessment, "buffer_state") or "unknown"
            )
    return lookup


def _coerce_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if hasattr(row, "model_dump"):
        return row.model_dump(mode="json")
    raise ValueError(f"Rows must be objects, got {type(row).__name__}")


def _value(row: dict[str, Any], key: str) -> Any:
    if key in row:
        return row[key]
    props = row.get("properties")
    if isinstance(props, dict) and key in props:
        return props[key]
    return None


def _entity_id(row: Any, entity_type: str) -> str:
    row = _coerce_row(row)
    id_key = {
        "Assembly": "assembly_id",
        "BufferAssessment": "buffer_assessment_id",
        "Component": "component_id",
        "Incident": "incident_id",
        "InventoryPosition": "inventory_position_id",
        "Product": "product_id",
        "Risk": "risk_id",
        "Supplier": "supplier_id",
        "WorkItem": "work_item_id",
    }.get(entity_type, f"{entity_type.lower()}_id")
    value = row.get(id_key) or row.get("entity_id") or _value(row, id_key)
    if value is None:
        raise ValueError(f"Could not determine {entity_type} id from row: {row!r}")
    return str(value)


def _edge_supplier_id(row: dict[str, Any]) -> str:
    if row.get("supplier_id") is not None:
        return str(row["supplier_id"])
    if row.get("to_type") == "Supplier" and row.get("to_id") is not None:
        return str(row["to_id"])
    if row.get("from_type") == "Supplier" and row.get("from_id") is not None:
        return str(row["from_id"])
    raise ValueError(f"Could not determine Supplier id from edge row: {row!r}")


def _edge_incident_id(row: dict[str, Any]) -> str:
    if row.get("incident_id") is not None:
        return str(row["incident_id"])
    if row.get("from_type") == "Incident" and row.get("from_id") is not None:
        return str(row["from_id"])
    if row.get("to_type") == "Incident" and row.get("to_id") is not None:
        return str(row["to_id"])
    raise ValueError(f"Could not determine Incident id from edge row: {row!r}")


def _edge_component_id(row: dict[str, Any]) -> str:
    if row.get("component_id") is not None:
        return str(row["component_id"])
    if row.get("to_type") == "Component" and row.get("to_id") is not None:
        return str(row["to_id"])
    if row.get("from_type") == "Component" and row.get("from_id") is not None:
        return str(row["from_id"])
    raise ValueError(f"Could not determine Component id from edge row: {row!r}")


def _edge_assembly_id(row: dict[str, Any]) -> str:
    if row.get("assembly_id") is not None:
        return str(row["assembly_id"])
    if row.get("to_type") == "Assembly" and row.get("to_id") is not None:
        return str(row["to_id"])
    if row.get("from_type") == "Assembly" and row.get("from_id") is not None:
        return str(row["from_id"])
    raise ValueError(f"Could not determine Assembly id from edge row: {row!r}")


def _edge_child_assembly_id(row: dict[str, Any]) -> str:
    if row.get("child_assembly_id") is not None:
        return str(row["child_assembly_id"])
    if row.get("from_type") == "Assembly" and row.get("from_id") is not None:
        return str(row["from_id"])
    raise ValueError(f"Could not determine child Assembly id from edge row: {row!r}")


def _edge_parent_assembly_id(row: dict[str, Any]) -> str:
    if row.get("parent_assembly_id") is not None:
        return str(row["parent_assembly_id"])
    if row.get("to_type") == "Assembly" and row.get("to_id") is not None:
        return str(row["to_id"])
    raise ValueError(f"Could not determine parent Assembly id from edge row: {row!r}")


def _edge_product_id(row: dict[str, Any]) -> str:
    if row.get("product_id") is not None:
        return str(row["product_id"])
    if row.get("to_type") == "Product" and row.get("to_id") is not None:
        return str(row["to_id"])
    if row.get("from_type") == "Product" and row.get("from_id") is not None:
        return str(row["from_id"])
    raise ValueError(f"Could not determine Product id from edge row: {row!r}")


def _edge_inventory_position_id(row: dict[str, Any]) -> str:
    if row.get("inventory_position_id") is not None:
        return str(row["inventory_position_id"])
    if row.get("to_type") == "InventoryPosition" and row.get("to_id") is not None:
        return str(row["to_id"])
    if row.get("from_type") == "InventoryPosition" and row.get("from_id") is not None:
        return str(row["from_id"])
    raise ValueError(f"Could not determine InventoryPosition id from edge row: {row!r}")


def _edge_buffer_assessment_id(row: dict[str, Any]) -> str:
    if row.get("buffer_assessment_id") is not None:
        return str(row["buffer_assessment_id"])
    if row.get("to_type") == "BufferAssessment" and row.get("to_id") is not None:
        return str(row["to_id"])
    if row.get("from_type") == "BufferAssessment" and row.get("from_id") is not None:
        return str(row["from_id"])
    raise ValueError(f"Could not determine BufferAssessment id from edge row: {row!r}")


def _geography_matches(scope: str, supplier_geo: str) -> bool:
    scope_tokens = [part.strip().lower() for part in scope.split(",") if part.strip()]
    supplier = supplier_geo.lower()
    return bool(scope_tokens) and all(token in supplier for token in scope_tokens)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
