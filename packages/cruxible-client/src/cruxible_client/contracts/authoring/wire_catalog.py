"""Frozen catalog of every public Pydantic authoring wire model.

This is deliberately separate from ``AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST``.
That digest identifies the audited top-level read/response surface; before the
lineage's first public release it can be re-pinned only atomically with the SDK
handshake, program stamp, snapshot, and guardrail. After first public release,
every change requires a coordinated version succession. This independent
catalog freezes the deeper request, payload, intent, and insertion closure.
"""

from __future__ import annotations

import hashlib
import inspect
from typing import Any, cast

from pydantic import BaseModel

from cruxible_client.contracts.authoring import models
from cruxible_client.contracts.primitives import canonical_json

AUTHORING_WIRE_CATALOG_VERSION = 1
AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST = (
    "sha256:9c7ba995eebd3be8f0e0c0da04af9d69931ef7f2c5e32418e3d50c1083d77823"
)

AUTHORING_WIRE_MODEL_NAMES = (
    "AcceptanceConditionV1",
    "ApprovalPolicyAuthoringPayloadV1",
    "AuthoringArtifactReferenceV1",
    "AuthoringCandidateReferenceV1",
    "AuthoringClaimStatementV1",
    "AuthoringDiagnosticV1",
    "AuthoringExactContentObjectV1",
    "AuthoringExistingClaimDispositionV1",
    "AuthoringIntentCompileRequestV1",
    "AuthoringIntentCompileRequestV2",
    "AuthoringIntentCompileRequestV3",
    "AuthoringIntentCreateRequestV1",
    "AuthoringIntentCreateRequestV2",
    "AuthoringIntentCreateRequestV3",
    "AuthoringIntentListV1",
    "AuthoringIntentPreflightRequestV1",
    "AuthoringIntentSubmitRequestV1",
    "AuthoringIntentV1",
    "AuthoringIntentV2",
    "AuthoringIntentViewV1",
    "AuthoringProgramOperationV1",
    "AuthoringProgramStampV1",
    "AuthoringReferenceExpectationV1",
    "AuthoringReferenceSuccessorV1",
    "AuthoringSubmitMemberV1",
    "AuthoringSubmitResultV1",
    "BlockedCheckV1",
    "CandidateStatusV1",
    "ChangeSetAuthoringPayloadV1",
    "ChangeSetClaimIdentityV1",
    "ClaimAuthoringPayloadV1",
    "ClaimAuthoringPayloadV2",
    "ClaimAuthoringPayloadV3",
    "ClaimDependencyDraftsV1",
    "ClaimRetirementMemberV1",
    "ClaimTypeAuthoringPayloadV1",
    "ClaimTypeSuccessionDependentV1",
    "ClaimTypeSuccessionMemberV1",
    "DiagnosticFrontierLimitsV1",
    "DiagnosticFrontierV1",
    "ExistingCaptureCitationSourceV1",
    "InsertionAbandonRequestV1",
    "InsertionAbandonResultV1",
    "InsertionAnchorWindowV1",
    "InsertionConfirmRequestV2",
    "InsertionConfirmResultV2",
    "InsertionConfirmationObservationV2",
    "InsertionExpectationV2",
    "InsertionPrepareRequestV2",
    "InsertionPrepareResultV2",
    "InsertionTargetV2",
    "InsertionTerminalTombstoneV2",
    "PlaybillBlockSyncItemV1",
    "PlaybillBlockSyncReadRequestV1",
    "PlaybillBlockSyncReadResultV1",
    "PlaybillBlockSyncResultV1",
    "PlaybillBlockSyncSuccessorCandidateV1",
    "PreflightCertificateV1",
    "PreflightResultV1",
    "ProcedureAuthoringPayloadV1",
    "ProcedureAuthoringPayloadV2",
    "ProcedureMandateAuthoringPayloadV1",
    "ProcedureRuntimePolicyAuthoringPayloadV1",
    "PublicationPreparationV2",
    "PublicationPrepareWarningV1",
    "PublicationSourceObservationV2",
    "QueryDefinitionAuthoringPayloadV1",
    "RepairAlternativeV1",
    "SelfSourceBodyV1",
    "SubjectAuthoringPayloadV1",
    "WorkingAnchorWindowV1",
    "WorkingDigestCoordinateV1",
    "WorkingGitBlobCoordinateV1",
    "WorkingSelectionObservationV1",
)


def discovered_authoring_wire_model_names() -> tuple[str, ...]:
    """Discover the complete public model inventory; the frozen tuple must match."""

    discovered: list[str] = []
    for name, value in vars(models).items():
        if name.startswith("_") or not inspect.isclass(value):
            continue
        if issubclass(value, BaseModel) and value.__module__ == models.__name__:
            discovered.append(name)
    return tuple(sorted(discovered))


def generate_authoring_wire_contract_catalog() -> dict[str, Any]:
    """Return the deterministic schema catalog committed by the frozen digest."""

    schemas: dict[str, Any] = {}
    for name in AUTHORING_WIRE_MODEL_NAMES:
        model = cast(type[BaseModel], getattr(models, name))
        model.model_rebuild()
        schemas[name] = model.model_json_schema(ref_template="#/$defs/{model}")
    return {
        "catalog_version": AUTHORING_WIRE_CATALOG_VERSION,
        "module": models.__name__,
        "models": schemas,
    }


def authoring_wire_contract_catalog_digest() -> str:
    """Digest the catalog with deterministic RFC-compliant JSON spelling."""

    content = canonical_json(generate_authoring_wire_contract_catalog()).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


__all__ = [
    "AUTHORING_WIRE_CATALOG_VERSION",
    "AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST",
    "AUTHORING_WIRE_MODEL_NAMES",
    "authoring_wire_contract_catalog_digest",
    "discovered_authoring_wire_model_names",
    "generate_authoring_wire_contract_catalog",
]
