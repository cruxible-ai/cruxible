"""Pure canonical rendering for Playbill principal records."""

from __future__ import annotations

from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.types import PrincipalRecord


def render_principal(record: PrincipalRecord) -> bytes:
    """Render one principal with the frozen canonical newline spelling."""

    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


__all__ = ["render_principal"]
