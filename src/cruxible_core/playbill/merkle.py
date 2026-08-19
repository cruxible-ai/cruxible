"""Path-segment merkle trie whose recompute cost tracks the change set.

The flat `manifest_root` commits to every member of the semantic projection in a
single preimage, so recomputing it after a change touching five members still
re-hashes all of them: verification is O(total members) per generation and
replay is O(generations x total members). This module builds the same commitment
as a path-segment trie, so accepting or replaying a change set re-hashes only the
touched leaves and the interior nodes on their root paths.

The same shape commits to the accepted dependency edge set, which had the same
O(total) problem for the same reason. One tree implementation serves both: a
`MerkleDomainFamily` parametrizes the leaf/node/root domain tags and the root
spelling, so the two commitments are domain-separated at every node and their
roots cannot be confused, without a second trie to keep in step. A family is a
wire commitment: adding one is a wire succession, and changing one is a break.

Nothing here is wired into settlement, recovery, or any served surface. The
primitives and their root spellings land first; the wire succession that adopts
them is separate work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Generic, TypeVar, cast

from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    CanonicalScalar,
    CanonicalValue,
    DependencyEdgeRoot,
    Manifest,
    MerkleNodeDigest,
    SemanticMerkleRoot,
    Sha256Value,
    manifest_for_tree,
    normalize_ledger_path,
    normalize_manifest_paths,
    typed_digest,
)
from cruxible_core.playbill.errors import CanonicalEncodingError, MerkleIntegrityError

MERKLE_LEAF_DOMAIN: Final = "playbill-merkle-leaf-v1"
MERKLE_NODE_DOMAIN: Final = "playbill-merkle-node-v1"
MERKLE_ROOT_DOMAIN: Final = "playbill-merkle-root-v1"

DEPGRAPH_LEAF_DOMAIN: Final = "playbill-depgraph-leaf-v1"
DEPGRAPH_NODE_DOMAIN: Final = "playbill-depgraph-node-v1"
DEPGRAPH_ROOT_DOMAIN: Final = "playbill-depgraph-root-v1"

ROOT_PREFIX: Final = ""

RootT = TypeVar("RootT", bound=Sha256Value)


@dataclass(frozen=True)
class MerkleDomainFamily(Generic[RootT]):
    """The complete domain separation of one merkle commitment.

    Every hash a tree takes is tagged with one of these three domains, and the
    root it publishes carries `root_type`'s algorithm spelling. Two families
    therefore disagree on every node digest of the same members *and* on how
    their roots are written, so neither a value nor a spelling can cross.
    """

    leaf: str
    node: str
    root: str
    root_type: type[RootT]


MANIFEST_MERKLE_DOMAINS: Final[MerkleDomainFamily[SemanticMerkleRoot]] = MerkleDomainFamily(
    leaf=MERKLE_LEAF_DOMAIN,
    node=MERKLE_NODE_DOMAIN,
    root=MERKLE_ROOT_DOMAIN,
    root_type=SemanticMerkleRoot,
)
"""Members are semantic-projection paths mapped to their exact content digests."""

DEPENDENCY_EDGE_DOMAINS: Final[MerkleDomainFamily[DependencyEdgeRoot]] = MerkleDomainFamily(
    leaf=DEPGRAPH_LEAF_DOMAIN,
    node=DEPGRAPH_NODE_DOMAIN,
    root=DEPGRAPH_ROOT_DOMAIN,
    root_type=DependencyEdgeRoot,
)
"""Members are source paths mapped to the digest of their outgoing edge set."""


@dataclass(frozen=True)
class MerkleNode:
    """One trie node: either a member leaf or an interior directory node."""

    prefix: str
    digest: MerkleNodeDigest
    member_digest: str | None = None
    segments: tuple[str, ...] = ()

    @property
    def is_leaf(self) -> bool:
        return self.member_digest is not None


@dataclass(frozen=True)
class MerkleTree(Generic[RootT]):
    """A merkle root plus every node needed to update it without a full rebuild."""

    root: RootT
    nodes: Mapping[str, MerkleNode]
    domains: MerkleDomainFamily[RootT]

    def node(self, prefix: str) -> MerkleNode:
        node = self.nodes.get(prefix)
        if node is None:
            raise MerkleIntegrityError(f"merkle manifest has no node at {prefix!r}")
        return node

    def members(self) -> Manifest:
        """Return the path-to-member-digest map this tree commits to."""

        return {
            node.prefix: cast(str, node.member_digest)
            for node in sorted(self.nodes.values(), key=lambda item: item.prefix.encode("utf-8"))
            if node.is_leaf
        }


MerkleManifest = MerkleTree[SemanticMerkleRoot]
"""The manifest-domain tree, named for the commitment it carries."""


def _depth(prefix: str) -> int:
    return 0 if prefix == ROOT_PREFIX else prefix.count("/") + 1


def _join(prefix: str, segment: str) -> str:
    return segment if prefix == ROOT_PREFIX else f"{prefix}/{segment}"


def _sorted_segments(segments: Iterable[str]) -> list[str]:
    return sorted(segments, key=lambda segment: segment.encode("utf-8"))


def _bottom_up(prefixes: Iterable[str]) -> list[str]:
    """Order interior prefixes so every child is recomputed before its parent."""

    return sorted(prefixes, key=lambda prefix: (-_depth(prefix), prefix.encode("utf-8")))


def _leaf_digest(
    path: str,
    member_digest: str,
    domains: MerkleDomainFamily[RootT],
) -> MerkleNodeDigest:
    return typed_digest(
        MerkleNodeDigest,
        domains.leaf,
        {"path": path, "member_digest": member_digest},
    )


def _interior_digest(
    children: Sequence[tuple[str, MerkleNodeDigest]],
    domains: MerkleDomainFamily[RootT],
) -> MerkleNodeDigest:
    entries: list[CanonicalValue] = [
        [segment, cast(CanonicalScalar, digest.value)] for segment, digest in children
    ]
    return typed_digest(MerkleNodeDigest, domains.node, {"children": entries})


def _root_value(root_node: MerkleNode, domains: MerkleDomainFamily[RootT]) -> RootT:
    return typed_digest(domains.root_type, domains.root, {"node": root_node.digest.value})


def _interior_node(
    prefix: str,
    segments: Iterable[str],
    nodes: Mapping[str, MerkleNode],
    domains: MerkleDomainFamily[RootT],
) -> MerkleNode:
    ordered = _sorted_segments(segments)
    children = [(segment, nodes[_join(prefix, segment)].digest) for segment in ordered]
    return MerkleNode(
        prefix=prefix,
        digest=_interior_digest(children, domains),
        segments=tuple(ordered),
    )


def _refuse_casefold_sibling(prefix: str, segment: str, siblings: Iterable[str]) -> None:
    folded = segment.casefold()
    for sibling in siblings:
        if sibling != segment and sibling.casefold() == folded:
            raise CanonicalEncodingError(
                f"case-fold-colliding siblings refused below {prefix or '/'}: "
                f"{sibling!r} and {segment!r}"
            )


def _member_digest(path: str, value: str) -> str:
    try:
        return ArtifactDigest(value).value
    except ValueError as exc:
        raise CanonicalEncodingError(f"merkle member digest is malformed at {path!r}") from exc


def normalize_member_manifest(manifest: Mapping[str, str]) -> Manifest:
    """Normalize a path-to-member-digest map exactly as the flat manifest does."""

    raw_by_normalized: dict[str, str] = {}
    for raw_path in manifest:
        path = normalize_ledger_path(raw_path)
        if path in raw_by_normalized:
            raise CanonicalEncodingError(
                "paths collide after NFC normalization: "
                f"{raw_by_normalized[path]!r} and {raw_path!r}"
            )
        raw_by_normalized[path] = raw_path
    ordered = normalize_manifest_paths(list(manifest))
    return {path: _member_digest(path, manifest[raw_by_normalized[path]]) for path in ordered}


def build_merkle_tree(
    manifest: Mapping[str, str],
    *,
    domains: MerkleDomainFamily[RootT],
) -> MerkleTree[RootT]:
    """Build the complete trie from a path-to-member-digest map."""

    # `normalize_member_manifest` already refused case-fold sibling collisions,
    # so the trie only has to discover structure here.
    members = normalize_member_manifest(manifest)
    interior: dict[str, list[str]] = {ROOT_PREFIX: []}
    for path in members:
        prefix = ROOT_PREFIX
        for segment in path.split("/"):
            siblings = interior.setdefault(prefix, [])
            if segment not in siblings:
                siblings.append(segment)
            prefix = _join(prefix, segment)
    for path in members:
        if path in interior:
            raise CanonicalEncodingError(f"path is both a member and a directory: {path!r}")

    nodes: dict[str, MerkleNode] = {
        path: MerkleNode(
            prefix=path,
            digest=_leaf_digest(path, digest, domains),
            member_digest=digest,
        )
        for path, digest in members.items()
    }
    for prefix in _bottom_up(interior):
        nodes[prefix] = _interior_node(prefix, interior[prefix], nodes, domains)
    return MerkleTree(
        root=_root_value(nodes[ROOT_PREFIX], domains),
        nodes=nodes,
        domains=domains,
    )


def build_merkle_manifest(manifest: Mapping[str, str]) -> MerkleManifest:
    """Build the manifest-domain trie from a path-to-member-digest map."""

    return build_merkle_tree(manifest, domains=MANIFEST_MERKLE_DOMAINS)


def merkle_manifest_root(tree: Mapping[str, bytes]) -> SemanticMerkleRoot:
    """Hash a tree's members through the trie, the merkle analogue of `manifest_root`."""

    return build_merkle_manifest(manifest_for_tree(tree)).root


