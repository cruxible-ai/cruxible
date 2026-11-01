"""Resolve only named enum vocabulary from one candidate tree."""

from collections.abc import Mapping
from functools import cache

from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.shapes import EnumResolver


def tree_enum_resolver(tree: Mapping[str, bytes]) -> EnumResolver:
    """One immutable tree's declared ClaimType enums, memoized by name."""

    @cache
    def resolve(name: str) -> tuple[str, ...] | None:
        path = claim_type_path(name)
        content = tree.get(path)
        if content is None:
            return None
        declared = parse_claim_type(content, path=path)
        schema = declared.literal_schema
        members = None if schema is None else schema.get("enum")
        return tuple(members) if isinstance(members, list) else None

    return resolve
