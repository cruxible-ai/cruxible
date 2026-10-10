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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

from cruxible_client.contracts.canonical_forms import (
    CanonicalFormDidNotConverge,
    canonicalize_literal,
)
from cruxible_client.contracts.claim_types import ClaimType, effective_canonical_form
from cruxible_client.contracts.subject_kinds import (
    KeyClaimTypeShape,
    SubjectKeyValue,
    SubjectKind,
    SubjectKindFormatError,
    key_value_problem,
    live_subject_kind,
    ordered_key_parts,
)

if TYPE_CHECKING:
    from cruxible_client.contracts.types import CompilerCoordinate
    from cruxible_core.indexes.subject_aliases import SubjectRefResolution

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


SUBJECT_COMPILER_UPGRADE_REQUIRED = "cruxible.subject.compiler_upgrade_required"


def admits_subject_kinds(compiler: CompilerCoordinate) -> bool:
    """Whether a compiler admits SubjectKinds (revision 33, subject identity).

    An instance on an earlier compiler cannot declare a kind, so it creates no
    new Subject at all until it upgrades; its existing Subjects stay readable,
    claimable and retirable.
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


def key_encoding(key: KeyTuple) -> str:
    """A canonical key's one comparable spelling: typed, with its predicates, in key order.

    Canonical JSON keeps literal types apart (``true`` is not ``1``), so two keys
    are equal exactly when their encodings are. Every comparison of keys -- the
    index, the change overlay, holder grouping -- compares these, never Python
    values (where ``True == 1``).
    """

    from cruxible_client.contracts.canonical import canonical_bytes

    return canonical_bytes([[predicate, value] for predicate, value in key]).decode("utf-8")


def key_order(key: KeyTuple) -> bytes:
    """The order core assigns ids in when one change creates several Subjects of a kind."""

    from cruxible_client.contracts.canonical import canonical_bytes

    return canonical_bytes(list(key_values(key)))


def _value_clause(value: SubjectKeyValue) -> tuple[str, tuple[object, ...]]:
    # Every column up to the value is constrained, so each form is one seek.
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

    One seek on the whole-key index (``subject_keys``): a composite key costs what
    a single one does, however many Subjects share its first part.
    """

    if not key:
        return ()
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT subject_path FROM subject_keys WHERE kind=? AND key_encoding=? "
            "ORDER BY subject_path",
            (kind, key_encoding(key)),
        )
    )


def subject_key_of(
    connection: sqlite3.Connection, path: str, keys: Sequence[str]
) -> KeyTuple | None:
    """The whole canonical key one Subject holds in ``connection``, or None.

    None when a key part has no live value, several, or one that is not a
    string, an integer or a boolean.
    """

    parts: list[tuple[str, SubjectKeyValue]] = []
    for predicate in keys:
        found = [value for _identity, value in live_key_claims(connection, path, predicate)]
        if len(found) != 1 or found[0] is None:
            return None
        parts.append((predicate, found[0]))
    return tuple(parts)


def refresh_subject_keys(
    connection: sqlite3.Connection,
    *,
    subjects: Iterable[str] | None = None,
    kinds: Iterable[str] = (),
) -> None:
    """Bring ``subject_keys`` up to date with the subjects, claims and subject_kinds rows.

    ``subjects=None`` rebuilds the table (a fresh projection or tree index).
    Otherwise only the named Subjects, and every Subject of the named kinds (a
    SubjectKind the change declares, re-keys or retires), are recomputed: an
    accept costs the Subjects and key Claims it touched.
    """

    keyed = {
        str(identity).removeprefix("SubjectKind:"): tuple(json.loads(str(keys)))
        for identity, keys in connection.execute(
            "SELECT identity, key_predicates FROM subject_kinds "
            "WHERE rule='keys' AND lifecycle='live'"
        )
    }
    targets: set[str] = set()
    if subjects is None:
        connection.execute("DELETE FROM subject_keys")
        for kind in keyed:
            low, high = _kind_range(kind)
            targets.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT path FROM subjects WHERE path>=? AND path<?", (low, high)
                )
            )
    else:
        targets.update(path for path in subjects if path.startswith(_SUBJECTS))
        for kind in set(kinds):
            low, high = _kind_range(kind)
            targets.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT path FROM subjects WHERE path>=? AND path<?", (low, high)
                )
            )
            targets.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT subject_path FROM subject_keys WHERE kind=?", (kind,)
                )
            )
        connection.executemany(
            "DELETE FROM subject_keys WHERE subject_path=?", ((path,) for path in targets)
        )
    rows: list[tuple[str, str, str]] = []
    for path in sorted(targets):
        kind = subject_kind_of(path)[0]
        keys = keyed.get(kind)
        if not keys:
            continue
        if (
            connection.execute(
                "SELECT 1 FROM subjects WHERE path=? AND lifecycle='live'", (path,)
            ).fetchone()
            is None
        ):
            continue
        key = subject_key_of(connection, path, keys)
        if key is not None:
            rows.append((path, kind, key_encoding(key)))
    connection.executemany("INSERT INTO subject_keys VALUES (?,?,?)", rows)


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


