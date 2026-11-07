"""SubjectKind: how a kind of Subject is identified, declared once and applied by core.

Identity is decided once, by declared rules, at one authoritative boundary
(maintainer ruling identity-design-1009, D1, D4, D6). Every new Subject is of a
kind with an accepted SubjectKind, and the SubjectKind says how core names it:

``keys``
    An ordered list of key parts (one, or several for a composite key). Each
    names a key ClaimType -- about this kind only, literal and one-valued --
    and the identity normalizer its value takes, pinned by version, from the
    fixed core set (``identity_normalizers``: ``none``, ``lowercase``,
    ``pep503``, ``purl-type``). A key part's canonical value is its ClaimType's
    canonical form, then its normalizer: two raw values that canonicalize to
    one value name the same Subject. The key parts of a Subject are ordinary
    Claims of those ClaimTypes, stating canonical values. ``uniqueness`` is
    ``unique`` (a second live Subject with the same canonical key is refused)
    or ``hint`` (never refused; equal keys surface in the duplicates audit).
``minted``
    Core mints an opaque id.

Authors never choose a Subject id. A keyed Subject's id is the readable
rendering of its canonical key values (``render_key_slug``); a minted Subject's
id is ``sub-`` and 32 hex digits. The id is a stable handle: it is never
re-derived when a key Claim later changes.

A key part may instead name a Subject of exactly one other kind (``subject<K>``):
the kind is then a *pair kind*, keyed by its ends. Such a part's key Claim is an
ordinary link Claim from the pair to the end, its canonical value the end's
Subject path (``SubjectEnd``), so an end's aliases and re-keys never move a
pair. A pair kind is ``unique``, its parts take normalizer ``none``, and the
kinds linked through Subject-valued parts form a DAG at most
``MAX_KEY_DEPTH`` deep. Its identity is frozen for life: every successor keeps
its ``identity_rule`` byte-equal, and a kind becomes a pair kind only while it
has no Subject at all.

A SubjectKind changes through the usual definition succession: changing its keys,
a key part's normalizer (or its version) or its uniqueness is a governed
successor, never a silent re-keying: a changed normalizer re-keys the kind, and
the same change re-authors every live key value the new rule moves. No existing
Subject's id moves.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    canonical_bytes,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.claims import SubjectClaimObject
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.identity_normalizers import (
    LOWERCASING_NORMALIZERS,
    KeyNormalizer,
    NoneNormalizer,
    normalizer_spelling,
)
from cruxible_client.contracts.identity_normalizers import (
    key_value_problem as key_value_problem,
)
from cruxible_client.contracts.semantic import SemanticAddress

SUBJECT_KIND_ARTIFACT_FORMAT: Final = "cruxible-subject-kind-v2"
SUBJECT_KIND_DIGEST_DOMAIN: Final = "cruxible-subject-kind-artifact-v2"
SUBJECT_KIND_PREFIX: Final = "subject-kinds/"
#: The most key ClaimTypes one composite key may name.
MAX_KEY_PARTS: Final = 8
#: How deep pair kinds may nest through Subject-valued key parts (a pair of
#: plain ends is depth 1).
MAX_KEY_DEPTH: Final = 4
#: Subject kinds a SubjectKind may never declare: the descriptor pseudo-kinds.
RESERVED_SUBJECT_KINDS: Final = frozenset({"semantic.subject", "semantic.claim_type"})

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*$")
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})+$")
_SUBJECT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
MINTED_SUBJECT_ID_RE: Final = re.compile(r"^sub-[0-9a-f]{32}$")
_DESCRIPTION_MAX: Final = 1024


class SubjectKindFormatError(FormatError):
    """A SubjectKind artifact or its successor transition is invalid."""


class _StrictSubjectKindModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


SubjectKeyValue: TypeAlias = str | int | bool
"""A literal key part's value: a string, an integer or a boolean, as its Claim stores it."""

_SUBJECT_PATH_RE = re.compile(
    r"^subjects/([a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*)/([a-z][a-z0-9_.-]{0,255})\.json$"
)


