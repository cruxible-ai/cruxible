"""Coded refusals for the read verbs: a wrong name names the nearest right ones.

``orient``, ``get`` and ``query`` refuse through one type, ``ReadRefusalError``
(declared beside the other served refusals in ``cruxible_client``, so a client
rebuilds the same class from the wire). A read never answers a wrong name with
an empty result or a server fault: it refuses with a stable code, the candidates
it could have meant, and the one operation that repairs the call.

This module also holds what every read verb shares at its boundary: the nearest
accepted names for a wrong one, and the accepted coordinate an ``at`` names.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

from cruxible_client.contracts import PlaybillAcceptedCoordinate as ClientCoordinate
from cruxible_client.contracts.errors import PlaybillError, ReadRefusalError
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance

_SEGMENTS = re.compile(r"[./:]")


def _leaf(name: str) -> str:
    return _SEGMENTS.split(name)[-1]


def nearest(value: str, names: Iterable[str], *, limit: int = 5) -> tuple[str, ...]:
    """The accepted names a mistyped or shortened one most likely meant.

    A name matches on its whole spelling or on its last segment, so a typo in a
    short predicate (``adoption_stat``) still finds ``dev.roadmap_item.adoption_state``,
    and a wrong prefix (``dev.item.adoption_state``) finds the same leaf elsewhere.
    """

    ordered = sorted(set(names))
    leaf = _leaf(value)
    by_leaf: dict[str, list[str]] = {}
    for name in ordered:
        by_leaf.setdefault(_leaf(name), []).append(name)
    exact_leaf = [name for name in by_leaf.get(leaf, ()) if name != value]
    close = difflib.get_close_matches(value, ordered, n=limit, cutoff=0.5)
    close_leaf = [
        name
        for item in difflib.get_close_matches(leaf, list(by_leaf), n=limit, cutoff=0.6)
        for name in by_leaf[item]
    ]
    return tuple(dict.fromkeys([*exact_leaf, *close, *close_leaf]))[:limit]


def resolve_read_coordinate(
    instance: PlaybillInstance,
    at: ClientCoordinate | AcceptedCoordinate | str | None,
) -> AcceptedProjectionCoordinate:
    """The accepted coordinate a read names: head, an exact coordinate, or a git oid."""

    if at is None:
        return instance.accepted_coordinate()
    try:
        if isinstance(at, str):
            return instance.coordinate_for_oid(at)
        return instance.resolve_accepted_coordinate(
            git_oid=at.git_oid,
            semantic_root=at.semantic_root,
            generation_root=at.generation_root,
            compiler_digest=at.compiler_digest,
        )
    except PlaybillError as exc:
        raise ReadRefusalError(
            "playbill.read.coordinate_not_accepted",
            f"at does not name an accepted generation of this instance ({exc})",
            http_status=404,
            repair_line="Omit at to read the current head, or pass a git oid from history",
            context={"field_path": "at"},
        ) from exc


__all__ = ["ReadRefusalError", "nearest", "resolve_read_coordinate"]
