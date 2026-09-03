"""Frozen Playbill canonical encoding and domain-separated SHA-256 values."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, TypeVar, cast

from cruxible_client.contracts.errors import CanonicalEncodingError
from cruxible_client.contracts.primitives import canonical_json, pretty_json

CanonicalScalar = None | bool | int | str
CanonicalValue = CanonicalScalar | list["CanonicalValue"] | dict[str, "CanonicalValue"]
Manifest = dict[str, str]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArtifactCodec(str, Enum):
    """Compiler-selected governed-artifact byte and path spelling."""

    CURRENT_PRETTY_JSON = "playbill-artifact-codec-pretty-json-v1"
    P2_B0_COMPACT_JSON = "playbill-artifact-codec-compact-json-v1"


CURRENT_ARTIFACT_CODEC = ArtifactCodec.CURRENT_PRETTY_JSON
P2_B0_ARTIFACT_CODEC = ArtifactCodec.P2_B0_COMPACT_JSON


def _normalize_string(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalEncodingError("string contains a non-Unicode surrogate") from exc
    return normalized


def normalize_canonical(value: object, *, location: str = "$") -> CanonicalValue:
    """Return the closed Playbill JSON value set or refuse.

    Playbill narrows the repository-wide canonical JSON primitive by refusing
    floats, runtime bytes, non-list sequences, non-string keys, and Unicode
    normalization collisions before serialization.
    """

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalEncodingError(f"{location}: floating-point values are forbidden")
    if isinstance(value, bytes):
        raise CanonicalEncodingError(
            f"{location}: runtime bytes are forbidden; binary fields use lowercase hex"
        )
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, list):
        return [
            normalize_canonical(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        normalized_items: list[tuple[str, CanonicalValue]] = []
        seen: set[str] = set()
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalEncodingError(f"{location}: object keys must be strings")
            key = _normalize_string(raw_key)
            if key in seen:
                raise CanonicalEncodingError(
                    f"{location}: keys collide after NFC normalization: {key!r}"
                )
            seen.add(key)
            normalized_items.append(
                (key, normalize_canonical(raw_value, location=f"{location}.{key}"))
            )
        normalized_items.sort(key=lambda item: item[0].encode("utf-8"))
        return dict(normalized_items)
    if isinstance(value, Sequence):
        raise CanonicalEncodingError(f"{location}: arrays must be concrete lists")
    raise CanonicalEncodingError(f"{location}: unsupported value type {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    """Encode one normalized value as whitespace-free UTF-8 JSON."""

    return canonical_json(normalize_canonical(value)).encode("utf-8")


def pretty_canonical_bytes(value: object) -> bytes:
    """Encode one normalized value as sorted indent-2 JSON with one final LF."""

    return pretty_json(normalize_canonical(value)).encode("utf-8") + b"\n"


def artifact_path_for_codec(current_path: str, codec: ArtifactCodec) -> str:
    """Return the one path spelling selected by ``codec``."""

    if not current_path.endswith(".json"):
        raise CanonicalEncodingError("current artifact path must end in .json")
    if codec is ArtifactCodec.CURRENT_PRETTY_JSON:
        return current_path
    if codec is ArtifactCodec.P2_B0_COMPACT_JSON:
        return current_path.removesuffix(".json") + ".yaml"
    raise CanonicalEncodingError(f"unsupported artifact codec: {codec!r}")


def artifact_bytes_for_codec(
    rendered_current: bytes,
    *,
    path: str,
    codec: ArtifactCodec,
) -> bytes:
    """Encode bytes from the compiler-selected codec; suffix only checks consistency."""

    allowed_suffixes = (
        (".json",) if codec is ArtifactCodec.CURRENT_PRETTY_JSON else (".yaml", ".json")
    )
    if not path.endswith(allowed_suffixes):
        raise CanonicalEncodingError(
            f"artifact path is inconsistent with {codec.value}: expected one of "
            f"{allowed_suffixes!r}"
        )
    if codec is ArtifactCodec.CURRENT_PRETTY_JSON:
        return rendered_current
    if codec is ArtifactCodec.P2_B0_COMPACT_JSON:
        return canonical_bytes(json.loads(rendered_current)) + b"\n"
    raise CanonicalEncodingError(f"unsupported artifact codec: {codec!r}")


def artifact_bytes_for_path(
    rendered_current: bytes,
    path: str,
    *,
    codec: ArtifactCodec,
) -> bytes:
    """Encode one artifact using an explicit compiler-selected codec."""

    return artifact_bytes_for_codec(rendered_current, path=path, codec=codec)


def artifact_path_matches(
    current_path: str,
    actual_path: str,
    *,
    codec: ArtifactCodec,
) -> bool:
    """Check that an identity-derived current path agrees with the selected codec."""

    return actual_path == artifact_path_for_codec(current_path, codec)


def canonical_digest(domain: str, payload: Mapping[str, object]) -> str:
    """Hash one exact domain-separated canonical document."""

    if "tag" in payload:
        raise CanonicalEncodingError("payload must not supply the reserved tag field")
    return hashlib.sha256(canonical_bytes({"tag": domain, **payload})).hexdigest()


class CanonicalDigester:
    """Concrete structural implementation of the canonical digest seam."""

    def digest(self, domain: str, payload: Mapping[str, object]) -> "Sha256Value":
        return Sha256Value(canonical_digest(domain, payload))


@dataclass(frozen=True)
class Sha256Value:
    """A kind-distinct SHA-256 value with an explicit algorithm tag."""

    value: str

    algorithm: ClassVar[str] = "sha256"
    kind: ClassVar[str] = "digest"

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.value):
            raise ValueError(f"{self.kind} must be 32 bytes of lowercase SHA-256 hex")

    @property
    def tagged(self) -> str:
        """Return the public algorithm-tagged spelling."""

        return f"{self.algorithm}:{self.value}"

    @classmethod
    def from_tagged(cls, value: str) -> "Sha256Value":
        prefix, separator, digest = value.partition(":")
        if separator != ":" or prefix != cls.algorithm:
            raise ValueError(f"{cls.kind} must use the {cls.algorithm!r} algorithm tag")
        return cls(digest)


class BootstrapRoot(Sha256Value):
    kind = "bootstrap root"


class ArtifactDigest(Sha256Value):
    kind = "artifact digest"


class CandidateDigest(Sha256Value):
    kind = "candidate digest"


class AcceptanceLawDigest(Sha256Value):
    kind = "acceptance-law digest"


class ApprovalDigest(Sha256Value):
    kind = "approval-attestation digest"


class ProposalDigest(Sha256Value):
    kind = "proposal digest"


class SemanticDiffDigest(Sha256Value):
    kind = "semantic diff digest"


class ChangeSetDigest(Sha256Value):
    kind = "change-set digest"


class SemanticManifestRoot(Sha256Value):
    kind = "semantic manifest root"


class MerkleNodeDigest(Sha256Value):
    kind = "merkle node digest"


class SemanticMerkleRoot(Sha256Value):
    """Merkle-structured manifest root, spelled so it can never read as flat.

    The flat `SemanticManifestRoot` commits to every member in one preimage; this
    root commits to a path-segment trie whose interior nodes are recomputed only
    along the changed paths. Both are 32-byte SHA-256 values, so the tagged
    spellings are deliberately disjoint: a flat root never parses as a merkle
    root and a merkle root never parses as a flat root, and no field can silently
    accept the wrong structure.
    """

    algorithm = "merkle-sha256"
    kind = "semantic merkle root"


class DependencyEdgeRoot(Sha256Value):
    """Merkle root over the accepted dependency edge set, spelled to stand alone.

    `playbill-dependency-graph-v2` committed to the full edge lists of both trees
    in one preimage, so a change touching five members re-hashed every edge in
    the instance. This root commits to the same edges as a path-segment trie
    keyed by each edge's source member, so only the touched members' edge sets
    and the interior nodes above them are re-hashed.

    Its algorithm tag is disjoint from both `sha256:` and the manifest merkle's
    `merkle-sha256:`. The two tries are also domain-separated at every node (see
    `merkle.DEPENDENCY_EDGE_DOMAINS`), so the separation holds in the digests as
    well as in the spellings: a dependency edge root can neither parse nor hash
    as a manifest root, and vice versa.
    """

    algorithm = "depgraph-sha256"
    kind = "dependency edge root"


class SemanticRoot(Sha256Value):
    kind = "semantic root"


class GenerationRoot(Sha256Value):
    kind = "generation root"


class LogicalDigest(Sha256Value):
    kind = "logical digest"


class CasDigest(Sha256Value):
    kind = "CAS digest"


DigestT = TypeVar("DigestT", bound=Sha256Value)


def typed_digest(digest_type: type[DigestT], domain: str, payload: Mapping[str, object]) -> DigestT:
    """Return a kind-distinct digest without changing the frozen preimage."""

    return digest_type(canonical_digest(domain, payload))


def file_digest(content: bytes) -> ArtifactDigest:
    """Hash exact artifact bytes independently of Git's object format."""

    return ArtifactDigest(hashlib.sha256(content).hexdigest())


