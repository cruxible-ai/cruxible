"""Pin Cruxible's current semantic coordinates at the reviewed C1 cut.

Update recipe: change an expected row only with a reviewed semantic-law change,
increment its semantic revision (or add a post-release successor), recompute the
``playbill-law-v1`` digest, and atomically review the compiler coordinate plus
the authoring catalog/snapshot re-pins required by that change.
"""

from __future__ import annotations

import ast
from pathlib import Path

import cruxible_client.contracts.captures as capture_contracts
import cruxible_core.compiler.compiler as compiler_module
from cruxible_client.contracts.canonical import AcceptanceLawDigest, canonical_digest, typed_digest
from cruxible_client.contracts.laws import (
    ACCEPTANCE_LAWS,
    APPROVAL_POLICY_ACCEPTANCE_LAW,
    ATTESTATION_ACCEPTANCE_LAW,
    CAPTURE_CONTRACT_ACCEPTANCE_LAW,
    CLAIM_LAW_V2_REVISION_10,
    CLAIM_LAW_V3_REVISION_11,
    CLAIM_TYPE_ACCEPTANCE_LAW,
    CLAIM_TYPE_V3_ACCEPTANCE_LAW,
    CLAIM_TYPE_V4_ACCEPTANCE_LAW,
    CLAIM_TYPE_V5_ACCEPTANCE_LAW,
    CLAIM_TYPE_V6_ACCEPTANCE_LAW,
    CLAIM_TYPE_V7_ACCEPTANCE_LAW,
    CLAIM_TYPE_V8_ACCEPTANCE_LAW,
    CLAIM_TYPE_V9_ACCEPTANCE_LAW,
    CLAIM_V2_ACCEPTANCE_LAW,
    CLAIM_V3_ACCEPTANCE_LAW,
    CONTRACT_ACCEPTANCE_LAW,
    DOCUMENT_ACCEPTANCE_LAW,
    EXHAUST_PROMOTION_ACCEPTANCE_LAW,
    LIGHT_CAPTURE_CONTRACT_LAW,
    LINE_V6_ACCEPTANCE_LAW,
    NAMED_PROVIDER_IDENTITY_LAW,
    NAMED_REFERENCE_LAWS,
    PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
    PROCEDURE_MANDATE_ACCEPTANCE_LAW,
    PROCEDURE_MANDATE_V2_ACCEPTANCE_LAW,
    PROCEDURE_MANDATE_V3_ACCEPTANCE_LAW,
    PROCEDURE_RUNTIME_POLICY_ACCEPTANCE_LAW,
    PROCEDURE_V2_ACCEPTANCE_LAW,
    PROVIDER_ACCEPTANCE_LAW,
    PROVIDER_BUILD_ACCEPTANCE_LAW,
    PROVIDER_V2_ACCEPTANCE_LAW,
    PROVIDER_V3_ACCEPTANCE_LAW,
    QUERY_DEFINITION_ACCEPTANCE_LAW,
    QUERY_DEFINITION_V2_ACCEPTANCE_LAW,
    RESOLUTION_CONTRACT_ACCEPTANCE_LAW,
    SUBJECT_ACCEPTANCE_LAW,
    SUBJECT_KIND_ACCEPTANCE_LAW,
    TRIGGER_ACCEPTANCE_LAW,
    InstalledAcceptanceLaw,
)
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.compiler import (
    CURRENT_COMPILER,
    CURRENT_COMPILER_REVISION,
    compiler_revision_label,
    current_compiler_coordinate,
)
from cruxible_core.proposals.candidate_cards import CARD_RENDERER_DIGEST

