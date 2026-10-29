"""The accepted-generation wire formats, their verifiers, and the producer.

`playbill-candidate-v2`, `playbill-sroot-v2`, `playbill-dependency-graph-v3`, and
`playbill-changeset-v3` are pinned here against their goldens, and every one of
them is what a new proposal produces. Genesis alone keeps the flat manifest root
and `playbill-sroot-v1`; no other receipt or candidate version parses.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.candidates import (
    CandidateRecord,
    ClosureProof,
    SemanticCandidate,
    SemanticCandidateV1,
    candidate_digest,
    render_candidate_record,
)
from cruxible_client.contracts.canonical import (
    DependencyEdgeRoot,
    SemanticManifestRoot,
    SemanticMerkleRoot,
    canonical_bytes,
    manifest_root,
    semantic_projection,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.merkle import merkle_manifest_root
from cruxible_client.contracts.types import GenerationDescriptor
from cruxible_core.claims.closure import (
    ClosureEvaluationV3,
    dependency_edge_root,
    evaluate_dependency_closure,
)
from cruxible_core.proposals.proposals import evaluate_proposal_tree
from cruxible_core.proposals.settlement import (
    SEMANTIC_ROOT_V2_DOMAIN,
    ChangeActorBinding,
    ChangeSetRecord,
    build_change_set_record,
    change_set_digest,
    compute_semantic_root_v2,
    parse_change_set_record,
    render_change_set,
)
from tests.core_support._support import initialize_local

DOCUMENT_PATH = "documents/playbill-design.json"
GOLDENS = Path(__file__).parents[1] / "goldens" / "playbill"
CANDIDATE_GOLDEN = GOLDENS / "candidate-v2.json"
SROOT_GOLDEN = GOLDENS / "sroot-v2.json"
CHANGESET_GOLDEN = GOLDENS / "changeset-v3.json"

FLAT_ROOT = "sha256:" + "22" * 32
MERKLE_ROOT = "merkle-sha256:" + "22" * 32
PARENT_ROOT = "sha256:" + "11" * 32
DIFF_DIGEST = "sha256:" + "33" * 32
CHANGESET_DIGEST = "sha256:" + "44" * 32
APPROVALS = ("sha256:" + "55" * 32, "sha256:" + "66" * 32)
TIMESTAMP = "2026-08-19T09:15:00.000000Z"


def _document_tree(instance: Any) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Return the accepted tree and a one-Document successor tree."""

    base_tree = instance.proposal_service().transport.read_tree(instance.inspect().head_oid)
    body = instance.store_document_body(b"proposed")
    shell = DocumentShell(
        identity="document:playbill-design",
        document_kind="design",
        title="Cruxible design",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    return base_tree, {**base_tree, DOCUMENT_PATH: render_document(shell)}


def _candidate_v2(**overrides: object) -> SemanticCandidate:
    values: dict[str, object] = {
        "parent_semantic_root": PARENT_ROOT,
        "candidate_manifest_root": MERKLE_ROOT,
        "semantic_diff_digest": DIFF_DIGEST,
        "scope": ("claims/alpha.json",),
        "timestamp": TIMESTAMP,
    }
    return SemanticCandidate.model_validate({**values, **overrides})


def _record_v3() -> ChangeSetRecord:
    golden = json.loads(CHANGESET_GOLDEN.read_bytes())
    record = parse_change_set_record(golden["canonical_bytes"].encode(), path="changesets/x.json")
    assert isinstance(record, ChangeSetRecord)
    return record


def _candidate_record_v3() -> CandidateRecord:
    record = _record_v3()
    return CandidateRecord(
        candidate=record.candidate,
        candidate_digest=record.candidate_digest,
        required_tier=record.required_tier,
        approval_requirements=record.approval_requirements,
        activation_policy=record.activation_policy,
        closure_proof=record.closure_proof,
        members=record.members,
        law_evidence=record.law_evidence,
        law_digests=record.law_digests,
        compiler_digest=record.compiler_digest,
    )


# --------------------------------------------------------------------------
# playbill-candidate-v2
# --------------------------------------------------------------------------


def test_candidate_v2_carries_the_merkle_root_and_never_the_flat_one() -> None:
    candidate = _candidate_v2()
    assert candidate.tag == "playbill-candidate-v2"
    assert candidate.candidate_manifest_root == MERKLE_ROOT
    SemanticMerkleRoot.from_tagged(candidate.candidate_manifest_root)

    with pytest.raises(ValidationError, match="candidate_manifest_root"):
        _candidate_v2(candidate_manifest_root=FLAT_ROOT)
    with pytest.raises(ValidationError, match="candidate_manifest_root"):
        SemanticCandidateV1.model_validate(
            {
                "parent_semantic_root": PARENT_ROOT,
                "candidate_manifest_root": MERKLE_ROOT,
                "semantic_diff_digest": DIFF_DIGEST,
                "scope": ("claims/alpha.json",),
                "timestamp": TIMESTAMP,
            }
        )
    # The two versions carry one root each, never both, and never the other's.
    assert set(candidate.model_dump()) == set(
        SemanticCandidateV1(
            parent_semantic_root=PARENT_ROOT,
            candidate_manifest_root=FLAT_ROOT,
            semantic_diff_digest=DIFF_DIGEST,
            scope=("claims/alpha.json",),
            timestamp=TIMESTAMP,
        ).model_dump()
    )


def test_candidate_v2_keeps_every_other_v1_field_law() -> None:
    with pytest.raises(ValidationError, match="scope"):
        _candidate_v2(scope=())
    with pytest.raises(ValidationError, match="scope"):
        _candidate_v2(scope=("b.json", "a.json"))
    with pytest.raises(ValidationError, match="timestamp"):
        _candidate_v2(timestamp="2026-08-19T09:15:00Z")
    with pytest.raises(ValidationError):
        _candidate_v2(parent_semantic_root=MERKLE_ROOT)
    with pytest.raises(ValidationError):
        SemanticCandidate.model_validate(
            {**_candidate_v2().model_dump(mode="json"), "base_oid": "0" * 40}
        )


def test_the_two_candidate_versions_never_share_a_digest_domain() -> None:
    v2 = _candidate_v2()
    v1 = SemanticCandidateV1(
        parent_semantic_root=PARENT_ROOT,
        candidate_manifest_root=FLAT_ROOT,
        semantic_diff_digest=DIFF_DIGEST,
        scope=("claims/alpha.json",),
        timestamp=TIMESTAMP,
    )
    assert candidate_digest(v1) != candidate_digest(v2)
    payload = v2.model_dump(mode="json")
    payload.pop("tag")
    # Not merely because the roots are spelled differently: the domain moves
    # too, so the same five values hashed under v1's domain give a third value.
    under_v1_domain = hashlib.sha256(
        canonical_bytes({"tag": "playbill-candidate-v1", **payload})
    ).hexdigest()
    assert under_v1_domain != candidate_digest(v2).value
    assert sorted(payload) == [
        "candidate_manifest_root",
        "parent_semantic_root",
        "scope",
        "semantic_diff_digest",
        "timestamp",
    ]


def test_candidate_v2_preimage_and_digest_match_golden() -> None:
    golden = json.loads(CANDIDATE_GOLDEN.read_bytes())
    assert golden["format"] == "playbill-candidate-v2-golden-v1"
    candidate = SemanticCandidate.model_validate(golden["candidate"])
    payload = candidate.model_dump(mode="json")
    payload.pop("tag")
    assert (
        canonical_bytes({"tag": "playbill-candidate-v2", **payload}).decode()
        == golden["canonical_preimage"]
    )
    assert candidate_digest(candidate).tagged == golden["candidate_digest"]

    sibling = SemanticCandidateV1.model_validate(golden["flat_rooted_v1_sibling"]["candidate"])
    assert candidate_digest(sibling).tagged == golden["flat_rooted_v1_sibling"]["candidate_digest"]
    assert candidate_digest(sibling).tagged != golden["candidate_digest"]


# --------------------------------------------------------------------------
# playbill-sroot-v2
# --------------------------------------------------------------------------


def _sroot_v2(**overrides: object) -> str:
    values: dict[str, object] = {
        "manifest_root_value": MERKLE_ROOT,
        "changeset_digest_value": CHANGESET_DIGEST,
        "approval_digests": APPROVALS,
        "parent_semantic_root": PARENT_ROOT,
        "parent_derivation": "playbill-sroot-v2",
    }
    return compute_semantic_root_v2(**cast(Any, {**values, **overrides})).tagged


def test_sroot_v2_hashes_tagged_spellings_and_v1_hashed_bare_hex() -> None:
    preimage = canonical_bytes(
        {
            "tag": SEMANTIC_ROOT_V2_DOMAIN,
            "manifest_root": MERKLE_ROOT,
            "changeset_digest": CHANGESET_DIGEST,
            "approval_digests": list(APPROVALS),
            "parent_semantic_root": PARENT_ROOT,
            "parent_derivation": "playbill-sroot-v2",
        }
    )
    assert b"merkle-sha256:" in preimage
    assert _sroot_v2() == "sha256:" + hashlib.sha256(preimage).hexdigest()

    v1_preimage = canonical_bytes(
        {
            "tag": "playbill-sroot-v1",
            "manifest_root": "22" * 32,
            "changeset_digest": "44" * 32,
            "approval_digests": ["55" * 32, "66" * 32],
            "parent_semantic_root": "11" * 32,
        }
    )
    assert b"sha256:" not in v1_preimage


def test_sroot_v2_requires_a_merkle_manifest_root() -> None:
    with pytest.raises(ValueError):
        _sroot_v2(manifest_root_value=FLAT_ROOT)


def test_the_chain_rule_distinguishes_a_genesis_parent_from_a_receipt_parent() -> None:
    from_genesis = _sroot_v2(parent_derivation="playbill-sroot-v1")
    from_receipt = _sroot_v2(parent_derivation="playbill-sroot-v2")
    # The same 32-byte parent value, and yet two different children: a chain
    # cannot be re-narrated after the fact.
    assert from_genesis != from_receipt


def test_sroot_v2_keeps_the_v1_approval_digest_laws() -> None:
    with pytest.raises(SettlementIntegrityError, match="sorted and unique"):
        _sroot_v2(approval_digests=(APPROVALS[1], APPROVALS[0]))
    with pytest.raises(SettlementIntegrityError, match="sorted and unique"):
        _sroot_v2(approval_digests=(APPROVALS[0], APPROVALS[0]))
    with pytest.raises(ValueError):
        _sroot_v2(approval_digests=("merkle-sha256:" + "55" * 32,))
    assert _sroot_v2(approval_digests=()) != _sroot_v2()


def test_a_v2_semantic_root_is_still_an_ordinary_generation_descriptor_value() -> None:
    root = compute_semantic_root_v2(
        manifest_root_value=MERKLE_ROOT,
        changeset_digest_value=CHANGESET_DIGEST,
        approval_digests=APPROVALS,
        parent_semantic_root=PARENT_ROOT,
        parent_derivation="playbill-sroot-v1",
    )
    descriptor = GenerationDescriptor(
        semantic_root=root.value,
        git_oid="aa" * 20,
        parent_generation_root="77" * 32,
    )
    # The descriptor preimage is untouched by the succession: only the
    # derivation of the value inside `semantic_root` moved.
    assert sorted(descriptor.model_dump(mode="json")) == [
        "git_oid",
        "parent_generation_root",
        "semantic_root",
        "tag",
    ]
    assert descriptor.tag == "playbill-gen-v1"


def test_sroot_v2_vectors_match_golden() -> None:
    golden = json.loads(SROOT_GOLDEN.read_bytes())
    assert golden["format"] == "playbill-sroot-v2-golden-v1"
    names = set()
    for vector in golden["vectors"]:
        names.add(vector["name"])
        arguments = dict(vector["input"])
        arguments["approval_digests"] = tuple(arguments["approval_digests"])
        root = compute_semantic_root_v2(**cast(Any, arguments))
        assert root.tagged == vector["semantic_root"]
        assert (
            canonical_bytes(
                {
                    "tag": SEMANTIC_ROOT_V2_DOMAIN,
                    "manifest_root": arguments["manifest_root_value"],
                    "changeset_digest": arguments["changeset_digest_value"],
                    "approval_digests": list(arguments["approval_digests"]),
                    "parent_semantic_root": arguments["parent_semantic_root"],
                    "parent_derivation": arguments["parent_derivation"],
                }
            ).decode()
            == vector["preimage"]
        )
    assert names == {"succession_boundary_v1_parent", "steady_state_v2_parent", "no_approvals"}


# --------------------------------------------------------------------------
# playbill-dependency-graph-v3 in the closure evaluation
# --------------------------------------------------------------------------


def test_closure_v3_commits_to_the_candidate_edge_root(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    base_tree, proposed = _document_tree(instance)
    scope = (DOCUMENT_PATH,)

    v3 = evaluate_dependency_closure(parent_tree=base_tree, candidate_tree=proposed, scope=scope)
    assert isinstance(v3, ClosureEvaluationV3)
    assert v3.tag == "playbill-closure-evaluation-v3"
    assert (v3.verdict, v3.paths) == ("complete", scope)
    DependencyEdgeRoot.from_tagged(v3.dependency_edge_root)
    with pytest.raises(ValidationError):
        ClosureEvaluationV3.model_validate(
            {**v3.model_dump(mode="json"), "dependency_edge_root": "sha256:" + "ab" * 32}
        )


def test_closure_proof_v3_refuses_a_root_from_any_other_family() -> None:
    proof = _record_v3().closure_proof
    assert proof.strategy == "dependency-closure-v3"
    DependencyEdgeRoot.from_tagged(proof.dependency_edge_root)
    for wrong in ("sha256:" + "ab" * 32, "merkle-sha256:" + "ab" * 32):
        with pytest.raises(ValidationError, match="dependency_edge_root"):
            ClosureProof.model_validate(
                {**proof.model_dump(mode="json"), "dependency_edge_root": wrong}
            )


# --------------------------------------------------------------------------
# playbill-changeset-v3 and the inert gate
# --------------------------------------------------------------------------


def test_record_v3_round_trips_and_matches_golden() -> None:
    golden = json.loads(CHANGESET_GOLDEN.read_bytes())
    assert golden["format"] == "playbill-changeset-v3-golden-v1"
    content = golden["canonical_bytes"].encode()

    record = parse_change_set_record(content, path="changesets/cs-1.json")
    assert isinstance(record, ChangeSetRecord)
    assert render_change_set(record) == content
    assert record.changeset_digest == golden["changeset_digest"]
    assert change_set_digest(record).tagged == golden["recomputed_changeset_digest"]
    assert record.candidate_digest == golden["candidate_digest"]
    assert record.model_dump(mode="json") == golden["record"]

    # The embedded versions are exactly the two that moved.
    assert isinstance(record.candidate, SemanticCandidate)
    assert record.closure_proof.tag == "playbill-closure-proof-v3"
    assert record.members[0].tag == "playbill-candidate-member-law-evidence-v2"
    assert record.law_evidence[0].tag == "playbill-member-law-evaluation-v2"


def test_record_v3_closes_its_candidate_correspondence() -> None:
    record = _record_v3()
    payload = record.model_dump(mode="json")
    with pytest.raises(ValidationError, match="self digest does not reproduce"):
        ChangeSetRecord.model_validate({**payload, "changeset_digest": "sha256:" + "ff" * 32})
    with pytest.raises(ValidationError):
        ChangeSetRecord.model_validate({**payload, "candidate_digest": "sha256:" + "ff" * 32})
    with pytest.raises(ValidationError):
        ChangeSetRecord.model_validate({**payload, "law_digests": {}})

    candidate_record = _candidate_record_v3()
    assert render_candidate_record(candidate_record).endswith(b"\n")
    with pytest.raises(ValidationError, match="v3 closure member-evidence digest"):
        CandidateRecord.model_validate(
            {
                **candidate_record.model_dump(mode="json"),
                "closure_proof": {
                    **candidate_record.closure_proof.model_dump(mode="json"),
                    "member_evidence_digest": "sha256:" + "ff" * 32,
                },
            }
        )


def test_a_v3_receipt_is_recognized_by_shared_parsing() -> None:
    content = render_change_set(_record_v3())
    recognized = parse_change_set_record(content, path="changesets/cs-1.json")
    assert isinstance(recognized, ChangeSetRecord)
    assert recognized.tag == "playbill-changeset-v3"


def test_a_new_proposal_produces_the_whole_succession_and_nothing_older(
    tmp_path: Path,
) -> None:
    """One evaluation, one wire version: the three commitments move together.

    The candidate's merkle manifest root, the closure proof's edge root, and the
    receipt that carries both are one succession, so a build that produced any
    two of them without the third would leave a receipt whose own evidence it
    could not verify. This is the test that they arrive together.
    """

    instance, _owner = initialize_local(tmp_path)
    base_tree, proposed = _document_tree(instance)
    evaluation = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=base_tree,
        proposed_tree=proposed,
        current=instance.accepted_coordinate(),
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
    )
    candidate = evaluation.candidate
    assert isinstance(candidate, CandidateRecord)
    assert candidate.tag == "playbill-validated-candidate-v3"
    assert isinstance(candidate.candidate, SemanticCandidate)
    SemanticMerkleRoot.from_tagged(candidate.candidate.candidate_manifest_root)
    assert candidate.closure_proof.tag == "playbill-closure-proof-v3"
    DependencyEdgeRoot.from_tagged(candidate.closure_proof.dependency_edge_root)

    record = build_change_set_record(
        candidate,
        sequence=1,
        approvals=(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
    )
    assert isinstance(record, ChangeSetRecord)
    content = render_change_set(record)
    assert parse_change_set_record(content, path="changesets/cs-1.json") == record

    # A single-member Document change is an ordinary change set now: it is judged
    # by the Document law through the one evaluator, not by a separate one.
    assert len(candidate.members) == 1
    assert candidate.members[0].artifact_kind == "document"
    assert candidate.closure_proof.paths == candidate.candidate.scope


def test_the_manifest_root_a_new_candidate_signs_is_the_trie_over_its_own_members(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    base_tree, proposed = _document_tree(instance)
    evaluation = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=base_tree,
        proposed_tree=proposed,
        current=instance.accepted_coordinate(),
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
    )
    candidate = evaluation.candidate
    assert isinstance(candidate, CandidateRecord)
    assert candidate.candidate.candidate_manifest_root == (
        merkle_manifest_root(semantic_projection(proposed)).tagged
    )
    # And it is not the flat root, which no longer parses in that field at all.
    with pytest.raises(ValueError):
        SemanticMerkleRoot.from_tagged(manifest_root(semantic_projection(proposed)).tagged)


def test_the_v3_edge_root_reproduces_from_the_receipt_it_travels_in() -> None:
    record = _record_v3()
    edges = record.members[0].dependency_proof_refs
    assert dependency_edge_root(edges).tagged == record.closure_proof.dependency_edge_root


def test_flat_and_merkle_manifest_roots_stay_mutually_unparseable() -> None:
    with pytest.raises(ValueError):
        SemanticManifestRoot.from_tagged(MERKLE_ROOT)
    with pytest.raises(ValueError):
        SemanticMerkleRoot.from_tagged(FLAT_ROOT)
    with pytest.raises(ValueError):
        DependencyEdgeRoot.from_tagged(FLAT_ROOT)
