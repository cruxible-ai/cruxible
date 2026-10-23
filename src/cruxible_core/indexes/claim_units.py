"""The unit index and the polarity law: a unit holds values or one none, never both.

A ClaimType v9 that declares an inverse partitions each of its slots
``(subject, predicate, qualifier)`` into units: the whole slot (key ``slot``),
one object (key ``object``) or one key value of the object Subject (key
``object_key``). ``claim_units`` holds one row per live Claim of such a type:
its unit and its polarity (``positive`` for a value, ``inverse`` for a none).

Upkeep follows the change, never the instance. An accepted generation
recomputes the rows of the Claims it writes, of the positives pointing at a
Subject whose key Claim it writes (the fan-in of that one Subject, through
``claims_by_object_subject``), and of every Claim of a ClaimType whose inverse
key it changes (that type's Claims, once). Every row is a function of the
``claims`` and ``claim_types`` rows, so a cold build (``refresh_claim_units``
with no change) yields the same rows.

The candidate boundary (``candidate_polarity_issues``) reads the parent's rows
for the units a change touches, laid over the units the change itself leaves:
O(1) seeks per changed Claim, plus the same fan-in. It refuses a change set
after which a unit holds a live value and a live none
(``cruxible.claim.polarity_conflict``), and a flip under an ``object_key``
inverse that leaves its unit (``cruxible.claim.flip_unit_moved``: the Claim law
cannot read the object's key, the boundary can).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.primitives import canonical_json
from cruxible_core.indexes.sql_batch import execute_in, insert_rows, select_in

SQLValue = str | int | None

#: The unit index and its seeks. ``unit`` is ``''`` for a slot unit, else the
#: unit encoding (``object_unit_text``) of the object or key value.
SCHEMA_STATEMENTS = (
    """CREATE TABLE claim_units (
            claim_identity TEXT PRIMARY KEY NOT NULL,
            subject_path TEXT NOT NULL, predicate TEXT NOT NULL, qualifier TEXT,
            unit_kind TEXT NOT NULL CHECK(unit_kind IN ('slot','object','object_key')),
            unit TEXT NOT NULL,
            polarity TEXT NOT NULL CHECK(polarity IN ('positive','inverse')),
            object_path TEXT
        ) STRICT""",
    "CREATE INDEX claim_units_by_unit ON claim_units"
    "(subject_path,predicate,unit,qualifier,polarity,claim_identity)",
    "CREATE INDEX claim_units_by_object ON claim_units(object_path,claim_identity) "
    "WHERE object_path IS NOT NULL",
    "CREATE INDEX claim_units_by_predicate ON claim_units(predicate,claim_identity)",
    "CREATE INDEX claim_types_by_inverse_key ON claim_types(inverse_key_predicate,identity) "
    "WHERE inverse_key_predicate IS NOT NULL",
)

POLARITY_CONFLICT = "cruxible.claim.polarity_conflict"
FLIP_UNIT_MOVED = "cruxible.claim.flip_unit_moved"

#: The ``claims`` columns a unit is read from.
_FACT_COLUMNS = (
    "identity",
    "path",
    "subject_path",
    "predicate",
    "qualifier",
    "lifecycle",
    "claim_type_identity",
    "subject_selector_scheme",
    "object_kind",
    "object_path",
    "object_selector_scheme",
    "object_selector_value",
    "literal_type",
    "literal_text",
    "literal_integer_text",
    "literal_boolean",
    "literal_digest",
    "none_of",
)
_FACTS_SQL = f"SELECT {','.join(_FACT_COLUMNS)} FROM main.claims WHERE identity IN ({{}})"
# ``+lifecycle`` keeps the planner on ``claims_by_subject_predicate`` (one seek).
_KEY_VALUES_SQL = (
    "SELECT identity, literal_type, literal_text, literal_integer_text, literal_boolean "
    "FROM main.claims WHERE subject_path=? AND predicate=? AND +lifecycle='live' "
    "AND object_kind='literal' AND qualifier IS NULL AND subject_selector_scheme='artifact-v1'"
)
_POINTING_SQL = (
    "SELECT identity, claim_type_identity FROM main.claims "
    "WHERE object_path=? AND object_kind='subject' AND lifecycle='live'"
)
_TYPE_CLAIMS_SQL = (
    "SELECT identity FROM main.claims WHERE predicate=? AND lifecycle='live' "
    "AND claim_type_identity=?"
)
_UNIT_HOLDERS_SQL = (
    "SELECT claim_identity, polarity FROM main.claim_units WHERE subject_path=? AND predicate=? "
    "AND unit=? AND qualifier IS ? AND unit_kind=?"
)

InverseKey = tuple[str, str | None]
Facts = Mapping[str, SQLValue]


def literal_value_digest(value: object) -> str:
    """The digest naming an array or object literal in the unit index."""

    from cruxible_client.contracts.canonical import canonical_bytes

    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def object_unit_text(fields: Facts) -> str | None:
    """The unit encoding of one object, from its ``claims`` columns; None for exact content.

    A Subject object is its address; a literal is its type and value (an array
    or object by its digest). An ``object_key`` unit encodes its key value the
    same way, as a literal, so an inverse's ``of`` and the key a positive's
    object holds compare as text.
    """

    kind = fields.get("object_kind")
    if kind == "subject":
        return canonical_json(
            [
                "subject",
                fields.get("object_path"),
                fields.get("object_selector_scheme"),
                fields.get("object_selector_value"),
            ]
        )
    if kind != "literal":
        return None
    literal_type = fields.get("literal_type")
    value: SQLValue = {
        "string": fields.get("literal_text"),
        "integer": fields.get("literal_integer_text"),
        "boolean": fields.get("literal_boolean"),
        "array": fields.get("literal_digest"),
        "object": fields.get("literal_digest"),
    }.get(str(literal_type))
    return canonical_json(["literal", literal_type, value])


def render_unit(unit_kind: str, unit: str) -> str:
    """A unit as a reader names it: ``the slot``, an object, or a key value."""

    if unit_kind == "slot":
        return "the slot"
    decoded = json.loads(unit)
    if decoded[0] == "subject":
        path = str(decoded[1])
        ref = path.removeprefix("subjects/").removesuffix(".json")
        return f"object {ref}"
    value = decoded[2]
    if decoded[1] == "boolean":
        value = bool(value)
    return f"key {value!r}" if unit_kind == "object_key" else f"object {value!r}"


@dataclass(frozen=True)
class UnitRow:
    """One live Claim's unit and polarity."""

    claim_identity: str
    subject_path: str
    predicate: str
    qualifier: str | None
    unit_kind: str
    unit: str
    polarity: str
    object_path: str | None

    @property
    def unit_key(self) -> tuple[str, str, str | None, str, str]:
        return (self.subject_path, self.predicate, self.qualifier, self.unit_kind, self.unit)

    def values(self) -> tuple[SQLValue, ...]:
        return (
            self.claim_identity,
            self.subject_path,
            self.predicate,
            self.qualifier,
            self.unit_kind,
            self.unit,
            self.polarity,
            self.object_path,
        )