def normalize_ledger_path(path: str) -> str:
    """Validate and normalize one relative POSIX ledger path."""

    normalized = _normalize_string(path)
    if not normalized:
        raise CanonicalEncodingError("ledger path must not be empty")
    if normalized.startswith("/"):
        raise CanonicalEncodingError(f"absolute ledger path refused: {path!r}")
    if "\\" in normalized or "\x00" in normalized:
        raise CanonicalEncodingError(f"non-POSIX ledger path refused: {path!r}")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CanonicalEncodingError(f"non-canonical ledger path refused: {path!r}")
    return normalized


def normalize_manifest_paths(paths: Sequence[str]) -> list[str]:
    """Normalize paths and refuse exact or case-fold sibling collisions."""

    normalized: list[str] = []
    exact: set[str] = set()
    siblings: dict[tuple[str, str], str] = {}
    for raw_path in paths:
        path = normalize_ledger_path(raw_path)
        if path in exact:
            raise CanonicalEncodingError(f"duplicate ledger path: {path!r}")
        exact.add(path)
        parts = path.split("/")
        for index, part in enumerate(parts):
            parent = "/".join(parts[:index])
            collision_key = (parent.casefold(), part.casefold())
            previous = siblings.get(collision_key)
            if previous is not None and previous != part:
                raise CanonicalEncodingError(
                    f"case-fold-colliding siblings refused below {parent or '/'}: "
                    f"{previous!r} and {part!r}"
                )
            siblings[collision_key] = part
        normalized.append(path)
    return sorted(normalized, key=lambda item: item.encode("utf-8"))


