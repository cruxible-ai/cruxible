"""``last_changed``: the generation a Subject last changed in, for ``query``.

A Subject changes when its own shell or any Claim about it (live or retired)
takes new bytes. Both facts come from maintained indexes, never from a walk of
history: the typed-state projection lists a Subject's Claim paths
(``claims_by_subject_predicate``), and the accepted-history index answers each
path's latest accepted version with one seek (``versions_by_path``). The work is
proportional to the Subjects asked about and their Claims, not to how many
generations the instance has accepted. A path no change record named carries
genesis bytes, so it last changed at generation 0.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from cruxible_client.contracts.compact_query import QueryFilterOperator
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.query_vocabulary import query_refusal

LAST_CHANGED_FIELD = "last_changed"
LAST_CHANGED_OPERATORS: tuple[QueryFilterOperator, ...] = (
    "eq",
    "gt",
    "gte",
    "in",
    "lt",
    "lte",
    "ne",
)
_CHUNK = 500


def subject_last_changed(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    subject_paths: Iterable[str],
) -> dict[str, int]:
    """Each Subject path's last-changed generation at ``coordinate``."""

    subjects = tuple(dict.fromkeys(subject_paths))
    if not subjects:
        return {}
    members: dict[str, list[str]] = {path: [path] for path in subjects}
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        for start in range(0, len(subjects), _CHUNK):
            chunk = subjects[start : start + _CHUNK]
            for subject_path, claim_path in connection.execute(
                "SELECT subject_path, path FROM claims "
                f"WHERE subject_path IN ({','.join('?' for _ in chunk)})",
                chunk,
            ):
                members[str(subject_path)].append(str(claim_path))
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        changed = history.last_changed(tuple(path for paths in members.values() for path in paths))
    return {
        subject: max(changed.get(path, 0) for path in paths) for subject, paths in members.items()
    }


def check_last_changed_filter(operator: str, value: object, *, field_path: str) -> object:
    """A ``where`` on ``last_changed``: a comparison with a generation number."""

    if operator not in LAST_CHANGED_OPERATORS:
        raise query_refusal(
            "cruxible.query.operator_not_applicable",
            f"{operator!r} does not apply to {LAST_CHANGED_FIELD} (generation)",
            nearest=LAST_CHANGED_OPERATORS,
            repair=f"use one of: {', '.join(LAST_CHANGED_OPERATORS)}",
            field_path=field_path,
        )
    entries: Sequence[object] = (
        tuple(value) if operator == "in" and isinstance(value, tuple) else (value,)
    )
    generations: list[int] = []
    for position, entry in enumerate(entries):
        # The CLI spells every value as text; JSON callers pass integers.
        if isinstance(entry, str) and entry.isascii() and entry.isdigit():
            entry = int(entry)
        if isinstance(entry, bool) or not isinstance(entry, int) or entry < 0:
            raise query_refusal(
                "cruxible.query.value_type_mismatch",
                f"{LAST_CHANGED_FIELD} compares with a generation number, not {entry!r}",
                repair="pass a nonnegative integer generation, e.g. last_changed>=835",
                field_path=(f"{field_path}.in[{position}]" if operator == "in" else field_path),
            )
        generations.append(entry)
    return tuple(generations) if operator == "in" else generations[0]


def last_changed_matches(operator: str, expected: object, generation: int) -> bool:
    if operator == "in":
        return generation in expected  # type: ignore[operator]
    assert isinstance(expected, int)
    return {
        "eq": generation == expected,
        "ne": generation != expected,
        "lt": generation < expected,
        "lte": generation <= expected,
        "gt": generation > expected,
        "gte": generation >= expected,
    }[operator]


__all__ = [
    "LAST_CHANGED_FIELD",
    "LAST_CHANGED_OPERATORS",
    "check_last_changed_filter",
    "last_changed_matches",
    "subject_last_changed",
]
