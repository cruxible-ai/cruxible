"""Freeze the sanctioned evidence-plane ClaimAttestation append path.

This is intentionally independent of the Door-A derivative-text inventory: the
attestation ledger writes attributed evidence events, never substrate text.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "cruxible_core"


def _scoped_calls(*, called_name: str) -> set[str]:
    callers: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(REPO_ROOT).as_posix()
        scopes: list[str] = []

        class Visitor(ast.NodeVisitor):
            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                scopes.append(node.name)
                self.generic_visit(node)
                scopes.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

            def visit_Call(self, node: ast.Call) -> None:
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                if name == called_name:
                    callers.add(f"{relative}::{scopes[-1] if scopes else '<module>'}")
                self.generic_visit(node)

        Visitor().visit(tree)
    return callers


def _store_gateway_calls() -> set[tuple[str, str]]:
    callers: set[tuple[str, str]] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(REPO_ROOT).as_posix()
        scopes: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                scopes.append(node.name)
                self.generic_visit(node)
                scopes.pop()

            def visit_Call(self, node: ast.Call) -> None:
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"append", "duplicate"}
                    and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Attribute)
                    and node.func.value.func.attr == "claim_attestation_evidence_store"
                ):
                    callers.add(
                        (
                            f"{relative}::{scopes[-1] if scopes else '<module>'}",
                            node.func.attr,
                        )
                    )
                self.generic_visit(node)

        Visitor().visit(tree)
    return callers


def test_claim_attestation_evidence_append_has_one_served_gateway() -> None:
    assert _scoped_calls(called_name="service_append_claim_attestation") == {
        "src/cruxible_core/runtime/playbill_api.py::playbill_append_claim_attestation"
    }
    assert _store_gateway_calls() == {
        (
            "src/cruxible_core/service/playbill_claim_attestations.py::"
            "service_append_claim_attestation",
            "duplicate",
        ),
        (
            "src/cruxible_core/service/playbill_claim_attestations.py::"
            "service_append_claim_attestation",
            "append",
        ),
    }


def test_claim_attestation_store_is_constructed_only_by_the_instance() -> None:
    assert _scoped_calls(called_name="ClaimAttestationEvidenceStore") == {
        "src/cruxible_core/playbill/instance.py::claim_attestation_evidence_store"
    }