def unit_row(
    facts: Facts,
    inverse: InverseKey | None,
    key_value: Callable[[str, str], str | None],
) -> UnitRow | None:
    """The unit row of one Claim under ``inverse``, or None (retired, no inverse, unreadable).

    ``key_value(subject_path, predicate)`` is the unit encoding of the one live
    key value an object Subject holds (None: none or several); it is read only
    for a positive under an ``object_key`` inverse.
    """

    if inverse is None or facts.get("lifecycle") != "live":
        return None
    kind, key_predicate = inverse
    polarity = "inverse" if facts.get("object_kind") == "none" else "positive"
    object_path: str | None = None
    unit: str | None
    if kind == "slot":
        unit = ""
    elif polarity == "inverse":
        unit = None if facts.get("none_of") is None else str(facts["none_of"])
    elif kind == "object":
        unit = object_unit_text(facts)
    elif facts.get("object_kind") == "subject" and key_predicate is not None:
        object_path = str(facts["object_path"])
        unit = key_value(object_path, key_predicate)
    else:
        unit = None
    if unit is None:
        return None
    qualifier = facts.get("qualifier")
    return UnitRow(
        claim_identity=str(facts["identity"]),
        subject_path=str(facts["subject_path"]),
        predicate=str(facts["predicate"]),
        qualifier=None if qualifier is None else str(qualifier),
        unit_kind=kind,
        unit=unit,
        polarity=polarity,
        object_path=object_path,
    )


