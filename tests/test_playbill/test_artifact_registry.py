"""PC-D artifact-path and component-tag activation."""

from __future__ import annotations

from cruxible_core.playbill.discovery import DESCRIPTOR_CLAIM_TYPE_SEEDS
from cruxible_core.playbill.projection_artifacts import (
    PLAYBILL_ARTIFACT_KINDS,
    PLAYBILL_FORMAT_RESERVATIONS,
    registered_path_kind,
)


def test_pc_d_activates_procedure_and_line_paths() -> None:
    assert registered_path_kind("claim-types/project.work_item/status.yaml") == "claim-type"
    assert registered_path_kind("capture-contracts/erp-release.yaml") == "capture-contract"
    assert registered_path_kind("claims/12/CLM-12" + "ab" * 15 + ".yaml") == "claim"
    assert registered_path_kind("procedures/product-lot-release.yaml") == "procedure"
    assert registered_path_kind("lines/product-lot-release.yaml") == "line"
    assert PLAYBILL_ARTIFACT_KINDS.reserved_kinds() == ()


def test_pc_d_activates_procedure_line_and_run_input_tags() -> None:
    assert PLAYBILL_FORMAT_RESERVATIONS.implemented_tags() == (
        "playbill-accepted-state-run-input-v1",
        "playbill-capture-contract-v1",
        "playbill-capture-envelope-v1",
        "playbill-claim-v1",
        "playbill-exhaust-run-input-v1",
        "playbill-landed-capture-run-input-v1",
        "playbill-line-slot-binding-v1",
        "playbill-line-v1",
        "playbill-procedure-pin-slot-ref-v1",
        "playbill-procedure-pin-slot-v1",
        "playbill-procedure-v1",
        "playbill-provider-v1",
        "playbill-source-acquisition-policy-v1",
        "playbill-standing-mandate-v1",
    )
    assert PLAYBILL_FORMAT_RESERVATIONS.reserved_tags() == ()


def test_descriptor_claim_type_identity_seed_list_is_exact() -> None:
    assert tuple(item.identity.qualified for item in DESCRIPTOR_CLAIM_TYPE_SEEDS) == (
        "ClaimType:semantic.alias",
        "ClaimType:semantic.distinct_from",
        "ClaimType:semantic.related_to",
        "ClaimType:semantic.tag",
    )
