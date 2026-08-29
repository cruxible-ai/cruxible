"""Client-owned Playbill authoring, SDK, and workspace adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.authoring.attestations import (
        ClaimAttestationV2Signer,
        LocalEd25519ClaimAttestationSigner,
    )
    from cruxible_client.authoring.sdk import Playbill

__all__ = [
    "ClaimAttestationV2Signer",
    "LocalEd25519ClaimAttestationSigner",
    "Playbill",
]


def __getattr__(name: str) -> Any:
    if name == "Playbill":
        from cruxible_client.authoring.sdk import Playbill

        return Playbill
    if name in {"ClaimAttestationV2Signer", "LocalEd25519ClaimAttestationSigner"}:
        from cruxible_client.authoring import attestations

        return getattr(attestations, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
