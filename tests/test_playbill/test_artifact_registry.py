"""PC-A2 artifact-path activation and future wire-tag reservations."""

from __future__ import annotations

import pytest

from cruxible_core.playbill.discovery import DESCRIPTOR_CLAIM_TYPE_SEEDS
from cruxible_core.playbill.errors import ProjectionFormatError
from cruxible_core.playbill.projection_artifacts import (
    PLAYBILL_ARTIFACT_KINDS,
    PLAYBILL_FORMAT_RESERVATIONS,
    registered_path_kind,
)


def test_claim_type_path_is_activated_while_capture_and_line_paths_refuse() -> None:
    assert registered_path_kind("claim-types/project.work_item/status.yaml") == "claim-type"
    assert {"capture-contract", "line"}.issubset(
        PLAYBILL_ARTIFACT_KINDS.reserved_kinds()
    )
    with pytest.raises(ProjectionFormatError, match="reserved but unimplemented"):
        registered_path_kind("capture-contracts/erp-release.yaml")
    with pytest.raises(ProjectionFormatError, match="reserved but unimplemented"):
        registered_path_kind("lines/product-lot-release.yaml")


def test_future_pc_a2_format_tags_are_exact_reservations_without_implementation() -> None:
    assert PLAYBILL_FORMAT_RESERVATIONS.implemented_tags() == ()
    assert PLAYBILL_FORMAT_RESERVATIONS.reserved_tags() == (
        "playbill-accepted-state-run-input-v1",
        "playbill-capture-contract-v1",
        "playbill-capture-envelope-v1",
        "playbill-exhaust-run-input-v1",
        "playbill-landed-capture-run-input-v1",
        "playbill-line-slot-binding-v1",
        "playbill-line-v1",
        "playbill-procedure-pin-slot-ref-v1",
    )


def test_descriptor_claim_type_identity_seed_list_is_exact() -> None:
    assert tuple(item.identity.qualified for item in DESCRIPTOR_CLAIM_TYPE_SEEDS) == (
        "ClaimType:semantic.alias",
        "ClaimType:semantic.distinct_from",
        "ClaimType:semantic.related_to",
        "ClaimType:semantic.tag",
    )
