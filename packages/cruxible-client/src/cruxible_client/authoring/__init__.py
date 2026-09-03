"""Client-owned Playbill authoring, SDK, and workspace adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.authoring.attestations import (
        ClaimAttestationV2Signer,
        LocalEd25519ClaimAttestationSigner,
    )
    from cruxible_client.authoring.sdk import Playbill, Prediction, PredictionSettlement

__all__ = [
    "ClaimAttestationV2Signer",
    "LocalEd25519ClaimAttestationSigner",
    "Playbill",
    "Prediction",
    "PredictionSettlement",
]


def __getattr__(name: str) -> Any:
    if name in {"Playbill", "Prediction", "PredictionSettlement"}:
        from cruxible_client.authoring import sdk

        return getattr(sdk, name)
    if name in {"ClaimAttestationV2Signer", "LocalEd25519ClaimAttestationSigner"}:
        from cruxible_client.authoring import attestations

        return getattr(attestations, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
