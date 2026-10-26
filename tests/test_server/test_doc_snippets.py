"""Every ```python block in ``docs/`` runs, in page order, against a scratch instance.

Each page that shows SDK snippets gets one fixture world: the vocabulary and
Subjects its prose describes (the car parts example, or the security kit's
incident fields), authored through the ordinary authoring and write paths.
The page's python blocks then run in order in one namespace, with
``Cruxible.connect()`` answering that world's connection. A statement whose
trailing comment (or the comment line under it) is a Python literal must
evaluate to that literal; a statement commented ``# SomeError: ...`` must
raise it. Every ``cruxible_write`` example a page shows for MCP clients must
validate as a write request and equal one write the page's SDK snippets sent,
so the two forms describe the same change.

A block that cannot run on its own (a fragment, elided arguments) is listed in
``NOT_RUN`` with the reason, so a new block is never skipped silently.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import re
import tokenize
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import cruxible_client
import cruxible_client.contracts.errors as client_errors
from cruxible_client.authoring.files import add_catalog_entry
from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts.write import WriteRequest
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord, lower_claim_type_input
from cruxible_core.runtime.playbill_manager import get_playbill_manager

DOCS = Path(__file__).resolve().parents[2] / "docs"
_FENCE = re.compile(r"^(~~~|```)(\w*)\s*$")
_KEY_FORM = ["nfkc", "trim", "collapse_whitespace", "casefold"]


# -- the page's blocks ---------------------------------------------------------


@dataclass(frozen=True)
class Block:
    doc: str
    line: int
    lang: str
    source: str


def _blocks(doc: Path) -> list[Block]:
    """Top-level fenced blocks, in page order."""

    blocks: list[Block] = []
    fence: str | None = None
    lang = ""
    start = 0
    body: list[str] = []
    for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
        if fence is None:
            opened = _FENCE.match(line)
            if opened:
                fence, lang, start, body = opened.group(1), opened.group(2), number, []
            continue
        if line.strip() == fence:
            blocks.append(Block(doc.name, start, lang, "\n".join(body) + "\n"))
            fence = None
            continue
        body.append(line)
    return blocks


def _python_blocks(doc: Path) -> list[Block]:
    return [block for block in _blocks(doc) if block.lang == "python"]


def _mcp_writes(doc: Path) -> list[tuple[Block, dict[str, Any]]]:
    """The ``cruxible_write`` argument examples a page shows."""

    found = []
    for block in _blocks(doc):
        if block.lang != "json":
            continue
        try:
            value = json.loads(block.source)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "because" in value and ({"changes", "docs"} & set(value)):
            found.append((block, value))
    return found


# -- running a block -----------------------------------------------------------


def _comments(source: str) -> dict[int, str]:
    found: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            found[token.start[0]] = token.string[1:].strip()
    return found


def _expectation(lines: list[str], comments: dict[int, str], node: ast.stmt) -> str | None:
    """The comment on the statement's last line, or on comment-only lines under it."""

    end = node.end_lineno or node.lineno
    if end in comments:
        return comments[end]
    following = []
    for number in range(end + 1, len(lines) + 1):
        stripped = lines[number - 1].strip()
        if not stripped.startswith("#"):
            break
        following.append(stripped[1:].strip())
    return " ".join(following) if following else None


def _expected_error(comment: str | None, namespace: dict[str, Any]) -> type[BaseException] | None:
    """``# Name: ...`` where Name is an exception: a builtin, the SDK's, or a write refusal."""

    if comment is None:
        return None
    named = re.match(r"([A-Za-z_]\w*):", comment)
    if named is None:
        return None
    name = named.group(1)
    error = next(
        (
            found
            for found in (
                namespace.get(name),
                getattr(builtins, name, None),
                getattr(cruxible_client, name, None),
                getattr(client_errors, name, None),
            )
            if found is not None
        ),
        None,
    )
    if isinstance(error, type) and issubclass(error, BaseException):
        return error
    return None