class SubjectEnd(_StrictSubjectKindModel):
    """A Subject-valued key part's canonical value: the end Subject's ledger path.

    The path is what the key Claim's object already stores, and a Subject's id
    never moves, so a pair keyed by its ends is immune to the ends' aliases,
    re-keys and normalizer changes. Its canonical JSON form inside a key is
    ``{"subject": path}``, which never equals a literal's.
    """

    path: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not _SUBJECT_PATH_RE.fullmatch(value):
            raise ValueError(f"{value!r} is not a Subject's ledger path")
        return value

    @property
    def subject_kind(self) -> str:
        match = _SUBJECT_PATH_RE.fullmatch(self.path)
        assert match is not None
        return match.group(1)

    @property
    def subject_id(self) -> str:
        match = _SUBJECT_PATH_RE.fullmatch(self.path)
        assert match is not None
        return match.group(2)


SubjectKeyPartValue: TypeAlias = SubjectKeyValue | SubjectEnd
"""A canonical key part: a literal, or (for a Subject-valued part) the end Subject."""


def key_part_json(value: SubjectKeyPartValue) -> object:
    """One canonical key part as canonical JSON reads it: a literal, or ``{"subject": path}``."""

    return {"subject": value.path} if isinstance(value, SubjectEnd) else value


class SubjectKeyRule(_StrictSubjectKindModel):
    """One key part: its key ClaimType, and the identity normalizer its value takes."""

    claim_type: str
    normalizer: KeyNormalizer

    @field_validator("claim_type")
    @classmethod
    def _claim_type(cls, value: str) -> str:
        if not _PREDICATE_RE.fullmatch(value):
            raise ValueError(f"key {value!r} is not a canonical ClaimType predicate")
        return value


class SubjectKindKeys(_StrictSubjectKindModel):
    """A Subject of this kind is named by its key: one part, or several in order."""

    rule: Literal["keys"] = "keys"
    #: The key parts, in key order: each a key ClaimType and its normalizer.
    parts: tuple[SubjectKeyRule, ...] = Field(min_length=1, max_length=MAX_KEY_PARTS)
    #: ``unique`` refuses a second live Subject with the same canonical key;
    #: ``hint`` never refuses and feeds the duplicates audit.
    uniqueness: Literal["unique", "hint"]

    @field_validator("parts")
    @classmethod
    def _parts(cls, value: tuple[SubjectKeyRule, ...]) -> tuple[SubjectKeyRule, ...]:
        predicates = [part.claim_type for part in value]
        if len(set(predicates)) != len(predicates):
            raise ValueError("a SubjectKind names each key ClaimType once")
        return value

    @property
    def keys(self) -> tuple[str, ...]:
        """The key ClaimTypes by predicate, in key order."""

        return tuple(part.claim_type for part in self.parts)


class SubjectKindMinted(_StrictSubjectKindModel):
    """A Subject of this kind is named by an opaque id core mints."""

    rule: Literal["minted"] = "minted"


SubjectKindIdentityRule: TypeAlias = Annotated[
    SubjectKindKeys | SubjectKindMinted, Field(discriminator="rule")
]


class SubjectKind(_StrictSubjectKindModel):
    """How Subjects of one kind are identified: by declared keys, or by minted ids."""

    artifact_format: Literal["cruxible-subject-kind-v2"] = SUBJECT_KIND_ARTIFACT_FORMAT
    identity: ArtifactIdentity
    description: str | None = Field(default=None, exclude_if=lambda value: value is None)
    identity_rule: SubjectKindIdentityRule
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if unicodedata.normalize("NFC", value) != value or value.strip() != value:
            raise ValueError("SubjectKind description must be NFC-normalized and trimmed")
        if not 1 <= len(value) <= _DESCRIPTION_MAX:
            raise ValueError(f"SubjectKind description must be 1..{_DESCRIPTION_MAX} characters")
        return value

    @model_validator(mode="after")
    def _shape(self) -> SubjectKind:
        if self.identity.kind != "SubjectKind" or not _KIND_RE.fullmatch(self.identity.name):
            raise ValueError("SubjectKind identity must be SubjectKind:<subject kind>")
        return self

    @property
    def pins(self) -> tuple[ArtifactPin, ...]:
        """A SubjectKind names its key ClaimTypes by identity and pins nothing."""

        return ()

    @property
    def subject_kind(self) -> str:
        """The Subject kind this declares."""

        return self.identity.name

    @property
    def keys(self) -> tuple[str, ...]:
        """The key ClaimTypes in key order; empty for a minted kind."""

        rule = self.identity_rule
        return rule.keys if isinstance(rule, SubjectKindKeys) else ()

    def normalizer(self, predicate: str) -> KeyNormalizer:
        """The identity normalizer key part ``predicate`` takes (``none`` off the key)."""

        rule = self.identity_rule
        if isinstance(rule, SubjectKindKeys):
            for part in rule.parts:
                if part.claim_type == predicate:
                    return part.normalizer
        return NoneNormalizer(version=1)

    @property
    def unique(self) -> bool:
        rule = self.identity_rule
        return isinstance(rule, SubjectKindKeys) and rule.uniqueness == "unique"

    @property
    def minted(self) -> bool:
        return isinstance(self.identity_rule, SubjectKindMinted)


