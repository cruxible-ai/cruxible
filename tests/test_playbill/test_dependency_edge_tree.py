"""`playbill-dependency-graph-v3`: one edge trie, its own domains, no full rebuilds."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from cruxible_client.contracts.candidates import DependencyProofReferenceV1
from cruxible_client.contracts.canonical import (
    DependencyEdgeRoot,
    SemanticManifestRoot,
    SemanticMerkleRoot,
    canonical_bytes,
)
from cruxible_client.contracts.errors import CanonicalEncodingError, MerkleIntegrityError
from cruxible_client.contracts.merkle import (
    DEPENDENCY_EDGE_DOMAINS,
    DEPGRAPH_LEAF_DOMAIN,
    DEPGRAPH_NODE_DOMAIN,
    DEPGRAPH_ROOT_DOMAIN,
    EMPTY_DEPENDENCY_EDGE_ROOT,
    EMPTY_MERKLE_ROOT,
    MANIFEST_MERKLE_DOMAINS,
    ROOT_PREFIX,
    MerkleNode,
    build_merkle_tree,
    verify_merkle_tree_nodes,
)
from cruxible_core.playbill.closure import (
    DEPENDENCY_EDGE_SET_DOMAIN,
    build_dependency_edge_tree,
    dependency_edge_members,
    dependency_edge_root,
    update_dependency_edge_tree,
    verify_dependency_edge_root,
)

GOLDEN = Path(__file__).parents[1] / "goldens" / "playbill" / "depgraph-v3.json"

# Every randomized law below is driven from this one constant. Replay
# determinism is the property under test, so the generator never reads a clock.
PROPERTY_SEED = 20260819


def _digest(domain: str, payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_bytes({"tag": domain, **payload})).hexdigest()


def _edge(
    source_path: str,
    target_path: str,
    *,
    role: str = "subject",
    source: str = "aa",
    target: str = "bb",
) -> DependencyProofReferenceV1:
    return DependencyProofReferenceV1(
        source_path=source_path,
        source_artifact_digest="sha256:" + source * 32,
        target_path=target_path,
        target_artifact_digest="sha256:" + target * 32,
        pin_role=role,
    )


EDGES: tuple[DependencyProofReferenceV1, ...] = (
    _edge("claims/alpha.json", "subjects/alpha.yaml"),
    _edge("claims/alpha.json", "claim-types/measure.yaml", role="claim-type", target="cc"),
    _edge("claims/nested/deep/beta.json", "subjects/alpha.yaml", source="dd"),
    _edge("documents/design.yaml", "subjects/alpha.yaml", role="referent", source="ee"),
)


def test_empty_edge_set_has_an_explicit_defined_root() -> None:
    tree = build_dependency_edge_tree(())
    assert set(tree.nodes) == {ROOT_PREFIX}
    assert tree.root == EMPTY_DEPENDENCY_EDGE_ROOT
    assert tree.root.tagged.startswith("depgraph-sha256:")
    node = _digest(DEPGRAPH_NODE_DOMAIN, {"children": []})
    assert tree.root == DependencyEdgeRoot(_digest(DEPGRAPH_ROOT_DOMAIN, {"node": node}))
    # An empty edge set and an empty manifest are both "nothing", and they still
    # commit to different values: the domains, not the contents, separate them.
    assert tree.root.value != EMPTY_MERKLE_ROOT.value


def test_member_leaf_node_and_root_preimages_are_each_domain_separated() -> None:
    edges = tuple(edge for edge in EDGES if edge.source_path == "claims/alpha.json")
    tree = build_dependency_edge_tree(edges)
    entries = [
        edge.model_dump(mode="json")
        for edge in sorted(edges, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
    ]
    member = _digest(DEPENDENCY_EDGE_SET_DOMAIN, {"edges": entries})
    assert dependency_edge_members(edges) == {"claims/alpha.json": member}

    leaf = tree.node("claims/alpha.json")
    assert leaf.is_leaf
    assert leaf.digest.value == _digest(
        DEPGRAPH_LEAF_DOMAIN, {"path": "claims/alpha.json", "member_digest": member}
    )
    directory = tree.node("claims")
    assert directory.digest.value == _digest(
        DEPGRAPH_NODE_DOMAIN, {"children": [["alpha.json", leaf.digest.value]]}
    )
    root_node = tree.node(ROOT_PREFIX)
    assert tree.root.value == _digest(DEPGRAPH_ROOT_DOMAIN, {"node": root_node.digest.value})
    assert len({member, leaf.digest.value, directory.digest.value, tree.root.value}) == 4


def test_the_two_domain_families_never_agree_on_the_same_members() -> None:
    members = dependency_edge_members(EDGES)
    edge_tree = build_merkle_tree(members, domains=DEPENDENCY_EDGE_DOMAINS)
    manifest_tree = build_merkle_tree(members, domains=MANIFEST_MERKLE_DOMAINS)

    assert edge_tree.members() == manifest_tree.members()
    assert set(edge_tree.nodes) == set(manifest_tree.nodes)
    for prefix in edge_tree.nodes:
        assert edge_tree.nodes[prefix].digest != manifest_tree.nodes[prefix].digest
    assert edge_tree.root.value != manifest_tree.root.value


def test_a_dependency_edge_root_can_never_be_read_as_a_manifest_root() -> None:
    edge_root = dependency_edge_root(EDGES)
    merkle_root = build_merkle_tree(
        dependency_edge_members(EDGES), domains=MANIFEST_MERKLE_DOMAINS
    ).root

    assert edge_root.tagged.startswith("depgraph-sha256:")
    assert merkle_root.tagged.startswith("merkle-sha256:")
    for wrong in (SemanticMerkleRoot, SemanticManifestRoot):
        with pytest.raises(ValueError):
            wrong.from_tagged(edge_root.tagged)
    with pytest.raises(ValueError):
        DependencyEdgeRoot.from_tagged(merkle_root.tagged)
    with pytest.raises(ValueError):
        DependencyEdgeRoot.from_tagged("sha256:" + edge_root.value)
    # Even a hypothetical hex collision cannot cross: the prefixes disagree.
    assert DependencyEdgeRoot(merkle_root.value).tagged != merkle_root.tagged
    with pytest.raises(ValueError):
        verify_dependency_edge_root(EDGES, claimed_root=merkle_root.tagged)


def test_only_members_with_outgoing_edges_become_leaves() -> None:
    tree = build_dependency_edge_tree(EDGES)
    assert set(tree.members()) == {
        "claims/alpha.json",
        "claims/nested/deep/beta.json",
        "documents/design.yaml",
    }
    # Pure targets are named by every edge that reaches them and still cost the
    # tree nothing: the edge set is fully described by its sources.
    assert "subjects/alpha.yaml" not in tree.nodes


def test_grouping_is_canonical_regardless_of_input_order() -> None:
    shuffled = tuple(reversed(EDGES))
    assert dependency_edge_members(shuffled) == dependency_edge_members(EDGES)
    left = build_dependency_edge_tree(EDGES)
    right = build_dependency_edge_tree(shuffled)
    assert left.root == right.root
    assert left.nodes == right.nodes


def test_duplicate_edges_are_preserved_exactly_as_the_flat_edge_list_kept_them() -> None:
    duplicated = (*EDGES, EDGES[0])
    assert dependency_edge_members(duplicated) != dependency_edge_members(EDGES)


def test_incremental_update_reuses_every_untouched_node_object() -> None:
    tree = build_dependency_edge_tree(EDGES)
    replacement = (_edge("claims/alpha.json", "providers/feed.yaml", role="provider", target="f1"),)
    updated = update_dependency_edge_tree(tree, updated={"claims/alpha.json": replacement})

    changed = {ROOT_PREFIX, "claims", "claims/alpha.json"}
    recomputed = {
        prefix
        for prefix, node in updated.nodes.items()
        if tree.nodes.get(prefix) is not node  # identity, not equality
    }
    assert recomputed == changed
    for prefix in set(tree.nodes) - changed:
        assert updated.nodes[prefix] is tree.nodes[prefix]

    expected = tuple(edge for edge in EDGES if edge.source_path != "claims/alpha.json")
    assert updated.root == build_dependency_edge_tree((*expected, *replacement)).root


def test_removal_prunes_emptied_directories_and_leaves_siblings_untouched() -> None:
    tree = build_dependency_edge_tree(EDGES)
    pruned = update_dependency_edge_tree(tree, removed=["claims/nested/deep/beta.json"])
    assert "claims/nested" not in pruned.nodes
    assert "claims/nested/deep" not in pruned.nodes
    assert pruned.nodes["documents"] is tree.nodes["documents"]

    expected = tuple(edge for edge in EDGES if edge.source_path != "claims/nested/deep/beta.json")
    assert pruned.root == build_dependency_edge_tree(expected).root
    assert pruned.nodes == build_dependency_edge_tree(expected).nodes


def test_a_member_losing_its_last_edge_loses_its_leaf() -> None:
    tree = build_dependency_edge_tree(EDGES)
    emptied = update_dependency_edge_tree(tree, updated={"documents/design.yaml": ()})
    assert "documents/design.yaml" not in emptied.nodes
    assert "documents" not in emptied.nodes
    expected = tuple(edge for edge in EDGES if edge.source_path != "documents/design.yaml")
    assert emptied.root == build_dependency_edge_tree(expected).root


def test_emptying_a_member_that_had_no_edges_is_the_no_op_it_describes() -> None:
    tree = build_dependency_edge_tree(EDGES)
    unchanged = update_dependency_edge_tree(tree, updated={"lines/daily.yaml": ()})
    assert unchanged.root == tree.root
    assert unchanged.nodes == tree.nodes
    # An explicit removal is still strict: it asserts the member was there.
    with pytest.raises(CanonicalEncodingError, match="non-member path"):
        update_dependency_edge_tree(tree, removed=["lines/daily.yaml"])


def test_removing_every_member_returns_the_defined_empty_root() -> None:
    tree = build_dependency_edge_tree(EDGES)
    emptied = update_dependency_edge_tree(tree, removed=sorted(tree.members()))
    assert emptied.root == EMPTY_DEPENDENCY_EDGE_ROOT
    assert set(emptied.nodes) == {ROOT_PREFIX}


def test_an_update_may_not_smuggle_edges_belonging_to_another_member() -> None:
    tree = build_dependency_edge_tree(EDGES)
    with pytest.raises(ValueError, match="outside"):
        update_dependency_edge_tree(
            tree,
            updated={"claims/alpha.json": (_edge("lines/daily.yaml", "procedures/scan.yaml"),)},
        )


def test_verification_accepts_the_true_root_and_refuses_a_perturbed_edge() -> None:
    tree = build_dependency_edge_tree(EDGES)
    verify_dependency_edge_root(EDGES, claimed_root=tree.root.tagged)
    for index in range(len(EDGES)):
        perturbed = list(EDGES)
        perturbed[index] = perturbed[index].model_copy(update={"pin_role": "tampered"})
        assert dependency_edge_root(tuple(perturbed)) != tree.root
        with pytest.raises(MerkleIntegrityError):
            verify_dependency_edge_root(tuple(perturbed), claimed_root=tree.root.tagged)
    with pytest.raises(MerkleIntegrityError):
        verify_dependency_edge_root((), claimed_root=tree.root.tagged)


def test_tampering_with_any_node_is_caught_by_node_verification() -> None:
    tree = build_dependency_edge_tree(EDGES)
    verify_merkle_tree_nodes(
        tree.nodes, claimed_root=tree.root.tagged, domains=DEPENDENCY_EDGE_DOMAINS
    )
    for prefix, node in tree.nodes.items():
        tampered = dict(tree.nodes)
        tampered[prefix] = MerkleNode(
            prefix=node.prefix,
            digest=type(node.digest)("ab" * 32),
            member_digest=node.member_digest,
            segments=node.segments,
        )
        with pytest.raises(MerkleIntegrityError):
            verify_merkle_tree_nodes(
                tampered, claimed_root=tree.root.tagged, domains=DEPENDENCY_EDGE_DOMAINS
            )


def test_edge_nodes_do_not_verify_under_the_manifest_domain_family() -> None:
    tree = build_dependency_edge_tree(EDGES)
    with pytest.raises(ValueError):
        verify_merkle_tree_nodes(
            tree.nodes, claimed_root=tree.root.tagged, domains=MANIFEST_MERKLE_DOMAINS
        )
    with pytest.raises(MerkleIntegrityError):
        verify_merkle_tree_nodes(
            tree.nodes,
            claimed_root=SemanticMerkleRoot(tree.root.value).tagged,
            domains=MANIFEST_MERKLE_DOMAINS,
        )


_SOURCES = (
    "claims/a.json",
    "claims/b.json",
    "claims/nested/deep/c.json",
    "documents/d.yaml",
    "lines/e.yaml",
)
_TARGETS = ("subjects/f.yaml", "claim-types/g.yaml", "providers/h.yaml", "procedures/i.yaml")
_ROLES = ("subject", "claim-type", "provider", "procedure", "referent")


def _random_digest(rng: random.Random) -> str:
    return "sha256:" + f"{rng.randrange(1 << 64):016x}" * 4


def _random_edges(rng: random.Random, source: str) -> tuple[DependencyProofReferenceV1, ...]:
    return tuple(
        DependencyProofReferenceV1(
            source_path=source,
            source_artifact_digest=_random_digest(rng),
            target_path=rng.choice(_TARGETS),
            target_artifact_digest=_random_digest(rng),
            pin_role=rng.choice(_ROLES),
        )
        for _ in range(rng.randrange(1, 4))
    )


def _flatten(
    by_source: Mapping[str, Sequence[DependencyProofReferenceV1]],
) -> tuple[DependencyProofReferenceV1, ...]:
    return tuple(edge for source in sorted(by_source) for edge in by_source[source])


def test_incremental_updates_match_from_scratch_builds_over_a_seeded_walk() -> None:
    rng = random.Random(PROPERTY_SEED)
    by_source: dict[str, tuple[DependencyProofReferenceV1, ...]] = {}
    tree = build_dependency_edge_tree(())
    saw_prune = False
    for _ in range(300):
        updates: dict[str, tuple[DependencyProofReferenceV1, ...]] = {}
        removals: list[str] = []
        for _ in range(rng.randrange(1, 4)):
            source = rng.choice(_SOURCES)
            if source in updates or source in removals:
                continue
            if by_source and source in by_source and rng.random() < 0.4:
                removals.append(source)
            else:
                # An empty edge set is a legitimate update and must behave
                # exactly like a removal, so the walk exercises both spellings.
                updates[source] = () if rng.random() < 0.1 else _random_edges(rng, source)
        before = set(tree.nodes)
        tree = update_dependency_edge_tree(tree, updated=updates, removed=sorted(removals))
        for source in removals:
            by_source.pop(source)
        for source, edges in updates.items():
            if edges:
                by_source[source] = edges
            else:
                by_source.pop(source, None)
        saw_prune = saw_prune or bool(before - set(tree.nodes) - set(removals))

        rebuilt = build_dependency_edge_tree(_flatten(by_source))
        assert tree.root == rebuilt.root
        assert tree.nodes == rebuilt.nodes
        assert set(tree.members()) == set(by_source)
        verify_merkle_tree_nodes(
            tree.nodes, claimed_root=tree.root.tagged, domains=DEPENDENCY_EDGE_DOMAINS
        )
    assert saw_prune, "the seeded walk must exercise subtree-emptying removals"
    assert by_source, "the seeded walk must end with a nonempty edge set"


def test_seeded_walk_is_reproducible_across_independent_runs() -> None:
    def walk() -> str:
        rng = random.Random(PROPERTY_SEED)
        tree = build_dependency_edge_tree(())
        for _ in range(50):
            source = rng.choice(_SOURCES)
            tree = update_dependency_edge_tree(tree, updated={source: _random_edges(rng, source)})
        return tree.root.tagged

    assert walk() == walk()


def test_dependency_edge_tree_has_a_frozen_end_to_end_golden() -> None:
    golden = json.loads(GOLDEN.read_bytes())
    assert golden["format"] == "playbill-depgraph-v3-golden-v1"
    expected = golden["expected"]

    parent_edges = tuple(
        DependencyProofReferenceV1.model_validate(item) for item in golden["input"]["parent_edges"]
    )
    final_edges = tuple(
        DependencyProofReferenceV1.model_validate(item) for item in golden["input"]["edges"]
    )
    rebuilt = build_dependency_edge_tree(final_edges)

    assert EMPTY_DEPENDENCY_EDGE_ROOT.tagged == expected["empty_root"]
    assert build_dependency_edge_tree(parent_edges).root.tagged == expected["parent_root"]
    assert rebuilt.root.tagged == expected["root"]
    assert dependency_edge_members(final_edges) == expected["members"]
    assert {prefix: node.digest.tagged for prefix, node in rebuilt.nodes.items()} == expected[
        "nodes"
    ]
    assert (
        canonical_bytes(
            {
                "tag": DEPGRAPH_LEAF_DOMAIN,
                "member_digest": expected["members"]["claims/alpha.json"],
                "path": "claims/alpha.json",
            }
        ).decode()
        == expected["leaf_preimage"]
    )
    assert (
        canonical_bytes(
            {"tag": DEPGRAPH_ROOT_DOMAIN, "node": rebuilt.node(ROOT_PREFIX).digest.value}
        ).decode()
        == expected["root_preimage"]
    )

    change_set = golden["input"]["change_set"]
    incremental = update_dependency_edge_tree(
        build_dependency_edge_tree(parent_edges),
        updated={
            path: tuple(DependencyProofReferenceV1.model_validate(item) for item in items)
            for path, items in change_set["updated"].items()
        },
        removed=change_set["removed"],
    )
    assert incremental.root.tagged == expected["root"]
    assert incremental.nodes == rebuilt.nodes
    verify_dependency_edge_root(final_edges, claimed_root=expected["root"])
