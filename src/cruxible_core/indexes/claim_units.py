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
bounded seeks per changed Claim (a written value seeks the unit's nones, a
written none the first few values; neither enumerates its own polarity), plus
the same fan-in. It refuses a change set after which a unit holds a live value
and a live none (``cruxible.claim.polarity_conflict``; one-valued rivals under
``conflict_result: unresolved`` may dispute only when the write declared a
contend, and only against opposites live in the change's own base), more than
one none
(``cruxible.claim.inverse_duplicate``), a flip under an ``object_key`` inverse
that leaves its unit (``cruxible.claim.flip_unit_moved``: the Claim law cannot
read the object's key, the boundary can), and a retirement riding along with a
flip that does not end as superseded (``cruxible.claim.flip_retirement_reason``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from functools import partial
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
INVERSE_DUPLICATE = "cruxible.claim.inverse_duplicate"
FLIP_RETIREMENT_REASON = "cruxible.claim.flip_retirement_reason"

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
    "SELECT claim_identity FROM main.claim_units WHERE subject_path=? AND predicate=? "
    "AND unit=? AND qualifier IS ? AND polarity=? AND unit_kind=?"
)
_UNIT_OF_SQL = (
    "SELECT subject_path, predicate, qualifier, unit_kind, unit FROM main.claim_units "
    "WHERE claim_identity=?"
)

InverseKey = tuple[str, str | None]
#: A unit: (subject_path, predicate, qualifier, unit_kind, unit).
UnitKey = tuple[str, str, str | None, str, str]
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
    def unit_key(self) -> UnitKey:
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


def _polarity_of(facts: Facts) -> str:
    return "inverse" if facts.get("object_kind") == "none" else "positive"


#: The holders a refusal names. A unit's population is never enumerated to judge
#: a change: each seek stops once it has found what the law asks about.
_SHOWN = 5


def _holders(
    parent: sqlite3.Connection,
    unit_key: UnitKey,
    polarity: str,
    *,
    skip: Mapping[str, object],
    limit: int | None,
) -> list[str]:
    """Up to ``limit`` of the parent's ``polarity`` holders of one unit, bar ``skip``.

    One index range, read lazily: the rows of ``skip`` (the Claims the change
    rewrites) are passed over, so the work is the change's own rows plus
    ``limit``. ``limit=None`` reads the whole range (a one-valued slot's rivals).
    """

    subject_path, predicate, qualifier, unit_kind, unit = unit_key
    found: list[str] = []
    cursor = parent.execute(
        _UNIT_HOLDERS_SQL, (subject_path, predicate, unit, qualifier, polarity, unit_kind)
    )
    try:
        for (identity,) in cursor:
            if str(identity) in skip:
                continue
            found.append(str(identity))
            if limit is not None and len(found) >= limit:
                break
    finally:
        cursor.close()
    return found


_UNIT_MEMBERS_SQL = (
    "SELECT claim_identity FROM main.claim_units WHERE subject_path=? AND predicate=? "
    "AND unit=? AND qualifier IS ? AND polarity=? AND unit_kind=? AND claim_identity IN ({})"
)


def _claim_identity_of(path: str) -> str | None:
    if not path.startswith("claims/") or not path.endswith(".json"):
        return None
    return "Claim:" + path.rsplit("/", 1)[-1].removesuffix(".json")


def _unseen_opposites(
    parent: sqlite3.Connection,
    unit_key: UnitKey,
    polarity: str,
    *,
    written: list[UnitRow],
    skip: Mapping[str, object],
    seen: Callable[[str, str], bool],
    rebased: bool,
    base_changes: frozenset[str] | None,
) -> list[str]:
    """The ``polarity`` holders of a disputed unit its writer did not see.

    The change's own rows are judged as its base held them. A parent row the
    change leaves alone was seen when the base is the parent, so a current-base
    dispute reads nothing more. Rebased, a holder unseen in the base landed
    meanwhile: with the paths that changed between base and parent
    (``base_changes``) only those are read, one keyed row each; without them
    the unit's holders of that polarity are read (rebased history only).
    """

    unseen = [row.claim_identity for row in written if not seen(row.claim_identity, polarity)]
    if not rebased:
        return unseen
    if base_changes is None:
        holders = _holders(parent, unit_key, polarity, skip=skip, limit=None)
    else:
        landed = sorted(
            identity
            for identity in map(_claim_identity_of, base_changes)
            if identity is not None and identity not in skip
        )
        subject_path, predicate, qualifier, unit_kind, unit = unit_key
        holders = (
            [
                str(row[0])
                for row in select_in(
                    parent,
                    _UNIT_MEMBERS_SQL,
                    landed,
                    prefix=(subject_path, predicate, unit, qualifier, polarity, unit_kind),
                )
            ]
            if landed
            else []
        )
    return unseen + [identity for identity in holders if not seen(identity, polarity)]


def _named(identities: list[str]) -> str:
    shown = ", ".join(_claim_ref(item) for item in identities[:_SHOWN])
    return shown + (", ..." if len(identities) > _SHOWN else "")


def polarity_issues(
    parent: sqlite3.Connection,
    *,
    claims: Mapping[str, Facts],
    claim_types: Mapping[str, Any],
    claim_type_of: Callable[[str], Any],
    base_claim: Callable[[str], Facts | None] | None = None,
    contending: frozenset[str] = frozenset(),
    base_changes: frozenset[str] | None = None,
) -> tuple[PolarityIssue, ...]:
    """The polarity law over one change, judged against its parent's unit index.

    ``claims`` holds the change's Claims as they stand after it (their
    ``claims`` columns plus ``identity``, ``path``, ``lifecycle`` and, for a
    retirement, ``retirement_reason``); ``claim_types`` the ClaimTypes it
    writes, by identity (None: removed); ``claim_type_of(identity)`` reads any
    ClaimType after the change. ``base_claim(identity)`` reads a Claim as the
    change's own base held it, when the change was made against an earlier
    head than ``parent`` (a rebase); None: its base is the parent;
    ``base_changes`` then names the paths that differ between the two, when
    known. ``contending`` names the changed Claims whose write declared a
    contend: only those may enter a one-valued dispute, and seeing the opposite
    in the base is never that declaration. The parent's rows are read only for
    the units the change touches, and only as far as each check needs: a value
    written into a unit seeks its nones, a none written seeks the values, and
    neither enumerates its own polarity.
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

    # The key values the change itself states, by (Subject, key predicate).
    change_keys: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for identity, facts in claims.items():
        if (
            facts.get("lifecycle") == "live"
            and facts.get("object_kind") == "literal"
            and facts.get("qualifier") is None
            and facts.get("subject_selector_scheme") == "artifact-v1"
        ):
            change_keys.setdefault(
                (str(facts.get("subject_path")), str(facts.get("predicate"))), []
            ).append(
                (
                    identity,
                    facts.get("literal_type"),
                    facts.get("literal_text"),
                    facts.get("literal_integer_text"),
                    facts.get("literal_boolean"),
                )
            )
    candidate_keys: dict[tuple[str, str], str | None] = {}

    def candidate_key_value(subject_path: str, predicate: str) -> str | None:
        if (subject_path, predicate) not in candidate_keys:
            rows = [
                row
                for row in parent.execute(_KEY_VALUES_SQL, (subject_path, predicate))
                if str(row[0]) not in in_scope
            ]
            rows.extend(change_keys.get((subject_path, predicate), ()))
            candidate_keys[(subject_path, predicate)] = _key_text(rows)
        return candidate_keys[(subject_path, predicate)]

    rows: list[UnitRow] = []
    for identity, facts in sorted(targets.items()):
        row = unit_row(
            facts,
            _inverse_key(candidate_type(str(facts["claim_type_identity"]))),
            candidate_key_value,
        )
        if row is not None:
            rows.append(row)

    def seen(identity: str, polarity: str, slot: tuple[str, str, str | None]) -> bool:
        """Whether the change's author saw this Claim hold ``polarity`` in ``slot``.

        A one-valued dispute is declared (``contending``) and stands only
        against Claims its writer could read: the opposite was live, in that
        slot and polarity, in the base the change was made against. A Claim the
        change writes was seen only as its base held it; one the parent holds
        that the change does not touch was seen when the base is the parent.
        """

        before: Facts | None
        if base_claim is None:
            if identity not in claims:
                return True
            before = parent_facts.get(identity)
        else:
            before = base_claim(identity)
        return (
            before is not None
            and before.get("lifecycle") == "live"
            and (before.get("subject_path"), before.get("predicate"), before.get("qualifier"))
            == slot
            and _polarity_of(before) == polarity
        )

    issues: list[PolarityIssue] = []
    by_unit: dict[UnitKey, list[UnitRow]] = {}
    for row in rows:
        by_unit.setdefault(row.unit_key, []).append(row)
    for unit_key, written in sorted(by_unit.items(), key=lambda item: repr(item[0])):
        subject_path, predicate, qualifier, unit_kind, unit = unit_key
        where = f"{_ref(subject_path)} {predicate}"
        shown_unit = render_unit(unit_kind, unit)
        written_values = [row.claim_identity for row in written if row.polarity == "positive"]
        written_nones = [row.claim_identity for row in written if row.polarity == "inverse"]
        # At most one none: two are found by a seek of two.
        nones = written_nones + _holders(parent, unit_key, "inverse", skip=targets, limit=2)
        if len(nones) > 1:
            first = written_nones[0] if written_nones else written[0].claim_identity
            issues.append(
                PolarityIssue(
                    INVERSE_DUPLICATE,
                    cause[first],
                    f"{where} would hold more than one none ({_named(nones)}) for "
                    f"{shown_unit}. A unit holds values or one none: restate the live none "
                    "by revising it, or retire it in the same change.",
                )
            )
            continue
        if not nones:
            continue
        # A none in the unit: the values it would sit beside. A written value
        # needs no seek (it is one); a written none seeks the first few.
        values = written_values + (
            _holders(parent, unit_key, "positive", skip=targets, limit=_SHOWN + 1)
            if written_nones
            else []
        )
        if not values:
            continue
        claim_type = candidate_type(str(targets[written[0].claim_identity]["claim_type_identity"]))
        if (
            unit_kind == "slot"
            and claim_type is not None
            and claim_type.resolution_policy.conflict_result == "unresolved"
        ):
            # One-valued rivals may dispute, but only deliberately: each Claim
            # the change writes into the dispute was written to contend (the
            # write declared it), and stands only against opposites its writer
            # saw, never against one that landed meanwhile. Rivals already
            # disputing in the base start no new dispute.
            slot = (subject_path, predicate, qualifier)
            entering = [row for row in written if not seen(row.claim_identity, row.polarity, slot)]
            if not entering:
                continue
            undeclared = [row for row in entering if row.claim_identity not in contending]
            if undeclared:
                issues.append(
                    PolarityIssue(
                        POLARITY_CONFLICT,
                        cause[undeclared[0].claim_identity],
                        f"{where} would hold both a value ({_named(values)}) and none "
                        f"({_named(nones)}) for {shown_unit}, and "
                        f"{_named([row.claim_identity for row in undeclared])} was not "
                        "written to contend. A one-valued field is disputed only by a write "
                        "that contends with the value its writer read: revise the opposite "
                        "Claim into this one (a flip), retire it in the same change, or "
                        "write this one with contend.",
                    )
                )
                continue
            blind = sorted(
                {
                    other
                    for opposite in {
                        "inverse" if row.polarity == "positive" else "positive" for row in entering
                    }
                    for other in _unseen_opposites(
                        parent,
                        unit_key,
                        opposite,
                        written=[row for row in written if row.polarity == opposite],
                        skip=targets,
                        seen=partial(seen, slot=slot),
                        rebased=base_claim is not None,
                        base_changes=base_changes,
                    )
                }
            )
            if not blind:
                continue
            issues.append(
                PolarityIssue(
                    POLARITY_CONFLICT,
                    cause[entering[0].claim_identity],
                    f"{where} would hold both a value and none for {shown_unit}, and "
                    f"{_named(blind)} was not live there when this change was made. A "
                    "one-valued field is disputed only by contending with the value its "
                    "writer read: write the change again at the head, revising the opposite "
                    "Claim (a flip) or contending with it.",
                )
            )
            continue
        issues.append(
            PolarityIssue(
                POLARITY_CONFLICT,
                cause[written[0].claim_identity],
                f"{where} would hold both a value ({_named(values)}) and none "
                f"({_named(nones)}) for {shown_unit}. A unit holds values or one none, never "
                "both: revise the opposite Claim into this one (a flip), or retire it in the "
                "same change.",
            )
        )
    # A flip under an object_key inverse stays in its unit: the key value it
    # names is read from state, which the Claim law does not hold.
    by_identity = {row.claim_identity: row for row in rows}
    flipped_units: set[UnitKey] = set()
    for identity, facts in sorted(claims.items()):
        before = parent_facts.get(identity)
        if before is None or facts.get("lifecycle") != "live":
            continue
        if _polarity_of(before) == _polarity_of(facts):
            continue
        new = by_identity.get(identity)
        if new is not None:
            flipped_units.add(new.unit_key)
        inverse = _inverse_key(candidate_type(str(facts["claim_type_identity"])))
        if inverse is None or inverse[0] != "object_key":
            continue
        old = unit_row({**before, "lifecycle": "live"}, inverse, parent_key_value_of)
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
    # The values a flip leaves behind retire with it, as superseded: the unit's
    # answer changed, which says nothing about whether they were wrong. Read
    # only when the change flips a unit: one keyed row per Claim it retires.
    if flipped_units:
        for identity, facts in sorted(claims.items()):
            if facts.get("lifecycle") == "live" or identity not in parent_facts:
                continue
            held = parent.execute(_UNIT_OF_SQL, (identity,)).fetchone()
            if held is None or tuple(held) not in flipped_units:
                continue
            reason = facts.get("retirement_reason")
            if reason == "superseded":
                continue
            issues.append(
                PolarityIssue(
                    FLIP_RETIREMENT_REASON,
                    str(facts["path"]),
                    f"{_claim_ref(identity)} retires beside a flip of "
                    f"{render_unit(str(held[3]), str(held[4]))}, so it ends as superseded, "
                    f"not {'unattributed' if reason is None else repr(reason)}: retire it "
                    "with reason superseded, or in a change of its own.",
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
    retirement = getattr(claim, "retirement", None)
    return {
        **_claim_fields(claim),
        "identity": claim.identity.qualified,
        "path": path,
        "lifecycle": claim.lifecycle.state,
        "retirement_reason": None if retirement is None else retirement.reason,
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
    base_tree: Mapping[str, bytes] | None = None,
    base_changes: frozenset[str] | None = None,
    contending: frozenset[str] = frozenset(),
) -> tuple[PolarityIssue, ...]:
    """The polarity law of one change set, judged at the candidate boundary.

    Admission and settlement both evaluate here, at the head they land on, so
    two changes that each pass alone cannot both land when together they would
    leave a unit holding both polarities: the second is judged against the
    first's accepted state. ``base_tree`` is the head the change was made
    against when that is not ``current_tree`` (a rebase), and ``base_changes``
    the paths that differ between them, when known. ``contending`` names the
    Claims (``Claim:<id>``) the change's write declared a contend for: a
    one-valued dispute needs that declaration, and stands only against the
    opposite Claims live in the base, the ones its writer saw.
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

    base_claim: Callable[[str], Facts | None] | None = None
    if base_tree is not None:
        from cruxible_client.contracts.claims import claim_path

        def base_claim(identity: str) -> Facts | None:
            path = claim_path(identity.removeprefix("Claim:"))
            content = base_tree.get(path)
            return None if content is None else _claim_facts(content, path)

    with _parent_index(
        current_tree, [item for item in claim_types.values() if item is not None]
    ) as parent:
        return polarity_issues(
            parent,
            claims=claims,
            claim_types=claim_types,
            claim_type_of=claim_type_of,
            base_claim=base_claim,
            contending=contending,
            base_changes=base_changes if base_tree is not None else None,
        )


__all__ = [
    "FLIP_RETIREMENT_REASON",
    "FLIP_UNIT_MOVED",
    "INVERSE_DUPLICATE",
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
