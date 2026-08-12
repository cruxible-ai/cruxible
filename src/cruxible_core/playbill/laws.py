"""Digest-pinned historical acceptance-law registry for Playbill candidates."""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_core.playbill.canonical import AcceptanceLawDigest, typed_digest
from cruxible_core.playbill.errors import ProposalIntegrityError
from cruxible_core.playbill.governance import AcceptanceLawCoordinate

DOCUMENT_LAW_IDENTIFIER = "playbill.document.v1"


def _document_law_coordinate() -> AcceptanceLawCoordinate:
    """Return the reviewed semantic coordinate for the v1 Document law.

    The revision is deliberately explicit rather than derived from Python source
    bytes. Any semantic evaluator change must register a successor coordinate and
    retain this implementation for historical replay.
    """

    digest = typed_digest(
        AcceptanceLawDigest,
        "playbill-law-v1",
        {
            "identifier": DOCUMENT_LAW_IDENTIFIER,
            "artifact_tag": "playbill-document-v1",
            "semantic_revision": 1,
        },
    )
    return AcceptanceLawCoordinate(
        identifier=DOCUMENT_LAW_IDENTIFIER,
        digest=digest.tagged,
    )


DOCUMENT_LAW = _document_law_coordinate()


@dataclass(frozen=True)
class InstalledAcceptanceLaw:
    """One retained evaluator coordinate available for candidate/replay use."""

    coordinate: AcceptanceLawCoordinate
    artifact_kind: str
    artifact_tag: str


class AcceptanceLawRegistry:
    """Closed historical registry; callers cannot substitute a law by label."""

    def __init__(self, laws: tuple[InstalledAcceptanceLaw, ...]) -> None:
        self._by_coordinate: dict[tuple[str, str], InstalledAcceptanceLaw] = {}
        self._current_by_tag: dict[str, InstalledAcceptanceLaw] = {}
        for law in laws:
            key = (law.coordinate.identifier, law.coordinate.digest)
            if key in self._by_coordinate:
                raise ValueError("duplicate installed acceptance-law coordinate")
            if law.artifact_tag in self._current_by_tag:
                raise ValueError("multiple current acceptance laws for one artifact tag")
            self._by_coordinate[key] = law
            self._current_by_tag[law.artifact_tag] = law

    def resolve_member(self, *, artifact_tag: str) -> InstalledAcceptanceLaw:
        """Resolve from accepted/candidate artifact state, never caller selection."""

        try:
            return self._current_by_tag[artifact_tag]
        except KeyError as exc:
            raise ProposalIntegrityError(
                f"no acceptance law is registered for artifact tag {artifact_tag!r}"
            ) from exc

    def require_historical(
        self,
        *,
        identifier: str,
        digest: str,
    ) -> InstalledAcceptanceLaw:
        """Require an exact retained evaluator during settlement or recovery."""

        try:
            return self._by_coordinate[(identifier, digest)]
        except KeyError as exc:
            raise ProposalIntegrityError(
                f"acceptance law cannot be reproduced at its recorded digest: {identifier}@{digest}"
            ) from exc


DOCUMENT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=DOCUMENT_LAW,
    artifact_kind="document",
    artifact_tag="playbill-document-v1",
)
PLAYBILL_ACCEPTANCE_LAWS = AcceptanceLawRegistry((DOCUMENT_ACCEPTANCE_LAW,))


__all__ = [
    "AcceptanceLawRegistry",
    "DOCUMENT_ACCEPTANCE_LAW",
    "DOCUMENT_LAW",
    "DOCUMENT_LAW_IDENTIFIER",
    "InstalledAcceptanceLaw",
    "PLAYBILL_ACCEPTANCE_LAWS",
]
