"""SubjectKind: how a kind of Subject is identified, declared once and applied by core.

Identity is decided once, by declared rules, at one authoritative boundary
(maintainer ruling identity-design-1009, D1, D4, D6). Every new Subject is of a
kind with an accepted SubjectKind, and the SubjectKind says how core names it:

``keys``
    An ordered list of key ClaimTypes (one, or several for a composite key).
    Each is a ClaimType v8 about this kind only, literal and one-valued; its
    canonical form governs its key value. The key parts of a Subject are
    ordinary Claims of those ClaimTypes. ``uniqueness`` is ``unique`` (a second
    live Subject with the same canonical key is refused) or ``hint`` (never
    refused; equal keys surface in the duplicates audit).
``minted``
    Core mints an opaque id.

Authors never choose a Subject id. A keyed Subject's id is the readable
rendering of its canonical key values (``render_key_slug``); a minted Subject's
id is ``sub-`` and 32 hex digits. The id is a stable handle: it is never
re-derived when a key Claim later changes.

A SubjectKind changes through the usual definition succession: changing its keys
or its uniqueness is a governed successor, never a silent re-keying, and no
existing Subject's id moves.
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
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    canonical_bytes,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.semantic import SemanticAddress

SUBJECT_KIND_ARTIFACT_FORMAT: Final = "cruxible-subject-kind-v1"
SUBJECT_KIND_DIGEST_DOMAIN: Final = "cruxible-subject-kind-artifact-v1"
SUBJECT_KIND_PREFIX: Final = "subject-kinds/"
#: The most key ClaimTypes one composite key may name.
MAX_KEY_PARTS: Final = 8
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
"""A key part's value: a string, an integer or a boolean, as its Claim stores it."""


class SubjectKindKeys(_StrictSubjectKindModel):
    """A Subject of this kind is named by its key: one ClaimType, or several in order."""

    rule: Literal["keys"] = "keys"
    #: The key ClaimTypes by predicate, in key order.
    keys: tuple[str, ...] = Field(min_length=1, max_length=MAX_KEY_PARTS)
    #: ``unique`` refuses a second live Subject with the same canonical key;
    #: ``hint`` never refuses and feeds the duplicates audit.
    uniqueness: Literal["unique", "hint"]

    @field_validator("keys")
    @classmethod
    def _keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for predicate in value:
            if not _PREDICATE_RE.fullmatch(predicate):
                raise ValueError(f"key {predicate!r} is not a canonical ClaimType predicate")
        if len(set(value)) != len(value):
            raise ValueError("a SubjectKind names each key ClaimType once")
        return value


class SubjectKindMinted(_StrictSubjectKindModel):
    """A Subject of this kind is named by an opaque id core mints."""

    rule: Literal["minted"] = "minted"


SubjectKindIdentityRule: TypeAlias = Annotated[
    SubjectKindKeys | SubjectKindMinted, Field(discriminator="rule")
]


class SubjectKind(_StrictSubjectKindModel):
    """How Subjects of one kind are identified: by declared keys, or by minted ids."""

    artifact_format: Literal["cruxible-subject-kind-v1"] = SUBJECT_KIND_ARTIFACT_FORMAT
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


def parse_subject_kind(
    content: bytes, *, path: str, codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC
) -> SubjectKind:
    try:
        subject_kind = SubjectKind.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SubjectKindFormatError("SubjectKind failed strict validation") from exc
    if not artifact_path_matches(subject_kind_path(subject_kind.identity.name), path, codec=codec):
        raise SubjectKindFormatError("SubjectKind identity/path disagreement")
    if artifact_bytes_for_path(render_subject_kind(subject_kind), path, codec=codec) != content:
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
    kind: str, shape: KeyClaimTypeShape | None, predicate: str
) -> str | None:
    """Why one ClaimType cannot be a key of ``kind``, or None when it can."""

    if shape is None or not shape.live:
        return (
            f"key ClaimType {predicate!r} is not a live accepted ClaimType; accept it in this "
            "change or an earlier one"
        )
    if shape.artifact_format != "playbill-claim-type-v8":
        return f"key ClaimType {predicate!r} must be a ClaimType v8 (it declares a canonical form)"
    if shape.object_kind != "literal":
        return f"key ClaimType {predicate!r} must take a literal value"
    if shape.cardinality != "one":
        return f"key ClaimType {predicate!r} must be one-valued (cardinality one)"
    if shape.allowed_subject_kinds != (kind,):
        return f"key ClaimType {predicate!r} must be about {kind} only; it admits " + ", ".join(
            shape.allowed_subject_kinds
        )
    return None