def manifest_for_tree(tree: Mapping[str, bytes]) -> Manifest:
    """Build a normalized path-to-content-digest manifest."""

    normalized_to_raw: dict[str, str] = {}
    for raw_path in tree:
        path = normalize_ledger_path(raw_path)
        if path in normalized_to_raw:
            raise CanonicalEncodingError(
                "paths collide after NFC normalization: "
                f"{normalized_to_raw[path]!r} and {raw_path!r}"
            )
        normalized_to_raw[path] = raw_path
    ordered_paths = normalize_manifest_paths(list(tree))
    return {path: file_digest(tree[normalized_to_raw[path]]).value for path in ordered_paths}


def manifest_for_tree_carrying(
    tree: Mapping[str, bytes],
    *,
    previous_tree: Mapping[str, bytes],
    previous_manifest: Manifest,
) -> Manifest:
    """Build `manifest_for_tree(tree)` without re-hashing byte-identical members.

    A member digest is a pure function of the member's bytes, so a member whose
    bytes are byte-for-byte the predecessor's already has its digest in
    `previous_manifest` and does not have to be hashed again. Every other member
    is hashed exactly as the from-scratch build hashes it, so the result is
    identical to `manifest_for_tree(tree)` whenever `previous_manifest` is the
    manifest of `previous_tree` -- and the carry is proven per member by exact
    byte comparison, never assumed from an external identifier.

    Path normalization, NFC-collision refusal, and case-fold sibling refusal run
    over the whole path set exactly as in `manifest_for_tree`: this reuses member
    digests, never the path set the manifest commits to.
    """

    normalized_to_raw: dict[str, str] = {}
    for raw_path in tree:
        path = normalize_ledger_path(raw_path)
        if path in normalized_to_raw:
            raise CanonicalEncodingError(
                "paths collide after NFC normalization: "
                f"{normalized_to_raw[path]!r} and {raw_path!r}"
            )
        normalized_to_raw[path] = raw_path
    manifest: Manifest = {}
    for path in normalize_manifest_paths(list(tree)):
        raw_path = normalized_to_raw[path]
        content = tree[raw_path]
        carried = previous_manifest.get(path)
        # A raw path present in both trees normalizes to the same member path in
        # both, so an exact-byte match discharges the whole proof obligation.
        if carried is not None and previous_tree.get(raw_path) == content:
            manifest[path] = carried
        else:
            manifest[path] = file_digest(content).value
    return manifest


