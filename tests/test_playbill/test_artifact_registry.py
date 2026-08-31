"""PC-D and PC-F artifact-path and component-tag activation."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.discovery import DESCRIPTOR_CLAIM_TYPE_SEEDS
from cruxible_client.contracts.errors import ProjectionFormatError, SubjectFormatError
from cruxible_client.contracts.subjects import SubjectShell, parse_subject, render_subject
from cruxible_core.playbill.compiler import (
    P2_B0_COMPILER,
    artifact_kinds_for_compiler,
    current_compiler_coordinate,
)
from cruxible_core.playbill.projection_artifacts import (
    PLAYBILL_ARTIFACT_KINDS,
    PLAYBILL_FORMAT_RESERVATIONS,
    registered_path_kind,
)


def test_pc_d_activates_procedure_and_line_paths() -> None:
    assert registered_path_kind("governance/approval-policy.json") == "approval-policy"
    assert registered_path_kind("claim-types/project.work_item/status.json") == "claim-type"
    assert registered_path_kind("capture-contracts/erp-release.json") == "capture-contract"
    assert registered_path_kind("claims/12/CLM-12" + "ab" * 15 + ".json") == "claim"
    assert registered_path_kind("procedures/product-lot-release.json") == "procedure"
    assert registered_path_kind("lines/product-lot-release.json") == "line"
    assert PLAYBILL_ARTIFACT_KINDS.reserved_kinds() == ()


def test_pc_f_activates_the_query_definition_path_kind() -> None:
    assert registered_path_kind("query-definitions/project.active_work.json") == "query-definition"
    assert "query-definition" in PLAYBILL_ARTIFACT_KINDS.implemented_kinds()
    assert PLAYBILL_ARTIFACT_KINDS.reserved_kinds() == ()


def test_pc_hr_codec_succeeds_without_changing_the_p2_b0_verifier() -> None:
    legacy = artifact_kinds_for_compiler(P2_B0_COMPILER)
    current = artifact_kinds_for_compiler(current_compiler_coordinate())
    assert legacy.resolve_path("subjects/project.work_item/wi-1.yaml") == "subject"
    assert current.resolve_path("subjects/project.work_item/wi-1.json") == "subject"
    with pytest.raises(ProjectionFormatError):
        legacy.resolve_path("subjects/project.work_item/wi-1.json")
    with pytest.raises(ProjectionFormatError):
        current.resolve_path("subjects/project.work_item/wi-1.yaml")

    subject = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-1"),
        subject_kind="project.work_item",
        subject_id="wi-1",
    )
    legacy_bytes = canonical_bytes(subject.model_dump(mode="json")) + b"\n"
    assert parse_subject(legacy_bytes, path="subjects/project.work_item/wi-1.yaml") == subject
    assert (
        parse_subject(render_subject(subject), path="subjects/project.work_item/wi-1.json")
        == subject
    )
    with pytest.raises(SubjectFormatError):
        parse_subject(render_subject(subject), path="subjects/project.work_item/wi-1.yaml")


def test_pc_e1_activates_procedure_line_run_input_and_promotion_tags() -> None:
    assert PLAYBILL_FORMAT_RESERVATIONS.implemented_tags() == (
        "playbill-accepted-state-run-input-v1",
        "playbill-approval-policy-v1",
        "playbill-capture-contract-v1",
        "playbill-capture-envelope-v1",
        "playbill-claim-v2",
        "playbill-claim-v3",
        "playbill-exhaust-promotion-v1",
        "playbill-exhaust-run-input-v1",
        "playbill-landed-capture-run-input-v1",
        "playbill-line-slot-binding-v1",
        "playbill-line-v1",
        "playbill-procedure-pin-slot-ref-v1",
        "playbill-procedure-pin-slot-v1",
        "playbill-procedure-runtime-policy-v1",
        "playbill-procedure-v1",
        "playbill-procedure-v2",
        "playbill-provider-v1",
        "playbill-query-definition-v1",
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
