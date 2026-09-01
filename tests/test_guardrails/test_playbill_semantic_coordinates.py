"""Pin Playbill's current semantic coordinates at the reviewed C1 cut.

Update recipe: change an expected row only with a reviewed semantic-law change,
increment its semantic revision (or add a post-release successor), recompute the
``playbill-law-v1`` digest, and atomically review the compiler coordinate plus
the authoring catalog/snapshot re-pins required by that change.
"""

from __future__ import annotations

from cruxible_client.contracts.canonical import AcceptanceLawDigest, canonical_digest, typed_digest
from cruxible_client.contracts.laws import (
    APPROVAL_POLICY_ACCEPTANCE_LAW,
    CAPTURE_CONTRACT_ACCEPTANCE_LAW,
    CLAIM_LAW_V3_REVISION_8,
    CLAIM_TYPE_ACCEPTANCE_LAW,
    CLAIM_TYPE_V3_ACCEPTANCE_LAW,
    CLAIM_TYPE_V4_ACCEPTANCE_LAW,
    CLAIM_V2_ACCEPTANCE_LAW,
    CLAIM_V3_ACCEPTANCE_LAW,
    CLAIM_V3_REVISION_7_ACCEPTANCE_LAW,
    DOCUMENT_ACCEPTANCE_LAW,
    EXHAUST_PROMOTION_ACCEPTANCE_LAW,
    LINE_ACCEPTANCE_LAW,
    LINE_V2_ACCEPTANCE_LAW,
    PLAYBILL_ACCEPTANCE_LAWS,
    PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
    PROCEDURE_ACCEPTANCE_LAW,
    PROCEDURE_MANDATE_ACCEPTANCE_LAW,
    PROCEDURE_REVISION_5_ACCEPTANCE_LAW,
    PROCEDURE_RUNTIME_POLICY_ACCEPTANCE_LAW,
    PROCEDURE_V2_ACCEPTANCE_LAW,
    PROCEDURE_V2_REVISION_5_ACCEPTANCE_LAW,
    PROVIDER_ACCEPTANCE_LAW,
    PROVIDER_INTERFACE_ACCEPTANCE_LAW,
    PROVIDER_V2_ACCEPTANCE_LAW,
    QUERY_DEFINITION_ACCEPTANCE_LAW,
    SOURCE_ACQUISITION_POLICY_ACCEPTANCE_LAW,
    STANDING_MANDATE_ACCEPTANCE_LAW,
    SUBJECT_ACCEPTANCE_LAW,
    InstalledAcceptanceLaw,
)
from cruxible_core.playbill.compiler import (
    P2_B0_COMPILER,
    P2_B1_COMPILER,
    P2_B2_COMPILER,
    P2_C_COMPILER,
    PC_DF2_COMPILER,
    PC_E1_COMPILER,
    PC_HR_COMPILER,
)

