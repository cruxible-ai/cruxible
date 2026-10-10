"""One Subject-reference resolver: the exact live Subject, else one alias, else the retired one.

A Subject reference names ``kind/id``. It resolves, in order:

1. to the live accepted Subject at exactly that path;
2. otherwise through the aliases of that kind: every live ``semantic.alias``
   Claim contributes a ``vocabulary_terms`` row (basis ``alias``) whose ``term``
   is the alias under the discovery normalizer (``normalize_discovery_term``:
   NFC, casefold, whitespace collapsed and trimmed). The reference's id under the
   same normalizer is looked up there, restricted to Subjects of the kind. One
   match resolves; several are ambiguous and the caller refuses naming every
   candidate;
3. otherwise to the retired accepted Subject at exactly that path; none leaves
   the reference unresolved for the caller's own rule.

So after a merge (alias the survivor with the retired Subject's id, retire it),
a reference to the retired Subject reaches the survivor.

Core applies the agent-authored aliases mechanically: it never guesses a match
the index does not hold. The Subject-reference entry points the CLI reference
lists all resolve through here, so a reference means the same Subject at each.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from cruxible_client.contracts.discovery import normalize_discovery_term

if TYPE_CHECKING:
    from cruxible_core.indexes.subject_identity import KeyReader

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
    via: Literal["identity", "alias", "key"] | None
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


def resolve_subject_ref(
    connection: sqlite3.Connection,
    kind: str,
    subject_id: str,
    *,
    key_reader: KeyReader | None = None,
) -> SubjectRefResolution:
    """Resolve one reference: exact live id, else alias, else single key, else exact retired.

    The one resolver every reference route shares (``subject_identity.IdentityView``).
    ``key_reader`` reads ``kind/<text>`` as a one-key kind's canonical key (from the
    SubjectKinds of the accepted tree); without one the key step is skipped.
    After a merge -- an alias of the surviving Subject naming the retired one --
    a reference to the retired Subject reaches the survivor.
    """

    from cruxible_core.indexes.subject_identity import IdentityView

    return IdentityView(connection, key_reader=key_reader).resolve(kind, subject_id)


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
    connection: sqlite3.Connection, value: str, *, key_reader: KeyReader | None = None
) -> tuple[str, SubjectRefResolution] | None:
    """Resolve a reference given as text; ``(spelling of the result, resolution)``.

    The spelling is the resolved Subject in the same form the value used, or the
    value itself when it resolved to nothing. ``None`` when it is no reference.
    """

    parts = split_reference(value)
    if parts is None:
        return None
    prefix, kind, subject_id = parts
    resolution = resolve_subject_ref(connection, kind, subject_id, key_reader=key_reader)
    if resolution.via in {"alias", "key"} and resolution.ref is not None:
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
    *,
    key_reader: KeyReader | None = None,
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
            found = resolve_reference_text(connection, value, key_reader=key_reader)
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


def tree_index_connection(
    tree: Mapping[str, bytes],
) -> Callable[[], AbstractContextManager[sqlite3.Connection]]:
    """Open one accepted tree's Subject and vocabulary index.

    An accepted root tree that stands on its projection reads that projection.
    A tree with none (cold replay, genesis, an edited tree) derives the same two
    tables from its own members with the row derivation the projection compiler
    uses (``typed_state.vocabulary_terms``), once, on first use: one rule,
    whichever way the tree is held.
    """

    reader = getattr(tree, "_accepted_reader", None)
    if reader is not None and getattr(tree, "_parent", None) is None:
        return projection_connection(reader)
    built: list[sqlite3.Connection] = []

    @contextmanager
    def connect() -> Iterator[sqlite3.Connection]:
        if not built:
            built.append(_tree_alias_index(tree))
        yield built[0]

    return connect


def tree_reference_resolver(tree: Mapping[str, bytes]) -> SubjectReferenceResolver:
    """The resolver for references read from one accepted tree (``tree_index_connection``)."""

    from cruxible_core.indexes.subject_identity import tree_key_reader

    return reference_resolver(tree_index_connection(tree), key_reader=tree_key_reader(tree))


def _owner_path(identity: str) -> str | None:
    kind, _, name = identity.partition(":")
    if kind == "Subject":
        return f"{_SUBJECTS}{name}.json"
    if kind == "Claim" and name.startswith("CLM-") and len(name) > 6:
        return f"claims/{name[4:6]}/{name}.json"
    return None


def _live_member(tree: Mapping[str, bytes], path: str) -> Any | None:
    """The live Subject or Claim at ``path`` in ``tree``, parsed; None otherwise."""

    from cruxible_client.contracts.claims import parse_claim
    from cruxible_client.contracts.subjects import parse_subject

    content = tree.get(path)
    if content is None:
        return None
    source: Any = (
        parse_subject(content, path=path)
        if path.startswith(_SUBJECTS)
        else parse_claim(content, path=path)
    )
    return source if source.lifecycle.state == "live" else None


SUBJECT_CREATION_COLLISION = "cruxible.subject.creation_collision"


def subject_collision_message(path: str, others: tuple[str, ...]) -> str:
    """The one refusal text for a new Subject that collides, at every boundary."""

    created = _ref_of(path)
    return (
        f"New Subject {created} collides with live {', '.join(others)}: its id, canonical "
        "token or alias is the same name. Name the existing Subject, add an alias to it, "
        f"or assert semantic.distinct_from between {created} and it in the same change."
    )


def _keyed(tree: Mapping[str, bytes], path: str) -> bool:
    """Whether the Subject at ``path`` is of a keyed kind: its key index is its identity check."""

    from cruxible_client.contracts.subject_kinds import SubjectKindFormatError, live_subject_kind

    kind = path.removeprefix(_SUBJECTS).partition("/")[0]
    try:
        declared = live_subject_kind(tree, kind)
    except SubjectKindFormatError:
        return False
    return declared is not None and bool(declared.keys)


def candidate_creation_collisions(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Each Subject a change creates that collides, with the live Subjects it meets.

    A Subject is created when it is live in the candidate and not live in the
    parent. A Subject of a keyed kind is left to its key index
    (``indexes.subject_identity``): this token and alias check holds minted and
    grandfathered kinds (identity-design-1009, D4). It collides with another
    live Subject of its kind, in the candidate state, whose id, canonical token
    or alias equals the new id's canonical token (the discovery normalizer).
    The candidate state is the parent's index overlaid with the changed owners
    only: a parent row whose owner the change touched is dropped, and each
    changed owner that is live in the candidate contributes its rows afresh --
    Subjects created or revived, aliases added, retirements -- never a rebuild
    of the index. A ``semantic.distinct_from``
    Claim the same change makes live exempts exactly the pair it names.
    """

    from cruxible_client.contracts.claims import SubjectClaimObject
    from cruxible_core.indexes.typed_state import vocabulary_terms

    changed = frozenset(scope)
    created: list[str] = []
    fresh_rows: list[tuple[str, str, str, str, str]] = []
    distinct: set[frozenset[str]] = set()
    for path in scope:
        if path.startswith("claims/"):
            content = candidate_tree.get(path)
            # Only alias and distinct_from Claims bear on a collision.
            if content is None or (
                b'"semantic.alias"' not in content and b'"semantic.distinct_from"' not in content
            ):
                continue
        elif not path.startswith(_SUBJECTS):
            continue
        source = _live_member(candidate_tree, path)
        if source is None:
            continue
        if path.startswith(_SUBJECTS):
            if _live_member(current_tree, path) is None and not _keyed(candidate_tree, path):
                created.append(path)
        else:
            statement = source.statement
            value = statement.object
            if statement.predicate == "semantic.distinct_from" and isinstance(
                value, SubjectClaimObject
            ):
                distinct.add(
                    frozenset({statement.subject.artifact_path, value.address.artifact_path})
                )
        fresh_rows.extend(vocabulary_terms(source, path=path))
    if not created:
        return ()

    found: list[tuple[str, tuple[str, ...]]] = []
    with tree_index_connection(current_tree)() as connection:
        for path in sorted(created):
            kind, _, rest = path.removeprefix(_SUBJECTS).partition("/")
            term = normalize_discovery_term(rest.removesuffix(".json"))
            low, high = _kind_range(kind)
            meets: set[str] = set()
            for identity, target in connection.execute(
                "SELECT source_identity, target_path FROM vocabulary_terms "
                "WHERE term=? AND basis IN ('canonical_token','alias') "
                "AND target_path>=? AND target_path<?",
                (term, low, high),
            ):
                owner = _owner_path(str(identity))
                if owner is not None and owner in changed:
                    continue  # the change re-states this owner's rows
                meets.add(str(target))
            for _identity, target, basis, row_term, _value in fresh_rows:
                if (
                    row_term == term
                    and basis in {"canonical_token", "alias"}
                    and low <= target < high
                ):
                    meets.add(target)
            colliding = tuple(
                sorted(
                    _ref_of(other)
                    for other in meets
                    if other != path
                    and frozenset({path, other}) not in distinct
                    and (
                        _live_member(candidate_tree, other) is not None
                        if other in changed
                        else _subject(connection, other, table="subjects", live=True)
                    )
                )
            )
            if colliding:
                found.append((path, colliding))
    return tuple(found)


