"""Canonical forms: the one identity rule a ClaimType declares for its literal values.

A ClaimType (v8) may declare ``canonical_form``: an ordered list of named
transforms from this closed set. Core applies it mechanically and never
interprets it:

- on write, every lowering path stores the canonical value, so a stored literal
  is always in canonical form (the raw value stays in the cited evidence);
- at every comparison entry point, an incoming operand compared with values of
  that predicate is canonicalized by the same form first.

The transforms, each over Unicode text:

``nfc``
    Unicode normalization form C.
``nfkc``
    Unicode normalization form KC (compatibility characters fold too).
``trim``
    Remove leading and trailing whitespace (every character ``str.isspace``
    accepts).
``collapse_whitespace``
    Replace every maximal run of whitespace with one U+0020 space.
``casefold``
    Unicode full case folding (``str.casefold``). Changes how a value displays.
``upper``
    Unicode full uppercase mapping (``str.upper``). Changes how a value displays.
``strip_punctuation``
    Remove every character whose Unicode general category is punctuation
    (``Pc``, ``Pd``, ``Ps``, ``Pe``, ``Pi``, ``Pf``, ``Po``). Changes how a
    value displays.

The canonical value of a text is the fixed point of applying the declared list
in order: the list is applied again until one pass changes nothing (at most
``MAX_CANONICAL_ROUNDS`` passes). That makes canonicalization idempotent by
construction even where one transform's output is not stable under an earlier
one (casefolding can denormalize, stripping punctuation can expose a
composable pair). A literal that is not text is canonicalized member by member:
every string inside a JSON array, and every string value (never a key) inside a
JSON object. Numbers, booleans and null never change.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Final, Literal, cast, get_args

CanonicalTransform = Literal[
    "nfc",
    "nfkc",
    "trim",
    "collapse_whitespace",
    "casefold",
    "upper",
    "strip_punctuation",
]
CANONICAL_TRANSFORMS: Final[tuple[CanonicalTransform, ...]] = cast(
    tuple[CanonicalTransform, ...], get_args(CanonicalTransform)
)
#: Transforms that change how a value reads, not only how it is spelled.
DISPLAY_CHANGING_TRANSFORMS: Final = frozenset({"casefold", "upper", "strip_punctuation"})
#: Pairs a form may name at most one of: each pair states one rule two ways.
_EXCLUSIVE: Final = (frozenset({"nfc", "nfkc"}), frozenset({"casefold", "upper"}))
MAX_CANONICAL_ROUNDS: Final = 8


class CanonicalFormDidNotConverge(ValueError):
    """Applying a canonical form kept changing a value past the round bound."""


def _collapse(text: str) -> str:
    out: list[str] = []
    in_space = False
    for character in text:
        if character.isspace():
            if not in_space:
                out.append(" ")
            in_space = True
        else:
            out.append(character)
            in_space = False
    return "".join(out)


def _strip_punctuation(text: str) -> str:
    return "".join(
        character for character in text if not unicodedata.category(character).startswith("P")
    )


def _apply(transform: str, text: str) -> str:
    if transform == "nfc":
        return unicodedata.normalize("NFC", text)
    if transform == "nfkc":
        return unicodedata.normalize("NFKC", text)
    if transform == "trim":
        return text.strip()
    if transform == "collapse_whitespace":
        return _collapse(text)
    if transform == "casefold":
        return text.casefold()
    if transform == "upper":
        return text.upper()
    if transform == "strip_punctuation":
        return _strip_punctuation(text)
    raise ValueError(f"unknown canonical transform {transform!r}")


def validate_canonical_form(form: Iterable[str]) -> tuple[CanonicalTransform, ...]:
    """Refuse a form naming an unknown transform, one twice, or both of a pair."""

    items = tuple(form)
    for item in items:
        if item not in CANONICAL_TRANSFORMS:
            raise ValueError(
                f"unknown canonical transform {item!r}; the transforms are "
                + ", ".join(CANONICAL_TRANSFORMS)
            )
    if len(set(items)) != len(items):
        raise ValueError("a canonical form names each transform at most once")
    for pair in _EXCLUSIVE:
        if pair <= set(items):
            first, second = sorted(pair)
            raise ValueError(f"a canonical form names at most one of {first} and {second}")
    return cast(tuple[CanonicalTransform, ...], items)


def canonicalize_text(text: str, form: Iterable[str]) -> str:
    """The canonical spelling of one text under ``form`` (the identity when empty)."""

    transforms = tuple(form)
    if not transforms:
        return text
    current = text
    for _round in range(MAX_CANONICAL_ROUNDS):
        following = current
        for transform in transforms:
            following = _apply(transform, following)
        if following == current:
            return current
        current = following
    raise CanonicalFormDidNotConverge(
        f"canonical form {', '.join(transforms)} did not settle on one spelling of {text!r}"
    )


def canonicalize_literal(value: object, form: Iterable[str]) -> object:
    """A literal value with every string in it canonical under ``form``."""

    transforms = tuple(form)
    if not transforms:
        return value
    if isinstance(value, str):
        return canonicalize_text(value, transforms)
    if isinstance(value, list | tuple):
        return [canonicalize_literal(item, transforms) for item in value]
    if isinstance(value, Mapping):
        return {key: canonicalize_literal(item, transforms) for key, item in value.items()}
    return value


def is_canonical_literal(value: object, form: Iterable[str]) -> bool:
    """Whether a stored literal already equals its canonical value under ``form``."""

    transforms = tuple(form)
    if not transforms:
        return True
    try:
        return canonicalize_literal(value, transforms) == value
    except CanonicalFormDidNotConverge:
        return False


__all__ = [
    "CANONICAL_TRANSFORMS",
    "CanonicalFormDidNotConverge",
    "CanonicalTransform",
    "DISPLAY_CHANGING_TRANSFORMS",
    "MAX_CANONICAL_ROUNDS",
    "canonicalize_literal",
    "canonicalize_text",
    "is_canonical_literal",
    "validate_canonical_form",
]