LAW_COORDINATES: tuple[
    tuple[InstalledAcceptanceLaw, str, str, int, str],
    ...,
] = (
    (
        PROVIDER_BUILD_ACCEPTANCE_LAW,
        "cruxible.provider.v4",
        "cruxible-provider-v4",
        1,
        "sha256:526170687d3add19411e11fc7d9cc7e74c7a6305e1ad564a8ba8b5c5c30de05f",
    ),
    (
        LINE_V6_ACCEPTANCE_LAW,
        "playbill.line.v6",
        "playbill-line-v6",
        1,
        "sha256:4e145cfe2c8250d7851d9383137686cc6591d2b5219cd12b18e271db555b3b97",
    ),
    (
        TRIGGER_ACCEPTANCE_LAW,
        "playbill.trigger.v1",
        "playbill-trigger-v1",
        1,
        "sha256:7fc3d357dbd23307f849c5750181a21035d7d9866e07db022fb3722e59fe2bce",
    ),
    (
        SUBJECT_KIND_ACCEPTANCE_LAW,
        "cruxible.subject-kind.v1",
        "cruxible-subject-kind-v1",
        1,
        "sha256:5f8e0ef33e854fc3355bc02a3cac0a6c08f216c59175b0a53ee2195bdc819510",
    ),
    (
        CONTRACT_ACCEPTANCE_LAW,
        "cruxible.contract.v1",
        "cruxible-contract-v1",
        1,
        "sha256:ef15ee167046bf9d2a08e7df6a4690338aee8c48d65f606e964da7e00e778a37",
    ),
    (
        CLAIM_TYPE_V5_ACCEPTANCE_LAW,
        "playbill.claim-type.v5",
        "playbill-claim-type-v5",
        5,
        "sha256:3148ff609e052c454a04f430d32c0085af5587cfbde5fbc62e460efa8eb3f288",
    ),
    (
        CLAIM_TYPE_V6_ACCEPTANCE_LAW,
        "playbill.claim-type.v6",
        "playbill-claim-type-v6",
        2,
        "sha256:9687a38153f0cfc5f5636b1d718d4242cba1a43151ba1684af31d3d37bc1b208",
    ),
    (
        CLAIM_TYPE_V7_ACCEPTANCE_LAW,
        "playbill.claim-type.v7",
        "playbill-claim-type-v7",
        1,
        "sha256:e9617add75e2747320ade92631b2ab4c50253b70693d92afb54fefbbdc4d9375",
    ),
    (
        CLAIM_TYPE_V8_ACCEPTANCE_LAW,
        "playbill.claim-type.v8",
        "playbill-claim-type-v8",
        1,
        "sha256:45308ae9d41af7abf075b38f9bd6eba05f77c2480506c2cd943d8ba70e55fc7f",
    ),
    (
        CLAIM_TYPE_V9_ACCEPTANCE_LAW,
        "cruxible.claim-type.v9",
        "cruxible-claim-type-v9",
        1,
        "sha256:add04a415c6ebc73fbf472604d023d0427a5522b96026e0d1bcc29c346c40945",
    ),
    (
        PROVIDER_V3_ACCEPTANCE_LAW,
        "playbill.provider.v3",
        "playbill-provider-v3",
        1,
        "sha256:dd33e1374ee6427ca41698f405fdf580f3887e68edc37c2269fa7b7fba819f49",
    ),
    (
        RESOLUTION_CONTRACT_ACCEPTANCE_LAW,
        "cruxible.resolution-contract.v1",
        "playbill-resolution-contract-v1",
        1,
        "sha256:d2255339f91b465d0f98e7770e052adf08a1775cae7c670ed5d8884410685913",
    ),
    (
        ATTESTATION_ACCEPTANCE_LAW,
        "cruxible.accepted-claim-attestation.v1",
        "playbill-claim-attestation-envelope-v2",
        1,
        "sha256:9dd4ea044be49926f2d5d82e5d265b83e10dafbd7868631ef4d95fc607effd2b",
    ),
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
        5,
        "sha256:8f6d6750d6d1c5492da7a501f7c24e0772900f45d1c450b1a9653bfaf5661ec0",
    ),
    (
        CLAIM_TYPE_V3_ACCEPTANCE_LAW,
        "playbill.claim-type.v3",
        "playbill-claim-type-v3",
        5,
        "sha256:8f03494162415e6823d4c457d79c1fb6bccb428953848aedf7daaab17e2f8bde",
    ),
    (
        CLAIM_TYPE_V4_ACCEPTANCE_LAW,
        "playbill.claim-type.v4",
        "playbill-claim-type-v4",
        5,
        "sha256:61f95319b62218fb614905e5e22baea97f134a9c865fbf1776afcbd3ac47c4a6",
    ),
    (
        CAPTURE_CONTRACT_ACCEPTANCE_LAW,
        "playbill.capture-contract.v1",
        "playbill-capture-contract-v1",
        4,
        "sha256:dafcf7d1104fec1da02c423478d801fe7e4060e78319ebe152d4789a41e2f25e",
    ),
    (
        CLAIM_V2_ACCEPTANCE_LAW,
        "playbill.claim.v2",
        "playbill-claim-v2",
        10,
        "sha256:d2d8bde91f63883aadd803b9c2794ae5a8cb310e3319c5e5a89610d7ed7dfb31",
    ),
    (
        CLAIM_V3_ACCEPTANCE_LAW,
        "playbill.claim.v3",
        "playbill-claim-v3",
        11,
        "sha256:1a75706bf54bbd681ed4da2c68f4050804904e417a2dcead8691a655e0953a00",
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
        PROCEDURE_MANDATE_V2_ACCEPTANCE_LAW,
        "playbill.procedure-mandate.v2",
        "playbill-procedure-mandate-v2",
        1,
        "sha256:5caa3dded0dc3c3c2c02eac605f95f8eb18ddd2a34b5701d18f9c20e10fc6124",
    ),
    (
        PROCEDURE_MANDATE_ACCEPTANCE_LAW,
        "playbill.procedure-mandate.v1",
        "playbill-procedure-mandate-v1",
        1,
        "sha256:dc874c06567d0405554a80cbc12f2f586765b9c6fcbe03a60634b72a06312ae9",
    ),
    (
        PROCEDURE_V2_ACCEPTANCE_LAW,
        "playbill.procedure.v2",
        "playbill-procedure-v2",
        6,
        "sha256:bcb271659f2952b6e655b90b38e478a433b4fcd41fd25f4b8a7dc2a0443aefe9",
    ),
    (
        QUERY_DEFINITION_V2_ACCEPTANCE_LAW,
        "playbill.query-definition.v2",
        "playbill-query-definition-v2",
        1,
        "sha256:45b2e729a3ac7168623dc799affcad2d97633c5e944ac56551d2d0d5b43eb5fc",
    ),
    (
        QUERY_DEFINITION_ACCEPTANCE_LAW,
        "playbill.query-definition.v1",
        "playbill-query-definition-v1",
        5,
        "sha256:9bd27f8ffacfae8f9c593f74bdb548526f27040a0dc37d41289dfd86f6365c7d",
    ),
    (
        EXHAUST_PROMOTION_ACCEPTANCE_LAW,
        "playbill.exhaust-promotion.v1",
        "playbill-exhaust-promotion-v1",
        3,
        "sha256:8ec42cbce3d28a995832a9a255b7618ae4a4de7078d4d85047c180b56f869920",
    ),
)