def _key_text(rows: Iterable[tuple[Any, ...]]) -> str | None:
    found = [row for row in rows]
    if len(found) != 1 or found[0][1] not in ("string", "integer", "boolean"):
        return None
    _identity, literal_type, text, integer_text, boolean = found[0]
    return object_unit_text(
        {
            "object_kind": "literal",
            "literal_type": literal_type,
            "literal_text": text,
            "literal_integer_text": integer_text,
            "literal_boolean": boolean,
        }
    )


def parent_key_value(connection: sqlite3.Connection) -> Callable[[str, str], str | None]:
    """An object Subject's one live key value in ``connection`` (one seek per Subject)."""

    known: dict[tuple[str, str], str | None] = {}

    def read(subject_path: str, predicate: str) -> str | None:
        if (subject_path, predicate) not in known:
            known[(subject_path, predicate)] = _key_text(
                connection.execute(_KEY_VALUES_SQL, (subject_path, predicate))
            )
        return known[(subject_path, predicate)]

    return read


def _facts(connection: sqlite3.Connection, identities: Iterable[str]) -> dict[str, dict[str, Any]]:
    return {
        str(row[0]): dict(zip(_FACT_COLUMNS, row, strict=True))
        for row in select_in(connection, _FACTS_SQL, sorted(set(identities)))
    }


def _parent_inverses(connection: sqlite3.Connection, identities: Iterable[str]) -> dict[str, Any]:
    return {
        str(identity): None if kind is None else (str(kind), predicate)
        for identity, kind, predicate in select_in(
            connection,
            "SELECT identity, inverse_key, inverse_key_predicate FROM main.claim_types "
            "WHERE identity IN ({})",
            sorted(set(identities)),
        )
    }


def _keyed_types(connection: sqlite3.Connection, predicate: str) -> set[str]:
    """The ClaimTypes whose inverse is keyed by ``predicate`` (one index seek)."""

    return {
        str(row[0])
        for row in connection.execute(
            "SELECT identity FROM main.claim_types "
            "WHERE inverse_key_predicate=? AND inverse_key='object_key'",
            (predicate,),
        )
    }


def _pointing(connection: sqlite3.Connection, subject_path: str, types: set[str]) -> set[str]:
    """Live positives whose object is ``subject_path``, of the named types (its fan-in)."""

    return {
        str(identity)
        for identity, claim_type in connection.execute(_POINTING_SQL, (subject_path,))
        if claim_type in types
    }


