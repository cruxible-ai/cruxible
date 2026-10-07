"""Digest-pinned historical acceptance-law registry for Cruxible candidates."""

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
CLAIM_LAW_V2_IDENTIFIER = "playbill.claim.v2"
CLAIM_LAW_V3_IDENTIFIER = "playbill.claim.v3"
CAPTURE_CONTRACT_LAW_IDENTIFIER = "playbill.capture-contract.v1"
PROVIDER_LAW_IDENTIFIER = "playbill.provider.v1"
PROVIDER_LAW_V2_IDENTIFIER = "playbill.provider.v2"
PROVIDER_INTERFACE_LAW_IDENTIFIER = "playbill.provider-interface.v1"
SOURCE_ACQUISITION_POLICY_LAW_IDENTIFIER = "playbill.source-acquisition-policy.v1"
PROCEDURE_MANDATE_LAW_IDENTIFIER = "playbill.procedure-mandate.v1"
PROCEDURE_LAW_V2_IDENTIFIER = "playbill.procedure.v2"
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


# Every ClaimType law revision before the one named current below ran the
# vocabulary reuse law: a new ClaimType whose canonical tokens or structural
# signature matched an accepted one was refused unless a same-change-set
# ``semantic.distinct_from`` Claim declared them distinct, and each member result
# recorded the reuse evidence. Those revisions stay installed only so accepted
# history and pending proposals replay and settle under them. The current
# revisions drop the reuse law and record no reuse evidence.
CLAIM_TYPE_LAW_REVISION_4 = _claim_type_law_coordinate(4)
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


