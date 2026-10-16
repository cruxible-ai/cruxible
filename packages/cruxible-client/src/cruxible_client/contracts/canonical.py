"""Frozen Cruxible canonical encoding and domain-separated SHA-256 values."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import typing
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import ClassVar, TypeVar, cast

from pydantic import BaseModel

from cruxible_client.contracts.content_memo import FrozenDict, FrozenList, is_sealed, sealed_memo
from cruxible_client.contracts.errors import CanonicalEncodingError
from cruxible_client.contracts.primitives import canonical_json, pretty_json

CanonicalScalar = None | bool | int | str
CanonicalValue = CanonicalScalar | list["CanonicalValue"] | dict[str, "CanonicalValue"]
Manifest = dict[str, str]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@lru_cache(maxsize=65536)
def _valid_sha256_hex(value: str) -> bool:
    """Validate each immutable hex value once, independently of its digest kind."""
    return _SHA256_RE.fullmatch(value) is not None


class ArtifactCodec(str, Enum):
    """Compiler-selected governed-artifact byte and path spelling."""

    CURRENT_PRETTY_JSON = "playbill-artifact-codec-pretty-json-v1"
    P2_B0_COMPACT_JSON = "playbill-artifact-codec-compact-json-v1"


CURRENT_ARTIFACT_CODEC = ArtifactCodec.CURRENT_PRETTY_JSON
P2_B0_ARTIFACT_CODEC = ArtifactCodec.P2_B0_COMPACT_JSON


def _normalize_string(value: str) -> str:
    # Exact ASCII strings are already NFC and cannot contain a surrogate.
    # Keep subclass handling on the original path (including its encoding hook).
    if type(value) is str and value.isascii():
        return value
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalEncodingError("string contains a non-Unicode surrogate") from exc
    return normalized


def normalize_canonical(value: object, *, location: str = "$") -> CanonicalValue:
    """Return the closed Cruxible JSON value set or refuse.

    Cruxible narrows the repository-wide canonical JSON primitive by refusing
    floats, runtime bytes, non-list sequences, non-string keys, and Unicode
    normalization collisions before serialization.
    """

    # Most values in serialized contracts are exact strings. Handle those
    # before the scalar rejection checks; subclasses retain the original path.
    if type(value) is str:
        return _normalize_string(value)
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


_SURROGATE = re.compile("[\ud800-\udfff]")


def _canonical_string(value: str) -> bool:
    # Exactly what ``_normalize_string`` returns unchanged: NFC and surrogate-free.
    return value.isascii() or (
        unicodedata.is_normalized("NFC", value) and _SURROGATE.search(value) is None
    )


#: Nesting depth past which a value always takes the original normalizing path.
#: Far below any supported Python's recursion limit for that path (3.11 spends
#: two frames per list level, 3.12+ one), so the direct path never accepts, or
#: refuses, a value the original would not: deeper values meet exactly the
#: original's limit.
_DIRECT_DEPTH = 200


def _canonical_ready(value: object) -> bool:
    """Whether ``value`` takes the direct encoder (``_ready``).

    A value nested deeper than ``_DIRECT_DEPTH``, or too deep to check from
    this stack, is left to the normalizing path, which accepts it, or refuses
    it, exactly as before.
    """

    try:
        return _ready(value, 0)
    except RecursionError:
        return False


def _ready(value: object, depth: int) -> bool:
    """Whether ``value`` is already in the closed canonical value set, exactly.

    Exact ``str`` (NFC, no surrogate), ``int``, ``bool`` and ``None``, and exact
    lists and string-keyed dicts of them (the sealed containers included): for
    such a value ``normalize_canonical`` changes nothing a JSON encoder sees --
    its keys are already NFC, so none can collide, and sorting by code point is
    sorting by UTF-8 -- so the stdlib encoder writes the canonical bytes
    directly. Anything else (a float, bytes, a tuple, a subclass, a non-NFC or
    surrogate string) takes the normalizing path and refuses exactly as before.

    ``depth`` is the containers above ``value``; one frame per level.
    """

    kind = type(value)
    if kind is str:
        return _canonical_string(cast(str, value))
    if kind is int or kind is bool or value is None:
        return True
    if depth >= _DIRECT_DEPTH:
        return False
    if kind is dict or kind is FrozenDict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str or not _canonical_string(key) or not _ready(item, depth + 1):
                return False
        return True
    if kind is list or kind is FrozenList:
        for item in cast(list[object], value):
            if not _ready(item, depth + 1):
                return False
        return True
    return False


def canonical_bytes(value: object) -> bytes:
    """Encode one normalized value as whitespace-free UTF-8 JSON."""

    if _canonical_ready(value):
        return canonical_json(value).encode("utf-8")
    return canonical_json(normalize_canonical(value)).encode("utf-8")


def _model_classes(annotation: object) -> frozenset[type]:
    """Every model class an annotation names, through unions, tuples and Annotated."""

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return frozenset((annotation,))
    found: set[type] = set()
    for argument in typing.get_args(annotation):
        found |= _model_classes(argument)
    return frozenset(found)


_COMPOSABLE_FIELDS: dict[type, dict[str, frozenset[type]]] = {}


def _composable_fields(cls: type[BaseModel]) -> dict[str, frozenset[type]]:
    """The fields of ``cls`` whose JSON dump is exactly their model value's own dump.

    A field qualifies when the class has no model serializer, computed field or
    alias-keyed dump, and the field is neither excluded, aliased nor given its
    own serializer; the value's class must then be one the annotation names (a
    subclass would be dumped as its declared class). Decided once per class.
    """

    found = _COMPOSABLE_FIELDS.get(cls)
    if found is None:
        decorators = cls.__pydantic_decorators__
        serialized = {
            name
            for decorator in decorators.field_serializers.values()
            for name in decorator.info.fields
        }
        found = {}
        if (
            not decorators.model_serializers
            and not cls.model_computed_fields
            and not cls.model_config.get("serialize_by_alias")
        ):
            for name, info in cls.model_fields.items():
                classes = _model_classes(info.annotation)
                if (
                    classes
                    and not info.exclude
                    and info.serialization_alias is None
                    and name not in serialized
                ):
                    found[name] = classes
        _COMPOSABLE_FIELDS[cls] = found
    return found


def _sealed_field_bytes(value: object, classes: frozenset[type]) -> bytes | None:
    """A field value's canonical bytes from sealed models' remembered encodings, if it is one."""

    if type(value) in classes and is_sealed(value):
        return model_canonical_bytes(cast(BaseModel, value))
    if (
        type(value) is tuple
        and value
        and all(type(item) in classes and is_sealed(item) for item in value)
    ):
        return canonical_list_bytes([model_canonical_bytes(item) for item in value])
    return None


@sealed_memo
def model_canonical_bytes(model: BaseModel) -> bytes:
    """``canonical_bytes(model.model_dump(mode="json"))``, encoded once per sealed model.

    A sealed model cannot change, so its encoding is remembered with it; any
    other model (built in code and never sealed, or a copy) is encoded afresh.
    Either way, a field holding sealed models (or a tuple of them) takes their
    remembered encodings and only the rest of the model is dumped and encoded:
    a record rebuilt around sealed members never re-encodes a member.
    """

    composed: dict[str, bytes] = {}
    for name, classes in _composable_fields(type(model)).items():
        encoded = _sealed_field_bytes(model.__dict__.get(name), classes)
        if encoded is not None:
            composed[name] = encoded
    if not composed:
        return canonical_bytes(model.model_dump(mode="json"))
    return composed_canonical_bytes(model.model_dump(mode="json", exclude=set(composed)), composed)


def composed_canonical_bytes(payload: Mapping[str, object], encoded: Mapping[str, bytes]) -> bytes:
    """``canonical_bytes({**payload, **values})``, each of ``values`` given already encoded.

    An object's canonical encoding is its keys' and values' encodings joined in
    key order, and a list's is its items' joined in order, so a value that was
    already encoded once (verified bytes, ``model_canonical_bytes`` of a sealed
    model, or a list composed by ``canonical_list_bytes``) composes into the same
    bytes without being normalized or encoded again. Each entry of ``encoded``
    must be exactly ``canonical_bytes`` of its value; ``payload`` is normalized
    here.
    """

    normalized = normalize_canonical(dict(payload))
    assert isinstance(normalized, dict)
    members: dict[str, bytes] = {
        key: canonical_json(value).encode("utf-8") for key, value in normalized.items()
    }
    for raw_key, value in encoded.items():
        key = _normalize_string(raw_key)
        if key in members:
            raise CanonicalEncodingError(f"$: keys collide after NFC normalization: {key!r}")
        members[key] = value
    return (
        b"{"
        + b",".join(
            canonical_json(key).encode("utf-8") + b":" + members[key]
            for key in sorted(members, key=lambda name: name.encode("utf-8"))
        )
        + b"}"
    )


def composed_canonical_digest(
    domain: str,
    payload: Mapping[str, object],
    encoded: Mapping[str, bytes],
) -> str:
    """``canonical_digest(domain, {**payload, **values})``, each of ``values`` given encoded.

    The preimage is ``composed_canonical_bytes`` of the payload with the domain
    as its reserved ``tag``.
    """

    if "tag" in payload or "tag" in {_normalize_string(key) for key in encoded}:
        raise CanonicalEncodingError("payload must not supply the reserved tag field")
    return hashlib.sha256(
        composed_canonical_bytes(
            payload,
            {**encoded, "tag": canonical_json(_normalize_string(domain)).encode("utf-8")},
        )
    ).hexdigest()


def canonical_list_bytes(items: Sequence[bytes]) -> bytes:
    """The canonical encoding of a list from each item's own canonical bytes."""

    return b"[" + b",".join(items) + b"]"