def _expected_value(comment: str | None) -> tuple[bool, Any]:
    if comment is None:
        return False, None
    try:
        return True, ast.literal_eval(comment)
    except (ValueError, SyntaxError):
        return False, None


def run_block(block: Block, namespace: dict[str, Any]) -> None:
    lines = block.source.splitlines()
    comments = _comments(block.source)
    tree = ast.parse(block.source, filename=f"{block.doc}:{block.line}")
    for node in tree.body:
        segment = ast.get_source_segment(block.source, node)
        where = f"{block.doc}:{block.line + node.lineno} {segment}"
        comment = _expectation(lines, comments, node)
        error = _expected_error(comment, namespace)
        if error is not None:
            with pytest.raises(error):
                _run(node, namespace, where)
            continue
        value = _run(node, namespace, where)
        expected, literal = _expected_value(comment)
        if expected and isinstance(node, ast.Expr):
            assert value == literal, f"{where}: {value!r} != {literal!r}"


def _run(node: ast.stmt, namespace: dict[str, Any], where: str) -> Any:
    if isinstance(node, ast.Expr):
        code = compile(ast.Expression(node.value), where, "eval")
        return eval(code, namespace)  # noqa: S307 - the docs' own snippets
    exec(compile(ast.Module([node], type_ignores=[]), where, "exec"), namespace)  # noqa: S102
    return None


# -- the fixture worlds --------------------------------------------------------


def _field(
    predicate: str,
    *,
    many: bool = False,
    schema: dict[str, object] | None = None,
    objects: tuple[str, ...] = (),
    roles: tuple[str, ...] = (),
    key: bool = False,
) -> dict[str, object]:
    record: dict[str, object] = {"predicate": predicate, "cardinality": "many" if many else "one"}
    if objects:
        record["allowed_object_subject_kinds"] = list(objects)
    else:
        record["literal_schema"] = schema or {"type": "string"}
    if roles:
        record["permitted_roles"] = list(roles)
        record["default_role"] = roles[0]
    if key:
        record["literal_schema"] = {"type": "string", "minLength": 1}
        record["canonical_form"] = _KEY_FORM
    return record


def _slug(kind: str) -> dict[str, object]:
    return _field(f"{kind}.slug", key=True)


def _enum(*members: str) -> dict[str, object]:
    return {"type": "string", "enum": sorted(members)}


def car_parts_fields(*, category: tuple[str, ...]) -> list[dict[str, object]]:
    """The car parts vocabulary of Building team knowledge ("Modeling habits")."""

    return [
        _slug("catalog.part"),
        _field("catalog.part.title"),
        _field("catalog.part.category", schema=_enum(*category)),
        _field("catalog.part.fits", many=True, objects=("catalog.vehicle",)),
        _field("catalog.part.replaces", many=True, objects=("catalog.part",)),
        _slug("catalog.vehicle"),
        _field("catalog.vehicle.make"),
        _field("catalog.vehicle.model"),
        _field("catalog.vehicle.year", schema={"type": "integer"}),
        _slug("catalog.decision"),
        _field("catalog.decision.summary"),
        _field("catalog.decision.status", schema=_enum("proposed", "accepted", "superseded")),
        _field("catalog.decision.governs", many=True, objects=("catalog.part", "catalog.vehicle")),
        _field("catalog.decision.amends", many=True, objects=("catalog.decision",)),
        _field("catalog.decision.ruling"),
        _slug("catalog.note"),
        _field("catalog.note.title"),
        _field(
            "catalog.note.about",
            many=True,
            objects=("catalog.part", "catalog.vehicle"),
            roles=("normative", "observation"),
        ),
    ]


CAR_KINDS = {
    "catalog.part": ["catalog.part.slug"],
    "catalog.vehicle": ["catalog.vehicle.slug"],
    "catalog.decision": ["catalog.decision.slug"],
    "catalog.note": ["catalog.note.slug"],
}

