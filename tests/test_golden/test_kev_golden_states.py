"""Golden coverage for the KEV reference and triage review surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.support.kev_golden import (
    ASSET_EXPOSURE_STEPS,
    ASSET_PRODUCTS_STEPS,
    EXPOSURE_RECONCILIATION_STEPS,
    KEV_GOLDEN_DIR,
    KEV_NAMED_QUERY_COVERAGE,
    KEV_NAMED_QUERY_GOLDENS,
    KEV_WORKFLOW_COVERAGE,
    assert_kev_config_coverage,
    assert_or_update_golden,
    build_auto_resolve_branch_cross_section,
    build_kev_reconciliation_positive_instance,
    build_kev_reference_instance,
    build_kev_state_cross_section,
    build_kev_triage_instance,
    build_named_query_surface_cross_section,
    build_pending_relationship_visibility_cross_section,
    build_workflow_step_output_cross_section,
    execute_kev_workflow_for_steps,
    run_kev_proposal,
    triage_query_specs,
)


@pytest.fixture(scope="module")
def kev_golden_reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Build all KEV reports once for this module."""
    root = tmp_path_factory.mktemp("kev-goldens")

    reference = build_kev_reference_instance(root / "reference")
    reference_state = build_kev_state_cross_section(reference, state="reference")

    asset_product_instance = build_kev_triage_instance(root / "asset-products", stage="local")
    asset_products_intermediate = build_workflow_step_output_cross_section(
        execute_kev_workflow_for_steps(asset_product_instance, "propose_asset_products"),
        ASSET_PRODUCTS_STEPS,
    )
    asset_products_proposal = run_kev_proposal(
        asset_product_instance,
        "propose_asset_products",
    )

    exposure_instance = build_kev_triage_instance(root / "asset-exposure", stage="classified")
    asset_exposure_intermediate = build_workflow_step_output_cross_section(
        execute_kev_workflow_for_steps(exposure_instance, "propose_asset_exposure"),
        ASSET_EXPOSURE_STEPS,
    )
    asset_exposure_proposal = run_kev_proposal(
        exposure_instance,
        "propose_asset_exposure",
    )

    review = build_kev_triage_instance(root / "review", stage="review")
    overlay_review_state = build_kev_state_cross_section(review, state="overlay")
    named_query_surfaces = build_named_query_surface_cross_section(
        review,
        triage_query_specs(review),
    )
    relationship_state_visibility = build_pending_relationship_visibility_cross_section(review)

    reconciliation = build_kev_reconciliation_positive_instance(root / "reconciliation")
    exposure_reconciliation_intermediate = build_workflow_step_output_cross_section(
        execute_kev_workflow_for_steps(reconciliation, "propose_exposure_reconciliation"),
        EXPOSURE_RECONCILIATION_STEPS,
    )
    exposure_reconciliation_proposal = run_kev_proposal(
        reconciliation,
        "propose_exposure_reconciliation",
    )
    auto_resolve_branches = build_auto_resolve_branch_cross_section(root / "auto-resolve")

    return {
        "reference_build_state": reference_state,
        "overlay_review_state": overlay_review_state,
        "asset_products_proposal": asset_products_proposal,
        "asset_exposure_proposal": asset_exposure_proposal,
        "exposure_reconciliation_proposal": exposure_reconciliation_proposal,
        "named_query_surfaces": named_query_surfaces,
        "relationship_state_visibility": relationship_state_visibility,
        "auto_resolve_branches": auto_resolve_branches,
        "asset_products_workflow": asset_products_intermediate,
        "asset_exposure_workflow": asset_exposure_intermediate,
        "exposure_reconciliation_workflow": exposure_reconciliation_intermediate,
    }


@pytest.mark.parametrize(
    ("report_name", "golden_path"),
    [
        ("reference_build_state", KEV_GOLDEN_DIR / "reference_build_state.json"),
        ("overlay_review_state", KEV_GOLDEN_DIR / "overlay_review_state.json"),
        ("asset_products_proposal", KEV_GOLDEN_DIR / "asset_products_proposal.json"),
        ("asset_exposure_proposal", KEV_GOLDEN_DIR / "asset_exposure_proposal.json"),
        (
            "exposure_reconciliation_proposal",
            KEV_GOLDEN_DIR / "exposure_reconciliation_proposal.json",
        ),
        ("named_query_surfaces", KEV_GOLDEN_DIR / "named_query_surfaces.json"),
        (
            "relationship_state_visibility",
            KEV_GOLDEN_DIR / "relationship_state_visibility.json",
        ),
        ("auto_resolve_branches", KEV_GOLDEN_DIR / "auto_resolve_branches.json"),
        (
            "asset_products_workflow",
            KEV_GOLDEN_DIR / "intermediate_payloads" / "asset_products_workflow.json",
        ),
        (
            "asset_exposure_workflow",
            KEV_GOLDEN_DIR / "intermediate_payloads" / "asset_exposure_workflow.json",
        ),
        (
            "exposure_reconciliation_workflow",
            KEV_GOLDEN_DIR / "intermediate_payloads" / "exposure_reconciliation_workflow.json",
        ),
    ],
)
def test_kev_golden_report(
    kev_golden_reports: dict[str, Any],
    report_name: str,
    golden_path: Path,
) -> None:
    assert_or_update_golden(kev_golden_reports[report_name], golden_path)


