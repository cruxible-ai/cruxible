"""Contract-freeze tests for the reduced Playbill client surface."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from fastapi.testclient import TestClient

from cruxible_client import __version__ as CLIENT_VERSION
from cruxible_client import contracts
from cruxible_client.authoring.sdk import SDK_CONTRACT_SNAPSHOT_DIGEST
from cruxible_client.contracts.authoring.models import AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.transport.http import CruxibleClient
from cruxible_core import __version__ as DAEMON_VERSION
from cruxible_core.server.app import create_app
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


def test_current_daemon_serves_the_snapshot_used_by_the_sdk_handshake() -> None:
    assert CLIENT_VERSION == DAEMON_VERSION
    with TestClient(create_app()) as server:
        response = server.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "version": DAEMON_VERSION,
        "sdk_contract_snapshot_digest": SDK_CONTRACT_SNAPSHOT_DIGEST,
    }


def test_client_reads_package_version_and_served_snapshot_from_version_probe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/version"
        return httpx.Response(
            200,
            json={
                "version": DAEMON_VERSION,
                "sdk_contract_snapshot_digest": SDK_CONTRACT_SNAPSHOT_DIGEST,
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[assignment]
        base_url="http://cruxible",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client._version_info() == (DAEMON_VERSION, SDK_CONTRACT_SNAPSHOT_DIGEST)
        assert client.version() == DAEMON_VERSION
    finally:
        client.close()


def test_contract_catalog_contains_only_host_credentials_and_playbill() -> None:
    current = generate_contract_manifest()
    assert set(current["models"]) == {
        "ClaimStatementCardV1",
        "GitWorkspaceNoteV1",
        "IsolatedExecutorRegistrationV1",
        "ObservationSettlementEvidenceV1",
        "PlaybillAcceptedCoordinate",
        "PlaybillActivationReceipt",
        "PlaybillApprovalChallenge",
        "PlaybillApprovalReceipt",
        "PlaybillAuditCoverage",
        "PlaybillAuditCoveredClaim",
        "PlaybillAuditCursor",
        "PlaybillAuditEvidenceRef",
        "PlaybillAuditFactors",
        "PlaybillAuditResult",
        "PlaybillAuditRow",
        "PlaybillAuditScope",
        "PlaybillAuthoringExampleResult",
        "PlaybillAuthoringIntentList",
        "PlaybillAuthoringIntentView",
        "PlaybillAuthoringPreflightResult",
        "PlaybillAuthoringSubmitResult",
        "PlaybillBlockDeclareResultV1",
        "PlaybillBlockDepublishResultV1",
        "PlaybillBlockSyncItemV1",
        "PlaybillBlockSyncReadRequestV1",
        "PlaybillBlockSyncReadResultV1",
        "PlaybillBlockSyncResultV1",
        "PlaybillBlockSyncSuccessorCandidateV1",
        "PlaybillBodyRead",
        "PlaybillCandidateStatus",
        "PlaybillCaptureAdmissionAccount",
        "PlaybillCaptureEvidenceKindAdmission",
        "PlaybillCasObjectResult",
        "PlaybillClaimExplanation",
        "PlaybillClaimExplanationV2",
        "PlaybillClaimExplanationV3",
        "PlaybillClaimHistory",
        "PlaybillClaimList",
        "PlaybillClaimRetirePreflight",
        "PlaybillClaimRetireResult",
        "PlaybillClaimTypeInputProposalResult",
        "PlaybillClaimTypeList",
        "PlaybillClaimTypeMigrationPreflight",
        "PlaybillClaimTypeMigrationResult",
        "PlaybillClaimTypeMigrationResultV2",
        "PlaybillClaimTypeMigrationResultV3",
        "PlaybillClaimTypeProposalLint",
        "PlaybillClaimTypeView",
        "PlaybillClaimView",
        "PlaybillClaimViewV2",
        "PlaybillContextCapsule",
        "PlaybillCoverageResult",
        "PlaybillCurationActionResult",
        "PlaybillCurationListResult",
        "PlaybillDiscoveryResult",
        "PlaybillDocumentHistory",
        "PlaybillDocumentList",
        "PlaybillDocumentView",
        "PlaybillExplainResult",
        "PlaybillExplainUnsupportedDetail",
        "PlaybillFloorExport",
        "PlaybillFloorFile",
        "PlaybillFloorRefreshResult",
        "PlaybillHostCompatibilityReasonV1",
        "PlaybillHostInspectionV1",
        "PlaybillHostResult",
        "PlaybillHostWorkspaceRegistrationV1",
        "PlaybillInitResult",
        "PlaybillInsertionAbandonResult",
        "PlaybillInsertionConfirmResultV2",
        "PlaybillInsertionPrepareResult",
        "PlaybillInstanceDecommissionResultV1",
        "PlaybillInterfaceInventory",
        "PlaybillNextResult",
        "PlaybillPolicyInForce",
        "PlaybillPolicyInForceList",
        "PlaybillPredictRequestV1",
        "PlaybillPredictResultV1",
        "PlaybillPredictionDeclarationV1",
        "PlaybillPrincipalList",
        "PlaybillProcedureBindResult",
        "PlaybillProcedureReadiness",
        "PlaybillProcedureRunState",
        "PlaybillProjectionAdvisory",
        "PlaybillProjectionEvidence",
        "PlaybillProposalInspection",
        "PlaybillProposalList",
        "PlaybillProposalListEntry",
        "PlaybillProposalReadmitResult",
        "PlaybillProposalReview",
        "PlaybillProposalSelectorResultV1",
        "PlaybillProposalWithdrawResult",
        "PlaybillProviderInterfaceEntry",
        "PlaybillProviderSeedResultV1",
        "PlaybillPublicationPrepareWarning",
        "PlaybillQueryDefinitionList",
        "PlaybillQueryDefinitionView",
        "PlaybillQueryRun",
        "PlaybillRefusalInspection",
        "PlaybillReviewedMember",
        "PlaybillSearchResult",
        "PlaybillSemanticFieldDelta",
        "PlaybillSemanticFieldValue",
        "PlaybillSettleRequestV1",
        "PlaybillSettleResultV1",
        "PlaybillSinceCursor",
        "PlaybillSinceRequest",
        "PlaybillSinceResult",
        "PlaybillSinceRow",
        "PlaybillSourceCheckResult",
        "PlaybillSourceContext",
        "PlaybillSubjectHistory",
        "PlaybillSubjectIncomingClaimV1",
        "PlaybillSubjectIncomingGroupV1",
        "PlaybillSubjectList",
        "PlaybillSubjectView",
        "PlaybillWhoAmI",
        "PlaybillWorkspaceActivationResult",
        "PlaybillWorkspaceAdvertisement",
        "PlaybillWorkspaceAttachResultV1",
        "PlaybillWorkspaceDetachResultV1",
        "PlaybillWorkspaceFloorStatus",
        "PlaybillWorkspaceFloorWriteResult",
        "PredictionEqualityRuleV1",
        "PredictionObservationSelectorV1",
        "PredictionPresenceRuleV1",
        "PredictionThresholdRuleV1",
        "ProcedurePendingSuccessorV1",
        "ProcedureRunAttributionV1",
        "ProcedureRunReceiptV2",
        "ProcedureRunReceiptV3",
        "ProcedureRunReceiptV4",
        "ProcedureRunReceiptV5",
        "ProcedureRunReceiptV6",
        "ProviderLaneStatusV1",
        "RuntimeCredentialBootstrapResult",
        "RuntimeCredentialListResult",
        "RuntimeCredentialMetadata",
        "RuntimeCredentialResult",
        "ServerInfoResult",
        "ServerRestartResult",
        "ServerStopResult",
        "SourceReadReceiptV1",
        "TerminalSettlementEvidenceV1",
        "WorkspaceFileSourceRequestV1",
    }
    assert set(current["literal_aliases"]) == {
        "ApprovalPolicyMode",
        "PlaybillAuthoringExampleName",
        "PlaybillHandEditNextReason",
        "PlaybillHostCompatibilityReasonCodeV1",
        "PlaybillHostCompatibilityV1",
        "PlaybillHostStatus",
        "PlaybillHostWorkspaceRegistrationStatus",
        "PlaybillNextReason",
        "PlaybillPolicyKind",
        "RuntimeCredentialPermissionMode",
        "ProviderLaneUnavailableCodeV1",
        "ProviderSeedRepairV1",
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


def test_no_model_the_contracts_namespace_publishes_is_undeclared() -> None:
    """Card 83: the snapshot covered only models WRITTEN in `contracts/__init__.py`.

    The package has no `__all__`; its export surface is the `X as X` import
    idiom, and the old `__module__ == contracts.__name__` filter dropped every
    model that reached the namespace that way. Twenty-seven models exported on
    purpose therefore moved no pin, and fields real clients see -- a receipt's
    requested path among them -- changed under a frozen surface without the
    freeze noticing. The namespace is the declaration; anything in it is
    covered, and adding a name to it is a pin movement.

    `published` deliberately carries NO module filter beyond excluding
    `pydantic.BaseModel` itself. A `cruxible_client.contracts.*` prefix here
    would be the same predicate `_public_models()` uses to compute `covered`,
    so `published - covered` would be empty by construction and the assertion
    could never fire. Without the filter a model defined anywhere -- another
    `cruxible_client` module, or a third-party package -- that reaches the
    namespace through the `X as X` idiom is published, and this test says so.
    """

    import inspect

    from pydantic import BaseModel

    from cruxible_client import contracts

    published = {
        name
        for name, value in vars(contracts).items()
        if not name.startswith("_")
        and inspect.isclass(value)
        and issubclass(value, BaseModel)
        and value is not BaseModel
    }
    covered = set(generate_contract_manifest()["models"])

    assert published - covered == set(), (
        "these models are published from cruxible_client.contracts and pinned nowhere"
    )