KeyReader = Callable[[str, str], "KeyTuple | None"]
"""Read ``kind/<text>`` as a canonical single key of ``kind``: None when it cannot be one."""


def tree_key_reader(tree: Mapping[str, bytes]) -> KeyReader:
    """The single-key reading of ``kind/<text>`` under the SubjectKinds of ``tree``."""

    known: dict[str, tuple[str, ClaimType] | None] = {}

    def read(kind: str, text: str) -> KeyTuple | None:
        if kind not in known:
            known[kind] = single_key(tree, kind)
        found = known[kind]
        if found is None:
            return None
        predicate, claim_type = found
        value = single_key_value(claim_type, text)
        return None if value is None else ((predicate, value),)

    return read


@dataclass
class IdentityOverlay:
    """What one change does to Subject identity, laid over its parent's index.

    Built from the change once -- by the candidate boundary from the candidate
    tree, by lowering and the write verbs from the members they are lowering --
    and read by the one resolver (``IdentityView``), so a creation resolves
    against the state the change itself leaves: Subjects it retires hold
    nothing, keys it frees or takes are freed or taken, aliases it adds or
    retires count or do not.
    """

    #: Subject path -> live after the change, for every Subject the change writes.
    subjects: dict[str, bool] = field(default_factory=dict)
    #: Subject path -> its whole canonical key after the change (None: none), for
    #: every Subject whose key the change may move.
    keys: dict[str, KeyTuple | None] = field(default_factory=dict)
    #: Alias rows the change ends and adds: (target path, discovery-normalized term).
    aliases_removed: set[tuple[str, str]] = field(default_factory=set)
    aliases_added: set[tuple[str, str]] = field(default_factory=set)


