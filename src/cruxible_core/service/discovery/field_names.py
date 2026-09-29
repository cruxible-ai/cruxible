"""One field-naming rule for the read verbs (read-model spec, Addendum 2).

``orient`` advertises field names and ``query`` resolves them, so both use
these two functions and every advertised name resolves back to exactly the
predicate it was shown for.
"""

from __future__ import annotations

from collections.abc import Collection

RESERVED_FIELD_NAMES = frozenset({"subject_id", "subject", "kind", "predicate", "claim", "flags"})
"""Names the read verbs reserve: Subject fields and row metadata keys."""

RESERVED_FIELD_PREFIX = "value."
"""Row keys under this prefix hold a column whose name is reserved metadata."""


def is_reserved_field_name(name: str) -> bool:
    """Whether ``name`` is a reserved field name or a ``value.*`` row key."""

    return name in RESERVED_FIELD_NAMES or name.startswith(RESERVED_FIELD_PREFIX)


def reserved_meaning(field: str, accepted: Collection[str]) -> bool:
    """Whether ``field`` means its reserved meaning rather than a predicate.

    A reserved name keeps its reserved meaning only when no accepted predicate's
    full name equals it.
    """

    return is_reserved_field_name(field) and field not in accepted


def short_field_name(predicate: str, kind: str, accepted: Collection[str]) -> str:
    """How predicate ``predicate`` of kind ``kind`` is shown.

    The ``kind + "."`` prefix is removed only if the short string is neither the
    full name of any accepted predicate nor a reserved name; otherwise the
    predicate is shown in full. There is no last-segment form.
    """

    prefix = f"{kind}."
    if predicate.startswith(prefix):
        short = predicate[len(prefix) :]
        if short not in accepted and not is_reserved_field_name(short):
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


__all__ = [
    "RESERVED_FIELD_NAMES",
    "RESERVED_FIELD_PREFIX",
    "is_reserved_field_name",
    "reserved_meaning",
    "resolve_field",
    "short_field_name",
]
