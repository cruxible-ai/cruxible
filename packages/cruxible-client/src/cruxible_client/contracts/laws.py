"""Digest-pinned acceptance-law registry for Cruxible candidates.

One installed law per artifact tag: the revision this release judges with.
A generation that recorded any other law coordinate cannot be reproduced and
refuses (``require_historical``), as does a forged coordinate.
"""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.canonical import AcceptanceLawDigest, typed_digest
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.governance import AcceptanceLawCoordinate

DOCUMENT_LAW_IDENTIFIER = "playbill.document.v1"
APPROVAL_POLICY_LAW_IDENTIFIER = "playbill.approval-policy.v1"
PROCEDURE_RUNTIME_POLICY_LAW_IDENTIFIER = "playbill.procedure-runtime-policy.v1"
CLAIM_TYPE_LAW_IDENTIFIER = "playbill.claim-type.v1"
CLAIM_TYPE_LAW_V3_IDENTIFIER = "playbill.claim-type.v3"
CLAIM_TYPE_LAW_V4_IDENTIFIER = "playbill.claim-type.v4"
CLAIM_TYPE_LAW_V5_IDENTIFIER = "playbill.claim-type.v5"
CLAIM_TYPE_LAW_V6_IDENTIFIER = "playbill.claim-type.v6"
CLAIM_TYPE_LAW_V7_IDENTIFIER = "playbill.claim-type.v7"
CLAIM_TYPE_LAW_V8_IDENTIFIER = "playbill.claim-type.v8"
CLAIM_TYPE_LAW_V9_IDENTIFIER = "cruxible.claim-type.v9"
CLAIM_LAW_V2_IDENTIFIER = "playbill.claim.v2"
CLAIM_LAW_V3_IDENTIFIER = "playbill.claim.v3"
CAPTURE_CONTRACT_LAW_IDENTIFIER = "playbill.capture-contract.v1"
PROVIDER_LAW_IDENTIFIER = "playbill.provider.v1"
PROVIDER_LAW_V2_IDENTIFIER = "playbill.provider.v2"
PROCEDURE_MANDATE_LAW_IDENTIFIER = "playbill.procedure-mandate.v1"
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


def _claim_type_law_coordinate(semantic_revision: int) -> AcceptanceLawCoordinate:
    return AcceptanceLawCoordinate(
        identifier=CLAIM_TYPE_LAW_IDENTIFIER,
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": CLAIM_TYPE_LAW_IDENTIFIER,
                "artifact_tag": "playbill-claim-type-v1",
                "semantic_revision": semantic_revision,
            },
        ).tagged,
    )


CLAIM_TYPE_LAW_REVISION_5 = _claim_type_law_coordinate(5)
CLAIM_TYPE_LAW = CLAIM_TYPE_LAW_REVISION_5


def _capture_contract_law_coordinate(semantic_revision: int) -> AcceptanceLawCoordinate:
    return AcceptanceLawCoordinate(
        identifier=CAPTURE_CONTRACT_LAW_IDENTIFIER,
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": CAPTURE_CONTRACT_LAW_IDENTIFIER,
                "artifact_tag": "playbill-capture-contract-v1",
                "semantic_revision": semantic_revision,
            },
        ).tagged,
    )


