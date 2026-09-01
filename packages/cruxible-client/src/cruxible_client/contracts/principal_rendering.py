"""Pure canonical rendering for Playbill principal records."""

from __future__ import annotations

from cruxible_client.contracts.canonical import pretty_canonical_bytes
from cruxible_client.contracts.types import PrincipalRecord


def render_principal(record: PrincipalRecord) -> bytes:
    """Render one principal with the frozen canonical newline spelling."""

    return pretty_canonical_bytes(record.model_dump(mode="json"))


__all__ = ["render_principal"]
