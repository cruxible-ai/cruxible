"""General label formatting helpers for canonical views."""

from __future__ import annotations

import re


def _humanize_label(value: str) -> str:
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    value = value.replace("_", " ").replace("-", " ").strip()
    return value.title()


def _humanize_list(values: list[str]) -> str:
    return ", ".join(_humanize_label(value) for value in values)


def _humanize_list_or_dash(values: list[str]) -> str:
    if not values:
        return "-"
    return _humanize_list(values)


def _pluralize_label(value: str) -> str:
    if value.endswith("y"):
        return f"{value[:-1]}ies"
    if value.endswith("s"):
        return value
    return f"{value}s"


def _code_list(values: list[str]) -> str:
    if not values:
        return "-"
    return ", ".join(f"`{value}`" for value in values)


def _query_return_entity(value: str) -> str:
    stripped = value.strip().strip('"')
    match = re.fullmatch(r"list\[(.+)\]", stripped, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return stripped


def _humanize_traversal_summary(value: str) -> str:
    relationships, separator, suffix = value.partition(" (")
    relationship_label = " | ".join(
        _humanize_label(relationship) for relationship in relationships.split("|")
    )
    if not separator:
        return relationship_label

    suffix = suffix.rstrip(")")
    parts = suffix.split(", ")
    direction = _humanize_label(parts[0]) if parts else ""
    details = ", ".join(parts[1:])
    if details:
        return f"{relationship_label} ({direction}, {details})"
    return f"{relationship_label} ({direction})"