LAW_COORDINATES: tuple[
    tuple[InstalledAcceptanceLaw, str, str, int, str],
    ...,
] = (
    (
        APPROVAL_POLICY_ACCEPTANCE_LAW,
        "playbill.approval-policy.v1",
        "playbill-approval-policy-v1",
        1,
        "sha256:027ad99ab0bde3c646371498d16ca8cd3005646fcfc9ac0f4ef3848fa2f2d631",
    ),
    (
        PROCEDURE_RUNTIME_POLICY_ACCEPTANCE_LAW,
        "playbill.procedure-runtime-policy.v1",
        "playbill-procedure-runtime-policy-v1",
        1,
        "sha256:ed84565df9497e9beaf88eeba107ec540d18a18778b9d98086db12ac7e5164f1",
    ),
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
        6,
        "sha256:2daba97d8453372b7fee8782a7ac116a9c184fdf4d96287257eb0204285ba887",
    ),
    (
        CLAIM_V3_ACCEPTANCE_LAW,
        "playbill.claim.v3",
        "playbill-claim-v3",
        8,
        "sha256:8aae4d764d32c52792d7ef2a81715c92d7c198b69cc74ec2f8882bcda0a16aa9",
    ),
    (
        PROVIDER_ACCEPTANCE_LAW,
        "playbill.provider.v1",
        "playbill-provider-v1",
        3,
        "sha256:421299d256d325476114cd527325909c354aecab66eae748f380fd38d6158b51",
    ),
    (
        PROVIDER_V2_ACCEPTANCE_LAW,
        "playbill.provider.v2",
        "playbill-provider-v2",
        1,
        "sha256:70cabd3f8e60587e32339678a041f906a0059f443451c6afb3478556f5ae0640",
    ),
    (
        PROVIDER_INTERFACE_ACCEPTANCE_LAW,
        "playbill.provider-interface.v1",
        "playbill-provider-interface-v1",
        1,
        "sha256:f42b09db7656931ac3c42d4f1ac526877bc3346bb83da79547e696f9c01f1636",
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
        PROCEDURE_MANDATE_ACCEPTANCE_LAW,
        "playbill.procedure-mandate.v1",
        "playbill-procedure-mandate-v1",
        1,
        "sha256:dc874c06567d0405554a80cbc12f2f586765b9c6fcbe03a60634b72a06312ae9",
    ),
    (
        PROCEDURE_ACCEPTANCE_LAW,
        "playbill.procedure.v1",
        "playbill-procedure-v1",
        6,
        "sha256:a198a563896996446abf261879f002a664722be359f63f1d7fe701119345a433",
    ),
    (
        PROCEDURE_V2_ACCEPTANCE_LAW,
        "playbill.procedure.v2",
        "playbill-procedure-v2",
        6,
        "sha256:bcb271659f2952b6e655b90b38e478a433b4fcd41fd25f4b8a7dc2a0443aefe9",
    ),
    (
        LINE_ACCEPTANCE_LAW,
        "playbill.line.v1",
        "playbill-line-v1",
        3,
        "sha256:ec822853eb8d3dbfddc0a54291205f5310cbf8bdf11f4326d300a1ab70ad5249",
    ),
    (
        LINE_V2_ACCEPTANCE_LAW,
        "playbill.line.v2",
        "playbill-line-v2",
        1,
        "sha256:d89ad9cd8c1f0433e866da524d6e5be56d45c067217a3bcb2d7be294e0c00d50",
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

HISTORICAL_LAW_COORDINATES: tuple[
    tuple[InstalledAcceptanceLaw, str, str, int, str],
    ...,
] = (
    (
        CLAIM_V3_REVISION_7_ACCEPTANCE_LAW,
        "playbill.claim.v3",
        "playbill-claim-v3",
        7,
        "sha256:db74ae5ae5f2fb90e2511e9a545f3c4507567323ef1adf237d6d5a2c431a9129",
    ),
    (
        PROCEDURE_REVISION_5_ACCEPTANCE_LAW,
        "playbill.procedure.v1",
        "playbill-procedure-v1",
        5,
        "sha256:5b8317597568eefa490c174ed69ab6a8bad567a37f9159b0d552e32881b13489",
    ),
    (
        PROCEDURE_V2_REVISION_5_ACCEPTANCE_LAW,
        "playbill.procedure.v2",
        "playbill-procedure-v2",
        5,
        "sha256:6aa5295ab49ce917156d718dd46f6e991a422298581e428bd7a252a7079cec83",
    ),
)


def test_playbill_acceptance_law_coordinates_are_exact() -> None:
    assert CLAIM_LAW_V3_REVISION_8.digest == (
        "sha256:8aae4d764d32c52792d7ef2a81715c92d7c198b69cc74ec2f8882bcda0a16aa9"
    )
    assert CLAIM_LAW_V3_REVISION_8 == CLAIM_V3_ACCEPTANCE_LAW.coordinate
    seen_coordinates: set[tuple[str, str]] = set()
    seen_tags: set[str] = set()
    for law, identifier, artifact_tag, revision, expected_digest in (
        *LAW_COORDINATES,
        *HISTORICAL_LAW_COORDINATES,
    ):
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
        if law.current:
            seen_tags.add(artifact_tag)

    assert len(seen_coordinates) == len(LAW_COORDINATES) + len(HISTORICAL_LAW_COORDINATES)
    assert len(seen_tags) == len(LAW_COORDINATES)
    assert set(PLAYBILL_ACCEPTANCE_LAWS._by_coordinate) == seen_coordinates
    assert set(PLAYBILL_ACCEPTANCE_LAWS._current_by_tag) == seen_tags


def test_playbill_compiler_coordinate_is_exact() -> None:
    pc_e1_expected = "sha256:62d6aa3f0c7b8657d9ecbdb1a7e5c8bd02fc711e114f850ce35e212035efa9df"
    pc_e1_computed = "sha256:" + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "projection_content": "claims-procedures-runtime-v1",
            "schema_version": 1,
            "semantic_revision": 10,
        },
    )
    expected = "sha256:58e6c8db50a1fd7e9f73578a2f827c86aa741ab6b4e60aa38ea08d6d792ae0b5"
    computed = "sha256:" + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "projection_content": "claims-procedures-runtime-v1",
            "schema_version": 1,
            "semantic_revision": 11,
        },
    )
    assert pc_e1_computed == pc_e1_expected
    assert PC_E1_COMPILER.rule_digest == pc_e1_expected
    assert computed == expected
    assert P2_B0_COMPILER.rule_digest == expected
    pc_hr_expected = "sha256:" + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "projection_content": "claims-procedures-runtime-v1",
            "schema_version": 1,
            "semantic_revision": 12,
        },
    )
    assert PC_HR_COMPILER.rule_digest == pc_hr_expected
    p2_b1_expected = "sha256:" + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "projection_content": "claims-procedures-runtime-v1",
            "schema_version": 1,
            "semantic_revision": 13,
        },
    )
    assert P2_B1_COMPILER.rule_digest == p2_b1_expected
    p2_c_expected = "sha256:" + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "projection_content": "claims-procedures-runtime-v1",
            "schema_version": 1,
            "semantic_revision": 14,
        },
    )
    assert p2_c_expected == (
        "sha256:189154dd919b8f88091da2053b1fe07c80e5074e93e7290c3b5a11c01370da1e"
    )
    assert P2_C_COMPILER.rule_digest == p2_c_expected
    pc_df2_expected = "sha256:" + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "projection_content": "claims-procedures-runtime-v1",
            "schema_version": 1,
            "semantic_revision": 15,
        },
    )
    assert pc_df2_expected == (
        "sha256:b8865b17412aa7d8606e44ee7d713858dbac9a4845e7b953e0e6a6800fb8735d"
    )
    assert PC_DF2_COMPILER.rule_digest == pc_df2_expected
    p2_b2_expected = "sha256:" + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "projection_content": "claims-procedures-runtime-v1",
            "schema_version": 1,
            "semantic_revision": 16,
        },
    )
    assert p2_b2_expected == (
        "sha256:dbe2bd6c03327a15e49712acd40c5cc6528e9b135374793c513519c83c687648"
    )
    assert P2_B2_COMPILER.rule_digest == p2_b2_expected
