"""Typed, attribute-addressed access to one accepted world.

Every authoring surface below `Playbill` still speaks strings: a predicate is a
dotted name, a Subject is a `kind/id` shorthand, and a literal is whatever the
caller typed. That is exactly the ergonomics markdown already has, so the SDK
gave up its one structural advantage -- it knows the accepted ontology and can
hand it back as objects with fields. `pb.world()` reads the accepted ClaimType
vocabulary once and exposes it as a tree: kinds nest, Subjects answer by
attribute or index, predicates carry their own structure and their own
admissible values, and every ref it mints carries the coordinate it was read at.

The world is a READ, never an authority. It refuses once the connection's
orientation moves, under the same law as every other typed ref, because a name
that resolved at one coordinate may name something else at the next.

    w = pb.world()

    vulnerability = w.sec.vulnerability["cve-2026-69247"]
    vulnerability.severity                      # live Claims under that predicate
    w.sec.vuln.severity.cardinality             # the ClaimType's own structure

    draft = pb.changes(rationale="Name the package this advisory affects.")
    package = draft.subject(w.sec.package.define("click"))
    draft.claim(
        subject=vulnerability,
        predicate=w.sec.vuln.affects_package,
        value=package,                          # the same set defines it
        role="observation",
        rationale="The advisory names this package.",
        self_source="affects: click\n",
        supported_by=None, copied_from=None, qualifier=None,
        effective_period=None, revises=None, dispositions={}, publish_to=None,
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
    PlaybillSdkError,
    ReferentSensitivity,
    SubjectRef,
)
from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.claim_type_structure import (
    ClaimTypeStructure,
    check_claim_type_structure,
)
from cruxible_client.contracts.projection import AcceptedCoordinate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_client.authoring.sdk import ClaimView, Playbill, SubjectDraft

_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class WorldStructureError(PlaybillSdkError):
    """The world cannot answer this name at the shape it was asked for."""

    code = "playbill.sdk.world_structure_refused"


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
        "kind",
        "literal_schema",
        "members",
        "object_kind",
        "permitted_roles",
        "predicate",
        "referent_sensitivity",
    }
)

SUBJECT_MEMBERS = frozenset(
    {
        "address",
        "claims",
        "coordinate",
        "explain",
        "kind",
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
            raise _refuse(predicate, f"declared type {declared!r} is not an exact Playbill type")
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


@dataclass(frozen=True)
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
    _world: World = field(repr=False, compare=False)
    _node: _Node = field(repr=False, compare=False)

    @property
    def predicate(self) -> str:
        return self.address

    @property
    def members(self) -> tuple[str, ...]:
        """Return the enum members this predicate's literal schema names."""

        return literal_schema_members(self.literal_schema)

    @property
    def as_kind(self) -> KindNamespace:
        """Reach the Subject kind this dotted name also names.

        A ClaimType wins attribute access over a Subject kind of the same dotted
        name, which would otherwise leave `define()` and `subject_ids`
        unreachable. This is that escape.
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


@dataclass(frozen=True)
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
        return self.address.split("/", 1)[0]

    @property
    def subject_id(self) -> str:
        return self.address.split("/", 1)[1]

    @property
    def claims(self) -> tuple[ClaimView, ...]:
        """Every live Claim this Subject is the subject of."""

        return self._world._claims_about(self.address)

    def explain(self) -> object:
        """Read this Subject's governance and provenance context."""

        self._world._assert_current()
        return self._world._playbill.explain(self)

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
    """

    __slots__ = ("_node", "_world")

    def __init__(self, world: World, node: _Node) -> None:
        self._world = world
        self._node = node

    @property
    def subject_kind(self) -> str | None:
        """Return this namespace's Subject kind, or None if it is only a prefix."""

        return self._node.path if self._node.subject_kind else None

    @property
    def subject_ids(self) -> tuple[str, ...]:
        """Return every accepted Subject ID of this kind, loading them on first ask."""

        return tuple(self._subjects())

    def define(self, subject_id: str) -> SubjectDraft:
        """Draft one new Subject of this kind for a changeset to define."""

        kind = self._require_kind()
        self._world._assert_current()
        from cruxible_client.contracts.artifacts import ArtifactLifecycle

        return self._world._playbill.subject(
            subject=f"{kind}/{subject_id}",
            pins=(),
            lifecycle=ArtifactLifecycle(),
        )

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


