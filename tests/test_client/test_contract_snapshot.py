"""Contract-freeze tests for the reduced Playbill client surface."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, get_args

import pytest

from cruxible_client import contracts
from cruxible_client.authoring.sdk import SDK_CONTRACT_SNAPSHOT_DIGEST, SUPPORTED_DAEMON_CONTRACTS
from cruxible_client.contracts.authoring.models import (
    AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    AUTHORING_SDK_VERSION,
)
from cruxible_client.contracts.primitives import canonical_json
from tests.support.client_contracts import (
    compare_contract_manifests,
    generate_contract_manifest,
    load_contract_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "tests/goldens/cruxible_client/contracts_snapshot.json"


def test_client_contract_snapshot_is_current() -> None:
    snapshot = load_contract_snapshot(SNAPSHOT_PATH)
    current = generate_contract_manifest()

    if current == snapshot:
        return

    report = compare_contract_manifests(snapshot, current)
    details = [*report.breaking, *report.compatible]
    detail_text = "\n".join(f"- {item}" for item in details[:25])
    pytest.fail(
        "cruxible-client contract snapshot drifted. Run "
        "`uv run python scripts/update_client_contract_snapshot.py` and review "
        "`tests/goldens/cruxible_client/contracts_snapshot.json`."
        + (f"\n\nDetected changes:\n{detail_text}" if detail_text else "")
    )


def test_authoring_program_stamp_commits_the_exact_public_contract_snapshot() -> None:
    manifest = generate_contract_manifest()
    digest = "sha256:" + hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()

    assert digest == AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
    assert SDK_CONTRACT_SNAPSHOT_DIGEST == digest
    assert SUPPORTED_DAEMON_CONTRACTS[AUTHORING_SDK_VERSION] == digest


def test_contract_catalog_contains_only_host_credentials_and_playbill() -> None:
    current = generate_contract_manifest()
    assert set(current["models"]) == {
        "PlaybillAcceptedCoordinate",
        "PlaybillActivationReceipt",
        "PlaybillAuditCoverage",
        "PlaybillAuditCoveredClaim",
        "PlaybillAuditCursor",
        "PlaybillAuditEvidenceRef",
        "PlaybillAuditFactors",
        "PlaybillAuditResult",
        "PlaybillAuditRow",
        "PlaybillAuditScope",
        "PlaybillApprovalChallenge",
        "PlaybillApprovalReceipt",
        "PlaybillBodyRead",
        "PlaybillCasObjectResult",
        "PlaybillDocumentHistory",
        "PlaybillDocumentList",
        "PlaybillDocumentView",
        "PlaybillExplainResult",
        "PlaybillExplainUnsupportedDetail",
        "PlaybillHostResult",
        "PlaybillInitResult",
        "PlaybillPrincipalList",
        "PlaybillProposalInspection",
        "PlaybillProposalReview",
        "PlaybillReviewedMember",
        "PlaybillRefusalInspection",
        "PlaybillSemanticFieldDelta",
        "PlaybillSemanticFieldValue",
        "PlaybillSourceCheckResult",
        "PlaybillSourceContext",
        "PlaybillSubjectView",
        "PlaybillSubjectList",
        "PlaybillSubjectHistory",
        "PlaybillClaimTypeView",
        "PlaybillClaimTypeList",
        "PlaybillClaimTypeInputProposalResult",
        "PlaybillClaimTypeMigrationPreflight",
        "PlaybillClaimTypeMigrationResult",
        "PlaybillClaimTypeMigrationResultV2",
        "PlaybillClaimTypeMigrationResultV3",
        "PlaybillClaimTypeProposalLint",
        "PlaybillClaimView",
        "PlaybillClaimList",
        "PlaybillClaimHistory",
        "PlaybillAuthoringIntentList",
        "PlaybillAuthoringExampleResult",
        "PlaybillAuthoringIntentView",
        "PlaybillAuthoringPreflightResult",
        "PlaybillAuthoringSubmitResult",
        "PlaybillCandidateStatus",
        "PlaybillCaptureAdmissionAccount",
        "PlaybillCaptureEvidenceKindAdmission",
        "PlaybillClaimRetirePreflight",
        "PlaybillClaimRetireResult",
        "PlaybillClaimExplanation",
        "PlaybillClaimExplanationV2",
        "PlaybillClaimExplanationV3",
        "PlaybillClaimViewV2",
        "PlaybillQueryDefinitionView",
        "PlaybillQueryDefinitionList",
        "PlaybillQueryRun",
        "PlaybillProcedureReadiness",
        "PlaybillPolicyInForce",
        "PlaybillPolicyInForceList",
        "PlaybillProcedureBindResult",
        "PlaybillProcedureRunState",
        "PlaybillNextResult",
        "PlaybillDiscoveryResult",
        "PlaybillContextCapsule",
        "PlaybillCurationListResult",
        "PlaybillCurationActionResult",
        "PlaybillCoverageResult",
        "PlaybillFloorFile",
        "PlaybillFloorExport",
        "PlaybillFloorRefreshResult",
        "PlaybillInsertionAbandonResult",
        "PlaybillInsertionConfirmResultV2",
        "PlaybillInsertionPrepareResult",
        "PlaybillPublicationPrepareWarning",
        "PlaybillInterfaceInventory",
        "PlaybillProposalList",
        "PlaybillProposalListEntry",
        "PlaybillProposalReadmitResult",
        "PlaybillProviderInterfaceEntry",
        "PlaybillSearchResult",
        "PlaybillSinceCursor",
        "PlaybillSinceRequest",
        "PlaybillSinceResult",
        "PlaybillSinceRow",
        "PlaybillWhoAmI",
        "PlaybillWorkspaceActivationResult",
        "PlaybillWorkspaceFloorStatus",
        "PlaybillWorkspaceFloorWriteResult",
        "RuntimeCredentialBootstrapResult",
        "RuntimeCredentialListResult",
        "RuntimeCredentialMetadata",
        "RuntimeCredentialResult",
        "ServerInfoResult",
        "ServerRestartResult",
        "ProviderLaneStatusV1",
    }
    assert set(current["literal_aliases"]) == {
        "ApprovalPolicyMode",
        "PlaybillAuthoringExampleName",
        "PlaybillHostStatus",
        "PlaybillNextReason",
        "PlaybillPolicyKind",
        "RuntimeCredentialPermissionMode",
        "ProviderLaneUnavailableCodeV1",
    }


def test_host_and_coordinate_contracts_are_strict() -> None:
    assert set(get_args(contracts.PlaybillHostStatus)) == {"created", "already_exists"}
    assert contracts.PlaybillHostResult.model_config["extra"] == "forbid"
    assert contracts.PlaybillAcceptedCoordinate.model_config["extra"] == "forbid"
    assert set(contracts.PlaybillAcceptedCoordinate.model_fields) == {
        "tag",
        "git_oid",
        "semantic_root",
        "generation_root",
        "compiler_digest",
    }


def _manifest(
    *,
    aliases: dict[str, Any] | None = None,
    models: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "literal_aliases": aliases or {},
        "models": models or {},
    }


def _model(**fields: dict[str, Any]) -> dict[str, Any]:
    required_fields = [
        field_name for field_name, field in fields.items() if field.get("required", False)
    ]
    return {
        "fields": fields,
        "json_schema": {},
        "required_fields": required_fields,
    }


def _field(schema: dict[str, Any], *, required: bool = False) -> dict[str, Any]:
    return {
        "has_default": not required,
        "has_default_factory": False,
        "json_name": "value",
        "required": required,
        "schema": schema,
    }


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            _manifest(models={}),
            "Removed model Result",
        ),
        (
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            _manifest(models={"Result": _model()}),
            "Removed field Result.value",
        ),
        (
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            _manifest(models={"Result": _model(value=_field({"type": "string"}, required=True))}),
            "Field became required Result.value",
        ),
        (
            _manifest(models={"Result": _model()}),
            _manifest(models={"Result": _model(value=_field({"type": "string"}, required=True))}),
            "Added required field Result.value",
        ),
        (
            _manifest(aliases={"Mode": {"values": ["run", "apply"]}}),
            _manifest(aliases={"Mode": {"values": ["run"]}}),
            "Removed Literal value(s) from Mode",
        ),
        (
            _manifest(
                models={
                    "Result": _model(
                        value=_field({"anyOf": [{"type": "string"}, {"type": "null"}]})
                    )
                }
            ),
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            "Narrowed field type Result.value",
        ),
        (
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            _manifest(models={"Result": _model(value=_field({"type": "integer"}))}),
            "Narrowed field type Result.value",
        ),
        (
            _manifest(models={"Result": _model(mode=_field({"enum": ["run", "apply"]}))}),
            _manifest(models={"Result": _model(mode=_field({"enum": ["run"]}))}),
            "Removed enum value(s) from Result.mode",
        ),
    ],
)
def test_contract_compatibility_reports_breaking_changes(
    old: dict[str, Any],
    new: dict[str, Any],
    expected: str,
) -> None:
    report = compare_contract_manifests(old, new)

    assert not report.is_compatible
    assert any(expected in item for item in report.breaking)


def test_contract_compatibility_allows_additive_changes() -> None:
    old = _manifest(
        aliases={"Mode": {"values": ["run"]}},
        models={
            "Result": _model(
                mode=_field({"enum": ["run"]}),
                score=_field({"type": "integer"}),
            )
        },
    )
    new = _manifest(
        aliases={"Mode": {"values": ["run", "apply"]}},
        models={
            "AddedResult": _model(value=_field({"type": "string"})),
            "Result": _model(
                detail=_field({"type": "string"}),
                mode=_field({"enum": ["run", "apply"]}),
                score=_field({"type": "number"}),
            ),
        },
    )

    report = compare_contract_manifests(old, new)

    assert report.breaking == ()
    assert report.is_compatible
    assert "Added model AddedResult" in report.compatible
    assert "Added optional field Result.detail" in report.compatible
    assert any("Added Literal value(s) to Mode" in item for item in report.compatible)
    assert any("Added enum value(s) to Result.mode" in item for item in report.compatible)
