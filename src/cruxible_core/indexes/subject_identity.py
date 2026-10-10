"""The authoritative Subject identity checks at candidate evaluation (D1, D4, D6).

Identity is decided once, by declared rules, at one boundary: candidate
evaluation, which admission and settlement both run against the parent state, so
every creation route is held to it -- authoring, write verbs, Procedure terminals
(proposal and settle), kit changes and raw proposals alike. History accepted
before a rule is not judged again on recovery replay.

A Subject is created when it is live in the candidate and absent from the
parent. Every created Subject must be of a kind an accepted SubjectKind declares,
live in the candidate (``cruxible.subject.kind_undeclared``). Subjects that
already exist are grandfathered whatever their kind: readable, claimable and
retirable, never re-created.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cruxible_client.contracts.canonical_forms import (
    CanonicalFormDidNotConverge,
    canonicalize_literal,
)
from cruxible_client.contracts.claim_types import ClaimType, effective_canonical_form
from cruxible_client.contracts.subject_kinds import (
    SubjectKeyValue,
    SubjectKind,
    SubjectKindFormatError,
    key_value_problem,
    live_subject_kind,
    ordered_key_parts,
)

if TYPE_CHECKING:
    from cruxible_client.contracts.types import CompilerCoordinate

_SUBJECTS = "subjects/"

SUBJECT_KIND_UNDECLARED = "cruxible.subject.kind_undeclared"


@dataclass(frozen=True)
class SubjectIdentityIssue:
    """One reason a change's Subject identity does not hold, for one Subject."""

    code: str
    #: The Subject's ledger path.
    path: str
    message: str
    #: The live Subjects the issue names (``kind/id``), when it names any.
    existing: tuple[str, ...] = ()


def admits_subject_kinds(compiler: CompilerCoordinate) -> bool:
    """Whether a compiler admits SubjectKinds, and so holds creation to them.

    An instance on an earlier compiler cannot declare a kind; it keeps creating
    Subjects as it did until it upgrades.
    """

    from cruxible_core.compiler.compiler import artifact_kinds_for_compiler

    return any(
        entry.kind == "subject-kind" for entry in artifact_kinds_for_compiler(compiler).entries()
    )


def subject_kind_of(path: str) -> tuple[str, str]:
    """``(kind, id)`` of a Subject's ledger path."""

    kind, _, rest = path.removeprefix(_SUBJECTS).partition("/")
    return kind, rest.removesuffix(".json")


KeyTuple = tuple[tuple[str, SubjectKeyValue], ...]
"""A canonical key: each key predicate and its canonical value, in the kind's key order."""


class SubjectKeyInvalid(ValueError):
    """A key that cannot name a Subject of its kind; the message says why."""


def canonical_key(
    subject_kind: SubjectKind,
    key: Mapping[str, object],
    *,
    claim_type: Callable[[str], ClaimType | None],
) -> KeyTuple:
    """The canonical key a creation names: each part in key order, in its canonical form.

    ``key`` maps key ClaimTypes (by predicate or last name) to raw values;
    ``claim_type`` reads a key ClaimType as the change sees it. Raises
    ``SubjectKeyInvalid`` naming what is missing, unknown or unsettled.
    """

    try:
        parts = ordered_key_parts(subject_kind, key)  # type: ignore[arg-type]
    except ValueError as exc:
        raise SubjectKeyInvalid(str(exc)) from exc
    found: list[tuple[str, SubjectKeyValue]] = []
    for part in parts:
        definition = claim_type(part.predicate)
        if definition is None:
            raise SubjectKeyInvalid(f"key ClaimType {part.predicate!r} is not accepted")
        try:
            value = canonicalize_literal(part.value, effective_canonical_form(definition))
        except CanonicalFormDidNotConverge as exc:
            raise SubjectKeyInvalid(str(exc)) from exc
        problem = key_value_problem(value)
        if problem is not None:
            raise SubjectKeyInvalid(f"{part.predicate}: {problem}")
        found.append((part.predicate, value))  # type: ignore[arg-type]
    return tuple(found)


def key_values(key: KeyTuple) -> tuple[SubjectKeyValue, ...]:
    return tuple(value for _predicate, value in key)


def key_order(key: KeyTuple) -> bytes:
    """The order core assigns ids in when one change creates several Subjects of a kind."""

    from cruxible_client.contracts.canonical import canonical_bytes

    return canonical_bytes(list(key_values(key)))


