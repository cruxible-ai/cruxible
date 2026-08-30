"""Digest-pinned historical acceptance-law registry for Playbill candidates."""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.canonical import AcceptanceLawDigest, typed_digest
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.governance import AcceptanceLawCoordinate

DOCUMENT_LAW_IDENTIFIER = "playbill.document.v1"
APPROVAL_POLICY_LAW_IDENTIFIER = "playbill.approval-policy.v1"
CLAIM_TYPE_LAW_IDENTIFIER = "playbill.claim-type.v1"
CLAIM_TYPE_LAW_V3_IDENTIFIER = "playbill.claim-type.v3"
CLAIM_TYPE_LAW_V4_IDENTIFIER = "playbill.claim-type.v4"
CLAIM_LAW_V2_IDENTIFIER = "playbill.claim.v2"
CLAIM_LAW_V3_IDENTIFIER = "playbill.claim.v3"
CAPTURE_CONTRACT_LAW_IDENTIFIER = "playbill.capture-contract.v1"
PROVIDER_LAW_IDENTIFIER = "playbill.provider.v1"
SOURCE_ACQUISITION_POLICY_LAW_IDENTIFIER = "playbill.source-acquisition-policy.v1"
STANDING_MANDATE_LAW_IDENTIFIER = "playbill.standing-mandate.v1"
PROCEDURE_LAW_IDENTIFIER = "playbill.procedure.v1"
PROCEDURE_LAW_V2_IDENTIFIER = "playbill.procedure.v2"
LINE_LAW_IDENTIFIER = "playbill.line.v1"
QUERY_DEFINITION_LAW_IDENTIFIER = "playbill.query-definition.v1"
EXHAUST_PROMOTION_LAW_IDENTIFIER = "playbill.exhaust-promotion.v1"
PRINCIPAL_LIFECYCLE_LAW_IDENTIFIER = "playbill.principal-lifecycle.v1"
SUBJECT_LAW_IDENTIFIER = "playbill.subject.v1"


def _document_law_coordinate() -> AcceptanceLawCoordinate:
    """Return the reviewed semantic coordinate for the v1 Document law.

    The revision is deliberately explicit rather than derived from Python source
    bytes. C1's unreleased lineage uses the authorized in-place revision-3 re-pin;
    after the first public release, semantic changes must register a successor
    coordinate and retain the deployed implementation for historical replay.
    """

    digest = typed_digest(
        AcceptanceLawDigest,
        "playbill-law-v1",
        {
            "identifier": DOCUMENT_LAW_IDENTIFIER,
            "artifact_tag": "playbill-document-v1",
            "semantic_revision": 3,
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
                "semantic_revision": 6,
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
                "semantic_revision": 3,
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
                "semantic_revision": 4,
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
                "semantic_revision": 3,
            },
        ).tagged,
    )


CAPTURE_CONTRACT_LAW = _capture_contract_law_coordinate()


def _artifact_law_coordinate(
    identifier: str,
    artifact_tag: str,
    *,
    semantic_revision: int = 2,
) -> AcceptanceLawCoordinate:
    """Name one artifact law at the revision of its meaning.

    The revision is part of the digest, so a law that starts refusing something
    it used to accept must move it: accepted artifacts pin the digest their
    acceptance was judged under, and leaving it still would let one digest stand
    for two different laws.
    """

    return AcceptanceLawCoordinate(
        identifier=identifier,
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": identifier,
                "artifact_tag": artifact_tag,
                "semantic_revision": semantic_revision,
            },
        ).tagged,
    )