def subject_kind_path(kind: str) -> str:
    if not _KIND_RE.fullmatch(kind):
        raise SubjectKindFormatError("SubjectKind is not path-addressable")
    return f"{SUBJECT_KIND_PREFIX}{kind}.json"


def render_subject_kind(subject_kind: SubjectKind) -> bytes:
    return pretty_canonical_bytes(subject_kind.model_dump(mode="json"))


def subject_kind_digest(subject_kind: SubjectKind) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest, SUBJECT_KIND_DIGEST_DOMAIN, subject_kind.model_dump(mode="json")
    )


def parse_subject_kind(content: bytes, *, path: str) -> SubjectKind:
    try:
        subject_kind = SubjectKind.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SubjectKindFormatError("SubjectKind failed strict validation") from exc
    if subject_kind_path(subject_kind.identity.name) != path:
        raise SubjectKindFormatError("SubjectKind identity/path disagreement")
    if render_subject_kind(subject_kind) != content:
        raise SubjectKindFormatError("SubjectKind is not in canonical wire form")
    return subject_kind


def live_subject_kind(tree: Mapping[str, bytes], kind: str) -> SubjectKind | None:
    """The live SubjectKind declaring ``kind`` in ``tree``, if one is accepted there."""

    try:
        path = subject_kind_path(kind)
    except SubjectKindFormatError:
        return None
    content = tree.get(path)
    if content is None:
        return None
    declared = parse_subject_kind(content, path=path)
    return declared if declared.lifecycle.state == "live" else None


class AcceptedSubjectKind(_StrictSubjectKindModel):
    path: str
    subject_kind: SubjectKind
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> AcceptedSubjectKind:
        if self.path != subject_kind_path(self.subject_kind.identity.name):
            raise ValueError("accepted SubjectKind path does not reproduce")
        if self.artifact_digest != subject_kind_digest(self.subject_kind).tagged:
            raise ValueError("accepted SubjectKind digest does not reproduce")
        return self


class SubjectKindLawResult(_StrictSubjectKindModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


class KeyClaimTypeShape(_StrictSubjectKindModel):
    """What the SubjectKind law reads of one key ClaimType in the candidate state."""

    predicate: str
    live: bool
    artifact_format: str
    object_kind: str
    cardinality: str
    allowed_subject_kinds: tuple[str, ...]
    #: The JSON type its literal schema declares (``string``, ``integer``...), if any.
    literal_type: str | None = None
    #: Its effective canonical form (the transforms applied before the normalizer).
    canonical_form: tuple[str, ...] = ()
    #: The kinds a Subject-valued ClaimType's object may be (empty for a literal).
    object_subject_kinds: tuple[str, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> SubjectKindLawResult:
    return SubjectKindLawResult(
        verdict="refused",
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity="error",
                message=message,
                subject=SemanticAddress.whole_artifact(path),
            ),
        ),
    )


