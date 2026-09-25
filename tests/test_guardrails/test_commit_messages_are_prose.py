"""Guardrail: a Playbill commit message is prose, and nothing ever reads it back.

The ledger's commit messages became a review summary so that a reviewer with
nothing but Git can read a proposal. That only stays true while they are prose:
the moment one caller parses a subject line for a disposition, or greps a body
for a path, the message has quietly become a wire format with no schema, no
version, and no canonical bytes -- and the evidence store, the candidate record,
and the note refs stop being the only places a fact about a proposal lives.

This guardrail states the rule as a property of the source tree rather than of
any single call site, so a new Git invocation anywhere in the package has to
answer it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    REPO_ROOT / "src",
    REPO_ROOT / "packages" / "cruxible-client" / "src",
)

# Every Git spelling that would hand a commit message back to a caller. The
# `--format`/`--pretty` placeholders are the direct route; `--oneline` is the
# same thing with the subject baked in.
MESSAGE_PLACEHOLDERS = (
    "%s",
    "%b",
    "%B",
    "%(subject",
    "%(body",
    "%(contents",
    "%(trailers",
    "--pretty",
    "--oneline",
    "--format=%(describe",
)

# Raw commit objects are read only through the ledger's hash-checked batch
# reader, which answers `(type, bytes)`; a function interprets a commit when it
# gates on that type being "commit". These are every such function, and the
# only header lines each may interpret. A direct `cat-file commit` would bypass
# both the hash check and this inventory.
DIRECT_COMMIT_OBJECT_READ = ("cat-file", "commit")
COMMIT_HEADER_READERS = {
    "parent_of": (b"parent ",),
    "_commit_tree": (b"tree ",),
    "main_history": (b"parent ",),
    "commit_timestamps": ("author ", "committer "),
}


def _source_files() -> tuple[Path, ...]:
    return tuple(
        sorted(path for root in SOURCE_ROOTS for path in root.rglob("*.py")),
    )


def _executable_strings(module: ast.Module) -> tuple[str, ...]:
    """Every string literal the code actually evaluates, docstrings excluded.

    The rule is about what the package ASKS Git for, so it is stated over
    literals that can reach a command line. Prose that merely names a
    placeholder -- this guardrail's own docstring, or a comment explaining why
    the message is not parsed -- is documentation, and comments never enter the
    tree at all.
    """

    documentation = {
        id(node.body[0].value)
        for node in ast.walk(module)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return tuple(
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
    )


@pytest.mark.parametrize("path", _source_files(), ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_no_source_file_asks_git_for_a_commit_message(path: Path) -> None:
    literals = _executable_strings(ast.parse(path.read_text(encoding="utf-8")))
    for placeholder in MESSAGE_PLACEHOLDERS:
        offenders = [value for value in literals if placeholder in value]
        assert not offenders, (
            f"{path.relative_to(REPO_ROOT)} names the Git message placeholder {placeholder!r}. "
            "Commit messages are prose for reviewers; read the fact from the evidence store, "
            "the candidate record, or a note ref instead."
        )


def _gates_on_a_commit_object(function: ast.FunctionDef) -> bool:
    """`found[0] != "commit"`: the batch reader's type slot compared to a commit."""

    return any(
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Subscript)
        and isinstance(node.left.slice, ast.Constant)
        and node.left.slice.value == 0
        and any(
            isinstance(item, ast.Constant) and item.value == "commit" for item in node.comparators
        )
        for node in ast.walk(function)
    )


def _is_header_cut(node: ast.AST) -> bool:
    """`split(b"\\n\\n", 1)[0]` (the header block) or `split(b"\\n", 1)[0]` (its first line)."""

    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == 0
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "split"
        and [arg.value for arg in node.value.args if isinstance(arg, ast.Constant)]
        in ([b"\n\n", 1], [b"\n", 1])
    )


def test_every_raw_commit_read_stops_at_the_header_lines_it_names() -> None:
    """Only the named header readers interpret a commit, and none looks past the header.

    Reading a commit object is legitimate -- its tree, parents, and author and
    committer instants are Git's own facts and live nowhere else -- but the same
    bytes carry the message. Every read goes through the hash-checked batch
    reader, so this pins each function that interprets a commit it read there:
    it cuts the bytes at the header before reading them, and matches only the
    header lines it is named for.
    """

    direct = []
    readers: dict[str, tuple[Path, ast.FunctionDef]] = {}
    helpers: dict[Path, dict[str, ast.FunctionDef]] = {}
    for path in _source_files():
        module = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.List)
            and [
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ][:2]
            == list(DIRECT_COMMIT_OBJECT_READ)
            for node in ast.walk(module)
        ):
            direct.append(path)
        helpers[path] = {
            node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
        }
        for node in ast.walk(module):
            if isinstance(node, ast.FunctionDef) and _gates_on_a_commit_object(node):
                readers[node.name] = (path, node)
    assert direct == []
    assert set(readers) == set(COMMIT_HEADER_READERS)
    assert {path.name for path, _function in readers.values()} == {"git.py"}

    for name, (path, function) in readers.items():
        # A module-level helper the reader hands the bytes to is part of the read.
        scope = [function] + [
            helpers[path][node.func.id]
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in helpers[path]
        ]
        nodes = [node for part in scope for node in ast.walk(part)]
        assert any(_is_header_cut(node) for node in nodes), name
        prefixes = {
            node.args[0].value
            for node in nodes
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        assert prefixes == set(COMMIT_HEADER_READERS[name]), name
        literals = {
            node.value
            for node in nodes
            if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)
        }
        assert not {
            value
            for value in literals
            if (value.decode("latin-1") if isinstance(value, bytes) else value).startswith(
                ("message", "encoding", "gpgsig")
            )
        }, name


def test_the_ledger_exposes_no_commit_message_reader() -> None:
    from cruxible_core.ledger.git import GitLedger

    named = [name for name in dir(GitLedger) if "message" in name.lower()]
    assert named == []