def _value_clause(value: SubjectKeyValue) -> tuple[str, tuple[object, ...]]:
    # Every column of the key index up to the value is constrained, so each form
    # is one seek on ``claims_by_key_value``.
    if isinstance(value, bool):
        return (
            "literal_type='boolean' AND literal_text IS NULL AND literal_integer_text IS NULL "
            "AND literal_boolean=?",
            (int(value),),
        )
    if isinstance(value, int):
        return (
            "literal_type='integer' AND literal_text IS NULL AND literal_integer_text=?",
            (str(value),),
        )
    return "literal_type='string' AND literal_text=?", (value,)


_KEY_CLAIM = (
    "lifecycle='live' AND object_kind='literal' AND qualifier IS NULL "
    "AND subject_selector_scheme='artifact-v1'"
)


def _kind_range(kind: str) -> tuple[str, str]:
    prefix = f"{_SUBJECTS}{kind}/"
    return prefix, prefix[:-1] + chr(ord("/") + 1)


def subjects_with_key(connection: sqlite3.Connection, kind: str, key: KeyTuple) -> tuple[str, ...]:
    """The live Subjects of ``kind`` whose live key Claims hold exactly ``key``, by path.

    One index seek on the first part (``claims_by_key_value``), then one probe per
    remaining part of each Subject it found: the key index of D4 is the live key
    Claims themselves, maintained at accept like every other Claim row.
    """

    if not key:
        return ()
    (first_predicate, first_value), rest = key[0], key[1:]
    low, high = _kind_range(kind)
    clause, values = _value_clause(first_value)
    found = [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT subject_path FROM claims "
            f"WHERE predicate=? AND {clause} AND {_KEY_CLAIM} "
            "AND subject_path>=? AND subject_path<? ORDER BY subject_path",
            (first_predicate, *values, low, high),
        )
    ]
    found = [
        path
        for path in found
        if connection.execute(
            "SELECT 1 FROM subjects WHERE path=? AND lifecycle='live'", (path,)
        ).fetchone()
        is not None
    ]
    matched: list[str] = []
    for path in found:
        if all(
            connection.execute(
                f"SELECT 1 FROM claims WHERE subject_path=? AND predicate=? AND "
                f"{_value_clause(value)[0]} AND {_KEY_CLAIM}",
                (path, predicate, *_value_clause(value)[1]),
            ).fetchone()
            is not None
            for predicate, value in rest
        ):
            matched.append(path)
    return tuple(matched)


def live_key_claims(
    connection: sqlite3.Connection, subject_path: str, predicate: str
) -> tuple[tuple[str, SubjectKeyValue | None], ...]:
    """Each live key Claim of one Subject and predicate: ``(Claim identity, value)``."""

    rows = connection.execute(
        "SELECT identity, literal_type, literal_text, literal_integer_text, literal_boolean "
        f"FROM claims WHERE subject_path=? AND predicate=? AND {_KEY_CLAIM} ORDER BY identity",
        (subject_path, predicate),
    ).fetchall()
    found: list[tuple[str, SubjectKeyValue | None]] = []
    for identity, literal_type, text, integer, boolean in rows:
        value: SubjectKeyValue | None
        if literal_type == "string":
            value = str(text)
        elif literal_type == "integer":
            value = int(integer)
        elif literal_type == "boolean":
            value = bool(boolean)
        else:
            value = None
        found.append((str(identity), value))
    return tuple(found)


def single_key(tree: Mapping[str, bytes], kind: str) -> tuple[str, ClaimType] | None:
    """The one key of ``kind`` when its SubjectKind in ``tree`` names a single key.

    Such a kind's ``kind/<text>`` reads as "the Subject whose key is <text>",
    whether the kind is ``unique`` or ``hint``.
    """

    from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type

    try:
        declared = live_subject_kind(tree, kind)
    except SubjectKindFormatError:
        return None
    if declared is None or len(declared.keys) != 1:
        return None
    predicate = declared.keys[0]
    content = tree.get(claim_type_path(predicate))
    if content is None:
        return None
    return predicate, parse_claim_type(content, path=claim_type_path(predicate))


def single_key_value(claim_type: ClaimType, text: str) -> SubjectKeyValue | None:
    """``text`` as the canonical value of a single key, or None when it cannot be one."""

    try:
        value = canonicalize_literal(text, effective_canonical_form(claim_type))
    except CanonicalFormDidNotConverge:
        return None
    return value if isinstance(value, str) and value else None


