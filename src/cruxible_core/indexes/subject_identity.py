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
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from cruxible_client.contracts.claim_types import ClaimType, effective_canonical_form
from cruxible_client.contracts.identity_normalizers import (
    NO_NORMALIZER,
    KeyNormalizationRefused,
    KeyNormalizer,
    canonical_key_part,
    normalize_key_value,
    normalizer_spelling,
)
from cruxible_client.contracts.shapes import (
    claim_value_kind,
    subject_value_kinds,
    value_type_name,
)
from cruxible_client.contracts.subject_kinds import (
    KeyClaimTypeShape,
    SubjectEnd,
    SubjectKeyPartValue,
    SubjectKeyValue,
    SubjectKind,
    SubjectKindFormatError,
    key_part_json,
    key_value_problem,
    live_subject_kind,
    ordered_key_values,
    parse_subject_kind,
    subject_key_ends,
)
from cruxible_core.indexes.sql_batch import execute_in, insert_rows, select_in

if TYPE_CHECKING:
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


def subject_kind_of(path: str) -> tuple[str, str]:
    """``(kind, id)`` of a Subject's ledger path."""

    kind, _, rest = path.removeprefix(_SUBJECTS).partition("/")
    return kind, rest.removesuffix(".json")


PartKeyTuple = tuple[tuple[str, SubjectKeyPartValue], ...]
"""A canonical key: each key predicate and its canonical value, in the kind's key order.

A Subject-valued part's value is the end Subject (``SubjectEnd``, its path).
"""

KeyTuple = PartKeyTuple
"""A canonical key as every route types it: literal parts and, for a pair kind, ends."""


@dataclass(frozen=True)
class PendingEnd:
    """An end created in the same change, before core has assigned its path [F2].

    Named by its creation member's identity, which is unique within a change
    (two minted ends, or two further holders of one ``hint`` key, never share
    one), so a pair's in-change token never confuses distinct ends. Only for
    ordering and early dedupe; never persisted: once the end's layer is
    assigned the token becomes the end's ``SubjectEnd``.
    """

    member: str


PendingKey = tuple[tuple[str, "SubjectKeyPartValue | PendingEnd"], ...]
"""A key whose Subject-valued parts may still be pending ends (``PendingEnd``)."""


def _key_part_json(value: SubjectKeyPartValue | PendingEnd) -> object:
    if isinstance(value, PendingEnd):
        return {"pending": value.member}
    return key_part_json(value)


class SubjectKeyInvalid(ValueError):
    """A key that cannot name a Subject of its kind; the message says why."""


def canonical_key_value(
    value: object, claim_type: ClaimType, normalizer: KeyNormalizer, *, predicate: str
) -> SubjectKeyPartValue:
    """One key part's canonical value: its ClaimType's canonical form, then its normalizer.

    The result must be a fixed point of both rules (a value the form would
    change again is a conflict between the two, never silently re-spelled).
    Raises ``SubjectKeyInvalid`` naming the part and why.
    """

    if claim_value_kind(claim_type.value) == "subject":
        # A Subject-valued part's canonical value is the end's path: no form,
        # no normalizer, and the end is of the one kind the part names.
        ends = subject_value_kinds(claim_type.value)
        if not isinstance(value, SubjectEnd) or value.subject_kind not in ends:
            raise SubjectKeyInvalid(
                f"{predicate}: a Subject-valued key part names a Subject of "
                + ", ".join(ends)
                + f", not {value!r}"
            )
        return value
    try:
        return canonical_key_part(  # type: ignore[return-value]
            value, effective_canonical_form(claim_type), normalizer
        )
    except KeyNormalizationRefused as exc:
        raise SubjectKeyInvalid(f"{predicate}: {exc}") from exc


def canonical_key(
    subject_kind: SubjectKind,
    key: Mapping[str, object],
    *,
    claim_type: Callable[[str], ClaimType | None],
) -> KeyTuple:
    """The canonical key a creation names: each part in key order, in its canonical form.

    ``key`` maps key ClaimTypes (by predicate or last name) to raw values;
    ``claim_type`` reads a key ClaimType as the change sees it. Each part's
    value takes its ClaimType's canonical form, then the identity normalizer
    the SubjectKind pins for it. Raises ``SubjectKeyInvalid`` naming what is
    missing, unknown or unsettled.
    """

    try:
        parts = ordered_key_values(subject_kind, key)  # type: ignore[arg-type]
    except ValueError as exc:
        raise SubjectKeyInvalid(str(exc)) from exc
    found: list[tuple[str, SubjectKeyPartValue]] = []
    for predicate, raw_value in parts:
        definition = claim_type(predicate)
        if definition is None:
            raise SubjectKeyInvalid(f"key ClaimType {predicate!r} is not accepted")
        value = canonical_key_value(
            raw_value,
            definition,
            subject_kind.normalizer(predicate),
            predicate=predicate,
        )
        found.append((predicate, value))
    return tuple(found)


def key_normalizer_of(
    claim_type: ClaimType, subject_kind: Callable[[str], SubjectKind | None]
) -> KeyNormalizer | None:
    """The normalizer a key ClaimType's values take, when it keys a live kind (R12).

    A key ClaimType is about exactly one kind; its SubjectKind (read through
    ``subject_kind``) names its normalizer. None for a non-key predicate and
    for ``none`` (its values compare under the ClaimType form alone).
    """

    from cruxible_client.contracts.identity_normalizers import NoneNormalizer

    kinds = tuple(claim_type.allowed_subject_kinds)
    if len(kinds) != 1:
        return None
    declared = subject_kind(kinds[0])
    if declared is None or claim_type.predicate not in declared.keys:
        return None
    rule = declared.normalizer(claim_type.predicate)
    return None if isinstance(rule, NoneNormalizer) else rule


def tree_subject_kind_reader(tree: Mapping[str, bytes]) -> Callable[[str], SubjectKind | None]:
    """The live SubjectKind of a kind in ``tree``, memoized per kind."""

    known: dict[str, SubjectKind | None] = {}

    def read(kind: str) -> SubjectKind | None:
        if kind not in known:
            try:
                known[kind] = live_subject_kind(tree, kind)
            except SubjectKindFormatError:
                known[kind] = None
        return known[kind]

    return read


