"""Deterministic merkle manifest laws: structure, incrementality, and tag disjointness."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import (
    CanonicalEncodingError,
    SemanticManifestRoot,
    SemanticMerkleRoot,
    canonical_bytes,
    manifest_for_tree,
    manifest_root,
)
from cruxible_client.contracts.errors import MerkleIntegrityError
from cruxible_client.contracts.merkle import (
    EMPTY_MERKLE_ROOT,
    MERKLE_LEAF_DOMAIN,
    MERKLE_NODE_DOMAIN,
    MERKLE_ROOT_DOMAIN,
    ROOT_PREFIX,
    MerkleNode,
    build_merkle_manifest,
    merkle_manifest_root,
    update_merkle_manifest,
    verify_merkle_manifest,
    verify_merkle_nodes,
)

GOLDEN = Path(__file__).parents[1] / "goldens" / "playbill" / "merkle-manifest-v1.json"

# Every randomized law below is driven from this one constant. Replay determinism
# is the property under test, so the generator must never read a clock.
PROPERTY_SEED = 20260818

MEMBERS: Mapping[str, str] = {
    "documents/playbill-design.json": "11" * 32,
    "documents/nested/deep/leaf.json": "22" * 32,
    "principals/daemon.json": "33" * 32,
    "subjects/alpha.json": "44" * 32,
}


def _digest(domain: str, payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_bytes({"tag": domain, **payload})).hexdigest()


def test_empty_manifest_has_an_explicit_defined_root() -> None:
    manifest = build_merkle_manifest({})
    assert set(manifest.nodes) == {ROOT_PREFIX}
    assert manifest.nodes[ROOT_PREFIX].segments == ()
    assert manifest.root == EMPTY_MERKLE_ROOT
    node = _digest(MERKLE_NODE_DOMAIN, {"children": []})
    assert manifest.root == SemanticMerkleRoot(_digest(MERKLE_ROOT_DOMAIN, {"node": node}))
    verify_merkle_nodes(manifest.nodes, claimed_root=manifest.root.tagged)


def test_node_preimages_are_domain_separated_by_leaf_interior_and_root() -> None:
    manifest = build_merkle_manifest({"a/b.yaml": "55" * 32})
    leaf = manifest.node("a/b.yaml")
    assert leaf.is_leaf
    assert leaf.digest.value == _digest(
        MERKLE_LEAF_DOMAIN, {"path": "a/b.yaml", "member_digest": "55" * 32}
    )
    directory = manifest.node("a")
    assert not directory.is_leaf
    assert directory.digest.value == _digest(
        MERKLE_NODE_DOMAIN, {"children": [["b.yaml", leaf.digest.value]]}
    )
    root_node = manifest.node(ROOT_PREFIX)
    assert root_node.digest.value == _digest(
        MERKLE_NODE_DOMAIN, {"children": [["a", directory.digest.value]]}
    )
    assert manifest.root.value == _digest(MERKLE_ROOT_DOMAIN, {"node": root_node.digest.value})
    # The three domains are genuinely distinct, not the same bytes under a label.
    assert len({leaf.digest.value, directory.digest.value, manifest.root.value}) == 3


def test_independent_builds_agree_on_root_and_every_node() -> None:
    shuffled = dict(sorted(MEMBERS.items(), reverse=True))
    left = build_merkle_manifest(MEMBERS)
    right = build_merkle_manifest(shuffled)
    assert left.root == right.root
    assert left.nodes == right.nodes
    assert left.members() == dict(sorted(MEMBERS.items()))


def test_merkle_and_flat_roots_read_the_same_tree_from_the_same_members() -> None:
    tree = {path: f"content for {path}".encode() for path in MEMBERS}
    assert merkle_manifest_root(tree) == build_merkle_manifest(manifest_for_tree(tree)).root


def test_merkle_and_flat_root_spellings_can_never_be_confused() -> None:
    tree = {path: f"content for {path}".encode() for path in MEMBERS}
    flat = manifest_root(tree)
    merkle = merkle_manifest_root(tree)
    assert flat.tagged.startswith("sha256:")
    assert merkle.tagged.startswith("merkle-sha256:")
    assert flat.value != merkle.value
    with pytest.raises(ValueError):
        SemanticMerkleRoot.from_tagged(flat.tagged)
    with pytest.raises(ValueError):
        SemanticManifestRoot.from_tagged(merkle.tagged)
    # Even a hypothetical hex collision cannot cross: the prefixes disagree.
    assert SemanticMerkleRoot(flat.value).tagged != flat.tagged
    with pytest.raises(MerkleIntegrityError):
        verify_merkle_manifest(
            manifest_for_tree(tree), claimed_root=SemanticMerkleRoot(flat.value).tagged
        )
    with pytest.raises(ValueError):
        verify_merkle_manifest(manifest_for_tree(tree), claimed_root=flat.tagged)


def test_verification_accepts_the_true_root_and_refuses_a_perturbed_leaf() -> None:
    members = dict(MEMBERS)
    claimed = build_merkle_manifest(members).root.tagged
    manifest = verify_merkle_manifest(members, claimed_root=claimed)
    verify_merkle_nodes(manifest.nodes, claimed_root=manifest.root.tagged)
    for path in members:
        perturbed = {**members, path: "ff" * 32}
        assert build_merkle_manifest(perturbed).root != manifest.root
        with pytest.raises(MerkleIntegrityError):
            verify_merkle_manifest(perturbed, claimed_root=manifest.root.tagged)


def test_tampering_with_any_interior_node_is_caught_by_node_verification() -> None:
    manifest = build_merkle_manifest(MEMBERS)
    for prefix, node in manifest.nodes.items():
        tampered = dict(manifest.nodes)
        # A well-formed digest that simply is not the one the node derives.
        tampered[prefix] = MerkleNode(
            prefix=node.prefix,
            digest=type(node.digest)("ab" * 32),
            member_digest=node.member_digest,
            segments=node.segments,
        )
        with pytest.raises(MerkleIntegrityError):
            verify_merkle_nodes(tampered, claimed_root=manifest.root.tagged)


def test_node_verification_refuses_unreachable_and_misfiled_nodes() -> None:
    manifest = build_merkle_manifest(MEMBERS)
    orphaned = {**manifest.nodes, "orphan/node.yaml": manifest.node("subjects/alpha.json")}
    with pytest.raises(MerkleIntegrityError):
        verify_merkle_nodes(orphaned, claimed_root=manifest.root.tagged)
    truncated = {
        prefix: node for prefix, node in manifest.nodes.items() if prefix != "documents/nested"
    }
    with pytest.raises(MerkleIntegrityError):
        verify_merkle_nodes(truncated, claimed_root=manifest.root.tagged)


def test_incremental_update_reuses_every_untouched_node_object() -> None:
    manifest = build_merkle_manifest(MEMBERS)
    updated = update_merkle_manifest(manifest, updated={"subjects/alpha.json": "99" * 32})

    changed_paths = {ROOT_PREFIX, "subjects", "subjects/alpha.json"}
    recomputed = {
        prefix
        for prefix, node in updated.nodes.items()
        if manifest.nodes.get(prefix) is not node  # identity, not equality
    }
    assert recomputed == changed_paths
    for prefix in set(manifest.nodes) - changed_paths:
        assert updated.nodes[prefix] is manifest.nodes[prefix]
    assert updated.root == build_merkle_manifest({**MEMBERS, "subjects/alpha.json": "99" * 32}).root


def test_removal_prunes_emptied_directories_and_leaves_siblings_untouched() -> None:
    manifest = build_merkle_manifest(MEMBERS)
    pruned = update_merkle_manifest(manifest, removed=["documents/nested/deep/leaf.json"])
    assert "documents/nested" not in pruned.nodes
    assert "documents/nested/deep" not in pruned.nodes
    assert (
        pruned.nodes["documents/playbill-design.json"]
        is (manifest.nodes["documents/playbill-design.json"])
    )
    assert pruned.nodes["principals"] is manifest.nodes["principals"]
    expected = {k: v for k, v in MEMBERS.items() if k != "documents/nested/deep/leaf.json"}
    assert pruned.root == build_merkle_manifest(expected).root
    assert pruned.nodes == build_merkle_manifest(expected).nodes


def test_removing_every_member_returns_the_defined_empty_root() -> None:
    manifest = build_merkle_manifest(MEMBERS)
    emptied = update_merkle_manifest(manifest, removed=list(MEMBERS))
    assert emptied.root == EMPTY_MERKLE_ROOT
    assert set(emptied.nodes) == {ROOT_PREFIX}


def test_empty_change_set_is_a_no_op() -> None:
    manifest = build_merkle_manifest(MEMBERS)
    unchanged = update_merkle_manifest(manifest)
    assert unchanged.root == manifest.root
    assert unchanged.nodes == manifest.nodes


def _random_path(rng: random.Random) -> str:
    directories = ("documents", "subjects", "principals", "documents/nested", "claims/a/b")
    return f"{rng.choice(directories)}/m{rng.randrange(12):02d}.json"


def _random_member(rng: random.Random) -> str:
    return f"{rng.randrange(1 << 64):016x}" * 4


def test_incremental_updates_match_from_scratch_builds_over_a_seeded_walk() -> None:
    rng = random.Random(PROPERTY_SEED)
    members: dict[str, str] = {}
    manifest = build_merkle_manifest(members)
    saw_prune = False
    for _ in range(400):
        updates: dict[str, str] = {}
        removals: set[str] = set()
        for _ in range(rng.randrange(1, 4)):
            if members and rng.random() < 0.4:
                path = rng.choice(sorted(members))
                if path in updates:
                    continue
                removals.add(path)
            else:
                path = _random_path(rng)
                if path in removals:
                    continue
                updates[path] = _random_member(rng)
        before = set(manifest.nodes)
        manifest = update_merkle_manifest(manifest, updated=updates, removed=sorted(removals))
        saw_prune = saw_prune or bool(before - set(manifest.nodes) - removals)
        for path in removals:
            members.pop(path)
        members.update(updates)
        rebuilt = build_merkle_manifest(members)
        assert manifest.root == rebuilt.root
        assert manifest.nodes == rebuilt.nodes
        assert manifest.members() == dict(sorted(members.items()))
        verify_merkle_nodes(manifest.nodes, claimed_root=manifest.root.tagged)
    assert saw_prune, "the seeded walk must exercise subtree-emptying removals"
    assert members, "the seeded walk must end with a nonempty manifest"


def test_seeded_walk_is_reproducible_across_independent_runs() -> None:
    def walk() -> str:
        rng = random.Random(PROPERTY_SEED)
        manifest = build_merkle_manifest({})
        for _ in range(50):
            manifest = update_merkle_manifest(
                manifest, updated={_random_path(rng): _random_member(rng)}
            )
        return manifest.root.tagged

    assert walk() == walk()


def test_structural_conflicts_and_malformed_members_are_refused() -> None:
    with pytest.raises(CanonicalEncodingError):
        build_merkle_manifest({"a": "11" * 32, "a/b.yaml": "22" * 32})
    with pytest.raises(CanonicalEncodingError):
        build_merkle_manifest({"a/b.yaml": "not-a-digest"})
    with pytest.raises(CanonicalEncodingError):
        build_merkle_manifest({"/absolute.yaml": "11" * 32})
    with pytest.raises(CanonicalEncodingError):
        build_merkle_manifest({"documents/a.json": "11" * 32, "documents/A.json": "22" * 32})

    manifest = build_merkle_manifest(MEMBERS)
    with pytest.raises(CanonicalEncodingError):
        update_merkle_manifest(manifest, updated={"documents": "11" * 32})
    with pytest.raises(CanonicalEncodingError):
        update_merkle_manifest(manifest, updated={"documents/playbill-design.json/x": "11" * 32})
    with pytest.raises(CanonicalEncodingError):
        update_merkle_manifest(manifest, removed=["documents"])
    with pytest.raises(CanonicalEncodingError):
        update_merkle_manifest(manifest, removed=["documents/absent.json"])
    with pytest.raises(CanonicalEncodingError):
        update_merkle_manifest(
            manifest, updated={"subjects/beta.json": "11" * 32}, removed=["subjects/beta.json"]
        )
    with pytest.raises(CanonicalEncodingError):
        update_merkle_manifest(manifest, updated={"documents/Nested/x.json": "11" * 32})
    with pytest.raises(CanonicalEncodingError):
        update_merkle_manifest(manifest, updated={"documents/Playbill-Design.json": "11" * 32})


def test_merkle_manifest_has_a_frozen_end_to_end_golden() -> None:
    golden = json.loads(GOLDEN.read_bytes())
    assert golden["format"] == "playbill-merkle-manifest-golden-v1"

    members = golden["input"]["members"]
    manifest = build_merkle_manifest(members)
    expected = golden["expected"]
    assert manifest.root.tagged == expected["root"]
    assert EMPTY_MERKLE_ROOT.tagged == expected["empty_root"]
    assert {prefix: node.digest.tagged for prefix, node in manifest.nodes.items()} == expected[
        "nodes"
    ]
    assert (
        canonical_bytes(
            {
                "tag": MERKLE_LEAF_DOMAIN,
                "member_digest": members["documents/playbill-design.json"],
                "path": "documents/playbill-design.json",
            }
        ).decode()
        == expected["leaf_preimage"]
    )
    assert (
        canonical_bytes(
            {"tag": MERKLE_ROOT_DOMAIN, "node": manifest.node(ROOT_PREFIX).digest.value}
        ).decode()
        == expected["root_preimage"]
    )

    incremental = update_merkle_manifest(
        build_merkle_manifest(golden["input"]["parent_members"]),
        updated=golden["input"]["change_set"]["updated"],
        removed=golden["input"]["change_set"]["removed"],
    )
    assert incremental.root.tagged == expected["root"]
    verify_merkle_nodes(incremental.nodes, claimed_root=expected["root"])
