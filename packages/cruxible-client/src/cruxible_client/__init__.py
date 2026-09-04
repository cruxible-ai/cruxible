"""Client package for talking to a governed Cruxible daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.authoring.attestations import (
        ClaimAttestationV2Signer,
        LocalEd25519ClaimAttestationSigner,
    )
    from cruxible_client.authoring.sdk import Playbill, Prediction, PredictionSettlement
    from cruxible_client.authoring.sdk_types import (
        AbsentSubject,
        AccessProfile,
        ActivationPolicy,
        Audience,
        CapabilityNotServed,
        CaptureRef,
        Cardinality,
        ClaimObjectKind,
        ClaimRef,
        ClaimRole,
        ClaimTypeRef,
        Disposition,
        Duration,
        EffectivePeriod,
        LiteralSchemaError,
        LiteralValue,
        LiteralValueTypeError,
        PendingClaimTypeRef,
        PendingSubjectRef,
        ProcedureRef,
        QueryRef,
        ReferentSensitivity,
        SlotRef,
        SourceRef,
        SubjectRef,
        TypedRef,
    )
    from cruxible_client.authoring.workspace import (
        PlaybillWorkspaceError,
        activate_with_workspace_refresh,
        inspect_workspace_floor,
        materialize_playbill_floor,
        observe_playbill_next_workspace,
    )
    from cruxible_client.authoring.world import (
        KindNamespace,
        World,
        WorldClaimType,
        WorldStructureError,
        WorldSubject,
    )
    from cruxible_client.contracts.artifacts import (
        ArtifactIdentity,
        ArtifactLifecycle,
        ArtifactPin,
    )
    from cruxible_client.contracts.captures import CanonicalDurationV1
    from cruxible_client.contracts.policies import (
        ClaimAdmissionPolicyV1,
        ClaimResolutionPolicyV1,
    )
    from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContractV1
    from cruxible_client.contracts.procedures.contract_schema import (
        ContractSchema,
        PropertySchema,
    )
    from cruxible_client.contracts.procedures.models import (
        ProcedureBudgetV3,
        ProcedureDefinitionV3,
        ProcedureHardCapsV3,
        ProcedurePinSlotRefV1,
        ProcedurePinSlotV1,
        ProjectNodeV3,
        StateTapNodeV3,
        TransformNodeV3,
    )
    from cruxible_client.transport.http import CruxibleClient

__all__ = [
    "AbsentSubject",
    "AccessProfile",
    "ActivationPolicy",
    "Audience",
    "ArtifactIdentity",
    "ArtifactLifecycle",
    "ArtifactPin",
    "CapabilityNotServed",
    "Cardinality",
    "CaptureRef",
    "ClaimObjectKind",
    "ClaimAdmissionPolicyV1",
    "ClaimAttestationV2Signer",
    "ClaimRef",
    "ClaimRole",
    "ClaimTypeRef",
    "ClaimResolutionPolicyV1",
    "CruxibleClient",
    "Disposition",
    "Duration",
    "CanonicalDurationV1",
    "ContractSchema",
    "EffectivePeriod",
    "KindNamespace",
    "LiteralSchemaError",
    "LiteralValue",
    "LiteralValueTypeError",
    "LocalEd25519ClaimAttestationSigner",
    "Playbill",
    "Prediction",
    "PredictionSettlement",
    "PendingClaimTypeRef",
    "PendingSubjectRef",
    "PlaybillInsertionApplication",
    "PlaybillInsertionApplyError",
    "PlaybillWorkspaceError",
    "activate_with_workspace_refresh",
    "inspect_workspace_floor",
    "observe_playbill_next_workspace",
    "materialize_playbill_floor",
    "ProcedureRef",
    "ProcedureBudgetV3",
    "ProcedureDefinitionV3",
    "ProcedureHardCapsV3",
    "ProcedureOwnedContractV1",
    "ProcedurePinSlotRefV1",
    "ProcedurePinSlotV1",
    "ProjectNodeV3",
    "PropertySchema",
    "QueryRef",
    "ReferentSensitivity",
    "SlotRef",
    "SourceRef",
    "StateTapNodeV3",
    "SubjectRef",
    "TypedRef",
    "TransformNodeV3",
    "World",
    "WorldClaimType",
    "WorldStructureError",
    "WorldSubject",
]

__version__ = "0.5.1"


def __getattr__(name: str) -> Any:
    """Load the HTTP client only when the public client class is requested."""
    if name == "CruxibleClient":
        from cruxible_client.transport.http import CruxibleClient

        return CruxibleClient
    if name in {"Playbill", "Prediction", "PredictionSettlement"}:
        from cruxible_client.authoring import sdk

        return getattr(sdk, name)
    if name in {"ClaimAttestationV2Signer", "LocalEd25519ClaimAttestationSigner"}:
        from cruxible_client.authoring import attestations

        return getattr(attestations, name)
    if name in {
        "AbsentSubject",
        "AccessProfile",
        "ActivationPolicy",
        "Audience",
        "CapabilityNotServed",
        "Cardinality",
        "CaptureRef",
        "ClaimObjectKind",
        "ClaimRef",
        "ClaimRole",
        "ClaimTypeRef",
        "Disposition",
        "Duration",
        "EffectivePeriod",
        "LiteralSchemaError",
        "LiteralValue",
        "LiteralValueTypeError",
        "PendingClaimTypeRef",
        "PendingSubjectRef",
        "ProcedureRef",
        "QueryRef",
        "ReferentSensitivity",
        "SlotRef",
        "SourceRef",
        "SubjectRef",
        "TypedRef",
    }:
        from cruxible_client.authoring import sdk_types

        return getattr(sdk_types, name)
    if name in {
        "KindNamespace",
        "World",
        "WorldClaimType",
        "WorldStructureError",
        "WorldSubject",
    }:
        from cruxible_client.authoring import world

        return getattr(world, name)
    if name in {"ArtifactIdentity", "ArtifactLifecycle", "ArtifactPin"}:
        from cruxible_client.contracts import artifacts as artifact_models

        return getattr(artifact_models, name)
    if name == "CanonicalDurationV1":
        from cruxible_client.contracts import captures

        return captures.CanonicalDurationV1
    if name in {"ClaimAdmissionPolicyV1", "ClaimResolutionPolicyV1"}:
        from cruxible_client.contracts import policies

        return getattr(policies, name)
    if name == "ProcedureOwnedContractV1":
        from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContractV1

        return ProcedureOwnedContractV1
    if name in {"ContractSchema", "PropertySchema"}:
        from cruxible_client.contracts.procedures import contract_schema

        return getattr(contract_schema, name)
    if name in {
        "ProcedureBudgetV3",
        "ProcedureDefinitionV3",
        "ProcedureHardCapsV3",
        "ProcedurePinSlotRefV1",
        "ProcedurePinSlotV1",
        "ProjectNodeV3",
        "StateTapNodeV3",
        "TransformNodeV3",
    }:
        from cruxible_client.contracts.procedures import models

        return getattr(models, name)
    if name in {
        "PlaybillInsertionApplication",
        "PlaybillInsertionApplyError",
    }:
        from cruxible_client.authoring import insertions

        return getattr(insertions, name)
    if name in {
        "PlaybillWorkspaceError",
        "activate_with_workspace_refresh",
        "inspect_workspace_floor",
        "observe_playbill_next_workspace",
        "materialize_playbill_floor",
    }:
        from cruxible_client.authoring import workspace

        return getattr(workspace, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