def manifest_root_from_members(manifest: Manifest) -> SemanticManifestRoot:
    """Hash one already-built path-to-member-digest manifest."""

    entries: list[CanonicalValue] = [
        [path, cast(CanonicalScalar, digest)] for path, digest in manifest.items()
    ]
    return typed_digest(
        SemanticManifestRoot,
        "playbill-manifest-v1",
        {"entries": entries},
    )


def manifest_root(tree: Mapping[str, bytes]) -> SemanticManifestRoot:
    """Hash sorted path and exact-content digest entries."""

    return manifest_root_from_members(manifest_for_tree(tree))


def semantic_projection(tree: Mapping[str, bytes]) -> dict[str, bytes]:
    """Return Π(tree), excluding deterministic daemon records and derivative cards."""

    return {
        path: content
        for path, content in tree.items()
        if not normalize_ledger_path(path).startswith(("changesets/", "cards/"))
    }


def semantic_diff_from_members(
    base: Manifest,
    candidate: Manifest,
) -> tuple[SemanticDiffDigest, tuple[str, ...]]:
    """Diff two already-built semantic manifests, never Git object IDs.

    The digest preimage covers only the changed entries and the returned scope is
    only the changed paths, so a caller that already holds both manifests never
    has to touch a single member byte to reproduce either value.
    """

    scope = tuple(
        sorted(
            {
                *base.keys(),
                *candidate.keys(),
            },
            key=lambda path: path.encode("utf-8"),
        )
    )
    changed = tuple(path for path in scope if base.get(path) != candidate.get(path))
    entries: list[CanonicalValue] = [
        [
            path,
            cast(CanonicalScalar, base.get(path)),
            cast(CanonicalScalar, candidate.get(path)),
        ]
        for path in changed
    ]
    return (
        typed_digest(
            SemanticDiffDigest,
            "playbill-sdiff-v1",
            {"entries": entries},
        ),
        changed,
    )


def semantic_diff(
    base_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
) -> tuple[SemanticDiffDigest, tuple[str, ...]]:
    """Hash sorted path/old/new content-digest entries, never Git object IDs."""

    return semantic_diff_from_members(
        manifest_for_tree(semantic_projection(base_tree)),
        manifest_for_tree(semantic_projection(candidate_tree)),
    )


__all__ = [
    "ArtifactCodec",
    "ArtifactDigest",
    "CURRENT_ARTIFACT_CODEC",
    "P2_B0_ARTIFACT_CODEC",
    "artifact_bytes_for_codec",
    "artifact_bytes_for_path",
    "artifact_path_for_codec",
    "artifact_path_matches",
    "AcceptanceLawDigest",
    "ApprovalDigest",
    "BootstrapRoot",
    "CandidateDigest",
    "CanonicalDigester",
    "CasDigest",
    "ChangeSetDigest",
    "DependencyEdgeRoot",
    "GenerationRoot",
    "LogicalDigest",
    "MerkleNodeDigest",
    "ProposalDigest",
    "SemanticManifestRoot",
    "SemanticMerkleRoot",
    "SemanticDiffDigest",
    "SemanticRoot",
    "Sha256Value",
    "canonical_bytes",
    "canonical_digest",
    "file_digest",
    "manifest_for_tree",
    "manifest_for_tree_carrying",
    "manifest_root",
    "manifest_root_from_members",
    "normalize_canonical",
    "normalize_ledger_path",
    "normalize_manifest_paths",
    "pretty_canonical_bytes",
    "semantic_diff",
    "semantic_diff_from_members",
    "semantic_projection",
    "typed_digest",
]