class World:
    """The accepted ontology of one instance, as objects rather than strings.

    Every name here answers at exactly one coordinate and refuses once the
    connection's orientation moves -- `kinds`, `predicates`, attribute access,
    `kind()`, `claim_type()`, `stub()` and every read a ref reaches. Only
    `repr()` and the immutable fields of a ref already held answer stale, so a
    debugger can still see what a stale world was.

    `kind()` and `claim_type()` are the escapes for a dotted name attribute
    access cannot spell: a Python keyword segment, or a kind a predicate of the
    same name wins.
    """

    __slots__ = (
        "_claim_cache",
        "_coordinate",
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
        playbill: Playbill,
        *,
        coordinate: AcceptedCoordinate,
        root: _Node,
        unstructured_predicates: tuple[str, ...],
    ) -> None:
        self._playbill = playbill
        self._coordinate = coordinate
        self._root = root
        self._subject_cache: dict[str, dict[str, WorldSubject]] = {}
        self._subjects_loaded = False
        self._row_cache: dict[str, tuple[Mapping[str, object], ...]] = {}
        self._claim_cache: dict[tuple[str, str | None], tuple[ClaimView, ...]] = {}
        self._view_cache: dict[str, ClaimView] = {}
        self.unstructured_predicates = unstructured_predicates

    @property
    def coordinate(self) -> AcceptedCoordinate:
        return self._coordinate

    @property
    def kinds(self) -> tuple[str, ...]:
        """Every accepted Subject kind this world knows, byte-sorted."""

        self._assert_current()
        return self._kind_paths()

    @property
    def predicates(self) -> tuple[str, ...]:
        """Every accepted predicate this world knows, byte-sorted."""

        self._assert_current()
        return self._predicate_paths()

    def _kind_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._walk(lambda node: node.subject_kind)))

    def _predicate_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._walk(lambda node: node.structure is not None)))

    def stub(self) -> str:
        """Render this world as a `.pyi` module stub."""

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
        """Read one accepted predicate by its full dotted name."""

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
        from cruxible_client.authoring.sdk import _api_coordinate

        playbill = self._playbill
        listing = playbill._client.list_playbill_subjects(
            playbill._instance_id,
            at=_api_coordinate(self._coordinate),
        )
        for view in listing.subjects:
            facts = {
                str(fact.get("schema_id")): fact.get("value")
                for fact in view.facts
                if isinstance(fact, Mapping)
            }
            address = _subject_address_of(view.envelope, facts)
            if address is None:
                continue
            lifecycle = facts.get("playbill.subject.lifecycle")
            if isinstance(lifecycle, Mapping):
                state = lifecycle.get("lifecycle")
                if isinstance(state, Mapping) and state.get("state") == "retired":
                    continue
            subject_kind, subject_id = address
            self._subject_cache.setdefault(subject_kind, {})[subject_id] = WorldSubject(
                address=f"{subject_kind}/{subject_id}",
                coordinate=self._coordinate,
                _world=self,
            )
        self._subjects_loaded = True

    def _claim_rows(self, subject_address: str) -> tuple[Mapping[str, object], ...]:
        """Walk every page of the subject-filtered list, cached per Subject.

        The served list carries a row budget, so one call answers the first page
        and says so. A read that returned that page as if it were the whole
        answer would under-report a Subject with more Claims than the budget and
        give no signal, which is the one failure mode hard state must not have.
        This follows the cursor to exhaustion, and refuses -- typed, naming what
        it had -- if the daemon reports a truncated page it cannot continue.

        A cursor that does not advance is that same refusal, not a page to fetch
        again. Two ways for a client to be wrong about a truncated list, and
        only one of them was covered: a daemon that reports no further page, and
        a daemon that reports the cursor it was just given. The second is skew
        rather than corruption -- the answer is not wrong, the walk simply never
        ends -- and an unbounded loop appending the same rows forever is a poor
        failure mode for a client whose whole thesis is refusing wrong answers.
        """

        cached = self._row_cache.get(subject_address)
        if cached is not None:
            return cached
        from cruxible_client.authoring.sdk import _subject_address

        subject = _subject_address(subject_address).model_dump(mode="json")
        rows: list[Mapping[str, object]] = []
        cursor: Mapping[str, object] | None = None
        while True:
            page = self._playbill._search(
                mode="list",
                query=None,
                kinds=("claim",),
                statuses=(),
                subject=subject,
                cursor=cursor,
            )
            rows.extend(row for row in page.rows if isinstance(row, Mapping))
            if not page.truncated:
                break
            if page.cursor is None or not page.rows:
                raise WorldStructureError(
                    f"the accepted list of Claims about {subject_address!r} is truncated "
                    f"after {len(rows)} rows and cannot be continued: the daemon reported "
                    "no further page. Repair: read the Claims through `playbill list` with "
                    "an explicit cursor rather than trusting a short answer here"
                )
            if cursor is not None and page.cursor == cursor:
                raise WorldStructureError(
                    f"the accepted list of Claims about {subject_address!r} is truncated "
                    f"after {len(rows)} rows and cannot be continued: the daemon handed "
                    "back the cursor it was given, so the walk does not advance. Repair: "
                    "read the Claims through `playbill list` with an explicit cursor "
                    "rather than trusting a short answer here"
                )
            cursor = page.cursor
        self._row_cache[subject_address] = tuple(rows)
        return self._row_cache[subject_address]

    def _claims_about(
        self,
        subject_address: str,
        *,
        predicate: str | None = None,
    ) -> tuple[ClaimView, ...]:
        self._assert_current()
        cached = self._claim_cache.get((subject_address, predicate))
        if cached is not None:
            return cached
        playbill = self._playbill
        views: list[ClaimView] = []
        for row in self._claim_rows(subject_address):
            identity = row.get("identity")
            if not isinstance(identity, str):
                continue
            # The served row already names the predicate and marks a retired
            # Claim, so a per-predicate view reads only the Claims that can
            # survive the filter instead of every Claim about the Subject.
            if predicate is not None and row.get("predicate") != predicate:
                continue
            if row.get("status") == "retired":
                continue
            view = self._view_cache.get(identity)
            if view is None:
                view = playbill.claim_view(identity)
                self._view_cache[identity] = view
            if view.lifecycle_state == "live":
                views.append(view)
        self._claim_cache[(subject_address, predicate)] = tuple(views)
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


