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
    build_kev_reconciliation_positive_instance,
    build_kev_reference_instance,
    build_kev_state_cross_section,
    build_kev_triage_instance,
    build_named_query_surface_cross_section,
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

    reconciliation = build_kev_reconciliation_positive_instance(root / "reconciliation")
    exposure_reconciliation_intermediate = build_workflow_step_output_cross_section(
        execute_kev_workflow_for_steps(reconciliation, "propose_exposure_reconciliation"),
        EXPOSURE_RECONCILIATION_STEPS,
    )
    exposure_reconciliation_proposal = run_kev_proposal(
        reconciliation,
        "propose_exposure_reconciliation",
    )

    return {
        "reference_build_state": reference_state,
        "overlay_review_state": overlay_review_state,
        "asset_products_proposal": asset_products_proposal,
        "asset_exposure_proposal": asset_exposure_proposal,
        "exposure_reconciliation_proposal": exposure_reconciliation_proposal,
        "named_query_surfaces": named_query_surfaces,
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
            "asset_products_workflow",
            KEV_GOLDEN_DIR / "intermediate_payloads" / "asset_products_workflow.json",
        ),
        (
            "asset_exposure_workflow",
            KEV_GOLDEN_DIR / "intermediate_payloads" / "asset_exposure_workflow.json",
        ),
        (
            "exposure_reconciliation_workflow",
            KEV_GOLDEN_DIR
            / "intermediate_payloads"
            / "exposure_reconciliation_workflow.json",
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
    assert {
        name for name, coverage in KEV_WORKFLOW_COVERAGE.items() if coverage == "golden"
    } == {
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
