"""One Subject-reference resolver: an exact Subject, else one alias of the kind.

A Subject reference names ``kind/id``. It resolves, in order:

1. to the accepted Subject at exactly that path, whatever its lifecycle;
2. otherwise through the aliases of that kind: every live ``semantic.alias``
   Claim contributes a ``vocabulary_terms`` row (basis ``alias``) whose ``term``
   is the alias under the discovery normalizer (``normalize_discovery_term``:
   NFC, casefold, whitespace collapsed and trimmed). The reference's id under the
   same normalizer is looked up there, restricted to Subjects of the kind. One
   match resolves; several are ambiguous and the caller refuses naming every
   candidate; none leaves the reference unresolved for the caller's own rule.

Core applies the agent-authored aliases mechanically: it never guesses a match
the index does not hold. Every Subject-reference entry point resolves through
here, so a reference means the same Subject wherever it is written.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from cruxible_client.contracts.discovery import normalize_discovery_term

_SUBJECTS = "subjects/"


def _kind_range(kind: str) -> tuple[str, str]:
    prefix = f"{_SUBJECTS}{kind}/"
    return prefix, prefix[:-1] + chr(ord("/") + 1)


def _ref_of(path: str) -> str:
    return path.removeprefix(_SUBJECTS).removesuffix(".json")


@dataclass(frozen=True)
class SubjectRefResolution:
    """What one ``kind/id`` reference resolved to at one accepted coordinate."""

    kind: str
    subject_id: str
    #: The resolved Subject's ledger path, or None when it resolved to none.
    path: str | None
    via: Literal["identity", "alias"] | None
    #: Every Subject the alias names, as ``kind/id``, when it names more than one.
    candidates: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    @property
    def ref(self) -> str | None:
        """The resolved Subject as ``kind/id``."""

        return None if self.path is None else _ref_of(self.path)


def _subject(connection: sqlite3.Connection, path: str, *, table: str, live: bool) -> bool:
    clause = " AND lifecycle='live'" if live else ""
    return (
        connection.execute(f"SELECT 1 FROM {table} WHERE path=?{clause}", (path,)).fetchone()
        is not None
    )


def alias_targets(
    connection: sqlite3.Connection,
    kind: str,
    name: str,
    *,
    terms_table: str = "vocabulary_terms",
    subjects_table: str = "subjects",
) -> tuple[str, ...]:
    """The live Subjects of ``kind`` an alias spelled ``name`` names, by path."""

    low, high = _kind_range(kind)
    rows = connection.execute(
        f"SELECT DISTINCT target_path FROM {terms_table} "
        "WHERE term=? AND basis='alias' AND target_path>=? AND target_path<? "
        "ORDER BY target_path",
        (normalize_discovery_term(name), low, high),
    ).fetchall()
    return tuple(
        str(row[0])
        for row in rows
        if _subject(connection, str(row[0]), table=subjects_table, live=True)
    )


def resolve_subject_ref(
    connection: sqlite3.Connection,
    kind: str,
    subject_id: str,
    *,
    terms_table: str = "vocabulary_terms",
    subjects_table: str = "subjects",
) -> SubjectRefResolution:
    """Resolve one reference: the exact Subject, else exactly one alias of the kind."""

    exact = f"{_SUBJECTS}{kind}/{subject_id}.json"
    if _subject(connection, exact, table=subjects_table, live=False):
        return SubjectRefResolution(kind=kind, subject_id=subject_id, path=exact, via="identity")
    targets = alias_targets(
        connection, kind, subject_id, terms_table=terms_table, subjects_table=subjects_table
    )
    if len(targets) == 1:
        return SubjectRefResolution(kind=kind, subject_id=subject_id, path=targets[0], via="alias")
    return SubjectRefResolution(
        kind=kind,
        subject_id=subject_id,
        path=None,
        via=None,
        candidates=tuple(_ref_of(path) for path in targets),
    )


def creation_collisions(
    connection: sqlite3.Connection,
    kind: str,
    subject_id: str,
    *,
    exempt: frozenset[str] = frozenset(),
    terms_table: str = "vocabulary_terms",
    subjects_table: str = "subjects",
) -> tuple[str, ...]:
    """The live Subjects of ``kind`` a new ``kind/id`` would collide with, as ``kind/id``.

    A new Subject collides with an existing one of its kind whose id, canonical
    token or alias equals the new id's canonical token (the discovery
    normalizer): the same thing would then have two Subjects. ``exempt`` names
    Subject paths the author asserted distinct (``semantic.distinct_from``).
    """

    low, high = _kind_range(kind)
    new_path = f"{_SUBJECTS}{kind}/{subject_id}.json"
    rows = connection.execute(
        f"SELECT DISTINCT target_path FROM {terms_table} "
        "WHERE term=? AND basis IN ('canonical_token','alias') "
        "AND target_path>=? AND target_path<? ORDER BY target_path",
        (normalize_discovery_term(subject_id), low, high),
    ).fetchall()
    return tuple(
        _ref_of(str(row[0]))
        for row in rows
        if str(row[0]) != new_path
        and str(row[0]) not in exempt
        and _subject(connection, str(row[0]), table=subjects_table, live=True)
    )


def split_reference(value: str) -> tuple[str, str, str] | None:
    """``(prefix, kind, id)`` of ``Subject:kind/id``, ``kind/id`` or a Subject path.

    ``prefix`` is the spelling around the reference (``Subject:`` or ``""``), so a
    resolved reference is written back the way it was given.
    """

    prefix = ""
    body = value
    if body.startswith("Subject:"):
        prefix, body = "Subject:", body.removeprefix("Subject:")
    elif body.startswith(_SUBJECTS) and body.endswith(".json"):
        body = body.removeprefix(_SUBJECTS).removesuffix(".json")
    kind, slash, subject_id = body.partition("/")
    if not slash or not kind or not subject_id:
        return None
    return prefix, kind, subject_id


def resolve_reference_text(
    connection: sqlite3.Connection, value: str
) -> tuple[str, SubjectRefResolution] | None:
    """Resolve a reference given as text; ``(spelling of the result, resolution)``.

    The spelling is the resolved Subject in the same form the value used, or the
    value itself when it resolved to nothing. ``None`` when it is no reference.
    """

    parts = split_reference(value)
    if parts is None:
        return None
    prefix, kind, subject_id = parts
    resolution = resolve_subject_ref(connection, kind, subject_id)
    if resolution.via == "alias" and resolution.ref is not None:
        if value.startswith(_SUBJECTS):
            return f"{_SUBJECTS}{resolution.ref}.json", resolution
        return prefix + resolution.ref, resolution
    return value, resolution


class SubjectReferenceAmbiguous(ValueError):
    """A reference that names no Subject and whose alias names several."""

    def __init__(self, value: str, resolution: SubjectRefResolution) -> None:
        super().__init__(
            f"{value} names no Subject, and an alias of {resolution.kind} names "
            f"{len(resolution.candidates)}: {', '.join(resolution.candidates)}"
        )
        self.value = value
        self.resolution = resolution


SubjectReferenceResolver = Callable[[str], str]
"""Resolve Subject-reference text at one accepted coordinate, spelled as given."""


def reference_resolver(
    connect: Callable[[], AbstractContextManager[sqlite3.Connection]],
) -> SubjectReferenceResolver:
    """A resolver over one accepted alias index (``connect`` opens it).

    Text that is no reference, and a reference that resolves to nothing, come
    back as given; an ambiguous alias raises ``SubjectReferenceAmbiguous``. Each
    distinct text is resolved once.
    """

    known: dict[str, str] = {}

    def resolve(value: str) -> str:
        if value in known:
            return known[value]
        if split_reference(value) is None:
            return value
        with connect() as connection:
            found = resolve_reference_text(connection, value)
        if found is None:
            known[value] = value
            return value
        spelled, resolution = found
        if resolution.ambiguous:
            raise SubjectReferenceAmbiguous(value, resolution)
        known[value] = spelled
        return spelled

    return resolve


def projection_connection(
    bind: Callable[[], AbstractContextManager[Any]],
) -> Callable[[], AbstractContextManager[sqlite3.Connection]]:
    """Open an accepted projection (``bind``) as the connection a resolver reads."""

    @contextmanager
    def connect() -> Iterator[sqlite3.Connection]:
        with bind() as projection:
            yield projection.typed.connection

    return connect


def tree_reference_resolver(tree: Mapping[str, bytes]) -> SubjectReferenceResolver:
    """The resolver for references read from one accepted tree.

    An accepted tree that stands on its projection resolves through that
    projection's alias index. A tree with none (cold replay, genesis) derives the
    same two tables from its own members with the same row derivation the
    projection compiler uses (``typed_state.vocabulary_terms``), once, on first
    use: one rule, whichever way the tree is held.
    """

    reader = getattr(tree, "_accepted_reader", None)
    if reader is not None:
        return reference_resolver(projection_connection(reader))
    built: list[sqlite3.Connection] = []

    @contextmanager
    def connect() -> Iterator[sqlite3.Connection]:
        if not built:
            built.append(_tree_alias_index(tree))
        yield built[0]

    return reference_resolver(connect)


def _tree_alias_index(tree: Mapping[str, bytes]) -> sqlite3.Connection:
    """Subjects and their alias and canonical-token terms, derived from a tree's members."""

    from cruxible_client.contracts.claims import parse_claim
    from cruxible_client.contracts.subjects import parse_subject
    from cruxible_core.indexes.typed_state import vocabulary_terms

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE subjects (path TEXT PRIMARY KEY, lifecycle TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE vocabulary_terms (source_identity TEXT, target_path TEXT, "
        "basis TEXT, term TEXT, value TEXT)"
    )
    for path in tree:
        source: Any
        if path.startswith(_SUBJECTS) and path.endswith(".json"):
            source = parse_subject(tree[path], path=path)
            connection.execute("INSERT INTO subjects VALUES (?,?)", (path, source.lifecycle.state))
        elif path.startswith("claims/") and b'"semantic.alias"' in tree[path]:
            source = parse_claim(tree[path], path=path)
        else:
            continue
        connection.executemany(
            "INSERT INTO vocabulary_terms VALUES (?,?,?,?,?)",
            vocabulary_terms(source, path=path),
        )
    return connection


__all__ = [
    "SubjectReferenceAmbiguous",
    "SubjectReferenceResolver",
    "SubjectRefResolution",
    "projection_connection",
    "reference_resolver",
    "tree_reference_resolver",
    "alias_targets",
    "creation_collisions",
    "resolve_reference_text",
    "resolve_subject_ref",
    "split_reference",
]