def key_claim_type_problem(
    kind: str,
    shape: KeyClaimTypeShape | None,
    predicate: str,
    normalizer: KeyNormalizer | None = None,
) -> str | None:
    """Why one ClaimType cannot be a key of ``kind`` under ``normalizer``, or None when it can.

    A key ClaimType is literal, or Subject-valued naming exactly one other kind
    (a pair kind's end). A normalizer other than ``none`` reads text, so its key
    ClaimType states strings; a lowercasing normalizer cannot follow an
    ``upper`` canonical form (the two rules would never agree on one value). A
    Subject-valued part takes ``none``: its value is a Subject path.
    """

    if shape is None or not shape.live:
        return (
            f"key ClaimType {predicate!r} is not a live accepted ClaimType; accept it in this "
            "change or an earlier one"
        )
    if shape.object_kind == "subject":
        ends = shape.object_subject_kinds
        if len(ends) != 1:
            return (
                f"key ClaimType {predicate!r} names Subjects of "
                + (", ".join(ends) or "no kind")
                + "; a Subject-valued key part names exactly one kind, not a union"
            )
        if ends[0] == kind:
            return (
                f"key ClaimType {predicate!r} names a Subject of {kind} itself; a pair's "
                "ends are of other kinds"
            )
    elif shape.object_kind != "literal":
        return f"key ClaimType {predicate!r} must take a literal value or one kind of Subject"
    if shape.cardinality != "one":
        return f"key ClaimType {predicate!r} must be one-valued (cardinality one)"
    if shape.allowed_subject_kinds != (kind,):
        return f"key ClaimType {predicate!r} must be about {kind} only; it admits " + ", ".join(
            shape.allowed_subject_kinds
        )
    if normalizer is not None and not isinstance(normalizer, NoneNormalizer):
        spelled = normalizer_spelling(normalizer)
        if shape.object_kind == "subject":
            return (
                f"key ClaimType {predicate!r} names a Subject, whose path {spelled} cannot "
                "normalize; give it normalizer none"
            )
        if shape.literal_type != "string":
            return (
                f"key ClaimType {predicate!r} states {shape.literal_type or 'untyped'} values, "
                f"and {spelled} normalizes text; give it a string literal schema or "
                "normalizer none"
            )
        if normalizer.normalizer in LOWERCASING_NORMALIZERS and "upper" in shape.canonical_form:
            return (
                f"key ClaimType {predicate!r} has canonical form upper, which {spelled} "
                "undoes; drop one of them"
            )
    return None


def subject_key_ends(
    subject_kind: SubjectKind, key_claim_types: Mapping[str, KeyClaimTypeShape | None]
) -> dict[str, str]:
    """Each Subject-valued key part of ``subject_kind``: its predicate and its end kind."""

    ends: dict[str, str] = {}
    for predicate in subject_kind.keys:
        shape = key_claim_types.get(predicate)
        if shape is not None and shape.object_kind == "subject" and shape.object_subject_kinds:
            ends[predicate] = shape.object_subject_kinds[0]
    return ends


def _pair_kind_problem(
    subject_kind: SubjectKind,
    ends: Mapping[str, str],
    *,
    subject_kind_of: Callable[[str], SubjectKind | None] | None,
    key_shape_of: Callable[[str], KeyClaimTypeShape | None] | None,
) -> tuple[str, str] | None:
    """Why a pair kind's ends cannot key it: ``(code, message)``, or None.

    Every end kind has a live SubjectKind; the kinds linked through
    Subject-valued key parts are acyclic and at most ``MAX_KEY_DEPTH`` deep. The
    walk reads each reachable kind once (at most ``MAX_KEY_PARTS`` parts each).
    """

    kind = subject_kind.subject_kind
    invalid = "cruxible.subject_kind.key_claim_type_invalid"
    if not subject_kind.unique:
        return (
            invalid,
            f"SubjectKind {kind} has a Subject-valued key part, so it is a pair kind, and a "
            "pair kind is unique: one pair per set of ends (uniqueness unique).",
        )
    heights: dict[str, int] = {}

    def ends_of(current: str) -> dict[str, str] | tuple[str, str]:
        if current == kind:
            return dict(ends)
        declared = None if subject_kind_of is None else subject_kind_of(current)
        if declared is None or declared.lifecycle.state != "live":
            return (
                invalid,
                f"SubjectKind {kind}: its key reaches Subjects of {current}, which no live "
                "SubjectKind declares; accept one in this change or an earlier one.",
            )
        if key_shape_of is None:
            return {}
        return subject_key_ends(
            declared, {predicate: key_shape_of(predicate) for predicate in declared.keys}
        )

    def height(current: str, chain: tuple[str, ...]) -> int | tuple[str, str]:
        # ``chain`` is the kinds from the judged kind down to ``current``; a
        # kind's height (the longest chain of ends below it) is its own, so it
        # is computed once.
        if current in heights:
            return heights[current]
        found = ends_of(current)
        if isinstance(found, tuple):
            return found
        tallest = 0
        for end in found.values():
            if end in chain:
                cycle = " -> ".join((*chain[chain.index(end) :], end))
                return (
                    "cruxible.subject_kind.key_cycle",
                    f"SubjectKind {kind}: kinds keyed through Subject-valued key parts form a "
                    f"cycle ({cycle}); a pair's ends are never, through any chain, the pair "
                    "kind itself.",
                )
            if len(chain) > MAX_KEY_DEPTH:
                # Already deeper than any pair kind may nest: refused below,
                # without walking further.
                return MAX_KEY_DEPTH + 1
            below = height(end, (*chain, end))
            if isinstance(below, tuple):
                return below
            tallest = max(tallest, below + 1)
        heights[current] = tallest
        return tallest

    result = height(kind, (kind,))
    if isinstance(result, tuple):
        return result
    if result > MAX_KEY_DEPTH:
        return (
            invalid,
            f"SubjectKind {kind}: pair kinds nest more than {MAX_KEY_DEPTH} deep through "
            "Subject-valued key parts.",
        )
    return None


