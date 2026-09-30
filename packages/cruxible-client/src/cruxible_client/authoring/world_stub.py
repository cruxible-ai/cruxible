"""Render one accepted world as a `.pyi` module stub.

A world is discovered at runtime, so an editor and a model both see `Any` where
the instance's own vocabulary is. This writes that vocabulary down as types at
exactly one coordinate: the kinds it nests, the Subject IDs that can be spelled
as attributes, every predicate, and each enum member a literal schema names.

The stub's classes are CLOSED. They carry the concrete surface of the runtime
objects but do not inherit the `__getattr__` those objects use to resolve a name
discovered at runtime, because an inherited `__getattr__` is exactly what makes
a type checker accept `world.sec.vuln.sevrity` -- the misspelling the stub
exists to catch. Dynamic access stays on the runtime objects; the stub types the
names this coordinate actually accepted, and nothing else.

The stub is a read, not a pin. It carries the coordinate it was generated at in
its header, and is byte-identical for the same world, so regenerating after an
activation shows the vocabulary movement as an ordinary diff.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from cruxible_client.authoring.compact_query import keyword_name
from cruxible_client.authoring.world import (
    CLAIM_TYPE_MEMBERS,
    KindNamespace,
    WorldClaimType,
    _is_identifier,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_client.authoring.world import World, _Node
    from cruxible_client.transport.http import CruxibleClient

STUB_HEADER_TAG = "playbill-world-stub-v1"

_NAMESPACE_MEMBERS = frozenset({"define", "select", "subject_ids", "subject_kind", "where"})
_WORLD_MEMBERS = frozenset(
    {
        "claim_type",
        "coordinate",
        "kind",
        "kinds",
        "predicates",
        "prefetch",
        "stub",
        "unstructured_predicates",
    }
)

_STUB_IMPORTS = (
    "from collections.abc import Sequence",
    "from collections.abc import Iterator",
    "from datetime import datetime",
    "from typing import Literal",
    "",
    "from cruxible_client.authoring.compact_query import QueryResult",
    "from cruxible_client.authoring.sdk import ClaimView, SubjectDraft",
    "from cruxible_client.authoring.sdk_types import (",
    "    Cardinality,",
    "    ClaimObjectKind,",
    "    ClaimRole,",
    "    ClaimTypeRef,",
    "    LiteralValue,",
    "    ReferentSensitivity,",
    "    SubjectRef,",
    ")",
    "from cruxible_client.authoring.world import KindNamespace, WorldClaimType",
    "from cruxible_client.contracts.claim_types import ClaimTypeMemberDescriptionV1",
    "from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1",
    "from cruxible_client.contracts.projection import AcceptedCoordinate",
    "from cruxible_client.contracts.write import (",
    "    Evidence,",
    "    WriteAccept,",
    "    WriteOutcome,",
    "    WriteRetireReason,",
    "    WriteRole,",
    ")",
)


def _encoded(path: str) -> str:
    """Return one dotted world path as an injective class-name suffix.

    A dot becomes a double underscore, which reads well -- but a segment may
    carry a double underscore of its own, and the accepted grammar admits both
    `a.b` and `a__b`, which would then claim the same class name and make the
    whole `.pyi` invalid. A path already spelling the separator is stamped with
    a digest of itself, so the readable form survives for every ordinary name
    and no two paths ever collide.
    """

    body = path.replace(".", "__")
    if "__" in path:
        body += "_" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
    return body


def _class_name(path: str) -> str:
    """Return the class one dotted world path resolves to as an attribute."""

    return "_W_" + _encoded(path)


def _kind_class_name(path: str) -> str:
    """Return the namespace class for a name that is also an accepted predicate."""

    return "_K_" + _encoded(path)


def _query_class_name(path: str) -> str:
    """Return the class a compact query over one accepted kind is typed as."""

    return "_Q_" + _encoded(path)


def _subject_class_name(path: str) -> str:
    """Return the class the Subjects of one accepted kind are typed as."""

    return "_S_" + _encoded(path)


def _sorted(values: Iterable[str]) -> list[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


class _Body:
    """One class body, which must carry a statement even when it declares nothing."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._statements = 0

    def declare(self, line: str) -> None:
        self._lines.append(f"    {line}")
        self._statements += 1

    def declare_lines(self, lines: list[str]) -> None:
        """Declare one statement spelled over several lines."""

        self._lines.extend(f"    {line}" for line in lines)
        self._statements += 1

    def note(self, line: str) -> None:
        self._lines.append(f"    # {line}")

    def rendered(self) -> list[str]:
        if self._statements:
            return list(self._lines)
        return [*self._lines, "    ..."]


