"""PC-D and PC-F artifact-path and component-tag activation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    P2_B0_ARTIFACT_CODEC,
    canonical_bytes,
    file_digest,
)
from cruxible_client.contracts.errors import ProjectionFormatError, SubjectFormatError
from cruxible_client.contracts.subjects import SubjectShell, parse_subject, render_subject
from cruxible_core.compiler.compiler import (
    P2_B0_COMPILER,
    P2_B2_COMPILER,
    P2_B4_COMPILER,
    P2_B4_UNIT2_COMPILER,
    P2_B5_COMPILER,
    PC_DF2_COMPILER,
    PC_HR_ARTIFACT_CODEC_COMPILERS,
    SUPPORTED_COMPILERS,
    artifact_kinds_for_compiler,
    current_compiler_coordinate,
)
from cruxible_core.compiler.projection_artifacts import (
    P2_B0_ARTIFACT_KINDS,
    P2_C_ARTIFACT_KINDS,
    PLAYBILL_ARTIFACT_KINDS,
    registered_path_kind,
)


def test_pc_d_activates_procedure_and_line_paths() -> None:
    assert registered_path_kind("governance/approval-policy.json") == "approval-policy"
    assert registered_path_kind("claim-types/project.work_item/status.json") == "claim-type"
    assert registered_path_kind("capture-contracts/erp-release.json") == "capture-contract"
    assert registered_path_kind("claims/12/CLM-12" + "ab" * 15 + ".json") == "claim"
    assert registered_path_kind("procedures/product-lot-release.json") == "procedure"
    assert registered_path_kind("lines/product-lot-release.json") == "line"


def test_pc_f_activates_the_query_definition_path_kind() -> None:
    assert registered_path_kind("query-definitions/project.active_work.json") == "query-definition"
    assert "query-definition" in {entry.kind for entry in PLAYBILL_ARTIFACT_KINDS.entries()}


def test_p2_b1_activates_provider_interface_only_at_the_successor_compiler() -> None:
    assert registered_path_kind("provider-interfaces/demo.interface.json") == ("provider-interface")
    with pytest.raises(ProjectionFormatError):
        PLAYBILL_ARTIFACT_KINDS.resolve_path("provider-interfaces/demo.interface.json")
    assert (
        artifact_kinds_for_compiler(current_compiler_coordinate()).resolve_path(
            "provider-interfaces/demo.interface.json"
        )
        == "provider-interface"
    )


def test_p2_c_activates_procedure_mandates_only_at_the_successor_compiler() -> None:
    assert registered_path_kind("procedure-mandates/demo.json") == "procedure-mandate"
    with pytest.raises(ProjectionFormatError):
        PLAYBILL_ARTIFACT_KINDS.resolve_path("procedure-mandates/demo.json")
    assert (
        artifact_kinds_for_compiler(current_compiler_coordinate()).resolve_path(
            "procedure-mandates/demo.json"
        )
        == "procedure-mandate"
    )


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
    assert (
        parse_subject(
            legacy_bytes,
            path="subjects/project.work_item/wi-1.yaml",
            codec=P2_B0_ARTIFACT_CODEC,
        )
        == subject
    )
    assert (
        parse_subject(render_subject(subject), path="subjects/project.work_item/wi-1.json")
        == subject
    )
    with pytest.raises(SubjectFormatError):
        parse_subject(render_subject(subject), path="subjects/project.work_item/wi-1.yaml")


def test_current_codec_lineage_is_closed_over_installed_compilers() -> None:
    assert PC_HR_ARTIFACT_CODEC_COMPILERS <= set(SUPPORTED_COMPILERS)
    assert current_compiler_coordinate() in PC_HR_ARTIFACT_CODEC_COMPILERS
    assert {
        PC_DF2_COMPILER,
        P2_B2_COMPILER,
        P2_B4_COMPILER,
        P2_B4_UNIT2_COMPILER,
        P2_B5_COMPILER,
    } <= PC_HR_ARTIFACT_CODEC_COMPILERS
    assert P2_B0_COMPILER not in PC_HR_ARTIFACT_CODEC_COMPILERS


def test_p2_b0_compact_bytes_are_pinned_for_every_non_changeset_governed_kind() -> None:
    from cruxible_client.contracts.acquisition_policies import parse_acquisition_policy
    from cruxible_client.contracts.approval_policy import parse_approval_policy
    from cruxible_client.contracts.captures import parse_capture_contract
    from cruxible_client.contracts.claim_types import parse_claim_type
    from cruxible_client.contracts.claims import parse_claim
    from cruxible_client.contracts.documents import parse_document
    from cruxible_client.contracts.procedure_runtime_policy import (
        parse_procedure_runtime_policy,
    )
    from cruxible_client.contracts.procedures.artifacts import parse_procedure
    from cruxible_client.contracts.procedures.line_specs import parse_line_spec
    from cruxible_client.contracts.providers import parse_provider
    from cruxible_client.contracts.query.definitions import parse_query_definition
    from cruxible_client.contracts.types import PrincipalRecord
    from cruxible_core.exhaust.promotions import parse_exhaust_promotion

    fixture_path = Path(__file__).parents[1] / "goldens/playbill/p2-b0-artifact-codec-v1.json"
    fixture = json.loads(fixture_path.read_bytes())
    parsers = {
        "approval-policy": parse_approval_policy,
        "capture-contract": parse_capture_contract,
        "claim": parse_claim,
        "claim-type": parse_claim_type,
        "document": parse_document,
        "exhaust-promotion": parse_exhaust_promotion,
        "line": parse_line_spec,
        "procedure": parse_procedure,
        "procedure-runtime-policy": parse_procedure_runtime_policy,
        "provider": parse_provider,
        "query-definition": parse_query_definition,
        "source-acquisition-policy": parse_acquisition_policy,
        "subject": parse_subject,
    }
    seen: set[str] = set()
    for row in fixture["artifacts"]:
        kind = row["kind"]
        path = row["p2_b0_path"]
        content = row["compact_wire"].encode("utf-8")
        assert P2_B0_ARTIFACT_KINDS.resolve_path(path) == kind
        assert file_digest(content).tagged == row["exact_member_digest"]
        if kind in parsers:
            parsers[kind](content, path=path, codec=P2_B0_ARTIFACT_CODEC)
        elif kind == "principal":
            assert PrincipalRecord.model_validate_json(content).principal_id == "owner"
        else:  # pragma: no cover - the fixture inventory is closed
            raise AssertionError(f"unverified P2-B0 artifact kind: {kind}")
        seen.add(kind)

    assert seen == {entry.kind for entry in P2_B0_ARTIFACT_KINDS.entries()} - {"changeset"}


def test_historical_claim_type_path_error_names_the_historical_spelling() -> None:
    from cruxible_client.contracts.claim_types import ClaimTypeFormatError, parse_claim_type

    fixture_path = Path(__file__).parents[1] / "goldens/playbill/p2-b0-artifact-codec-v1.json"
    fixture = json.loads(fixture_path.read_bytes())
    row = next(item for item in fixture["artifacts"] if item["kind"] == "claim-type")

    with pytest.raises(ClaimTypeFormatError, match=r"attribute_0000\.yaml"):
        parse_claim_type(
            row["compact_wire"].encode("utf-8"),
            path="claim-types/project.work_item/wrong.yaml",
            codec=P2_B0_ARTIFACT_CODEC,
        )


def test_calibration_readings_have_no_governed_path_kind() -> None:
    # Calibration readings are compute-produced, CAS-pinned artifacts. Registering a
    # governed tree path would collapse the ratified policy/readings/mandates split.
    assert "calibration-reading" not in {entry.kind for entry in P2_C_ARTIFACT_KINDS.entries()}