class KeyRules:
    """SubjectKind identity rules as a Contract value check reads them (``KeyCanonicalizer``).

    ``subject_kind`` and ``claim_type`` read the live SubjectKind of a kind and
    an accepted ClaimType at one coordinate (an admitted run's, or a tree's);
    both are memoized, so a run reads each definition once.
    """

    def __init__(
        self,
        subject_kind: Callable[[str], SubjectKind | None],
        claim_type: Callable[[str], ClaimType | None],
    ) -> None:
        self._subject_kind = subject_kind
        self._claim_type = claim_type
        self._kinds: dict[str, SubjectKind | None] = {}
        self._types: dict[str, ClaimType | None] = {}

    def kind(self, subject_kind: str) -> SubjectKind | None:
        if subject_kind not in self._kinds:
            declared = self._subject_kind(subject_kind)
            self._kinds[subject_kind] = (
                declared if declared is not None and declared.lifecycle.state == "live" else None
            )
        return self._kinds[subject_kind]

    def claim_type(self, predicate: str) -> ClaimType | None:
        if predicate not in self._types:
            self._types[predicate] = self._claim_type(predicate)
        return self._types[predicate]

    def canonical(self, subject_kind: str, part: str | None, value: object) -> object:
        from cruxible_client.contracts.shapes import KeyValueRefused
        from cruxible_client.contracts.subject_kinds import key_predicate, key_reference_mapping

        declared = self.kind(subject_kind)
        if declared is None or declared.minted:
            raise KeyValueRefused(f"no live keyed SubjectKind declares {subject_kind}")
        try:
            if part is not None:
                predicate = key_predicate(declared, part)
                definition = None if predicate is None else self.claim_type(predicate)
                if predicate is None or definition is None:
                    raise SubjectKeyInvalid(f"{part!r} is not an accepted key of {subject_kind}")
                return canonical_key_value(
                    value, definition, declared.normalizer(predicate), predicate=predicate
                )
            if isinstance(value, Mapping):
                given: dict[str, object] = dict(value)
            elif key_value_problem(value) is None:
                given = key_reference_mapping(declared, value)  # type: ignore[arg-type]
            else:
                raise SubjectKeyInvalid(f"{value!r} is not a key of {subject_kind}")
            key = canonical_key(declared, given, claim_type=self.claim_type)
        except (SubjectKeyInvalid, ValueError) as exc:
            raise KeyValueRefused(str(exc)) from exc
        if not isinstance(value, Mapping):
            return key[0][1]
        prefix = declared.subject_kind + "."
        return {predicate.removeprefix(prefix): part_value for predicate, part_value in key}

    def rule_token(self, subject_kind: str) -> object:
        from cruxible_client.contracts.subject_kinds import subject_kind_digest

        declared = self.kind(subject_kind)
        if declared is None:
            return None
        return {
            "subject_kind": subject_kind_digest(declared).tagged,
            "forms": {
                predicate: (
                    None
                    if (definition := self.claim_type(predicate)) is None
                    else list(effective_canonical_form(definition))
                )
                for predicate in declared.keys
            },
        }


def tree_key_rules(tree: Mapping[str, bytes]) -> KeyRules:
    """The identity rules of the SubjectKinds and key ClaimTypes ``tree`` accepts."""

    from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type

    def subject_kind(kind: str) -> SubjectKind | None:
        try:
            return live_subject_kind(tree, kind)
        except SubjectKindFormatError:
            return None

    def claim_type(predicate: str) -> ClaimType | None:
        path = claim_type_path(predicate)
        content = tree.get(path)
        return None if content is None else parse_claim_type(content, path=path)

    return KeyRules(subject_kind, claim_type)


def key_values(key: PartKeyTuple) -> tuple[SubjectKeyPartValue, ...]:
    return tuple(value for _predicate, value in key)


def key_encoding(key: PartKeyTuple | PendingKey) -> str:
    """A canonical key's one comparable spelling: typed, with its predicates, in key order.

    Canonical JSON keeps literal types apart (``true`` is not ``1``), so two keys
    are equal exactly when their encodings are. Every comparison of keys -- the
    index, the change overlay, holder grouping -- compares these, never Python
    values (where ``True == 1``). A Subject-valued part encodes as
    ``{"subject": path}`` (a pending end as ``{"pending": member}``): a JSON
    object never equals a literal, so every literal key keeps its bytes.
    """

    from cruxible_client.contracts.canonical import canonical_bytes

    return canonical_bytes([[predicate, _key_part_json(value)] for predicate, value in key]).decode(
        "utf-8"
    )


def key_order(key: PartKeyTuple) -> bytes:
    """The order core assigns ids in when one change creates several Subjects of a kind."""

    from cruxible_client.contracts.canonical import canonical_bytes

    return canonical_bytes([key_part_json(value) for value in key_values(key)])


def kind_depths(
    kinds: Iterable[str],
    *,
    subject_kind_of: Callable[[str], SubjectKind | None],
    key_shape_of: Callable[[str], KeyClaimTypeShape | None],
) -> dict[str, int]:
    """Each kind's layer for id assignment: 0 without Subject-valued key parts, else 1 + its ends'.

    Ends are named before their pairs: a layer's keys are canonical only once the
    layer below has paths. The SubjectKind law keeps the kinds acyclic and at
    most ``MAX_KEY_DEPTH`` deep, so the walk is bounded; a kind it cannot read
    is layer 0.
    """

    from cruxible_client.contracts.subject_kinds import MAX_KEY_DEPTH

    depths: dict[str, int] = {}

    def depth(kind: str, seen: tuple[str, ...]) -> int:
        if kind in depths:
            return depths[kind]
        declared = subject_kind_of(kind)
        found = 0
        if declared is not None and len(seen) <= MAX_KEY_DEPTH:
            ends = subject_key_ends(
                declared, {predicate: key_shape_of(predicate) for predicate in declared.keys}
            )
            for end in ends.values():
                if end not in seen:
                    found = max(found, 1 + depth(end, (*seen, end)))
        depths[kind] = found
        return found

    for kind in kinds:
        depth(kind, (kind,))
    return depths


def creation_order(
    depth: int, kind: str, key: PartKeyTuple, member: str
) -> tuple[int, str, bytes, str]:
    """The one order every route assigns creation ids in: layer, kind, canonical key, member.

    Lowering (layer by layer) and the candidate boundary's re-derivation both
    sort by it, so they assign the same ids whatever the members' order.
    """

    return (depth, kind, key_order(key), member)


# ``+lifecycle`` keeps the planner on ``claims_by_subject_predicate`` (see
# ``typed_state.LIVE_SLOT_CLAIMS_SQL``). A key part is a literal or (a pair
# kind's end) a Subject; a literal key ClaimType never holds a Subject object,
# so its rows are exactly what the literal-only filter read.
_KEY_CLAIM = (
    "+lifecycle='live' AND object_kind IN ('literal','subject') AND qualifier IS NULL "
    "AND subject_selector_scheme='artifact-v1'"
)
LIVE_KEY_CLAIMS_SQL = (
    "SELECT identity, literal_type, literal_text, literal_integer_text, literal_boolean, "
    "object_kind, object_path, object_selector_scheme "
    f"FROM claims WHERE subject_path=? AND predicate=? AND {_KEY_CLAIM} ORDER BY identity"
)


def _kind_range(kind: str) -> tuple[str, str]:
    prefix = f"{_SUBJECTS}{kind}/"
    return prefix, prefix[:-1] + chr(ord("/") + 1)


def subjects_with_key(
    connection: sqlite3.Connection, kind: str, key: PartKeyTuple
) -> tuple[str, ...]:
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
) -> PartKeyTuple | None:
    """The whole canonical key one Subject holds in ``connection``, or None.

    None when a key part has no live value, several, or one that is not a
    string, an integer or a boolean.
    """

    parts: list[tuple[str, SubjectKeyPartValue]] = []
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
        execute_in(
            connection, "DELETE FROM subject_keys WHERE subject_path IN ({})", sorted(targets)
        )
    keyed_targets = sorted(path for path in targets if keyed.get(subject_kind_of(path)[0]))
    live = {
        str(row[0])
        for row in select_in(
            connection,
            "SELECT path FROM subjects WHERE lifecycle='live' AND path IN ({})",
            keyed_targets,
        )
    }
    rows: list[tuple[str, str, str]] = []
    for path in keyed_targets:
        if path not in live:
            continue
        kind = subject_kind_of(path)[0]
        key = subject_key_of(connection, path, keyed[kind])
        if key is not None:
            rows.append((path, kind, key_encoding(key)))
    insert_rows(connection, "subject_keys", rows)


def live_key_claims(
    connection: sqlite3.Connection, subject_path: str, predicate: str
) -> tuple[tuple[str, SubjectKeyPartValue | None], ...]:
    """Each live key Claim of one Subject and predicate: ``(Claim identity, value)``.

    A Subject object naming a whole Subject is that end (``SubjectEnd``).
    """

    rows = connection.execute(LIVE_KEY_CLAIMS_SQL, (subject_path, predicate)).fetchall()
    found: list[tuple[str, SubjectKeyPartValue | None]] = []
    for row in rows:
        identity, literal_type, text, integer, boolean, object_kind, object_path, scheme = row
        value: SubjectKeyPartValue | None
        if object_kind == "subject":
            value = _subject_end(object_path, scheme)
        elif literal_type == "string":
            value = str(text)
        elif literal_type == "integer":
            value = int(integer)
        elif literal_type == "boolean":
            value = bool(boolean)
        else:
            value = None
        found.append((str(identity), value))
    return tuple(found)


