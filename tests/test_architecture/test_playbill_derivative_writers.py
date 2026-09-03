"""Freeze the complete sanctioned persistent derivative-text writer inventory.

Door A guarantees that every persistent derivative-text writer routes through a
verifying primitive: frame_projection_block re-parses the framed bytes and proves
exactly one block with the exact stamp and body digest, and the repin pipeline must
spell assert_projection_block_frame, which performs the same proof on its output.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from pathlib import Path

import pytest

from cruxible_client.authoring.blocks import repin_projection_block, sync_projection_blocks
from cruxible_client.authoring.insertions import apply_playbill_publication
from cruxible_core.playbill.authoring.insertions import build_publication_preparation
from cruxible_core.playbill.candidate_cards import derive_candidate_cards

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIMITIVE_DEFINITION = (
    REPO_ROOT / "packages/cruxible-client/src/cruxible_client/contracts/declared_blocks.py"
)
PRIMITIVES = {"frame_projection_block", "render_projection_opening"}
# The card renderers live beside the writer that calls them, so the scan skips
# nothing: every caller in the tree is enumerated and must be sanctioned.
CARD_PRIMITIVE_DEFINITION: Path | None = None
# One persistent tree writer, plus the read-only block-sync renderer that shows a
# card without ever writing one into a candidate tree.
SANCTIONED_CARD_CALLERS = {
    "src/cruxible_core/playbill/candidate_cards.py::derive_candidate_cards",
    "src/cruxible_core/service/playbill_projection_sync.py::_read_artifact_backing",
}
SANCTIONED_CALLERS = {
    "projection_repin": {
        "packages/cruxible-client/src/cruxible_client/authoring/blocks.py::repin_projection_block",
    },
    "projection_sync": {
        "packages/cruxible-client/src/cruxible_client/authoring/blocks.py::sync_projection_blocks",
    },
    "publication_v2": {
        "packages/cruxible-client/src/cruxible_client/authoring/insertions.py::"
        "apply_playbill_publication",
        "src/cruxible_core/playbill/authoring/insertions.py::build_publication_preparation",
    },
}
SANCTIONED_WRITERS: dict[str, tuple[Callable[..., object], str, tuple[str, ...]]] = {
    "packages/cruxible-client/src/cruxible_client/authoring/blocks.py::repin_projection_block": (
        repin_projection_block,
        "assert_projection_block_frame",
        ("replace one declared block's opening marker",),
    ),
    "packages/cruxible-client/src/cruxible_client/authoring/blocks.py::sync_projection_blocks": (
        sync_projection_blocks,
        "frame_projection_block",
        ("atomically synchronize accepted publication block bytes",),
    ),
    "packages/cruxible-client/src/cruxible_client/authoring/insertions.py::"
    "apply_playbill_publication": (
        apply_playbill_publication,
        "frame_projection_block",
        ("apply one prepared publication to client-observed bytes",),
    ),
    "src/cruxible_core/playbill/authoring/insertions.py::build_publication_preparation": (
        build_publication_preparation,
        "frame_projection_block",
        (
            "prepare a full-file publication postimage from an insertion expectation",
            "reproduce and verify the exact framed block bytes from a preparation",
        ),
    ),
}
CARD_DERIVATIVE_WRITERS: dict[str, tuple[Callable[..., object], tuple[str, ...]]] = {
    "src/cruxible_core/playbill/candidate_cards.py::derive_candidate_cards": (
        derive_candidate_cards,
        (
            "render changed artifact cards through render_candidate_card",
            "render removed artifact cards through render_removal_card",
        ),
    ),
}


def _projection_primitive_callers(
    *,
    repo_root: Path = REPO_ROOT,
    source_roots: tuple[Path, ...] | None = None,
    primitive_definition: Path = PRIMITIVE_DEFINITION,
) -> set[str]:
    return _primitive_callers(
        primitives=PRIMITIVES,
        repo_root=repo_root,
        source_roots=source_roots,
        primitive_definition=primitive_definition,
    )


def _primitive_callers(
    *,
    primitives: set[str],
    repo_root: Path = REPO_ROOT,
    source_roots: tuple[Path, ...] | None = None,
    primitive_definition: Path | None = PRIMITIVE_DEFINITION,
) -> set[str]:
    callers: set[str] = set()
    roots = source_roots or (repo_root / "src", repo_root / "packages")
    for source_root in roots:
        for path in source_root.rglob("*.py"):
            # `None` excludes nothing: the primitive's own definition is scanned
            # like every other module.
            excluded = primitive_definition is not None and (
                path.resolve() == primitive_definition.resolve()
            )
            if excluded:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            relative = path.relative_to(repo_root).as_posix()
            scopes: list[str] = []
            direct_aliases = {
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if alias.name in primitives
            }

            class Visitor(ast.NodeVisitor):
                def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                    scopes.append(node.name)
                    self.generic_visit(node)
                    scopes.pop()

                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    self._visit_function(node)

                def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                    self._visit_function(node)

                def visit_Lambda(self, node: ast.Lambda) -> None:
                    scopes.append(f"<lambda@{node.lineno}>")
                    self.generic_visit(node)
                    scopes.pop()

                def visit_Call(self, node: ast.Call) -> None:
                    is_primitive = (
                        isinstance(node.func, ast.Name)
                        and (node.func.id in primitives or node.func.id in direct_aliases)
                    ) or (isinstance(node.func, ast.Attribute) and node.func.attr in primitives)
                    if is_primitive:
                        scope = scopes[-1] if scopes else "<module>"
                        callers.add(f"{relative}::{scope}")
                    self.generic_visit(node)

            Visitor().visit(tree)
    return callers


CARD_PRIMITIVES = {"render_candidate_card", "render_removal_card"}


def _card_primitive_callers(
    *,
    repo_root: Path = REPO_ROOT,
    primitive_definition: Path = CARD_PRIMITIVE_DEFINITION,
) -> set[str]:
    """Scan the tree for every caller of a card-rendering primitive."""

    return _primitive_callers(
        primitives=CARD_PRIMITIVES,
        repo_root=repo_root,
        primitive_definition=primitive_definition,
    )


def _assert_only_sanctioned_callers(callers: set[str]) -> None:
    assert callers == set().union(*SANCTIONED_CALLERS.values())


def test_sanctioned_writer_inventory_matches_primitive_callers() -> None:
    """Anchor Door A at shared framing primitives, not direct assertion spelling."""

    expected_callers = set().union(*SANCTIONED_CALLERS.values())

    assert set(SANCTIONED_WRITERS) == expected_callers
    for writer, primitive, operations in SANCTIONED_WRITERS.values():
        assert f"{primitive}(" in inspect.getsource(writer)
        assert operations
    assert (
        len(
            SANCTIONED_WRITERS[
                "src/cruxible_core/playbill/authoring/insertions.py::build_publication_preparation"
            ][2]
        )
        == 2
    )


def test_projection_primitive_callers_equal_the_two_writer_inventory() -> None:
    assert set(SANCTIONED_CALLERS) == {
        "projection_repin",
        "projection_sync",
        "publication_v2",
    }
    _assert_only_sanctioned_callers(_projection_primitive_callers())


def test_candidate_card_derivative_writer_is_the_only_one_in_the_tree() -> None:
    """Scan for card-rendering callers instead of restating the inventory.

    The previous shape asserted a dict literal equalled its own single key and
    grepped the writer for the two calls it obviously makes, so a second card
    writer added anywhere in the tree would not have been caught.
    """

    assert set(CARD_DERIVATIVE_WRITERS) == {
        "src/cruxible_core/playbill/candidate_cards.py::derive_candidate_cards"
    }
    assert _card_primitive_callers() == SANCTIONED_CARD_CALLERS
    writer, operations = next(iter(CARD_DERIVATIVE_WRITERS.values()))
    source = inspect.getsource(writer)
    assert "render_candidate_card(" in source
    assert "render_removal_card(" in source
    assert operations


def test_projection_primitive_guard_catches_every_noncanonical_spelling(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "src" / "guard_evasions"
    fixtures = {
        "attribute.py": """
