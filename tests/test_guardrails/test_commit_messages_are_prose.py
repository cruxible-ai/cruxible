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

# The one call that reads a raw commit object at all, and the only two line
# prefixes it is allowed to interpret.
COMMIT_OBJECT_READER = ("cat-file", "commit")
IDENTITY_PREFIXES = ("author ", "committer ")


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


def test_only_one_call_reads_a_raw_commit_object_and_it_reads_only_identities() -> None:
    """`commit_timestamps` is the sole reader, and it never looks past the header.

    Reading the commit object is legitimate -- the author and committer instants
    are Git's own facts and live nowhere else -- but the same bytes carry the
    message, so this pins that the parser stops at the two identity lines.
    """

    readers = []
    for path in _source_files():
        module = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.List)
            and [
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ][:2]
            == list(COMMIT_OBJECT_READER)
        ]
        if calls:
            readers.append(path)
    assert [path.name for path in readers] == ["git.py"]

    module = ast.parse(readers[0].read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "commit_timestamps"
    )
    literals = {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert IDENTITY_PREFIXES[0] in literals and IDENTITY_PREFIXES[1] in literals
    assert not {value for value in literals if value.startswith("message")}


def test_the_ledger_exposes_no_commit_message_reader() -> None:
    from cruxible_core.playbill.git import GitLedger

    named = [name for name in dir(GitLedger) if "message" in name.lower()]
    assert named == []
