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
        store_names: list[set[str]] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                scopes.append(node.name)
                store_names.append(set())
                self.generic_visit(node)
                store_names.pop()
                scopes.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self.visit_FunctionDef(node)

            def visit_Assign(self, node: ast.Assign) -> None:
                if (
                    store_names
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "claim_attestation_evidence_store"
                ):
                    store_names[-1].update(
                        target.id for target in node.targets if isinstance(target, ast.Name)
                    )
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                direct = (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"append", "duplicate"}
                    and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Attribute)
                    and node.func.value.func.attr == "claim_attestation_evidence_store"
                )
                bound = (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"append", "duplicate"}
                    and isinstance(node.func.value, ast.Name)
                    and bool(store_names)
                    and node.func.value.id in store_names[-1]
                )
                if direct or bound:
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


def test_evidence_writer_guard_catches_a_store_bound_to_a_local(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    source_root = tmp_path / "src" / "cruxible_core"
    source_root.mkdir(parents=True)
    (source_root / "rogue.py").write_text(
        "def rogue(instance, attestation, account):\n"
        "    store = instance.claim_attestation_evidence_store()\n"
        "    store.append(attestation=attestation, verification_account=account, note=None)\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(_store_gateway_calls.__globals__, "SOURCE_ROOT", source_root)
    monkeypatch.setitem(_store_gateway_calls.__globals__, "REPO_ROOT", tmp_path)

    assert _store_gateway_calls() == {
        ("src/cruxible_core/rogue.py::rogue", "append"),
    }