def refresh_claim_units(
    connection: sqlite3.Connection,
    *,
    claims: Iterable[str] | None = None,
    key_subjects: Iterable[tuple[str, str]] = (),
    claim_types: Iterable[str] = (),
) -> None:
    """Bring ``claim_units`` up to date with the ``claims`` and ``claim_types`` rows.

    ``claims=None`` rebuilds the table (a fresh projection or a cold check).
    Otherwise only these are recomputed: the named Claims (written by the
    change, old and new rows alike), the positives pointing at each
    ``(subject_path, predicate)`` of ``key_subjects`` whose predicate keys an
    inverse (a key Claim the change wrote), and every Claim of each of
    ``claim_types`` (its inverse key changed).
    """

    if claims is None:
        connection.execute("DELETE FROM main.claim_units")
        targets = {
            str(row[0])
            for row in connection.execute(
                "SELECT c.identity FROM main.claims c JOIN main.claim_types t "
                "ON t.identity=c.claim_type_identity "
                "WHERE t.inverse_key IS NOT NULL AND c.lifecycle='live'"
            )
        }
    else:
        targets = set(claims)
        keyed: dict[str, set[str]] = {}
        for subject_path, predicate in sorted(set(key_subjects)):
            if predicate not in keyed:
                keyed[predicate] = _keyed_types(connection, predicate)
            if keyed[predicate]:
                targets |= _pointing(connection, subject_path, keyed[predicate])
            targets.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT claim_identity FROM main.claim_units WHERE object_path=?",
                    (subject_path,),
                )
            )
        for identity in sorted(set(claim_types)):
            row = connection.execute(
                "SELECT predicate FROM main.claim_types WHERE identity=?", (identity,)
            ).fetchone()
            predicate = identity.removeprefix("ClaimType:") if row is None else str(row[0])
            targets.update(
                str(found[0])
                for found in connection.execute(_TYPE_CLAIMS_SQL, (predicate, identity))
            )
            targets.update(
                str(found[0])
                for found in connection.execute(
                    "SELECT claim_identity FROM main.claim_units WHERE predicate=?", (predicate,)
                )
            )
        execute_in(
            connection, "DELETE FROM main.claim_units WHERE claim_identity IN ({})", sorted(targets)
        )
    facts = _facts(connection, targets)
    inverses = _parent_inverses(
        connection, {str(item["claim_type_identity"]) for item in facts.values()}
    )
    key_value = parent_key_value(connection)
    rows = []
    for identity in sorted(facts):
        item = facts[identity]
        row = unit_row(item, inverses.get(str(item["claim_type_identity"])), key_value)
        if row is not None:
            rows.append(row.values())
    insert_rows(connection, "main.claim_units", rows)


# -- the candidate boundary ----------------------------------------------------------


@dataclass(frozen=True)
class PolarityIssue:
    code: str
    path: str
    message: str


def _inverse_key(claim_type: Any) -> InverseKey | None:
    inverse = getattr(claim_type, "inverse", None)
    if inverse is None:
        return None
    return (inverse.key.kind, getattr(inverse.key, "predicate", None))


def _ref(path: str) -> str:
    return path.removeprefix("subjects/").removesuffix(".json")


def _claim_ref(identity: str) -> str:
    return identity.removeprefix("Claim:")