def update_merkle_tree(
    manifest: MerkleTree[RootT],
    *,
    updated: Mapping[str, str] | None = None,
    removed: Iterable[str] | None = None,
) -> MerkleTree[RootT]:
    """Apply one change set, rehashing only touched leaves and their ancestors.

    Untouched subtrees are carried over as the identical `MerkleNode` objects, so
    no digest outside the changed paths' root paths is recomputed. The node map
    itself is copied, which is a pointer copy per member and never a hash.
    """

    domains = manifest.domains
    updates = {
        normalize_ledger_path(path): _member_digest(path, digest)
        for path, digest in (updated or {}).items()
    }
    deletions = {normalize_ledger_path(path) for path in (removed or ())}
    collisions = sorted(set(updates) & deletions)
    if collisions:
        raise CanonicalEncodingError(f"merkle change set both updates and removes: {collisions}")

    nodes = dict(manifest.nodes)
    dirty: dict[str, list[str]] = {}

    def segments_of(prefix: str) -> list[str]:
        if prefix not in dirty:
            node = nodes.get(prefix)
            if node is None:
                dirty[prefix] = []
            elif node.is_leaf:
                raise CanonicalEncodingError(f"path is both a member and a directory: {prefix!r}")
            else:
                dirty[prefix] = list(node.segments)
        return dirty[prefix]

    for path in sorted(deletions):
        node = nodes.get(path)
        if node is None or not node.is_leaf:
            raise CanonicalEncodingError(f"merkle removal names a non-member path: {path!r}")
        del nodes[path]
        child = path
        pruning = True
        while child != ROOT_PREFIX:
            prefix, _, segment = child.rpartition("/")
            # Every ancestor is marked dirty, not only the pruned ones: a
            # surviving directory still commits to the child digest that moved.
            siblings = segments_of(prefix)
            if pruning:
                if segment in siblings:
                    siblings.remove(segment)
                if siblings or prefix == ROOT_PREFIX:
                    pruning = False
                else:
                    # The directory emptied out and leaves with its last member.
                    nodes.pop(prefix, None)
                    dirty.pop(prefix, None)
            child = prefix

    for path, member_digest in sorted(updates.items()):
        existing = nodes.get(path)
        if existing is not None and not existing.is_leaf:
            raise CanonicalEncodingError(f"path is both a member and a directory: {path!r}")
        segments = path.split("/")
        prefix = ROOT_PREFIX
        for segment in segments[:-1]:
            siblings = segments_of(prefix)
            if segment not in siblings:
                _refuse_casefold_sibling(prefix, segment, siblings)
                siblings.append(segment)
            prefix = _join(prefix, segment)
        siblings = segments_of(prefix)
        if segments[-1] not in siblings:
            _refuse_casefold_sibling(prefix, segments[-1], siblings)
            siblings.append(segments[-1])
        nodes[path] = MerkleNode(
            prefix=path,
            digest=_leaf_digest(path, member_digest, domains),
            member_digest=member_digest,
        )

    for prefix in _bottom_up(dirty):
        nodes[prefix] = _interior_node(prefix, dirty[prefix], nodes, domains)
    return MerkleTree(
        root=_root_value(nodes[ROOT_PREFIX], domains),
        nodes=nodes,
        domains=domains,
    )


