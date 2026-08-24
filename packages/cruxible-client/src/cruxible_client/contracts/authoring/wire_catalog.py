"""Frozen catalog of every public Pydantic authoring wire model.

This is deliberately separate from ``AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST``.
That older digest continues to identify the top-level read/response surface and
its preimage is unchanged.  This catalog independently freezes the deeper
authoring request, payload, intent, diagnostic, and insertion model closure.
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
    "sha256:f8c4946bd4d5e4fb872dc933f1289f0b76492fb467c70608cc3325302c5cf49f"
)

AUTHORING_WIRE_MODEL_NAMES = (
    "AcceptanceConditionV1",
    "AuthoringArtifactReferenceV1",
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
    "AuthoringSubmitResultV1",
    "BlockedCheckV1",
    "CandidateStatusV1",
    "ClaimAuthoringPayloadV1",
    "ClaimAuthoringPayloadV2",
    "ClaimDependencyDraftsV1",
    "DiagnosticFrontierLimitsV1",
    "DiagnosticFrontierV1",
    "InsertionAbandonRequestV1",
    "InsertionAbandonResultV1",
    "InsertionAnchorWindowV1",
    "InsertionConfirmRequestV1",
    "InsertionConfirmResultV1",
    "InsertionConfirmationObservationV1",
    "InsertionExpectationV1",
    "InsertionPatchEnvelopeV1",
    "InsertionTargetV1",
    "InsertionTerminalTombstoneV1",
    "PreflightCertificateV1",
    "PreflightResultV1",
    "ProcedureAuthoringPayloadV1",
    "ProcedureAuthoringPayloadV2",
    "RepairAlternativeV1",
    "SelfSourceBodyV1",
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
