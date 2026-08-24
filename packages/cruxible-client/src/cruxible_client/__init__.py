"""Client package for talking to a governed Cruxible daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.authoring.sdk import Playbill
    from cruxible_client.authoring.sdk_types import (
        AccessProfile,
        ActivationPolicy,
        Audience,
        BriefClaimExpectation,
        BriefKind,
        BriefQueryRender,
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
    from cruxible_client.transport.http import CruxibleClient

__all__ = [
    "AccessProfile",
    "ActivationPolicy",
    "Audience",
    "BriefClaimExpectation",
    "BriefKind",
    "BriefQueryRender",
    "CapabilityNotServed",
    "Cardinality",
    "ClaimObjectKind",
    "ClaimRef",
    "ClaimRole",
    "ClaimTypeRef",
    "CruxibleClient",
    "Disposition",
    "Duration",
    "EffectivePeriod",
    "Playbill",
    "PlaybillInsertionApplication",
    "PlaybillInsertionApplyError",
    "PlaybillWorkspaceError",
    "activate_with_workspace_refresh",
    "apply_playbill_insertion",
    "inspect_workspace_floor",
    "observe_playbill_next_workspace",
    "materialize_playbill_floor",
    "prepare_playbill_brief",
    "ProcedureRef",
    "QueryRef",
    "ReferentSensitivity",
    "SlotRef",
    "SourceRef",
    "SubjectRef",
    "TypedRef",
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
        "BriefClaimExpectation",
        "BriefKind",
        "BriefQueryRender",
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
    if name in {
        "PlaybillInsertionApplication",
        "PlaybillInsertionApplyError",
        "apply_playbill_insertion",
    }:
        from cruxible_client.authoring import insertions

        return getattr(insertions, name)
    if name == "prepare_playbill_brief":
        from cruxible_client.authoring.briefs import prepare_playbill_brief

        return prepare_playbill_brief
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