import cruxible_client.contracts.declared_blocks as declared_blocks

def attribute_call():
    return declared_blocks.frame_projection_block()
""",
        "aliased.py": """
from cruxible_client.contracts.declared_blocks import frame_projection_block as _framer

def aliased_call():
    return _framer()
""",
        "module_level.py": """
from cruxible_client.contracts.declared_blocks import render_projection_opening

render_projection_opening()
""",
        "lambda_scope.py": """
from cruxible_client.contracts.declared_blocks import frame_projection_block

lambda_call = lambda: frame_projection_block()
""",
        "declared_blocks.py": """
from cruxible_client.contracts.declared_blocks import frame_projection_block

def same_basename_call():
    return frame_projection_block()
""",
    }
    fixture_root.mkdir(parents=True)
    for name, source in fixtures.items():
        (fixture_root / name).write_text(source)

    callers = _projection_primitive_callers(
        repo_root=tmp_path,
        source_roots=(tmp_path / "src",),
    )

    assert any(caller.endswith("attribute.py::attribute_call") for caller in callers)
    assert any(caller.endswith("aliased.py::aliased_call") for caller in callers)
    assert any(caller.endswith("module_level.py::<module>") for caller in callers)
    assert any("lambda_scope.py::<lambda@" in caller for caller in callers)
    assert any(caller.endswith("declared_blocks.py::same_basename_call") for caller in callers)
    with pytest.raises(AssertionError):
        _assert_only_sanctioned_callers(callers)
