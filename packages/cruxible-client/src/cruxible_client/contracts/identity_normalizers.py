"""SubjectKind identity normalizers: the fixed core set a key part names (ruling D4, R12).

A keyed SubjectKind names, per key part, one normalizer from this closed set
and pins its version. Core applies it mechanically after the key ClaimType's
canonical form; the result is the key part's canonical value, the one its key
Claim stores and every key comparison uses. Two raw values that normalize to
one value name the same Subject. There is no configurable normalization: a
rule is one of these names at one version, and changing a key part's rule is
a SubjectKind successor that re-keys explicitly (never silently).

``none`` (v1)
    The value as it is.
``lowercase`` (v1)
    Unicode NFC, then the Unicode default lowercase mapping (``str.lower``),
    then NFC again.
``pep503`` (v1)
    A Python package name (PEP 508 ``^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$``)
    in its PEP 503 normal form: every run of ``-``, ``_`` and ``.`` becomes one
    ``-``, then ASCII lowercase. A value that is not such a name is refused.
``purl-type`` (v1)
    A package URL (``pkg:type/namespace/name@version?qualifiers#subpath``),
    parsed by the purl-spec rules and re-spelled canonically, with per-type
    rules (``PURL_TYPE_RULES_V1``). ``identity: package`` excludes the
    qualifiers from the key (``pkg:deb/debian/curl@8.0?arch=amd64`` and
    ``...?arch=arm64`` name one package release); ``identity: package_variant``
    keeps them, sorted, so a variant is (package, qualifiers).

A value a normalizer cannot read is refused with ``KeyNormalizationRefused``
(problem code ``key_unparseable``), never guessed: an ambiguous purl (two
values for one qualifier) refuses too.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Final, Literal, TypeAlias
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Every normalizer core implements, and the versions of each.
NORMALIZER_VERSIONS: Final[dict[str, tuple[int, ...]]] = {
    "none": (1,),
    "lowercase": (1,),
    "pep503": (1,),
    "purl-type": (1,),
}
#: Normalizers whose output is lowercase text (they conflict with an ``upper`` form).
LOWERCASING_NORMALIZERS: Final = frozenset({"lowercase", "pep503", "purl-type"})


class KeyNormalizationRefused(ValueError):
    """A key value its part's normalizer cannot read; ``code`` is the problem code."""

    code: Literal["key_unparseable"] = "key_unparseable"


class _StrictNormalizerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


def _version(name: str, value: int) -> int:
    if value not in NORMALIZER_VERSIONS[name]:
        implemented = ", ".join(str(item) for item in NORMALIZER_VERSIONS[name])
        raise ValueError(f"core implements {name} version {implemented}, not {value}")
    return value


class NoneNormalizer(_StrictNormalizerModel):
    """The key part's value as it is (after its ClaimType's canonical form)."""

    normalizer: Literal["none"] = "none"
    version: int

    _v = field_validator("version")(lambda value: _version("none", value))


class LowercaseNormalizer(_StrictNormalizerModel):
    """NFC, Unicode default lowercase, NFC."""

    normalizer: Literal["lowercase"] = "lowercase"
    version: int

    _v = field_validator("version")(lambda value: _version("lowercase", value))


class Pep503Normalizer(_StrictNormalizerModel):
    """A Python package name in its PEP 503 normal form."""

    normalizer: Literal["pep503"] = "pep503"
    version: int

    _v = field_validator("version")(lambda value: _version("pep503", value))


class PurlTypeNormalizer(_StrictNormalizerModel):
    """A package URL in canonical form under per-type rules; qualifiers kept or excluded."""

    normalizer: Literal["purl-type"] = "purl-type"
    version: int
    #: ``package`` excludes qualifiers from the key; ``package_variant`` keeps them.
    identity: Literal["package", "package_variant"]

    _v = field_validator("version")(lambda value: _version("purl-type", value))


KeyNormalizer: TypeAlias = Annotated[
    NoneNormalizer | LowercaseNormalizer | Pep503Normalizer | PurlTypeNormalizer,
    Field(discriminator="normalizer"),
]
NO_NORMALIZER: Final = NoneNormalizer(version=1)