# Revision 4: a live successor must be compatible with its predecessor (a
# breaking change is a new contract identity), revival is refused, retirement is
# its own transition, and a contract that live ClaimType evidence rules or
# ResolutionContract windows name cannot move without them.
CAPTURE_CONTRACT_LAW_REVISION_4 = _capture_contract_law_coordinate(4)


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
PROCEDURE_RUNTIME_POLICY_LAW = _artifact_law_coordinate(
    PROCEDURE_RUNTIME_POLICY_LAW_IDENTIFIER,
    "playbill-procedure-runtime-policy-v1",
    semantic_revision=1,
)
# Revision 10: a key Claim core writes with its Subject's creation is covered by
# the creation, and a revision that keeps its statement may replace a citation
# with one of the text it rests on now (same role, origin and CaptureContract).
CLAIM_LAW_V2_REVISION_10 = _artifact_law_coordinate(
    CLAIM_LAW_V2_IDENTIFIER,
    "playbill-claim-v2",
    semantic_revision=10,
)
CLAIM_LAW_V2 = CLAIM_LAW_V2_REVISION_10
# Revision 11: capture-contract pins are provenance, ClaimType v7 semantics
# apply, and an attributed retirement may name the Subject version it was
# accepted about while that Subject is retired or succeeded.
CLAIM_LAW_V3_REVISION_11 = _artifact_law_coordinate(
    CLAIM_LAW_V3_IDENTIFIER,
    "playbill-claim-v3",
    semantic_revision=11,
)
CLAIM_LAW_V3 = CLAIM_LAW_V3_REVISION_11
CLAIM_TYPE_LAW_V3_REVISION_5 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V3_IDENTIFIER,
    "playbill-claim-type-v3",
    semantic_revision=5,
)
CLAIM_TYPE_LAW_V3 = CLAIM_TYPE_LAW_V3_REVISION_5
CLAIM_TYPE_LAW_V4_REVISION_5 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V4_IDENTIFIER,
    "playbill-claim-type-v4",
    semantic_revision=5,
)
CLAIM_TYPE_LAW_V4 = CLAIM_TYPE_LAW_V4_REVISION_5
CLAIM_TYPE_LAW_V5_REVISION_5 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V5_IDENTIFIER,
    "playbill-claim-type-v5",
    semantic_revision=5,
)
CLAIM_TYPE_LAW_V6_REVISION_2 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V6_IDENTIFIER,
    "playbill-claim-type-v6",
    semantic_revision=2,
)
# ClaimType v7: descriptions, a default role, an evidence requirement and a
# revision-evidence rule.
CLAIM_TYPE_LAW_V7_REVISION_1 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V7_IDENTIFIER,
    "playbill-claim-type-v7",
    semantic_revision=1,
)
# ClaimType v8: v7 plus the canonical form its literal values are stored and
# compared in. The v7 law applies unchanged.
CLAIM_TYPE_LAW_V8_REVISION_1 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V8_IDENTIFIER,
    "playbill-claim-type-v8",
    semantic_revision=1,
)
# ClaimType v9: v8 plus an inverse (how a Claim states that a unit holds
# nothing). The v8 law applies, and an
# inverse must name a unit (never exact content), evidence rules the type
# declares and, keyed by object_key, a key of every object SubjectKind.
CLAIM_TYPE_LAW_V9_REVISION_1 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V9_IDENTIFIER,
    "cruxible-claim-type-v9",
    semantic_revision=1,
)
PROVIDER_LAW = _artifact_law_coordinate(
    PROVIDER_LAW_IDENTIFIER,
    "playbill-provider-v1",
    semantic_revision=3,
)
PROVIDER_LAW_V2 = _artifact_law_coordinate(
    PROVIDER_LAW_V2_IDENTIFIER,
    "playbill-provider-v2",
    semantic_revision=1,
)
PROCEDURE_MANDATE_LAW = _artifact_law_coordinate(
    PROCEDURE_MANDATE_LAW_IDENTIFIER,
    "playbill-procedure-mandate-v1",
    semantic_revision=1,
)
# Revision 5 refuses moving a query that live ClaimTypes corroborate
# through by exact digest unless those ClaimTypes move with it.
QUERY_DEFINITION_LAW = _artifact_law_coordinate(
    QUERY_DEFINITION_LAW_IDENTIFIER,
    "playbill-query-definition-v1",
    semantic_revision=5,
)
EXHAUST_PROMOTION_LAW = _artifact_law_coordinate(
    EXHAUST_PROMOTION_LAW_IDENTIFIER,
    "playbill-exhaust-promotion-v1",
    semantic_revision=3,
)


@dataclass(frozen=True)
class InstalledAcceptanceLaw:
    """The one installed evaluator coordinate for an artifact tag."""

    coordinate: AcceptanceLawCoordinate
    artifact_kind: str
    artifact_tag: str


