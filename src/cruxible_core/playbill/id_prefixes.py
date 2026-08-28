"""Resolve a unique short id prefix to the one full id it names.

A full Claim, AuthoringIntent, or proposal id is 32 or 64 hex characters, and
the surfaces that take one take it as an opaque string a caller has to copy
whole. A prefix is enough to name a thing when only one thing answers to it;
when several do, saying which ones is more useful than refusing blankly.
"""

from __future__ import annotations

from collections.abc import Iterable

from cruxible_client.contracts.errors import PlaybillFormatError

MINIMUM_PREFIX_HEX = 8


class AmbiguousIdPrefix(PlaybillFormatError):
    """A short id prefix named more than one accepted artifact."""

    error_code = "playbill.id.prefix_ambiguous"


def resolve_id_prefix(
    value: str,
    candidates: Iterable[str],
    *,
    marker: str,
    label: str,
) -> str:
    """Return the single id `value` names, or `value` unchanged when it is not a prefix.

    Only a well-formed short prefix resolves: `marker` plus at least
    MINIMUM_PREFIX_HEX hex characters. Anything else -- a full id, an unrelated
    string -- is returned untouched so the caller's own not-found refusal still
    speaks. Ambiguity is refused with the candidates listed, never guessed.
    """

    if not value.startswith(marker):
        return value
    suffix = value[len(marker) :]
    if len(suffix) < MINIMUM_PREFIX_HEX or not _is_hex(suffix):
        return value
    matches = sorted(
        {item for item in candidates if item.startswith(value)},
        key=lambda item: item.encode("utf-8"),
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousIdPrefix(
            f"{AmbiguousIdPrefix.error_code}: {label} prefix {value!r} names "
            f"{len(matches)} artifacts: {', '.join(matches)}"
        )
    return value


def _is_hex(value: str) -> bool:
    return all(character in "0123456789abcdef" for character in value)


__all__ = ["MINIMUM_PREFIX_HEX", "AmbiguousIdPrefix", "resolve_id_prefix"]
