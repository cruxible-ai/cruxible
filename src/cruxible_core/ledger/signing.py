"""Client-held approval signer seam; this module never belongs on a server request."""

from __future__ import annotations

from cruxible_client.authoring.signing import (
    ApprovalSigner as ApprovalSigner,
)
from cruxible_client.authoring.signing import (
    LocalEd25519ApprovalSigner as LocalEd25519ApprovalSigner,
)

__all__ = [
    "ApprovalSigner",
    "LocalEd25519ApprovalSigner",
]
