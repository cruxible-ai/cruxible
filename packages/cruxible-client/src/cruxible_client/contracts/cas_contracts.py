"""Pure content-addressing helpers and body-store protocol contracts."""

from __future__ import annotations

import hashlib
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.canonical import CasDigest


class _StrictCasModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BodyAccessContext(_StrictCasModel):
    """Policy seam for protected body and body-derived metadata access."""

    tag: Literal["playbill-body-access-v1"] = "playbill-body-access-v1"
    principal_id: str
    can_read_body: bool = False


class CasObjectMetadata(_StrictCasModel):
    digest: str
    present: bool
    byte_length: int | None
    redacted: bool


class BodyProjectionProtocol(Protocol):
    """Compiler-only CAS seam; every byte read still requires explicit access."""

    def verify(self, digest: str) -> bool: ...

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes: ...

    def metadata(
        self,
        digest: str,
        *,
        access: BodyAccessContext,
    ) -> CasObjectMetadata: ...


def digest_bytes(content: bytes) -> CasDigest:
    """Return the frozen tagged SHA-256 address for exact body bytes."""

    return CasDigest(hashlib.sha256(content).hexdigest())


__all__ = [
    "BodyAccessContext",
    "BodyProjectionProtocol",
    "CasObjectMetadata",
    "digest_bytes",
]
