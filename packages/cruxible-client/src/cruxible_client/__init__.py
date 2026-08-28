"""Client package for talking to a governed Cruxible daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.authoring.sdk import Playbill
    from cruxible_client.authoring.sdk_types import (
        AccessProfile,
        ActivationPolicy,
        Audience,
        CapabilityNotServed,
        Cardinality,
        ClaimObjectKind,
        ClaimRef,
        ClaimRole,
        ClaimTypeRef,
        Disposition,
        Duration,
        EffectivePeriod,
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
    "AccessProfile",
    "ActivationPolicy",
    "Audience",
    "ArtifactIdentity",
    "ArtifactLifecycle",
    "ArtifactPin",
    "CapabilityNotServed",
    "Cardinality",
    "ClaimObjectKind",
    "ClaimAdmissionPolicyV1",
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
    "Playbill",
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
]

__version__ = "0.4.0"


def __getattr__(name: str) -> Any:
    """Load the HTTP client only when the public client class is requested."""
    if name == "CruxibleClient":
        from cruxible_client.transport.http import CruxibleClient

        return CruxibleClient
    if name == "Playbill":
        from cruxible_client.authoring.sdk import Playbill

        return Playbill
    if name in {
        "AccessProfile",
        "ActivationPolicy",
        "Audience",
        "CapabilityNotServed",
        "Cardinality",
        "ClaimObjectKind",
        "ClaimRef",
        "ClaimRole",
        "ClaimTypeRef",
        "Disposition",
        "Duration",
        "EffectivePeriod",
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