LAW_COORDINATES += (
    (
        LIGHT_CAPTURE_CONTRACT_LAW,
        "cruxible.capture-contract.v2",
        "cruxible-capture-contract-v2",
        1,
        "sha256:49b22140bec5b033c02b238e4be3c28fc665a93e5fc831aa6fb60febf2621830",
    ),
)


LAW_COORDINATES += (
    (
        NAMED_REFERENCE_LAWS[0],
        "cruxible.capture-contract.named.v1",
        "cruxible-capture-contract-v3",
        1,
        "sha256:6555affa65a4c90224a03b0b3305fad1c89ef4f61d15bbee153a24c04b61dcc1",
    ),
    (
        NAMED_REFERENCE_LAWS[1],
        "cruxible.procedure.named.v1",
        "cruxible-procedure-v3",
        1,
        "sha256:43266f5543258472166c75b9c7ab202e48104873eded063bf95ee1b84fcffef7",
    ),
    (
        NAMED_REFERENCE_LAWS[2],
        "cruxible.query-definition.named.v1",
        "cruxible-query-definition-v3",
        1,
        "sha256:f39c88c44f5fe5cf49a61413d7c754790229050de06b21b1e831c28d52a90562",
    ),
    (
        NAMED_REFERENCE_LAWS[3],
        "cruxible.trigger.named.v1",
        "cruxible-trigger-v2",
        1,
        "sha256:b3ec92b76ee0ccb89e8c3c22f67f6cd5f2faa2ba77a597fc2f274f4f149f15fe",
    ),
    (
        NAMED_REFERENCE_LAWS[4],
        "cruxible.line.named.v1",
        "cruxible-line-v8",
        1,
        "sha256:5a348c529ba5e86c6fe1e416fc0a06e04c9b6c382b9eca9c3ae1c18ad4226b98",
    ),
    (
        NAMED_REFERENCE_LAWS[5],
        "cruxible.provider.named.v1",
        "cruxible-provider-v6",
        1,
        "sha256:949d9d7219df87ef73187c23a5db31bb346c808b1f60e77680fcde6b3f38ddac",
    ),
    (
        PROCEDURE_MANDATE_V3_ACCEPTANCE_LAW,
        "cruxible.procedure-mandate.v3",
        "cruxible-procedure-mandate-v3",
        1,
        "sha256:dc26f33bab27da535fe802668a957e8218c5ce5c39347cee07992965255b0522",
    ),
    (
        NAMED_PROVIDER_IDENTITY_LAW,
        "cruxible.provider-identity.v3",
        "cruxible-provider-identity-v3",
        1,
        "sha256:eeee10b2d9b95eea4bf55bd47a0500c63657ebb202b9e0c659faff3eb0644e5a",
    ),
)


