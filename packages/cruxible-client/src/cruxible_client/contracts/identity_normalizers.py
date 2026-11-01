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
    rules (``PURL_TYPE_RULES_V1``: a component folds to lowercase exactly when
    its purl type definition declares it case-insensitive). ``identity: package`` excludes the
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
    """Which of one type's components purl-type folds to lowercase, and pypi's name form."""

    lower_namespace: bool = False
    lower_name: bool = False
    lower_version: bool = False
    lower_subpath: bool = False
    pep503_name: bool = False


_NS_NAME: Final = PurlTypeRule(lower_namespace=True, lower_name=True)
#: purl-type v1's per-type rules. Source: the purl-spec type definitions
#: (``types/<type>-definition.json`` at package-url/purl-spec
#: d66745e85940191e5c9690e9f82bc2d75120003f). A component folds to lowercase
#: exactly when its definition states ``"case_sensitive": false`` (the schema:
#: "If false, the valid form is lowercase"); ``case_sensitive`` defaults to
#: true, so every component and type not listed keeps its case (npm's scope
#: and name are case-sensitive: grandfathered mixed-case packages exist). The
#: definitions' free-text ``normalization_rules`` are not applied, except
#: pypi's name, which takes its PEP 503 form (ruling R12; it implies the
#: definition's "replace underscore with dash"). Changing a row is a new
#: purl-type version.
PURL_TYPE_RULES_V1: Final[dict[str, PurlTypeRule]] = {
    "alpm": _NS_NAME,
    "apk": _NS_NAME,
    "bitbucket": _NS_NAME,
    "bitnami": PurlTypeRule(lower_name=True),
    "brew": _NS_NAME,
    "chrome-extension": PurlTypeRule(lower_name=True),
    "composer": _NS_NAME,
    "deb": _NS_NAME,
    "github": _NS_NAME,
    "hex": _NS_NAME,
    "huggingface": PurlTypeRule(lower_version=True),
    "luarocks": _NS_NAME,
    "oci": PurlTypeRule(lower_name=True, lower_version=True),
    "otp": PurlTypeRule(lower_name=True, lower_subpath=True),
    "pub": PurlTypeRule(lower_name=True),
    "pypi": PurlTypeRule(pep503_name=True, lower_version=True),
    "qpkg": PurlTypeRule(lower_namespace=True),
    "rpm": PurlTypeRule(lower_namespace=True),
    "vscode-extension": PurlTypeRule(lower_namespace=True, lower_name=True, lower_version=True),
    "yocto": PurlTypeRule(lower_namespace=True),
}
#: Clause 5.6.2: a type is ASCII letters, digits, ``.`` and ``-``, letter first.
_PURL_TYPE: Final = re.compile(r"^[a-z][a-z0-9.-]*$")
#: Clause 5.6.6: a qualifier key is lowercase ASCII letters, digits, ``.``,
#: ``-`` and ``_``, letter first, never percent-encoded.
_QUALIFIER_KEY: Final = re.compile(r"^[a-z][a-z0-9._-]*$")
#: Every ``%`` begins one percent-encoded triplet (RFC 3986 section 2.1).
_BAD_PERCENT: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")
#: Characters never percent-encoded in a canonical purl component: the purl
#: Alphanumeric and Punctuation characters (``.-_~``), and ``:`` (left
#: unencoded, as the reference implementations write it).
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
    """Percent-decode one component (UTF-8, then NFC); a malformed triplet refuses."""

    if _BAD_PERCENT.search(text):
        raise KeyNormalizationRefused(f"the purl {what} has a malformed percent-encoding")
    try:
        decoded = unquote(text, errors="strict")
    except UnicodeDecodeError as exc:
        raise KeyNormalizationRefused(f"the purl {what} is not UTF-8 when decoded") from exc
    return unicodedata.normalize("NFC", decoded)


def _segments(text: str, *, what: str, drop_dots: bool) -> tuple[str, ...]:
    """A namespace or subpath: split on ``/``, each segment decoded, then filtered.

    Decoding comes first (how-to-parse), so an encoded ``.`` or ``..`` subpath
    segment drops like a literal one, and a decoded ``/`` (which could not be
    re-spelled as one segment) refuses.
    """

    found: list[str] = []
    for raw in text.split("/"):
        segment = _decode(raw, what=what)
        if not segment or (drop_dots and segment in (".", "..")):
            continue
        if "/" in segment:
            raise KeyNormalizationRefused(f"a purl {what} segment holds an encoded '/'")
        found.append(segment)
    return tuple(found)


def parse_purl_v1(text: str) -> PurlIdentity:
    """Read one package URL by the purl-spec parse order, under purl-type v1's rules.

    Sources: purl-spec ``docs/specification/how-to-parse.md`` and Clause 5
    (``standard/Clause-5-Package-URL-Specification.md``). ``#`` splits the
    subpath (each segment decoded, then empty, ``.`` and ``..`` segments
    dropped; a decoded ``/`` refuses), ``?`` the qualifiers (keys lowercased and
    letter-initial, empty values dropped, a repeated key refused), ``:`` the
    ``pkg`` scheme, then the type (lowercased, letter-initial), the version
    (after the last ``@`` of the final segment), the name (the final segment)
    and the namespace (the segments before it; a decoded ``/`` refuses). Every
    component is percent-decoded (a malformed ``%`` triplet refuses; UTF-8,
    then NFC) and re-encoded canonically: every octet outside ``A-Za-z0-9``,
    ``.-_~`` and ``:`` as uppercase ``%XX``. The per-type rules then fold the
    components their definition declares case-insensitive.
    """

    if not text or any(character.isspace() or ord(character) < 0x20 for character in text):
        raise KeyNormalizationRefused("a purl is non-empty and has no whitespace or controls")
    remainder, hash_mark, subpath_text = text.partition("#")
    subpath: tuple[str, ...] = ()
    if hash_mark:
        subpath = _segments(subpath_text, what="subpath", drop_dots=True)
    remainder, question, qualifier_text = remainder.partition("?")
    qualifiers: dict[str, str] = {}
    if question:
        for pair in qualifier_text.split("&"):
            if not pair:
                continue
            key, equals, value = pair.partition("=")
            key = key.lower()
            if not equals or not _QUALIFIER_KEY.fullmatch(key):
                raise KeyNormalizationRefused(
                    f"purl qualifier {pair!r} is not key=value with a letter-initial key"
                )
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
        raise KeyNormalizationRefused(
            "a purl names its type (ASCII letters, digits, '.' and '-', letter first): "
            "pkg:<type>/<name>"
        )
    head, _, last = remainder.rpartition("/")
    version: str | None = None
    if "@" in last:
        last, _, version_text = last.rpartition("@")
        version = _decode(version_text, what="version") or None
    name = _decode(last, what="name")
    if not name:
        raise KeyNormalizationRefused("a purl names its package")
    namespace = _segments(head, what="namespace", drop_dots=False)
    rule = PURL_TYPE_RULES_V1.get(purl_type, PurlTypeRule())
    if rule.lower_namespace:
        namespace = tuple(lowercase_v1(part) for part in namespace)
    if rule.lower_name:
        name = lowercase_v1(name)
    if rule.pep503_name:
        name = pep503_v1(name)
    if rule.lower_version and version is not None:
        version = lowercase_v1(version)
    if rule.lower_subpath:
        subpath = tuple(lowercase_v1(part) for part in subpath)
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
