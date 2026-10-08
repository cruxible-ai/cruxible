"""Typed, attribute-addressed access to one accepted world.

Every authoring surface below `Cruxible` still speaks strings: a predicate is a
dotted name, a Subject is a `kind/id` shorthand, and a literal is whatever the
caller typed. That is exactly the ergonomics markdown already has, so the SDK
gave up its one structural advantage -- it knows the accepted ontology and can
hand it back as objects with fields. `cx.world()` reads the accepted ClaimType
vocabulary once and exposes it as a tree: kinds nest, Subjects answer by
attribute or index, predicates carry their own structure and their own
admissible values, and every ref it mints carries the coordinate it was read at.

The world is a READ, never an authority. It refuses once the connection's
orientation moves, under the same law as every other typed ref, because a name
that resolved at one coordinate may name something else at the next.

    w = cx.world()

    vulnerability = w.sec.vulnerability["cve-2026-69247"]
    vulnerability.severity                      # live Claims under that predicate
    w.sec.vuln.severity.cardinality             # the ClaimType's own structure

    draft = cx.changes(rationale="Name the package this advisory affects.")
    package = draft.subject(w.sec.package.define("click"))
    draft.claim(
        subject=vulnerability,
        predicate=w.sec.vuln.affects_package,
        value=package,                          # the same set defines it
        role="observation",
        rationale="The advisory names this package.",
        self_source="affects: click\n",
        supported_by=None, copied_from=None, qualifier=None,
        effective_period=None, revises=None, dispositions={},
        subject_definition=None, claim_type_definition=None,
    )
    intent = draft.prepare()
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from cruxible_client.authoring.sdk_types import (
    AbsentSubject,
    Cardinality,
    ClaimObjectKind,
    ClaimRole,
    ClaimTypeRef,
    LiteralSchemaError,
    LiteralValue,
    ReferentSensitivity,
    SdkError,
    SubjectRef,
)
from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.claim_type_structure import (
    ClaimTypeStructure,
    check_claim_type_structure,
)
from cruxible_client.contracts.claim_types import (
    ClaimTypeMemberDescription,
    EvidenceRequirement,
    RevisionEvidence,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.records import RecordConstructor

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_client.authoring.compact_query import CompactQuery
    from cruxible_client.authoring.sdk import ClaimView, Cruxible, SubjectDraft
    from cruxible_client.contracts.compact_query import QueryClaimValue
    from cruxible_client.contracts.write import (
        Change,
        Evidence,
        WriteAccept,
        WriteOutcome,
        WriteRetireReason,
        WriteRole,
    )

_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _write_value(value: object) -> Any:
    if isinstance(value, SubjectRef):
        return value.address
    if isinstance(value, LiteralValue):
        return value.value
    return value


class WorldStructureError(SdkError):
    """The world cannot answer this name at the shape it was asked for."""

    code = "cruxible.sdk.world_structure_refused"


class Names(tuple[str, ...]):
    """A byte-sorted tuple of names that also answers when called.

    ``w.kinds`` and ``w.kinds()`` are the same tuple, so neither spelling is a
    wrong guess. Next: ``w.kind(name)`` or ``w.claim_type(name)`` for one of
    them, or ``w.describe()`` for the whole vocabulary.
    """

    __slots__ = ()

    def __call__(self) -> tuple[str, ...]:
        """Return the names themselves. Next: ``w.kind(name)`` for one of them."""

        return tuple(self)


def _is_identifier(value: str) -> bool:
    """Return whether this ID can be spelled as a Python attribute."""

    return value.isidentifier() and not keyword.iskeyword(value)


# One law for every collision in this module: the fixed surface wins on
# attribute access, and index access reaches the discovered name. `w.sec.package`
# is the accepted structure even when a Subject is called `package`;
# `subject.claims` is every Claim even when a predicate leaf is called `claims`;
# `severity.cardinality` is the ClaimType's structure even when an enum member
# is called `cardinality`. Each of those names stays reachable -- by index, or
# by the ClaimType's call form -- and `__dir__` and the generated stub advertise
# only what attribute access really answers.
CLAIM_TYPE_MEMBERS = frozenset(
    {
        "address",
        "allowed_object_subject_kinds",
        "allowed_subject_kinds",
        "as_kind",
        "cardinality",
        "coordinate",
        "default_role",
        "description",
        "evidence_requirement",
        "kind",
        "literal_schema",
        "member_descriptions",
        "members",
        "object_kind",
        "permitted_roles",
        "predicate",
        "referent_sensitivity",
        "revision_evidence",
        "value",
    }
)

SUBJECT_MEMBERS = frozenset(
    {
        "add",
        "address",
        "claims",
        "coordinate",
        "explain",
        "kind",
        "retire",
        "set",
        "subject_id",
        "subject_kind",
    }
)


# ---------------------------------------------------------------------------
# Literal schema admission
# ---------------------------------------------------------------------------

_TYPE_CHECKS: Mapping[str, tuple[type, ...]] = {
    "array": (list, tuple),
    "boolean": (bool,),
    "integer": (int,),
    "null": (type(None),),
    "number": (int, float),
    "object": (dict,),
    "string": (str,),
}


def literal_schema_members(schema: Mapping[str, object] | None) -> tuple[str, ...]:
    """Return the string enum members a literal schema names, in schema order."""

    if schema is None:
        return ()
    members = schema.get("enum")
    if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
        return ()
    return tuple(item for item in members if isinstance(item, str))


def _refuse(predicate: str, reason: str) -> LiteralSchemaError:
    return LiteralSchemaError(predicate=predicate, reason=reason)


def admit_literal(
    value: object,
    *,
    predicate: str,
    schema: Mapping[str, object] | None,
) -> CanonicalValue:
    """Admit one value against a ClaimType's declared literal schema.

    This is a pre-wire read of the schema the ClaimType already publishes, over
    the keywords `ClaimTypeStructure` admits plus the exact string and numeric
    bounds. It is deliberately not a general JSON Schema implementation: an
    unrecognised keyword is left to the daemon, which stays the only authority
    on admission. What it buys is the round trip -- a mistyped enum member or a
    digest that is 39 hex characters refuses here, naming the predicate, rather
    than after a proposal.
    """

    canonical = normalize_canonical(value)
    if schema is None:
        return canonical
    declared = schema.get("type")
    if isinstance(declared, str):
        admissible = _TYPE_CHECKS.get(declared)
        if admissible is None:
            raise _refuse(predicate, f"declared type {declared!r} is not an exact Cruxible type")
        if declared != "boolean" and isinstance(canonical, bool):
            raise _refuse(predicate, f"a boolean is not {declared}")
        if not isinstance(canonical, admissible):
            raise _refuse(predicate, f"value is not {declared}")
    if "const" in schema and canonical != schema["const"]:
        raise _refuse(predicate, f"value is not the declared const {schema['const']!r}")
    members = schema.get("enum")
    if isinstance(members, Sequence) and not isinstance(members, (str, bytes)):
        if canonical not in tuple(members):
            spelled = ", ".join(repr(item) for item in members)
            raise _refuse(predicate, f"value is outside the declared enum: {spelled}")
    if isinstance(canonical, str):
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, canonical) is None:
            raise _refuse(predicate, f"value does not match the declared pattern {pattern!r}")
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(canonical) < minimum_length:
            raise _refuse(predicate, f"value is shorter than the declared {minimum_length}")
        maximum_length = schema.get("maxLength")
        if isinstance(maximum_length, int) and len(canonical) > maximum_length:
            raise _refuse(predicate, f"value is longer than the declared {maximum_length}")
    if isinstance(canonical, (int, float)) and not isinstance(canonical, bool):
        admits: Mapping[str, Callable[[float, float], bool]] = {
            "minimum": lambda value, bound: value >= bound,
            "maximum": lambda value, bound: value <= bound,
            "exclusiveMinimum": lambda value, bound: value > bound,
            "exclusiveMaximum": lambda value, bound: value < bound,
        }
        for keyword_name, within in admits.items():
            bound = schema.get(keyword_name)
            if isinstance(bound, (int, float)) and not within(canonical, bound):
                raise _refuse(predicate, f"value violates the declared {keyword_name} {bound!r}")
    return canonical


# ---------------------------------------------------------------------------
# The world tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Node:
    """One dotted name in the world, which may be a kind, a predicate, or both."""

    path: str
    children: dict[str, _Node] = field(default_factory=dict)
    subject_kind: bool = False
    structure: ClaimTypeStructure | None = None
    meaning: _Meaning | None = None


@dataclass(frozen=True)
class _Meaning:
    """What a predicate means and how its Claims are backed (ClaimType v7).

    A ClaimType before v7 has no description, no default role, requirement
    ``self`` and revision evidence ``accumulate``: the meaning it always had.
    """

    description: str | None = None
    member_descriptions: tuple[ClaimTypeMemberDescription, ...] = ()
    default_role: ClaimRole | None = None
    evidence_requirement: EvidenceRequirement = "self"
    revision_evidence: RevisionEvidence = "accumulate"


@dataclass(frozen=True, repr=False)
class WorldClaimType(ClaimTypeRef):
    """One accepted predicate, carrying its structure and its admissible values.

    Passing this where a predicate is wanted works exactly as a `ClaimTypeRef`
    does, because it is one. What it adds is the read every caller otherwise
    makes by hand: what the ClaimType admits as an object, at what cardinality,
    for which roles, and -- for a literal schema with an enum -- each member as
    a typed value that can only state a Claim under this predicate.
    """

    object_kind: ClaimObjectKind
    cardinality: Cardinality
    allowed_subject_kinds: tuple[str, ...]
    allowed_object_subject_kinds: tuple[str, ...]
    permitted_roles: tuple[ClaimRole, ...]
    referent_sensitivity: ReferentSensitivity
    literal_schema: dict[str, object] | None
    #: What the predicate means (ClaimType v7), and what each enum member means.
    description: str | None
    member_descriptions: tuple[ClaimTypeMemberDescription, ...]
    #: The role a write takes when it names none.
    default_role: ClaimRole | None
    #: What backs a Claim: ``none``, ``self`` (before v7) or ``captured``.
    evidence_requirement: EvidenceRequirement
    #: What a statement-changing revision keeps: ``replace`` or ``accumulate`` (before v7).
    revision_evidence: RevisionEvidence
    _world: World = field(repr=False, compare=False)
    _node: _Node = field(repr=False, compare=False)

    @property
    def predicate(self) -> str:
        """The full dotted predicate. Next: ``cx.query(kind, select=[leaf])`` for its values."""

        return self.address

    @property
    def members(self) -> tuple[str, ...]:
        """Return the enum members this predicate's literal schema names.

        Next: ``claim_type.<member>`` or ``claim_type("<member>")`` for a typed value.
        """

        return literal_schema_members(self.literal_schema)

    @property
    def as_kind(self) -> KindNamespace:
        """Reach the Subject kind this dotted name also names.

        A ClaimType wins attribute access over a Subject kind of the same dotted
        name, which would otherwise leave `define()` and `subject_ids`
        unreachable. This is that escape.

        Next: ``claim_type.as_kind["<id>"]``.
        """

        self._world._assert_current()
        if not self._node.subject_kind:
            raise WorldStructureError(
                f"{self.address!r} is an accepted predicate but not an accepted "
                "Subject kind, so it names no Subjects"
            )
        return KindNamespace(self._world, self._node)

    def __call__(self, value: object) -> LiteralValue:
        """Mint one literal object for this predicate, admitted before the wire."""

        self._world._assert_current()
        if self.object_kind is not ClaimObjectKind.LITERAL:
            raise WorldStructureError(
                f"ClaimType {self.address!r} takes a {self.object_kind.value} object, "
                "so it has no literal values to construct"
            )
        return LiteralValue(
            predicate=self.address,
            value=admit_literal(value, predicate=self.address, schema=self.literal_schema),
            coordinate=self.coordinate,
        )

    def value(self, **fields: object) -> LiteralValue:
        """Construct a structured literal using this accepted ClaimType's fields.

        Next: pass it as ``value=`` to a write.
        """
        if self.literal_schema is None:
            raise WorldStructureError(f"{self.address!r} has no declared record schema")
        record = RecordConstructor.from_json_schema(self.literal_schema)(**fields)
        return self(record)

    def __getitem__(self, subject_id: str) -> WorldSubject:
        """Read a Subject when this dotted name is also an accepted kind."""

        return KindNamespace(self._world, self._node)[subject_id]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        child = self._node.children.get(name)
        if child is not None:
            return self._world._materialize(child)
        members = self.members
        if name in members:
            return self(name)
        if self._node.subject_kind:
            raise AttributeError(
                f"{self.address!r} is an accepted predicate and an accepted Subject "
                f"kind, and the predicate wins attribute access, so it has no {name!r}; "
                f"reach the kind with {self.address.rsplit('.', 1)[-1]}.as_kind and a "
                f"Subject with {self.address.rsplit('.', 1)[-1]}[{name!r}]"
            )
        if members:
            spelled = ", ".join(sorted(members))
            raise AttributeError(
                f"{self.address!r} names no enum member {name!r}; its literal schema "
                f"admits: {spelled}"
            )
        raise AttributeError(
            f"{self.address!r} declares no enum in its literal schema, so it has no "
            f"member {name!r}; construct a value with {self.address.rsplit('.', 1)[-1]}(...)"
        )

    def __dir__(self) -> list[str]:
        # A member named like one of this class's own fields is shadowed by the
        # field. Advertising it would promise an attribute read that answers the
        # structure instead; the call form `severity('cardinality')` mints it.
        reachable = (member for member in self.members if member not in CLAIM_TYPE_MEMBERS)
        return sorted({*super().__dir__(), *self._node.children, *reachable})


@dataclass(frozen=True, repr=False)
class WorldSubject(SubjectRef):
    """One accepted Subject, readable through the verbs that already serve it.

    A predicate's last segment answers as an attribute -- `vulnerability.severity`
    is the live Claims under `sec.vuln.severity`. A leaf that collides with one
    of this class's own names (`claims`, `explain`, `address`, `coordinate`,
    `kind`, `subject_kind`, `subject_id`) is shadowed by the member, so it is
    reachable only by index: `vulnerability["sec.vuln.claims"]`, which also takes
    a bare leaf and a `ClaimTypeRef`. `__dir__` advertises only the leaves
    attribute access really answers.
    """

    _world: World = field(repr=False, compare=False)

    @property
    def subject_kind(self) -> str:
        """The Subject's kind. Next: ``w.kind(subject.subject_kind)``."""

        return self.address.split("/", 1)[0]

    @property
    def subject_id(self) -> str:
        """The Subject's ID. Next: ``cx.get(subject)`` for its fields and flags."""

        return self.address.split("/", 1)[1]

    @property
    def claims(self) -> tuple[ClaimView, ...]:
        """Every live Claim this Subject is the subject of.

        Next: ``cx.get(view.claim_id, detail="evidence")`` for what backs one.
        """

        return self._world._claims_about(self.address)

    def explain(self) -> object:
        """Read this Subject's governance and provenance context.

        Next: Cruxible.orient() to map state, Cruxible.query() for rows, or Cruxible.get().
        """

        self._world._assert_current()
        return self._world._playbill._get(self.address, "why", None, self.coordinate).why

    def set(
        self,
        /,
        *,
        because: str,
        evidence: Evidence | None = None,
        role: WriteRole | None = None,
        contend: bool = False,
        dry_run: bool = False,
        accept: WriteAccept = "if_allowed",
        **fields: object,
    ) -> WriteOutcome:
        """Set single-value fields of this Subject, by leaf: ``set(status="done", because=...)``.

        Every field set here is one change of one change set. The names are
        checked against this World before the wire; a leaf that is a Python
        keyword takes one trailing underscore (``class_``). References from this
        World stay valid after it: the next write is checked from this World's
        coordinate plus its own writes, so only a slot someone else moved refuses.

        Next: ``outcome.next`` for what is still needed; ``cx.get(subject)`` reads it back.
        """

        from cruxible_client.contracts.write import SetChange

        changes = [
            SetChange(
                subject=self.address,
                field=self._world._write_field(self.subject_kind, name),
                value=_write_value(value),
                evidence=evidence,
                role=role,
                contend=contend,
            )
            for name, value in fields.items()
        ]
        return self._world._write(changes, because=because, dry_run=dry_run, accept=accept)

    def add(
        self,
        /,
        *,
        because: str,
        evidence: Evidence | None = None,
        role: WriteRole | None = None,
        expect_absent: bool = False,
        dry_run: bool = False,
        accept: WriteAccept = "if_allowed",
        **fields: object,
    ) -> WriteOutcome:
        """Add one more value to many-valued fields of this Subject, by leaf.

        A value already there is answered as done; ``expect_absent=True`` refuses
        it instead (``cruxible.write.value_already_present``).

        Next: ``outcome.next`` for what is still needed.
        """

        from cruxible_client.contracts.write import AddChange

        changes = [
            AddChange(
                subject=self.address,
                field=self._world._write_field(self.subject_kind, name),
                value=_write_value(value),
                evidence=evidence,
                role=role,
                expect_absent=expect_absent,
            )
            for name, value in fields.items()
        ]
        return self._world._write(changes, because=because, dry_run=dry_run, accept=accept)

    def retire(
        self,
        field: str | ClaimTypeRef,
        /,
        *,
        because: str,
        reason: WriteRetireReason = "was-rescinded",
        dry_run: bool = False,
        accept: WriteAccept = "if_allowed",
    ) -> WriteOutcome:
        """Retire the one live value of a field of this Subject.

        Next: ``outcome.next``; ``cx.get(subject)`` shows the field without it.
        """

        from cruxible_client.contracts.write import RetireChange, SlotRef

        name = field.address if isinstance(field, ClaimTypeRef) else field
        predicate = (
            name
            if "." in name and self._world._node_at(name) is not None
            else self._world._write_field(self.subject_kind, name)
        )
        change = RetireChange(target=SlotRef(subject=self.address, field=predicate), reason=reason)
        return self._world._write([change], because=because, dry_run=dry_run, accept=accept)

    def __getitem__(self, predicate: str | ClaimTypeRef) -> tuple[ClaimView, ...]:
        """Read the live Claims under one predicate, named in full or by leaf."""

        name = predicate.address if isinstance(predicate, ClaimTypeRef) else predicate
        kind = self.address.split("/", 1)[0]
        resolved = (
            self._world.claim_type(name)
            if "." in name and self._world._node_at(name) is not None
            else self._world._predicate_for(kind, name)
        )
        return self._world._claims_about(self.address, predicate=resolved.address)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in SUBJECT_MEMBERS:
            # Reaching here for one of this class's OWN names means the member
            # ran and raised: Python routes an AttributeError escaping a
            # property or a method back into __getattr__, which would then
            # report a real fault -- a mis-built contract object deep inside a
            # Claim read -- as "no accepted predicate 'claims' is admitted for
            # this Subject kind". A naming mistake and a broken read would look
            # identical, and only one of them is the caller's to fix.
            raise AttributeError(
                f"reading {name!r} on Subject {self.address!r} failed inside the member "
                f"itself; {name!r} is one of this Subject's own names, not a predicate"
            )
        predicate = self._world._predicate_for(self.address.split("/", 1)[0], name)
        return self._world._claims_about(self.address, predicate=predicate.address)

    def __dir__(self) -> list[str]:
        leaves = self._world._predicate_leaves(self.address.split("/", 1)[0])
        return sorted({*super().__dir__(), *leaves})