def update_merkle_manifest(
    manifest: MerkleManifest,
    *,
    updated: Mapping[str, str] | None = None,
    removed: Iterable[str] | None = None,
) -> MerkleManifest:
    """Apply one change set to the manifest-domain trie."""

    return update_merkle_tree(manifest, updated=updated, removed=removed)


def verify_merkle_tree(
    manifest: Mapping[str, str],
    *,
    claimed_root: str,
    domains: MerkleDomainFamily[RootT],
) -> MerkleTree[RootT]:
    """Rebuild from members and refuse unless the claimed merkle root reproduces."""

    expected = domains.root_type.from_tagged(claimed_root)
    built = build_merkle_tree(manifest, domains=domains)
    if built.root != expected:
        raise MerkleIntegrityError("merkle manifest root does not reproduce from its members")
    return built


def verify_merkle_manifest(manifest: Mapping[str, str], *, claimed_root: str) -> MerkleManifest:
    """Rebuild the manifest-domain trie and refuse unless its root reproduces."""

    return verify_merkle_tree(
        manifest,
        claimed_root=claimed_root,
        domains=MANIFEST_MERKLE_DOMAINS,
    )


def verify_merkle_tree_nodes(
    nodes: Mapping[str, MerkleNode],
    *,
    claimed_root: str,
    domains: MerkleDomainFamily[RootT],
) -> None:
    """Recompute every node digest from the node map and refuse any drift.

    This is the carried-forward-state check: a node map is only usable as a warm
    cache if every digest in it still derives from its own children.
    """

    expected = domains.root_type.from_tagged(claimed_root)
    visited: list[str] = []
    pending = [ROOT_PREFIX]
    while pending:
        prefix = pending.pop()
        node = nodes.get(prefix)
        if node is None:
            raise MerkleIntegrityError(f"merkle node map has no node at {prefix!r}")
        if node.prefix != prefix:
            raise MerkleIntegrityError(f"merkle node at {prefix!r} disagrees with its own prefix")
        visited.append(prefix)
        if not node.is_leaf:
            pending.extend(_join(prefix, segment) for segment in node.segments)
    if len(visited) != len(nodes):
        raise MerkleIntegrityError("merkle node map holds nodes unreachable from its root")

    for prefix in reversed(visited):
        node = nodes[prefix]
        if node.is_leaf:
            recomputed = _leaf_digest(prefix, cast(str, node.member_digest), domains)
        else:
            ordered = _sorted_segments(node.segments)
            if list(node.segments) != ordered:
                raise MerkleIntegrityError(f"merkle node children are unordered at {prefix!r}")
            recomputed = _interior_digest(
                [(segment, nodes[_join(prefix, segment)].digest) for segment in ordered],
                domains,
            )
        if recomputed != node.digest:
            raise MerkleIntegrityError(f"merkle node digest does not reproduce at {prefix!r}")
    if _root_value(nodes[ROOT_PREFIX], domains) != expected:
        raise MerkleIntegrityError("merkle root does not reproduce from its node map")