CAPTURE_CONTRACT_LAW_REVISION_3 = _capture_contract_law_coordinate(3)
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
CLAIM_LAW_V2_REVISION_6 = _artifact_law_coordinate(
    CLAIM_LAW_V2_IDENTIFIER,
    "playbill-claim-v2",
    semantic_revision=6,
)
# Revision 7: capture-contract pins are provenance, as in Claim law v3 revision 9.
CLAIM_LAW_V2_REVISION_7 = _artifact_law_coordinate(
    CLAIM_LAW_V2_IDENTIFIER,
    "playbill-claim-v2",
    semantic_revision=7,
)
# Revision 8: a v7 ClaimType's revision-evidence rule and evidence requirement
# apply. A revision that changes its statement under `replace` carries exactly
# the evidence it cites; `captured` refuses a new, non-carry Claim with no
# Capture under a declared contract; `none` records the origin as support. A
# carry (byte-identical backing) keeps every check it had. For every ClaimType
# before v7 it judges exactly as revision 7 did.
CLAIM_LAW_V2_REVISION_8 = _artifact_law_coordinate(
    CLAIM_LAW_V2_IDENTIFIER,
    "playbill-claim-v2",
    semantic_revision=8,
)
CLAIM_LAW_V2 = CLAIM_LAW_V2_REVISION_8
CLAIM_LAW_V3_REVISION_7 = _artifact_law_coordinate(
    CLAIM_LAW_V3_IDENTIFIER,
    "playbill-claim-v3",
    semantic_revision=7,
)
CLAIM_LAW_V3_REVISION_8 = _artifact_law_coordinate(
    CLAIM_LAW_V3_IDENTIFIER,
    "playbill-claim-v3",
    semantic_revision=8,
)
# Revision 9: a Claim's capture-contract pins are provenance. They resolve to
# any accepted historical version, several versions of one contract may back one
# Claim, and a contract successor never strands the Claims that cite it.
CLAIM_LAW_V3_REVISION_9 = _artifact_law_coordinate(
    CLAIM_LAW_V3_IDENTIFIER,
    "playbill-claim-v3",
    semantic_revision=9,
)
# Revision 10: the ClaimType v7 semantics of Claim law v2 revision 8.
CLAIM_LAW_V3_REVISION_10 = _artifact_law_coordinate(
    CLAIM_LAW_V3_IDENTIFIER,
    "playbill-claim-v3",
    semantic_revision=10,
)
# Current is an operational alias only. Historical replay and shape-law
# selection must name the exact revision directly.
CLAIM_LAW_V3 = CLAIM_LAW_V3_REVISION_10
# Current revisions drop the vocabulary reuse law (see CLAIM_TYPE_LAW above).
CLAIM_TYPE_LAW_V3_REVISION_4 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V3_IDENTIFIER,
    "playbill-claim-type-v3",
    semantic_revision=4,
)
CLAIM_TYPE_LAW_V3_REVISION_5 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V3_IDENTIFIER,
    "playbill-claim-type-v3",
    semantic_revision=5,
)
CLAIM_TYPE_LAW_V3 = CLAIM_TYPE_LAW_V3_REVISION_5
CLAIM_TYPE_LAW_V4_REVISION_4 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V4_IDENTIFIER,
    "playbill-claim-type-v4",
    semantic_revision=4,
)
CLAIM_TYPE_LAW_V4_REVISION_5 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V4_IDENTIFIER,
    "playbill-claim-type-v4",
    semantic_revision=5,
)
CLAIM_TYPE_LAW_V4 = CLAIM_TYPE_LAW_V4_REVISION_5
CLAIM_TYPE_LAW_V5_REVISION_4 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V5_IDENTIFIER,
    "playbill-claim-type-v5",
    semantic_revision=4,
)
CLAIM_TYPE_LAW_V5_REVISION_5 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V5_IDENTIFIER,
    "playbill-claim-type-v5",
    semantic_revision=5,
)
CLAIM_TYPE_LAW_V6_REVISION_1 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V6_IDENTIFIER,
    "playbill-claim-type-v6",
    semantic_revision=1,
)
CLAIM_TYPE_LAW_V6_REVISION_2 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V6_IDENTIFIER,
    "playbill-claim-type-v6",
    semantic_revision=2,
)
# ClaimType v7: descriptions, a default role, an evidence requirement and a
# revision-evidence rule, under compiler revision 31.
CLAIM_TYPE_LAW_V7_REVISION_1 = _artifact_law_coordinate(
    CLAIM_TYPE_LAW_V7_IDENTIFIER,
    "playbill-claim-type-v7",
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
PROVIDER_INTERFACE_LAW = _artifact_law_coordinate(
    PROVIDER_INTERFACE_LAW_IDENTIFIER,
    "playbill-provider-interface-v1",
    semantic_revision=1,
)
SOURCE_ACQUISITION_POLICY_LAW = _artifact_law_coordinate(
    SOURCE_ACQUISITION_POLICY_LAW_IDENTIFIER,
    "playbill-source-acquisition-policy-v1",
    semantic_revision=3,
)
PROCEDURE_MANDATE_LAW = _artifact_law_coordinate(
    PROCEDURE_MANDATE_LAW_IDENTIFIER,
    "playbill-procedure-mandate-v1",
    semantic_revision=1,
)
PROCEDURE_LAW_V2 = _artifact_law_coordinate(
    PROCEDURE_LAW_V2_IDENTIFIER,
    "playbill-procedure-v2",
    semantic_revision=6,
)
# Revision 4 retains the relation-traversal refusal and removes dormant role authority.
QUERY_DEFINITION_LAW_REVISION_4 = _artifact_law_coordinate(
    QUERY_DEFINITION_LAW_IDENTIFIER,
    "playbill-query-definition-v1",
    semantic_revision=4,
)
# Revision 5 also refuses moving a query that live ClaimTypes corroborate
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
    """One retained evaluator coordinate available for candidate/replay use."""

    coordinate: AcceptanceLawCoordinate
    artifact_kind: str
    artifact_tag: str
    current: bool = True


class AcceptanceLawRegistry:
    """Closed historical registry; callers cannot substitute a law by label."""

    def __init__(self, laws: tuple[InstalledAcceptanceLaw, ...]) -> None:
        self._by_coordinate: dict[tuple[str, str], InstalledAcceptanceLaw] = {}
        self._current_by_tag: dict[str, InstalledAcceptanceLaw] = {}
        for law in laws:
            key = (law.coordinate.identifier, law.coordinate.digest)
            if key in self._by_coordinate:
                raise ValueError("duplicate installed acceptance-law coordinate")
            self._by_coordinate[key] = law
            if law.current:
                if law.artifact_tag in self._current_by_tag:
                    raise ValueError("multiple current acceptance laws for one artifact tag")
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
CLAIM_TYPE_REVISION_4_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_REVISION_4,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v1",
    current=False,
)
CLAIM_TYPE_V3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V3_REVISION_5,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v3",
)
CLAIM_TYPE_V3_REVISION_4_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V3_REVISION_4,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v3",
    current=False,
)
CLAIM_TYPE_V4_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V4_REVISION_5,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v4",
)
CLAIM_TYPE_V4_REVISION_4_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V4_REVISION_4,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v4",
    current=False,
)
CLAIM_TYPE_V5_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V5_REVISION_5,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v5",
)
CLAIM_TYPE_V5_REVISION_4_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V5_REVISION_4,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v5",
    current=False,
)
CLAIM_TYPE_V6_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V6_REVISION_2,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v6",
)
CLAIM_TYPE_V6_REVISION_1_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V6_REVISION_1,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v6",
    current=False,
)
CLAIM_TYPE_V7_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_TYPE_LAW_V7_REVISION_1,
    artifact_kind="claim-type",
    artifact_tag="playbill-claim-type-v7",
)
CAPTURE_CONTRACT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CAPTURE_CONTRACT_LAW_REVISION_4,
    artifact_kind="capture-contract",
    artifact_tag="playbill-capture-contract-v1",
)
CAPTURE_CONTRACT_REVISION_3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CAPTURE_CONTRACT_LAW_REVISION_3,
    artifact_kind="capture-contract",
    artifact_tag="playbill-capture-contract-v1",
    current=False,
)
CLAIM_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V2_REVISION_8,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v2",
)
CLAIM_V2_REVISION_7_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V2_REVISION_7,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v2",
    current=False,
)
CLAIM_V2_REVISION_6_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V2_REVISION_6,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v2",
    current=False,
)
CLAIM_V3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V3_REVISION_10,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v3",
)
CLAIM_V3_REVISION_9_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V3_REVISION_9,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v3",
    current=False,
)
CLAIM_V3_REVISION_8_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V3_REVISION_8,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v3",
    current=False,
)
CLAIM_V3_REVISION_7_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=CLAIM_LAW_V3_REVISION_7,
    artifact_kind="claim",
    artifact_tag="playbill-claim-v3",
    current=False,
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
PROVIDER_INTERFACE_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROVIDER_INTERFACE_LAW,
    artifact_kind="provider-interface",
    artifact_tag="playbill-provider-interface-v1",
)
SOURCE_ACQUISITION_POLICY_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=SOURCE_ACQUISITION_POLICY_LAW,
    artifact_kind="source-acquisition-policy",
    artifact_tag="playbill-source-acquisition-policy-v1",
)
PROCEDURE_MANDATE_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.procedure-mandate.v2", "playbill-procedure-mandate-v2", semantic_revision=1
    ),
    artifact_kind="procedure-mandate",
    artifact_tag="playbill-procedure-mandate-v2",
)
PROCEDURE_MANDATE_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROCEDURE_MANDATE_LAW,
    artifact_kind="procedure-mandate",
    artifact_tag="playbill-procedure-mandate-v1",
)
PROCEDURE_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=PROCEDURE_LAW_V2,
    artifact_kind="procedure",
    artifact_tag="playbill-procedure-v2",
)
QUERY_DEFINITION_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=QUERY_DEFINITION_LAW,
    artifact_kind="query-definition",
    artifact_tag="playbill-query-definition-v1",
)
QUERY_DEFINITION_REVISION_4_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=QUERY_DEFINITION_LAW_REVISION_4,
    artifact_kind="query-definition",
    artifact_tag="playbill-query-definition-v1",
    current=False,
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
# A Blueprint keeps stable identity and types every open slot by an accepted
# ProviderInterface; it never runs, so it needs no runtime closure.
BLUEPRINT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "cruxible.blueprint.v1", "cruxible-blueprint-v1", semantic_revision=1
    ),
    artifact_kind="blueprint",
    artifact_tag="cruxible-blueprint-v1",
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
# Revision 10 (to compiler revision 32) also refuses an instance that still
# holds a Line which embeds its trigger: revision 32 admits none.
GOVERNED_TRIGGERS_UPGRADE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.compiler-upgrade.v1", "playbill-compiler-upgrade-v1", semantic_revision=10
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
    current=False,
)
AUTHORITY_VERBS_UPGRADE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.compiler-upgrade.v1", "playbill-compiler-upgrade-v1", semantic_revision=9
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
    current=False,
)
TRIGGER_CAPTURE_UPGRADE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.compiler-upgrade.v1", "playbill-compiler-upgrade-v1", semantic_revision=8
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
    current=False,
)

