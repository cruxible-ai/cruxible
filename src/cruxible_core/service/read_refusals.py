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


#: The shortest git-oid prefix ``at`` accepts: the compact coordinate every read prints.
OID_PREFIX_MIN = 12
_MAX_OID_CANDIDATES = 5
_OID_PREFIX = re.compile(rf"[0-9a-f]{{{OID_PREFIX_MIN},64}}")
_AT_REPAIR = "Omit at to read the current head, or pass an accepted git oid or a unique prefix"


def _not_accepted(message: str, candidates: Iterable[str] = ()) -> ReadRefusalError:
    return ReadRefusalError(
        "playbill.read.coordinate_not_accepted",
        f"at does not name an accepted generation of this instance ({message})",
        http_status=404,
        candidates=candidates,
        repair_line=_AT_REPAIR,
        context={"field_path": "at"},
    )


def _oid_for(instance: PlaybillInstance, at: str) -> str:
    """The one accepted generation's oid that ``at`` (a full oid or a unique prefix) names."""

    if not _OID_PREFIX.fullmatch(at):
        if len(at) < OID_PREFIX_MIN and re.fullmatch(r"[0-9a-f]+", at):
            raise ReadRefusalError(
                "playbill.read.coordinate_prefix_too_short",
                f"at {at!r} is {len(at)} hex characters; a git-oid prefix needs at least "
                f"{OID_PREFIX_MIN}",
                repair_line=f"Pass the {OID_PREFIX_MIN}-character coordinate a read printed",
                context={"field_path": "at"},
            )
        raise _not_accepted("at must contain 12 to 64 lowercase hex characters")
    oids = [generation.oid for generation in reversed(instance.accepted_history())]
    matches = [oid for oid in oids if oid.startswith(at)]
    if len(matches) > 1:
        raise ReadRefusalError(
            "playbill.read.coordinate_ambiguous",
            f"at {at!r} is a prefix of {len(matches)} accepted generations' git oids",
            http_status=409,
            candidates=matches[:_MAX_OID_CANDIDATES],
            repair_line="Pass one of them in full",
            context={"field_path": "at", "matches": len(matches)},
        )
    if not matches:
        close = difflib.get_close_matches(at, oids, n=_MAX_OID_CANDIDATES, cutoff=0)
        raise _not_accepted(f"no accepted git oid starts with {at}", close)
    return matches[0]


def resolve_read_coordinate(
    instance: PlaybillInstance,
    at: ClientCoordinate | AcceptedCoordinate | str | None,
) -> AcceptedProjectionCoordinate:
    """The accepted coordinate a read names: head, an exact coordinate, or a git oid.

    A git oid may be shortened to a unique prefix of at least ``OID_PREFIX_MIN``
    hex characters, so the compact coordinate a read prints can be passed back.
    Prefixes resolve against accepted generations only.
    """

    if at is None:
        return instance.accepted_coordinate()
    try:
        if isinstance(at, str):
            return instance.coordinate_for_oid(_oid_for(instance, at))
        return instance.resolve_accepted_coordinate(
            git_oid=at.git_oid,
            semantic_root=at.semantic_root,
            generation_root=at.generation_root,
            compiler_digest=at.compiler_digest,
        )
    except PlaybillError as exc:
        raise _not_accepted(str(exc)) from exc


__all__ = ["OID_PREFIX_MIN", "ReadRefusalError", "nearest", "resolve_read_coordinate"]