class AcceptanceLawRegistry:
    """Closed registry; callers cannot substitute a law by label."""

    def __init__(self, laws: tuple[InstalledAcceptanceLaw, ...]) -> None:
        self._by_coordinate: dict[tuple[str, str], InstalledAcceptanceLaw] = {}
        self._current_by_tag: dict[str, InstalledAcceptanceLaw] = {}
        for law in laws:
            key = (law.coordinate.identifier, law.coordinate.digest)
            if key in self._by_coordinate:
                raise ValueError("duplicate installed acceptance-law coordinate")
            if law.artifact_tag in self._current_by_tag:
                raise ValueError("multiple acceptance laws for one artifact tag")
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
        """Require the exact recorded evaluator during settlement or recovery."""

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
PROCEDURE_RUNTIME_POLICY_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROCEDURE_RUNTIME_POLICY_LAW,
    artifact_kind="procedure-runtime-policy",
    artifact_tag="playbill-procedure-runtime-policy-v1",
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
    coordinate=CLAIM_TYPE_LAW_REVISION_5,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v1",
)
CLAIM_TYPE_V3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V3_REVISION_5,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v3",
)
CLAIM_TYPE_V4_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V4_REVISION_5,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v4",
)
CLAIM_TYPE_V5_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V5_REVISION_5,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v5",
)
CLAIM_TYPE_V6_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V6_REVISION_2,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v6",
)
CLAIM_TYPE_V7_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V7_REVISION_1,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v7",
)
CLAIM_TYPE_V8_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V8_REVISION_1,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v8",
)
CLAIM_TYPE_V9_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V9_REVISION_1,
    artifact_kind="claim-type",
    artifact_tag="cruxible-claim-type-v9",
)
CAPTURE_CONTRACT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CAPTURE_CONTRACT_LAW_REVISION_4,
    artifact_kind="capture-contract",
    artifact_tag="playbill-capture-contract-v1",
)
LIGHT_CAPTURE_CONTRACT_LAW = InstalledAcceptanceLaw(
    coordinate=AcceptanceLawCoordinate(
        identifier="cruxible.capture-contract.v2",
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": "cruxible.capture-contract.v2",
                "artifact_tag": "cruxible-capture-contract-v2",
                "semantic_revision": 1,
            },
        ).tagged,
    ),
    artifact_kind="capture-contract",
    artifact_tag="cruxible-capture-contract-v2",
)
CLAIM_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V2_REVISION_10,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v2",
)
CLAIM_V3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V3_REVISION_11,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v3",
)
PROVIDER_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROVIDER_LAW,
    artifact_kind="provider",
    artifact_tag="playbill-provider-v1",
)
PROVIDER_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROVIDER_LAW_V2,
    artifact_kind="provider",
    artifact_tag="playbill-provider-v2",
)
PROCEDURE_MANDATE_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.procedure-mandate.v2", "playbill-procedure-mandate-v2", semantic_revision=1
    ),
    artifact_kind="procedure-mandate",
    artifact_tag="playbill-procedure-mandate-v2",
)
#: A Mandate pins one exact Line (ruling C3).
PROCEDURE_MANDATE_V3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "cruxible.procedure-mandate.v3", "cruxible-procedure-mandate-v3", semantic_revision=1
    ),
    artifact_kind="procedure-mandate",
    artifact_tag="cruxible-procedure-mandate-v3",
)
PROCEDURE_MANDATE_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROCEDURE_MANDATE_LAW,
    artifact_kind="procedure-mandate",
    artifact_tag="playbill-procedure-mandate-v1",
)
QUERY_DEFINITION_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=QUERY_DEFINITION_LAW,
    artifact_kind="query-definition",
    artifact_tag="playbill-query-definition-v1",
)
QUERY_DEFINITION_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.query-definition.v2", "playbill-query-definition-v2", semantic_revision=1
    ),
    artifact_kind="query-definition",
    artifact_tag="playbill-query-definition-v2",
)
EXHAUST_PROMOTION_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=EXHAUST_PROMOTION_LAW,
    artifact_kind="exhaust-promotion",
    artifact_tag="playbill-exhaust-promotion-v1",
)
ATTESTATION_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "cruxible.accepted-claim-attestation.v1",
        "playbill-claim-attestation-envelope-v2",
        semantic_revision=1,
    ),
    artifact_kind="attestation",
    artifact_tag="playbill-claim-attestation-envelope-v2",
)


# A Line with no embedded trigger. Retiring it, or changing the event it
# accepts, cannot strand live Triggers aimed at it: they move in the same set.
LINE_V6_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.line.v6", "playbill-line-v6", semantic_revision=1
    ),
    artifact_kind="line",
    artifact_tag="playbill-line-v6",
)
# A SubjectKind declares how Subjects of one kind are identified: by key
# ClaimTypes (live, v8, literal, one-valued, about that kind only) or by minted
# ids. A retired SubjectKind is never revived.
SUBJECT_KIND_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "cruxible.subject-kind.v1", "cruxible-subject-kind-v1", semantic_revision=1
    ),
    artifact_kind="subject-kind",
    artifact_tag="cruxible-subject-kind-v1",
)
# A Contract is one immutable data shape: a version never changes, a successor
# names its predecessor's digest, and every nested Contract pin resolves exactly.
CONTRACT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "cruxible.contract.v1", "cruxible-contract-v1", semantic_revision=1
    ),
    artifact_kind="contract",
    artifact_tag="cruxible-contract-v1",
)
# A Trigger aims at a live Line that accepts its event, or at an internal
# action on a cadence; a retired Trigger is never revived.
TRIGGER_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.trigger.v1", "playbill-trigger-v1", semantic_revision=1
    ),
    artifact_kind="trigger",
    artifact_tag="playbill-trigger-v1",
)