def evaluate_subject_kind_law(
    subject_kind: SubjectKind,
    *,
    path: str,
    predecessor: AcceptedSubjectKind | None,
    key_claim_types: Mapping[str, KeyClaimTypeShape | None],
    subject_kind_of: Callable[[str], SubjectKind | None] | None = None,
    key_shape_of: Callable[[str], KeyClaimTypeShape | None] | None = None,
) -> SubjectKindLawResult:
    """Judge one SubjectKind against its predecessor and the key ClaimTypes it names.

    ``key_claim_types`` maps each key predicate to that ClaimType as the
    candidate holds it (None when absent). A retiring SubjectKind is judged only
    for its succession: it declares nothing any longer, and every Subject of its
    kind is grandfathered (readable, claimable, retirable, never re-created).

    A Subject-valued key part makes the kind a pair kind: ``subject_kind_of``
    and ``key_shape_of`` read other kinds' SubjectKinds and key ClaimTypes in
    the candidate, so the law can see that every end kind is declared and that
    the kinds are acyclic and shallow (``key_cycle``). Without them a pair kind
    refuses. Freezing a pair kind's identity across succession needs the
    parent's Subjects and is judged at the candidate boundary
    (``endpoint_declaration_fixed``).
    """

    kind = subject_kind.identity.name
    if path != subject_kind_path(kind):
        return _refusal(
            "cruxible.subject_kind.path_mismatch",
            "SubjectKind identity/path disagreement.",
            path=path,
        )
    if kind in RESERVED_SUBJECT_KINDS:
        return _refusal(
            "cruxible.subject_kind.reserved",
            f"{kind} is a descriptor pseudo-kind, never the kind of a Subject.",
            path=path,
        )
    if predecessor is None:
        if subject_kind.lifecycle.predecessor_digest is not None:
            return _refusal(
                "cruxible.subject_kind.invalid_genesis",
                "A new SubjectKind cannot name a predecessor.",
                path=path,
            )
    else:
        if subject_kind.identity != predecessor.subject_kind.identity:
            return _refusal(
                "cruxible.subject_kind.stable_identity_changed",
                "A SubjectKind successor must retain its stable identity.",
                path=path,
            )
        if subject_kind.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "cruxible.subject_kind.predecessor_mismatch",
                "A SubjectKind successor does not pin its exact predecessor.",
                path=path,
            )
        if (
            subject_kind.lifecycle.state == "live"
            and predecessor.subject_kind.lifecycle.state == "retired"
        ):
            return _refusal(
                "cruxible.subject_kind.revival_refused",
                "A retired SubjectKind cannot be revived.",
                path=path,
            )
    if subject_kind.lifecycle.state == "live":
        for predicate in subject_kind.keys:
            problem = key_claim_type_problem(
                kind,
                key_claim_types.get(predicate),
                predicate,
                subject_kind.normalizer(predicate),
            )
            if problem is not None:
                return _refusal(
                    "cruxible.subject_kind.key_claim_type_invalid",
                    f"SubjectKind {kind}: {problem}.",
                    path=path,
                )
        ends = subject_key_ends(subject_kind, key_claim_types)
        if ends:
            pair_problem = _pair_kind_problem(
                subject_kind, ends, subject_kind_of=subject_kind_of, key_shape_of=key_shape_of
            )
            if pair_problem is not None:
                return _refusal(pair_problem[0], pair_problem[1], path=path)
    return SubjectKindLawResult(
        verdict="accepted",
        artifact_digest=subject_kind_digest(subject_kind).tagged,
        required_tier="governed_write",
    )


# -- ids --------------------------------------------------------------------------

#: Every maximal run of characters outside this set becomes one ``-``.
_SLUG_RUN_RE = re.compile(r"[^a-z0-9.]+")
_SUBJECT_ID_MAX = 256
_SUFFIX_HEX = 8
#: How many suffixed spellings an id assignment tries before refusing.
MAX_ID_ATTEMPTS: Final = 64