APPROVAL_POLICY_LAW = _artifact_law_coordinate(
    APPROVAL_POLICY_LAW_IDENTIFIER,
    "playbill-approval-policy-v1",
    semantic_revision=1,
)
CLAIM_LAW_V2 = _artifact_law_coordinate(
    CLAIM_LAW_V2_IDENTIFIER,
    "playbill-claim-v2",
    semantic_revision=6,
)
CLAIM_LAW_V3 = _artifact_law_coordinate(
    CLAIM_LAW_V3_IDENTIFIER,
    "playbill-claim-v3",
    semantic_revision=7,
)
CLAIM_TYPE_LAW_V3 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V3_IDENTIFIER,
    "playbill-claim-type-v3",
    semantic_revision=4,
)
CLAIM_TYPE_LAW_V4 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V4_IDENTIFIER,
    "playbill-claim-type-v4",
    semantic_revision=4,
)
PROVIDER_LAW = _artifact_law_coordinate(
    PROVIDER_LAW_IDENTIFIER,
    "playbill-provider-v1",
    semantic_revision=3,
)
SOURCE_ACQUISITION_POLICY_LAW = _artifact_law_coordinate(
    SOURCE_ACQUISITION_POLICY_LAW_IDENTIFIER,
    "playbill-source-acquisition-policy-v1",
    semantic_revision=3,
)
STANDING_MANDATE_LAW = _artifact_law_coordinate(
    STANDING_MANDATE_LAW_IDENTIFIER,
    "playbill-standing-mandate-v1",
    semantic_revision=3,
)
PROCEDURE_LAW = _artifact_law_coordinate(
    PROCEDURE_LAW_IDENTIFIER,
    "playbill-procedure-v1",
    semantic_revision=3,
)
PROCEDURE_LAW_V2 = _artifact_law_coordinate(
    PROCEDURE_LAW_V2_IDENTIFIER,
    "playbill-procedure-v2",
    semantic_revision=3,
)
LINE_LAW = _artifact_law_coordinate(
    LINE_LAW_IDENTIFIER,
    "playbill-line-v1",
    semantic_revision=3,
)
# Revision 4 retains the relation-traversal refusal and removes dormant role authority.
QUERY_DEFINITION_LAW = _artifact_law_coordinate(
    QUERY_DEFINITION_LAW_IDENTIFIER,
    "playbill-query-definition-v1",
    semantic_revision=4,
)
EXHAUST_PROMOTION_LAW = _artifact_law_coordinate(
    EXHAUST_PROMOTION_LAW_IDENTIFIER,
    "playbill-exhaust-promotion-v1",
    semantic_revision=3,
)


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
APPROVAL_POLICY_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=APPROVAL_POLICY_LAW,
    artifact_kind="approval-policy",
    artifact_tag="playbill-approval-policy-v1",
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
CLAIM_TYPE_V3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V3,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v3",
)
CLAIM_TYPE_V4_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V4,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v4",
)
CAPTURE_CONTRACT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CAPTURE_CONTRACT_LAW,
    artifact_kind="capture-contract",
    artifact_tag="playbill-capture-contract-v1",
)
CLAIM_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V2,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v2",
)
CLAIM_V3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V3,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v3",
)
PROVIDER_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROVIDER_LAW,
    artifact_kind="provider",
    artifact_tag="playbill-provider-v1",
)
SOURCE_ACQUISITION_POLICY_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=SOURCE_ACQUISITION_POLICY_LAW,
    artifact_kind="source-acquisition-policy",
    artifact_tag="playbill-source-acquisition-policy-v1",
)
STANDING_MANDATE_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=STANDING_MANDATE_LAW,
    artifact_kind="standing-mandate",
    artifact_tag="playbill-standing-mandate-v1",
)
PROCEDURE_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROCEDURE_LAW,
    artifact_kind="procedure",
    artifact_tag="playbill-procedure-v1",
)
PROCEDURE_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROCEDURE_LAW_V2,
    artifact_kind="procedure",
    artifact_tag="playbill-procedure-v2",
)
LINE_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=LINE_LAW,
    artifact_kind="line",
    artifact_tag="playbill-line-v1",
)
QUERY_DEFINITION_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=QUERY_DEFINITION_LAW,
    artifact_kind="query-definition",
    artifact_tag="playbill-query-definition-v1",
)
EXHAUST_PROMOTION_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=EXHAUST_PROMOTION_LAW,
    artifact_kind="exhaust-promotion",
    artifact_tag="playbill-exhaust-promotion-v1",
)
PLAYBILL_ACCEPTANCE_LAWS = AcceptanceLawRegistry(
    (
        APPROVAL_POLICY_ACCEPTANCE_LAW,
        CAPTURE_CONTRACT_ACCEPTANCE_LAW,
        CLAIM_V2_ACCEPTANCE_LAW,
        CLAIM_V3_ACCEPTANCE_LAW,
        CLAIM_TYPE_ACCEPTANCE_LAW,
        CLAIM_TYPE_V3_ACCEPTANCE_LAW,
        CLAIM_TYPE_V4_ACCEPTANCE_LAW,
        DOCUMENT_ACCEPTANCE_LAW,
        EXHAUST_PROMOTION_ACCEPTANCE_LAW,
        PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
        PROCEDURE_ACCEPTANCE_LAW,
        PROCEDURE_V2_ACCEPTANCE_LAW,
        LINE_ACCEPTANCE_LAW,
        PROVIDER_ACCEPTANCE_LAW,
        QUERY_DEFINITION_ACCEPTANCE_LAW,
        SOURCE_ACQUISITION_POLICY_ACCEPTANCE_LAW,
        STANDING_MANDATE_ACCEPTANCE_LAW,
        SUBJECT_ACCEPTANCE_LAW,
    )
)


