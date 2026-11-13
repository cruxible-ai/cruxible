"""Client package for talking to a governed Cruxible daemon.

Start with ``cx = Cruxible.connect()``, then read: ``cx.orient()`` maps
accepted state, ``cx.query(kind, ...)`` answers questions over it, ``cx.get(ref)``
opens one thing, and the exported floor (``.cruxible/floor/current/``) is
greppable. ``cx.world().describe()`` names every verb and the vocabulary;
``cx.next(...)`` says what needs attention.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.authoring.approval import ApprovalReviewMismatch, ReviewedProposal
    from cruxible_client.authoring.attestations import (
        ClaimAttestationSigner,
        LocalEd25519ClaimAttestationSigner,
    )
    from cruxible_client.authoring.compact_query import CompactQuery, QueryNameError, QueryResult
    from cruxible_client.authoring.sdk import Cruxible, Prediction, PredictionSettlement
    from cruxible_client.authoring.sdk_types import (
        AbsentSubject,
        AccessProfile,
        ActivationPolicy,
        CapabilityNotServed,
        CaptureRef,
        CaptureView,
        Cardinality,
        ClaimRef,
        ClaimRole,
        ClaimRoleNotPermittedError,
        ClaimTypeRef,
        ClaimValueError,
        Disposition,
        Duration,
        EffectivePeriod,
        ExactContent,
        ExactContentTypeError,
        LiteralValue,
        LiteralValueTypeError,
        PendingClaimTypeRef,
        PendingSubjectRef,
        ProcedureRef,
        QueryRef,
        ReferentSensitivity,
        SourceRef,
        SubjectRef,
        TypedRef,
        WholeValueUnavailable,
    )
    from cruxible_client.authoring.signing import ApprovalSigner, LocalEd25519ApprovalSigner
    from cruxible_client.authoring.workspace import (
        WorkspaceError,
        observe_next_workspace,
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
    from cruxible_client.contracts.captures import CanonicalDuration
    from cruxible_client.contracts.policies import (
        ClaimAdmissionPolicy,
        ClaimResolutionPolicy,
    )
    from cruxible_client.contracts.procedures.models import (
        ProcedureBudget,
        ProcedureDefinition,
        ProcedureHardCaps,
        ProcedureSlot,
        ProjectNode,
        ReadNode,
        TransformNode,
    )
    from cruxible_client.contracts.read_values import (
        ShownObject,
        TruncatedText,
    )
    from cruxible_client.contracts.write import NoneValue, SlotRef
    from cruxible_client.transport.http import CruxibleClient


__all__ = [
    "ApprovalReviewMismatch",
    "ReviewedProposal",
    "ApprovalSigner",
    "LocalEd25519ApprovalSigner",
    "AbsentSubject",
    "AccessProfile",
    "ActivationPolicy",
    "ArtifactIdentity",
    "ArtifactLifecycle",
    "ArtifactPin",
    "CapabilityNotServed",
    "Cardinality",
    "CompactQuery",
    "CaptureRef",
    "CaptureView",
    "ClaimAdmissionPolicy",
    "ClaimAttestationSigner",
    "ClaimRef",
    "ClaimRole",
    "ClaimTypeRef",
    "ClaimResolutionPolicy",
    "CruxibleClient",
    "Disposition",
    "Duration",
    "CanonicalDuration",
    "EffectivePeriod",
    "ExactContent",
    "ClaimRoleNotPermittedError",
    "ExactContentTypeError",
    "KindNamespace",
    "ClaimValueError",
    "LiteralValue",
    "LiteralValueTypeError",
    "LocalEd25519ClaimAttestationSigner",
    "PendingClaimTypeRef",
    "PendingSubjectRef",
    "Cruxible",
    "Prediction",
    "PredictionSettlement",
    "WorkspaceError",
    "observe_next_workspace",
    "ProcedureRef",
    "ProcedureBudget",
    "ProcedureDefinition",
    "ProcedureHardCaps",
    "ProcedureSlot",
    "ProjectNode",
    "QueryNameError",
    "QueryRef",
    "QueryResult",
    "ReferentSensitivity",
    "NoneValue",
    "SlotRef",
    "SourceRef",
    "ReadNode",
    "SubjectRef",
    "TypedRef",
    "TransformNode",
    "World",
    "WorldClaimType",
    "WorldStructureError",
    "WorldSubject",
    "ShownObject",
    "TruncatedText",
    "WholeValueUnavailable",
]

__version__ = "0.5.1"


def __dir__() -> list[str]:
    """Every public name, though most load only on first use.

    Next: ``Cruxible.connect(...)`` to open a connection, then ``cx.orient()``.
    """

    return sorted({*__all__, "__version__"})


def __getattr__(name: str) -> Any:
    """Load public adapters only when requested."""
    if name in {"ApprovalReviewMismatch", "ReviewedProposal"}:
        from cruxible_client.authoring import approval

        return getattr(approval, name)
    if name in {"ApprovalSigner", "LocalEd25519ApprovalSigner"}:
        from cruxible_client.authoring import signing

        return getattr(signing, name)
    if name == "CruxibleClient":
        from cruxible_client.transport.http import CruxibleClient

        return CruxibleClient
    if name in {"Cruxible", "Prediction", "PredictionSettlement"}:
        from cruxible_client.authoring import sdk

        return getattr(sdk, name)
    if name in {"CompactQuery", "QueryNameError", "QueryResult"}:
        from cruxible_client.authoring import compact_query

        return getattr(compact_query, name)
    if name in {"ClaimAttestationSigner", "LocalEd25519ClaimAttestationSigner"}:
        from cruxible_client.authoring import attestations

        return getattr(attestations, name)
    if name in {
        "AbsentSubject",
        "AccessProfile",
        "ActivationPolicy",
        "CapabilityNotServed",
        "Cardinality",
        "CaptureRef",
        "CaptureView",
        "ClaimRef",
        "ClaimRole",
        "ClaimTypeRef",
        "Disposition",
        "Duration",
        "EffectivePeriod",
        "ExactContent",
        "ClaimRoleNotPermittedError",
        "ExactContentTypeError",
        "ClaimValueError",
        "LiteralValue",
        "LiteralValueTypeError",
        "PendingClaimTypeRef",
        "PendingSubjectRef",
        "ProcedureRef",
        "QueryRef",
        "ReferentSensitivity",
        "SourceRef",
        "SubjectRef",
        "TypedRef",
        "WholeValueUnavailable",
    }:
        from cruxible_client.authoring import sdk_types

        return getattr(sdk_types, name)
    if name in {"ShownObject", "TruncatedText"}:
        from cruxible_client.contracts import read_values

        return getattr(read_values, name)
    if name in {"NoneValue", "SlotRef"}:
        from cruxible_client.contracts import write

        return getattr(write, name)
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
    if name == "CanonicalDuration":
        from cruxible_client.contracts import captures

        return captures.CanonicalDuration
    if name in {"ClaimAdmissionPolicy", "ClaimResolutionPolicy"}:
        from cruxible_client.contracts import policies

        return getattr(policies, name)
    if name in {
        "ProcedureBudget",
        "ProcedureDefinition",
        "ProcedureHardCaps",
        "ProcedureSlot",
        "ProjectNode",
        "ReadNode",
        "TransformNode",
    }:
        from cruxible_client.contracts.procedures import models

        return getattr(models, name)
    if name in {
        "WorkspaceError",
        "observe_next_workspace",
    }:
        from cruxible_client.authoring import workspace

        return getattr(workspace, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