def _part_text(value: SubjectKeyPartValue) -> str:
    if isinstance(value, SubjectEnd):
        return value.subject_id
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return value


def render_key_part(value: SubjectKeyPartValue) -> str:
    """One canonical key value as an id part: NFKC, lowercase, runs to ``-``, trimmed.

    A Subject-valued part renders as the end's id, which is already a slug.
    """

    if isinstance(value, SubjectEnd):
        return value.subject_id
    text = unicodedata.normalize("NFKC", _part_text(value)).lower()
    return _SLUG_RUN_RE.sub("-", text).strip("-")


def key_digest_hex(values: Sequence[SubjectKeyPartValue]) -> str:
    """The SHA-256 hex digest of a canonical key tuple's canonical JSON bytes."""

    return hashlib.sha256(canonical_bytes([key_part_json(value) for value in values])).hexdigest()


def render_key_slug(values: Sequence[SubjectKeyPartValue]) -> str:
    """The readable rendering of a canonical key tuple, a valid Subject id.

    Each part is rendered by ``render_key_part``; composite parts are joined
    with ``--``. A rendering that does not begin with a letter gets an ``x``
    prefix. A rendering with no readable character left is ``x-`` and the first
    8 hex digits of the key digest. One longer than a Subject id may be is cut
    and ends ``-`` and those 8 hex digits.
    """

    if not values:
        raise ValueError("a key has at least one part")
    slug = "--".join(render_key_part(value) for value in values)
    if not slug.strip("-"):
        return "x-" + key_digest_hex(values)[:_SUFFIX_HEX]
    if not ("a" <= slug[0] <= "z"):
        slug = "x" + slug
    if len(slug) > _SUBJECT_ID_MAX:
        keep = _SUBJECT_ID_MAX - _SUFFIX_HEX - 1
        slug = slug[:keep].rstrip("-") + "-" + key_digest_hex(values)[:_SUFFIX_HEX]
    if not _SUBJECT_ID_RE.fullmatch(slug):  # pragma: no cover - the rules above ensure it
        raise ValueError(f"key rendering {slug!r} is not a Subject id")
    return slug


def subject_id_candidates(values: Sequence[SubjectKeyPartValue]) -> Iterator[str]:
    """The ids a key may take, in order: the rendering, then it suffixed.

    When two different canonical keys render to the same slug, the later one
    takes ``<slug>-<8 hex of the key digest>``, and ``-2``, ``-3``, ... after that
    should those be taken too. (A ``hint`` kind's further holder of the very same
    key is named by ``distinct_holder_id`` instead.)
    """

    slug = render_key_slug(values)
    yield slug
    suffix = key_digest_hex(values)[:_SUFFIX_HEX]
    base = slug if len(slug) + 1 + _SUFFIX_HEX <= _SUBJECT_ID_MAX else slug[: _SUBJECT_ID_MAX - 12]
    base = base.rstrip("-")
    yield f"{base}-{suffix}"
    for attempt in range(2, MAX_ID_ATTEMPTS):
        yield f"{base}-{suffix}-{attempt}"


class SubjectIdUnassignable(ValueError):
    """Every spelling a key may take is already a Subject's id."""


def assign_subject_id(
    values: Sequence[SubjectKeyPartValue], *, taken: Callable[[str], bool]
) -> str:
    """The first id a key may take that names no Subject yet (``taken``)."""

    for candidate in subject_id_candidates(values):
        if not taken(candidate):
            return candidate
    raise SubjectIdUnassignable(
        f"every id the key {[key_part_json(value) for value in values]!r} may take already "
        "names a Subject"
    )


DISTINCT_HOLDER_DOMAIN: Final = "cruxible-distinct-key-holder-v1"


def distinct_holder_id(
    values: Sequence[SubjectKeyPartValue], *, operation: str, member: str
) -> str:
    """The id of a further Subject holding a key another Subject of a ``hint`` kind holds.

    Only an explicit "create new" makes one; it cannot reuse the key's rendering,
    so it takes ``<rendering>-`` and the first 8 hex digits of a digest of the
    operation creating it and the member, deterministic for that change.
    """

    digest = typed_digest(
        ArtifactDigest, DISTINCT_HOLDER_DOMAIN, {"member": member, "operation": operation}
    ).tagged.removeprefix("sha256:")
    slug = render_key_slug(values)
    base = slug if len(slug) + 1 + _SUFFIX_HEX <= _SUBJECT_ID_MAX else slug[: _SUBJECT_ID_MAX - 9]
    return f"{base.rstrip('-')}-{digest[:_SUFFIX_HEX]}"


