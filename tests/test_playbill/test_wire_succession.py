"""The dark half of the coordinated wire succession: formats, verifiers, inert gate.

`playbill-candidate-v2`, `playbill-sroot-v2`, `playbill-dependency-graph-v3`, and
`playbill-changeset-v3` land here with their verifiers and goldens and nothing
else. No producer emits them, and every path that could act on one refuses it.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.candidates import (
    CandidateRecordV2,
    CandidateRecordV3,
    ClosureProofV3,
    SemanticCandidate,
    SemanticCandidateV2,
    candidate_digest,
    render_candidate_record,
)
from cruxible_core.playbill.canonical import (
    DependencyEdgeRoot,
    SemanticManifestRoot,
    SemanticMerkleRoot,
    canonical_bytes,
)
from cruxible_core.playbill.checkpoints import _prefix_records
from cruxible_core.playbill.closure import (
    ClosureEvaluationV3,
    dependency_edge_root,
    evaluate_dependency_closure,
    evaluate_dependency_closure_v3,
)
from cruxible_core.playbill.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_core.playbill.errors import (
    SettlementIntegrityError,
    UnproducibleWireVersionError,
)
from cruxible_core.playbill.projection_artifacts import parse_projection_tree
from cruxible_core.playbill.projection_extensions import playbill_runtime_extension_registry
from cruxible_core.playbill.proposals import evaluate_proposal_tree
from cruxible_core.playbill.settlement import (
    SEMANTIC_ROOT_V2_DOMAIN,
    ChangeActorBinding,
    ChangeSetRecordV3,
    build_change_set_record,
    change_set_digest,
    compute_semantic_root,
    compute_semantic_root_v2,
    parse_change_set_record,
    parse_producible_change_set_record,
    prepare_generation,
    render_change_set,
    require_producible_candidate,
    require_producible_change_set,
)
from cruxible_core.playbill.types import GenerationDescriptor
from tests.test_playbill._support import initialize_local

DOCUMENT_PATH = "documents/playbill-design.yaml"
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
        title="Playbill design",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
            approval_roles=("owner", "reviewer"),
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    return base_tree, {**base_tree, DOCUMENT_PATH: render_document(shell)}


def _candidate_v2(**overrides: object) -> SemanticCandidateV2:
    values: dict[str, object] = {
        "parent_semantic_root": PARENT_ROOT,
        "candidate_manifest_root": MERKLE_ROOT,
        "semantic_diff_digest": DIFF_DIGEST,
        "scope": ("claims/alpha.json",),
        "timestamp": TIMESTAMP,
    }
    return SemanticCandidateV2.model_validate({**values, **overrides})


def _record_v3() -> ChangeSetRecordV3:
    golden = json.loads(CHANGESET_GOLDEN.read_bytes())
    record = parse_change_set_record(golden["canonical_bytes"].encode(), path="changesets/x.json")
    assert isinstance(record, ChangeSetRecordV3)
    return record


def _candidate_record_v3() -> CandidateRecordV3:
    record = _record_v3()
    return CandidateRecordV3(
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
        SemanticCandidate.model_validate(
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
        SemanticCandidate(
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
        SemanticCandidateV2.model_validate(
            {**_candidate_v2().model_dump(mode="json"), "base_oid": "0" * 40}
        )


def test_the_two_candidate_versions_never_share_a_digest_domain() -> None:
    v2 = _candidate_v2()
    v1 = SemanticCandidate(
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
    candidate = SemanticCandidateV2.model_validate(golden["candidate"])
    payload = candidate.model_dump(mode="json")
    payload.pop("tag")
    assert (
        canonical_bytes({"tag": "playbill-candidate-v2", **payload}).decode()
        == golden["canonical_preimage"]
    )
    assert candidate_digest(candidate).tagged == golden["candidate_digest"]

    sibling = SemanticCandidate.model_validate(golden["flat_rooted_v1_sibling"]["candidate"])
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


def test_sroot_v1_and_v2_differ_on_the_same_underlying_values() -> None:
    v1 = compute_semantic_root(
        manifest_root_value=FLAT_ROOT,
        changeset_digest_value=CHANGESET_DIGEST,
        approval_digests=APPROVALS,
        parent_semantic_root=PARENT_ROOT,
    )
    assert v1.tagged != _sroot_v2(parent_derivation="playbill-sroot-v1")
    assert v1.tagged != _sroot_v2()


def test_sroot_v2_requires_a_merkle_manifest_root_and_v1_requires_a_flat_one() -> None:
    with pytest.raises(ValueError):
        _sroot_v2(manifest_root_value=FLAT_ROOT)
    with pytest.raises(ValueError):
        compute_semantic_root(
            manifest_root_value=MERKLE_ROOT,
            changeset_digest_value=CHANGESET_DIGEST,
            approval_digests=APPROVALS,
            parent_semantic_root=PARENT_ROOT,
        )


def test_the_succession_chain_rule_distinguishes_a_v1_parent_from_a_v2_parent() -> None:
    from_v1 = _sroot_v2(parent_derivation="playbill-sroot-v1")
    from_v2 = _sroot_v2(parent_derivation="playbill-sroot-v2")
    # The same 32-byte parent value, and yet two different children: a chain
    # cannot be re-narrated across the succession boundary after the fact.
    assert from_v1 != from_v2


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

    v1 = compute_semantic_root(
        manifest_root_value=FLAT_ROOT,
        changeset_digest_value=CHANGESET_DIGEST,
        approval_digests=APPROVALS,
        parent_semantic_root=PARENT_ROOT,
    )
    assert v1.tagged == golden["v1_root_over_the_same_hex"]
    assert v1.tagged not in {vector["semantic_root"] for vector in golden["vectors"]}


# --------------------------------------------------------------------------
# playbill-dependency-graph-v3 in the closure evaluation
# --------------------------------------------------------------------------


def test_closure_v3_reads_the_same_closure_and_only_moves_the_commitment(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    base_tree, proposed = _document_tree(instance)
    scope = (DOCUMENT_PATH,)

    v2 = evaluate_dependency_closure(parent_tree=base_tree, candidate_tree=proposed, scope=scope)
    v3 = evaluate_dependency_closure_v3(parent_tree=base_tree, candidate_tree=proposed, scope=scope)
    assert isinstance(v3, ClosureEvaluationV3)
    assert v3.tag == "playbill-closure-evaluation-v3"
    assert (v3.verdict, v3.paths) == (v2.verdict, v2.paths)
    assert v3.member_dependency_proofs == v2.member_dependency_proofs
    assert v3.missing_dependents == v2.missing_dependents
    assert v3.unresolved_pins == v2.unresolved_pins
    DependencyEdgeRoot.from_tagged(v3.dependency_edge_root)
    with pytest.raises(ValidationError):
        ClosureEvaluationV3.model_validate(
            {**v3.model_dump(mode="json"), "dependency_edge_root": v2.dependency_graph_digest}
        )


def test_closure_proof_v3_refuses_a_root_from_any_other_family() -> None:
    proof = _record_v3().closure_proof
    assert proof.strategy == "dependency-closure-v3"
    DependencyEdgeRoot.from_tagged(proof.dependency_edge_root)
    for wrong in ("sha256:" + "ab" * 32, "merkle-sha256:" + "ab" * 32):
        with pytest.raises(ValidationError, match="dependency_edge_root"):
            ClosureProofV3.model_validate(
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
    assert isinstance(record, ChangeSetRecordV3)
    assert render_change_set(record) == content
    assert record.changeset_digest == golden["changeset_digest"]
    assert change_set_digest(record).tagged == golden["recomputed_changeset_digest"]
    assert record.candidate_digest == golden["candidate_digest"]
    assert record.model_dump(mode="json") == golden["record"]

    # The embedded versions are exactly the two that moved.
    assert isinstance(record.candidate, SemanticCandidateV2)
    assert record.closure_proof.tag == "playbill-closure-proof-v3"
    assert record.members[0].tag == "playbill-candidate-member-law-evidence-v2"
    assert record.law_evidence[0].tag == "playbill-member-law-evaluation-v2"


def test_record_v3_closes_the_same_correspondence_the_v2_record_closes() -> None:
    record = _record_v3()
    payload = record.model_dump(mode="json")
    with pytest.raises(ValidationError, match="self digest does not reproduce"):
        ChangeSetRecordV3.model_validate({**payload, "changeset_digest": "sha256:" + "ff" * 32})
    with pytest.raises(ValidationError):
        ChangeSetRecordV3.model_validate({**payload, "candidate_digest": "sha256:" + "ff" * 32})
    with pytest.raises(ValidationError):
        ChangeSetRecordV3.model_validate({**payload, "law_digests": {}})

    candidate_record = _candidate_record_v3()
    assert render_candidate_record(candidate_record).endswith(b"\n")
    with pytest.raises(ValidationError, match="v3 closure member-evidence digest"):
        CandidateRecordV3.model_validate(
            {
                **candidate_record.model_dump(mode="json"),
                "closure_proof": {
                    **candidate_record.closure_proof.model_dump(mode="json"),
                    "member_evidence_digest": "sha256:" + "ff" * 32,
                },
            }
        )
    # The v2 record still refuses a v2 candidate carrying a v3 closure proof.
    with pytest.raises(ValidationError):
        CandidateRecordV2.model_validate(candidate_record.model_dump(mode="json"))


def test_a_v3_receipt_is_recognized_by_shared_parsing() -> None:
    content = render_change_set(_record_v3())
    recognized = parse_change_set_record(content, path="changesets/cs-1.json")
    assert isinstance(recognized, ChangeSetRecordV3)
    assert recognized.tag == "playbill-changeset-v3"


def test_the_inert_gate_refuses_a_v3_receipt_at_every_entry(tmp_path: Path) -> None:
    record = _record_v3()
    content = render_change_set(record)
    path = f"changesets/cs-{record.sequence:020d}.json"

    # 1. the shared parse-and-gate seam used by replay, checkpoints, projection
    with pytest.raises(UnproducibleWireVersionError, match="playbill-changeset-v3"):
        parse_producible_change_set_record(content, path=path, operation="replay")

    # 2. checkpoint prefix re-derivation
    with pytest.raises(UnproducibleWireVersionError):
        _prefix_records({path: content}, sequence=record.sequence)

    # 3. accepted projection, whose format-error translation must not swallow it
    with pytest.raises(UnproducibleWireVersionError):
        parse_projection_tree({path: content}, registry=playbill_runtime_extension_registry())

    # 4. change-set production
    candidate = _candidate_record_v3()
    with pytest.raises(UnproducibleWireVersionError, match="playbill-validated-candidate-v3"):
        build_change_set_record(
            candidate,
            sequence=1,
            approvals=(),
            actor_binding=ChangeActorBinding(actor_id="owner"),
        )

    # 5. settlement, before the ledger is read, signed, or written
    with pytest.raises(UnproducibleWireVersionError, match="settlement"):
        prepare_generation(
            cast(Any, None),
            base=cast(Any, None),
            candidate_tree={},
            candidate=candidate,
            approval_submissions=(),
            bodies=cast(Any, None),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            sequence=1,
        )


def test_the_gate_passes_the_versions_this_build_does_produce(tmp_path: Path) -> None:
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
    assert candidate is not None
    assert require_producible_candidate(candidate, operation="settlement") is candidate

    record = build_change_set_record(
        candidate,
        sequence=1,
        approvals=(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
    )
    assert require_producible_change_set(record, operation="replay") is record
    content = render_change_set(record)
    path = f"changesets/cs-{record.sequence:020d}.json"
    assert parse_producible_change_set_record(content, path=path, operation="replay") == record


# The three format modules define these versions and so construct them; nothing
# else may, which is what "dark" means for this slice.
_FORMAT_MODULES = {"candidates.py", "closure.py", "settlement.py"}
_DARK_CALLABLES = {
    "SemanticCandidateV2",
    "CandidateRecordV3",
    "ChangeSetRecordV3",
    "ClosureProofV3",
    "ClosureEvaluationV3",
    "compute_semantic_root_v2",
    "evaluate_dependency_closure_v3",
}


def test_nothing_outside_the_format_modules_produces_a_dark_version() -> None:
    source = Path(__file__).parents[2] / "src" / "cruxible_core"
    offenders: dict[str, set[str]] = {}
    for path in sorted(source.rglob("*.py")):
        if path.name in _FORMAT_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        } | {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        dark = called & _DARK_CALLABLES
        if dark:
            offenders[str(path)] = dark
    assert offenders == {}, offenders


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
