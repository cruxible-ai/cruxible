"""Freeze the complete sanctioned persistent derivative-text writer inventory.

Door A guarantees that every persistent derivative-text writer routes through a
shared framing primitive. It does not require each caller to invoke the lower-level
frame assertion directly; the shared primitive owns that verification boundary.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from pathlib import Path

import pytest

from cruxible_client.authoring.blocks import repin_projection_block
from cruxible_client.authoring.insertions import apply_playbill_publication
from cruxible_core.playbill.authoring.insertions import build_publication_preparation

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIMITIVE_DEFINITION = (
    REPO_ROOT / "packages/cruxible-client/src/cruxible_client/contracts/declared_blocks.py"
)
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
SANCTIONED_WRITERS: dict[str, tuple[Callable[..., object], str]] = {
    "packages/cruxible-client/src/cruxible_client/authoring/blocks.py::repin_projection_block": (
        repin_projection_block,
        "render_projection_opening",
    ),
    "packages/cruxible-client/src/cruxible_client/authoring/insertions.py::"
    "apply_playbill_publication": (apply_playbill_publication, "frame_projection_block"),
    "src/cruxible_core/playbill/authoring/insertions.py::build_publication_preparation": (
        build_publication_preparation,
        "frame_projection_block",
    ),
}


def _projection_primitive_callers(
    *,
    repo_root: Path = REPO_ROOT,
    source_roots: tuple[Path, ...] | None = None,
    primitive_definition: Path = PRIMITIVE_DEFINITION,
) -> set[str]:
    callers: set[str] = set()
    roots = source_roots or (repo_root / "src", repo_root / "packages")
    for source_root in roots:
        for path in source_root.rglob("*.py"):
            if path.resolve() == primitive_definition.resolve():
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            relative = path.relative_to(repo_root).as_posix()
            scopes: list[str] = []
            direct_aliases = {
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if alias.name in PRIMITIVES
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
                        and (node.func.id in PRIMITIVES or node.func.id in direct_aliases)
                    ) or (isinstance(node.func, ast.Attribute) and node.func.attr in PRIMITIVES)
                    if is_primitive:
                        scope = scopes[-1] if scopes else "<module>"
                        callers.add(f"{relative}::{scope}")
                    self.generic_visit(node)

            Visitor().visit(tree)
    return callers


def _assert_only_sanctioned_callers(callers: set[str]) -> None:
    assert callers == set().union(*SANCTIONED_CALLERS.values())


def test_sanctioned_writer_inventory_matches_primitive_callers() -> None:
    """Anchor Door A at shared framing primitives, not direct assertion spelling."""

    expected_callers = set().union(*SANCTIONED_CALLERS.values())

    assert set(SANCTIONED_WRITERS) == expected_callers
    for writer, primitive in SANCTIONED_WRITERS.values():
        assert f"{primitive}(" in inspect.getsource(writer)


def test_projection_primitive_callers_equal_the_two_writer_inventory() -> None:
    assert set(SANCTIONED_CALLERS) == {"projection_repin", "publication_v2"}
    _assert_only_sanctioned_callers(_projection_primitive_callers())


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