def is_distinct_holder_id(subject_id: str, values: Sequence[SubjectKeyPartValue]) -> bool:
    """Whether ``subject_id`` has the form of a further holder of this key."""

    slug = render_key_slug(values)
    base = slug if len(slug) + 1 + _SUFFIX_HEX <= _SUBJECT_ID_MAX else slug[: _SUBJECT_ID_MAX - 9]
    return re.fullmatch(re.escape(base.rstrip("-")) + r"-[0-9a-f]{8}", subject_id) is not None


MINTED_SUBJECT_ID_DOMAIN: Final = "cruxible-minted-subject-id-v1"


def minted_subject_id(*, operation: str, member: str) -> str:
    """The opaque id core mints for one minted Subject, from the operation creating it.

    The same pattern the Procedure proposal terminal uses for Claim IDs: a
    domain-separated digest of the operation's identity (an authoring intent, or
    an admitted run's terminal item) and the member it creates, so a retry
    lowers to the same id.
    """

    digest = typed_digest(
        ArtifactDigest, MINTED_SUBJECT_ID_DOMAIN, {"member": member, "operation": operation}
    ).tagged.removeprefix("sha256:")
    return f"sub-{digest[:32]}"


# -- key values and references -------------------------------------------------------


_KEY_PART_SCHEMES: Final = frozenset({"artifact-v1", "subject-creation-v1", "subject-key-v1"})


