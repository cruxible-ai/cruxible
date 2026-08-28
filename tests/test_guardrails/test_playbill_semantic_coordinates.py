"""Pin Playbill's current semantic coordinates at the reviewed C1 cut.

Update recipe: change an expected row only with a reviewed semantic-law change,
increment its semantic revision (or add a post-release successor), recompute the
``playbill-law-v1`` digest, and atomically review the compiler coordinate plus
the authoring catalog/snapshot re-pins required by that change.
"""

from __future__ import annotations

from cruxible_client.contracts.canonical import AcceptanceLawDigest, canonical_digest, typed_digest
from cruxible_client.contracts.laws import (
    CAPTURE_CONTRACT_ACCEPTANCE_LAW,
    CLAIM_TYPE_ACCEPTANCE_LAW,
    CLAIM_TYPE_V3_ACCEPTANCE_LAW,
    CLAIM_TYPE_V4_ACCEPTANCE_LAW,
    CLAIM_V2_ACCEPTANCE_LAW,
    CLAIM_V3_ACCEPTANCE_LAW,
    DOCUMENT_ACCEPTANCE_LAW,
    EXHAUST_PROMOTION_ACCEPTANCE_LAW,
    LINE_ACCEPTANCE_LAW,
    PLAYBILL_ACCEPTANCE_LAWS,
    PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
    PROCEDURE_ACCEPTANCE_LAW,
    PROCEDURE_V2_ACCEPTANCE_LAW,
    PROVIDER_ACCEPTANCE_LAW,
    QUERY_DEFINITION_ACCEPTANCE_LAW,
    SOURCE_ACQUISITION_POLICY_ACCEPTANCE_LAW,
    STANDING_MANDATE_ACCEPTANCE_LAW,
    SUBJECT_ACCEPTANCE_LAW,
    InstalledAcceptanceLaw,
)
from cruxible_core.playbill.compiler import PC_E1_COMPILER