def canonical_list_digest(domain: str, field: str, items: Sequence[bytes]) -> str:
    """``canonical_digest(domain, {field: values})`` from each value's own canonical bytes."""

    return composed_canonical_digest(domain, {}, {field: canonical_list_bytes(items)})


_DICTS = (dict, FrozenDict)
_LISTS = (list, FrozenList)


def _identical(left: object, right: object, depth: int = 0) -> bool:
    """Equal in content and in every value's kind, built only of plain ASCII-keyed JSON.

    Strings must be ASCII, integers and bools compare only with their own kind
    (``1 == True`` in Python, never in JSON), and dicts and lists only with
    dicts and lists (sealed ones included). One frame per nesting level; past
    ``_DIRECT_DEPTH`` the values are left to ``canonical_bytes`` (``False``).
    """

    kind = type(left)
    if kind is str:
        return type(right) is str and left == right and cast(str, left).isascii()
    if kind is int or kind is bool:
        return type(right) is kind and left == right
    if left is None:
        return right is None
    if depth >= _DIRECT_DEPTH:
        return False
    if kind in _DICTS:
        if type(right) not in _DICTS:
            return False
        mine = cast(dict[object, object], left)
        theirs = cast(dict[object, object], right)
        if len(mine) != len(theirs):
            return False
        for key, item in mine.items():
            if not (
                type(key) is str
                and key.isascii()
                and key in theirs
                and _identical(item, theirs[key], depth + 1)
            ):
                return False
        return True
    if kind in _LISTS:
        if type(right) not in _LISTS:
            return False
        mine_items = cast(list[object], left)
        their_items = cast(list[object], right)
        if len(mine_items) != len(their_items):
            return False
        for item, other in zip(mine_items, their_items):
            if not _identical(item, other, depth + 1):
                return False
        return True
    return False