class IdentityView:
    """Subject identity at one parent, overlaid with one change: the shared resolver.

    Every creation and reference route resolves through here (identity
    D1/D4/D6, review F-003): a reference ``kind/<text>`` names the exact live
    Subject, else the one live Subject an alias of the kind names, else the one
    live Subject a single key of the kind names (``text`` read in its key
    ClaimType's canonical form), else the exact retired Subject. Aliases come
    before keys because an alias is a name an author asserted for exactly that
    Subject (a merge leaves one), while ``kind/<text>`` read as a key is a
    convenience reading of text; and E-a's alias resolution keeps every meaning
    it had. Several matches at the alias or key step are ambiguous.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        overlay: IdentityOverlay | None = None,
        *,
        key_reader: KeyReader | None = None,
    ) -> None:
        self.connection = connection
        self.overlay = overlay or IdentityOverlay()
        self.key_reader = key_reader
        self._overlay_holders: dict[tuple[str, str], set[str]] | None = None

    # -- the parent, overlaid ------------------------------------------------

    def exists(self, path: str) -> bool:
        if path in self.overlay.subjects:
            return True
        return (
            self.connection.execute("SELECT 1 FROM subjects WHERE path=?", (path,)).fetchone()
            is not None
        )

    def live(self, path: str) -> bool:
        if path in self.overlay.subjects:
            return self.overlay.subjects[path]
        return (
            self.connection.execute(
                "SELECT 1 FROM subjects WHERE path=? AND lifecycle='live'", (path,)
            ).fetchone()
            is not None
        )

    def alias_targets(self, kind: str, name: str) -> tuple[str, ...]:
        """The live Subjects of ``kind`` an alias spelled ``name`` names, by path."""

        from cruxible_client.contracts.discovery import normalize_discovery_term

        term = normalize_discovery_term(name)
        low, high = _kind_range(kind)
        found = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT target_path, value FROM vocabulary_terms "
                "WHERE term=? AND basis='alias' AND target_path>=? AND target_path<?",
                (term, low, high),
            )
            if (str(row[0]), term) not in self.overlay.aliases_removed
        }
        found.update(
            target
            for target, added in self.overlay.aliases_added
            if added == term and low <= target < high
        )
        return tuple(sorted(path for path in found if self.live(path)))

    def holders(self, kind: str, key: KeyTuple) -> tuple[str, ...]:
        """The live Subjects of ``kind`` holding exactly the canonical ``key``.

        One seek on the parent's whole-key index, less the Subjects the change
        moves, plus those the change leaves holding it (one dict, by encoding).
        """

        if self._overlay_holders is None:
            self._overlay_holders = {}
            for path, held in self.overlay.keys.items():
                if held is not None:
                    self._overlay_holders.setdefault(
                        (subject_kind_of(path)[0], key_encoding(held)), set()
                    ).add(path)
        found = {
            path
            for path in subjects_with_key(self.connection, kind, key)
            if path not in self.overlay.keys
        }
        found.update(self._overlay_holders.get((kind, key_encoding(key)), ()))
        return tuple(sorted(path for path in found if self.live(path)))

    def add_creation(self, path: str, key: KeyTuple | None) -> None:
        """Lay one Subject the change creates over the view (it holds ``key``)."""

        self.overlay.subjects[path] = True
        self.overlay.keys[path] = key
        self._overlay_holders = None

    def taken(self, kind: str, subject_id: str) -> bool:
        """Whether a new ``kind/<id>`` would reuse a name: a Subject's id, or a live alias."""

        path = f"{_SUBJECTS}{kind}/{subject_id}.json"
        return self.exists(path) or bool(self.alias_targets(kind, subject_id))

    # -- resolution ----------------------------------------------------------

    def resolve(self, kind: str, text: str) -> SubjectRefResolution:
        """``kind/<text>``: exact live id, else alias, else single key, else exact retired."""

        from cruxible_core.indexes.subject_aliases import SubjectRefResolution

        exact = f"{_SUBJECTS}{kind}/{text}.json"
        if self.live(exact):
            return SubjectRefResolution(kind=kind, subject_id=text, path=exact, via="identity")
        aliased = self.alias_targets(kind, text)
        if len(aliased) == 1:
            return SubjectRefResolution(kind=kind, subject_id=text, path=aliased[0], via="alias")
        if len(aliased) > 1:
            return SubjectRefResolution(
                kind=kind,
                subject_id=text,
                path=None,
                via="alias",
                candidates=tuple(_ref(path) for path in aliased),
            )
        key = None if self.key_reader is None else self.key_reader(kind, text)
        if key is not None:
            held = self.holders(kind, key)
            if len(held) == 1:
                return SubjectRefResolution(kind=kind, subject_id=text, path=held[0], via="key")
            if len(held) > 1:
                return SubjectRefResolution(
                    kind=kind,
                    subject_id=text,
                    path=None,
                    via="key",
                    candidates=tuple(_ref(path) for path in held),
                )
        if self.exists(exact):
            # A retired Subject no alias or key supersedes is still the one it names.
            return SubjectRefResolution(kind=kind, subject_id=text, path=exact, via="identity")
        return SubjectRefResolution(kind=kind, subject_id=text, path=None, via=None)

    def resolve_creation(
        self, kind: str, key: KeyTuple, *, single_text: str | None, create_new: bool
    ) -> CreationResolution:
        """Where a creation by ``key`` lands: an existing Subject, several, or a new one.

        A one-key creation's text is first read as an alias of the kind (an alias
        is a name asserted for exactly one Subject); then the canonical key's
        live holders: none creates, one is reused, several refuse. ``create_new``
        (a ``hint`` kind's deliberate further holder) creates whatever holds the
        key. The exact-id and retired steps of ``resolve`` do not apply: a Subject
        whose id merely equals the key's rendering but holds another key (or none)
        is another thing, and a retired one's id stays reserved -- the new Subject
        takes a free id.
        """

        if create_new:
            return CreationResolution(path=None, holders=self.holders(kind, key))
        if single_text is not None:
            aliased = self.alias_targets(kind, single_text)
            if aliased:
                return CreationResolution(
                    path=aliased[0] if len(aliased) == 1 else None, holders=aliased, via="alias"
                )
        held = self.holders(kind, key)
        return CreationResolution(
            path=held[0] if len(held) == 1 else None, holders=held, via="key" if held else None
        )