def normalizer_spelling(rule: KeyNormalizer) -> str:
    """``name@version`` (and the purl identity), for messages and review."""

    text = f"{rule.normalizer}@{rule.version}"
    if isinstance(rule, PurlTypeNormalizer):
        text += f" ({rule.identity})"
    return text


# -- lowercase, pep503 -------------------------------------------------------------------


def lowercase_v1(text: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).lower())


_PEP508_NAME: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_PEP503_RUN: Final = re.compile(r"[-_.]+")


def pep503_v1(text: str) -> str:
    if not _PEP508_NAME.fullmatch(text):
        raise KeyNormalizationRefused(f"{text!r} is not a Python package name (PEP 508)")
    return _PEP503_RUN.sub("-", text).lower()


# -- purl-type ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PurlTypeRule:
    """How purl-type folds one type's components: lowercase, or a PEP 503 name."""

    lower_namespace: bool = False
    lower_name: bool = False
    pep503_name: bool = False


_LOWER_BOTH: Final = PurlTypeRule(lower_namespace=True, lower_name=True)
#: purl-type v1's per-type rules, from the purl-spec type definitions. A type
#: not listed keeps the case of every component (its names are case-sensitive,
#: or the spec states no rule): two spellings stay two Subjects, never merged.
PURL_TYPE_RULES_V1: Final[dict[str, PurlTypeRule]] = {
    "alpm": _LOWER_BOTH,
    "apk": _LOWER_BOTH,
    "bitbucket": _LOWER_BOTH,
    "composer": _LOWER_BOTH,
    "deb": _LOWER_BOTH,
    "github": _LOWER_BOTH,
    "hex": _LOWER_BOTH,
    "npm": _LOWER_BOTH,
    "pypi": PurlTypeRule(pep503_name=True),
    "rpm": PurlTypeRule(lower_namespace=True),
}
_PURL_TYPE: Final = re.compile(r"^[a-z.+-][a-z0-9.+-]*$")
_QUALIFIER_KEY: Final = re.compile(r"^[a-z.\-_][a-z0-9.\-_]*$")
#: Characters never percent-encoded in a canonical purl component (RFC 3986
#: unreserved, and ``:``, which the purl spec never encodes).
_PURL_SAFE: Final = "-._~:"


@dataclass(frozen=True)
class PurlIdentity:
    """One package URL read under purl-type v1: its package and variant spellings."""

    type: str
    namespace: tuple[str, ...]
    name: str
    version: str | None
    qualifiers: tuple[tuple[str, str], ...]
    subpath: tuple[str, ...]

    def _spell(self, *, qualifiers: bool) -> str:
        text = f"pkg:{self.type}/"
        if self.namespace:
            text += "/".join(_encode(part) for part in self.namespace) + "/"
        text += _encode(self.name)
        if self.version is not None:
            text += "@" + _encode(self.version)
        if qualifiers and self.qualifiers:
            text += "?" + "&".join(f"{key}={_encode(value)}" for key, value in self.qualifiers)
        if self.subpath:
            text += "#" + "/".join(_encode(part) for part in self.subpath)
        return text

    @property
    def package(self) -> str:
        """The canonical purl without its qualifiers: the package (release) identity."""

        return self._spell(qualifiers=False)

    @property
    def variant(self) -> str:
        """The canonical purl with its sorted qualifiers: (package, qualifiers)."""

        return self._spell(qualifiers=True)


def _encode(text: str) -> str:
    return quote(text, safe=_PURL_SAFE)


def _decode(text: str, *, what: str) -> str:
    try:
        decoded = unquote(text, errors="strict")
    except UnicodeDecodeError as exc:
        raise KeyNormalizationRefused(f"the purl {what} is not UTF-8 when decoded") from exc
    return unicodedata.normalize("NFC", decoded)


