"""One field-naming rule for the read verbs (read-model spec, Addendum 2).

``orient`` advertises field names and ``query`` resolves them, so both use
these two functions and every advertised name resolves back to exactly the
predicate it was shown for.
"""

from __future__ import annotations

from collections.abc import Collection


def short_field_name(predicate: str, kind: str, accepted: Collection[str]) -> str:
    """How predicate ``predicate`` of kind ``kind`` is shown.

    The ``kind + "."`` prefix is removed only if the short string is not itself
    the full name of any accepted predicate; otherwise the predicate is shown in
    full. There is no last-segment form.
    """

    prefix = f"{kind}."
    if predicate.startswith(prefix):
        short = predicate[len(prefix) :]
        if short not in accepted:
            return short
    return predicate


def resolve_field(field: str, kind: str, applicable: Collection[str]) -> str | None:
    """The predicate a field names for kind ``kind``, or None to refuse.

    ``applicable`` is the accepted predicates that apply to ``kind``: a field
    that is one of their full names is that predicate; otherwise
    ``kind + "." + field`` is, if it applies.
    """

    if field in applicable:
        return field
    qualified = f"{kind}.{field}"
    if qualified in applicable:
        return qualified
    return None


__all__ = ["resolve_field", "short_field_name"]