def single_key_holders(
    connection: sqlite3.Connection, tree: Mapping[str, bytes], kind: str, text: str
) -> tuple[str, ...]:
    """The live Subjects whose single key ``text`` spells, by path (none, one, or several)."""

    found = single_key(tree, kind)
    if found is None:
        return ()
    predicate, claim_type = found
    value = single_key_value(claim_type, text)
    if value is None:
        return ()
    return subjects_with_key(connection, kind, ((predicate, value),))


def key_ambiguous_message(kind: str, holders: Sequence[str]) -> str:
    return (
        f"{len(holders)} live {kind} Subjects hold this key: "
        + ", ".join(_ref(path) for path in holders)
        + "; name one by id."
    )


def subject_key_message(created: str, kind: str, key: KeyTuple, others: Sequence[str]) -> str:
    """The one refusal text for a Subject whose canonical key another live Subject holds."""

    spelled = ", ".join(f"{predicate}={value!r}" for predicate, value in key)
    return (
        f"{created} would hold the key {spelled}, which live {', '.join(others)} already holds; "
        f"{kind} keys are unique. Use {others[0]}; or, if they are the same thing, alias it and "
        "retire the other (semantic.alias + retire); if they are different things, give them "
        "different keys."
    )


def subject_creation_shape(kind: str, key_hint: str = "<value>") -> str:
    """The authoring input that creates a Subject of ``kind``, as JSON an author fills in."""

    return json.dumps(
        {"kind": "subject", "subject_kind": kind, "key": {"<key ClaimType>": key_hint}}
    )


def undeclared_kind_repair(kind: str) -> str:
    """The SubjectKind an author writes so ``kind`` can have new Subjects."""

    keyed = json.dumps(
        {
            "kind": "subject_kind",
            "name": kind,
            "keys": [f"{kind}.slug"],
            "uniqueness": "unique",
        }
    )
    minted = json.dumps({"kind": "subject_kind", "name": kind, "minted": True})
    return (
        f"Author a SubjectKind for {kind} first: keyed by a key ClaimType (a ClaimType v8 about "
        f"{kind} only, literal and one-valued, accepted in the same change or earlier), e.g. "
        f"{keyed}, or minted, {minted}. `cruxible authoring example subject-kind` prints one."
    )


def undeclared_kind_message(kind: str) -> str:
    return (
        f"No accepted SubjectKind declares {kind}, so no new Subject of it can be created; "
        f"existing {kind} Subjects stay readable, claimable and retirable. "
        + undeclared_kind_repair(kind)
    )


class _DeclaredKinds:
    """The live SubjectKind of each kind in one tree, read once per kind."""

    def __init__(self, tree: Mapping[str, bytes]) -> None:
        self._tree = tree
        self._known: dict[str, SubjectKind | None] = {}

    def __call__(self, kind: str) -> SubjectKind | None:
        if kind not in self._known:
            try:
                self._known[kind] = live_subject_kind(self._tree, kind)
            except SubjectKindFormatError:
                self._known[kind] = None
        return self._known[kind]


