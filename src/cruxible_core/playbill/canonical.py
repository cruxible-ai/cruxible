"""Frozen Playbill canonical encoding and domain-separated SHA-256 values."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, TypeVar, cast

from cruxible_core.playbill.errors import CanonicalEncodingError
from cruxible_core.primitives import canonical_json

CanonicalScalar = None | bool | int | str
CanonicalValue = CanonicalScalar | list["CanonicalValue"] | dict[str, "CanonicalValue"]
Manifest = dict[str, str]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def manifest_root(tree: Mapping[str, bytes]) -> SemanticManifestRoot:
    """Hash sorted path and exact-content digest entries."""

    manifest = manifest_for_tree(tree)
    entries: list[CanonicalValue] = [
        [path, cast(CanonicalScalar, digest)] for path, digest in manifest.items()
    ]
    return typed_digest(
        SemanticManifestRoot,
        "playbill-manifest-v1",
        {"entries": entries},
    )


def semantic_projection(tree: Mapping[str, bytes]) -> dict[str, bytes]:
    """Return Π(tree), excluding only deterministic daemon change-set records."""

    return {
        path: content
        for path, content in tree.items()
        if not normalize_ledger_path(path).startswith("changesets/")
    }


def semantic_diff(
    base_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
) -> tuple[SemanticDiffDigest, tuple[str, ...]]:
    """Hash sorted path/old/new content-digest entries, never Git object IDs."""

    base = manifest_for_tree(semantic_projection(base_tree))
    candidate = manifest_for_tree(semantic_projection(candidate_tree))
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


__all__ = [
    "ArtifactDigest",
    "AcceptanceLawDigest",
    "ApprovalDigest",
    "BootstrapRoot",
    "CandidateDigest",
    "CanonicalDigester",
    "CasDigest",
    "ChangeSetDigest",
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
    "manifest_root",
    "normalize_canonical",
    "normalize_ledger_path",
    "normalize_manifest_paths",
    "semantic_diff",
    "semantic_projection",
    "typed_digest",
]
