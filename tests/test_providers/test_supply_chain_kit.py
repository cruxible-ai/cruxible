"""Direct provider tests for the supply-chain blast-radius kit."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

os.environ.setdefault("CRUXIBLE_KIT_DEV_RESOLVE", "1")

from cruxible_core.config.loader import load_config
from cruxible_core.provider.types import ProviderContext, ResolvedArtifact

ROOT = Path(__file__).resolve().parents[2]
KIT_DIR = ROOT / "kits" / "supply-chain-blast-radius"
SEED_DIR = KIT_DIR / "data" / "seed"
BOM_DIR = SEED_DIR / "bom"
PROVIDER_PATH = KIT_DIR / "providers" / "supply_chain_blast_radius.py"
CONFIG = load_config(str(KIT_DIR / "config.yaml"))


def _load_provider() -> ModuleType:
    spec = importlib.util.spec_from_file_location("supply_chain_blast_radius_provider", PROVIDER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provider = _load_provider()


def _context(provider_name: str, *, artifact: bool = True) -> ProviderContext:
    return ProviderContext(
        workflow_name="test",
        step_id="step",
        provider_name=provider_name,
        provider_version="1.0.0",
        artifact=ResolvedArtifact(
            name="supply_chain_seed_bundle",
            kind="directory",
            uri="./data/seed",
            local_path=str(SEED_DIR),
            digest="sha256:test",
        )
        if artifact
        else None,
    )


def _entity_property_keys(entity_type: str) -> set[str]:
    return set(CONFIG.entity_types[entity_type].properties)


def _relationship_property_keys(relationship_type: str) -> set[str]:
    relationship = CONFIG.get_relationship(relationship_type)
    assert relationship is not None
    return set(relationship.properties)


def _assert_keys(rows: list[dict[str, Any]], keys: set[str]) -> None:
    assert rows
    for row in rows:
        assert keys <= set(row), row


def _normalize_evidence(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _component_evidence_phrases(component: dict[str, Any]) -> set[str]:
    name = _normalize_evidence(component["name"])
    category = _normalize_evidence(component["category"])
    phrases: set[str] = set()

    manufacturer = component.get("manufacturer")
    if manufacturer:
        phrases.add(_normalize_evidence(manufacturer))
    mpn = component.get("mpn")
    if mpn:
        phrases.add(_normalize_evidence(mpn))

    if "gates" in name and "belt" in name:
        phrases.add("gates belt")
    if "igus" in name or "cable carrier" in name or "cable chain" in name:
        phrases.add("igus")
    if "misumi" in name or component.get("manufacturer") == "Misumi":
        phrases.add("misumi")
        phrases.add("extrusions")
    if "linear rail" in name or category == "rail":
        phrases.add("linear rail")
    if "hiwin" in name:
        phrases.add("hiwin")
    if "mgn9h" in name:
        phrases.add("mgn9h")
    if "mgn12h" in name or "mgn12" in name:
        phrases.add("mgn12")
    if "printed" in name or category == "printed part":
        phrases.add("printed parts")
    if "panel" in name:
        phrases.add("panels")
    if "ac" in name and "heater" in name:
        phrases.add("ac heater")
    if "dragon" in name and "high flow" in name:
        phrases.add("dragon standard flow or high flow")
    if "raspberry pi" in name:
        phrases.add("raspberry pi")
    if "24 awg" in name:
        phrases.add("24 awg")
    if "18 awg" in name:
        phrases.add("18 awg")
    if "ptfe" in name:
        phrases.add("ptfe")
    if "silicone" in name:
        phrases.add("silicone wire")
    if "mic6" in name or "cast aluminum" in name:
        phrases.add("mic6 aluminum")

    return {phrase for phrase in phrases if phrase}


def _relationship_row(
    relationship_type: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "relationship_type": relationship_type,
        "from_type": from_type,
        "from_id": from_id,
        "to_type": to_type,
        "to_id": to_id,
        "properties": properties or {},
    }


def test_seed_loader_expands_voron_bom_and_row_contracts() -> None:
    payload = provider.load_seed_data({}, _context("load_supply_chain_seed_data"))

    assert len(payload["components"]) == 206
    assert len(payload["suppliers"]) == 13
    assert len(payload["products"]) == 3

    _assert_keys(payload["suppliers"], _entity_property_keys("Supplier"))
    _assert_keys(payload["components"], _entity_property_keys("Component"))
    _assert_keys(payload["assemblies"], _entity_property_keys("Assembly"))
    _assert_keys(payload["products"], _entity_property_keys("Product"))
    _assert_keys(payload["shipments"], _entity_property_keys("Shipment"))

    _assert_keys(
        payload["supplier_supplies_component"],
        _relationship_property_keys("supplier_supplies_component"),
    )
    _assert_keys(
        payload["supplier_supplies_assembly"],
        _relationship_property_keys("supplier_supplies_assembly"),
    )
    _assert_keys(
        payload["component_part_of_assembly"],
        _relationship_property_keys("component_part_of_assembly"),
    )
    _assert_keys(
        payload["assembly_part_of_assembly"],
        _relationship_property_keys("assembly_part_of_assembly"),
    )
    _assert_keys(
        payload["assembly_part_of_product"],
        _relationship_property_keys("assembly_part_of_product"),
    )
    _assert_keys(payload["product_in_shipment"], _relationship_property_keys("product_in_shipment"))

    ldo_z = next(row for row in payload["components"] if row["component_id"] == "C-MOTOR-LDO-42STH40-1684AC")
    assert ldo_z["manufacturer"] is None
    assert ldo_z["mpn"] is None
    ldo_ab = next(row for row in payload["components"] if row["component_id"] == "C-MOTOR-LDO-42STH48-2004MAH")
    assert ldo_ab["manufacturer"] is None
    assert ldo_ab["mpn"] is None
    assert not any(row["component_id"] == "C-MOTION-LEADSCREW-T8-350" for row in payload["components"])


def test_component_provenance_traces_to_pinned_voron_artifacts() -> None:
    bundle = json.loads((SEED_DIR / "bundle.json").read_text())
    manifest = json.loads((BOM_DIR / "manifest.json").read_text())
    corpus_parts: list[str] = []

    for file_meta in manifest["files"]:
        artifact_path = BOM_DIR / file_meta["path"]
        artifact_bytes = artifact_path.read_bytes()
        assert len(artifact_bytes) == file_meta["size_bytes"]
        assert hashlib.sha256(artifact_bytes).hexdigest() == file_meta["sha256"]
        corpus_parts.append(artifact_bytes.decode())

    corpus = _normalize_evidence("\n".join(corpus_parts))
    components, _bom_edges, component_meta = provider._expand_components(bundle)

    assert len(components) == 206
    by_provenance = {"voron_bom": 0, "synthetic": 0}
    synthetic_real_claims: list[str] = []
    untraced_voron_claims: list[str] = []

    for component in components:
        meta = component_meta[component["component_id"]]
        provenance = meta.get("provenance")
        assert provenance in by_provenance, component
        by_provenance[provenance] += 1

        if provenance == "synthetic":
            if component.get("manufacturer") and component.get("mpn"):
                synthetic_real_claims.append(component["component_id"])
            continue

        evidence_phrases = _component_evidence_phrases(component)
        if not any(phrase in corpus for phrase in evidence_phrases):
            untraced_voron_claims.append(component["component_id"])

    assert not synthetic_real_claims
    assert not untraced_voron_claims
    assert by_provenance == {"voron_bom": 96, "synthetic": 110}


def test_incident_feed_and_supplier_scope_rows_match_auto_properties() -> None:
    seed = provider.load_seed_data({}, _context("load_supply_chain_seed_data"))
    feed = provider.load_incident_feed({}, _context("load_incident_feed"))

    _assert_keys(feed["incidents"], _entity_property_keys("Incident"))
    assert {row["incident_id"] for row in feed["incidents"]} >= {
        "INC-GD-STEPPER-2026-07",
        "INC-TW-RAIL-2026-07",
    }

    assessment = provider.assess_incident_supplier_scope(
        {"incidents": feed["incidents"], "suppliers": seed["suppliers"]},
        _context("assess_incident_supplier_scope", artifact=False),
    )
    _assert_keys(assessment["items"], _relationship_property_keys("incident_impacts_supplier"))
    assert "verdict" in assessment["items"][0]
    assert any(
        row["incident_id"] == "INC-GD-STEPPER-2026-07"
        and row["supplier_id"] == "S-CN-GD-STEPPER"
        and row["match_basis"] == "geography"
        and row["verdict"] == "unsure"
        for row in assessment["items"]
    )


def test_supplier_scope_verdicts_follow_direct_and_reviewable_geography_paths() -> None:
    assessment = provider.assess_incident_supplier_scope(
        {
            "incidents": [
                {
                    "incident_id": "INC-DIRECT",
                    "title": "Direct supplier incident",
                    "severity": "low",
                    "scope_type": "supplier",
                    "scope_id": "S-DIRECT",
                    "status": "open",
                    "reported_at": "2026-07-01",
                    "closed_at": None,
                    "summary": None,
                },
                {
                    "incident_id": "INC-GEO",
                    "title": "Regional supplier incident",
                    "severity": "low",
                    "scope_type": "geography",
                    "scope_id": "Metro",
                    "status": "open",
                    "reported_at": "2026-07-01",
                    "closed_at": None,
                    "summary": None,
                },
            ],
            "suppliers": [
                {
                    "supplier_id": "S-DIRECT",
                    "name": "Direct Supplier",
                    "primary_geography": "Elsewhere",
                    "tier_hint": None,
                },
                {
                    "supplier_id": "S-GEO",
                    "name": "Geography Supplier",
                    "primary_geography": "Metro Region",
                    "tier_hint": None,
                },
            ],
        },
        _context("assess_incident_supplier_scope", artifact=False),
    )

    by_pair = {(row["incident_id"], row["supplier_id"]): row for row in assessment["items"]}
    assert by_pair[("INC-DIRECT", "S-DIRECT")]["match_basis"] == "direct"
    assert by_pair[("INC-DIRECT", "S-DIRECT")]["verdict"] == "support"
    assert by_pair[("INC-GEO", "S-GEO")]["match_basis"] == "geography"
    assert by_pair[("INC-GEO", "S-GEO")]["verdict"] == "unsure"


def test_load_inventory_positions_fixture_filters_and_shapes_rows() -> None:
    payload = provider.load_inventory_positions(
        {
            "item_ids": ["C-MOTOR-LDO-42STH40-1684AC"],
            "item_types": ["component"],
            "location_ids": [],
            "as_of": "2026-07-01",
        },
        _context("load_inventory_positions"),
    )

    assert [row["item_id"] for row in payload["inventory_positions"]] == [
        "C-MOTOR-LDO-42STH40-1684AC",
        "C-MOTOR-LDO-42STH40-1684AC",
    ]
    _assert_keys(payload["locations"], _entity_property_keys("Location"))
    _assert_keys(payload["inventory_positions"], _entity_property_keys("InventoryPosition"))
    assert payload["component_inventory_positions"]
    assert payload["inventory_position_locations"]


def test_buffer_coverage_computes_days_of_cover_and_edges() -> None:
    payload = provider.assess_buffer_coverage(
        {
            "components": [
                {
                    "entity_type": "Component",
                    "entity_id": "C-MOTOR-LDO-42STH40-1684AC",
                    "properties": {
                        "name": "Z motor",
                        "component_kind": "part",
                        "manufacturer": "LDO Motors",
                        "mpn": "42STH40-1684AC",
                        "revision": None,
                        "lifecycle_status": "active",
                        "criticality": "critical",
                        "category": "motor",
                    },
                }
            ],
            "assemblies": [
                {
                    "entity_type": "Assembly",
                    "entity_id": "A-Z",
                    "properties": {
                        "name": "Z drive",
                        "revision": None,
                        "lifecycle_status": "active",
                        "criticality": "critical",
                        "category": "motion",
                    },
                }
            ],
            "products": [
                {
                    "entity_type": "Product",
                    "entity_id": "P-350",
                    "properties": {
                        "sku": "SKU-350",
                        "name": "350 kit",
                        "lifecycle_status": "active",
                    },
                }
            ],
            "inventory_positions": [
                {
                    "entity_type": "InventoryPosition",
                    "entity_id": "INV-Z",
                    "properties": {
                        "item_type": "component",
                        "item_id": "C-MOTOR-LDO-42STH40-1684AC",
                        "location_id": "L-1",
                        "quantity_on_hand": 6.0,
                        "quantity_allocated": 2.0,
                        "net_available": 4.0,
                        "unit_of_measure": "ea",
                        "as_of": "2026-07-01",
                    },
                }
            ],
            "component_inventory_position_edges": [
                _relationship_row(
                    "component_inventory_position",
                    "Component",
                    "C-MOTOR-LDO-42STH40-1684AC",
                    "InventoryPosition",
                    "INV-Z",
                )
            ],
            "assembly_inventory_position_edges": [],
            "component_part_of_assembly_edges": [
                _relationship_row(
                    "component_part_of_assembly",
                    "Component",
                    "C-MOTOR-LDO-42STH40-1684AC",
                    "Assembly",
                    "A-Z",
                    {"quantity": 4},
                )
            ],
            "assembly_part_of_assembly_edges": [],
            "assembly_part_of_product_edges": [
                _relationship_row(
                    "assembly_part_of_product",
                    "Assembly",
                    "A-Z",
                    "Product",
                    "P-350",
                    {"quantity": 1},
                )
            ],
            "demand_context": {
                "as_of": "2026-07-01",
                "horizon_duration": 14,
                "horizon_unit": "days",
                "product_open_demand_units": {"P-350": 8},
            },
        },
        _context("assess_buffer_coverage", artifact=False),
    )

    _assert_keys(payload["buffer_assessments"], _entity_property_keys("BufferAssessment"))
    component_row = next(
        row for row in payload["buffer_assessments"] if row["item_type"] == "component"
    )
    assert component_row["buffer_state"] == "partial_buffer"
    assert component_row["required_per_unit"] == 4.0
    assert payload["component_buffer_assessments"]
    assert payload["product_buffer_assessments"]
    assert payload["buffer_assessment_inventory"]


def test_component_and_assembly_cascade_inline_payloads() -> None:
    impacted_supplier_edges = [
        _relationship_row(
            "incident_impacts_supplier",
            "Incident",
            "INC-1",
            "Supplier",
            "S-CN-GD-STEPPER",
            {"match_basis": "direct", "rationale": "direct supplier outage"},
        )
    ]
    component_payload = provider.assess_incident_component_cascade(
        {
            "impacted_supplier_edges": impacted_supplier_edges,
            "supplier_supplies_component_edges": [
                _relationship_row(
                    "supplier_supplies_component",
                    "Supplier",
                    "S-CN-GD-STEPPER",
                    "Component",
                    "C-MOTOR",
                    {
                        "qualification_status": "qualified",
                        "activation_state": "active",
                        "sourcing_role": "primary",
                    },
                ),
                _relationship_row(
                    "supplier_supplies_component",
                    "Supplier",
                    "S-CN-GD-STEPPER",
                    "Component",
                    "C-BOARD",
                    {
                        "qualification_status": "qualified",
                        "activation_state": "active",
                        "sourcing_role": "primary",
                    },
                ),
                _relationship_row(
                    "supplier_supplies_component",
                    "Supplier",
                    "S-US-DIST-MOTION",
                    "Component",
                    "C-BOARD",
                    {
                        "qualification_status": "conditional",
                        "activation_state": "standby",
                        "sourcing_role": "secondary",
                    },
                ),
            ],
            "components": [
                {
                    "entity_type": "Component",
                    "entity_id": "C-MOTOR",
                    "properties": {"lifecycle_status": "active"},
                },
                {
                    "entity_type": "Component",
                    "entity_id": "C-BOARD",
                    "properties": {"lifecycle_status": "active"},
                },
            ],
        },
        _context("assess_incident_component_cascade", artifact=False),
    )
    _assert_keys(component_payload["items"], _relationship_property_keys("incident_impacts_component"))
    by_component = {row["component_id"]: row for row in component_payload["items"]}
    assert by_component["C-MOTOR"]["alternate_state"] == "no_alternate"
    assert by_component["C-MOTOR"]["verdict"] == "support"
    assert by_component["C-BOARD"]["alternate_state"] == "alternate_viable"
    assert by_component["C-BOARD"]["verdict"] == "unsure"

    assembly_payload = provider.assess_incident_assembly_cascade(
        {
            "impacted_supplier_edges": [
                _relationship_row(
                    "incident_impacts_supplier",
                    "Incident",
                    "INC-2",
                    "Supplier",
                    "S-CN-TOOLHEAD",
                )
            ],
            "supplier_supplies_assembly_edges": [
                _relationship_row(
                    "supplier_supplies_assembly",
                    "Supplier",
                    "S-CN-TOOLHEAD",
                    "Assembly",
                    "A-TOOLHEAD",
                    {
                        "qualification_status": "qualified",
                        "activation_state": "active",
                        "sourcing_role": "primary",
                    },
                )
            ],
            "assemblies": [
                {
                    "entity_type": "Assembly",
                    "entity_id": "A-TOOLHEAD",
                    "properties": {"lifecycle_status": "active"},
                }
            ],
        },
        _context("assess_incident_assembly_cascade", artifact=False),
    )
    _assert_keys(assembly_payload["items"], _relationship_property_keys("incident_impacts_assembly"))
    assert assembly_payload["items"][0]["impacted_supplier_id"] == "S-CN-TOOLHEAD"
    assert assembly_payload["items"][0]["verdict"] == "support"


def test_product_exposure_inline_payload_uses_bom_and_buffer_context() -> None:
    payload = provider.assess_incident_product_exposure(
        {
            "impacted_component_edges": [
                _relationship_row(
                    "incident_impacts_component",
                    "Incident",
                    "INC-1",
                    "Component",
                    "C-MOTOR",
                    {"alternate_state": "no_alternate", "rationale": "single source"},
                )
            ],
            "impacted_assembly_edges": [],
            "component_part_of_assembly_edges": [
                _relationship_row(
                    "component_part_of_assembly",
                    "Component",
                    "C-MOTOR",
                    "Assembly",
                    "A-Z",
                    {"quantity": 4},
                )
            ],
            "assembly_part_of_assembly_edges": [],
            "assembly_part_of_product_edges": [
                _relationship_row(
                    "assembly_part_of_product",
                    "Assembly",
                    "A-Z",
                    "Product",
                    "P-350",
                    {"quantity": 1},
                )
            ],
            "buffer_assessments": [
                {
                    "entity_type": "BufferAssessment",
                    "entity_id": "BUF-1",
                    "properties": {
                        "item_type": "component",
                        "item_id": "C-MOTOR",
                        "product_id": "P-350",
                        "bom_variant_id": "v",
                        "net_available": 0.0,
                        "required_per_unit": 4.0,
                        "open_demand_units": 8.0,
                        "estimated_consumption_rate": 2.0,
                        "rate_period": "day",
                        "coverage_duration": 0.0,
                        "coverage_unit": "days",
                        "horizon_duration": 14.0,
                        "horizon_unit": "days",
                        "buffer_state": "no_buffer",
                        "unit_of_measure": "ea",
                        "as_of": "2026-07-01",
                        "rationale": "none available",
                    },
                }
            ],
            "component_buffer_assessment_edges": [
                _relationship_row(
                    "component_buffer_assessment",
                    "Component",
                    "C-MOTOR",
                    "BufferAssessment",
                    "BUF-1",
                )
            ],
            "assembly_buffer_assessment_edges": [],
            "product_buffer_assessment_edges": [
                _relationship_row(
                    "product_buffer_assessment",
                    "Product",
                    "P-350",
                    "BufferAssessment",
                    "BUF-1",
                )
            ],
            "assemblies": [
                {
                    "entity_type": "Assembly",
                    "entity_id": "A-Z",
                    "properties": {"name": "Z drive"},
                }
            ],
            "products": [
                {
                    "entity_type": "Product",
                    "entity_id": "P-350",
                    "properties": {"sku": "SKU", "name": "350", "lifecycle_status": "active"},
                }
            ],
        },
        _context("assess_incident_product_exposure", artifact=False),
    )

    assert payload["items"] == [
        {
            "incident_id": "INC-1",
            "product_id": "P-350",
            "bom_depth_bucket": "direct",
            "buffer_state": "no_buffer",
            "exposure_basis": "component_bom_path",
            "exposure_path_summary": "C-MOTOR -> A-Z -> P-350",
            "contributing_path_count": 1,
            "rationale": "1 accepted upstream impact path(s) reach product P-350; worst buffer_state=no_buffer.",
            "verdict": "support",
        }
    ]

    unknown_buffer = provider.assess_incident_product_exposure(
        {
            "impacted_component_edges": [
                _relationship_row(
                    "incident_impacts_component",
                    "Incident",
                    "INC-1",
                    "Component",
                    "C-MOTOR",
                    {"alternate_state": "no_alternate", "rationale": "single source"},
                )
            ],
            "impacted_assembly_edges": [],
            "component_part_of_assembly_edges": [
                _relationship_row(
                    "component_part_of_assembly",
                    "Component",
                    "C-MOTOR",
                    "Assembly",
                    "A-Z",
                    {"quantity": 4},
                )
            ],
            "assembly_part_of_assembly_edges": [],
            "assembly_part_of_product_edges": [
                _relationship_row(
                    "assembly_part_of_product",
                    "Assembly",
                    "A-Z",
                    "Product",
                    "P-350",
                    {"quantity": 1},
                )
            ],
            "buffer_assessments": [],
            "component_buffer_assessment_edges": [],
            "assembly_buffer_assessment_edges": [],
            "product_buffer_assessment_edges": [],
            "assemblies": [
                {
                    "entity_type": "Assembly",
                    "entity_id": "A-Z",
                    "properties": {"name": "Z drive"},
                }
            ],
            "products": [
                {
                    "entity_type": "Product",
                    "entity_id": "P-350",
                    "properties": {"sku": "SKU", "name": "350", "lifecycle_status": "active"},
                }
            ],
        },
        _context("assess_incident_product_exposure", artifact=False),
    )
    assert unknown_buffer["items"][0]["buffer_state"] == "unknown"
    assert unknown_buffer["items"][0]["verdict"] == "unsure"


def test_guangdong_seed_incident_cascades_to_product_exposure() -> None:
    seed = provider.load_seed_data({}, _context("load_supply_chain_seed_data"))
    feed = provider.load_incident_feed({}, _context("load_incident_feed"))
    supplier_scope = provider.assess_incident_supplier_scope(
        {"incidents": feed["incidents"], "suppliers": seed["suppliers"]},
        _context("assess_incident_supplier_scope", artifact=False),
    )
    impacted_supplier_edges = [
        _relationship_row(
            "incident_impacts_supplier",
            "Incident",
            row["incident_id"],
            "Supplier",
            row["supplier_id"],
            {"match_basis": row["match_basis"], "rationale": row["rationale"]},
        )
        for row in supplier_scope["items"]
        if row["incident_id"] == "INC-GD-STEPPER-2026-07"
    ]
    component_cascade = provider.assess_incident_component_cascade(
        {
            "impacted_supplier_edges": impacted_supplier_edges,
            "supplier_supplies_component_edges": seed["supplier_supplies_component"],
            "components": seed["components"],
        },
        _context("assess_incident_component_cascade", artifact=False),
    )
    impacted_component_edges = [
        _relationship_row(
            "incident_impacts_component",
            "Incident",
            row["incident_id"],
            "Component",
            row["component_id"],
            {"alternate_state": row["alternate_state"], "rationale": row["rationale"]},
        )
        for row in component_cascade["items"]
        if row["incident_id"] == "INC-GD-STEPPER-2026-07" and row["verdict"] == "support"
    ]
    inventory = provider.load_inventory_positions(
        {"item_ids": [], "item_types": [], "location_ids": [], "as_of": "2026-07-01"},
        _context("load_inventory_positions"),
    )
    buffer_payload = provider.assess_buffer_coverage(
        {
            "components": seed["components"],
            "assemblies": seed["assemblies"],
            "products": seed["products"],
            "inventory_positions": inventory["inventory_positions"],
            "component_inventory_position_edges": inventory["component_inventory_positions"],
            "assembly_inventory_position_edges": inventory["assembly_inventory_positions"],
            "component_part_of_assembly_edges": seed["component_part_of_assembly"],
            "assembly_part_of_assembly_edges": seed["assembly_part_of_assembly"],
            "assembly_part_of_product_edges": seed["assembly_part_of_product"],
            "demand_context": {},
        },
        _context("assess_buffer_coverage"),
    )

    exposure = provider.assess_incident_product_exposure(
        {
            "impacted_component_edges": impacted_component_edges,
            "impacted_assembly_edges": [],
            "component_part_of_assembly_edges": seed["component_part_of_assembly"],
            "assembly_part_of_assembly_edges": seed["assembly_part_of_assembly"],
            "assembly_part_of_product_edges": seed["assembly_part_of_product"],
            "buffer_assessments": buffer_payload["buffer_assessments"],
            "component_buffer_assessment_edges": buffer_payload["component_buffer_assessments"],
            "assembly_buffer_assessment_edges": buffer_payload["assembly_buffer_assessments"],
            "product_buffer_assessment_edges": buffer_payload["product_buffer_assessments"],
            "assemblies": seed["assemblies"],
            "products": seed["products"],
        },
        _context("assess_incident_product_exposure", artifact=False),
    )

    by_product = {row["product_id"]: row for row in exposure["items"]}
    assert by_product["PRD-V24R2-350-LDO"]["buffer_state"] == "partial_buffer"
    assert by_product["PRD-V24R2-350-LDO"]["verdict"] == "support"
    assert "PRD-V24R2-350-HIW" not in by_product


def test_buffer_coverage_arithmetic_states_rates_and_multilevel_quantities() -> None:
    payload = provider.assess_buffer_coverage(
        {
            "components": [
                {"component_id": "C-SUFFICIENT"},
                {"component_id": "C-NOBUF"},
                {"component_id": "C-UNKNOWN"},
                {"component_id": "C-RATE"},
                {"component_id": "C-CHILD"},
                {"component_id": "C-ZERO"},
            ],
            "assemblies": [
                {"assembly_id": "A-ROOT"},
                {"assembly_id": "A-CHILD"},
                {"assembly_id": "A-ZERO"},
            ],
            "products": [
                {"product_id": "P-TEST"},
                {"product_id": "P-ZERO"},
            ],
            "inventory_positions": [
                {
                    "inventory_position_id": "INV-SUFFICIENT",
                    "item_type": "component",
                    "item_id": "C-SUFFICIENT",
                    "location_id": "L-1",
                    "quantity_on_hand": 30.0,
                    "quantity_allocated": 0.0,
                    "net_available": 30.0,
                    "unit_of_measure": "ea",
                    "as_of": "2026-07-01",
                },
                {
                    "inventory_position_id": "INV-NOBUF",
                    "item_type": "component",
                    "item_id": "C-NOBUF",
                    "location_id": "L-1",
                    "quantity_on_hand": 2.0,
                    "quantity_allocated": 2.0,
                    "net_available": 0.0,
                    "unit_of_measure": "ea",
                    "as_of": "2026-07-01",
                },
                {
                    "inventory_position_id": "INV-RATE",
                    "item_type": "component",
                    "item_id": "C-RATE",
                    "location_id": "L-1",
                    "quantity_on_hand": 8.0,
                    "quantity_allocated": 0.0,
                    "net_available": 8.0,
                    "unit_of_measure": "ea",
                    "as_of": "2026-07-01",
                },
                {
                    "inventory_position_id": "INV-CHILD",
                    "item_type": "component",
                    "item_id": "C-CHILD",
                    "location_id": "L-1",
                    "quantity_on_hand": 100.0,
                    "quantity_allocated": 0.0,
                    "net_available": 100.0,
                    "unit_of_measure": "ea",
                    "as_of": "2026-07-01",
                },
                {
                    "inventory_position_id": "INV-ZERO",
                    "item_type": "component",
                    "item_id": "C-ZERO",
                    "location_id": "L-1",
                    "quantity_on_hand": 5.0,
                    "quantity_allocated": 0.0,
                    "net_available": 5.0,
                    "unit_of_measure": "ea",
                    "as_of": "2026-07-01",
                },
            ],
            "component_inventory_position_edges": [
                {"component_id": "C-SUFFICIENT", "inventory_position_id": "INV-SUFFICIENT"},
                {"component_id": "C-NOBUF", "inventory_position_id": "INV-NOBUF"},
                {"component_id": "C-RATE", "inventory_position_id": "INV-RATE"},
                {"component_id": "C-CHILD", "inventory_position_id": "INV-CHILD"},
                {"component_id": "C-ZERO", "inventory_position_id": "INV-ZERO"},
            ],
            "assembly_inventory_position_edges": [],
            "component_part_of_assembly_edges": [
                {"component_id": "C-SUFFICIENT", "assembly_id": "A-ROOT", "quantity": 1},
                {"component_id": "C-NOBUF", "assembly_id": "A-ROOT", "quantity": 1},
                {"component_id": "C-UNKNOWN", "assembly_id": "A-ROOT", "quantity": 1},
                {"component_id": "C-RATE", "assembly_id": "A-ROOT", "quantity": 1},
                {"component_id": "C-CHILD", "assembly_id": "A-CHILD", "quantity": 3},
                {"component_id": "C-ZERO", "assembly_id": "A-ZERO", "quantity": 1},
            ],
            "assembly_part_of_assembly_edges": [
                {"child_assembly_id": "A-CHILD", "parent_assembly_id": "A-ROOT", "quantity": 2}
            ],
            "assembly_part_of_product_edges": [
                {"assembly_id": "A-ROOT", "product_id": "P-TEST", "quantity": 1},
                {"assembly_id": "A-ZERO", "product_id": "P-ZERO", "quantity": 1},
            ],
            "demand_context": {
                "as_of": "2026-07-01",
                "horizon_duration": 10,
                "horizon_unit": "days",
                "product_open_demand_units": {"P-TEST": 10, "P-ZERO": 0},
                "item_consumption_rates": {"component:C-RATE": {"rate": 2.0, "period": "day"}},
            },
        },
        _context("assess_buffer_coverage", artifact=False),
    )

    rows = {(row["item_id"], row["product_id"]): row for row in payload["buffer_assessments"]}
    assert rows[("C-SUFFICIENT", "P-TEST")]["buffer_state"] == "sufficient_buffer"
    assert rows[("C-NOBUF", "P-TEST")]["buffer_state"] == "no_buffer"
    assert rows[("C-UNKNOWN", "P-TEST")]["buffer_state"] == "unknown"
    assert rows[("C-UNKNOWN", "P-TEST")]["net_available"] is None
    assert rows[("C-ZERO", "P-ZERO")]["buffer_state"] == "sufficient_buffer"
    assert rows[("C-RATE", "P-TEST")]["estimated_consumption_rate"] == 2.0
    assert rows[("C-RATE", "P-TEST")]["coverage_duration"] == 4.0
    assert rows[("C-CHILD", "P-TEST")]["required_per_unit"] == 6.0


def test_seed_taiwan_rail_cascade_rows_are_unsure_with_viable_alternate() -> None:
    seed = provider.load_seed_data({}, _context("load_supply_chain_seed_data"))
    feed = provider.load_incident_feed({}, _context("load_incident_feed"))
    supplier_scope = provider.assess_incident_supplier_scope(
        {"incidents": feed["incidents"], "suppliers": seed["suppliers"]},
        _context("assess_incident_supplier_scope", artifact=False),
    )
    impacted_supplier_edges = [
        _relationship_row(
            "incident_impacts_supplier",
            "Incident",
            row["incident_id"],
            "Supplier",
            row["supplier_id"],
            {"match_basis": row["match_basis"], "rationale": row["rationale"]},
        )
        for row in supplier_scope["items"]
        if row["incident_id"] == "INC-TW-RAIL-2026-07" and row["verdict"] == "support"
    ]

    component_cascade = provider.assess_incident_component_cascade(
        {
            "impacted_supplier_edges": impacted_supplier_edges,
            "supplier_supplies_component_edges": seed["supplier_supplies_component"],
            "components": seed["components"],
        },
        _context("assess_incident_component_cascade", artifact=False),
    )

    rail_rows = [row for row in component_cascade["items"] if row["incident_id"] == "INC-TW-RAIL-2026-07"]
    assert {row["component_id"] for row in rail_rows} >= {
        "C-RAIL-HIWIN-MGN12H-350",
        "C-RAIL-HIWIN-MGN9H-350",
        "C-RAIL-LDO-MGN12H-350",
        "C-RAIL-LDO-MGN9H-350",
    }
    assert all(row["alternate_state"] == "alternate_viable" for row in rail_rows)
    assert all(row["verdict"] == "unsure" for row in rail_rows)


def test_closed_uk_incident_is_excluded_from_supplier_scope() -> None:
    seed = provider.load_seed_data({}, _context("load_supply_chain_seed_data"))
    feed = provider.load_incident_feed({}, _context("load_incident_feed"))
    supplier_scope = provider.assess_incident_supplier_scope(
        {"incidents": feed["incidents"], "suppliers": seed["suppliers"]},
        _context("assess_incident_supplier_scope", artifact=False),
    )

    assert not any(row["incident_id"] == "INC-UK-HOTEND-2026-06" for row in supplier_scope["items"])
    assert any(
        row["supplier_id"] == "S-UK-HOTEND" and row["component_id"] == "C-HOTEND-E3D-REVO-VORON"
        for row in seed["supplier_supplies_component"]
    )


def test_operations_routing_and_supplier_risk_provider_outputs() -> None:
    assert {"analyze_operations_routing", "apply_operations_routing", "propose_risk_attaches_to_supplier"} <= set(
        CONFIG.workflows
    )
    routing = provider.analyze_operations_routing({}, _context("analyze_operations_routing"))

    _assert_keys(
        routing["work_items"],
        {"title", "summary", "description", "rationale", "type", "status", "priority", "target_date"},
    )
    _assert_keys(routing["risks"], {"title", "summary", "status", "priority"})
    assert {row["work_item_id"] for row in routing["work_items"]} == {
        "WI-GD-STEPPER-ALLOCATE",
        "WI-SH-TOOLHEAD-REPLAN",
        "WI-TW-RAIL-QUALIFY-DIST",
    }
    assert routing["work_item_addresses_incident"] == [
        {"work_item_id": "WI-GD-STEPPER-ALLOCATE", "incident_id": "INC-GD-STEPPER-2026-07"},
        {"work_item_id": "WI-SH-TOOLHEAD-REPLAN", "incident_id": "INC-SH-TOOLHEAD-2026-07"},
        {"work_item_id": "WI-TW-RAIL-QUALIFY-DIST", "incident_id": "INC-TW-RAIL-2026-07"},
    ]

    seed = provider.load_seed_data({}, _context("load_supply_chain_seed_data"))
    feed = provider.load_incident_feed({}, _context("load_incident_feed"))
    supplier_scope = provider.assess_incident_supplier_scope(
        {"incidents": feed["incidents"], "suppliers": seed["suppliers"]},
        _context("assess_incident_supplier_scope", artifact=False),
    )
    impacted_supplier_edges = [
        _relationship_row(
            "incident_impacts_supplier",
            "Incident",
            row["incident_id"],
            "Supplier",
            row["supplier_id"],
            {"match_basis": row["match_basis"], "rationale": row["rationale"]},
        )
        for row in supplier_scope["items"]
        if row["verdict"] == "support" or row["incident_id"] == "INC-GD-STEPPER-2026-07"
    ]
    component_cascade = provider.assess_incident_component_cascade(
        {
            "impacted_supplier_edges": impacted_supplier_edges,
            "supplier_supplies_component_edges": seed["supplier_supplies_component"],
            "components": seed["components"],
        },
        _context("assess_incident_component_cascade", artifact=False),
    )
    supplier_risks = provider.assess_supplier_risk_attachments(
        {
            "incidents": feed["incidents"],
            "suppliers": seed["suppliers"],
            "risks": routing["risks"],
            "impacted_supplier_edges": impacted_supplier_edges,
            "impacted_component_edges": [
                _relationship_row(
                    "incident_impacts_component",
                    "Incident",
                    row["incident_id"],
                    "Component",
                    row["component_id"],
                    {"alternate_state": row["alternate_state"], "rationale": row["rationale"]},
                )
                for row in component_cascade["items"]
            ],
            "impacted_assembly_edges": [],
        },
        _context("assess_supplier_risk_attachments"),
    )

    _assert_keys(supplier_risks["items"], {"risk_id", "supplier_id", "impact_basis"})
    by_risk = {row["risk_id"]: row for row in supplier_risks["items"]}
    assert by_risk["RISK-GD-STEPPER-SINGLE-SOURCE"]["supplier_id"] == "S-CN-GD-STEPPER"
    assert by_risk["RISK-GD-STEPPER-SINGLE-SOURCE"]["source_evidence_verdict"] == "support"
    assert by_risk["RISK-TW-RAIL-CONDITIONAL-ALT"]["supplier_id"] == "S-TW-RAIL"


def test_supplier_risk_attachment_verdicts_follow_cascade_evidence() -> None:
    base_payload = {
        "incidents": [
            {
                "incident_id": "INC-GD-STEPPER-2026-07",
                "title": "Open incident",
                "severity": "critical",
                "scope_type": "supplier",
                "scope_id": "S-CN-GD-STEPPER",
                "status": "open",
                "reported_at": "2026-07-01",
                "closed_at": None,
                "summary": None,
            }
        ],
        "suppliers": [
            {
                "supplier_id": "S-CN-GD-STEPPER",
                "name": "Guangdong Stepper Works",
                "primary_geography": "Shenzhen, Guangdong, China",
                "tier_hint": None,
            }
        ],
        "risks": [
            {
                "risk_id": "RISK-GD-STEPPER-SINGLE-SOURCE",
                "title": "Stepper single-source exposure",
                "summary": None,
                "status": "open",
                "priority": "high",
            }
        ],
        "impacted_supplier_edges": [
            _relationship_row(
                "incident_impacts_supplier",
                "Incident",
                "INC-GD-STEPPER-2026-07",
                "Supplier",
                "S-CN-GD-STEPPER",
                {"match_basis": "direct", "rationale": "direct supplier incident"},
            )
        ],
        "impacted_assembly_edges": [],
    }
    review_gated = provider.assess_supplier_risk_attachments(
        {**base_payload, "impacted_component_edges": []},
        _context("assess_supplier_risk_attachments"),
    )["items"]
    assert review_gated[0]["source_evidence_verdict"] == "support"
    assert review_gated[0]["maintainer_judgment_verdict"] == "unsure"
    assert review_gated[0]["verdict"] == "unsure"

    supported = provider.assess_supplier_risk_attachments(
        {
            **base_payload,
            "impacted_component_edges": [
                _relationship_row(
                    "incident_impacts_component",
                    "Incident",
                    "INC-GD-STEPPER-2026-07",
                    "Component",
                    "C-MOTOR",
                    {"alternate_state": "no_alternate", "rationale": "single source"},
                )
            ],
        },
        _context("assess_supplier_risk_attachments"),
    )["items"]
    assert supported[0]["maintainer_judgment_verdict"] == "support"
    assert supported[0]["verdict"] == "support"