BRAKES = ("brakes", "filters", "lighting", "wipers")


@dataclass
class World:
    cx: Cruxible
    workspace: Path
    sent: list[WriteRequest] = field(default_factory=list)


def _connect(client: TestClient, instance_id: str, workspace: Path) -> Cruxible:
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    return Cruxible._from_client(  # noqa: SLF001
        transport, instance_id=instance_id, workspace=workspace, clock=lambda: datetime.now(UTC)
    )


def _define(
    client: TestClient,
    instance_id: str,
    fields: list[dict[str, object]],
    kinds: dict[str, list[str]],
) -> None:
    """Accept the fields and kinds as one authored change set."""

    instance = get_playbill_manager().get(instance_id)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    members: list[dict[str, object]] = [
        {
            "kind": "claim_type",
            "claim_type": lower_claim_type_input(
                ClaimTypeInputRecord.model_validate(record), tree=tree, identity_rules=True
            ).model_dump(mode="json"),
        }
        for record in fields
    ]
    members += [
        {"kind": "subject_kind", "name": kind, "keys": keys, "uniqueness": "unique"}
        for kind, keys in kinds.items()
    ]
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    landed = transport.submit_authoring_input(
        instance_id,
        input={"kind": "change_set", "rationale": "The page's vocabulary.", "members": members},
    )
    assert landed.outcome.status == "landed", landed.model_dump(mode="json")


def _page(path: Path, text: str, workspace: Path, *, name: str) -> None:
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    add_catalog_entry(workspace, name=name, path=target, fields={})


def _car_world(
    client: TestClient, instance_id: str, workspace: Path, *, category: tuple[str, ...] = BRAKES
) -> Cruxible:
    _define(client, instance_id, car_parts_fields(category=category), CAR_KINDS)
    _page(Path("docs/fitment.md"), "# Fitment notes\n", workspace, name="fitment")
    _page(Path("docs/decisions.md"), "# Decisions\n", workspace, name="decisions")
    cx = _connect(client, instance_id, workspace)
    seeded = (
        cx.changes(because="The catalog as the examples start.")
        .set("catalog.vehicle/honda-civic-2019", "make", "Honda")
        .set("catalog.vehicle/honda-civic-2019", "model", "Civic")
        .set("catalog.vehicle/honda-civic-2019", "year", 2019)
        .set("catalog.part/bp-1142", "title", "Front brake pads")
        .set("catalog.part/bp-1142", "category", "brakes")
        .set("catalog.part/bp-2210", "title", "Ceramic front brake pads")
        .set("catalog.part/bp-2210", "category", "brakes")
        .set("catalog.part/af-208", "title", "Air filter")
        .set("catalog.part/af-208", "category", "filters")
        .set("catalog.part/wb-330", "title", "Wiper blade")
        .set("catalog.part/wb-330", "category", "wipers")
        .write()
    )
    assert seeded.status == "accepted", seeded
    return cx


def car_parts(client: TestClient, instance_id: str, workspace: Path) -> Cruxible:
    return _car_world(client, instance_id, workspace)


def split_category(client: TestClient, instance_id: str, workspace: Path) -> Cruxible:
    """Evolving your vocabulary: brakes widened to pads and rotors, a ruling in a field."""

    cx = _car_world(
        client, instance_id, workspace, category=(*BRAKES, "brake-pads", "brake-rotors")
    )
    ruling = (
        "We stock ceramic front pads (bp-2210) as the default for the 2019 Civic instead of "
        "the semi-metallic bp-1142. Customers complained about brake dust and noise."
    )
    seeded = (
        cx.changes(because="The ruling as first recorded.", subject=DEFAULT_PADS)
        .set("status", "accepted")
        .set("ruling", ruling)
        .write()
    )
    assert seeded.status == "accepted", seeded
    return cx


DEFAULT_PADS = "catalog.decision/ceramic-pads-default"


# -- the pages -----------------------------------------------------------------