def polarity_issues(
    parent: sqlite3.Connection,
    *,
    claims: Mapping[str, Facts],
    claim_types: Mapping[str, Any],
    claim_type_of: Callable[[str], Any],
) -> tuple[PolarityIssue, ...]:
    """The polarity law over one change, judged against its parent's unit index.

    ``claims`` holds the change's Claims as they stand after it (their
    ``claims`` columns plus ``identity``, ``path`` and ``lifecycle``);
    ``claim_types`` the ClaimTypes it writes, by identity (None: removed);
    ``claim_type_of(identity)`` reads any ClaimType after the change. The
    parent's own rows are read only for the units the change touches.
    """

    in_scope = set(claims)
    parent_facts = _facts(parent, in_scope)
    parent_key_value_of = parent_key_value(parent)
    candidate_types: dict[str, Any] = {}

    def candidate_type(identity: str) -> Any:
        if identity not in candidate_types:
            candidate_types[identity] = (
                claim_types[identity] if identity in claim_types else claim_type_of(identity)
            )
        return candidate_types[identity]

    parent_types = _parent_inverses(parent, claim_types)

    def keyed_types(predicate: str) -> set[str]:
        found = {item for item in _keyed_types(parent, predicate) if item not in claim_types}
        found.update(
            identity
            for identity, claim_type in claim_types.items()
            if _inverse_key(claim_type) == ("object_key", predicate)
        )
        return found

    # The Claims whose unit the change may set, with the member that moved each.
    targets: dict[str, Facts] = dict(claims)
    cause: dict[str, str] = {identity: str(facts["path"]) for identity, facts in claims.items()}
    key_subjects: dict[tuple[str, str], str] = {}
    for identity, facts in (*claims.items(), *parent_facts.items()):
        pair = (str(facts["subject_path"]), str(facts["predicate"]))
        key_subjects.setdefault(pair, cause[identity])
    keyed: dict[str, set[str]] = {}
    moved: dict[str, str] = {}
    for (subject_path, predicate), member in sorted(key_subjects.items()):
        if predicate not in keyed:
            keyed[predicate] = keyed_types(predicate)
        if keyed[predicate]:
            for identity in _pointing(parent, subject_path, keyed[predicate]) - in_scope:
                moved.setdefault(identity, member)
    for identity, claim_type in sorted(claim_types.items()):
        after = _inverse_key(claim_type)
        if after is None or after == parent_types.get(identity):
            continue
        predicate = str(claim_type.predicate)
        for (found,) in parent.execute(_TYPE_CLAIMS_SQL, (predicate, identity)):
            if str(found) not in in_scope:
                moved.setdefault(str(found), claim_types_path(predicate))
    for identity, facts in _facts(parent, moved).items():
        targets[identity] = facts
        cause[identity] = moved[identity]

    def candidate_key_value(subject_path: str, predicate: str) -> str | None:
        rows = [
            row
            for row in parent.execute(_KEY_VALUES_SQL, (subject_path, predicate))
            if str(row[0]) not in in_scope
        ]
        rows.extend(
            (
                identity,
                facts.get("literal_type"),
                facts.get("literal_text"),
                facts.get("literal_integer_text"),
                facts.get("literal_boolean"),
            )
            for identity, facts in claims.items()
            if facts.get("subject_path") == subject_path
            and facts.get("predicate") == predicate
            and facts.get("lifecycle") == "live"
            and facts.get("object_kind") == "literal"
            and facts.get("qualifier") is None
            and facts.get("subject_selector_scheme") == "artifact-v1"
        )
        return _key_text(rows)

    rows: list[UnitRow] = []
    for identity, facts in sorted(targets.items()):
        row = unit_row(
            facts,
            _inverse_key(candidate_type(str(facts["claim_type_identity"]))),
            candidate_key_value,
        )
        if row is not None:
            rows.append(row)
    issues: list[PolarityIssue] = []
    by_unit: dict[tuple[str, str, str | None, str, str], list[UnitRow]] = {}
    for row in rows:
        by_unit.setdefault(row.unit_key, []).append(row)
    for unit_key, written in sorted(by_unit.items(), key=lambda item: repr(item[0])):
        subject_path, predicate, qualifier, unit_kind, unit = unit_key
        holders = {
            str(identity): str(polarity)
            for identity, polarity in parent.execute(
                _UNIT_HOLDERS_SQL, (subject_path, predicate, unit, qualifier, unit_kind)
            )
            if str(identity) not in targets
        }
        holders.update((row.claim_identity, row.polarity) for row in written)
        positives = sorted(item for item, polarity in holders.items() if polarity == "positive")
        inverses = sorted(item for item, polarity in holders.items() if polarity == "inverse")
        if not positives or not inverses:
            continue
        claim_type = candidate_type(str(targets[written[0].claim_identity]["claim_type_identity"]))
        if (
            unit_kind == "slot"
            and claim_type is not None
            and claim_type.resolution_policy.conflict_result == "unresolved"
        ):
            # One-valued rivals: a value and a none contend, and read as disputed.
            continue
        named = ", ".join(_claim_ref(item) for item in positives)
        issues.append(
            PolarityIssue(
                POLARITY_CONFLICT,
                cause[written[0].claim_identity],
                f"{_ref(subject_path)} {predicate} would hold both a value ({named}) and none "
                f"({', '.join(_claim_ref(item) for item in inverses)}) for "
                f"{render_unit(unit_kind, unit)}. A unit holds values or one none, never both: "
                "revise the opposite Claim into this one (a flip), or retire it in the same "
                "change.",
            )
        )
    # A flip under an object_key inverse stays in its unit: the key value it
    # names is read from state, which the Claim law does not hold.
    by_identity = {row.claim_identity: row for row in rows}
    for identity, facts in sorted(claims.items()):
        before = parent_facts.get(identity)
        if before is None or facts.get("lifecycle") != "live":
            continue
        if (before.get("object_kind") == "none") == (facts.get("object_kind") == "none"):
            continue
        inverse = _inverse_key(candidate_type(str(facts["claim_type_identity"])))
        if inverse is None or inverse[0] != "object_key":
            continue
        old = unit_row({**before, "lifecycle": "live"}, inverse, parent_key_value_of)
        new = by_identity.get(identity)
        if old is not None and new is not None and old.unit == new.unit:
            continue
        issues.append(
            PolarityIssue(
                FLIP_UNIT_MOVED,
                str(facts["path"]),
                f"A flip stays in its unit: {_claim_ref(identity)} held "
                f"{'an unreadable unit' if old is None else render_unit('object_key', old.unit)} "
                f"of {inverse[1]} and now names "
                f"{'an unreadable unit' if new is None else render_unit('object_key', new.unit)}. "
                "State the none for the key the value held, or retire this Claim and write "
                "the other.",
            )
        )
    return tuple(issues)


