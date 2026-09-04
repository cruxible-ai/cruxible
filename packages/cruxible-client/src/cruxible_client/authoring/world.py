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
        return sorted({*super().__dir__(), *self._node.children, *self.members})


@dataclass(frozen=True)
class WorldSubject(SubjectRef):
    """One accepted Subject, readable through the verbs that already serve it."""

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

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        predicate = self._world._predicate_for(self.subject_kind, name)
        return tuple(claim for claim in self.claims if claim.predicate == predicate.address)

    def __dir__(self) -> list[str]:
        return sorted({*super().__dir__(), *self._world._predicate_leaves(self.subject_kind)})


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
    """The accepted ontology of one instance, as objects rather than strings."""

    __slots__ = (
        "_claim_cache",
        "_coordinate",
        "_playbill",
        "_root",
        "_subject_cache",
        "_subjects_loaded",
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
        self._claim_cache: dict[str, tuple[ClaimView, ...]] = {}
        self.unstructured_predicates = unstructured_predicates

    @property
    def coordinate(self) -> AcceptedCoordinate:
        return self._coordinate

    @property
    def kinds(self) -> tuple[str, ...]:
        """Every accepted Subject kind this world knows, byte-sorted."""

        return tuple(sorted(self._walk(lambda node: node.subject_kind)))

    @property
    def predicates(self) -> tuple[str, ...]:
        """Every accepted predicate this world knows, byte-sorted."""

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

    def _predicate_leaves(self, subject_kind: str) -> Mapping[str, str]:
        """Map each unambiguous last segment to the predicate it names for a kind."""

        by_leaf: dict[str, list[str]] = {}
        for predicate in self.predicates:
            node = self._node_at(predicate)
            assert node is not None and node.structure is not None
            if subject_kind not in node.structure.allowed_subject_kinds:
                continue
            by_leaf.setdefault(predicate.rsplit(".", 1)[-1], []).append(predicate)
        return {leaf: names[0] for leaf, names in by_leaf.items() if len(names) == 1}

    def _predicate_for(self, subject_kind: str, leaf: str) -> WorldClaimType:
        by_leaf: dict[str, list[str]] = {}
        for predicate in self.predicates:
            node = self._node_at(predicate)
            assert node is not None and node.structure is not None
            if subject_kind not in node.structure.allowed_subject_kinds:
                continue
            by_leaf.setdefault(predicate.rsplit(".", 1)[-1], []).append(predicate)
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

    def _claims_about(self, subject_address: str) -> tuple[ClaimView, ...]:
        self._assert_current()
        cached = self._claim_cache.get(subject_address)
        if cached is not None:
            return cached
        from cruxible_client.authoring.sdk import _subject_address

        playbill = self._playbill
        page = playbill._search(
            mode="list",
            query=None,
            kinds=("claim",),
            statuses=(),
            subject=_subject_address(subject_address).model_dump(mode="json"),
        )
        views = tuple(
            view
            for view in (
                playbill.claim_view(str(row["identity"]))
                for row in page.rows
                if isinstance(row.get("identity"), str)
            )
            if view.lifecycle_state == "live"
        )
        self._claim_cache[subject_address] = views
        return views

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
        return (
            f"<World at {self._coordinate.git_oid} "
            f"kinds={len(self.kinds)} predicates={len(self.predicates)}>"
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
    "KindNamespace",
    "World",
    "WorldClaimType",
    "WorldStructureError",
    "WorldSubject",
    "admit_literal",
    "build_world",
    "literal_schema_members",
]