def canonical_equal(left: object, right: object) -> bool:
    """``canonical_bytes(left) == canonical_bytes(right)``, encoding only when it must.

    Two values identical in content and kind, built only of ASCII strings,
    integers, bools, None, lists and ASCII-keyed dicts, have one canonical
    encoding (ASCII is its own NFC form and can neither collide nor hold a
    surrogate), so they are decided without encoding. Anything else --
    different values that might still share an encoding, or values holding
    other types -- is encoded and compared, refusing exactly as
    ``canonical_bytes`` does, as do values too deeply nested to compare.
    """

    try:
        if _identical(left, right):
            return True
    except RecursionError:
        pass
    return canonical_bytes(left) == canonical_bytes(right)


def pretty_canonical_bytes(value: object) -> bytes:
    """Encode one normalized value as sorted indent-2 JSON with one final LF."""

    if _canonical_ready(value):
        return pretty_json(value).encode("utf-8") + b"\n"
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


@dataclass(frozen=True)
class Sha256Value:
    """A kind-distinct SHA-256 value with an explicit algorithm tag."""

    value: str

    algorithm: ClassVar[str] = "sha256"
    kind: ClassVar[str] = "digest"

    def __post_init__(self) -> None:
        if not _valid_sha256_hex(self.value):
            raise ValueError(f"{self.kind} must be 32 bytes of lowercase SHA-256 hex")

    @property
    def tagged(self) -> str:
        """Return the public algorithm-tagged spelling."""

        return f"{self.algorithm}:{self.value}"

    @classmethod
    @lru_cache(maxsize=65536)
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