def _subject_end(path: object, scheme: object) -> SubjectEnd | None:
    """A Subject object as a key part: the whole Subject it names, else None."""

    if scheme != "artifact-v1" or not isinstance(path, str):
        return None
    try:
        return SubjectEnd(path=path)
    except ValueError:
        return None


@dataclass(frozen=True)
class SingleKey:
    """The one key of a single-key kind: its predicate, ClaimType and normalizer."""

    predicate: str
    claim_type: ClaimType
    normalizer: KeyNormalizer


def single_key(tree: Mapping[str, bytes], kind: str) -> SingleKey | None:
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
    claim_type = parse_claim_type(content, path=claim_type_path(predicate))
    if claim_value_kind(claim_type.value) == "subject":
        # A pair is never named by bare key text: only by id, by its creation,
        # or by a structured key.
        return None
    return SingleKey(
        predicate=predicate,
        claim_type=claim_type,
        normalizer=declared.normalizer(predicate),
    )


def single_key_value(key: SingleKey, text: str) -> SubjectKeyValue | None:
    """``text`` as the canonical value of a single key, or None when it cannot be one."""

    try:
        value = canonical_key_value(text, key.claim_type, key.normalizer, predicate=key.predicate)
    except SubjectKeyInvalid:
        return None
    return value if isinstance(value, str) and value else None


KeyReader = Callable[[str, str], "KeyTuple | None"]
"""Read ``kind/<text>`` as a canonical single key of ``kind``: None when it cannot be one."""