RESOLUTION_CONTRACT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "cruxible.resolution-contract.v1", "playbill-resolution-contract-v1", semantic_revision=1
    ),
    artifact_kind="resolution-contract",
    artifact_tag="playbill-resolution-contract-v1",
)


PROVIDER_V3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.provider.v3", "playbill-provider-v3", semantic_revision=1
    ),
    artifact_kind="provider",
    artifact_tag="playbill-provider-v3",
)
# One provider build per artifact: a build is added beside
# the other builds of its name and never replaced; a contract registration is
# added beside the other contracts of its interface id and never replaced.
PROVIDER_BUILD_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "cruxible.provider.v4", "cruxible-provider-v4", semantic_revision=1
    ),
    artifact_kind="provider",
    artifact_tag="cruxible-provider-v4",
)


NAMED_REFERENCE_FORMATS = {
    "capture-contract": "cruxible-capture-contract-v3",
    "procedure": "cruxible-procedure-v3",
    "query-definition": "cruxible-query-definition-v3",
    "trigger": "cruxible-trigger-v2",
    "line": "cruxible-line-v8",
    "provider": "cruxible-provider-v6",
}
NAMED_REFERENCE_LAWS = tuple(
    InstalledAcceptanceLaw(
        coordinate=_artifact_law_coordinate(f"cruxible.{kind}.named.v1", tag, semantic_revision=1),
        artifact_kind=kind,
        artifact_tag=tag,
    )
    for kind, tag in NAMED_REFERENCE_FORMATS.items()
)

# Public names refer to the one current named-law entry for each v3 tag.
LIGHT_PROCEDURE_LAW = next(law for law in NAMED_REFERENCE_LAWS if law.artifact_kind == "procedure")
QUERY_DEFINITION_V3_ACCEPTANCE_LAW = next(
    law for law in NAMED_REFERENCE_LAWS if law.artifact_kind == "query-definition"
)

NAMED_PROVIDER_IDENTITY_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "cruxible.provider-identity.v3", "cruxible-provider-identity-v3", semantic_revision=1
    ),
    artifact_kind="provider",
    artifact_tag="cruxible-provider-identity-v3",
)

ACCEPTANCE_LAWS = AcceptanceLawRegistry(
    (
        *NAMED_REFERENCE_LAWS,
        NAMED_PROVIDER_IDENTITY_LAW,
        LIGHT_CAPTURE_CONTRACT_LAW,
        PROVIDER_V3_ACCEPTANCE_LAW,
        PROVIDER_BUILD_ACCEPTANCE_LAW,
        APPROVAL_POLICY_ACCEPTANCE_LAW,
        PROCEDURE_RUNTIME_POLICY_ACCEPTANCE_LAW,
        CAPTURE_CONTRACT_ACCEPTANCE_LAW,
        ATTESTATION_ACCEPTANCE_LAW,
        RESOLUTION_CONTRACT_ACCEPTANCE_LAW,
        CLAIM_V2_ACCEPTANCE_LAW,
        CLAIM_V3_ACCEPTANCE_LAW,
        CLAIM_TYPE_ACCEPTANCE_LAW,
        CLAIM_TYPE_V3_ACCEPTANCE_LAW,
        CLAIM_TYPE_V4_ACCEPTANCE_LAW,
        CLAIM_TYPE_V5_ACCEPTANCE_LAW,
        CLAIM_TYPE_V6_ACCEPTANCE_LAW,
        CLAIM_TYPE_V7_ACCEPTANCE_LAW,
        CLAIM_TYPE_V8_ACCEPTANCE_LAW,
        CLAIM_TYPE_V9_ACCEPTANCE_LAW,
        DOCUMENT_ACCEPTANCE_LAW,
        EXHAUST_PROMOTION_ACCEPTANCE_LAW,
        PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
        LINE_V6_ACCEPTANCE_LAW,
        TRIGGER_ACCEPTANCE_LAW,
        SUBJECT_KIND_ACCEPTANCE_LAW,
        CONTRACT_ACCEPTANCE_LAW,
        PROVIDER_ACCEPTANCE_LAW,
        PROVIDER_V2_ACCEPTANCE_LAW,
        QUERY_DEFINITION_ACCEPTANCE_LAW,
        QUERY_DEFINITION_V2_ACCEPTANCE_LAW,
        PROCEDURE_MANDATE_ACCEPTANCE_LAW,
        PROCEDURE_MANDATE_V2_ACCEPTANCE_LAW,
        PROCEDURE_MANDATE_V3_ACCEPTANCE_LAW,
        SUBJECT_ACCEPTANCE_LAW,
    )
)


__all__ = [
    "AcceptanceLawRegistry",
    "InstalledAcceptanceLaw",
]
