"""Resolve named enum vocabulary at one immutable candidate or accepted coordinate."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import TYPE_CHECKING

from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, parse_claim_type
from cruxible_client.contracts.shapes import EnumResolver

if TYPE_CHECKING:
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.runtime.instance import PlaybillInstance


def enum_members_of(claim_type: ClaimType | None) -> tuple[str, ...] | None:
    """The literal enum declared by a ClaimType, when it has one."""

    from cruxible_client.contracts.shapes import EnumType

    if claim_type is not None and isinstance(claim_type.value, EnumType):
        return claim_type.value.members
    schema = None if claim_type is None else claim_type.literal_schema
    members = None if schema is None else schema.get("enum")
    return tuple(members) if isinstance(members, list) else None


def accepted_enum_resolver(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> EnumResolver:
    """Read each ClaimType once at the run's admitted, immutable coordinate."""

    @cache
    def resolve(name: str) -> tuple[str, ...] | None:
        with instance.bind_accepted_projection(coordinate) as projection:
            claim_type = projection.typed.source(f"ClaimType:{name}")
        return enum_members_of(claim_type)

    return resolve


def tree_enum_resolver(tree: Mapping[str, bytes]) -> EnumResolver:
    """One immutable tree's declared ClaimType enums, memoized by name."""

    @cache
    def resolve(name: str) -> tuple[str, ...] | None:
        path = claim_type_path(name)
        content = tree.get(path)
        if content is None:
            return None
        return enum_members_of(parse_claim_type(content, path=path))

    return resolve
