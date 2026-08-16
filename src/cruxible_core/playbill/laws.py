"""Digest-pinned historical acceptance-law registry for Playbill candidates."""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_core.playbill.canonical import AcceptanceLawDigest, typed_digest
from cruxible_core.playbill.errors import ProposalIntegrityError
from cruxible_core.playbill.governance import AcceptanceLawCoordinate

DOCUMENT_LAW_IDENTIFIER = "playbill.document.v1"
CLAIM_TYPE_LAW_IDENTIFIER = "playbill.claim-type.v1"
CLAIM_LAW_IDENTIFIER = "playbill.claim.v1"
CAPTURE_CONTRACT_LAW_IDENTIFIER = "playbill.capture-contract.v1"
PRINCIPAL_LIFECYCLE_LAW_IDENTIFIER = "playbill.principal-lifecycle.v1"
SUBJECT_LAW_IDENTIFIER = "playbill.subject.v1"


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


def _principal_lifecycle_law_coordinate() -> AcceptanceLawCoordinate:
    return AcceptanceLawCoordinate(
        identifier=PRINCIPAL_LIFECYCLE_LAW_IDENTIFIER,
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": PRINCIPAL_LIFECYCLE_LAW_IDENTIFIER,
                "artifact_tag": "playbill-principal-v1",
                "semantic_revision": 1,
            },
        ).tagged,
    )


PRINCIPAL_LIFECYCLE_LAW = _principal_lifecycle_law_coordinate()


def _subject_law_coordinate() -> AcceptanceLawCoordinate:
    return AcceptanceLawCoordinate(
        identifier=SUBJECT_LAW_IDENTIFIER,
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": SUBJECT_LAW_IDENTIFIER,
                "artifact_tag": "playbill-subject-v1",
                "semantic_revision": 1,
            },
        ).tagged,
    )


SUBJECT_LAW = _subject_law_coordinate()


def _claim_type_law_coordinate() -> AcceptanceLawCoordinate:
    return AcceptanceLawCoordinate(
        identifier=CLAIM_TYPE_LAW_IDENTIFIER,
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": CLAIM_TYPE_LAW_IDENTIFIER,
                "artifact_tag": "playbill-claim-type-v1",
                "semantic_revision": 1,
            },
        ).tagged,
    )


CLAIM_TYPE_LAW = _claim_type_law_coordinate()


def _capture_contract_law_coordinate() -> AcceptanceLawCoordinate:
    return AcceptanceLawCoordinate(
        identifier=CAPTURE_CONTRACT_LAW_IDENTIFIER,
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": CAPTURE_CONTRACT_LAW_IDENTIFIER,
                "artifact_tag": "playbill-capture-contract-v1",
                "semantic_revision": 1,
            },
        ).tagged,
    )


CAPTURE_CONTRACT_LAW = _capture_contract_law_coordinate()


def _claim_law_coordinate() -> AcceptanceLawCoordinate:
    return AcceptanceLawCoordinate(
        identifier=CLAIM_LAW_IDENTIFIER,
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": CLAIM_LAW_IDENTIFIER,
                "artifact_tag": "playbill-claim-v1",
                "semantic_revision": 1,
            },
        ).tagged,
    )


CLAIM_LAW = _claim_law_coordinate()


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
PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PRINCIPAL_LIFECYCLE_LAW,
    artifact_kind="principal-lifecycle",
    artifact_tag="playbill-principal-v1",
)
SUBJECT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=SUBJECT_LAW,
    artifact_kind="subject",
    artifact_tag="playbill-subject-v1",
)
CLAIM_TYPE_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v1",
)
CAPTURE_CONTRACT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CAPTURE_CONTRACT_LAW,
    artifact_kind="capture-contract",
    artifact_tag="playbill-capture-contract-v1",
)
CLAIM_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v1",
)
PLAYBILL_ACCEPTANCE_LAWS = AcceptanceLawRegistry(
    (
        CAPTURE_CONTRACT_ACCEPTANCE_LAW,
        CLAIM_ACCEPTANCE_LAW,
        CLAIM_TYPE_ACCEPTANCE_LAW,
        DOCUMENT_ACCEPTANCE_LAW,
        PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
        SUBJECT_ACCEPTANCE_LAW,
    )
)


__all__ = [
    "AcceptanceLawRegistry",
    "CLAIM_TYPE_ACCEPTANCE_LAW",
    "CLAIM_TYPE_LAW",
    "CLAIM_TYPE_LAW_IDENTIFIER",
    "CAPTURE_CONTRACT_ACCEPTANCE_LAW",
    "CAPTURE_CONTRACT_LAW",
    "CAPTURE_CONTRACT_LAW_IDENTIFIER",
    "CLAIM_ACCEPTANCE_LAW",
    "CLAIM_LAW",
    "CLAIM_LAW_IDENTIFIER",
    "DOCUMENT_ACCEPTANCE_LAW",
    "DOCUMENT_LAW",
    "DOCUMENT_LAW_IDENTIFIER",
    "InstalledAcceptanceLaw",
    "PLAYBILL_ACCEPTANCE_LAWS",
    "PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW",
    "PRINCIPAL_LIFECYCLE_LAW",
    "PRINCIPAL_LIFECYCLE_LAW_IDENTIFIER",
    "SUBJECT_ACCEPTANCE_LAW",
    "SUBJECT_LAW",
    "SUBJECT_LAW_IDENTIFIER",
]