@dataclass(frozen=True)
class CreationResolution:
    """What a creation by key resolved to: one existing Subject, several, or none."""

    path: str | None
    holders: tuple[str, ...] = ()
    via: str | None = None

    @property
    def ambiguous(self) -> bool:
        return self.path is None and len(self.holders) > 1 and self.via is not None

    @property
    def creates(self) -> bool:
        return self.path is None and not self.ambiguous


def assign_creation_id(
    view: IdentityView,
    kind: str,
    key: KeyTuple,
    *,
    names: set[str],
) -> str:
    """The id core assigns a first holder of ``key``: its rendering, disambiguated.

    The rendering is taken when it is an existing Subject's id (live or
    retired), a live alias of the kind (an alias is never shadowed by a new id),
    or an id this change already assigned (``names``); then the key digest
    suffix, and so on. Raises ``SubjectIdUnassignable``.
    """

    from cruxible_client.contracts.subject_kinds import assign_subject_id

    return assign_subject_id(
        key_values(key),
        taken=lambda candidate: candidate in names or view.taken(kind, candidate),
    )


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


def compiler_upgrade_repair(kind: str) -> str:
    """What an instance on a compiler before SubjectKinds does to create a ``kind``."""

    return (
        "Upgrade the instance's compiler (`cruxible compiler upgrade`; `cruxible next` names "
        f"the exact command), then declare {kind} with a SubjectKind and create by its key. "
        + undeclared_kind_repair(kind)
    )


def compiler_upgrade_message(kind: str) -> str:
    return (
        f"This instance's compiler predates SubjectKinds, so no new Subject can be created; "
        f"existing {kind} Subjects stay readable, claimable and retirable. "
        + compiler_upgrade_repair(kind)
    )