def tree_key_reader(tree: Mapping[str, bytes]) -> KeyReader:
    """The single-key reading of ``kind/<text>`` under the SubjectKinds of ``tree``."""

    known: dict[str, SingleKey | None] = {}

    def read(kind: str, text: str) -> KeyTuple | None:
        if kind not in known:
            known[kind] = single_key(tree, kind)
        found = known[kind]
        if found is None:
            return None
        value = single_key_value(found, text)
        return None if value is None else ((found.predicate, value),)

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
    keys: dict[str, PartKeyTuple | None] = field(default_factory=dict)
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

    def holders(self, kind: str, key: PartKeyTuple) -> tuple[str, ...]:
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

    def add_creation(self, path: str, key: PartKeyTuple | None) -> None:
        """Lay one Subject the change creates over the view (it holds ``key``).

        The holder map already built is extended in place, so a batch of N
        creations encodes each key once, never N(N-1)/2 times.
        """

        held = self.overlay.keys.get(path)
        self.overlay.subjects[path] = True
        self.overlay.keys[path] = key
        if self._overlay_holders is None:
            return
        kind = subject_kind_of(path)[0]
        if held is not None:
            self._overlay_holders.get((kind, key_encoding(held)), set()).discard(path)
        if key is not None:
            self._overlay_holders.setdefault((kind, key_encoding(key)), set()).add(path)

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

    def resolve_key_reference(
        self, kind: str, key: PartKeyTuple, *, single_text: str | None
    ) -> SubjectRefResolution:
        """A reference to an existing Subject by its key, through the one resolver order.

        A one-key reference is ``kind/<text>`` (``resolve``): exact live id,
        alias, the key, exact retired id. A composite key has no text: only its
        holders name it.
        """

        from cruxible_core.indexes.subject_aliases import SubjectRefResolution

        if single_text is not None:
            exact = f"{_SUBJECTS}{kind}/{single_text}.json"
            if self.live(exact):
                return SubjectRefResolution(
                    kind=kind, subject_id=single_text, path=exact, via="identity"
                )
            aliased = self.alias_targets(kind, single_text)
            if aliased:
                return SubjectRefResolution(
                    kind=kind,
                    subject_id=single_text,
                    path=aliased[0] if len(aliased) == 1 else None,
                    via="alias",
                    candidates=() if len(aliased) == 1 else tuple(_ref(item) for item in aliased),
                )
        text = single_text or ", ".join(f"{predicate}={value!r}" for predicate, value in key)
        held = self.holders(kind, key)
        if held:
            return SubjectRefResolution(
                kind=kind,
                subject_id=text,
                path=held[0] if len(held) == 1 else None,
                via="key",
                candidates=() if len(held) == 1 else tuple(_ref(item) for item in held),
            )
        if single_text is not None and self.exists(f"{_SUBJECTS}{kind}/{single_text}.json"):
            return SubjectRefResolution(
                kind=kind,
                subject_id=single_text,
                path=f"{_SUBJECTS}{kind}/{single_text}.json",
                via="identity",
            )
        return SubjectRefResolution(kind=kind, subject_id=text, path=None, via=None)

    def resolve_creation(
        self, kind: str, key: PartKeyTuple, *, single_text: str | None, create_new: bool
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
    key: PartKeyTuple,
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


def key_ambiguous_message(kind: str, holders: Sequence[str]) -> str:
    return (
        f"{len(holders)} live {kind} Subjects hold this key: "
        + ", ".join(_ref(path) for path in holders)
        + "; name one by id."
    )


def spell_key(key: PartKeyTuple) -> str:
    """A canonical key as a refusal names it: ``predicate=value``, an end as ``kind/id``."""

    return ", ".join(
        f"{predicate}={_ref(value.path) if isinstance(value, SubjectEnd) else repr(value)}"
        for predicate, value in key
    )


def subject_key_message(created: str, kind: str, key: PartKeyTuple, others: Sequence[str]) -> str:
    """The one refusal text for a Subject whose canonical key another live Subject holds."""

    spelled = spell_key(key)
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


def write_create_change_shape(
    kind: str, keys: Sequence[str], *, label: str = "new", minted: bool = False
) -> str:
    """The ``write`` change that creates a Subject of ``kind``, its key parts named.

    The same write names the created Subject as ``{"created": label}``. A minted
    kind's create carries no key; a keyed one names each declared key predicate.
    """

    change: dict[str, object] = {"op": "create", "label": label, "subject_kind": kind}
    if not minted:
        change["key"] = [{"predicate": predicate, "value": "<value>"} for predicate in keys]
    return json.dumps(change)


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
        f"Author a SubjectKind for {kind} first: keyed by a key ClaimType (a ClaimType about "
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
    value: SubjectKeyPartValue | None
    live: bool
    #: The Claim's statement, compared before and after for a pair's endpoint.
    statement: Any = None


def _key_claim_row(
    content: bytes | None, path: str, key_kinds: Mapping[str, str]
) -> _KeyClaimRow | None:
    """The key part one Claim version states, when it states one."""

    from cruxible_client.contracts.claims import (
        LiteralClaimObject,
        SubjectClaimObject,
        parse_claim,
    )

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
    value: SubjectKeyPartValue | None
    if isinstance(statement.object, SubjectClaimObject):
        address = statement.object.address
        value = _subject_end(address.artifact_path, address.selector.scheme)
    elif isinstance(statement.object, LiteralClaimObject):
        raw = statement.object.value
        value = None if key_value_problem(raw) is not None else raw  # type: ignore[assignment]
    else:
        value = None
    return _KeyClaimRow(
        subject_path=subject.artifact_path,
        predicate=statement.predicate,
        value=value,
        live=claim.lifecycle.state == "live",
        statement=statement,
    )


SUBJECT_KEY_ENDPOINT_FIXED = "cruxible.subject.key_endpoint_fixed"
SUBJECT_KEY_ENDPOINT_RETIRED = "cruxible.subject.key_endpoint_retired"
SUBJECT_KIND_ENDPOINT_DECLARATION_FIXED = "cruxible.subject_kind.endpoint_declaration_fixed"


def _any_subject_kind(tree: Mapping[str, bytes], kind: str) -> SubjectKind | None:
    """The SubjectKind declaring ``kind`` in ``tree``, whatever its lifecycle."""

    from cruxible_client.contracts.subject_kinds import subject_kind_path

    try:
        path = subject_kind_path(kind)
    except SubjectKindFormatError:
        return None
    content = tree.get(path)
    if content is None:
        return None
    try:
        return parse_subject_kind(content, path=path)
    except SubjectKindFormatError:
        return None


class _SubjectParts:
    """Which key predicates are Subject-valued, read from their ClaimTypes once each.

    The candidate's ClaimType decides, the parent's when the candidate holds none;
    a part Subject-valued in the parent stays one (its successor is refused
    otherwise, ``key_claim_type_broken``).
    """

    def __init__(self, current_tree: Mapping[str, bytes], candidate_tree: Mapping[str, bytes]):
        self._current = current_tree
        self._candidate = candidate_tree
        self._shapes: dict[str, KeyClaimTypeShape | None] = {}
        self._parts: dict[str, bool] = {}

    def shape(self, predicate: str) -> KeyClaimTypeShape | None:
        if predicate not in self._shapes:
            self._shapes[predicate] = key_claim_type_shape(self._candidate, predicate)
        return self._shapes[predicate]

    def is_part(self, predicate: str) -> bool:
        if predicate not in self._parts:
            before = key_claim_type_shape(self._current, predicate)
            after = self.shape(predicate)
            self._parts[predicate] = any(
                shape is not None and shape.object_kind == "subject" for shape in (before, after)
            )
        return self._parts[predicate]


_POINTING_KEY_CLAIMS_SQL = (
    "SELECT identity, subject_path, predicate FROM claims WHERE object_path=? "
    "AND object_kind='subject' AND lifecycle='live' AND qualifier IS NULL "
    "AND subject_selector_scheme='artifact-v1' ORDER BY identity"
)


def _endpoint_issues(
    connection: sqlite3.Connection,
    *,
    touched: Mapping[str, tuple[_KeyClaimRow | None, _KeyClaimRow | None]],
    created: set[str],
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
    subject_parts: _SubjectParts,
    touched_identities: set[str],
    kind_keys: Callable[[str], tuple[str, ...]],
) -> list[SubjectIdentityIssue]:
    """A pair's endpoints are frozen for life, and its ends stay live while it is.

    - A Subject-valued key Claim is written only in its pair's creation change,
      never revised, and retired only lifecycle-only, together with its pair
      (``key_endpoint_fixed``). Moving a judgment to another pair is an explicit
      retire and create. Literal key parts keep their re-key behaviour.
    - A Subject the change retires that is a live pair's end refuses unless the
      change retires that pair too (``key_endpoint_retired``): one seek on the
      Claims pointing at it (its fan-in), overlaid with the change's own.

    Cost: the touched key Claims, plus the fan-in of each Subject retired.
    """

    issues: list[SubjectIdentityIssue] = []
    for _path, (before, after) in sorted(touched.items()):
        row = after if after is not None else before
        assert row is not None
        if not subject_parts.is_part(row.predicate):
            continue
        pair = row.subject_path
        problem: str | None = None
        if before is None:
            if pair not in created:
                problem = (
                    f"adds a {row.predicate} endpoint to {_ref(pair)}, which already exists; a "
                    "pair's endpoints are written only when it is created"
                )
        elif after is None or before.statement != after.statement:
            problem = (
                f"changes {_ref(pair)}'s {row.predicate} endpoint; a pair's endpoints never change"
            )
        elif before.live and not after.live and _live(candidate_tree, pair):
            problem = (
                f"retires {_ref(pair)}'s {row.predicate} endpoint while {_ref(pair)} stays "
                "live; an endpoint retires only with its pair"
            )
        elif not before.live and after.live:
            problem = f"revives a retired {row.predicate} endpoint of {_ref(pair)}"
        if problem is not None:
            issues.append(
                SubjectIdentityIssue(
                    SUBJECT_KEY_ENDPOINT_FIXED,
                    pair,
                    f"This change {problem}. To move a judgment to other ends, retire "
                    f"{_ref(pair)} with its Claims and create the pair of the new ends.",
                )
            )
    # The change's own live endpoint Claims, by end.
    pointing: dict[str, set[str]] = {}
    for _before, after in touched.values():
        if (
            after is not None
            and after.live
            and isinstance(after.value, SubjectEnd)
            and subject_parts.is_part(after.predicate)
        ):
            pointing.setdefault(after.value.path, set()).add(after.subject_path)
    for path in scope:
        if (
            not path.startswith(_SUBJECTS)
            or path in created
            or not _live(current_tree, path)
            or _live(candidate_tree, path)
        ):
            continue
        pairs = set(pointing.get(path, ()))
        for identity, subject_path, predicate in connection.execute(
            _POINTING_KEY_CLAIMS_SQL, (path,)
        ):
            pair = str(subject_path)
            if (
                str(identity) not in touched_identities
                and str(predicate) in kind_keys(subject_kind_of(pair)[0])
                and subject_parts.is_part(str(predicate))
            ):
                pairs.add(pair)
        live_pairs = sorted(_ref(pair) for pair in pairs if _live(candidate_tree, pair))
        if live_pairs:
            issues.append(
                SubjectIdentityIssue(
                    SUBJECT_KEY_ENDPOINT_RETIRED,
                    path,
                    f"This change retires {_ref(path)}, an end of live pair(s) "
                    + ", ".join(live_pairs[:5])
                    + (", ..." if len(live_pairs) > 5 else "")
                    + "; retire them with their Claims in the same change (a pair outlives "
                    "no end), or keep the end.",
                    tuple(live_pairs),
                )
            )
    return issues


def _endpoint_declaration_issues(
    connection: sqlite3.Connection,
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    changed_kinds: set[str],
) -> list[SubjectIdentityIssue]:
    """A pair kind's identity is frozen for life (R13, F1, F12).

    Once a SubjectKind has a Subject-valued key part, every successor (a
    retiring one too) keeps its ``identity_rule`` byte-equal: the parts, their
    order, their normalizers and uniqueness. A kind becomes a pair kind only
    while it has no Subject at all, live or retired (one range probe): a Subject
    created before has no endpoints. A new identity is a new kind. Cost: the
    changed SubjectKinds' parts, one predecessor read and one probe each.
    """

    from cruxible_client.contracts.subject_kinds import subject_kind_path

    issues: list[SubjectIdentityIssue] = []
    for kind in sorted(changed_kinds):
        new = _any_subject_kind(candidate_tree, kind)
        if new is None:
            continue
        previous = _any_subject_kind(current_tree, kind)
        if previous is not None and subject_key_ends(
            previous,
            {
                predicate: key_claim_type_shape(current_tree, predicate)
                for predicate in previous.keys
            },
        ):
            if new.identity_rule.model_dump(mode="json") != previous.identity_rule.model_dump(
                mode="json"
            ):
                issues.append(
                    SubjectIdentityIssue(
                        SUBJECT_KIND_ENDPOINT_DECLARATION_FIXED,
                        subject_kind_path(kind),
                        f"{kind} is a pair kind (a Subject-valued key part), and its identity "
                        "rule is frozen for life: every successor keeps its key parts, their "
                        "order, normalizers and uniqueness. A new pair identity is a new kind; "
                        "move pairs to it by retiring each with its Claims and creating it anew.",
                    )
                )
            continue
        # Whatever the successor's lifecycle: a retiring successor that adds a
        # Subject part still makes every Subject of the kind a pair to a full build.
        if not subject_key_ends(
            new,
            {predicate: key_claim_type_shape(candidate_tree, predicate) for predicate in new.keys},
        ):
            continue
        if _kind_has_subjects(connection, kind):
            issues.append(_not_yet_a_pair_kind(kind))
    return issues


def _parent_kind_keys(connection: sqlite3.Connection, kind: str) -> tuple[str, ...]:
    """The key predicates the parent's SubjectKind of ``kind`` names, whatever its lifecycle."""

    row = connection.execute(
        "SELECT key_predicates FROM subject_kinds WHERE identity=? AND rule='keys'",
        (f"SubjectKind:{kind}",),
    ).fetchone()
    return () if row is None else tuple(str(item) for item in json.loads(str(row[0])))


_REFERRING_TYPES_SQL = (
    "SELECT n.source_identity FROM claim_type_names n JOIN claim_types t "
    "ON t.identity=n.source_identity WHERE n.name=? AND n.kind='subject_kind' "
    "AND t.object_kind='subject' ORDER BY n.source_identity"
)


def _ancestor_depth_issues(
    connection: sqlite3.Connection,
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
    changed_kinds: set[str],
    declared: Callable[[str], SubjectKind | None],
) -> list[SubjectIdentityIssue]:
    """A kind that gains a Subject-valued end deepens every pair kind keyed through it (F2).

    The SubjectKind law walks the judged kind's ends only, and an unchanged
    pair kind keyed through it pins nothing that would judge it again. A
    kind's height grows only when some kind at or below it gains an end: a
    SubjectKind successor or a key ClaimType turning Subject-valued (a first
    part, or another part of a mixed key), changed in this very change. So
    every kind the change declares or whose key ClaimType it changes is
    compared, ends before and after; one that gains an end is measured itself
    (unless its own law judged it), and every live ancestor the change leaves
    as it was is measured again over the candidate: none may nest more than
    ``MAX_KEY_DEPTH`` deep. The climb seeks the parent's live ClaimTypes naming
    each kind as a Subject (one indexed seek per kind) and stops at the first
    ancestor too deep, so it is bounded by the depth: a governance event's
    cost, paid only by such a change.
    """

    from cruxible_client.contracts.subject_kinds import MAX_KEY_DEPTH, subject_kind_path

    shapes: dict[str, KeyClaimTypeShape | None] = {}

    def shape(predicate: str) -> KeyClaimTypeShape | None:
        if predicate not in shapes:
            shapes[predicate] = key_claim_type_shape(candidate_tree, predicate)
        return shapes[predicate]

    from cruxible_client.contracts.claim_types import parse_claim_type

    seeds = set(changed_kinds)
    for path in scope:
        if not path.startswith("claim-types/"):
            continue
        for tree in (candidate_tree, current_tree):
            content = tree.get(path)
            if content is not None:
                seeds.update(parse_claim_type(content, path=path).allowed_subject_kinds)
    converted: list[str] = []
    for kind in sorted(seeds):
        new = declared(kind)
        if new is None:
            continue
        gained = set(subject_key_ends(new, {p: shape(p) for p in new.keys}).items())
        if not gained:
            continue
        previous = _any_subject_kind(current_tree, kind)
        if previous is not None:
            gained -= set(
                subject_key_ends(
                    previous, {p: key_claim_type_shape(current_tree, p) for p in previous.keys}
                ).items()
            )
        if gained:
            converted.append(kind)
    if not converted:
        return []
    heights: dict[str, int] = {}

    def height(kind: str, chain: tuple[str, ...]) -> int:
        # Exact up to MAX_KEY_DEPTH; anything deeper reads as MAX_KEY_DEPTH + 1
        # or more (the walk stops there).
        if kind in heights:
            return heights[kind]
        if len(chain) > MAX_KEY_DEPTH + 1:
            return MAX_KEY_DEPTH + 1
        rule = declared(kind)
        found = 0
        if rule is not None:
            for end in subject_key_ends(rule, {p: shape(p) for p in rule.keys}).values():
                if end not in chain:  # a cycle is the law's refusal
                    found = max(found, 1 + height(end, (*chain, end)))
        heights[kind] = found
        return found

    def referrers(kind: str) -> list[str]:
        """The live kinds keyed by a Subject-valued part naming ``kind``."""

        found: list[str] = []
        for (identity,) in connection.execute(_REFERRING_TYPES_SQL, (kind,)):
            predicate = str(identity).removeprefix("ClaimType:")
            part = shape(predicate)
            if (
                part is None
                or part.object_kind != "subject"
                or kind not in part.object_subject_kinds
                or len(part.allowed_subject_kinds) != 1
            ):
                continue
            owner = part.allowed_subject_kinds[0]
            rule = declared(owner)
            if rule is not None and predicate in rule.keys:
                found.append(owner)
        return found

    issues: list[SubjectIdentityIssue] = []
    reported: set[str] = set()

    def too_deep(kind: str, ancestor: str, chain: tuple[str, ...]) -> None:
        if ancestor in reported:
            return
        reported.add(ancestor)
        issues.append(
            SubjectIdentityIssue(
                "cruxible.subject_kind.key_claim_type_invalid",
                subject_kind_path(kind),
                f"This change gives {kind} a Subject-valued end, which nests the pair kind "
                f"{ancestor} more than {MAX_KEY_DEPTH} deep through Subject-valued key parts "
                f"({' -> '.join(chain)} -> ...). Declare the deeper pair as a new kind, or keep "
                "the key literal.",
                (ancestor,),
            )
        )

    for kind in converted:
        if kind not in changed_kinds and height(kind, (kind,)) > MAX_KEY_DEPTH:
            too_deep(kind, kind, (kind,))
            continue
        frontier: list[tuple[str, tuple[str, ...]]] = [(kind, (kind,))]
        climbed: set[str] = {kind}
        while frontier:
            below, chain = frontier.pop()
            for ancestor in referrers(below):
                if ancestor in climbed:
                    continue
                climbed.add(ancestor)
                upward = (ancestor, *chain)
                if ancestor not in changed_kinds and height(ancestor, (ancestor,)) > MAX_KEY_DEPTH:
                    too_deep(kind, ancestor, upward)
                    continue
                frontier.append((ancestor, upward))
    return issues


def _kind_has_subjects(connection: sqlite3.Connection, kind: str) -> bool:
    """Whether the parent holds any Subject of ``kind``, live or retired (one range probe)."""

    low, high = _kind_range(kind)
    return (
        connection.execute(
            "SELECT 1 FROM subjects WHERE path>=? AND path<? LIMIT 1", (low, high)
        ).fetchone()
        is not None
    )


def _not_yet_a_pair_kind(kind: str) -> SubjectIdentityIssue:
    from cruxible_client.contracts.subject_kinds import subject_kind_path

    return SubjectIdentityIssue(
        SUBJECT_KIND_ENDPOINT_DECLARATION_FIXED,
        subject_kind_path(kind),
        f"{kind} already has Subjects, which were not created as pairs and have no "
        "endpoints; a kind becomes a pair kind (a Subject-valued key part) only "
        "while it has no Subject, live or retired. Declare the pair as a new kind.",
    )


class _KindKeys:
    """Each kind's key predicates as a change leaves them: one indexed read per kind.

    A kind the change declares or succeeds reads its candidate SubjectKind
    (whatever its lifecycle); any other kind its parent row (whatever its
    lifecycle). A Claim version is a key Claim when its predicate is a key of
    its own Subject's kind, so a touched Claim costs a read of its statement's
    head and its kind's keys, never the instance's vocabulary.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        candidate_tree: Mapping[str, bytes],
        changed_kinds: set[str],
    ) -> None:
        self._connection = connection
        self._candidate = candidate_tree
        self._changed = changed_kinds
        self._known: dict[str, tuple[str, ...]] = {}

    def keys(self, kind: str) -> tuple[str, ...]:
        if kind not in self._known:
            if kind in self._changed:
                declared = _any_subject_kind(self._candidate, kind)
                self._known[kind] = () if declared is None else tuple(declared.keys)
            else:
                self._known[kind] = _parent_kind_keys(self._connection, kind)
        return self._known[kind]

    def key_claim_row(self, content: bytes | None, path: str) -> _KeyClaimRow | None:
        head = _claim_head(content)
        if head is None:
            return None
        subject, predicate = head
        if not subject.startswith(_SUBJECTS):
            return None
        kind = subject_kind_of(subject)[0]
        if predicate not in self.keys(kind):
            return None
        return _key_claim_row(content, path, {predicate: kind})


def _claim_head(content: bytes | None) -> tuple[str, str] | None:
    """A Claim version's Subject path and predicate, read without validating the Claim."""

    if content is None:
        return None
    try:
        statement = json.loads(content)["statement"]
        return str(statement["subject"]["artifact_path"]), str(statement["predicate"])
    except (ValueError, KeyError, TypeError):
        return None


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
    """The live alias rows one Claim version contributes: (target path, term).

    Read through the row function accepted vocabulary extraction uses
    (``typed_state.vocabulary_terms``), so the boundary's view of a change's
    aliases is exactly what the accepted generation will index.
    """

    from cruxible_client.contracts.claims import parse_claim
    from cruxible_core.indexes.typed_state import vocabulary_terms

    if content is None or b'"semantic.alias"' not in content:
        return ()
    return tuple(
        (target, term)
        for _identity, target, basis, term, _value in vocabulary_terms(
            parse_claim(content, path=path), path=path
        )
        if basis == "alias"
    )


@dataclass(frozen=True)
class RecordedCreation:
    """One creation member's actual assignment, as core recorded it when lowering it.

    ``key`` is the canonical key encoding (``key_encoding``; None for a minted
    kind), ``rule`` how the id was derived, ``outcome`` whether the member
    created a Subject or reused one, and ``subject_id`` the id it assigned or
    reused.
    """

    member: str
    kind: str
    key: str | None
    rule: Literal["first_holder", "further_holder", "minted"]
    outcome: Literal["created", "reused"]
    subject_id: str


SubjectAssignmentAuthority: TypeAlias = Callable[[str, RecordedCreation], bool]
"""Whether core recorded exactly this creation for one operation.

Called as ``(operation, creation)``, where ``creation`` is what the candidate
claims: its member, kind, canonical key, rule, ``created`` and id. It answers
True only when the durable record of that operation -- written by core when it
lowered an authoring intent, or when a Procedure terminal delivers -- holds an
equal ``created`` entry; a ``reused`` entry authorizes nothing. The candidate
boundary asks it for every minted or further-holder Subject a change creates, at
admission, readmission and settlement; the record does not change when a
proposal is rebased or readmitted, so neither does the answer.
"""


def _authoritative(
    authority: SubjectAssignmentAuthority | None,
    assignment: Any,
    creation: RecordedCreation,
) -> bool:
    if authority is None or assignment is None:
        return False
    return bool(authority(assignment.operation, creation))


def _unrecorded_assignment(path: str, kind: str, assignment: Any) -> SubjectIdentityIssue:
    return SubjectIdentityIssue(
        SUBJECT_ID_NOT_ASSIGNED,
        path,
        f"{_ref(path)} records that operation {assignment.operation!r} (member "
        f"{assignment.member!r}) created it, and core recorded no such creation of that "
        f"member: a {assignment.rule.replace('_', ' ')} {kind} is named only by core, with "
        "exactly the key and id it assigned when it lowered that operation. Create it "
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
            if (
                expected == subject_id
                and assignment is not None
                and not _authoritative(
                    assignment_authority,
                    assignment,
                    RecordedCreation(
                        member=assignment.member,
                        kind=kind,
                        key=None,
                        rule="minted",
                        outcome="created",
                        subject_id=subject_id,
                    ),
                )
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
    # A Subject the change retires may be a live pair's end, whatever its kind.
    retiring = any(
        path.startswith(_SUBJECTS)
        and path not in created_set
        and _live(current_tree, path)
        and not _live(candidate_tree, path)
        for path in scope
    )
    if not affected and not changed_kinds and not scope_claims and not scope_types and not retiring:
        return tuple(issues)

    with tree_index_connection(current_tree)() as connection:
        # Each touched Claim's Subject kind's key predicates, read by one indexed
        # seek per kind (the parent's SubjectKind overlaid with this change's, a
        # retired kind's included: a grandfathered pair's endpoints stay frozen),
        # never by enumerating the keyed kinds.
        kind_keys = _KindKeys(connection, candidate_tree, changed_kinds)
        # The key Claim versions the change touches, and the Subjects whose key they move.
        touched: dict[str, tuple[_KeyClaimRow | None, _KeyClaimRow | None]] = {}
        aliases_removed: set[tuple[str, str]] = set()
        aliases_added: set[tuple[str, str]] = set()
        for path in scope_claims:
            before_bytes, after_bytes = current_tree.get(path), candidate_tree.get(path)
            aliases_removed.update(_alias_rows(before_bytes, path))
            aliases_added.update(_alias_rows(after_bytes, path))
            rows = (
                kind_keys.key_claim_row(before_bytes, path),
                kind_keys.key_claim_row(after_bytes, path),
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
        subject_parts = _SubjectParts(current_tree, candidate_tree)
        issues.extend(
            _endpoint_issues(
                connection,
                touched=touched,
                created=created_set,
                current_tree=current_tree,
                candidate_tree=candidate_tree,
                scope=scope,
                subject_parts=subject_parts,
                touched_identities=touched_identities,
                kind_keys=kind_keys.keys,
            )
        )
        issues.extend(
            _endpoint_declaration_issues(
                connection,
                current_tree=current_tree,
                candidate_tree=candidate_tree,
                changed_kinds=changed_kinds,
            )
        )
        issues.extend(
            _ancestor_depth_issues(
                connection,
                current_tree=current_tree,
                candidate_tree=candidate_tree,
                scope=scope,
                changed_kinds=changed_kinds,
                declared=declared,
            )
        )

        # The change's live key-Claim values, grouped once by owner and predicate:
        # reading one owner's key part costs its own rows, not every touched Claim.
        touched_values: dict[tuple[str, str], list[SubjectKeyPartValue | None]] = {}
        for _before, after in touched.values():
            if after is not None and after.live:
                touched_values.setdefault((after.subject_path, after.predicate), []).append(
                    after.value
                )

        def values(subject: str, predicate: str) -> list[SubjectKeyPartValue | None]:
            found: list[SubjectKeyPartValue | None] = [
                value
                for identity, value in live_key_claims(connection, subject, predicate)
                if identity not in touched_identities
            ]
            found.extend(touched_values.get((subject, predicate), ()))
            return found

        keys_now: dict[str, PartKeyTuple] = {}
        for subject in sorted(affected):
            kind = affected[subject]
            declared_kind = declared(kind)
            if (
                declared_kind is None
                or not declared_kind.keys
                or not _live(candidate_tree, subject)
            ):
                continue
            parts: list[tuple[str, SubjectKeyPartValue]] = []
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
                            "integer, a boolean or (a Subject-valued part) a whole Subject.",
                        )
                    )
                    break
                if isinstance(found[0], SubjectEnd):
                    if subject in created_set and not _live(candidate_tree, found[0].path):
                        issues.append(
                            SubjectIdentityIssue(
                                SUBJECT_KEY_ENDPOINT_RETIRED,
                                subject,
                                f"{_ref(subject)} is a new {kind} whose key part {predicate} "
                                f"names {_ref(found[0].path)}, which is not live in this "
                                "change; a pair's ends are live Subjects.",
                                (_ref(found[0].path),),
                            )
                        )
                        break
                    parts.append((predicate, found[0]))
                    continue
                normalized = _normalizer_issue(subject, predicate, found[0], declared_kind)
                if normalized is not None:
                    issues.append(normalized)
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

        def holders(kind: str, key: PartKeyTuple) -> set[str]:
            return set(view.holders(kind, key))

        # Created keyed Subjects carry core's binding and take exactly the id it
        # derives, in the one creation order: ends' layers before their pairs',
        # then kind and canonical key (``creation_order``, which lowering shares).
        names: dict[str, set[str]] = {}
        first_taken: set[tuple[str, str]] = set()
        creating = [item for item in created if item in keys_now]
        layers = kind_depths(
            {subject_kind_of(item)[0] for item in creating},
            subject_kind_of=declared,
            key_shape_of=subject_parts.shape,
        )
        for subject in sorted(
            creating,
            key=lambda item: creation_order(
                layers.get(subject_kind_of(item)[0], 0),
                subject_kind_of(item)[0],
                keys_now[item],
                item,
            ),
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
                    assignment_authority,
                    assignment,
                    RecordedCreation(
                        member=assignment.member,
                        kind=kind,
                        key=key_encoding(key),
                        rule="further_holder",
                        outcome="created",
                        subject_id=subject_id,
                    ),
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
        # A changed normalizer re-keys the kind: every live key value it moves is
        # re-authored in this same change, never re-spelled silently.
        for kind in sorted(changed_kinds):
            declared_kind = declared(kind)
            previous = live_subject_kind(current_tree, kind)
            if declared_kind is not None and previous is not None:
                issues.extend(
                    _normalizer_rekey_issues(
                        connection,
                        kind,
                        declared_kind,
                        previous,
                        candidate_tree=candidate_tree,
                        values=values,
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
                    previous is not None
                    and previous.unique
                    and previous.identity_rule == declared_kind.identity_rule
                )
            ):
                continue
            issues.extend(_kind_wide_conflicts(connection, kind, declared_kind, keys_now, affected))
    return tuple(issues)


SUBJECT_KIND_KEY_BROKEN = "cruxible.subject_kind.key_claim_type_broken"
SUBJECT_KIND_REKEY_UNHANDLED = "cruxible.subject_kind.rekey_unhandled"
SUBJECT_KEY_NOT_CANONICAL = "cruxible.subject.key_not_canonical"


def _normalizer_issue(
    subject: str, predicate: str, value: SubjectKeyValue, declared_kind: SubjectKind
) -> SubjectIdentityIssue | None:
    """Why one live key value is not canonical under its part's normalizer, or None.

    Every route that names a Subject by key normalizes for the author; this is
    the one boundary that holds every other route (a revised key Claim, a raw
    proposal) to it.
    """

    rule = declared_kind.normalizer(predicate)
    try:
        canonical = normalize_key_value(rule, value)
    except KeyNormalizationRefused as exc:
        return SubjectIdentityIssue(
            SUBJECT_KEY_INVALID,
            subject,
            f"{_ref(subject)}'s key part {predicate} under {normalizer_spelling(rule)}: {exc}.",
        )
    if canonical == value:
        return None
    return SubjectIdentityIssue(
        SUBJECT_KEY_NOT_CANONICAL,
        subject,
        f"{_ref(subject)}'s key part {predicate} states {value!r}, and its canonical value "
        f"under {normalizer_spelling(rule)} is {canonical!r}. A key Claim states the "
        "canonical value; state that one (naming a Subject by key normalizes it for you).",
    )


def _normalizer_rekey_issues(
    connection: sqlite3.Connection,
    kind: str,
    declared_kind: SubjectKind,
    previous: SubjectKind,
    *,
    candidate_tree: Mapping[str, bytes],
    values: Callable[[str, str], list[SubjectKeyPartValue | None]],
) -> list[SubjectIdentityIssue]:
    """A SubjectKind successor that changes a key part's normalizer: an explicit re-key.

    Every live Subject of the kind must hold that part already in the new rule's
    canonical value in the candidate -- its key Claims re-authored in the same
    change -- or the change is refused (``rekey_unhandled``), naming the
    Subjects still to move. One pass over the kind's live Subjects: a
    governance event's cost, run only when a normalizer (or its version)
    changes. Two Subjects the new rule gives one key are then judged by the
    kind-wide uniqueness check (``key_conflict``).
    """

    from cruxible_client.contracts.subject_kinds import subject_kind_path

    changed = [
        predicate
        for predicate in declared_kind.keys
        if predicate in previous.keys
        and previous.normalizer(predicate) != declared_kind.normalizer(predicate)
    ]
    if not changed or declared_kind.lifecycle.state != "live":
        return []
    low, high = _kind_range(kind)
    subjects = [
        str(row[0])
        for row in connection.execute(
            "SELECT path FROM subjects WHERE path>=? AND path<? AND lifecycle='live'", (low, high)
        )
        if _live(candidate_tree, str(row[0]))
    ]
    issues: list[SubjectIdentityIssue] = []
    for predicate in changed:
        rule = declared_kind.normalizer(predicate)
        stale: list[str] = []
        for subject in subjects:
            for value in values(subject, predicate):
                if value is None or isinstance(value, SubjectEnd):
                    continue
                try:
                    fixed: object = normalize_key_value(rule, value)
                except KeyNormalizationRefused:
                    fixed = None
                if fixed != value:
                    stale.append(subject)
                    break
        if stale:
            issues.append(
                SubjectIdentityIssue(
                    SUBJECT_KIND_REKEY_UNHANDLED,
                    subject_kind_path(kind),
                    f"{kind} key {predicate} moves from "
                    f"{normalizer_spelling(previous.normalizer(predicate))} to "
                    f"{normalizer_spelling(rule)}: a re-keying. {len(stale)} live {kind} "
                    f"Subject(s) still hold a value the new rule re-spells "
                    f"({', '.join(_ref(item) for item in stale[:5])}"
                    f"{', ...' if len(stale) > 5 else ''}); re-author their key Claims to "
                    "the new canonical value in the same change (re_author), retire them, or "
                    "keep the rule.",
                    tuple(_ref(item) for item in stale),
                )
            )
    return issues


def key_claim_type_shape(tree: Mapping[str, bytes], predicate: str) -> KeyClaimTypeShape | None:
    """What the SubjectKind law reads of one key ClaimType in ``tree`` (None when absent)."""

    from cruxible_client.contracts.claim_types import (
        ClaimTypeFormatError,
        claim_type_path,
        parse_claim_type,
    )

    try:
        path = claim_type_path(predicate)
    except ClaimTypeFormatError:
        return None
    content = tree.get(path)
    if content is None:
        return None
    return claim_type_key_shape(parse_claim_type(content, path=path))


def claim_type_key_shape(claim_type: ClaimType) -> KeyClaimTypeShape:
    """What the SubjectKind law (and the creation layers) read of one key ClaimType."""

    from cruxible_client.contracts.subject_kinds import KeyClaimTypeShape

    predicate = claim_type.predicate
    schema_type = value_type_name(claim_type.value)
    return KeyClaimTypeShape(
        predicate=predicate,
        live=claim_type.lifecycle.state == "live",
        artifact_format=claim_type.artifact_format,
        object_kind=claim_value_kind(claim_type.value),
        cardinality=claim_type.cardinality,
        allowed_subject_kinds=tuple(claim_type.allowed_subject_kinds),
        literal_type=schema_type if isinstance(schema_type, str) else None,
        canonical_form=tuple(effective_canonical_form(claim_type)),
        object_subject_kinds=tuple(subject_value_kinds(claim_type.value)),
    )


def _key_definition_issues(
    connection: sqlite3.Connection,
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
    declared: Callable[[str], SubjectKind | None],
    changed_kinds: set[str],
    values: Callable[[str, str], list[SubjectKeyPartValue | None]],
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
    issues: list[SubjectIdentityIssue] = _pair_key_type_issues(
        connection, changed=changed, changed_kinds=changed_kinds, candidate_tree=candidate_tree
    )
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
    for kind, keys in sorted(keyed.items()):
        kind_path = subject_kind_path(kind)
        for predicate in keys:
            if predicate not in changed:
                continue
            old, new = changed[predicate]
            if claim_value_kind(old.value) == "subject" or (
                new is not None and claim_value_kind(new.value) == "subject"
            ):
                continue  # a pair kind's endpoint: ``_pair_key_type_issues`` judges it
            declared_kind = declared(kind)
            if kind not in changed_kinds:
                problem = key_claim_type_problem(
                    kind,
                    key_claim_type_shape(candidate_tree, predicate),
                    predicate,
                    None if declared_kind is None else declared_kind.normalizer(predicate),
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
            normalizer = (
                NO_NORMALIZER if declared_kind is None else declared_kind.normalizer(predicate)
            )
            for subject in subjects:
                for value in values(subject, predicate):
                    if value is None or isinstance(value, SubjectEnd):
                        continue
                    try:
                        fixed: object = canonical_key_value(
                            value, new, normalizer, predicate=predicate
                        )
                    except SubjectKeyInvalid:
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
                parts: list[tuple[str, SubjectKeyPartValue]] = []
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


def _pair_key_type_issues(
    connection: sqlite3.Connection,
    *,
    changed: Mapping[str, tuple[ClaimType, ClaimType | None]],
    changed_kinds: set[str],
    candidate_tree: Mapping[str, bytes],
) -> list[SubjectIdentityIssue]:
    """A pair kind's endpoint ClaimTypes are frozen with it (R13, F1).

    A key ClaimType that is a Subject-valued part of any kind, live or retired,
    is succeeded only with the same value type ``subject<K>``, cardinality one
    and about that kind only, and never retired while the kind exists -- even
    when the same change succeeds the SubjectKind. A literal key ClaimType of a
    live kind becomes Subject-valued only beside a SubjectKind successor, which
    the boundary judges (``endpoint_declaration_fixed``), and never while the
    kind holds a Subject. That conversion guard reads the key as the change
    leaves it: a successor that drops the predicate from its key (say, to
    minted) makes no pair kind, so the ClaimType may become an ordinary link. A
    key ClaimType is about its kind only, so the kinds it may key are its own
    allowed kinds: one indexed read each.
    """

    from cruxible_client.contracts.subject_kinds import subject_kind_path

    issues: list[SubjectIdentityIssue] = []
    for predicate, (old, new) in sorted(changed.items()):
        candidates = {
            *old.allowed_subject_kinds,
            *(() if new is None else new.allowed_subject_kinds),
        }
        keyed = sorted(
            kind for kind in candidates if predicate in _parent_kind_keys(connection, kind)
        )
        for kind in keyed:
            if claim_value_kind(old.value) == "subject":
                kept = (
                    new is not None
                    and new.lifecycle.state == "live"
                    and claim_value_kind(new.value) == "subject"
                    and tuple(subject_value_kinds(new.value))
                    == tuple(subject_value_kinds(old.value))
                    and new.cardinality == "one"
                    and tuple(new.allowed_subject_kinds) == (kind,)
                )
                if not kept:
                    ends = ", ".join(subject_value_kinds(old.value))
                    issues.append(
                        SubjectIdentityIssue(
                            SUBJECT_KIND_KEY_BROKEN,
                            subject_kind_path(kind),
                            f"{predicate} is a Subject-valued key part of the pair kind {kind}, "
                            f"whose endpoints are frozen for life: its successor keeps value "
                            f"subject<{ends}>, cardinality one and about {kind} only, and it is "
                            f"never retired while {kind} exists. A new pair identity is a new "
                            "kind.",
                        )
                    )
            elif new is not None and claim_value_kind(new.value) == "subject":
                # Literal -> Subject: the kind would become a pair kind -- if
                # the change leaves the predicate in its key. Never while it
                # holds a Subject (live or retired, its kind live or retired: a
                # full build would make those pairs without endpoints), and
                # only beside a SubjectKind successor, whose law and boundary
                # judge the pair kind.
                if kind in changed_kinds:
                    successor = _any_subject_kind(candidate_tree, kind)
                    if successor is None or predicate not in successor.keys:
                        continue
                if _kind_has_subjects(connection, kind):
                    issues.append(_not_yet_a_pair_kind(kind))
                    continue
                if kind in changed_kinds:
                    continue
                issues.append(
                    SubjectIdentityIssue(
                        SUBJECT_KIND_KEY_BROKEN,
                        subject_kind_path(kind),
                        f"{predicate} keys {kind} as a literal, and this change makes it name a "
                        f"Subject: that turns {kind} into a pair kind. Declare the pair as a new "
                        "kind (a kind becomes a pair kind only while it has no Subject), or keep "
                        "the key literal.",
                    )
                )
    return issues


def _kind_wide_conflicts(
    connection: sqlite3.Connection,
    kind: str,
    declared_kind: SubjectKind,
    keys_now: Mapping[str, PartKeyTuple],
    affected: Mapping[str, str],
) -> list[SubjectIdentityIssue]:
    """Every pair of live Subjects of ``kind`` that hold one key, once the kind is unique.

    One pass over the kind's live Subjects under the new keys (a governance
    event's cost: it runs only when a SubjectKind becomes unique or changes its
    keys), overlaid with the Subjects the change itself moves.
    """

    low, high = _kind_range(kind)
    tuples: dict[str, PartKeyTuple] = {}
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
    "SUBJECT_ID_NOT_ASSIGNED",
    "RecordedCreation",
    "SubjectAssignmentAuthority",
    "SUBJECT_KIND_KEY_BROKEN",
    "SUBJECT_KIND_REKEY_UNHANDLED",
    "SUBJECT_KEY_NOT_CANONICAL",
    "KeyRules",
    "key_normalizer_of",
    "tree_subject_kind_reader",
    "SingleKey",
    "tree_key_rules",
    "canonical_key_value",
    "key_claim_type_shape",
    "SUBJECT_KEY_CONFLICT",
    "SUBJECT_KEY_CONTENDED",
    "SUBJECT_KEY_INVALID",
    "SUBJECT_KEY_MISSING",
    "KeyTuple",
    "claim_type_key_shape",
    "PartKeyTuple",
    "PendingEnd",
    "PendingKey",
    "creation_order",
    "kind_depths",
    "spell_key",
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
    "single_key_value",
    "subject_creation_shape",
    "write_create_change_shape",
    "subject_key_message",
    "subjects_with_key",
    "SUBJECT_KIND_UNDECLARED",
    "SubjectIdentityIssue",
    "candidate_identity_issues",
    "created_subjects",
    "subject_kind_of",
    "undeclared_kind_message",
    "undeclared_kind_repair",
]