def parse_purl_v1(text: str) -> PurlIdentity:
    """Read one package URL by the purl-spec parse order, under purl-type v1's rules.

    ``#`` splits the subpath (empty, ``.`` and ``..`` segments drop), ``?`` the
    qualifiers (keys lowercased, empty values dropped, a repeated key refused),
    ``:`` the ``pkg`` scheme, then the type (lowercased), the version (after the
    last ``@`` of the final segment) and the name (the final segment); the
    segments before it are the namespace. Every component is percent-decoded
    (UTF-8, then NFC) and re-encoded canonically: every octet outside the RFC
    3986 unreserved set and ``:`` as uppercase ``%XX``.
    """

    if not text or any(character.isspace() or ord(character) < 0x20 for character in text):
        raise KeyNormalizationRefused("a purl is non-empty and has no whitespace or controls")
    remainder, hash_mark, subpath_text = text.partition("#")
    subpath: tuple[str, ...] = ()
    if hash_mark:
        subpath = tuple(
            _decode(segment, what="subpath")
            for segment in subpath_text.strip("/").split("/")
            if segment not in ("", ".", "..")
        )
    remainder, question, qualifier_text = remainder.partition("?")
    qualifiers: dict[str, str] = {}
    if question:
        for pair in qualifier_text.split("&"):
            if not pair:
                continue
            key, equals, value = pair.partition("=")
            key = key.lower()
            if not equals or not _QUALIFIER_KEY.fullmatch(key):
                raise KeyNormalizationRefused(f"purl qualifier {pair!r} is not key=value")
            decoded = _decode(value, what=f"qualifier {key}")
            if not decoded:
                continue
            if key in qualifiers:
                raise KeyNormalizationRefused(
                    f"the purl states qualifier {key!r} twice; one purl names one variant"
                )
            qualifiers[key] = decoded
    scheme, colon, remainder = remainder.partition(":")
    if not colon or scheme.lower() != "pkg":
        raise KeyNormalizationRefused("a purl begins with the scheme pkg:")
    remainder = remainder.strip("/")
    type_text, slash, remainder = remainder.partition("/")
    purl_type = type_text.lower()
    if not slash or not _PURL_TYPE.fullmatch(purl_type):
        raise KeyNormalizationRefused("a purl names its type: pkg:<type>/<name>")
    head, _, last = remainder.rpartition("/")
    version: str | None = None
    if "@" in last:
        last, _, version_text = last.rpartition("@")
        version = _decode(version_text, what="version") or None
    name = _decode(last, what="name")
    if not name:
        raise KeyNormalizationRefused("a purl names its package")
    namespace = tuple(_decode(segment, what="namespace") for segment in head.split("/") if segment)
    rule = PURL_TYPE_RULES_V1.get(purl_type, PurlTypeRule())
    if rule.lower_namespace:
        namespace = tuple(part.lower() for part in namespace)
    if rule.lower_name:
        name = name.lower()
    if rule.pep503_name:
        name = pep503_v1(name)
    return PurlIdentity(
        type=purl_type,
        namespace=namespace,
        name=name,
        version=version,
        qualifiers=tuple(sorted(qualifiers.items())),
        subpath=subpath,
    )


# -- applying a rule -----------------------------------------------------------------------


def normalize_key_value(rule: KeyNormalizer, value: object) -> object:
    """``value`` under ``rule``: its canonical key value, or ``KeyNormalizationRefused``.

    ``none`` returns any value unchanged; every other normalizer reads text only.
    """

    if isinstance(rule, NoneNormalizer):
        return value
    if not isinstance(value, str):
        raise KeyNormalizationRefused(
            f"{normalizer_spelling(rule)} normalizes text, not {type(value).__name__}"
        )
    if isinstance(rule, LowercaseNormalizer):
        return lowercase_v1(value)
    if isinstance(rule, Pep503Normalizer):
        return pep503_v1(value)
    identity = parse_purl_v1(value)
    return identity.package if rule.identity == "package" else identity.variant


__all__ = [
    "LOWERCASING_NORMALIZERS",
    "NORMALIZER_VERSIONS",
    "NO_NORMALIZER",
    "PURL_TYPE_RULES_V1",
    "KeyNormalizationRefused",
    "KeyNormalizer",
    "LowercaseNormalizer",
    "NoneNormalizer",
    "Pep503Normalizer",
    "PurlIdentity",
    "PurlTypeRule",
    "PurlTypeNormalizer",
    "lowercase_v1",
    "normalize_key_value",
    "normalizer_spelling",
    "parse_purl_v1",
    "pep503_v1",
]