class KindNamespace:
    """One dotted name in the world: a Subject kind, a prefix, or both.

    Attribute access resolves the world's own structure first -- a nested kind
    or a predicate -- and only then a Subject ID, because structure is what the
    world is for and a Subject named `severity` must not shadow the predicate.
    Index access always means a Subject ID, which is also how an ID that is not
    a Python identifier is spelled.

    Next: ``kind["<id>"]`` for a Subject, ``kind.where(...)`` for a query,
    ``kind.define("<id>")`` for a new one.
    """

    __slots__ = ("_node", "_world")

    def __init__(self, world: World, node: _Node) -> None:
        self._world = world
        self._node = node

    @property
    def subject_kind(self) -> str | None:
        """Return this namespace's Subject kind, or None if it is only a prefix.

        Next: ``kind.subject_ids``.
        """

        return self._node.path if self._node.subject_kind else None

    @property
    def subject_ids(self) -> tuple[str, ...]:
        """Return every accepted Subject ID of this kind, loading them on first ask.

        Next: ``kind["<id>"]`` for one Subject.
        """

        return tuple(self._subjects())

    def define(self, subject_id: str) -> SubjectDraft:
        """Draft one new Subject of this kind for a changeset to define.

        Next: ``draft.submit()``; a write that names a new Subject of this kind adds it too.
        """

        kind = self._require_kind()
        self._world._assert_current()
        from cruxible_client.contracts.artifacts import ArtifactLifecycle

        return self._world._playbill.subject(
            subject=f"{kind}/{subject_id}",
            pins=(),
            lifecycle=ArtifactLifecycle(),
        )

    def where(self, /, **filters: object) -> CompactQuery:
        """Start a compact query over this kind, filtered all-of by keyword.

        ``where(adoption_state="adopted", implementation_state__ne="completed")``;
        suffixes ``__ne``, ``__lt``, ``__lte``, ``__gt``, ``__gte``, ``__in``,
        ``__exists`` and ``__contains`` pick the operator. A leaf that is
        ``self``, a Python keyword, contains ``__`` or ends in ``_`` takes one
        trailing underscore before any suffix (``self_``, ``class___ne``).
        Names and enum values are checked against this World before the wire.

        Next: ``.select(...)``, then ``.run()`` or iterate it.
        """

        return self._query().where(**filters)

    def select(self, *fields: str) -> CompactQuery:
        """Start a compact query over this kind that shows only these columns.

        Next: ``.run()`` or iterate it.
        """

        return self._query().select(*fields)

    def _query(self) -> CompactQuery:
        from cruxible_client.authoring.compact_query import CompactQuery

        kind = self._require_kind()
        self._world._assert_current()
        return CompactQuery(self._world, kind)

    def _require_kind(self) -> str:
        if not self._node.subject_kind:
            spelled = ", ".join(sorted(self._node.children)) or "nothing"
            raise WorldStructureError(
                f"{self._node.path!r} is not an accepted Subject kind; it only nests: {spelled}"
            )
        return self._node.path

    def _subjects(self) -> Mapping[str, WorldSubject]:
        return self._world._subjects_of(self._require_kind())

    def __getitem__(self, subject_id: str) -> WorldSubject:
        kind = self._require_kind()
        found = self._subjects().get(subject_id)
        if found is None:
            raise AbsentSubject(
                subject_kind=kind,
                subject_id=subject_id,
                coordinate=self._world.coordinate,
            )
        return found

    def __contains__(self, subject_id: object) -> bool:
        return isinstance(subject_id, str) and subject_id in self._subjects()

    def __iter__(self) -> Iterator[WorldSubject]:
        return iter(self._subjects().values())

    def __len__(self) -> int:
        return len(self._subjects())

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        child = self._node.children.get(name)
        if child is not None:
            return self._world._materialize(child)
        if not self._node.subject_kind:
            spelled = ", ".join(sorted(self._node.children)) or "nothing"
            raise AttributeError(
                f"{self._node.path!r} is not an accepted Subject kind and nests no "
                f"{name!r}; it nests: {spelled}"
            )
        return self[name]

    def __dir__(self) -> list[str]:
        names = {*super().__dir__(), *self._node.children}
        if self._node.subject_kind:
            names.update(item for item in self._subjects() if _is_identifier(item))
        return sorted(names)

    def __repr__(self) -> str:
        shape = "kind" if self._node.subject_kind else "namespace"
        return f"<KindNamespace {self._node.path!r} ({shape})>"