__all__ = [
    "AcceptanceLawRegistry",
    "APPROVAL_POLICY_ACCEPTANCE_LAW",
    "APPROVAL_POLICY_LAW",
    "APPROVAL_POLICY_LAW_IDENTIFIER",
    "CLAIM_TYPE_ACCEPTANCE_LAW",
    "CLAIM_TYPE_V3_ACCEPTANCE_LAW",
    "CLAIM_TYPE_V4_ACCEPTANCE_LAW",
    "CLAIM_TYPE_LAW",
    "CLAIM_TYPE_LAW_IDENTIFIER",
    "CLAIM_TYPE_LAW_V3",
    "CLAIM_TYPE_LAW_V3_IDENTIFIER",
    "CLAIM_TYPE_LAW_V4",
    "CLAIM_TYPE_LAW_V4_IDENTIFIER",
    "CAPTURE_CONTRACT_ACCEPTANCE_LAW",
    "CAPTURE_CONTRACT_LAW",
    "CAPTURE_CONTRACT_LAW_IDENTIFIER",
    "CLAIM_LAW_V2",
    "CLAIM_LAW_V2_IDENTIFIER",
    "CLAIM_LAW_V3",
    "CLAIM_LAW_V3_IDENTIFIER",
    "CLAIM_V2_ACCEPTANCE_LAW",
    "CLAIM_V3_ACCEPTANCE_LAW",
    "DOCUMENT_ACCEPTANCE_LAW",
    "DOCUMENT_LAW",
    "DOCUMENT_LAW_IDENTIFIER",
    "EXHAUST_PROMOTION_ACCEPTANCE_LAW",
    "EXHAUST_PROMOTION_LAW",
    "EXHAUST_PROMOTION_LAW_IDENTIFIER",
    "InstalledAcceptanceLaw",
    "LINE_ACCEPTANCE_LAW",
    "LINE_LAW",
    "LINE_LAW_IDENTIFIER",
    "PLAYBILL_ACCEPTANCE_LAWS",
    "PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW",
    "PRINCIPAL_LIFECYCLE_LAW",
    "PRINCIPAL_LIFECYCLE_LAW_IDENTIFIER",
    "PROVIDER_ACCEPTANCE_LAW",
    "PROVIDER_LAW",
    "PROVIDER_LAW_IDENTIFIER",
    "PROCEDURE_ACCEPTANCE_LAW",
    "PROCEDURE_LAW_V2",
    "PROCEDURE_LAW_V2_IDENTIFIER",
    "PROCEDURE_V2_ACCEPTANCE_LAW",
    "PROCEDURE_LAW",
    "PROCEDURE_LAW_IDENTIFIER",
    "QUERY_DEFINITION_ACCEPTANCE_LAW",
    "QUERY_DEFINITION_LAW",
    "QUERY_DEFINITION_LAW_IDENTIFIER",
    "SOURCE_ACQUISITION_POLICY_ACCEPTANCE_LAW",
    "SOURCE_ACQUISITION_POLICY_LAW",
    "SOURCE_ACQUISITION_POLICY_LAW_IDENTIFIER",
    "STANDING_MANDATE_ACCEPTANCE_LAW",
    "STANDING_MANDATE_LAW",
    "STANDING_MANDATE_LAW_IDENTIFIER",
    "SUBJECT_ACCEPTANCE_LAW",
    "SUBJECT_LAW",
    "SUBJECT_LAW_IDENTIFIER",
]