_DOC_MEMBERS = 12
_DOC_LINE_CHARS = 120


def _escaped(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _clipped(text: str) -> str:
    return text if len(text) <= _DOC_LINE_CHARS else text[: _DOC_LINE_CHARS - 1] + "\u2026"


def _meaning_docstring(claim_type: WorldClaimType) -> list[str]:
    """The attribute docstring after a predicate leaf: its meaning, member by member.

    Empty for a predicate that describes nothing, so a world without ClaimType
    v7 descriptions renders exactly as before.
    """

    if claim_type.description is None and not claim_type.member_descriptions:
        return []
    lines = [] if claim_type.description is None else claim_type.description.splitlines()
    members = [
        _clipped(f"{item.member!r} \u2014 {' '.join(item.description.split())}")
        for item in claim_type.member_descriptions
    ]
    if members:
        if lines:
            lines.append("")
        lines.extend(members[:_DOC_MEMBERS])
        if len(members) > _DOC_MEMBERS:
            lines.append(f"... and {len(members) - _DOC_MEMBERS} more members")
    escaped = [_escaped(line) for line in lines]
    if len(escaped) == 1:
        return [f'"""{escaped[0]}"""']
    return [f'"""{escaped[0]}', *escaped[1:], '"""']


def _children(
    node: _Node, body: _Body, *, reserved: frozenset[str], world: World | None = None
) -> None:
    """Declare every child segment attribute access can actually spell.

    A segment may be a Python keyword (`dev.class`) or collide with a name the
    class already declares. Emitting one anyway produced a `.pyi` no parser would
    read -- and one such segment broke the whole file, not just its own line --
    so those are named in a comment carrying the escape that reaches them.
    """

    for child in _sorted(node.children):
        path = node.children[child].path
        if not _is_identifier(child):
            body.note(
                f"{child!r} is not a Python attribute; reach it with "
                f"world.kind({path!r}) or world.claim_type({path!r})"
            )
            continue
        if child in reserved:
            body.note(
                f"{child!r} is shadowed by a member of this class; reach it with "
                f"world.kind({path!r}) or world.claim_type({path!r})"
            )
            continue
        body.declare(f"{child}: {_class_name(path)}")
        if world is not None and node.children[child].structure is not None:
            claim_type = world.claim_type(path)
            if isinstance(claim_type, WorldClaimType):
                doc = _meaning_docstring(claim_type)
                if doc:
                    body.declare_lines(doc)


def _subject_block(world: World, node: _Node) -> list[str]:
    """Type the Subjects of one accepted kind, predicate leaf by predicate leaf."""

    lines = [f"class {_subject_class_name(node.path)}(SubjectRef):"]
    lines.append(f'    """Subjects of accepted kind {node.path}."""')
    lines.append("")
    body = _Body()
    body.declare("address: str")
    body.declare("coordinate: AcceptedCoordinate")
    body.declare("subject_kind: str")
    body.declare("subject_id: str")
    body.declare("claims: tuple[ClaimView, ...]")
    body.declare("def explain(self) -> object: ...")
    body.declare(
        "def __getitem__(self, predicate: str | ClaimTypeRef) -> tuple[ClaimView, ...]: ..."
    )
    reachable = world._predicate_leaves(node.path)
    for leaf in _sorted(reachable):
        body.declare(f"{leaf}: tuple[ClaimView, ...]")
        doc = _meaning_docstring(world.claim_type(reachable[leaf]))
        if doc:
            body.declare_lines(doc)
    _write_members(world, node.path, body)
    for leaf, predicates in sorted(world._leaf_map(node.path).items()):
        if leaf in reachable or len(predicates) != 1:
            continue
        body.note(
            f"predicate leaf {leaf!r} is not readable as an attribute; reach it with "
            f"subject[{predicates[0]!r}]"
        )
    lines.extend(body.rendered())
    return lines


def _write_annotation(claim_type: WorldClaimType) -> str:
    """The value one write keyword takes: an enum member is a ``Literal``."""

    object_kind = claim_type.object_kind.value
    if object_kind == "subject":
        return "str | SubjectRef"
    if object_kind == "exact_content":
        return "str"
    if claim_type.members:
        return "Literal[" + ", ".join(repr(member) for member in claim_type.members) + "]"
    declared = (claim_type.literal_schema or {}).get("type")
    return {"string": "str", "integer": "int", "number": "int", "boolean": "bool"}.get(
        str(declared), "str | int | bool"
    )


def _write_members(world: World, kind: str, body: _Body) -> None:
    """Type ``set`` over single-value leaves and ``add`` over many-valued ones."""

    by_cardinality: dict[str, list[str]] = {"one": [], "many": []}
    for leaf, predicates in sorted(world._leaf_map(kind).items()):
        spelled = keyword_name(leaf)
        if len(predicates) != 1 or spelled is None:
            continue
        claim_type = world.claim_type(predicates[0])
        by_cardinality[claim_type.cardinality.value].append(
            f"    {spelled}: {_write_annotation(claim_type)} = ...,"
        )
    common = [
        "    because: str,",
        "    evidence: Evidence | None = ...,",
        "    role: WriteRole | None = ...,",
    ]
    tail = ["    dry_run: bool = ...,", "    accept: WriteAccept = ...,"]
    body.declare_lines(
        ["def set(", "    self,", "    /,", "    *,", *common, "    contend: bool = ...,", *tail]
        + by_cardinality["one"]
        + [") -> WriteOutcome: ..."]
    )
    body.declare_lines(
        [
            "def add(",
            "    self,",
            "    /,",
            "    *,",
            *common,
            "    expect_absent: bool = ...,",
            *tail,
        ]
        + by_cardinality["many"]
        + [") -> WriteOutcome: ..."]
    )
    body.declare_lines(
        [
            "def retire(",
            "    self,",
            "    field: str | ClaimTypeRef,",
            "    /,",
            "    *,",
            "    because: str,",
            "    reason: WriteRetireReason = ...,",
            *tail,
            ") -> WriteOutcome: ...",
        ]
    )


def _namespace_block(world: World, node: _Node, *, class_name: str) -> list[str]:
    lines = [f"class {class_name}:"]
    lines.append(f'    """{node.path}"""')
    lines.append("")
    body = _Body()
    if node.subject_kind:
        subject = _subject_class_name(node.path)
        body.declare("subject_kind: str")
        body.declare("subject_ids: tuple[str, ...]")
        body.declare("def define(self, subject_id: str) -> SubjectDraft: ...")
        body.declare(f"def __getitem__(self, subject_id: str) -> {subject}: ...")
        body.declare("def __contains__(self, subject_id: object) -> bool: ...")
        body.declare(f"def __iter__(self) -> Iterator[{subject}]: ...")
        body.declare("def __len__(self) -> int: ...")
        _query_members(world, node.path, body, include_run=False)
    else:
        body.declare("subject_kind: None")
    _children(node, body, reserved=_NAMESPACE_MEMBERS, world=world)
    if node.subject_kind:
        namespace = KindNamespace(world, node)
        for subject_id in _sorted(namespace.subject_ids):
            if not _is_identifier(subject_id) or subject_id in _NAMESPACE_MEMBERS:
                continue
            if subject_id in node.children:
                continue
            body.declare(f"{subject_id}: {_subject_class_name(node.path)}")
    lines.extend(body.rendered())
    return lines


_ORDERED_OPERATORS = ("lt", "lte", "gt", "gte")


def _value_annotation(claim_type: WorldClaimType) -> tuple[str, tuple[str, ...]]:
    """The keyword value type of one predicate and the operators that apply to it.

    Mirrors the daemon's value checks, so a filter the stub accepts is one the
    daemon evaluates, and an enum member outside the schema is a type error.
    """

    object_kind = claim_type.object_kind.value
    if object_kind == "subject":
        return "str | SubjectRef", ("eq", "ne", "in", "exists", "contains")
    if object_kind == "exact_content":
        return "bool", ("exists",)
    members = claim_type.members
    if members:
        spelled = ", ".join(repr(member) for member in members)
        return f"Literal[{spelled}]", ("eq", "ne", "in", "exists", "contains")
    schema = claim_type.literal_schema or {}
    declared = schema.get("type")
    if declared == "string":
        annotation = "str | datetime" if schema.get("format") == "date-time" else "str"
        return annotation, ("eq", "ne", *_ORDERED_OPERATORS, "in", "exists", "contains")
    if declared == "integer":
        return "int", ("eq", "ne", *_ORDERED_OPERATORS, "in", "exists")
    if declared == "number":
        return "int | str", ("eq", "ne", *_ORDERED_OPERATORS, "in", "exists")
    if declared == "boolean":
        return "bool", ("eq", "ne", "in", "exists")
    return "str | int | bool", ("eq", "ne", "in", "exists")


def _filter_parameters(world: World, kind: str) -> tuple[list[str], list[str]]:
    """Keyword filters and selectable field names for one kind, deterministically.

    A leaf spelled with the documented escape (`keyword_name`: `self_`,
    `class_`, `status__ne_`) never repeats the `self` parameter or collides
    with another leaf's operator-suffixed keyword.
    """

    parameters: list[str] = []
    fields: list[str] = ["subject_id"]
    for leaf, predicates in sorted(world._leaf_map(kind).items()):
        fields.extend(predicates)
        spelled = keyword_name(leaf)
        if len(predicates) != 1 or spelled is None or leaf == "subject_id":
            continue
        fields.append(leaf)
        annotation, operators = _value_annotation(world.claim_type(predicates[0]))
        for operator in operators:
            name = spelled if operator == "eq" else f"{spelled}__{operator}"
            if operator == "in":
                value = f"Sequence[{annotation}]"
            elif operator == "exists":
                value = "bool"
            elif operator == "contains":
                value = "str"
            else:
                value = annotation
            parameters.append(f"{name}: {value} | None = ...,")
    for operator in ("eq", "ne", *_ORDERED_OPERATORS, "in", "contains"):
        name = "subject_id" if operator == "eq" else f"subject_id__{operator}"
        value = "Sequence[str]" if operator == "in" else "str"
        parameters.append(f"{name}: {value} | None = ...,")
    return parameters, sorted(set(fields), key=lambda item: item.encode("utf-8"))


def _query_members(world: World, kind: str, body: _Body, *, include_run: bool) -> None:
    query = _query_class_name(kind)
    parameters, fields = _filter_parameters(world, kind)
    body.declare_lines(
        ["def where(", "    self,", "    *,", *(f"    {item}" for item in parameters)]
        + [f") -> {query}: ..."]
    )
    spelled = ", ".join(f'"{name}"' for name in fields)
    body.declare_lines(
        ["def select(", f"    self, *fields: Literal[{spelled}]", f") -> {query}: ..."]
    )
    if include_run:
        body.declare(f"def order_by(self, *fields: str) -> {query}: ...")
        body.declare(f"def limit(self, count: int) -> {query}: ...")
        body.declare("def request(self) -> PlaybillQueryRequestV1: ...")
        body.declare("def run(self) -> QueryResult: ...")
        body.declare("def __iter__(self) -> Iterator[dict[str, object]]: ...")


def _query_block(world: World, node: _Node) -> list[str]:
    """Type the compact query over one accepted kind, filter by filter."""

    lines = [f"class {_query_class_name(node.path)}:"]
    lines.append(f'    """A compact query over accepted kind {node.path}."""')
    lines.append("")
    body = _Body()
    _query_members(world, node.path, body, include_run=True)
    lines.extend(body.rendered())
    return lines


def _predicate_block(world: World, node: _Node) -> list[str]:
    claim_type = world.claim_type(node.path)
    assert isinstance(claim_type, WorldClaimType)
    lines = [f"class {_class_name(node.path)}(ClaimTypeRef):"]
    lines.append(f'    """{node.path}"""')
    lines.append("")
    lines.append(
        f"    # object_kind={claim_type.object_kind.value}"
        f" cardinality={claim_type.cardinality.value}"
        f" referent_sensitivity={claim_type.referent_sensitivity.value}"
    )
    lines.append(
        "    # permitted_roles=" + ",".join(role.value for role in claim_type.permitted_roles)
    )
    lines.append("    # allowed_subject_kinds=" + ",".join(claim_type.allowed_subject_kinds))
    if claim_type.allowed_object_subject_kinds:
        lines.append(
            "    # allowed_object_subject_kinds="
            + ",".join(claim_type.allowed_object_subject_kinds)
        )
    body = _Body()
    body.declare("address: str")
    body.declare("coordinate: AcceptedCoordinate")
    body.declare("predicate: str")
    body.declare("object_kind: ClaimObjectKind")
    body.declare("cardinality: Cardinality")
    body.declare("allowed_subject_kinds: tuple[str, ...]")
    body.declare("allowed_object_subject_kinds: tuple[str, ...]")
    body.declare("permitted_roles: tuple[ClaimRole, ...]")
    body.declare("referent_sensitivity: ReferentSensitivity")
    body.declare("literal_schema: dict[str, object] | None")
    body.declare("members: tuple[str, ...]")
    body.declare("description: str | None")
    body.declare("member_descriptions: tuple[ClaimTypeMemberDescriptionV1, ...]")
    body.declare("default_role: ClaimRole | None")
    body.declare('evidence_requirement: Literal["none", "self", "captured"]')
    body.declare('revision_evidence: Literal["replace", "accumulate"]')
    body.declare("def __call__(self, value: object) -> LiteralValue: ...")
    if node.subject_kind:
        body.declare(f"as_kind: {_kind_class_name(node.path)}")
        body.declare(
            f"def __getitem__(self, subject_id: str) -> {_subject_class_name(node.path)}: ..."
        )
    _children(node, body, reserved=CLAIM_TYPE_MEMBERS, world=world)
    leaf = node.path.rsplit(".", 1)[-1]
    for member in _sorted(claim_type.members):
        if member in node.children:
            continue
        if member in CLAIM_TYPE_MEMBERS:
            body.note(
                f"enum member {member!r} is shadowed by this ClaimType's own structure; "
                f"mint it with {leaf}({member!r})"
            )
            continue
        if not _is_identifier(member):
            body.note(
                f"enum member {member!r} is not a Python attribute; mint it with {leaf}({member!r})"
            )
            continue
        body.declare(f"{member}: LiteralValue")
    lines.extend(body.rendered())
    return lines


def _blocks(world: World, node: _Node) -> list[list[str]]:
    """Emit every descendant block, children before the parent that names them."""

    blocks: list[list[str]] = []
    for name in _sorted(node.children):
        blocks.extend(_blocks(world, node.children[name]))
    if not node.path:
        return blocks
    if node.subject_kind:
        blocks.append(_subject_block(world, node))
        blocks.append(_query_block(world, node))
    if node.structure is not None:
        if node.subject_kind:
            blocks.append(_namespace_block(world, node, class_name=_kind_class_name(node.path)))
        blocks.append(_predicate_block(world, node))
    else:
        blocks.append(_namespace_block(world, node, class_name=_class_name(node.path)))
    return blocks


def render_world_stub(world: World) -> str:
    """Return the `.pyi` source for one world, byte-identical per coordinate."""

    coordinate = world.coordinate
    lines = [
        f"# {STUB_HEADER_TAG}: generated by `cruxible playbill world stub`.",
        "# Accepted coordinate this world was read at:",
        f"#   git_oid          {coordinate.git_oid}",
        f"#   semantic_root    {coordinate.semantic_root}",
        f"#   generation_root  {coordinate.generation_root}",
        f"#   compiler_digest  {coordinate.compiler_digest}",
        "# Regenerate after every activation. A stub types one coordinate; it",
        "# carries no authority over the next one.",
        "#",
        "# These classes are closed: a name this coordinate did not accept is a",
        "# type error, not `Any`. Bind the runtime object to them once --",
        '#   world = cast("World", pb.world())',
        "# -- and every kind, Subject, predicate and enum member below is checked.",
        "",
        *_STUB_IMPORTS,
        "",
    ]
    for block in _blocks(world, world._root):
        lines.extend(block)
        lines.append("")
    lines.append("class World:")
    lines.append('    """The accepted vocabulary at the coordinate in this header."""')
    lines.append("")
    body = _Body()
    body.declare("coordinate: AcceptedCoordinate")
    body.declare("kinds: tuple[str, ...]")
    body.declare("predicates: tuple[str, ...]")
    body.declare("unstructured_predicates: tuple[str, ...]")
    body.declare("def claim_type(self, predicate: str) -> WorldClaimType: ...")
    body.declare("def kind(self, subject_kind: str) -> KindNamespace: ...")
    body.declare("def stub(self) -> str: ...")
    body.declare(
        "def prefetch(self, *, subjects: Sequence[str | SubjectRef], "
        "predicates: Sequence[str | ClaimTypeRef] = (), page_size: int = 128, "
        "max_claims: int = 4096) -> tuple[ClaimView, ...]: ..."
    )
    _children(world._root, body, reserved=_WORLD_MEMBERS)
    lines.extend(body.rendered())
    lines.append("")
    return "\n".join(lines)


def render_world_stub_for(
    client: CruxibleClient,
    instance_id: str,
    *,
    workspace: str | Path,
) -> str:
    """Render the `.pyi` for one instance's accepted world over an open client.

    The sanctioned entry point for a caller that holds a client rather than a
    `Playbill` -- the CLI leaf, and anything else outside this package -- so no
    caller has to reach for a private constructor. The workspace is only the
    root a relative source selection would resolve against; this reads nothing
    from it, so a directory with no Playbill workspace is fine.
    """

    from cruxible_client.authoring.sdk import Playbill

    return (
        Playbill._from_client(
            client,
            instance_id=instance_id,
            workspace=Path(workspace),
        )
        .world()
        .stub()
    )


__all__ = ["STUB_HEADER_TAG", "render_world_stub", "render_world_stub_for"]