def verify_merkle_nodes(nodes: Mapping[str, MerkleNode], *, claimed_root: str) -> None:
    """Recompute every manifest-domain node digest and refuse any drift."""

    verify_merkle_tree_nodes(
        nodes,
        claimed_root=claimed_root,
        domains=MANIFEST_MERKLE_DOMAINS,
    )


EMPTY_MERKLE_ROOT: Final = build_merkle_manifest({}).root
"""The root of a manifest with no members, defined rather than left undefined."""

EMPTY_DEPENDENCY_EDGE_ROOT: Final = build_merkle_tree({}, domains=DEPENDENCY_EDGE_DOMAINS).root
"""The root of an edge set with no edges, defined rather than left undefined."""


__all__ = [
    "DEPENDENCY_EDGE_DOMAINS",
    "DEPGRAPH_LEAF_DOMAIN",
    "DEPGRAPH_NODE_DOMAIN",
    "DEPGRAPH_ROOT_DOMAIN",
    "EMPTY_DEPENDENCY_EDGE_ROOT",
    "EMPTY_MERKLE_ROOT",
    "MANIFEST_MERKLE_DOMAINS",
    "MERKLE_LEAF_DOMAIN",
    "MERKLE_NODE_DOMAIN",
    "MERKLE_ROOT_DOMAIN",
    "MerkleDomainFamily",
    "MerkleManifest",
    "MerkleNode",
    "MerkleTree",
    "ROOT_PREFIX",
    "build_merkle_manifest",
    "build_merkle_tree",
    "merkle_manifest_root",
    "normalize_member_manifest",
    "update_merkle_manifest",
    "update_merkle_tree",
    "verify_merkle_manifest",
    "verify_merkle_nodes",
    "verify_merkle_tree",
    "verify_merkle_tree_nodes",
]