#: The verbs ``World.describe()`` names, as (call, what it does).
_DESCRIBED_VERBS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Read:",
        (
            ("cx.orient()", "map accepted state; orient(kind=...) for one kind"),
            ('cx.query("<kind>", where=[...])', "one page of values with verdict flags"),
            ('cx.get("<ref>")', "one thing: kind/id, CLM-..., a predicate, CAP-..."),
            ("grep -r <text> .cruxible/floor/current/", "the exported floor, one file per Subject"),
            ('w.<kind>["<id>"].<field>', "the live Claims under one field"),
            ("w.<kind>.where(<field>=...)", "a compact query over one kind"),
        ),
    ),
    (
        "Write:",
        (
            ('w.<kind>["<id>"].set(<field>=...)', "replace a single value (because=...)"),
            ('w.<kind>["<id>"].add(<field>=...)', "add to a many-valued field"),
            ('w.<kind>["<id>"].retire("<field>")', "end the live value"),
            ('cx.changes(because="...")', "several changes as one change set"),
        ),
    ),
    ("Then:", (("cx.next(expiring_within=...)", "what needs attention"),)),
)


class World:
    """The accepted ontology of one instance, as objects rather than strings.

    Every name here answers at exactly one coordinate through a borrowed pinned
    context. Moving the live client's head does not move or invalidate this
    World. Vocabulary, lazy reads, pagination and caches share its coordinate.

    `kind()` and `claim_type()` are the escapes for a dotted name attribute
    access cannot spell: a Python keyword segment, or a kind a predicate of the
    same name wins.

    Next: ``print(w.describe())`` for every verb and field, ``w.kinds`` for the kinds,
    ``w.<kind>["<id>"]`` for one Subject.
    """

    __slots__ = (
        "_claim_cache",
        "_coordinate",
        "_write_basis",
        "_playbill",
        "_root",
        "_row_cache",
        "_subject_cache",
        "_subjects_loaded",
        "_view_cache",
        "unstructured_predicates",
    )

    def __init__(
        self,
        cx: Cruxible,
        *,
        coordinate: AcceptedCoordinate,
        root: _Node,
        unstructured_predicates: tuple[str, ...],
    ) -> None:
        self._playbill = cx
        self._coordinate = coordinate
        self._root = root
        self._subject_cache: dict[str, dict[str, WorldSubject]] = {}
        self._subjects_loaded = False
        self._row_cache: dict[str, tuple[Mapping[str, object], ...]] = {}
        self._claim_cache: dict[tuple[str, str | None], tuple[ClaimView, ...]] = {}
        self._view_cache: dict[str, ClaimView] = {}
        self.unstructured_predicates = unstructured_predicates
        # The coordinate this World's writes are checked from: its own, advanced
        # past each of its own accepted writes while no one else wrote between.
        self._write_basis = coordinate.git_oid

    @property
    def coordinate(self) -> AcceptedCoordinate:
        """The accepted coordinate every name in this World answers at.

        Next: ``cx.at(w.coordinate)`` for other reads at the same state.
        """

        return self._coordinate

    @property
    def kinds(self) -> Names:
        """Every accepted Subject kind this world knows, byte-sorted.

        An attribute and a call alike: ``w.kinds`` or ``w.kinds()``. Next:
        ``w.kind("dev.batch")`` (or ``w.dev.batch``) for one kind, or
        ``cx.orient(kind=...)`` for its counts and sample Subjects.
        """

        self._assert_current()
        return Names(self._kind_paths())

    @property
    def predicates(self) -> Names:
        """Every accepted predicate this world knows, byte-sorted.

        An attribute and a call alike: ``w.predicates`` or ``w.predicates()``.
        Next: ``w.claim_type(predicate)`` for one predicate's structure.
        """

        self._assert_current()
        return Names(self._predicate_paths())

    def describe(self) -> str:
        """Name the verbs that act on this world, then its vocabulary, as text to read.

        Each Subject kind is listed with its fields: the leaf a Subject answers
        by attribute, then what the field holds (``literal``, ``-> kind`` for
        a Subject, ``exact_content``), its cardinality, enum members and
        description. Next: ``print(w.describe())``, then one of the verbs it
        lists -- ``cx.query(kind, ...)`` to read values, ``cx.get(ref)`` for
        one thing, ``w.<kind>[id].set(...)`` to write.
        """

        self._assert_current()
        lines = [
            f"World at {self._coordinate.git_oid[:12]}: "
            f"{len(self._kind_paths())} Subject kinds, {len(self._predicate_paths())} predicates.",
        ]
        for heading, verbs in _DESCRIBED_VERBS:
            lines.append(heading)
            lines.extend(f"  {call:<40} {what}" for call, what in verbs)
        lines.append("Vocabulary:")
        for kind in self._kind_paths():
            lines.append(f"  {kind}")
            for leaf, names in sorted(self._leaf_map(kind).items()):
                # A leaf two predicates share is reachable only by full name.
                for name in names:
                    label = leaf if len(names) == 1 else name
                    lines.append("    " + self._describe_field(label, name))
        if self.unstructured_predicates:
            lines.append("  unreadable by this client: " + ", ".join(self.unstructured_predicates))
        return "\n".join(lines)

    def _describe_field(self, label: str, predicate: str) -> str:
        """One field line of ``describe()``: what it holds, how many, and what it means."""

        node = self._node_at(predicate)
        assert node is not None and node.structure is not None
        structure = node.structure
        held = (
            "-> " + " | ".join(structure.allowed_object_subject_kinds)
            if structure.object_kind == "subject"
            else structure.object_kind
        )
        parts = [f"{label}: {held}, {structure.cardinality}"]
        members = literal_schema_members(structure.literal_schema)
        if members:
            parts.append("[" + ", ".join(members) + "]")
        if node.meaning is not None and node.meaning.description:
            parts.append("-- " + node.meaning.description.splitlines()[0])
        return " ".join(parts)

    def _kind_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._walk(lambda node: node.subject_kind)))

    def _predicate_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._walk(lambda node: node.structure is not None)))

    def stub(self) -> str:
        """Render this world as a `.pyi` module stub.

        Next: write it beside your script as a ``.pyi`` so an editor checks every name.
        """

        from cruxible_client.authoring.world_stub import render_world_stub

        return render_world_stub(self)

    def _walk(self, admits: Any) -> list[str]:
        found: list[str] = []
        stack = [self._root]
        while stack:
            node = stack.pop()
            if node.path and admits(node):
                found.append(node.path)
            stack.extend(node.children.values())
        return found

    def _assert_current(self) -> None:
        self._playbill._assert_coordinate(self._coordinate)

    def _write_field(self, subject_kind: str, keyword_or_leaf: str) -> str:
        """The full predicate one write keyword names for a kind, checked here."""

        # The keyword escape (`class_`, `self_`) adds exactly one underscore to a
        # leaf that could not be a keyword as it stands; no other leaf ends in one.
        leaf = keyword_or_leaf[:-1] if keyword_or_leaf.endswith("_") else keyword_or_leaf
        return self._predicate_for(subject_kind, leaf).address

    def _write(
        self,
        changes: Sequence[Change],
        *,
        because: str,
        dry_run: bool,
        accept: WriteAccept,
    ) -> WriteOutcome:
        """Send this World's changes, checked from its write basis; keep references valid.

        A World is a snapshot, so its references would go stale the moment its
        own write moved the head. Instead its writes are checked from its
        coordinate advanced past its own accepted writes -- as long as nothing
        else was accepted in between -- so a field refuses only when someone
        else changed it.
        """

        from cruxible_client.contracts.write import WriteRequest

        if not changes:
            raise TypeError("a write needs at least one field=value")
        outcome = self._playbill._write(
            WriteRequest(
                because=because,
                changes=tuple(changes),
                dry_run=dry_run,
                accept=accept,
                at=self._write_basis,
                surface="sdk",
                full_coordinate=True,
            )
        )
        base = outcome.base
        if (
            outcome.status == "accepted"
            and base is not None
            and self._write_basis[: len(base.git_oid)] == base.git_oid
        ):
            self._write_basis = outcome.coordinate.git_oid
        return outcome

    def _materialize(self, node: _Node) -> KindNamespace | WorldClaimType:
        """Resolve one node to the object its accepted structure makes it.

        A predicate wins over a bare namespace: names it nests stay reachable
        through the ClaimType's own attribute access, and Subject IDs, when the
        same dotted name is also an accepted Subject kind, stay reachable by
        index. That keeps one deterministic answer for a name that is both.
        """

        self._assert_current()
        structure = node.structure
        if structure is None:
            return KindNamespace(self, node)
        meaning = node.meaning or _Meaning()
        return WorldClaimType(
            address=structure.predicate,
            coordinate=self._coordinate,
            object_kind=ClaimObjectKind(structure.object_kind),
            cardinality=Cardinality(structure.cardinality),
            allowed_subject_kinds=structure.allowed_subject_kinds,
            allowed_object_subject_kinds=structure.allowed_object_subject_kinds,
            permitted_roles=tuple(ClaimRole(role) for role in structure.permitted_roles),
            referent_sensitivity=ReferentSensitivity(structure.referent_sensitivity),
            literal_schema=(
                None if structure.literal_schema is None else dict(structure.literal_schema)
            ),
            description=meaning.description,
            member_descriptions=meaning.member_descriptions,
            default_role=meaning.default_role,
            evidence_requirement=meaning.evidence_requirement,
            revision_evidence=meaning.revision_evidence,
            _world=self,
            _node=node,
        )

    def _node_at(self, path: str) -> _Node | None:
        node = self._root
        for segment in path.split("."):
            child = node.children.get(segment)
            if child is None:
                return None
            node = child
        return node

    def claim_type(self, predicate: str) -> WorldClaimType:
        """Read one accepted predicate by its full dotted name.

        Next: its ``members``, ``cardinality`` and ``description``; ``cx.query(kind,
        select=[...])`` for its values.
        """

        node = self._node_at(predicate)
        if node is None or node.structure is None:
            raise WorldStructureError(f"{predicate!r} is not an accepted predicate")
        built = self._materialize(node)
        assert isinstance(built, WorldClaimType)
        return built

    def kind(self, subject_kind: str) -> KindNamespace:
        """Read one accepted Subject kind by its full dotted name.

        The escape for a kind whose segments attribute access cannot spell -- a
        Python keyword such as `dev.class` -- and for one a predicate of the same
        dotted name wins, exactly as `claim_type` is the escape for a predicate.

        Next: ``kind["<id>"]`` for one Subject, ``kind.where(...)`` to query it.
        """

        node = self._node_at(subject_kind)
        if node is None or not node.subject_kind:
            raise WorldStructureError(f"{subject_kind!r} is not an accepted Subject kind")
        self._assert_current()
        return KindNamespace(self, node)

    def _leaf_map(self, subject_kind: str) -> Mapping[str, list[str]]:
        """Map each predicate last segment to every predicate it names for a kind."""

        by_leaf: dict[str, list[str]] = {}
        for predicate in self._predicate_paths():
            node = self._node_at(predicate)
            assert node is not None and node.structure is not None
            if subject_kind not in node.structure.allowed_subject_kinds:
                continue
            by_leaf.setdefault(predicate.rsplit(".", 1)[-1], []).append(predicate)
        return by_leaf

    def _predicate_leaves(self, subject_kind: str) -> Mapping[str, str]:
        """Map each leaf attribute access really answers to the predicate it names.

        A leaf that is ambiguous, or that a `WorldSubject` member already claims,
        is left out: it is reachable by index, and advertising it here -- in
        `dir()` and in the generated stub -- would promise an attribute read that
        answers something else.
        """

        return {
            leaf: names[0]
            for leaf, names in self._leaf_map(subject_kind).items()
            if len(names) == 1 and leaf not in SUBJECT_MEMBERS and _is_identifier(leaf)
        }

    def _predicate_for(self, subject_kind: str, leaf: str) -> WorldClaimType:
        by_leaf = self._leaf_map(subject_kind)
        candidates = sorted(by_leaf.get(leaf, ()))
        if len(candidates) == 1:
            return self.claim_type(candidates[0])
        if candidates:
            spelled = ", ".join(candidates)
            raise AttributeError(
                f"{leaf!r} names more than one predicate admitted for {subject_kind!r}: "
                f"{spelled}; read the one you mean by its full name"
            )
        spelled = ", ".join(sorted(self._predicate_leaves(subject_kind))) or "nothing"
        raise AttributeError(
            f"no accepted predicate {leaf!r} is admitted for Subject kind "
            f"{subject_kind!r}; it admits: {spelled}"
        )

    def _subjects_of(self, subject_kind: str) -> Mapping[str, WorldSubject]:
        self._assert_current()
        if not self._subjects_loaded:
            self._load_subjects()
        return self._subject_cache.get(subject_kind, {})

    def _load_subjects(self) -> None:
        """Read every live Subject of every kind, complete, at this World's coordinate.

        One value-free ``query`` per kind lists Subjects ordered by ID, and
        asks for retired ones too (marked by ``lifecycle``) so every listed
        row is one the evaluator bound. A page continues by its cursor; an
        answer the server capped continues as a new window after the last ID
        it listed. Anything else that stops short -- a truncated answer with
        neither a cursor nor a cap, a cursor or window that does not advance,
        a row without its lifecycle -- refuses, and nothing is cached: a
        partial inventory would call an accepted Subject absent.
        """

        loaded: dict[str, dict[str, WorldSubject]] = {}
        for subject_kind in self._kind_paths():
            loaded[subject_kind] = {
                subject_id: WorldSubject(
                    address=f"{subject_kind}/{subject_id}",
                    coordinate=self._coordinate,
                    _world=self,
                )
                for subject_id in self._live_subject_ids(subject_kind)
            }
        for subject_kind, subjects in loaded.items():
            if subjects:
                self._subject_cache[subject_kind] = subjects
        self._subjects_loaded = True

    def _live_subject_ids(self, subject_kind: str) -> list[str]:
        from cruxible_client.contracts.compact_query import (
            QUERY_MAX_LIMIT,
            QueryRequest,
        )

        cx = self._playbill
        live: list[str] = []
        after: str | None = None
        while True:
            last: str | None = None
            cursor: str | None = None
            while True:
                page = cx._client.query(
                    cx._instance_id,
                    request=QueryRequest.model_validate(
                        {
                            "kind": subject_kind,
                            "select": ("subject_id",),
                            "where": ()
                            if after is None
                            else ({"field": "subject_id", "gt": after},),
                            "order_by": ("subject_id",),
                            "status": ("live", "retired"),
                            "limit": QUERY_MAX_LIMIT,
                            "cursor": cursor,
                            "at": None
                            if cursor is not None
                            else self._coordinate.model_dump(mode="json"),
                        }
                    ),
                )
                if page.receipt.coordinate.model_dump(mode="json") != (
                    self._coordinate.model_dump(mode="json")
                ):
                    raise WorldStructureError(
                        "Subject listing returned a different accepted coordinate"
                    )
                for row in page.rows:
                    lifecycle = row.get("lifecycle")
                    if lifecycle not in ("live", "retired"):
                        raise WorldStructureError(
                            f"the {subject_kind} Subject listing did not state each "
                            "Subject's lifecycle"
                        )
                    last = str(row["subject_id"])
                    if lifecycle == "live":
                        live.append(last)
                if page.next_cursor is None:
                    break
                if not page.rows or page.next_cursor == cursor:
                    raise WorldStructureError(
                        f"the {subject_kind} Subject listing is truncated and its cursor "
                        "does not advance; no Subjects were cached"
                    )
                cursor = page.next_cursor
            if not page.truncated:
                return live
            if not page.capped:
                raise WorldStructureError(
                    f"the {subject_kind} Subject listing is truncated with no cursor to "
                    "continue it; no Subjects were cached"
                )
            if last is None or (after is not None and last <= after):
                raise WorldStructureError(
                    f"the {subject_kind} Subject listing hit the server cap "
                    f"({', '.join(page.capped)}) without advancing; no Subjects were cached"
                )
            after = last

    def prefetch(
        self,
        *,
        subjects: Sequence[str | SubjectRef],
        predicates: Sequence[str | ClaimTypeRef] = (),
        page_size: int = 128,
        max_claims: int = 4096,
    ) -> tuple[ClaimView, ...]:
        """Fill selected attribute caches in bounded, coordinate-pinned pages.

        Strings are subject kind/id addresses or paths and fully qualified predicates.
        Every live contender is retained. If the explicit budget is exceeded,
        no partial attribute cache is installed and the caller can narrow the
        selection or increase ``max_claims``.

        Next: Cruxible.orient() to map state, Cruxible.query() for rows, or Cruxible.get().
        """
        from datetime import datetime

        from cruxible_client.contracts.claim_reads import ClaimReadBatchRequest

        self._assert_current()
        if max_claims < 1:
            raise ValueError("max_claims must be positive")
        for ref in (*subjects, *predicates):
            if isinstance(ref, (SubjectRef, ClaimTypeRef)):
                self._playbill._assert_coordinate(ref.coordinate)
        paths = tuple(ref.address if isinstance(ref, SubjectRef) else ref for ref in subjects)
        addresses = tuple(
            path.removeprefix("subjects/").removesuffix(".json")
            if path.startswith("subjects/")
            else path
            for path in paths
        )
        paths = tuple(f"subjects/{address}.json" for address in addresses)
        names = tuple(ref.address if isinstance(ref, ClaimTypeRef) else ref for ref in predicates)
        request = ClaimReadBatchRequest.model_validate(
            {
                "at": self._coordinate.model_dump(mode="json"),
                "subject_paths": paths,
                "predicates": names,
                "limit": page_size,
                "evaluation_time": datetime.fromisoformat(self._playbill._evaluation_time()),
            }
        )
        cursor: str | None = None
        seen: set[str] = set()
        views: list[ClaimView] = []
        while True:
            result = self._playbill._client.read_claim_batch(
                self._playbill._instance_id,
                request=request.model_copy(update={"cursor": cursor}),
            )
            self._assert_current()
            if result.coordinate.model_dump(mode="json") != self._coordinate.model_dump(
                mode="json"
            ):
                raise WorldStructureError("prefetch returned a different accepted coordinate")
            for raw in result.claims:
                if raw.coordinate != result.coordinate:
                    raise WorldStructureError("prefetch Claim returned a different coordinate")
                view = self._playbill._with_exact_text(
                    self._playbill._typed_claim_view(raw),
                    AcceptedCoordinate.model_validate(raw.coordinate.model_dump(mode="json")),
                )
                if (
                    view.claim_id in seen
                    or view.subject not in paths
                    or (names and view.predicate not in names)
                    or view.lifecycle_state != "live"
                ):
                    raise WorldStructureError("prefetch returned an invalid or duplicate selection")
                seen.add(view.claim_id)
                views.append(view)
            if len(views) > max_claims:
                raise WorldStructureError("prefetch exceeds max_claims; narrow the selection")
            if not result.truncated:
                if result.cursor is not None:
                    raise WorldStructureError("complete prefetch page unexpectedly has a cursor")
                break
            if not result.claims or result.cursor is None or result.cursor == cursor:
                raise WorldStructureError("prefetch pagination does not advance")
            cursor = result.cursor
        # Commit only complete selections, including genuinely empty attributes.
        for address, path in zip(addresses, paths, strict=True):
            selected = tuple(view for view in views if view.subject == path)
            for name in names or (None,):
                self._claim_cache[(address, name)] = tuple(
                    view for view in selected if name is None or view.predicate == name
                )
            if not names:
                for name in {view.predicate for view in selected}:
                    self._claim_cache[(address, name)] = tuple(
                        view for view in selected if view.predicate == name
                    )
        for view in views:
            self._view_cache[view.claim_id] = view
        return tuple(views)

    def values(
        self,
        *,
        subjects: Sequence[str | SubjectRef],
        predicates: Sequence[str | ClaimTypeRef] = (),
    ) -> tuple[QueryClaimValue, ...]:
        """Each live Claim's value, verdict and status for these Subjects, through ``query``.

        Lighter than ``prefetch`` when only values and verdicts are wanted: one
        ``query`` per Subject kind asks for each cell's Claims, including those
        resolution overturned or refused. Strings are subject kind/id addresses
        or paths and fully qualified predicates. A string value over 500
        characters is a ``TruncatedText`` (``preview``, ``length``), never the
        value: ``cx.get(item.claim, detail="evidence").evidence.value`` reads it whole.

        Next: Cruxible.orient() to map state, Cruxible.query() for rows, or Cruxible.get().
        """
        from cruxible_client.contracts.compact_query import QUERY_MAX_SELECT

        self._assert_current()
        for ref in (*subjects, *predicates):
            if isinstance(ref, (SubjectRef, ClaimTypeRef)):
                self._playbill._assert_coordinate(ref.coordinate)
        addresses = [
            address.removeprefix("subjects/").removesuffix(".json")
            for address in (ref.address if isinstance(ref, SubjectRef) else ref for ref in subjects)
        ]
        names = {ref.address if isinstance(ref, ClaimTypeRef) else ref for ref in predicates}
        by_kind: dict[str, list[str]] = {}
        for address in dict.fromkeys(addresses):
            kind, _, subject_id = address.partition("/")
            by_kind.setdefault(kind, []).append(subject_id)
        cx = self._playbill
        # Every page is one answer: this World's coordinate and one evaluation
        # instant, however many Subject and predicate batches it takes.
        evaluation_time = cx._evaluation_time()
        values: list[QueryClaimValue] = []
        for kind, ids in by_kind.items():
            admitted = {full for group in self._leaf_map(kind).values() for full in group}
            select = sorted(admitted & names if names else admitted)
            for start in range(0, len(ids), 256) if select else ():
                for first in range(0, len(select), QUERY_MAX_SELECT):
                    values.extend(
                        self._value_page(
                            kind,
                            ids[start : start + 256],
                            select[first : first + QUERY_MAX_SELECT],
                            evaluation_time=evaluation_time,
                        )
                    )
        return tuple(values)

    def _value_page(
        self,
        kind: str,
        ids: Sequence[str],
        select: Sequence[str],
        *,
        evaluation_time: str,
    ) -> list[QueryClaimValue]:
        """Every Claim value of one Subject and predicate batch, every page of it."""
        from cruxible_client.contracts.compact_query import (
            QUERY_MAX_LIMIT,
            QueryClaim,
            QueryClaimValue,
            QueryRequest,
        )

        cx = self._playbill
        values: list[QueryClaimValue] = []
        cursor: str | None = None
        while True:
            page = cx._client.query(
                cx._instance_id,
                request=QueryRequest.model_validate(
                    {
                        "kind": kind,
                        "where": [{"field": "subject_id", "in": list(ids)}],
                        "select": list(select),
                        "status": ("live", "overturned", "refused"),
                        "claims": True,
                        "limit": QUERY_MAX_LIMIT,
                        "cursor": cursor,
                        "at": None
                        if cursor is not None
                        else self._coordinate.model_dump(mode="json"),
                        "evaluation_time": None if cursor is not None else evaluation_time,
                    }
                ),
            )
            self._assert_current()
            if page.receipt.coordinate.model_dump(mode="json") != (
                self._coordinate.model_dump(mode="json")
            ):
                raise WorldStructureError("Claim values returned a different accepted coordinate")
            predicate_of = {
                column.name: column.predicate for column in page.columns if column.predicate
            }
            for row in page.rows:
                cells = row.get("claims") or {}
                if not isinstance(cells, dict):
                    raise WorldStructureError("Claim values returned a row with no Claim cells")
                for column, entries in cells.items():
                    if not isinstance(entries, list):
                        raise WorldStructureError("Claim values returned a malformed Claim cell")
                    values.extend(
                        QueryClaimValue(
                            **dict(QueryClaim.model_validate(entry)),
                            subject=str(row["subject"]),
                            predicate=predicate_of[column],
                        )
                        for entry in entries
                    )
            if not page.truncated:
                return values
            if page.next_cursor is None or page.next_cursor == cursor or not page.rows:
                raise WorldStructureError(
                    f"the {kind} Claim values are truncated and cannot be continued"
                )
            cursor = page.next_cursor

    def _claims_about(
        self,
        subject_address: str,
        *,
        predicate: str | None = None,
    ) -> tuple[ClaimView, ...]:
        self._assert_current()
        cached = self._claim_cache.get((subject_address, predicate))
        if cached is None and (subject_address, None) in self._claim_cache:
            cached = tuple(
                view
                for view in self._claim_cache[(subject_address, None)]
                if predicate is None or view.predicate == predicate
            )
        if cached is not None:
            return cached
        # One bounded, coordinate-pinned batch read of every live Claim about
        # the Subject (or of one predicate), walked to its last page.
        self.prefetch(
            subjects=(subject_address,), predicates=() if predicate is None else (predicate,)
        )
        return self._claim_cache[(subject_address, predicate)]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        child = self._root.children.get(name)
        if child is None:
            spelled = ", ".join(sorted(self._root.children)) or "nothing"
            raise AttributeError(
                f"this world names no {name!r}; it names: {spelled}. Repair: refresh "
                "the connection if the vocabulary was accepted after this world was read."
            )
        return self._materialize(child)

    def __dir__(self) -> list[str]:
        return sorted({*super().__dir__(), *self._root.children})

    def __repr__(self) -> str:
        # Deliberately does not assert the coordinate: a debugger looking at a
        # stale world must still be able to see what it was.
        return (
            f"<World at {self._coordinate.git_oid} "
            f"kinds={len(self._kind_paths())} predicates={len(self._predicate_paths())}>"
        )