def _subject_address_of(
    envelope: Mapping[str, object],
    facts: Mapping[str, object],
) -> tuple[str, str] | None:
    """Read one Subject's kind and ID from its projection, or None if unreadable."""

    identity_fact = facts.get("playbill.subject.identity")
    if isinstance(identity_fact, Mapping):
        subject_kind = identity_fact.get("subject_kind")
        subject_id = identity_fact.get("subject_id")
        if isinstance(subject_kind, str) and isinstance(subject_id, str):
            return subject_kind, subject_id
    identity = envelope.get("identity")
    if not isinstance(identity, str):
        return None
    name = identity.removeprefix("Subject:")
    if name.count("/") != 1:
        return None
    subject_kind, subject_id = name.split("/", 1)
    return subject_kind, subject_id


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
    )


def build_world(
    playbill: Playbill,
    *,
    coordinate: AcceptedCoordinate,
    claim_type_envelopes: Sequence[Mapping[str, object]],
) -> World:
    """Assemble one world from the accepted ClaimType vocabulary.

    Subject kinds come from the vocabulary rather than from the Subjects
    themselves, which is what lets `pb.world()` name every kind without reading
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
        _replace(root, structure.predicate, structure=structure)
        subject_kinds.update(structure.allowed_subject_kinds)
        subject_kinds.update(structure.allowed_object_subject_kinds)
    for subject_kind in sorted(subject_kinds):
        if not all(_SEGMENT_RE.fullmatch(segment) for segment in subject_kind.split(".")):
            continue
        _insert(root, subject_kind)
        _replace(root, subject_kind, subject_kind=True)
    return World(
        playbill,
        coordinate=coordinate,
        root=root,
        unstructured_predicates=tuple(sorted(set(unstructured))),
    )


__all__ = [
    "CLAIM_TYPE_MEMBERS",
    "KindNamespace",
    "SUBJECT_MEMBERS",
    "World",
    "WorldClaimType",
    "WorldStructureError",
    "WorldSubject",
    "admit_literal",
    "build_world",
    "literal_schema_members",
]