WorldBuilder = Callable[[TestClient, str, Path], Cruxible]

#: doc -> the world its python blocks run against.
PAGES: dict[str, WorldBuilder] = {
    "building-team-knowledge.md": car_parts,
    "concepts.md": car_parts,
    "documents-and-notes.md": car_parts,
    "evolving-your-vocabulary.md": split_category,
    "for-ai-agents.md": car_parts,
    "lines-and-automation.md": car_parts,
}

#: (doc, block line) -> why that block is not run.
NOT_RUN: dict[tuple[str, int], str] = {
    ("lines-and-automation.md", 60): (
        "the Procedure's shape: its contracts and budget are elided (PATH, SUMMARY, budget=...)"
    ),
}

#: docs whose python blocks are all fragments of a larger program.
NOT_RUN_DOCS: dict[str, str] = {
    "procedure-source.md": (
        "Procedure source reference: fragments and @procedure bodies compiled by the "
        "source compiler, never run as Python against an instance"
    ),
}


def test_a_commented_value_or_error_is_checked() -> None:
    checked = "x = 2\nx + 1    # 3\n[].pop()    # IndexError: raised\n"
    run_block(Block("t.md", 1, "python", checked), {})
    with pytest.raises(AssertionError, match="2 != 3"):
        run_block(Block("t.md", 1, "python", "x = 2\nx    # 3\n"), {})
    with pytest.raises(pytest.fail.Exception):
        run_block(Block("t.md", 1, "python", "1    # IndexError: raised\n"), {})
    run_block(Block("t.md", 1, "python", "x = (1,\n     2)\nx\n# (1, 2)\n"), {})


def test_every_python_block_in_docs_is_run_or_listed() -> None:
    unlisted = []
    for doc in sorted(DOCS.glob("*.md")):
        if doc.name in NOT_RUN_DOCS:
            continue
        for block in _python_blocks(doc):
            if doc.name not in PAGES and (doc.name, block.line) not in NOT_RUN:
                unlisted.append(f"{doc.name}:{block.line}")
    assert not unlisted, f"python blocks no page world runs: {unlisted}"
    for doc, line in NOT_RUN:
        assert any(b.line == line for b in _python_blocks(DOCS / doc)), (doc, line)


def _comparable(request: WriteRequest) -> dict[str, Any]:
    dumped = request.model_dump(mode="json")
    return {
        "because": dumped["because"],
        "subject": dumped.get("subject"),
        "changes": dumped.get("changes") or [],
        "docs": dumped.get("docs") or [],
    }


@pytest.fixture
def world(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[WorldBuilder], World]]:
    client, instance_id, _key = playbill_http

    def build(builder: WorldBuilder) -> World:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        cx = builder(client, instance_id, workspace)
        built = World(cx=cx, workspace=workspace)
        original = cx._write  # noqa: SLF001

        def recording(request: WriteRequest) -> Any:
            built.sent.append(request)
            return original(request)

        monkeypatch.setattr(cx, "_write", recording)
        monkeypatch.setattr(Cruxible, "connect", classmethod(lambda cls, *a, **k: cx))
        monkeypatch.chdir(workspace)
        return built

    yield build


@pytest.mark.parametrize("doc", sorted(PAGES))
def test_page_python_blocks_run_in_order(doc: str, world: Callable[[WorldBuilder], World]) -> None:
    built = world(PAGES[doc])
    namespace: dict[str, Any] = {"__name__": f"docs.{doc}"}
    for block in _python_blocks(DOCS / doc):
        if (doc, block.line) in NOT_RUN:
            continue
        run_block(block, namespace)
    sent = [_comparable(request) for request in built.sent]
    for block, arguments in _mcp_writes(DOCS / doc):
        request = WriteRequest.model_validate({**arguments, "surface": "mcp"})
        assert _comparable(request) in sent, (
            f"{doc}:{block.line}: the MCP example writes what no SDK snippet on the page wrote"
        )