def boundary_identity_issues(
    compiler: CompilerCoordinate,
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
    assignment_authority: SubjectAssignmentAuthority | None = None,
) -> tuple[SubjectIdentityIssue, ...]:
    """The identity issues of a change on its instance's compiler (live admission only).

    Under a compiler that admits SubjectKinds, every rule of
    ``candidate_identity_issues``; under an earlier one, every new Subject
    refuses (``compiler_upgrade_required``). Recovery replay of accepted history
    does not come here.
    """

    if admits_subject_kinds(compiler):
        return candidate_identity_issues(
            current_tree=current_tree,
            candidate_tree=candidate_tree,
            scope=scope,
            assignment_authority=assignment_authority,
        )
    return tuple(
        SubjectIdentityIssue(
            SUBJECT_COMPILER_UPGRADE_REQUIRED,
            path,
            compiler_upgrade_message(subject_kind_of(path)[0]),
        )
        for path in created_subjects(
            current_tree=current_tree, candidate_tree=candidate_tree, scope=scope
        )
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
SUBJECT_ASSIGNMENT_CHANGED = "cruxible.subject.assignment_changed"


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


def _shell(tree: Mapping[str, bytes], path: str) -> Any | None:
    from cruxible_client.contracts.subjects import parse_subject

    content = tree.get(path)
    if content is None:
        return None
    try:
        return parse_subject(content, path=path)
    except ValueError:
        return None


def _alias_rows(content: bytes | None, path: str) -> tuple[tuple[str, str], ...]:
    """The live alias rows one Claim version contributes: (target path, term)."""

    from cruxible_client.contracts.claims import LiteralClaimObject, parse_claim
    from cruxible_client.contracts.discovery import normalize_discovery_term

    if content is None or b'"semantic.alias"' not in content:
        return ()
    claim = parse_claim(content, path=path)
    statement = claim.statement
    if (
        claim.lifecycle.state != "live"
        or statement.predicate != "semantic.alias"
        or not isinstance(statement.object, LiteralClaimObject)
        or not isinstance(statement.object.value, str)
    ):
        return ()
    return ((statement.subject.artifact_path, normalize_discovery_term(statement.object.value)),)


SubjectAssignmentAuthority: TypeAlias = Callable[[str, str, str], bool]
"""Whether an authoritative core record creates one member of one kind.

Called as ``(operation, member, kind)``: the operation is a durable core record
-- a stored authoring intent, or the record a Procedure terminal writes for its
operation -- found by its id (one lookup), whose content creates exactly the
member ``member`` of ``kind``. The candidate boundary asks it for every minted
or further-holder Subject a change creates, at admission and at settlement; the
record does not change when the proposal is rebased or readmitted, so neither
does the answer.
"""


def _authoritative(
    authority: SubjectAssignmentAuthority | None,
    assignment: Any,
    kind: str,
) -> bool:
    if authority is None or assignment is None:
        return False
    return bool(authority(assignment.operation, assignment.member, kind))


def _unrecorded_assignment(path: str, kind: str, assignment: Any) -> SubjectIdentityIssue:
    return SubjectIdentityIssue(
        SUBJECT_ID_NOT_ASSIGNED,
        path,
        f"{_ref(path)} records that operation {assignment.operation!r} (member "
        f"{assignment.member!r}) created it, and no core record of that operation creates "
        f"that member of {kind}: a {assignment.rule.replace('_', ' ')} Subject is named only "
        "by core, for an authoring intent or a Procedure operation it holds. Create it "
        "through authoring (a subject member naming its kind) and let core name it.",
    )


def candidate_identity_issues(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
    assignment_authority: SubjectAssignmentAuthority | None = None,
) -> tuple[SubjectIdentityIssue, ...]:
    """Every Subject identity rule a change breaks, judged against its parent.

    The parent is read through its indexes (the whole-key index, the alias
    terms) overlaid with the owners the change touches -- Subjects it creates or
    retires, key and alias Claims it adds, revises or retires, SubjectKinds it
    declares or succeeds -- never a rebuild:

    - every created Subject is of a declared kind (``kind_undeclared``);
    - every created Subject carries core's assignment binding and has exactly
      the id it derives (``id_not_assigned``): a minted id from its recorded
      operation and member; a first holder of a key, the key's rendering,
      suffixed when the rendering is a Subject's id, a live alias of the kind,
      or an id this change assigned first; a further holder of a ``hint`` key,
      the rendering and a digest of its recorded operation and member. A
      recorded operation and member must name an authoritative core record
      (``assignment_authority``: a stored authoring intent, or a Procedure
      operation record) that creates exactly that member of that kind; without
      one -- a raw proposal, an invented or copied binding -- only a keyed first
      holder can be created. A successor keeps its Subject's binding
      (``assignment_changed``);
    - a created Subject of a keyed kind states each key part as one live Claim
      (``key_missing``, ``key_contended``, ``key_invalid``);
    - under a ``unique`` kind no two live Subjects hold the same canonical key,
      whether a Subject is created, a key Claim revised, or the kind newly made
      unique (``key_conflict``, naming the Subject that holds it).
    """

    from cruxible_client.contracts.subject_kinds import (
        distinct_holder_id,
        minted_subject_id,
    )
    from cruxible_core.indexes.subject_aliases import tree_index_connection

    declared = _DeclaredKinds(candidate_tree)
    issues: list[SubjectIdentityIssue] = []
    created = created_subjects(
        current_tree=current_tree, candidate_tree=candidate_tree, scope=scope
    )
    created_set = set(created)
    affected: dict[str, str] = {}
    shells: dict[str, Any] = {}
    for path in created:
        kind, subject_id = subject_kind_of(path)
        declared_kind = declared(kind)
        shell = _shell(candidate_tree, path)
        shells[path] = shell
        if declared_kind is None:
            issues.append(
                SubjectIdentityIssue(SUBJECT_KIND_UNDECLARED, path, undeclared_kind_message(kind))
            )
            continue
        assignment = None if shell is None else shell.assignment
        if declared_kind.minted:
            expected: str | None = (
                None
                if assignment is None or assignment.rule != "minted"
                else minted_subject_id(operation=assignment.operation, member=assignment.member)
            )
            if expected == subject_id and not _authoritative(
                assignment_authority, assignment, kind
            ):
                issues.append(_unrecorded_assignment(path, kind, assignment))
            elif expected != subject_id:
                issues.append(
                    SubjectIdentityIssue(
                        SUBJECT_ID_NOT_ASSIGNED,
                        path,
                        f"{_ref(path)} is a new minted {kind}, whose id core mints from the "
                        "operation and member that create it (recorded as its assignment); "
                        "this one carries no such binding. Create it with a subject member "
                        "naming its kind and let core name it.",
                    )
                )
        elif declared_kind.keys:
            affected[path] = kind
    # A successor keeps the binding core gave its Subject.
    for path in scope:
        if (
            path.startswith(_SUBJECTS)
            and path not in created_set
            and path in current_tree
            and path in candidate_tree
        ):
            before, after = _shell(current_tree, path), _shell(candidate_tree, path)
            if before is not None and after is not None and before.assignment != after.assignment:
                issues.append(
                    SubjectIdentityIssue(
                        SUBJECT_ASSIGNMENT_CHANGED,
                        path,
                        f"{_ref(path)} changes how core named it; a Subject's assignment never "
                        "changes after creation.",
                    )
                )
    changed_kinds = {
        path.removeprefix("subject-kinds/").removesuffix(".json")
        for path in scope
        if path.startswith("subject-kinds/")
    }
    scope_claims = tuple(path for path in scope if path.startswith("claims/"))
    scope_types = any(path.startswith("claim-types/") for path in scope)
    if not affected and not changed_kinds and not scope_claims and not scope_types:
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
        aliases_removed: set[tuple[str, str]] = set()
        aliases_added: set[tuple[str, str]] = set()
        for path in scope_claims:
            before_bytes, after_bytes = current_tree.get(path), candidate_tree.get(path)
            aliases_removed.update(_alias_rows(before_bytes, path))
            aliases_added.update(_alias_rows(after_bytes, path))
            if not needles or not any(
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
                    if subject in created_set:
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

        issues.extend(
            _key_definition_issues(
                connection,
                current_tree=current_tree,
                candidate_tree=candidate_tree,
                scope=scope,
                declared=declared,
                changed_kinds=changed_kinds,
                values=values,
            )
        )

        # The parent as this change leaves it: Subjects it retires or revives, keys
        # it moves, aliases it adds or ends. ``holders`` reads keys through it;
        # ``taken`` reads names through it without the Subjects the change
        # creates, which take their ids in canonical-key order below.
        lifecycle = {
            path: _live(candidate_tree, path)
            for path in scope
            if path.startswith(_SUBJECTS) and path.endswith(".json")
        }
        view = IdentityView(
            connection,
            IdentityOverlay(
                subjects=dict(lifecycle),
                keys={subject: keys_now.get(subject) for subject in affected},
                aliases_removed=aliases_removed - aliases_added,
                aliases_added=aliases_added - aliases_removed,
            ),
        )
        naming = IdentityView(
            connection,
            IdentityOverlay(
                subjects={
                    path: live for path, live in lifecycle.items() if path not in created_set
                },
                aliases_removed=aliases_removed - aliases_added,
                aliases_added=aliases_added - aliases_removed,
            ),
        )

        def holders(kind: str, key: KeyTuple) -> set[str]:
            return set(view.holders(kind, key))

        # Created keyed Subjects carry core's binding and take exactly the id it
        # derives, in canonical-key order.
        names: dict[str, set[str]] = {}
        first_taken: set[tuple[str, str]] = set()
        for subject in sorted(
            (item for item in created if item in keys_now),
            key=lambda item: (subject_kind_of(item)[0], key_order(keys_now[item]), item),
        ):
            kind, subject_id = subject_kind_of(subject)
            key = keys_now[subject]
            assigned = names.setdefault(kind, set())
            shell = shells.get(subject)
            assignment = None if shell is None else shell.assignment
            others = holders(kind, key) - {subject}
            outside = {other for other in others if other not in created_set}
            marker = (kind, key_encoding(key))
            expected = None
            if assignment is not None and assignment.rule == "further_holder":
                if outside or others:
                    expected = distinct_holder_id(
                        key_values(key), operation=assignment.operation, member=assignment.member
                    )
                if expected == subject_id and not _authoritative(
                    assignment_authority, assignment, kind
                ):
                    assigned.add(subject_id)
                    issues.append(_unrecorded_assignment(subject, kind, assignment))
                    continue
            elif assignment is None and not outside and marker not in first_taken:
                first_taken.add(marker)
                try:
                    expected = assign_creation_id(naming, kind, key, names=assigned)
                except ValueError:
                    expected = None
            assigned.add(subject_id)
            if expected != subject_id:
                issues.append(
                    SubjectIdentityIssue(
                        SUBJECT_ID_NOT_ASSIGNED,
                        subject,
                        f"{_ref(subject)} is a new {kind}, whose id core assigns from its key "
                        "and records how; this one's id or binding is not the one core "
                        "assigns. Create it with a subject member naming its kind and key and "
                        "let core name it.",
                    )
                )

        for subject in sorted(keys_now):
            kind = affected[subject]
            declared_kind = declared(kind)
            if declared_kind is None or not declared_kind.unique:
                continue
            others_unique = tuple(
                sorted(_ref(other) for other in holders(kind, keys_now[subject]) - {subject})
            )
            if others_unique:
                issues.append(
                    SubjectIdentityIssue(
                        SUBJECT_KEY_CONFLICT,
                        subject,
                        subject_key_message(_ref(subject), kind, keys_now[subject], others_unique),
                        others_unique,
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


SUBJECT_KIND_KEY_BROKEN = "cruxible.subject_kind.key_claim_type_broken"
SUBJECT_KIND_REKEY_UNHANDLED = "cruxible.subject_kind.rekey_unhandled"


def key_claim_type_shape(tree: Mapping[str, bytes], predicate: str) -> KeyClaimTypeShape | None:
    """What the SubjectKind law reads of one key ClaimType in ``tree`` (None when absent)."""

    from cruxible_client.contracts.claim_types import (
        ClaimTypeFormatError,
        claim_type_path,
        parse_claim_type,
    )
    from cruxible_client.contracts.subject_kinds import KeyClaimTypeShape

    try:
        path = claim_type_path(predicate)
    except ClaimTypeFormatError:
        return None
    content = tree.get(path)
    if content is None:
        return None
    claim_type = parse_claim_type(content, path=path)
    return KeyClaimTypeShape(
        predicate=predicate,
        live=claim_type.lifecycle.state == "live",
        artifact_format=claim_type.artifact_format,
        object_kind=claim_type.object_kind,
        cardinality=claim_type.cardinality,
        allowed_subject_kinds=tuple(claim_type.allowed_subject_kinds),
    )


def _key_definition_issues(
    connection: sqlite3.Connection,
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
    declared: Callable[[str], SubjectKind | None],
    changed_kinds: set[str],
    values: Callable[[str, str], list[SubjectKeyValue | None]],
) -> list[SubjectIdentityIssue]:
    """A key ClaimType's succession or retirement that breaks a live SubjectKind (review F-004).

    Each live SubjectKind depends on its key ClaimTypes. A change that succeeds or
    retires one is judged against every kind it keys: the kind's key contract
    (live, v8, literal, one-valued, about the kind only) must still hold -- unless
    the change also succeeds that SubjectKind, whose own law then judges it
    (``key_claim_type_broken``). A changed canonical form re-keys the kind: every
    live key value must already be in the new form in the candidate, its
    holders re-authored in the same change (``rekey_unhandled``), and a unique
    kind must hold each new key once (``key_conflict``).
    """

    from cruxible_client.contracts.claim_types import (
        effective_canonical_form,
        parse_claim_type,
    )
    from cruxible_client.contracts.subject_kinds import key_claim_type_problem, subject_kind_path

    changed: dict[str, tuple[ClaimType, ClaimType | None]] = {}
    for path in scope:
        if not path.startswith("claim-types/"):
            continue
        before, after = current_tree.get(path), candidate_tree.get(path)
        if before is None:
            continue
        old = parse_claim_type(before, path=path)
        new = None if after is None else parse_claim_type(after, path=path)
        changed[old.predicate] = (old, new)
    if not changed:
        return []
    keyed: dict[str, tuple[str, ...]] = {}
    for identity, keys in connection.execute(
        "SELECT identity, key_predicates FROM subject_kinds WHERE rule='keys' AND lifecycle='live'"
    ):
        keyed[str(identity).removeprefix("SubjectKind:")] = tuple(json.loads(str(keys)))
    for kind in changed_kinds:
        declared_kind = declared(kind)
        if declared_kind is None or not declared_kind.keys:
            keyed.pop(kind, None)
        else:
            keyed[kind] = declared_kind.keys
    issues: list[SubjectIdentityIssue] = []
    for kind, keys in sorted(keyed.items()):
        kind_path = subject_kind_path(kind)
        for predicate in keys:
            if predicate not in changed:
                continue
            old, new = changed[predicate]
            if kind not in changed_kinds:
                problem = key_claim_type_problem(
                    kind, key_claim_type_shape(candidate_tree, predicate), predicate
                )
                if problem is not None:
                    issues.append(
                        SubjectIdentityIssue(
                            SUBJECT_KIND_KEY_BROKEN,
                            kind_path,
                            f"SubjectKind {kind} is keyed by {predicate}, and this change "
                            f"breaks that key: {problem}. Succeed the SubjectKind in the same "
                            "change (other keys, or minted), or keep the key ClaimType's shape.",
                        )
                    )
                    continue
            if new is None or effective_canonical_form(old) == effective_canonical_form(new):
                continue
            form = effective_canonical_form(new)
            declared_kind = declared(kind)
            low, high = _kind_range(kind)
            subjects = [
                str(row[0])
                for row in connection.execute(
                    "SELECT path FROM subjects WHERE path>=? AND path<? AND lifecycle='live'",
                    (low, high),
                )
                if _live(candidate_tree, str(row[0]))
            ]
            stale: list[str] = []
            for subject in subjects:
                for value in values(subject, predicate):
                    if value is None:
                        continue
                    try:
                        fixed = canonicalize_literal(value, form)
                    except CanonicalFormDidNotConverge:
                        fixed = None
                    if fixed != value:
                        stale.append(subject)
                        break
            if stale:
                issues.append(
                    SubjectIdentityIssue(
                        SUBJECT_KIND_REKEY_UNHANDLED,
                        kind_path,
                        f"{predicate} keys {kind}, and this change gives it a new canonical "
                        f"form: a re-keying. {len(stale)} live {kind} Subject(s) still hold "
                        f"a key value outside it ({', '.join(_ref(item) for item in stale[:5])}"
                        f"{', ...' if len(stale) > 5 else ''}); re-author their key Claims in "
                        "the same change (re_author), or keep the form.",
                        tuple(_ref(item) for item in stale),
                    )
                )
                continue
            if declared_kind is None or not declared_kind.unique:
                continue
            groups: dict[str, list[str]] = {}
            for subject in subjects:
                parts: list[tuple[str, SubjectKeyValue]] = []
                for key_predicate in declared_kind.keys:
                    found = values(subject, key_predicate)
                    if len(found) != 1 or found[0] is None:
                        break
                    parts.append((key_predicate, found[0]))
                else:
                    groups.setdefault(key_encoding(tuple(parts)), []).append(subject)
            for holders in groups.values():
                if len(holders) > 1:
                    ordered = sorted(holders)
                    issues.append(
                        SubjectIdentityIssue(
                            SUBJECT_KEY_CONFLICT,
                            ordered[1],
                            f"Re-keying {kind} by {predicate}'s new canonical form gives "
                            f"{', '.join(_ref(item) for item in ordered)} one key; {kind} keys "
                            "are unique. Alias and retire all but one in the same change, or "
                            "keep the form.",
                            tuple(_ref(item) for item in ordered),
                        )
                    )
    return issues


def _kind_wide_conflicts(
    connection: sqlite3.Connection,
    kind: str,
    declared_kind: SubjectKind,
    keys_now: Mapping[str, KeyTuple],
    affected: Mapping[str, str],
) -> list[SubjectIdentityIssue]:
    """Every pair of live Subjects of ``kind`` that hold one key, once the kind is unique.

    One pass over the kind's live Subjects under the new keys (a governance
    event's cost: it runs only when a SubjectKind becomes unique or changes its
    keys), overlaid with the Subjects the change itself moves.
    """

    low, high = _kind_range(kind)
    tuples: dict[str, KeyTuple] = {}
    for (subject,) in connection.execute(
        "SELECT path FROM subjects WHERE path>=? AND path<? AND lifecycle='live'", (low, high)
    ):
        path = str(subject)
        if path in affected:
            continue
        key = subject_key_of(connection, path, declared_kind.keys)
        if key is not None:
            tuples[path] = key
    tuples.update((path, key) for path, key in keys_now.items() if affected.get(path) == kind)
    groups: dict[str, list[str]] = {}
    for path, key in tuples.items():
        groups.setdefault(key_encoding(key), []).append(path)
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
    "SUBJECT_COMPILER_UPGRADE_REQUIRED",
    "SUBJECT_ID_NOT_ASSIGNED",
    "SubjectAssignmentAuthority",
    "SUBJECT_KIND_KEY_BROKEN",
    "SUBJECT_KIND_REKEY_UNHANDLED",
    "key_claim_type_shape",
    "boundary_identity_issues",
    "compiler_upgrade_repair",
    "SUBJECT_KEY_CONFLICT",
    "SUBJECT_KEY_CONTENDED",
    "SUBJECT_KEY_INVALID",
    "SUBJECT_KEY_MISSING",
    "KeyTuple",
    "SubjectKeyInvalid",
    "canonical_key",
    "key_encoding",
    "key_order",
    "refresh_subject_keys",
    "subject_key_of",
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