RESOLUTION_CONTRACT_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "cruxible.resolution-contract.v1", "playbill-resolution-contract-v1", semantic_revision=1
    ),
    artifact_kind="resolution-contract",
    artifact_tag="playbill-resolution-contract-v1",
)

COMPILER_UPGRADE_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=AcceptanceLawCoordinate(
        identifier="playbill.compiler-upgrade.v1",
        digest=typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": "playbill.compiler-upgrade.v1",
                "artifact_tag": "playbill-compiler-upgrade-v1",
                "semantic_revision": 1,
            },
        ).tagged,
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
)


SDK_SOURCE_PROCEDURE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        PROCEDURE_LAW_V2_IDENTIFIER, "playbill-procedure-v2", semantic_revision=8
    ),
    artifact_kind="procedure",
    artifact_tag="playbill-procedure-v2",
    current=False,
)
SOURCE_CHECKED_PROCEDURE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        PROCEDURE_LAW_V2_IDENTIFIER, "playbill-procedure-v2", semantic_revision=9
    ),
    artifact_kind="procedure",
    artifact_tag="playbill-procedure-v2",
    current=False,
)
SOURCE_CHECKED_UPGRADE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.compiler-upgrade.v1", "playbill-compiler-upgrade-v1", semantic_revision=7
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
    current=False,
)
SDK_SOURCE_UPGRADE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.compiler-upgrade.v1", "playbill-compiler-upgrade-v1", semantic_revision=5
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
    current=False,
)
CLAIM_EVIDENCE_UPGRADE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.compiler-upgrade.v1", "playbill-compiler-upgrade-v1", semantic_revision=6
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
    current=False,
)
PROVIDER_CONTRACT_UPGRADE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.compiler-upgrade.v1", "playbill-compiler-upgrade-v1", semantic_revision=2
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
    current=False,
)