class SubjectKeyPart(_StrictSubjectKindModel):
    """One key part: the key ClaimType and its value.

    A literal part's value is a string, an integer or a boolean. A
    Subject-valued part (a pair kind's end) names the end as a Subject Claim
    object, by one of the existing address forms: the Subject itself
    (``artifact-v1``), the Subject a creation member of the same change lands on
    (``subject-creation-v1``), or the Subject its kind's key names
    (``subject-key-v1``). Lowering lands it on the end's path, which is the
    part's canonical value.
    """

    predicate: str
    value: SubjectKeyValue | SubjectClaimObject

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        if not _PREDICATE_RE.fullmatch(value):
            raise ValueError(f"key {value!r} is not a canonical ClaimType predicate")
        return value

    @field_validator("value")
    @classmethod
    def _value(
        cls, value: SubjectKeyValue | SubjectClaimObject
    ) -> SubjectKeyValue | SubjectClaimObject:
        if isinstance(value, SubjectClaimObject):
            address = value.address
            if address.selector.scheme not in _KEY_PART_SCHEMES or (
                address.selector.scheme == "artifact-v1"
                and not (
                    address.artifact_path.startswith("subjects/") and not address.selector.value
                )
            ):
                raise ValueError(
                    "a Subject-valued key part names a whole Subject, the Subject a creation "
                    "member of the same change lands on, or the Subject a key names"
                )
            return value
        problem = key_value_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class SubjectKeyReference(_StrictSubjectKindModel):
    """A Subject named by its kind's key: the one holding it, or the one a change creates.

    A Procedure item names a Subject this way when the run has a key value but no
    id: it resolves through the key index (and the aliases) to the existing
    Subject, or to the Subject an item of the same terminal creates with that
    key. ``key`` is the bare value for a one-key kind, or maps each key ClaimType
    (by predicate, or by its last name under the kind) to its value. Both use
    the key's canonical form and lower to the same governed mapping.
    """

    subject_kind: str
    key: (
        Annotated[dict[str, SubjectKeyValue], Field(min_length=1, max_length=MAX_KEY_PARTS)]
        | SubjectKeyValue
    )

    @field_validator("subject_kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if not _KIND_RE.fullmatch(value):
            raise ValueError("subject_kind is not a canonical Subject kind")
        return value

    @field_validator("key", mode="before")
    @classmethod
    def _key(cls, value: object) -> object:
        for item in value.values() if isinstance(value, dict) else (value,):
            problem = key_value_problem(item)
            if problem is not None:
                raise ValueError(problem)
        return value


def key_reference_mapping(
    subject_kind: SubjectKind, key: Mapping[str, object] | SubjectKeyValue
) -> dict[str, object]:
    """Expand a bare reference at the boundary; preserve existing mapping bytes.

    The short key name is the ordinary authoring spelling. A composite kind
    requires a mapping, with every declared part named in the repair.
    """

    if isinstance(key, Mapping):
        return dict(key)
    if len(subject_kind.keys) != 1:
        shape = {predicate: "<value>" for predicate in subject_kind.keys}
        raise ValueError(f"a bare key value names only a one-key kind; give key={shape!r}")
    predicate = subject_kind.keys[0]
    prefix = subject_kind.subject_kind + "."
    return {predicate.removeprefix(prefix): key}


def key_predicate(subject_kind: SubjectKind, name: str) -> str | None:
    """The key predicate ``name`` spells for this kind: the predicate, or its last name."""

    for predicate in subject_kind.keys:
        if name == predicate or (
            predicate.startswith(subject_kind.subject_kind + ".")
            and name == predicate[len(subject_kind.subject_kind) + 1 :]
        ):
            return predicate
    return None


def ordered_key_values(
    subject_kind: SubjectKind, key: Mapping[str, SubjectKeyPartValue]
) -> tuple[tuple[str, SubjectKeyPartValue], ...]:
    """The key in the kind's key order; a missing, unknown or repeated part refuses.

    Raises ``ValueError`` naming the problem; the caller types it to its surface.
    """

    if subject_kind.minted:
        if key:
            raise ValueError(
                f"{subject_kind.subject_kind} is minted: core names its Subjects, so a "
                "creation supplies no key"
            )
        return ()
    found: dict[str, SubjectKeyPartValue] = {}
    for name, value in key.items():
        problem = None if isinstance(value, SubjectEnd) else key_value_problem(value)
        if problem is not None:
            raise ValueError(problem)
        predicate = key_predicate(subject_kind, name)
        if predicate is None:
            raise ValueError(
                f"{name!r} is not a key of {subject_kind.subject_kind}; its keys are "
                + ", ".join(subject_kind.keys)
            )
        if predicate in found:
            raise ValueError(f"key {predicate!r} is given twice")
        found[predicate] = value
    missing = [predicate for predicate in subject_kind.keys if predicate not in found]
    if missing:
        raise ValueError(
            f"a {subject_kind.subject_kind} is named by "
            + ", ".join(subject_kind.keys)
            + "; missing "
            + ", ".join(missing)
        )
    return tuple((predicate, found[predicate]) for predicate in subject_kind.keys)


def ordered_key_parts(
    subject_kind: SubjectKind, key: Mapping[str, SubjectKeyValue]
) -> tuple[SubjectKeyPart, ...]:
    """The public typed parts, over the same canonical key validation."""
    return tuple(
        SubjectKeyPart(predicate=predicate, value=value)  # type: ignore[arg-type]
        for predicate, value in ordered_key_values(subject_kind, key)
    )


__all__ = [
    "MAX_ID_ATTEMPTS",
    "MAX_KEY_DEPTH",
    "MAX_KEY_PARTS",
    "MINTED_SUBJECT_ID_DOMAIN",
    "MINTED_SUBJECT_ID_RE",
    "RESERVED_SUBJECT_KINDS",
    "SUBJECT_KIND_ARTIFACT_FORMAT",
    "SUBJECT_KIND_PREFIX",
    "AcceptedSubjectKind",
    "KeyClaimTypeShape",
    "SubjectIdUnassignable",
    "SubjectKeyPart",
    "SubjectKeyRule",
    "SubjectKeyReference",
    "key_reference_mapping",
    "SubjectEnd",
    "SubjectKeyPartValue",
    "SubjectKeyValue",
    "SubjectKind",
    "SubjectKindFormatError",
    "SubjectKindIdentityRule",
    "SubjectKindKeys",
    "SubjectKindLawResult",
    "SubjectKindMinted",
    "assign_subject_id",
    "distinct_holder_id",
    "is_distinct_holder_id",
    "evaluate_subject_kind_law",
    "key_claim_type_problem",
    "key_digest_hex",
    "key_part_json",
    "key_predicate",
    "key_value_problem",
    "live_subject_kind",
    "minted_subject_id",
    "ordered_key_parts",
    "parse_subject_kind",
    "render_key_part",
    "render_key_slug",
    "render_subject_kind",
    "subject_id_candidates",
    "subject_kind_digest",
    "subject_kind_path",
    "subject_key_ends",
]