def _tree_alias_index(tree: Mapping[str, bytes]) -> sqlite3.Connection:
    """Subjects and their alias and canonical-token terms, derived from a tree's members."""

    from cruxible_client.contracts.claims import parse_claim
    from cruxible_client.contracts.subject_kinds import parse_subject_kind
    from cruxible_client.contracts.subjects import parse_subject
    from cruxible_core.indexes.typed_state import _claim_fields, vocabulary_terms

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE subjects (path TEXT PRIMARY KEY, lifecycle TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE vocabulary_terms (source_identity TEXT, target_path TEXT, "
        "basis TEXT, term TEXT, value TEXT)"
    )
    # The key Claims of declared keyed kinds, as the projection's claims rows hold them.
    connection.execute(
        "CREATE TABLE claims (identity TEXT, subject_path TEXT, predicate TEXT, qualifier TEXT, "
        "lifecycle TEXT, object_kind TEXT, subject_selector_scheme TEXT, literal_type TEXT, "
        "literal_text TEXT, literal_integer_text TEXT, literal_boolean INTEGER)"
    )
    connection.execute(
        "CREATE TABLE subject_kinds (identity TEXT, rule TEXT, uniqueness TEXT, "
        "key_predicates TEXT, lifecycle TEXT)"
    )
    connection.execute(
        "CREATE TABLE subject_keys (subject_path TEXT PRIMARY KEY, kind TEXT, key_encoding TEXT)"
    )
    connection.execute("CREATE INDEX subject_keys_by_key ON subject_keys(kind,key_encoding)")
    key_predicates: set[str] = set()
    for path in tree:
        if path.startswith("subject-kinds/") and path.endswith(".json"):
            declared = parse_subject_kind(tree[path], path=path)
            rule = declared.identity_rule
            connection.execute(
                "INSERT INTO subject_kinds VALUES (?,?,?,?,?)",
                (
                    declared.identity.qualified,
                    rule.rule,
                    getattr(rule, "uniqueness", None),
                    json.dumps(list(declared.keys)),
                    declared.lifecycle.state,
                ),
            )
            if declared.lifecycle.state == "live":
                key_predicates.update(declared.keys)
    needles = tuple(f'"{predicate}"'.encode() for predicate in sorted(key_predicates))
    for path in tree:
        source: Any
        if path.startswith(_SUBJECTS) and path.endswith(".json"):
            source = parse_subject(tree[path], path=path)
            connection.execute("INSERT INTO subjects VALUES (?,?)", (path, source.lifecycle.state))
        elif path.startswith("claims/") and (
            b'"semantic.alias"' in tree[path] or any(needle in tree[path] for needle in needles)
        ):
            source = parse_claim(tree[path], path=path)
            if source.statement.predicate in key_predicates:
                fields = _claim_fields(source)
                connection.execute(
                    "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source.identity.qualified,
                        fields["subject_path"],
                        fields["predicate"],
                        fields["qualifier"],
                        source.lifecycle.state,
                        fields["object_kind"],
                        fields["subject_selector_scheme"],
                        fields.get("literal_type"),
                        fields.get("literal_text"),
                        fields.get("literal_integer_text"),
                        fields.get("literal_boolean"),
                    ),
                )
        else:
            continue
        connection.executemany(
            "INSERT INTO vocabulary_terms VALUES (?,?,?,?,?)",
            vocabulary_terms(source, path=path),
        )
    from cruxible_core.indexes.subject_identity import refresh_subject_keys

    refresh_subject_keys(connection)
    return connection


__all__ = [
    "SubjectReferenceAmbiguous",
    "SubjectReferenceResolver",
    "SubjectRefResolution",
    "SUBJECT_CREATION_COLLISION",
    "candidate_creation_collisions",
    "subject_collision_message",
    "projection_connection",
    "tree_index_connection",
    "reference_resolver",
    "tree_reference_resolver",
    "creation_collisions",
    "resolve_reference_text",
    "resolve_subject_ref",
    "split_reference",
]
