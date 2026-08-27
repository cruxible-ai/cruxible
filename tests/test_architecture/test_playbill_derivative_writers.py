"""Freeze the complete sanctioned persistent derivative-text writer inventory."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from cruxible_client.authoring.blocks import repin_projection_block
from cruxible_client.authoring.insertions import apply_playbill_publication

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIMITIVES = {"frame_projection_block", "render_projection_opening"}
SANCTIONED_CALLERS = {
    "projection_repin": {
        "packages/cruxible-client/src/cruxible_client/authoring/blocks.py::repin_projection_block",
    },
    "publication_v2": {
        "packages/cruxible-client/src/cruxible_client/authoring/insertions.py::"
        "apply_playbill_publication",
        "src/cruxible_core/playbill/authoring/insertions.py::build_publication_preparation",
    },
}


def _projection_primitive_callers() -> set[str]:
    callers: set[str] = set()
    for source_root in (REPO_ROOT / "src", REPO_ROOT / "packages"):
        for path in source_root.rglob("*.py"):
            if path.name == "declared_blocks.py":
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            relative = path.relative_to(REPO_ROOT).as_posix()
            functions: list[str] = []

            class Visitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    functions.append(node.name)
                    self.generic_visit(node)
                    functions.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node: ast.Call) -> None:
                    name = node.func.id if isinstance(node.func, ast.Name) else None
                    if name in PRIMITIVES and functions:
                        callers.add(f"{relative}::{functions[-1]}")
                    self.generic_visit(node)

            Visitor().visit(tree)
    return callers


def test_every_sanctioned_derivative_writer_uses_the_shared_block_assertion() -> None:
    writers = {
        "projection_repin": repin_projection_block,
        "publication_v2": apply_playbill_publication,
    }

    assert set(writers) == {"projection_repin", "publication_v2"}
    for writer in writers.values():
        assert "assert_projection_block_frame(" in inspect.getsource(writer)


def test_projection_primitive_callers_equal_the_two_writer_inventory() -> None:
    assert set(SANCTIONED_CALLERS) == {"projection_repin", "publication_v2"}
    assert _projection_primitive_callers() == set().union(*SANCTIONED_CALLERS.values())