def created_subjects(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> tuple[str, ...]:
    """The Subject paths a change creates: in the candidate, absent from the parent."""

    return tuple(
        path
        for path in scope
        if path.startswith(_SUBJECTS)
        and path.endswith(".json")
        and path in candidate_tree
        and path not in current_tree
    )


SUBJECT_KEY_MISSING = "cruxible.subject.key_missing"
SUBJECT_KEY_CONTENDED = "cruxible.subject.key_contended"
SUBJECT_KEY_INVALID = "cruxible.subject.key_invalid"
SUBJECT_KEY_CONFLICT = "cruxible.subject.key_conflict"
SUBJECT_ID_NOT_ASSIGNED = "cruxible.subject.id_not_assigned"


def _ref(path: str) -> str:
    kind, subject_id = subject_kind_of(path)
    return f"{kind}/{subject_id}"


def _live(tree: Mapping[str, bytes], path: str) -> bool:
    from cruxible_client.contracts.subjects import parse_subject

    content = tree.get(path)
    if content is None:
        return False
    try:
        return parse_subject(content, path=path).lifecycle.state == "live"
    except ValueError:
        return False


@dataclass(frozen=True)
class _KeyClaimRow:
    """One key Claim version a change touches: whose key part it is, and its value."""

    subject_path: str
    predicate: str
    value: SubjectKeyValue | None
    live: bool


def _key_claim_row(
    content: bytes | None, path: str, key_kinds: Mapping[str, str]
) -> _KeyClaimRow | None:
    """The key part one Claim version states, when it states one."""

    from cruxible_client.contracts.claims import LiteralClaimObject, parse_claim

    if content is None:
        return None
    claim = parse_claim(content, path=path)
    statement = claim.statement
    subject = statement.subject
    kind = key_kinds.get(statement.predicate)
    if (
        kind is None
        or statement.qualifier is not None
        or subject.selector.scheme != "artifact-v1"
        or not subject.artifact_path.startswith(f"{_SUBJECTS}{kind}/")
    ):
        return None
    value = statement.object.value if isinstance(statement.object, LiteralClaimObject) else None
    return _KeyClaimRow(
        subject_path=subject.artifact_path,
        predicate=statement.predicate,
        value=None if key_value_problem(value) is not None else value,  # type: ignore[arg-type]
        live=claim.lifecycle.state == "live",
    )


def candidate_identity_issues(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> tuple[SubjectIdentityIssue, ...]:
    """Every Subject identity rule a change breaks, judged against its parent.

    The parent is read through its key index (``subjects_with_key``) overlaid with
    the owners the change touches -- Subjects it creates or retires, key Claims it
    adds, revises or retires, SubjectKinds it declares or succeeds -- never a
    rebuild of the index:

    - every created Subject is of a declared kind (``kind_undeclared``);
    - a created Subject of a keyed kind states each key part as one live Claim
      (``key_missing``, ``key_contended``, ``key_invalid``) and has exactly the id
      core assigns its canonical key (``id_not_assigned``);
    - under a ``unique`` kind no two live Subjects hold the same canonical key,
      whether a Subject is created, a key Claim revised, or the kind newly made
      unique (``key_conflict``, naming the Subject that holds it).
    """

    from cruxible_client.contracts.subject_kinds import (
        assign_subject_id,
        is_distinct_holder_id,
    )
    from cruxible_client.contracts.subjects import subject_path
    from cruxible_core.indexes.subject_aliases import tree_index_connection

    declared = _DeclaredKinds(candidate_tree)
    issues: list[SubjectIdentityIssue] = []
    created = created_subjects(
        current_tree=current_tree, candidate_tree=candidate_tree, scope=scope
    )
    affected: dict[str, str] = {}
    for path in created:
        kind, _subject_id = subject_kind_of(path)
        declared_kind = declared(kind)
        if declared_kind is None:
            issues.append(
                SubjectIdentityIssue(SUBJECT_KIND_UNDECLARED, path, undeclared_kind_message(kind))
            )
        elif declared_kind.keys:
            affected[path] = kind
    changed_kinds = {
        path.removeprefix("subject-kinds/").removesuffix(".json")
        for path in scope
        if path.startswith("subject-kinds/")
    }
    scope_claims = tuple(path for path in scope if path.startswith("claims/"))
    if not affected and not changed_kinds and not scope_claims:
        return tuple(issues)

    with tree_index_connection(current_tree)() as connection:
        # Every live keyed kind's key predicates, the parent's overlaid with this change.
        key_kinds: dict[str, str] = {}
        for identity, keys in connection.execute(
            "SELECT identity, key_predicates FROM subject_kinds "
            "WHERE rule='keys' AND lifecycle='live'"
        ):
            kind = str(identity).removeprefix("SubjectKind:")
            if kind not in changed_kinds:
                key_kinds.update((predicate, kind) for predicate in json.loads(str(keys)))
        for kind in changed_kinds:
            declared_kind = declared(kind)
            if declared_kind is not None:
                key_kinds.update((predicate, kind) for predicate in declared_kind.keys)
        needles = tuple(f'"{predicate}"'.encode() for predicate in sorted(key_kinds))
        # The key Claim versions the change touches, and the Subjects whose key they move.
        touched: dict[str, tuple[_KeyClaimRow | None, _KeyClaimRow | None]] = {}
        for path in scope_claims if needles else ():
            before_bytes, after_bytes = current_tree.get(path), candidate_tree.get(path)
            if not any(
                needle in content
                for content in (before_bytes, after_bytes)
                if content is not None
                for needle in needles
            ):
                continue
            rows = (
                _key_claim_row(before_bytes, path, key_kinds),
                _key_claim_row(after_bytes, path, key_kinds),
            )
            if rows == (None, None):
                continue
            touched[path] = rows
            for row in rows:
                if row is not None:
                    affected.setdefault(row.subject_path, subject_kind_of(row.subject_path)[0])
        touched_identities = {
            f"Claim:{path.rsplit('/', 1)[-1].removesuffix('.json')}" for path in touched
        }

        def values(subject: str, predicate: str) -> list[SubjectKeyValue | None]:
            found = [
                value
                for identity, value in live_key_claims(connection, subject, predicate)
                if identity not in touched_identities
            ]
            found.extend(
                after.value
                for _before, after in touched.values()
                if after is not None
                and after.live
                and after.subject_path == subject
                and after.predicate == predicate
            )
            return found

        keys_now: dict[str, KeyTuple] = {}
        for subject in sorted(affected):
            kind = affected[subject]
            declared_kind = declared(kind)
            if (
                declared_kind is None
                or not declared_kind.keys
                or not _live(candidate_tree, subject)
            ):
                continue
            parts: list[tuple[str, SubjectKeyValue]] = []
            for predicate in declared_kind.keys:
                found = values(subject, predicate)
                if len(found) > 1:
                    issues.append(
                        SubjectIdentityIssue(
                            SUBJECT_KEY_CONTENDED,
                            subject,
                            f"{_ref(subject)} holds {len(found)} live values of its key "
                            f"{predicate}; a key part holds exactly one value.",
                        )
                    )
                    break
                if not found:
                    if subject in created:
                        issues.append(
                            SubjectIdentityIssue(
                                SUBJECT_KEY_MISSING,
                                subject,
                                f"{_ref(subject)} is a new {kind}, which is named by its key "
                                f"({', '.join(declared_kind.keys)}), but the change states no "
                                f"live {predicate} Claim about it. Create it with a subject "
                                "member naming its kind and key; core writes the key Claims.",
                            )
                        )
                    break
                if found[0] is None:
                    issues.append(
                        SubjectIdentityIssue(
                            SUBJECT_KEY_INVALID,
                            subject,
                            f"{_ref(subject)}'s key part {predicate} is not a string, an "
                            "integer or a boolean.",
                        )
                    )
                    break
                parts.append((predicate, found[0]))
            else:
                keys_now[subject] = tuple(parts)

        # A unique kind's key names one live Subject.
        def holders(kind: str, key: KeyTuple) -> set[str]:
            found = {
                path
                for path in subjects_with_key(connection, kind, key)
                if path not in affected and (path not in scope or _live(candidate_tree, path))
            }
            found.update(
                other
                for other, other_key in keys_now.items()
                if affected.get(other) == kind and other_key == key
            )
            return found

        # Created keyed Subjects take exactly the id core assigns, in canonical-key
        # order: the key's rendering (suffixed when another key's Subject has it),
        # or -- a further holder of a hint kind's key -- that rendering and an
        # 8-hex digest of the operation that created it.
        assigned: dict[str, set[str]] = {}
        seen: dict[tuple[str, bytes], bool] = {}
        for subject in sorted(
            (item for item in created if item in keys_now),
            key=lambda item: (subject_kind_of(item)[0], key_order(keys_now[item]), item),
        ):
            kind, subject_id = subject_kind_of(subject)
            key = keys_now[subject]
            names = assigned.setdefault(kind, set())
            marker = (kind, key_order(key))
            if marker not in seen:
                seen[marker] = any(other not in created for other in holders(kind, key) - {subject})
            if seen[marker]:
                # Another Subject holds the key: a unique kind is refused below; a
                # hint kind's further holder has the distinct-holder form.
                names.add(subject_id)
                if not is_distinct_holder_id(subject_id, key_values(key)):
                    issues.append(
                        SubjectIdentityIssue(
                            SUBJECT_ID_NOT_ASSIGNED,
                            subject,
                            f"{_ref(subject)} is a further {kind} holding a key another live "
                            f"Subject holds; core names it {kind}/<rendering>-<8 hex>. Create "
                            "it with a subject member (create_new) and let core name it.",
                        )
                    )
                continue
            seen[marker] = True

            def taken(candidate: str, names: set[str] = names, kind: str = kind) -> bool:
                return candidate in names or subject_path(kind, candidate) in current_tree

            try:
                expected = assign_subject_id(key_values(key), taken=taken)
            except ValueError:
                expected = ""
            names.add(expected)
            if subject_id != expected:
                issues.append(
                    SubjectIdentityIssue(
                        SUBJECT_ID_NOT_ASSIGNED,
                        subject,
                        f"{_ref(subject)} is a new {kind}, whose id core assigns from its key: "
                        f"it is {kind}/{expected}. Create it with a subject member naming its "
                        "kind and key and let core name it.",
                        (f"{kind}/{expected}",),
                    )
                )

        for subject in sorted(keys_now):
            kind = affected[subject]
            declared_kind = declared(kind)
            if declared_kind is None or not declared_kind.unique:
                continue
            others = tuple(
                sorted(_ref(other) for other in holders(kind, keys_now[subject]) - {subject})
            )
            if others:
                issues.append(
                    SubjectIdentityIssue(
                        SUBJECT_KEY_CONFLICT,
                        subject,
                        subject_key_message(_ref(subject), kind, keys_now[subject], others),
                        others,
                    )
                )
        # A kind newly unique, or re-keyed while unique, holds every key once.
        for kind in sorted(changed_kinds):
            declared_kind = declared(kind)
            previous = live_subject_kind(current_tree, kind)
            if (
                declared_kind is None
                or not declared_kind.unique
                or (
                    previous is not None and previous.unique and previous.keys == declared_kind.keys
                )
            ):
                continue
            issues.extend(_kind_wide_conflicts(connection, kind, declared_kind, keys_now, affected))
    return tuple(issues)


def _kind_wide_conflicts(
    connection: sqlite3.Connection,
    kind: str,
    declared_kind: SubjectKind,
    keys_now: Mapping[str, KeyTuple],
    affected: Mapping[str, str],
) -> list[SubjectIdentityIssue]:
    """Every pair of live Subjects of ``kind`` that hold one key, once the kind is unique.

    One pass over the kind's live first-key Claims (a governance event's cost: it
    runs only when a SubjectKind becomes unique or changes its keys), overlaid
    with the Subjects the change itself moves.
    """

    low, high = _kind_range(kind)
    first = declared_kind.keys[0]
    tuples: dict[str, KeyTuple] = {}
    for (subject,) in connection.execute(
        f"SELECT DISTINCT subject_path FROM claims WHERE predicate=? AND {_KEY_CLAIM} "
        "AND subject_path>=? AND subject_path<?",
        (first, low, high),
    ):
        path = str(subject)
        if path in affected:
            continue
        live = connection.execute(
            "SELECT 1 FROM subjects WHERE path=? AND lifecycle='live'", (path,)
        ).fetchone()
        if live is None:
            continue
        parts: list[tuple[str, SubjectKeyValue]] = []
        for predicate in declared_kind.keys:
            found = [value for _identity, value in live_key_claims(connection, path, predicate)]
            if len(found) != 1 or found[0] is None:
                break
            parts.append((predicate, found[0]))
        else:
            tuples[path] = tuple(parts)
    tuples.update((path, key) for path, key in keys_now.items() if affected.get(path) == kind)
    groups: dict[str, list[str]] = {}
    for path, key in tuples.items():
        groups.setdefault(json.dumps(key_values(key), sort_keys=True), []).append(path)
    issues: list[SubjectIdentityIssue] = []
    for paths in groups.values():
        if len(paths) < 2:
            continue
        ordered = sorted(paths)
        for path in ordered[1:]:
            others = (_ref(ordered[0]),)
            issues.append(
                SubjectIdentityIssue(
                    SUBJECT_KEY_CONFLICT,
                    path,
                    subject_key_message(_ref(path), kind, tuples[path], others),
                    others,
                )
            )
    return issues


__all__ = [
    "SUBJECT_ID_NOT_ASSIGNED",
    "SUBJECT_KEY_CONFLICT",
    "SUBJECT_KEY_CONTENDED",
    "SUBJECT_KEY_INVALID",
    "SUBJECT_KEY_MISSING",
    "KeyTuple",
    "SubjectKeyInvalid",
    "canonical_key",
    "key_order",
    "key_values",
    "live_key_claims",
    "single_key",
    "key_ambiguous_message",
    "single_key_holders",
    "single_key_value",
    "subject_creation_shape",
    "subject_key_message",
    "subjects_with_key",
    "SUBJECT_KIND_UNDECLARED",
    "SubjectIdentityIssue",
    "admits_subject_kinds",
    "candidate_identity_issues",
    "created_subjects",
    "subject_kind_of",
    "undeclared_kind_message",
    "undeclared_kind_repair",
]
