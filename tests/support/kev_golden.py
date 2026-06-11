"""Helpers for deterministic KEV golden-file tests."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.composer import compose_config_files
from cruxible_core.config.loader import load_config, save_config
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.evidence import RelationshipEvidence
from cruxible_core.graph.types import RelationshipInstance, RelationshipMetadata
from cruxible_core.kits import write_materialized_kit_metadata
from cruxible_core.service import (
    service_apply_workflow,
    service_lock,
    service_propose_workflow,
    service_query_surface,
    service_resolve_group,
    service_run,
)
from cruxible_core.workflow import execute_workflow
from tests.support.state_cross_section import (
    CrossSectionLimits,
    StateCrossSectionSpec,
    assert_matches_golden,
    build_state_cross_section,
    normalize_cross_section_value,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KEV_REFERENCE_KIT_DIR = REPO_ROOT / "kits" / "kev-reference"
KEV_TRIAGE_KIT_DIR = REPO_ROOT / "kits" / "kev-triage"
KEV_GOLDEN_DIR = REPO_ROOT / "tests" / "goldens" / "kev"

JsonObject = dict[str, Any]

REFERENCE_QUERY_NAMES = (
    "vulnerability_products",
    "product_vulnerabilities",
    "vendor_products",
    "vendor_vulnerabilities",
)
TRIAGE_QUERY_NAMES = (
    "vulnerability_asset_context",
    "product_asset_context",
    "owner_patch_queue",
    "vendor_service_impact",
    "control_coverage_gap",
    "vulnerability_class_context",
)
KEV_NAMED_QUERY_GOLDENS = (*TRIAGE_QUERY_NAMES, *REFERENCE_QUERY_NAMES)
KEV_WORKFLOW_COVERAGE: dict[str, str] = {
    "build_public_kev_reference": "golden",
    "build_local_state": "golden",
    "propose_asset_products": "golden",
    "propose_asset_exposure": "golden",
    "propose_vulnerability_classification": "integration",
    "propose_exposure_reconciliation": "golden",
}
KEV_NAMED_QUERY_COVERAGE: dict[str, str] = {
    **{name: "golden" for name in KEV_NAMED_QUERY_GOLDENS},
    "asset_vulnerability_postures_requiring_action": "integration",
}

ASSET_PRODUCTS_STEPS = (
    "inventory",
    "reference_products",
    "matches",
    "match_summary",
    "candidates",
    "signals",
    "proposal",
)
ASSET_EXPOSURE_STEPS = (
    "asset_products",
    "vulnerability_products",
    "affected_product_join",
    "affected_assessments",
    "assets",
    "asset_controls",
    "vulnerability_classifications",
    "control_mitigations",
    "assessments",
    "exposure_summary",
    "candidates",
    "version_signals",
    "exploitability_signals",
    "control_signals",
    "proposal",
)
EXPOSURE_RECONCILIATION_STEPS = (
    "asset_products",
    "vulnerability_products",
    "reconciliation_product_join",
    "affected_assessments",
    "accepted_exposures",
    "remediated_edges",
    "reconciliation",
    "reconciliation_summary",
    "candidates",
    "remediation_signals",
    "proposal",
)


def assert_or_update_golden(actual: Mapping[str, Any], golden_path: Path) -> None:
    """Assert against a golden, or update it when CRUXIBLE_UPDATE_GOLDENS=1."""
    if os.environ.get("CRUXIBLE_UPDATE_GOLDENS") == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        return
    assert_matches_golden(actual, golden_path)


def build_kev_reference_instance(tmp_path: Path) -> CruxibleInstance:
    """Create and apply an isolated KEV reference instance."""
    kit_root = _materialize_reference_kit(tmp_path / "kev-reference-kit")
    instance = CruxibleInstance.init(tmp_path / "reference-instance", str(kit_root / "config.yaml"))
    service_lock(instance, force=True)
    _apply_canonical_workflow(instance, "build_public_kev_reference")
    return instance


def build_kev_triage_instance(
    tmp_path: Path,
    *,
    stage: str = "local",
) -> CruxibleInstance:
    """Create an isolated composed KEV triage instance at a requested lifecycle stage."""
    kit_root = _materialize_triage_kit(tmp_path / "kev-triage-kit")
    instance = CruxibleInstance.init(tmp_path / "triage-instance", str(kit_root / "config.yaml"))
    service_lock(instance, force=True)
    _apply_canonical_workflow(instance, "build_public_kev_reference")
    _apply_canonical_workflow(instance, "build_local_state")
    if stage == "local":
        return instance

    _approve_workflow_group(instance, "propose_asset_products")
    if stage == "asset_products":
        return instance

    _approve_default_vulnerability_classification(instance)
    if stage == "classified":
        return instance

    _approve_workflow_group(instance, "propose_asset_exposure")
    if stage == "exposure":
        return instance

    if stage == "review":
        service_propose_workflow(instance, "propose_exposure_reconciliation", {})
        _add_review_context_edges(instance)
        return instance

    raise ValueError(f"Unknown KEV triage stage: {stage}")


def build_kev_reconciliation_positive_instance(tmp_path: Path) -> CruxibleInstance:
    """Build a KEV instance with one stale approved exposure for reconciliation goldens."""
    instance = build_kev_triage_instance(tmp_path, stage="exposure")
    _add_stale_exposure_for_reconciliation_golden(instance)
    return instance


def run_kev_proposal(
    instance: CruxibleInstance,
    workflow_name: str,
    input_payload: dict[str, Any] | None = None,
) -> JsonObject:
    """Run a proposal workflow through the service layer and summarize its review output."""
    result = service_propose_workflow(instance, workflow_name, input_payload or {})
    report: JsonObject = {
        "workflow": workflow_name,
        "group_status": result.group_status,
        "review_priority": result.review_priority,
        "group_id": result.group_id,
        "suppressed": result.suppressed,
        "relationship_type": _relationship_type_from_output(result.output),
        "candidate_count": _safe_int(result.output, "candidate_count"),
        "member_count": _safe_int(result.output, "candidate_count"),
        "query_receipt_ids": result.query_receipt_ids,
        "read_metadata": result.read_metadata,
        "output": _proposal_output_summary(result.output),
    }
    if result.group_id is not None:
        store = instance.get_group_store()
        try:
            group = store.get_group(result.group_id)
            members = store.get_members(result.group_id)
        finally:
            store.close()
        if group is not None:
            report["group"] = {
                "relationship_type": group.relationship_type,
                "status": group.status,
                "review_priority": group.review_priority,
                "member_count": group.member_count,
                "signal_sources_used": group.signal_sources_used,
                "source_workflow_name": group.source_workflow_name,
                "source_step_ids": group.source_step_ids,
                "source_query_receipt_ids": group.source_query_receipt_ids,
                "thesis_facts": group.thesis_facts,
                "analysis_state": group.analysis_state,
            }
        report["members"] = [
            _member_summary(member)
            for member in sorted(
                members,
                key=lambda item: (
                    item.from_type,
                    item.from_id,
                    item.relationship_type,
                    item.to_type,
                    item.to_id,
                ),
            )[:12]
        ]
    return normalize_cross_section_value(report)


def build_kev_state_cross_section(
    instance: CruxibleInstance,
    *,
    state: str,
) -> JsonObject:
    """Build a normalized KEV lifecycle state report."""
    if state == "reference":
        spec = StateCrossSectionSpec(
            entity_types=("Vendor", "Product", "Vulnerability"),
            relationship_types=("product_from_vendor", "vulnerability_affects_product"),
            include_receipts=True,
            include_state=True,
            limits=CrossSectionLimits(
                entities_per_type=10,
                relationships_per_type=14,
                receipts=6,
            ),
        )
        report = build_state_cross_section(instance, spec)
        report["query_surfaces"] = build_named_query_surface_cross_section(
            instance,
            _reference_query_specs(instance),
        )
        return normalize_cross_section_value(report)
    if state != "overlay":
        raise ValueError(f"Unknown KEV state cross section: {state}")

    spec = StateCrossSectionSpec(
        entity_types=(
            "Asset",
            "BusinessService",
            "Owner",
            "CompensatingControl",
            "VulnerabilityClass",
            "Exception",
            "PatchWindow",
        ),
        relationship_types=(
            "service_depends_on_asset",
            "asset_owned_by",
            "asset_has_control",
            "asset_has_exception",
            "asset_patch_window",
            "control_mitigates_class",
            "asset_runs_product",
            "asset_vulnerability_posture",
            "vulnerability_classified_as",
            "asset_remediated_vulnerability",
            "asset_patch_exception_for",
        ),
        include_groups=True,
        include_receipts=True,
        include_state=True,
        limits=CrossSectionLimits(
            entities_per_type=10,
            relationships_per_type=16,
            groups=8,
            group_members=12,
            receipts=10,
        ),
    )
    return build_state_cross_section(instance, spec)


def build_named_query_surface_cross_section(
    instance: CruxibleInstance,
    query_specs: Mapping[str, Mapping[str, Any]],
    *,
    limit: int = 5,
) -> JsonObject:
    """Run selected KEV named queries and include their public row surfaces."""
    reports: list[JsonObject] = []
    for query_name in sorted(query_specs):
        spec = query_specs[query_name]
        params = dict(spec.get("params", {}))
        query_limit = int(spec.get("limit", limit))
        result = service_query_surface(instance, query_name, params, limit=query_limit)
        reports.append(
            {
                "name": query_name,
                "params": params,
                "limit": query_limit,
                "total_results": result.total,
                "returned_results": len(result.items),
                "truncated": result.truncated,
                "limit_truncated": result.limit_truncated,
                "path_truncated": result.path_truncated,
                "truncation_reasons": result.truncation_reasons,
                "result_shape": result.result_shape,
                "relationship_state": result.relationship_state,
                "results": [_query_row_summary(row) for row in result.items],
            }
        )
    return normalize_cross_section_value({"queries": reports})


def build_workflow_step_output_cross_section(
    result: Any,
    selected_steps: Iterable[str],
    *,
    item_limit: int = 8,
) -> JsonObject:
    """Summarize allowlisted lower-level workflow step outputs."""
    steps: list[JsonObject] = []
    for step_name in selected_steps:
        output = result.step_outputs.get(step_name)
        steps.append(
            {
                "step": step_name,
                "producer_step_id": result.alias_step_ids.get(step_name, step_name),
                "output": _payload_cross_section(output, item_limit=item_limit),
            }
        )
    return normalize_cross_section_value(
        {
            "workflow": result.workflow,
            "mode": result.mode,
            "read_metadata": result.read_metadata,
            "query_receipt_ids": result.query_receipt_ids,
            "steps": steps,
        }
    )


def execute_kev_workflow_for_steps(
    instance: CruxibleInstance,
    workflow_name: str,
    input_payload: dict[str, Any] | None = None,
) -> Any:
    """Execute a workflow through the lower-level executor for step output inspection."""
    return execute_workflow(
        instance,
        instance.load_config(),
        workflow_name,
        input_payload or {},
        persist_receipt=False,
        persist_query_receipts=False,
        persist_traces=False,
    )


def triage_query_specs(instance: CruxibleInstance) -> dict[str, dict[str, Any]]:
    """Return stable params for the composed KEV query surface golden."""
    ids = _selected_triage_ids(instance)
    return {
        "vulnerability_asset_context": {
            "params": {"cve_id": ids["cve_id"]},
            "limit": 5,
        },
        "product_asset_context": {
            "params": {"product_id": ids["product_id"]},
            "limit": 5,
        },
        "owner_patch_queue": {
            "params": {"owner_id": ids["owner_id"]},
            "limit": 5,
        },
        "vendor_service_impact": {
            "params": {"vendor_id": ids["vendor_id"]},
            "limit": 5,
        },
        "control_coverage_gap": {
            "params": {"control_id": ids["control_id"]},
            "limit": 5,
        },
        "vulnerability_class_context": {
            "params": {"class_id": ids["class_id"]},
            "limit": 5,
        },
        **_reference_query_specs(
            instance,
            cve_id=ids["cve_id"],
            product_id=ids["product_id"],
            vendor_id=ids["vendor_id"],
        ),
    }


def assert_kev_config_coverage() -> None:
    """Fail when KEV workflows or named queries grow without coverage classification."""
    config = compose_config_files(
        base_path=KEV_REFERENCE_KIT_DIR / "config.yaml",
        overlay_path=KEV_TRIAGE_KIT_DIR / "config.yaml",
    )
    workflow_names = set(config.workflows)
    query_names = set(config.named_queries)
    missing_workflows = workflow_names - set(KEV_WORKFLOW_COVERAGE)
    extra_workflows = set(KEV_WORKFLOW_COVERAGE) - workflow_names
    missing_queries = query_names - set(KEV_NAMED_QUERY_COVERAGE)
    extra_queries = set(KEV_NAMED_QUERY_COVERAGE) - query_names
    assert not missing_workflows, f"KEV workflows missing coverage labels: {missing_workflows}"
    assert not extra_workflows, f"Unknown KEV workflow coverage labels: {extra_workflows}"
    assert not missing_queries, f"KEV named queries missing coverage labels: {missing_queries}"
    assert not extra_queries, f"Unknown KEV query coverage labels: {extra_queries}"


def _materialize_reference_kit(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(KEV_REFERENCE_KIT_DIR, root, ignore=shutil.ignore_patterns("__pycache__"))
    config = load_config(KEV_REFERENCE_KIT_DIR / "config.yaml")
    config.artifacts["public_kev_bundle"].uri = "./data"
    save_config(config, root / "config.yaml")
    write_materialized_kit_metadata(root)
    return root


def _materialize_triage_kit(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(KEV_TRIAGE_KIT_DIR, root, ignore=shutil.ignore_patterns("__pycache__"))
    config = compose_config_files(
        base_path=KEV_REFERENCE_KIT_DIR / "config.yaml",
        overlay_path=KEV_TRIAGE_KIT_DIR / "config.yaml",
    )
    public_data_root = root / "data" / "public_reference"
    if public_data_root.exists():
        shutil.rmtree(public_data_root)
    shutil.copytree(KEV_REFERENCE_KIT_DIR / "data", public_data_root)
    config.artifacts["public_kev_bundle"].uri = "./data/public_reference"
    config.artifacts["local_seed_bundle"].uri = "./data/seed"
    save_config(config, root / "config.yaml")
    write_materialized_kit_metadata(root)
    return root


def _apply_canonical_workflow(instance: CruxibleInstance, workflow_name: str) -> None:
    preview = service_run(instance, workflow_name, {})
    assert preview.mode == "preview"
    assert preview.apply_digest is not None
    applied = service_apply_workflow(
        instance,
        workflow_name,
        {},
        expected_apply_digest=preview.apply_digest,
        expected_head_snapshot_id=preview.head_snapshot_id,
    )
    assert applied.committed_snapshot_id is not None


def _approve_workflow_group(
    instance: CruxibleInstance,
    workflow_name: str,
    input_payload: dict[str, Any] | None = None,
) -> None:
    proposed = service_propose_workflow(instance, workflow_name, input_payload or {})
    assert proposed.group_id is not None
    resolved = service_resolve_group(
        instance,
        proposed.group_id,
        "approve",
        expected_pending_version=1,
    )
    assert resolved.edges_created > 0


def _approve_default_vulnerability_classification(instance: CruxibleInstance) -> None:
    graph = instance.load_graph()
    cve_id = "CVE-2021-41773"
    if graph.get_entity("Vulnerability", cve_id) is None:
        cve_id = sorted(
            graph.list_entities("Vulnerability"),
            key=lambda item: item.entity_id,
        )[0].entity_id
    _approve_workflow_group(
        instance,
        "propose_vulnerability_classification",
        {
            "items": [
                {
                    "cve_id": cve_id,
                    "class_id": "path_traversal",
                    "basis": "Golden fixture classification for path traversal control coverage.",
                    "source": "golden_fixture",
                    "verdict": "support",
                }
            ]
        },
    )


def _add_review_context_edges(instance: CruxibleInstance) -> None:
    """Add small approved closure/exception context for broad investigation goldens."""
    graph = instance.load_graph()
    posture_edges = sorted(
        graph.list_edges("asset_vulnerability_posture"),
        key=lambda item: (item["from_id"], item["to_id"]),
    )
    if not posture_edges:
        return
    target = posture_edges[0]
    asset_id = target["from_id"]
    cve_id = target["to_id"]
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_remediated_vulnerability",
            from_type="Asset",
            from_id=asset_id,
            to_type="Vulnerability",
            to_id=cve_id,
            properties={
                "remediation_type": "patched",
                "verified_at": "2026-05-10",
                "verification_basis": "Golden fixture closure evidence.",
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                ),
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        {
                            "source": "scanner_export",
                            "source_record_id": f"{asset_id}:{cve_id}:patched",
                        }
                    ],
                    rationale="Golden fixture closure evidence for broad-query context.",
                ),
            ),
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_patch_exception_for",
            from_type="Asset",
            from_id=asset_id,
            to_type="Vulnerability",
            to_id=cve_id,
            properties={
                "exception_id": "EXC-GOLDEN-001",
                "review_due_at": "2026-06-15",
                "scope_basis": "Golden fixture scoped exception for broad-query context.",
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                ),
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        {
                            "source": "grc_exception",
                            "source_record_id": f"{asset_id}:{cve_id}:exception",
                        }
                    ],
                    rationale="Golden fixture scoped exception context.",
                ),
            ),
        )
    )
    instance.save_graph(graph)


def _add_stale_exposure_for_reconciliation_golden(instance: CruxibleInstance) -> None:
    """Inject one accepted stale posture edge that current product evidence does not support."""
    graph = instance.load_graph()
    current_affected_pairs = _current_reconciliation_affected_pairs(instance)
    existing_postures = {
        (edge["from_id"], edge["to_id"]) for edge in graph.list_edges("asset_vulnerability_posture")
    }
    existing_remediations = {
        (edge["from_id"], edge["to_id"])
        for edge in graph.list_edges("asset_remediated_vulnerability")
    }
    asset_product_edges = sorted(
        graph.list_edges("asset_runs_product"),
        key=lambda item: (item["from_id"], item["to_id"]),
    )
    vulnerability_product_edges = sorted(
        graph.list_edges("vulnerability_affects_product"),
        key=lambda item: (item["from_id"], item["to_id"]),
    )
    selected: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
    for asset_product in asset_product_edges:
        asset_id = str(asset_product["from_id"])
        product_id = str(asset_product["to_id"])
        for vulnerability_product in vulnerability_product_edges:
            cve_id = str(vulnerability_product["from_id"])
            pair = (asset_id, cve_id)
            if pair in current_affected_pairs:
                continue
            if pair in existing_postures or pair in existing_remediations:
                continue
            if str(vulnerability_product["to_id"]) == product_id:
                continue
            selected = (asset_product, vulnerability_product)
            break
        if selected is not None:
            break
    if selected is None:
        raise AssertionError("Could not find a deterministic stale exposure pair")

    asset_product, vulnerability_product = selected
    asset_id = str(asset_product["from_id"])
    cve_id = str(vulnerability_product["from_id"])
    product_id = str(asset_product["to_id"])
    installed_version = str(
        asset_product.get("properties", {}).get("installed_version") or "unknown"
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_vulnerability_posture",
            from_type="Asset",
            from_id=asset_id,
            to_type="Vulnerability",
            to_id=cve_id,
            properties={
                "status": "exposed",
                "priority": "high",
                "product_id": product_id,
                "installed_version": installed_version,
                "affected_basis": (
                    "Golden fixture stale posture intentionally no longer supported "
                    "by current affected-product evidence."
                ),
                "exposure_basis": "Golden fixture accepted exposure for reconciliation coverage.",
                "control_basis": "No current control claim is needed for this stale fixture edge.",
                "review_due_at": "2026-06-30",
            },
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(
                    review=RelationshipReviewState(status="approved", source="human")
                ),
                evidence=RelationshipEvidence(
                    evidence_refs=[
                        {
                            "source": "golden_fixture",
                            "source_record_id": f"{asset_id}:{cve_id}:stale-exposure",
                        }
                    ],
                    rationale=(
                        f"{asset_id}->{cve_id} via {product_id} is an accepted stale "
                        "exposure inserted to exercise remediation proposal goldens."
                    ),
                ),
            ),
        )
    )
    instance.save_graph(graph)


def _current_reconciliation_affected_pairs(instance: CruxibleInstance) -> set[tuple[str, str]]:
    result = execute_kev_workflow_for_steps(instance, "propose_exposure_reconciliation")
    affected = result.step_outputs.get("affected_assessments", {})
    if not isinstance(affected, Mapping):
        return set()
    items = affected.get("items", [])
    if not isinstance(items, list):
        return set()
    pairs: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        asset_id = item.get("asset_id")
        cve_id = item.get("cve_id")
        if isinstance(asset_id, str) and isinstance(cve_id, str):
            pairs.add((asset_id, cve_id))
    return pairs


def _reference_query_specs(
    instance: CruxibleInstance,
    *,
    cve_id: str | None = None,
    product_id: str | None = None,
    vendor_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    graph = instance.load_graph()
    selected_cve = cve_id or _first_existing_entity_id(
        graph,
        "Vulnerability",
        preferred=("CVE-2021-41773",),
    )
    selected_product = product_id
    if selected_product is None:
        selected_product = next(
            (
                edge["to_id"]
                for edge in sorted(
                    graph.list_edges("vulnerability_affects_product"),
                    key=lambda item: (item["from_id"], item["to_id"]),
                )
                if edge["from_id"] == selected_cve
            ),
            _first_existing_entity_id(graph, "Product"),
        )
    selected_vendor = vendor_id
    if selected_vendor is None:
        selected_vendor = next(
            (
                edge["to_id"]
                for edge in sorted(
                    graph.list_edges("product_from_vendor"),
                    key=lambda item: (item["from_id"], item["to_id"]),
                )
                if edge["from_id"] == selected_product
            ),
            _first_existing_entity_id(graph, "Vendor"),
        )
    return {
        "product_vulnerabilities": {
            "params": {"product_id": selected_product},
            "limit": 5,
        },
        "vendor_products": {
            "params": {"vendor_id": selected_vendor},
            "limit": 5,
        },
        "vendor_vulnerabilities": {
            "params": {"vendor_id": selected_vendor},
            "limit": 5,
        },
        "vulnerability_products": {
            "params": {"cve_id": selected_cve},
            "limit": 5,
        },
    }


def _selected_triage_ids(instance: CruxibleInstance) -> dict[str, str]:
    graph = instance.load_graph()
    posture = sorted(
        graph.list_edges("asset_vulnerability_posture"),
        key=lambda item: (
            item["properties"].get("priority", ""),
            item["from_id"],
            item["to_id"],
        ),
    )[0]
    asset_id = posture["from_id"]
    cve_id = posture["to_id"]
    product_id = str(posture["properties"].get("product_id") or "")
    if not product_id:
        product_id = next(
            edge["to_id"]
            for edge in sorted(
                graph.list_edges("asset_runs_product"),
                key=lambda item: (item["from_id"], item["to_id"]),
            )
            if edge["from_id"] == asset_id
        )
    vendor_id = next(
        edge["to_id"]
        for edge in sorted(
            graph.list_edges("product_from_vendor"),
            key=lambda item: (item["from_id"], item["to_id"]),
        )
        if edge["from_id"] == product_id
    )
    owner_id = next(
        edge["to_id"]
        for edge in sorted(
            graph.list_edges("asset_owned_by"),
            key=lambda item: (item["from_id"], item["to_id"]),
        )
        if edge["from_id"] == asset_id
    )
    control_id = next(
        (
            edge["from_id"]
            for edge in sorted(
                graph.list_edges("control_mitigates_class"),
                key=lambda item: (item["from_id"], item["to_id"]),
            )
            if edge["to_id"] == "path_traversal"
        ),
        _first_existing_entity_id(graph, "CompensatingControl"),
    )
    return {
        "asset_id": asset_id,
        "cve_id": cve_id,
        "product_id": product_id,
        "vendor_id": vendor_id,
        "owner_id": owner_id,
        "control_id": control_id,
        "class_id": "path_traversal",
    }


def _first_existing_entity_id(
    graph: Any,
    entity_type: str,
    *,
    preferred: tuple[str, ...] = (),
) -> str:
    for entity_id in preferred:
        if graph.get_entity(entity_type, entity_id) is not None:
            return entity_id
    return sorted(graph.list_entities(entity_type), key=lambda item: item.entity_id)[0].entity_id


def _relationship_type_from_output(output: Any) -> str | None:
    if isinstance(output, Mapping):
        value = output.get("relationship_type")
        if isinstance(value, str):
            return value
    return None


def _safe_int(payload: Any, key: str) -> int | None:
    if isinstance(payload, Mapping) and isinstance(payload.get(key), int):
        return int(payload[key])
    return None


def _proposal_output_summary(output: Any) -> Any:
    if not isinstance(output, Mapping):
        return output
    keys = (
        "relationship_type",
        "status",
        "candidate_count",
        "on_empty",
        "group_created",
        "proposal_step_id",
        "candidates_from",
        "pending_refresh_mode",
        "signal_sources_used",
        "query_receipt_ids",
        "suggested_priority",
        "thesis_text",
        "thesis_facts",
        "analysis_state",
    )
    summary = {key: output.get(key) for key in keys if key in output}
    members = output.get("members")
    if isinstance(members, list):
        summary["members"] = [
            _payload_cross_section(member, item_limit=4) for member in members[:8]
        ]
    return summary


def _member_summary(member: Any) -> JsonObject:
    data = member.model_dump(mode="json") if hasattr(member, "model_dump") else dict(member)
    return {
        "tuple": {
            "from_type": data.get("from_type"),
            "from_id": data.get("from_id"),
            "relationship_type": data.get("relationship_type"),
            "to_type": data.get("to_type"),
            "to_id": data.get("to_id"),
        },
        "properties": data.get("properties", {}),
        "signals": sorted(
            data.get("signals", []),
            key=lambda item: (item.get("signal_source", ""), item.get("signal", "")),
        ),
        "source_query_evidence": data.get("source_query_evidence", []),
    }


def _payload_cross_section(value: Any, *, item_limit: int) -> Any:
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for key, item in sorted(value.items(), key=lambda entry: str(entry[0])):
            if isinstance(item, list):
                result[f"{key}_count"] = len(item)
                result[key] = [
                    _payload_cross_section(row, item_limit=item_limit) for row in item[:item_limit]
                ]
            else:
                result[str(key)] = _payload_cross_section(item, item_limit=item_limit)
        return result
    if isinstance(value, list):
        return [_payload_cross_section(item, item_limit=item_limit) for item in value[:item_limit]]
    if hasattr(value, "model_dump"):
        return _payload_cross_section(value.model_dump(mode="json"), item_limit=item_limit)
    return value


def _query_row_summary(row: Any) -> JsonObject:
    if hasattr(row, "values"):
        source = getattr(row, "source", None)
        return {
            "values": getattr(row, "values"),
            "source": _query_row_summary(source) if source is not None else None,
        }
    if hasattr(row, "path") and hasattr(row, "entry") and hasattr(row, "result"):
        return {
            "entry": _entity_summary(getattr(row, "entry")),
            "result": _entity_summary(getattr(row, "result")),
            "entities": [_entity_summary(entity) for entity in getattr(row, "entities", [])],
            "path": [_edge_summary(segment) for segment in getattr(row, "path", [])],
            "includes": {
                alias: _include_summary(include)
                for alias, include in sorted(getattr(row, "includes", {}).items())
            },
        }
    if hasattr(row, "relationship_type") and hasattr(row, "from_id") and hasattr(row, "to_id"):
        summary = _edge_summary(row)
        if getattr(row, "from_entity", None) is not None:
            summary["from_entity"] = _entity_summary(getattr(row, "from_entity"))
        if getattr(row, "to_entity", None) is not None:
            summary["to_entity"] = _entity_summary(getattr(row, "to_entity"))
        return summary
    if hasattr(row, "entity_type") and hasattr(row, "entity_id"):
        return _entity_summary(row)
    if hasattr(row, "model_dump"):
        return row.model_dump(mode="json")
    return dict(row) if isinstance(row, Mapping) else {"value": row}


def _include_summary(include: Any) -> JsonObject:
    return {
        "alias": getattr(include, "alias", None),
        "many": getattr(include, "many", None),
        "exists": getattr(include, "exists", None),
        "count": getattr(include, "count", None),
        "limit": getattr(include, "limit", None),
        "truncated": getattr(include, "truncated", None),
        "items": [
            {
                "edge": _edge_summary(getattr(item, "edge")),
                "source": _entity_summary(getattr(item, "source")),
                "target": _entity_summary(getattr(item, "target")),
            }
            for item in getattr(include, "items", [])
        ],
    }


def _entity_summary(entity: Any) -> JsonObject:
    data = entity.model_dump(mode="json") if hasattr(entity, "model_dump") else dict(entity)
    return {
        "entity_type": data.get("entity_type"),
        "entity_id": data.get("entity_id"),
        "properties": data.get("properties", {}),
    }


def _edge_summary(edge: Any) -> JsonObject:
    data = edge.model_dump(mode="json") if hasattr(edge, "model_dump") else dict(edge)
    metadata = data.get("metadata", {})
    assertion = metadata.get("assertion", {}) if isinstance(metadata, Mapping) else {}
    review = assertion.get("review", {}) if isinstance(assertion, Mapping) else {}
    return {
        "alias": data.get("alias"),
        "relationship_type": data.get("relationship_type"),
        "from_type": data.get("from_type"),
        "from_id": data.get("from_id"),
        "to_type": data.get("to_type"),
        "to_id": data.get("to_id"),
        "properties": data.get("properties", {}),
        "review_status": review.get("status") if isinstance(review, Mapping) else None,
    }
