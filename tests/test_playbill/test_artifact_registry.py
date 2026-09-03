"""PC-D and PC-F artifact-path and component-tag activation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactKindRegistry
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    P2_B0_ARTIFACT_CODEC,
    canonical_bytes,
    file_digest,
    pretty_canonical_bytes,
)
from cruxible_client.contracts.discovery import DESCRIPTOR_CLAIM_TYPE_SEEDS
from cruxible_client.contracts.errors import ProjectionFormatError, SubjectFormatError
from cruxible_client.contracts.projection_extensions import fixture_extension_registry
from cruxible_client.contracts.subjects import SubjectShell, parse_subject, render_subject
from cruxible_core.playbill.compiler import (
    P2_B0_COMPILER,
    P2_B2_COMPILER,
    P2_B4_COMPILER,
    P2_B4_UNIT2_COMPILER,
    PC_DF2_COMPILER,
    PC_HR_ARTIFACT_CODEC_COMPILERS,
    SUPPORTED_COMPILERS,
    artifact_kinds_for_compiler,
    current_compiler_coordinate,
)
from cruxible_core.playbill.projection_artifacts import (
    P2_B0_ARTIFACT_KINDS,
    P2_C_ARTIFACT_KINDS,
    PLAYBILL_ARTIFACT_KINDS,
    PLAYBILL_FORMAT_RESERVATIONS,
    FixtureArtifact,
    FixturePresentation,
    parse_projection_tree,
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
    from cruxible_client.contracts.standing_mandates import parse_standing_mandate
    from cruxible_client.contracts.types import PrincipalRecord
    from cruxible_core.playbill.exhaust.promotions import parse_exhaust_promotion

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
        "standing-mandate": parse_standing_mandate,
        "subject": parse_subject,
    }
    seen: set[str] = set()
    fixture_rows: dict[str, bytes] = {}
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
        elif kind in {"fixture", "presentation"}:
            fixture_rows[path] = content
        else:  # pragma: no cover - the fixture inventory is closed
            raise AssertionError(f"unverified P2-B0 artifact kind: {kind}")
        seen.add(kind)

    assert seen == set(P2_B0_ARTIFACT_KINDS.implemented_kinds()) - {"changeset"}
    parse_projection_tree(
        fixture_rows,
        registry=fixture_extension_registry(),
        artifact_kinds=P2_B0_ARTIFACT_KINDS,
        artifact_codec=P2_B0_ARTIFACT_CODEC,
    )


def test_current_fixture_and_presentation_codec_does_not_depend_on_registry_identity() -> None:
    copied_kinds = ArtifactKindRegistry(PLAYBILL_ARTIFACT_KINDS.entries())
    fixture = FixtureArtifact(artifact_id="example", revision=1)
    presentation = FixturePresentation(subject_identity="example", label="Example")
    parsed = parse_projection_tree(
        {
            "artifacts/fixtures/example.json": pretty_canonical_bytes(
                fixture.model_dump(mode="json")
            ),
            "presentation/fixtures/example.json": pretty_canonical_bytes(
                presentation.model_dump(mode="json")
            ),
        },
        registry=fixture_extension_registry(),
        artifact_kinds=copied_kinds,
        artifact_codec=CURRENT_ARTIFACT_CODEC,
    )

    assert parsed.envelopes[0].identity == "example"
    assert parsed.presentation_facts[0].value == "Example"


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


def test_p2_b2_reserves_every_current_artifact_tag() -> None:
    assert PLAYBILL_FORMAT_RESERVATIONS.implemented_tags() == (
        "playbill-accepted-state-run-input-v1",
        "playbill-approval-policy-v1",
        "playbill-capture-contract-v1",
        "playbill-capture-envelope-v1",
        "playbill-capture-envelope-v2",
        "playbill-capture-procedure-egress-evidence-v1",
        "playbill-capture-provider-invocation-evidence-v1",
        "playbill-claim-v2",
        "playbill-claim-v3",
        "playbill-exhaust-promotion-v1",
        "playbill-exhaust-run-input-v1",
        "playbill-landed-capture-run-input-v1",
        "playbill-line-slot-binding-v1",
        "playbill-line-v1",
        "playbill-line-v2",
        "playbill-pending-admission-material-reservation-v1",
        "playbill-prepared-procedure-run-v4",
        "playbill-prepared-procedure-run-v5",
        "playbill-procedure-acquisition-plan-v2",
        "playbill-procedure-admission-bound-payload-v4",
        "playbill-procedure-admission-bound-payload-v5",
        "playbill-procedure-calibration-cohort-membership-witness-v1",
        "playbill-procedure-calibration-cohort-v1",
        "playbill-procedure-calibration-reading-artifact-v1",
        "playbill-procedure-calibration-reading-identity-v1",
        "playbill-procedure-calibration-reading-v1",
        "playbill-procedure-calibration-relation-cohort-witness-v1",
        "playbill-procedure-calibration-score-v1",
        "playbill-procedure-derived-source-request-v1",
        "playbill-procedure-mandate-v1",
        "playbill-procedure-pin-slot-ref-v1",
        "playbill-procedure-pin-slot-v1",
        "playbill-procedure-producer-receipt-v1",
        "playbill-procedure-provider-binding-v2",
        "playbill-procedure-resolution-v2",
        "playbill-procedure-run-admission-v4",
        "playbill-procedure-run-admission-v5",
        "playbill-procedure-run-receipt-v5",
        "playbill-procedure-run-receipt-v6",
        "playbill-procedure-runtime-policy-v1",
        "playbill-procedure-source-capture-association-v1",
        "playbill-procedure-v1",
        "playbill-procedure-v2",
        "playbill-provider-bucket-classification-plan-v1",
        "playbill-provider-bucket-classifier-installation-v1",
        "playbill-provider-bucket-conformance-fixture-proof-v1",
        "playbill-provider-bucket-conformance-fixture-v1",
        "playbill-provider-budget-translation-v1",
        "playbill-provider-container-materialization-reference-v1",
        "playbill-provider-egress-observation-v1",
        "playbill-provider-external-occurrence-plan-v1",
        "playbill-provider-extras-environment-pin-map-v1",
        "playbill-provider-implementation-closure-v1",
        "playbill-provider-implementation-v1",
        "playbill-provider-interface-v1",
        "playbill-provider-invocation-completed-v1",
        "playbill-provider-invocation-outcome-v1",
        "playbill-provider-invocation-output-digest-v1",
        "playbill-provider-invocation-receipt-v1",
        "playbill-provider-invocation-started-v1",
        "playbill-provider-local-materialization-reference-v1",
        "playbill-provider-result-to-external-capture-v1",
        "playbill-provider-secret-binding-identity-v1",
        "playbill-provider-secret-receipt-reference-v1",
        "playbill-provider-secret-reference-v1",
        "playbill-provider-secret-resolution-plan-v1",
        "playbill-provider-v1",
        "playbill-provider-v2",
        "playbill-query-definition-v1",
        "playbill-resolution-claim-endpoint-v1",
        "playbill-resolution-contract-activation-v2",
        "playbill-run-material-reservation-v1",
        "playbill-settled-outcome-history-v1",
        "playbill-settled-outcome-relation-v1",
        "playbill-settled-outcome-row-v1",
        "playbill-settled-outcomes-access-profile-v1",
        "playbill-settled-outcomes-query-receipt-v1",
        "playbill-settled-outcomes-query-request-v1",
        "playbill-settled-outcomes-query-result-v1",
        "playbill-source-acquisition-policy-v1",
        "playbill-source-read-receipt-v1",
        "playbill-standing-mandate-v1",
        "playbill-verified-provider-binding-v1",
        "playbill-workspace-file-source-request-v1",
    )
    assert PLAYBILL_FORMAT_RESERVATIONS.reserved_tags() == ("playbill-run-material-reservation-v2",)
    for implemented_tag in (
        "playbill-pending-admission-material-reservation-v1",
        "playbill-run-material-reservation-v1",
    ):
        with pytest.raises(ValueError, match="already implemented"):
            PLAYBILL_FORMAT_RESERVATIONS.activate(implemented_tag)
    # Calibration readings are compute-produced, CAS-pinned artifacts. Registering a
    # governed tree path would collapse the ratified policy/readings/mandates split.
    assert "calibration-reading" not in P2_C_ARTIFACT_KINDS.implemented_kinds()


def test_descriptor_claim_type_identity_seed_list_is_exact() -> None:
    assert tuple(item.identity.qualified for item in DESCRIPTOR_CLAIM_TYPE_SEEDS) == (
        "ClaimType:semantic.alias",
        "ClaimType:semantic.distinct_from",
        "ClaimType:semantic.related_to",
        "ClaimType:semantic.tag",
    )