def _insert(root: _Node, path: str) -> _Node:
    node = root
    for segment in path.split("."):
        child = node.children.get(segment)
        if child is None:
            prefix = f"{node.path}.{segment}" if node.path else segment
            child = _Node(path=prefix)
            node.children[segment] = child
        node = child
    return node


def _replace(root: _Node, path: str, **updates: object) -> None:
    parent = root
    segments = path.split(".")
    for segment in segments[:-1]:
        parent = parent.children[segment]
    existing = parent.children[segments[-1]]
    parent.children[segments[-1]] = _Node(
        path=existing.path,
        children=existing.children,
        subject_kind=cast(bool, updates.get("subject_kind", existing.subject_kind)),
        structure=cast("ClaimTypeStructure | None", updates.get("structure", existing.structure)),
        meaning=cast("_Meaning | None", updates.get("meaning", existing.meaning)),
    )


def _meaning(envelope: Mapping[str, object]) -> _Meaning:
    """Read a ClaimType's v7 meaning; anything unreadable keeps the pre-v7 meaning."""

    description = envelope.get("description")
    members: list[ClaimTypeMemberDescription] = []
    raw_members = envelope.get("member_descriptions")
    for item in raw_members if isinstance(raw_members, list) else ():
        try:
            members.append(ClaimTypeMemberDescription.model_validate(item))
        except ValueError:
            continue
    role = envelope.get("default_role")
    requirement = envelope.get("evidence_requirement")
    revision = envelope.get("revision_evidence")
    return _Meaning(
        description=description if isinstance(description, str) else None,
        member_descriptions=tuple(members),
        default_role=ClaimRole(role) if role in {item.value for item in ClaimRole} else None,
        evidence_requirement=(
            cast(EvidenceRequirement, requirement)
            if requirement in {"none", "self", "captured"}
            else "self"
        ),
        revision_evidence=(
            cast(RevisionEvidence, revision)
            if revision in {"replace", "accumulate"}
            else "accumulate"
        ),
    )