LAW_COORDINATES: tuple[
    tuple[InstalledAcceptanceLaw, str, str, int, str],
    ...,
] = (
    (
        DOCUMENT_ACCEPTANCE_LAW,
        "playbill.document.v1",
        "playbill-document-v1",
        3,
        "sha256:1723c5d87c29634c6732091104bc93416fbfb2248165ec05b710d4aa20587ea8",
    ),
    (
        PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
        "playbill.principal-lifecycle.v1",
        "playbill-principal-v1",
        6,
        "sha256:21fcbf048960ddf7d9fc9ca51dfdb45e0a0e994708be32f81e946e18cc92eeb3",
    ),
    (
        SUBJECT_ACCEPTANCE_LAW,
        "playbill.subject.v1",
        "playbill-subject-v1",
        3,
        "sha256:656b60c438b61bf018bd4ae2e473d71fe08a47c450edf261e2c4adf86339ecd2",
    ),
    (
        CLAIM_TYPE_ACCEPTANCE_LAW,
        "playbill.claim-type.v1",
        "playbill-claim-type-v1",
        4,
        "sha256:7007f0be7dce19938fd7d0b24ef81ebff9a3d875c0704f7be89a3b649362056e",
    ),
    (
        CLAIM_TYPE_V3_ACCEPTANCE_LAW,
        "playbill.claim-type.v3",
        "playbill-claim-type-v3",
        4,
        "sha256:a872bc19183d4815e42577f0a31c871866d1a5c112db3dfbf63d728606eef35d",
    ),
    (
        CLAIM_TYPE_V4_ACCEPTANCE_LAW,
        "playbill.claim-type.v4",
        "playbill-claim-type-v4",
        4,
        "sha256:82cb58ba1671cd3467477b1e957ff1aaff8373b917f8c4ee9999a1ed8884f2b7",
    ),
    (
        CAPTURE_CONTRACT_ACCEPTANCE_LAW,
        "playbill.capture-contract.v1",
        "playbill-capture-contract-v1",
        3,
        "sha256:89618916c5a6667555e5bea86ad56929248bc4681ebd1f69215731a322cca473",
    ),
    (
        CLAIM_V2_ACCEPTANCE_LAW,
        "playbill.claim.v2",
        "playbill-claim-v2",
        4,
        "sha256:3cc4862e708d4053c7ec226af6f0d2daf4e8fde095b99c11e0e5b0a03c8879e9",
    ),
    (
        CLAIM_V3_ACCEPTANCE_LAW,
        "playbill.claim.v3",
        "playbill-claim-v3",
        4,
        "sha256:81b047be51705aa09d79ee9792f3153b445d7f1b117be99feb520c29eb06d617",
    ),
    (
        PROVIDER_ACCEPTANCE_LAW,
        "playbill.provider.v1",
        "playbill-provider-v1",
        3,
        "sha256:421299d256d325476114cd527325909c354aecab66eae748f380fd38d6158b51",
    ),
    (
        SOURCE_ACQUISITION_POLICY_ACCEPTANCE_LAW,
        "playbill.source-acquisition-policy.v1",
        "playbill-source-acquisition-policy-v1",
        3,
        "sha256:017aa56afdd0160f062abd5957d5900c1c201875f9c3f8a11ba7eb27074ae8a3",
    ),
    (
        STANDING_MANDATE_ACCEPTANCE_LAW,
        "playbill.standing-mandate.v1",
        "playbill-standing-mandate-v1",
        3,
        "sha256:ab79c01eee9bd149a301d2de27b82d3bb46d4e18717907020e54c4e9d3a75fe7",
    ),
    (
        PROCEDURE_ACCEPTANCE_LAW,
        "playbill.procedure.v1",
        "playbill-procedure-v1",
        3,
        "sha256:6ad4b29ee3638e2b492dd51ee4b1d786a742f1e6b22b6f87abf43e9565039522",
    ),
    (
        PROCEDURE_V2_ACCEPTANCE_LAW,
        "playbill.procedure.v2",
        "playbill-procedure-v2",
        3,
        "sha256:a7fa785a64f32648e704ab2c88f6882dea18d6ca708feac363634c706f50812d",
    ),
    (
        LINE_ACCEPTANCE_LAW,
        "playbill.line.v1",
        "playbill-line-v1",
        3,
        "sha256:ec822853eb8d3dbfddc0a54291205f5310cbf8bdf11f4326d300a1ab70ad5249",
    ),
    (
        QUERY_DEFINITION_ACCEPTANCE_LAW,
        "playbill.query-definition.v1",
        "playbill-query-definition-v1",
        4,
        "sha256:451b68d9eb1e1b6c776da06d7b6fd1e96e4f16b0d63d5a43f0580e06d03f9547",
    ),
    (
        EXHAUST_PROMOTION_ACCEPTANCE_LAW,
        "playbill.exhaust-promotion.v1",
        "playbill-exhaust-promotion-v1",
        3,
        "sha256:8ec42cbce3d28a995832a9a255b7618ae4a4de7078d4d85047c180b56f869920",
    ),
)


def test_playbill_acceptance_law_coordinates_are_exact() -> None:
    seen_coordinates: set[tuple[str, str]] = set()
    seen_tags: set[str] = set()
    for law, identifier, artifact_tag, revision, expected_digest in LAW_COORDINATES:
        computed = typed_digest(
            AcceptanceLawDigest,
            "playbill-law-v1",
            {
                "identifier": identifier,
                "artifact_tag": artifact_tag,
                "semantic_revision": revision,
            },
        ).tagged
        assert computed == expected_digest
        assert law.coordinate.identifier == identifier
        assert law.coordinate.digest == expected_digest
        assert law.artifact_tag == artifact_tag
        seen_coordinates.add((identifier, expected_digest))
        seen_tags.add(artifact_tag)

    assert len(seen_coordinates) == len(LAW_COORDINATES)
    assert len(seen_tags) == len(LAW_COORDINATES)
    assert set(PLAYBILL_ACCEPTANCE_LAWS._by_coordinate) == seen_coordinates
    assert set(PLAYBILL_ACCEPTANCE_LAWS._current_by_tag) == seen_tags


def test_playbill_compiler_coordinate_is_exact() -> None:
    expected = "sha256:d84e0032bec88ea8ab6c8708afe8b6c4f298a34a8eef98ad629d5e2018680243"
    computed = "sha256:" + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "projection_content": "claims-procedures-runtime-v1",
            "schema_version": 1,
            "semantic_revision": 6,
        },
    )
    assert computed == expected
    assert PC_E1_COMPILER.rule_digest == expected
