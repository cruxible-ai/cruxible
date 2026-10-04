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
    "sha256:e54b561ac1b0667d737d6b6f69b256ad5369d65f686e7943cbf19c55fe2b4543"
)

AUTHORING_WIRE_MODEL_NAMES = (
    "AcceptanceCondition",
    "ApprovalPolicyAuthoringPayload",
    "AttestationAuthoringPayload",
    "AuthoringArtifactReference",
    "AuthoringCandidateReference",
    "AuthoringClaimStatement",
    "AuthoringDiagnostic",
    "AuthoringExactContentObject",
    "AuthoringExistingClaimDisposition",
    "AuthoringIntentCompileRequestV1",
    "AuthoringIntentCompileRequestV2",
    "AuthoringIntentCompileRequest",
    "AuthoringIntentCreateRequestV1",
    "AuthoringIntentCreateRequestV2",
    "AuthoringIntentCreateRequest",
    "AuthoringIntentList",
    "AuthoringIntentPreflightRequest",
    "AuthoringIntentSubmitRequest",
    "AuthoringIntentV1",
    "AuthoringIntent",
    "AuthoringIntentView",
    "AuthoringProgramOperation",
    "AuthoringProgramStamp",
    "AuthoringReferenceExpectation",
    "AuthoringReferenceSuccessor",
    "AuthoringSlotExpectation",
    "AuthoringSubmitMember",
    "AuthoringSubmitResult",
    "BlockedCheck",
    "CandidateStatus",
    "CaptureContractAuthoringPayload",
    "ChangeSetAuthoringPayload",
    "ChangeSetClaimIdentity",
    "ClaimAuthoringPayloadV1",
    "ClaimAuthoringPayloadV2",
    "ClaimAuthoringPayload",
    "ClaimDependencyDrafts",
    "ClaimDerivationBinding",
    "ClaimRetirementMember",
    "ClaimTypeAuthoringPayload",
    "ClaimTypeSuccessionDependent",
    "ClaimTypeSuccessionMember",
    "DiagnosticFrontierLimits",
    "DiagnosticFrontier",
    "ExistingCaptureCitationSource",
    "InsertionAbandonRequest",
    "InsertionAbandonResult",
    "InsertionAnchorWindow",
    "InsertionConfirmRequest",
    "InsertionConfirmResult",
    "InsertionConfirmationObservation",
    "InsertionExpectation",
    "InsertionPrepareRequest",
    "InsertionPrepareResult",
    "InsertionTarget",
    "InsertionTerminalTombstone",
    "LineAuthoringPayload",
    "MandateConditionAuthoring",
    "MandateScopeAuthoring",
    "PlaybillBlockDetachResult",
    "PlaybillBlockSyncItem",
    "PlaybillBlockSyncReadRequest",
    "PlaybillBlockSyncReadResult",
    "PlaybillBlockSyncResult",
    "PlaybillBlockSyncSuccessorCandidate",
    "PlaybillProjectionCheckRequest",
    "PlaybillProjectionCheckResult",
    "PreflightCertificate",
    "PreflightResult",
    "ProcedureAuthoringPayloadV1",
    "ProcedureAuthoringPayload",
    "ProcedureMandateAuthoringPayload",
    "ProcedureRuntimePolicyAuthoringPayload",
    "ProjectionDependencyIssue",
    "PublicationPreparation",
    "PublicationPrepareWarning",
    "PublicationSourceObservation",
    "QueryDefinitionAuthoringPayload",
    "RepairAlternative",
    "ResolutionContractAuthoringPayload",
    "SelfSourceBody",
    "SourceAcquisitionPolicyAuthoringPayload",
    "SubjectAuthoringPayload",
    "TriggerAuthoringPayload",
    "WorkingAnchorWindow",
    "WorkingDigestCoordinate",
    "WorkingGitBlobCoordinate",
    "WorkingSelectionObservation",
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