def claim_types_path(predicate: str) -> str:
    from cruxible_client.contracts.claim_types import claim_type_path

    return claim_type_path(predicate)


# -- the boundary over trees -----------------------------------------------------------


def _claim_facts(content: bytes, path: str) -> dict[str, Any]:
    from cruxible_client.contracts.claims import parse_claim
    from cruxible_core.indexes.typed_state import _claim_fields

    claim = parse_claim(content, path=path)
    return {
        **_claim_fields(claim),
        "identity": claim.identity.qualified,
        "path": path,
        "lifecycle": claim.lifecycle.state,
    }


_COLD_SQL = (
    """CREATE TABLE claims (
        identity TEXT PRIMARY KEY, path TEXT, subject_path TEXT, predicate TEXT, qualifier TEXT,
        lifecycle TEXT, claim_type_identity TEXT, subject_selector_scheme TEXT,
        object_kind TEXT, object_path TEXT, object_selector_scheme TEXT,
        object_selector_value TEXT, literal_type TEXT, literal_text TEXT,
        literal_integer_text TEXT, literal_boolean INTEGER, literal_digest TEXT, none_of TEXT
    )""",
    "CREATE INDEX claims_by_subject_predicate ON claims(subject_path,predicate)",
    "CREATE INDEX claims_by_object_subject ON claims(object_path) WHERE object_kind='subject'",
    "CREATE INDEX claims_by_predicate ON claims(predicate,lifecycle)",
    """CREATE TABLE claim_types (
        identity TEXT PRIMARY KEY, predicate TEXT, inverse_key TEXT, inverse_key_predicate TEXT
    )""",
    *SCHEMA_STATEMENTS,
)


