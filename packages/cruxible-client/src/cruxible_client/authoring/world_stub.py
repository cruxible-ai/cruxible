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

_NAMESPACE_MEMBERS = frozenset({"define", "subject_ids", "subject_kind"})
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
    "",
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
    "from cruxible_client.contracts.projection import AcceptedCoordinate",
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

    def note(self, line: str) -> None:
        self._lines.append(f"    # {line}")

    def rendered(self) -> list[str]:
        if self._statements:
            return list(self._lines)
        return [*self._lines, "    ..."]


def _children(node: _Node, body: _Body, *, reserved: frozenset[str]) -> None:
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
    for leaf, predicates in sorted(world._leaf_map(node.path).items()):
        if leaf in reachable or len(predicates) != 1:
            continue
        body.note(
            f"predicate leaf {leaf!r} is not readable as an attribute; reach it with "
            f"subject[{predicates[0]!r}]"
        )
    lines.extend(body.rendered())
    return lines


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
    else:
        body.declare("subject_kind: None")
    _children(node, body, reserved=_NAMESPACE_MEMBERS)
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
    body.declare("def __call__(self, value: object) -> LiteralValue: ...")
    if node.subject_kind:
        body.declare(f"as_kind: {_kind_class_name(node.path)}")
        body.declare(
            f"def __getitem__(self, subject_id: str) -> {_subject_class_name(node.path)}: ..."
        )
    _children(node, body, reserved=CLAIM_TYPE_MEMBERS)
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
