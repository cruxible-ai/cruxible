"""Client-only evidence that carries prose for the existing document-write seam."""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.write import DocOp, SectionEvidence


@dataclass(frozen=True)
class AuthoredSection:
    """A section edit and its citation, made by ``cx.section(..., text=...)``.

    No file is read or written at construction. Next: pass as ``evidence=`` to
    a typed set, then inspect ``outcome.docs`` for post-admission file conflicts.
    """

    operation: DocOp
    citation: SectionEvidence