def evaluate_subject_kind_law(
    subject_kind: SubjectKind,
    *,
    path: str,
    predecessor: AcceptedSubjectKind | None,
    key_claim_types: Mapping[str, KeyClaimTypeShape | None],
) -> SubjectKindLawResult:
    """Judge one SubjectKind against its predecessor and the key ClaimTypes it names.

    ``key_claim_types`` maps each key predicate to that ClaimType as the
    candidate holds it (None when absent). A retiring SubjectKind is judged only
    for its succession: it declares nothing any longer, and every Subject of its
    kind is grandfathered (readable, claimable, retirable, never re-created).
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
            problem = key_claim_type_problem(kind, key_claim_types.get(predicate), predicate)
            if problem is not None:
                return _refusal(
                    "cruxible.subject_kind.key_claim_type_invalid",
                    f"SubjectKind {kind}: {problem}.",
                    path=path,
                )
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


def _part_text(value: SubjectKeyValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return value


def render_key_part(value: SubjectKeyValue) -> str:
    """One canonical key value as an id part: NFKC, lowercase, runs to ``-``, trimmed."""

    text = unicodedata.normalize("NFKC", _part_text(value)).lower()
    return _SLUG_RUN_RE.sub("-", text).strip("-")


def key_digest_hex(values: Sequence[SubjectKeyValue]) -> str:
    """The SHA-256 hex digest of a canonical key tuple's canonical JSON bytes."""

    return hashlib.sha256(canonical_bytes(list(values))).hexdigest()


def render_key_slug(values: Sequence[SubjectKeyValue]) -> str:
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


def subject_id_candidates(values: Sequence[SubjectKeyValue]) -> Iterator[str]:
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


def assign_subject_id(values: Sequence[SubjectKeyValue], *, taken: Callable[[str], bool]) -> str:
    """The first id a key may take that names no Subject yet (``taken``)."""

    for candidate in subject_id_candidates(values):
        if not taken(candidate):
            return candidate
    raise SubjectIdUnassignable(
        f"every id the key {list(values)!r} may take already names a Subject"
    )


DISTINCT_HOLDER_DOMAIN: Final = "cruxible-distinct-key-holder-v1"


def distinct_holder_id(values: Sequence[SubjectKeyValue], *, operation: str, member: str) -> str:
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


def is_distinct_holder_id(subject_id: str, values: Sequence[SubjectKeyValue]) -> bool:
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


def key_value_problem(value: object) -> str | None:
    """Why a value cannot be a key part, or None: keys are strings, integers or booleans."""

    if isinstance(value, bool | int):
        return None
    if isinstance(value, str):
        return None if value else "a key part is never empty"
    return f"a key part is a string, an integer or a boolean, not {type(value).__name__}"


class SubjectKeyPart(_StrictSubjectKindModel):
    """One key part: the key ClaimType and its value."""

    predicate: str
    value: SubjectKeyValue

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        if not _PREDICATE_RE.fullmatch(value):
            raise ValueError(f"key {value!r} is not a canonical ClaimType predicate")
        return value

    @field_validator("value")
    @classmethod
    def _value(cls, value: SubjectKeyValue) -> SubjectKeyValue:
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


def ordered_key_parts(
    subject_kind: SubjectKind, key: Mapping[str, SubjectKeyValue]
) -> tuple[SubjectKeyPart, ...]:
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
    found: dict[str, SubjectKeyValue] = {}
    for name, value in key.items():
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
    return tuple(
        SubjectKeyPart(predicate=predicate, value=found[predicate])
        for predicate in subject_kind.keys
    )


__all__ = [
    "MAX_ID_ATTEMPTS",
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
    "SubjectKeyReference",
    "key_reference_mapping",
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
]
