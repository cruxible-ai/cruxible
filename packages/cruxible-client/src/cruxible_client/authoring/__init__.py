"""Client-owned Cruxible authoring, SDK, and workspace adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.authoring.approval import ApprovalReviewMismatch, ReviewedProposal
    from cruxible_client.authoring.attestations import (
        ClaimAttestationSigner,
        LocalEd25519ClaimAttestationSigner,
    )
    from cruxible_client.authoring.procedures import Sequence as ProcedureSequence
    from cruxible_client.authoring.sdk import Cruxible, Prediction, PredictionSettlement
    from cruxible_client.authoring.signing import ApprovalSigner, LocalEd25519ApprovalSigner

__all__ = [
    "ApprovalReviewMismatch",
    "ReviewedProposal",
    "ApprovalSigner",
    "LocalEd25519ApprovalSigner",
    "ClaimAttestationSigner",
    "LocalEd25519ClaimAttestationSigner",
    "Cruxible",
    "ProcedureSequence",
    "Prediction",
    "PredictionSettlement",
]


def __getattr__(name: str) -> Any:
    if name == "ProcedureSequence":
        from cruxible_client.authoring.procedures import Sequence

        return Sequence
    if name in {"ApprovalReviewMismatch", "ReviewedProposal"}:
        from cruxible_client.authoring import approval

        return getattr(approval, name)
    if name in {"ApprovalSigner", "LocalEd25519ApprovalSigner"}:
        from cruxible_client.authoring import signing

        return getattr(signing, name)
    if name in {"Cruxible", "Prediction", "PredictionSettlement"}:
        from cruxible_client.authoring import sdk

        return getattr(sdk, name)
    if name in {"ClaimAttestationSigner", "LocalEd25519ClaimAttestationSigner"}:
        from cruxible_client.authoring import attestations

        return getattr(attestations, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
