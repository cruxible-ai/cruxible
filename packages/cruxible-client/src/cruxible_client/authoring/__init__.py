"""Client-owned Playbill authoring, SDK, and workspace adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.authoring.approval import ApprovalReviewMismatch, ReviewedProposal
    from cruxible_client.authoring.attestations import (
        ClaimAttestationV2Signer,
        LocalEd25519ClaimAttestationSigner,
    )
    from cruxible_client.authoring.sdk import Playbill, Prediction, PredictionSettlement
    from cruxible_client.authoring.signing import ApprovalSigner, LocalEd25519ApprovalSigner

__all__ = [
    "ApprovalReviewMismatch",
    "ReviewedProposal",
    "ApprovalSigner",
    "LocalEd25519ApprovalSigner",
    "ClaimAttestationV2Signer",
    "LocalEd25519ClaimAttestationSigner",
    "Playbill",
    "Prediction",
    "PredictionSettlement",
]


def __getattr__(name: str) -> Any:
    if name in {"ApprovalReviewMismatch", "ReviewedProposal"}:
        from cruxible_client.authoring import approval

        return getattr(approval, name)
    if name in {"ApprovalSigner", "LocalEd25519ApprovalSigner"}:
        from cruxible_client.authoring import signing

        return getattr(signing, name)
    if name in {"Playbill", "Prediction", "PredictionSettlement"}:
        from cruxible_client.authoring import sdk

        return getattr(sdk, name)
    if name in {"ClaimAttestationV2Signer", "LocalEd25519ClaimAttestationSigner"}:
        from cruxible_client.authoring import attestations

        return getattr(attestations, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