class _FileDigests:
    """Member digests by their exact bytes, bounded by entries and by bytes held.

    A new member's bytes travel by reference from the lowering that renders them
    through evaluation, the evaluated tree state, the Merkle update and the
    projection rows, and each of those layers takes the member's file digest. A
    ``bytes`` object caches its own hash, so every layer handed the same object
    finds the digest with one dict probe; equal bytes read back from Git find it
    with one comparison. Only exact ``bytes`` are kept (a subclass could compare
    or hash otherwise), and a value is an immutable digest shared by every
    caller.
    """

    _CAPACITY = 16384
    _MAX_BYTES = 32 * 1024 * 1024
    _MAX_ENTRY = 1024 * 1024

    def __init__(self) -> None:
        self._entries: OrderedDict[bytes, ArtifactDigest] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def digest(self, content: bytes) -> ArtifactDigest:
        if type(content) is not bytes or len(content) > self._MAX_ENTRY:
            return ArtifactDigest(hashlib.sha256(content).hexdigest())
        with self._lock:
            found = self._entries.get(content)
            if found is not None:
                self._entries.move_to_end(content)
                return found
        value = ArtifactDigest(hashlib.sha256(content).hexdigest())
        with self._lock:
            if content not in self._entries:
                self._entries[content] = value
                self._bytes += len(content)
            while self._entries and (
                len(self._entries) > self._CAPACITY or self._bytes > self._MAX_BYTES
            ):
                old, _digest = self._entries.popitem(last=False)
                self._bytes -= len(old)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


_FILE_DIGESTS = _FileDigests()


def file_digest(content: bytes) -> ArtifactDigest:
    """Hash exact artifact bytes independently of Git's object format, once per content."""

    return _FILE_DIGESTS.digest(content)


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
    previous_manifest: Mapping[str, str],
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


def manifest_root_from_members(manifest: Mapping[str, str]) -> SemanticManifestRoot:
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


CARD_NAMESPACE = "cards/"
"""The one spelling of the derivative-card sidecar namespace.

It lives here because the semantic projection has to exclude it and cannot
import the core renderer that owns the rest of the card law.
"""


def is_candidate_card_path(path: str) -> bool:
    """Return whether a ledger path belongs to the derivative card namespace."""

    return normalize_ledger_path(path).startswith(CARD_NAMESPACE)


def semantic_projection(tree: Mapping[str, bytes]) -> dict[str, bytes]:
    """Return Π(tree), excluding deterministic daemon records and derivative cards."""

    return {
        path: content
        for path, content in tree.items()
        if not (
            normalize_ledger_path(path).startswith("changesets/") or is_candidate_card_path(path)
        )
    }


def semantic_diff_from_members(
    base: Mapping[str, str],
    candidate: Mapping[str, str],
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
    "CARD_NAMESPACE",
    "ArtifactCodec",
    "ArtifactDigest",
    "CURRENT_ARTIFACT_CODEC",
    "P2_B0_ARTIFACT_CODEC",
    "artifact_bytes_for_codec",
    "is_candidate_card_path",
    "artifact_bytes_for_path",
    "artifact_path_for_codec",
    "artifact_path_matches",
    "AcceptanceLawDigest",
    "ApprovalDigest",
    "BootstrapRoot",
    "CandidateDigest",
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
    "canonical_equal",
    "canonical_list_bytes",
    "canonical_list_digest",
    "composed_canonical_bytes",
    "composed_canonical_digest",
    "file_digest",
    "manifest_for_tree",
    "manifest_for_tree_carrying",
    "manifest_root",
    "manifest_root_from_members",
    "model_canonical_bytes",
    "normalize_canonical",
    "normalize_ledger_path",
    "normalize_manifest_paths",
    "pretty_canonical_bytes",
    "semantic_diff",
    "semantic_diff_from_members",
    "semantic_projection",
    "typed_digest",
]