def test_kev_workflow_and_query_coverage_map_is_current() -> None:
    assert_kev_config_coverage()
    assert {
        name for name, coverage in KEV_NAMED_QUERY_COVERAGE.items() if coverage == "golden"
    } == set(KEV_NAMED_QUERY_GOLDENS)
    assert {name for name, coverage in KEV_WORKFLOW_COVERAGE.items() if coverage == "golden"} == {
        "build_public_kev_reference",
        "build_local_state",
        "propose_asset_products",
        "propose_asset_exposure",
        "propose_exposure_reconciliation",
    }


def test_reconciliation_golden_exercises_positive_candidate_path(
    kev_golden_reports: dict[str, Any],
) -> None:
    proposal = kev_golden_reports["exposure_reconciliation_proposal"]
    assert proposal["candidate_count"] > 0
    assert proposal["group_status"] == "pending_review"
    assert proposal["relationship_type"] == "asset_remediated_vulnerability"

    workflow = kev_golden_reports["exposure_reconciliation_workflow"]
    steps = {step["step"]: step["output"] for step in workflow["steps"]}
    assert steps["reconciliation"]["items_count"] > 0
    assert steps["candidates"]["candidates_count"] > 0
    assert steps["remediation_signals"]["signals_count"] > 0
    assert steps["proposal"]["candidate_count"] > 0


def test_named_query_golden_pins_receipts_assertions_and_pending_visibility(
    kev_golden_reports: dict[str, Any],
) -> None:
    surface = kev_golden_reports["named_query_surfaces"]
    queries = {query["name"]: query for query in surface["queries"]}

    for query_name in ("owner_patch_queue", "control_coverage_gap"):
        receipt = queries[query_name]["receipt"]
        assert receipt["present"] is True
        assert receipt["operation_type"] == "query"
        assert receipt["relationship_state_source"] == "query_config"
        assert _contains_relationship_assertion(queries[query_name]["results"])

    assert _contains_pending_relationship_assertion(queries["control_coverage_gap"]["results"])


def test_relationship_state_visibility_golden_proves_pending_reviewable_boundary(
    kev_golden_reports: dict[str, Any],
) -> None:
    visibility = kev_golden_reports["relationship_state_visibility"]["states"]
    assert visibility["reviewable"]["pending_fixture_count"] == 1
    assert visibility["pending"]["pending_fixture_count"] == 1
    assert visibility["live"]["pending_fixture_count"] == 0
    assert visibility["accepted"]["pending_fixture_count"] == 0
    assert visibility["reviewable"]["pending_fixture_matches"][0]["review_status"] == "pending"


def test_auto_resolve_branch_golden_pins_trust_and_unsure_paths(
    kev_golden_reports: dict[str, Any],
) -> None:
    branches = kev_golden_reports["auto_resolve_branches"]
    trusted_support = branches["trusted_all_support"]
    trusted_unsure = branches["trusted_unsure_always_review"]

    assert trusted_support["group_status"] == "auto_resolved"
    assert trusted_support["prior_resolution"]["trust_status"] == "trusted"
    assert trusted_support["member_count"] == 1

    assert trusted_unsure["group_status"] == "pending_review"
    assert trusted_unsure["review_priority"] == "review"
    assert trusted_unsure["prior_resolution"]["trust_status"] == "trusted"
    assert any(
        signal["signal"] == "unsure"
        for member in trusted_unsure["members"]
        for signal in member["signals"]
    )


def _contains_relationship_assertion(value: Any) -> bool:
    if isinstance(value, dict):
        assertion = value.get("assertion")
        if isinstance(assertion, dict) and "review" in assertion and "lifecycle" in assertion:
            return True
        return any(_contains_relationship_assertion(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_relationship_assertion(item) for item in value)
    return False


def _contains_pending_relationship_assertion(value: Any) -> bool:
    if isinstance(value, dict):
        assertion = value.get("assertion")
        if isinstance(assertion, dict):
            review = assertion.get("review")
            if isinstance(review, dict) and review.get("status") == "pending":
                return True
        return any(_contains_pending_relationship_assertion(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_pending_relationship_assertion(item) for item in value)
    return False