def build_world(
    cx: Cruxible,
    *,
    coordinate: AcceptedCoordinate,
    claim_type_envelopes: Sequence[Mapping[str, object]],
) -> World:
    """Assemble one world from the accepted ClaimType vocabulary.

    Subject kinds come from the vocabulary rather than from the Subjects
    themselves, which is what lets `cx.world()` name every kind without reading
    a single Subject: a kind with no ClaimType admitting it is a kind nothing
    can be said about.
    """

    root = _Node(path="")
    unstructured: list[str] = []
    subject_kinds: set[str] = set()
    for envelope in claim_type_envelopes:
        lifecycle = envelope.get("lifecycle")
        if isinstance(lifecycle, Mapping) and lifecycle.get("state") == "retired":
            continue
        check = check_claim_type_structure(
            {
                "predicate": envelope.get("predicate"),
                "allowed_subject_kinds": envelope.get("allowed_subject_kinds", ()),
                "object_kind": envelope.get("object_kind"),
                "literal_schema": envelope.get("literal_schema"),
                "allowed_object_subject_kinds": envelope.get("allowed_object_subject_kinds", ()),
                "cardinality": envelope.get("cardinality"),
                "permitted_roles": envelope.get("permitted_roles", ()),
                "referent_sensitivity": envelope.get("referent_sensitivity", "identity"),
            }
        )
        predicate = envelope.get("predicate")
        if check.status != "valid" or check.structure is None:
            # Structure this client cannot read is daemon/client skew, not a
            # caller mistake. Naming it on the world keeps it visible instead of
            # dropping a predicate silently.
            if isinstance(predicate, str):
                unstructured.append(predicate)
            continue
        structure = check.structure
        _insert(root, structure.predicate)
        _replace(root, structure.predicate, structure=structure, meaning=_meaning(envelope))
        subject_kinds.update(structure.allowed_subject_kinds)
        subject_kinds.update(structure.allowed_object_subject_kinds)
    for subject_kind in sorted(subject_kinds):
        if not all(_SEGMENT_RE.fullmatch(segment) for segment in subject_kind.split(".")):
            continue
        _insert(root, subject_kind)
        _replace(root, subject_kind, subject_kind=True)
    return World(
        cx,
        coordinate=coordinate,
        root=root,
        unstructured_predicates=tuple(sorted(set(unstructured))),
    )


__all__ = [
    "CLAIM_TYPE_MEMBERS",
    "KindNamespace",
    "Names",
    "SUBJECT_MEMBERS",
    "World",
    "WorldClaimType",
    "WorldStructureError",
    "WorldSubject",
    "admit_literal",
    "build_world",
    "literal_schema_members",
]