PROVIDER_V3_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.provider.v3", "playbill-provider-v3", semantic_revision=1
    ),
    artifact_kind="provider",
    artifact_tag="playbill-provider-v3",
)
PROVIDER_INTERFACE_V2_ACCEPTANCE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.provider-interface.v2", "playbill-provider-interface-v2", semantic_revision=1
    ),
    artifact_kind="provider-interface",
    artifact_tag="playbill-provider-interface-v2",
)
PROVIDER_PACKAGE_UPGRADE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.compiler-upgrade.v1", "playbill-compiler-upgrade-v1", semantic_revision=3
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
    current=False,
)

RESOURCE_BUDGET_UPGRADE_LAW = InstalledAcceptanceLaw(
    coordinate=_artifact_law_coordinate(
        "playbill.compiler-upgrade.v1", "playbill-compiler-upgrade-v1", semantic_revision=4
    ),
    artifact_kind="compiler-upgrade",
    artifact_tag="playbill-compiler-upgrade-v1",
    current=False,
)

ACCEPTANCE_LAWS = AcceptanceLawRegistry(
    (
        PROVIDER_V3_ACCEPTANCE_LAW,
        PROVIDER_INTERFACE_V2_ACCEPTANCE_LAW,
        PROVIDER_PACKAGE_UPGRADE_LAW,
        RESOURCE_BUDGET_UPGRADE_LAW,
        SDK_SOURCE_UPGRADE_LAW,
        CLAIM_EVIDENCE_UPGRADE_LAW,
        SDK_SOURCE_PROCEDURE_LAW,
        SOURCE_CHECKED_PROCEDURE_LAW,
        SOURCE_CHECKED_UPGRADE_LAW,
        TRIGGER_CAPTURE_UPGRADE_LAW,
        AUTHORITY_VERBS_UPGRADE_LAW,
        GOVERNED_TRIGGERS_UPGRADE_LAW,
        PROVIDER_CONTRACT_UPGRADE_LAW,
        COMPILER_UPGRADE_ACCEPTANCE_LAW,
        APPROVAL_POLICY_ACCEPTANCE_LAW,
        PROCEDURE_RUNTIME_POLICY_ACCEPTANCE_LAW,
        CAPTURE_CONTRACT_ACCEPTANCE_LAW,
        CAPTURE_CONTRACT_REVISION_3_ACCEPTANCE_LAW,
        ATTESTATION_ACCEPTANCE_LAW,
        RESOLUTION_CONTRACT_ACCEPTANCE_LAW,
        CLAIM_V2_ACCEPTANCE_LAW,
        CLAIM_V2_REVISION_7_ACCEPTANCE_LAW,
        CLAIM_V2_REVISION_6_ACCEPTANCE_LAW,
        CLAIM_V3_ACCEPTANCE_LAW,
        CLAIM_V3_REVISION_9_ACCEPTANCE_LAW,
        CLAIM_V3_REVISION_8_ACCEPTANCE_LAW,
        CLAIM_V3_REVISION_7_ACCEPTANCE_LAW,
        CLAIM_TYPE_ACCEPTANCE_LAW,
        CLAIM_TYPE_REVISION_4_ACCEPTANCE_LAW,
        CLAIM_TYPE_V3_ACCEPTANCE_LAW,
        CLAIM_TYPE_V3_REVISION_4_ACCEPTANCE_LAW,
        CLAIM_TYPE_V4_ACCEPTANCE_LAW,
        CLAIM_TYPE_V4_REVISION_4_ACCEPTANCE_LAW,
        CLAIM_TYPE_V5_ACCEPTANCE_LAW,
        CLAIM_TYPE_V5_REVISION_4_ACCEPTANCE_LAW,
        CLAIM_TYPE_V6_ACCEPTANCE_LAW,
        CLAIM_TYPE_V6_REVISION_1_ACCEPTANCE_LAW,
        CLAIM_TYPE_V7_ACCEPTANCE_LAW,
        DOCUMENT_ACCEPTANCE_LAW,
        EXHAUST_PROMOTION_ACCEPTANCE_LAW,
        PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
        PROCEDURE_V2_ACCEPTANCE_LAW,
        LINE_V6_ACCEPTANCE_LAW,
        BLUEPRINT_ACCEPTANCE_LAW,
        TRIGGER_ACCEPTANCE_LAW,
        PROVIDER_ACCEPTANCE_LAW,
        PROVIDER_V2_ACCEPTANCE_LAW,
        PROVIDER_INTERFACE_ACCEPTANCE_LAW,
        QUERY_DEFINITION_ACCEPTANCE_LAW,
        QUERY_DEFINITION_REVISION_4_ACCEPTANCE_LAW,
        QUERY_DEFINITION_V2_ACCEPTANCE_LAW,
        SOURCE_ACQUISITION_POLICY_ACCEPTANCE_LAW,
        PROCEDURE_MANDATE_ACCEPTANCE_LAW,
        PROCEDURE_MANDATE_V2_ACCEPTANCE_LAW,
        SUBJECT_ACCEPTANCE_LAW,
    )
)


__all__ = [
    "AcceptanceLawRegistry",
    "InstalledAcceptanceLaw",
]