def _cold_index(tree: Mapping[str, bytes], extra_types: Iterable[Any]) -> sqlite3.Connection:
    """The unit index of a tree with no projection, derived from its members (cold only).

    Only Claims of an inverse ClaimType (the tree's, or ``extra_types`` a change
    writes) and of a key predicate an inverse names are read; every row is
    derived exactly as an accepted projection derives it.
    """

    from cruxible_client.contracts.claim_types import parse_claim_type

    connection = sqlite3.connect(":memory:")
    for statement in _COLD_SQL:
        connection.execute(statement)
    predicates: set[str] = set()
    for path in tree:
        if not (path.startswith("claim-types/") and path.endswith(".json")):
            continue
        claim_type = parse_claim_type(tree[path], path=path)
        key = _inverse_key(claim_type)
        connection.execute(
            "INSERT INTO claim_types VALUES (?,?,?,?)",
            (
                claim_type.identity.qualified,
                claim_type.predicate,
                None if key is None else key[0],
                None if key is None else key[1],
            ),
        )
        if key is not None:
            predicates.add(claim_type.predicate)
            if key[1] is not None:
                predicates.add(key[1])
    for claim_type in extra_types:
        key = _inverse_key(claim_type)
        if key is not None:
            predicates.add(claim_type.predicate)
            if key[1] is not None:
                predicates.add(key[1])
    needles = tuple(f'"{predicate}"'.encode() for predicate in sorted(predicates))
    rows = []
    for path in tree:
        if not path.startswith("claims/") or not any(needle in tree[path] for needle in needles):
            continue
        facts = _claim_facts(tree[path], path)
        if facts["predicate"] in predicates:
            rows.append(tuple(facts.get(column) for column in _FACT_COLUMNS))
    insert_rows(connection, f"claims ({','.join(_FACT_COLUMNS)})", rows)
    refresh_claim_units(connection)
    return connection


def _parent_index(
    tree: Mapping[str, bytes], extra_types: Iterable[Any]
) -> AbstractContextManager[sqlite3.Connection]:
    reader = getattr(tree, "_accepted_reader", None)
    if reader is not None and getattr(tree, "_parent", None) is None:
        from cruxible_core.indexes.subject_aliases import projection_connection

        return projection_connection(reader)()

    @contextmanager
    def cold() -> Iterator[sqlite3.Connection]:
        connection = _cold_index(tree, extra_types)
        try:
            yield connection
        finally:
            connection.close()

    return cold()


def candidate_polarity_issues(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> tuple[PolarityIssue, ...]:
    """The polarity law of one change set, judged at the candidate boundary.

    Admission and settlement both evaluate here, at the head they land on, so
    two changes that each pass alone cannot both land when together they would
    leave a unit holding both polarities: the second is judged against the
    first's accepted state.
    """

    from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type

    claim_paths = [path for path in scope if path.startswith("claims/")]
    type_paths = [path for path in scope if path.startswith("claim-types/")]
    if not claim_paths and not type_paths:
        return ()
    claims = {
        facts["identity"]: facts
        for facts in (
            _claim_facts(candidate_tree[path], path)
            for path in claim_paths
            if path in candidate_tree
        )
    }
    claim_types: dict[str, Any] = {}
    for path in type_paths:
        if path in candidate_tree:
            parsed = parse_claim_type(candidate_tree[path], path=path)
            claim_types[parsed.identity.qualified] = parsed
        elif path in current_tree:
            claim_types[parse_claim_type(current_tree[path], path=path).identity.qualified] = None

    def claim_type_of(identity: str) -> Any:
        path = claim_type_path(identity.removeprefix("ClaimType:"))
        content = candidate_tree.get(path)
        return None if content is None else parse_claim_type(content, path=path)

    with _parent_index(
        current_tree, [item for item in claim_types.values() if item is not None]
    ) as parent:
        return polarity_issues(
            parent, claims=claims, claim_types=claim_types, claim_type_of=claim_type_of
        )


__all__ = [
    "FLIP_UNIT_MOVED",
    "POLARITY_CONFLICT",
    "PolarityIssue",
    "SCHEMA_STATEMENTS",
    "UnitRow",
    "candidate_polarity_issues",
    "literal_value_digest",
    "object_unit_text",
    "parent_key_value",
    "polarity_issues",
    "refresh_claim_units",
    "render_unit",
    "unit_row",
]