def test_playbill_acceptance_law_coordinates_are_exact() -> None:
    """One installed law per artifact tag, and nothing else in the registry."""

    assert CLAIM_LAW_V3_REVISION_11 == CLAIM_V3_ACCEPTANCE_LAW.coordinate
    assert CLAIM_LAW_V2_REVISION_10 == CLAIM_V2_ACCEPTANCE_LAW.coordinate
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

    assert len(seen_coordinates) == len(seen_tags) == len(LAW_COORDINATES)
    assert set(ACCEPTANCE_LAWS._by_coordinate) == seen_coordinates
    assert set(ACCEPTANCE_LAWS._current_by_tag) == seen_tags


def test_playbill_compiler_coordinate_is_exact() -> None:
    expected = "sha256:e77d9aaac5116b76aaf7b7060dedde6bca2cdaaeaa909c59c391bd20f3779924"
    computed = "sha256:" + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "schema_version": 1,
            "projection_content": "claims-procedures-runtime-v1",
            "semantic_revision": 36,
            "candidate_card_renderer_digest": CARD_RENDERER_DIGEST,
            "revision_name": "light-artifacts-v1",
        },
    )
    assert computed == expected
    assert CURRENT_COMPILER.rule_digest == expected
    assert current_compiler_coordinate() == CURRENT_COMPILER


def test_no_revision_literal_lives_outside_the_compiler_and_this_guard() -> None:
    """One preimage, one place: a second copy is how the card path went dark.

    Scoped to `src/cruxible_core`, which owns the semantic compiler lineage. The
    client package carries its own `semantic_revision` literals for the
    acceptance-law lineage, which is a different succession and not this guard's.
    """

    source_root = Path(compiler_module.__file__).resolve().parents[3]
    offenders: list[str] = []
    for path in sorted((source_root / "cruxible_core").rglob("*.py")):
        if path.name == "compiler.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "semantic_revision":
                offenders.append(f"{path.name}:{node.value.lineno}")
            if (
                isinstance(node, ast.Constant)
                and node.value == "semantic_revision"
                and path.name != "compiler.py"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_capture_v1_run_id_grammar_is_retained_exactly() -> None:
    assert capture_contracts._RUN_ID_RE.pattern == (  # noqa: SLF001
        r"^[a-z0-9][a-z0-9_.:-]{0,255}$"
    )
    assert capture_contracts._RUN_ID_V2_RE.pattern == (  # noqa: SLF001
        r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$"
    )


def test_the_release_installs_exactly_one_compiler_coordinate() -> None:
    """One coordinate: an earlier build's is refused, not replayed (no 0.3.2 porting)."""

    assert CURRENT_COMPILER_REVISION == "light-artifacts-v1"
    assert compiler_revision_label(CURRENT_COMPILER) == "light-artifacts-v1"
    earlier = CompilerCoordinate(
        rule_digest="sha256:56542b566100db8fadfd4bfcea8f0fe58e37de444885906f3914ec1cc225d085"
    )
    assert compiler_revision_label(earlier) is None
    assert {
        name
        for name, value in vars(compiler_module).items()
        if isinstance(value, CompilerCoordinate)
    } == {"CURRENT_COMPILER"}
